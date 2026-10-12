from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional, Tuple

import numpy as np

from modules.speaker.audio_playback import AudioPlayback, PlaybackFormat
from modules.speaker.events import PlaybackCompleteEvent, PlaybackStartEvent
from modules.speaker.events_bus import EventBus


class PlaybackWorker:
    def __init__(
        self,
        playback_q: asyncio.Queue[Tuple[Optional[str], bytes, int, bool]],
        bus: EventBus,
        playing_evt: asyncio.Event,
        *,
        audio_driver: str = "pulse",
        playback_device: str = "",
        prefer_native_alsa: bool = True,
        playback_channels: int = 1,
        alsa_period_time_us: int = 0,
        alsa_buffer_time_us: int = 0,
        cpu_affinity: str = "",
        nice: int = 0,
        rt_priority: int = 0,
        playback_state: Optional[dict] = None,
        barge_in_evt: Optional[asyncio.Event] = None,
    ):
        self.playback_q = playback_q
        self.bus = bus
        self.playback = AudioPlayback(
            driver=audio_driver,
            device=playback_device,
            log_prefix="play",
            prefer_native_alsa=prefer_native_alsa,
            alsa_period_time_us=alsa_period_time_us,
            alsa_buffer_time_us=alsa_buffer_time_us,
            cpu_affinity=cpu_affinity,
            nice=nice,
            rt_priority=rt_priority,
        )
        self.log = logging.getLogger("speaker.playback")
        self.playing_evt = playing_evt
        self._stash: deque[Tuple[Optional[str], bytes, int, bool]] = deque()
        self._last_end: Optional[dict] = None
        self.playback_channels = playback_channels
        self._playback_state = playback_state or {"ts": 0.0, "rms": 0.0}
        self._barge_in_evt = barge_in_evt

    async def run(self, last_playback_end: Optional[dict] = None):
        self._last_end = last_playback_end
        try:
            while True:
                turn_id, pcm, sr, is_last = await self._next_chunk()
                await self._play_stream(turn_id, pcm, sr, is_last)
        finally:
            await self.playback.stop("worker-exit")

    async def _next_chunk(self) -> Tuple[Optional[str], bytes, int, bool]:
        if self._stash:
            return self._stash.popleft()
        return await self.playback_q.get()

    async def _play_stream(self, turn_id: Optional[str], first_pcm: bytes, sr: int, is_last: bool):
        cancel_evt = asyncio.Event()

        async def pcm_iter():
            self._record_playback_chunk(first_pcm)
            yield first_pcm
            if is_last:
                return
            while True:
                t_id, pcm, sr2, last = await self._next_chunk()
                # Keep chunks for other turns for later playback instead of dropping.
                if turn_id is not None and t_id != turn_id:
                    self._stash.append((t_id, pcm, sr2, last))
                    continue
                yield pcm
                self._record_playback_chunk(pcm)
                if last:
                    return

        async def _barge_watcher():
            if self._barge_in_evt is None:
                return
            await self._barge_in_evt.wait()
            cancel_evt.set()

        barge_task = asyncio.create_task(_barge_watcher(), name="barge_watcher")
        try:
            await self.bus.publish(PlaybackStartEvent(turn_id=turn_id), topic="playback")
            self.playing_evt.set()
            await self.playback.play_raw_stream(
                pcm_iter(),
                fmt=PlaybackFormat(sample_rate_hz=sr, channels=self.playback_channels),
                cancel_evt=cancel_evt,
            )
        except Exception as e:
            self.log.exception("playback error turn_id=%s err=%r", turn_id, e)
        finally:
            barge_task.cancel()
            await asyncio.gather(barge_task, return_exceptions=True)
            # Barge-in interrupted this stream: discard any chunks stashed for
            # future turns so they don't play before the new barge-in response.
            if cancel_evt.is_set():
                self._stash.clear()
            self.playing_evt.clear()
            if self._last_end is not None:
                self._last_end["ts"] = time.monotonic()
            evt = PlaybackCompleteEvent(turn_id=turn_id)
            await self.bus.publish(evt, topic="control")
            await self.bus.publish(evt, topic="playback")
            self.log.info("playback complete turn_id=%s", turn_id)

    def _record_playback_chunk(self, pcm: bytes) -> None:
        if not pcm:
            return
        try:
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            self._playback_state["rms"] = float(np.sqrt(np.mean(samples ** 2))) if samples.size > 0 else 0.0
            self._playback_state["ts"] = time.monotonic()
        except Exception:
            pass
