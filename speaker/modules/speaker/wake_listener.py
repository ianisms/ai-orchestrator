from __future__ import annotations

import asyncio
import logging
import time
import numpy as np

from modules.speaker.events import WakewordEvent, PlaybackCompleteEvent
from modules.speaker.state_machine import SpeakerState, SpeakerStateMachine
from modules.speaker.wakeword_loop import WakewordLoop, WakewordConfig
from modules.speaker.events_bus import EventBus


class WakeListener:
    def __init__(
        self,
        cfg: WakewordConfig,
        wake_q: asyncio.Queue[bytes],
        bus: EventBus,
        playback_q: asyncio.Queue[tuple],
        sample_rate: int,
        playing_evt: asyncio.Event,
        fsm: SpeakerStateMachine | None = None,
        chime_freq_hz: float = 880.0,
        chime_ms: int = 140,
        chime_enable: bool = True,
    ):
        self.loop = WakewordLoop(cfg, ww_q=wake_q, events_q=asyncio.Queue(maxsize=256))
        self.bus = bus
        self.playback_q = playback_q
        self.sample_rate = sample_rate
        self.events_q: asyncio.Queue[dict] = self.loop.events_q  # type: ignore[attr-defined]
        self.playing_evt = playing_evt
        self.fsm = fsm or SpeakerStateMachine()
        self.log = logging.getLogger("speaker.wake")
        self._chime_enable = chime_enable
        self._chime_pcm = self._build_chime(chime_freq_hz, chime_ms)

    def _build_chime(self, freq: float, ms: int) -> bytes:
        dur_s = max(0.01, ms / 1000.0)
        t = np.linspace(0, dur_s, int(self.sample_rate * dur_s), False, dtype=np.float32)
        wave = (0.08 * np.sin(2 * np.pi * freq * t)).clip(-1.0, 1.0)
        return (wave * 32767.0).astype(np.int16).tobytes()

    def _chime(self) -> tuple:
        return (None, self._chime_pcm, self.sample_rate, True)

    async def _wait_for_playback_complete(self, turn_id: str, timeout: float = 2.0) -> bool:
        gen = self.bus.subscribe(topic="playback")
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    evt = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    return False
                except asyncio.TimeoutError:
                    return False
                if isinstance(evt, PlaybackCompleteEvent) and evt.turn_id == turn_id:
                    return True
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass

    async def run(self):
        # Run wakeword model
        loop_task = asyncio.create_task(self.loop.run(), name="wake_model")

        try:
            while True:
                evt = await self.events_q.get()
                if evt.get("type") == "wake":
                    score = float(evt.get("score", 0.0))
                    ts = float(evt.get("ts", time.monotonic()))
                    if self.playing_evt.is_set():
                        # Ignore wakes triggered by our own playback
                        self.log.debug("wake ignored: playback active score=%.3f", score)
                        continue
                    chime_id = f"chime-{int(ts * 1000)}"
                    await self.fsm.force(SpeakerState.WAKE_CHIME)
                    if self._chime_enable:
                        # Re-check after the yield point above; playback may have
                        # started between the first guard and fsm.force().
                        if self.playing_evt.is_set():
                            self.log.debug("wake ignored: playback started during fsm transition score=%.3f", score)
                            await self.fsm.force(SpeakerState.IDLE)
                            continue
                        try:
                            _, pcm, sr, is_last = self._chime()
                            await self.playback_q.put((chime_id, pcm, sr, is_last))
                        except Exception as exc:
                            self.log.warning("chime enqueue failed: %r", exc)
                        # Wait for chime playback to finish (best-effort)
                        await self._wait_for_playback_complete(chime_id, timeout=2.0)
                    self.log.info("wake detected score=%.3f", score)
                    await self.bus.publish(WakewordEvent(score=score, ts=ts), topic="control")
                    await self.fsm.force(SpeakerState.IDLE)
        finally:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
