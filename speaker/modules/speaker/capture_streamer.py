from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import AsyncIterator, Optional

from modules.speaker.config import AppConfig
from modules.speaker.events import WakewordEvent, PlaybackCompleteEvent, SpeechStreamSentEvent, BargeinEvent
from modules.speaker.events_bus import EventBus
from modules.speaker.grpc_client import OrchestratorGrpcClient, OrchestratorGrpcConfig
from modules.speaker.state_machine import SpeakerState, SpeakerStateMachine
from modules.speaker.vad import EnergyVAD, VADConfig


class CaptureStreamer:
    def __init__(
        self,
        cfg: AppConfig,
        capture_q: asyncio.Queue[bytes],
        playback_q: asyncio.Queue[tuple],
        bus: EventBus,
        prebuffer: Optional[deque] = None,
        playing_evt: Optional[asyncio.Event] = None,
        capturing_evt: Optional[asyncio.Event] = None,
        last_playback_end: Optional[dict] = None,
        fsm: Optional[SpeakerStateMachine] = None,
        barge_q: Optional[asyncio.Queue] = None,
        barge_in_evt: Optional[asyncio.Event] = None,
    ):
        self.cfg = cfg
        self.capture_q = capture_q
        self.playback_q = playback_q
        self.bus = bus
        self.prebuffer = prebuffer
        self.playing_evt = playing_evt or asyncio.Event()
        self.capturing_evt = capturing_evt or asyncio.Event()
        self.last_playback_end = last_playback_end or {"ts": 0.0}
        self.fsm = fsm or SpeakerStateMachine()
        self.vad = EnergyVAD(
            VADConfig(
                frame_ms=cfg.mic_frame_ms,
                sample_rate_hz=cfg.mic_sample_rate_hz,
                rms_threshold=cfg.vad_rms_threshold,
                silence_hangover_ms=cfg.vad_silence_hangover_ms,
                max_utterance_ms=cfg.vad_max_utterance_ms,
                min_speech_ms=cfg.min_commit_speech_ms,
                adaptive_enable=bool(getattr(cfg, "vad_adaptive_enable", True)),
                adaptive_floor=float(getattr(cfg, "vad_adaptive_floor", 60.0)),
                adaptive_ceiling=float(getattr(cfg, "vad_adaptive_ceiling", 500.0)),
                adaptive_ceiling_high=float(getattr(cfg, "vad_adaptive_ceiling_high", 900.0)),
                adaptive_high_noise_threshold=float(getattr(cfg, "vad_adaptive_high_noise_threshold", 260.0)),
                adaptive_margin=float(getattr(cfg, "vad_adaptive_margin", 1.8)),
                noise_ema_alpha=float(getattr(cfg, "vad_noise_ema_alpha", 0.03)),
                calibration_path=str(getattr(cfg, "vad_calibration_path", "")),
            )
        )
        self.grpc = OrchestratorGrpcClient(
            OrchestratorGrpcConfig(
                target=cfg.server_target,
                session_id=cfg.session_id_base,
                locale=cfg.locale,
                request_partials=cfg.request_partials,
                client_sample_rate_hz=cfg.mic_sample_rate_hz,
                stt_send_timeout_ms=int(getattr(cfg, "stt_send_timeout_ms", 12000)),
                stop_capture_on_first_tts_audio=bool(getattr(cfg, "stop_capture_on_first_tts_audio", True)),
                stop_on_thinking=bool(getattr(cfg, "stop_on_thinking", True)),
                keepalive_time_ms=int(getattr(cfg, "grpc_keepalive_time_ms", 5000)),
                keepalive_timeout_ms=int(getattr(cfg, "grpc_keepalive_timeout_ms", 3000)),
                connect_max_attempts=int(getattr(cfg, "grpc_connect_max_attempts", 8)),
                connect_ready_timeout_ms=int(getattr(cfg, "grpc_connect_ready_timeout_ms", 4000)),
                verbose=bool(getattr(cfg, "verbose", False)),
                stream_log_every_n=max(1, int(getattr(cfg, "stream_log_every_n", 50))),
            )
        )
        self.log = logging.getLogger("speaker.capture")
        self.followup_count = 0
        self.pending_wake = False
        self.frames_streamed = 0
        self._barge_q = barge_q
        self._barge_in_evt = barge_in_evt
        self._barge_in_prebuffer: list[bytes] = []
        self._conversation_id: str = self._load_conversation_id()

    async def _enqueue_playback_chunk(self, turn_id: str, chunk: tuple) -> None:
        high = max(1, int(getattr(self.cfg, "playback_backpressure_high_watermark", 384)))
        if self.playback_q.qsize() < high:
            await self.playback_q.put(chunk)
            return

        drop_nonfinal = bool(getattr(self.cfg, "playback_backpressure_drop_nonfinal", True))
        kept: deque[tuple] = deque()
        dropped = 0
        while not self.playback_q.empty():
            try:
                item = self.playback_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            item_turn, _pcm, _sr, item_last = item
            if item_turn != turn_id:
                dropped += 1
                continue
            if drop_nonfinal and not item_last:
                dropped += 1
                continue
            kept.append(item)

        while kept:
            try:
                self.playback_q.put_nowait(kept.popleft())
            except asyncio.QueueFull:
                break

        if dropped:
            self.log.warning("playback backpressure dropped=%s turn_id=%s", dropped, turn_id)
        await self.playback_q.put(chunk)

    async def run(self):
        barge_task: Optional[asyncio.Task] = None
        if bool(getattr(self.cfg, "barge_in_enable", True)) and self._barge_q is not None:
            barge_task = asyncio.create_task(self._barge_in_monitor(), name="barge_in_monitor")
        try:
            async for evt in self.bus.subscribe(topic="control"):
                if self.capturing_evt.is_set():
                    self.log.debug("skipping event while already capturing")
                    continue
                if isinstance(evt, WakewordEvent):
                    self._conversation_id = uuid.uuid4().hex
                    asyncio.get_running_loop().run_in_executor(
                        None, self._save_conversation_id, self._conversation_id
                    )
                    self.log.info("wakeword event received conversation_id=%s", self._conversation_id)
                    self.followup_count = 0
                    await self.fsm.force(SpeakerState.CAPTURING)
                    await self._capture_and_stream(reset_context=True)
                    await self.fsm.force(SpeakerState.IDLE)
                elif isinstance(evt, BargeinEvent):
                    prebuffer = self._barge_in_prebuffer[:]
                    self._barge_in_prebuffer.clear()
                    if self._barge_in_evt is not None:
                        self._barge_in_evt.clear()
                    self.log.info("barge-in: starting new turn conversation_id=%s", self._conversation_id)
                    await self.fsm.force(SpeakerState.CAPTURING)
                    await self._capture_and_stream(reset_context=False, prebuffer_frames=prebuffer or None)
                    await self.fsm.force(SpeakerState.IDLE)
                elif isinstance(evt, PlaybackCompleteEvent):
                    self.log.debug("playback complete event turn_id=%s", evt.turn_id)
                    # Skip synthetic chime turns and any internal __ prefix turns.
                    if evt.turn_id and (
                        evt.turn_id.startswith("chime-") or evt.turn_id.startswith("__")
                    ):
                        continue
                    if not self.cfg.followup_enable:
                        continue
                    if self.cfg.followup_max < 0 or self.followup_count < self.cfg.followup_max:
                        prebuffer = await self._await_followup_speech()
                        if prebuffer is None:
                            continue
                        self.followup_count += 1
                        await self.fsm.force(SpeakerState.CAPTURING)
                        await self._capture_and_stream(reset_context=False, prebuffer_frames=prebuffer)
                        await self.fsm.force(SpeakerState.IDLE)
        finally:
            if barge_task is not None:
                barge_task.cancel()
                await asyncio.gather(barge_task, return_exceptions=True)

    async def _capture_and_stream(self, *, reset_context: bool, prebuffer_frames: Optional[list[bytes]] = None):
        turn_id = f"{self.cfg.session_id_base}-{int(time.monotonic() * 1000)}"
        turn_start = time.monotonic()
        self.log.info("capture start turn_id=%s reset=%s", turn_id, reset_context)
        self.frames_streamed = 0
        self.capturing_evt.set()

        # Per-turn cancel event wired to barge-in so the gRPC client can send
        # a Cancel message to the server when the user interrupts playback.
        if self._barge_in_evt is not None:
            self._barge_in_evt.clear()
        turn_cancel_evt = asyncio.Event()

        async def _barge_relay():
            if self._barge_in_evt is None:
                return
            await self._barge_in_evt.wait()
            turn_cancel_evt.set()

        barge_relay_task: asyncio.Task = asyncio.create_task(_barge_relay(), name="barge_relay")

        first_chunk: Optional[tuple] = None
        skip_until = 0.0

        if prebuffer_frames is None:
            # Drop any stale frames (e.g., wakeword audio) before we begin.
            drained = 0
            max_drain = max(0, int(self.cfg.drain_max_frames))
            try:
                while drained < max_drain:
                    self.capture_q.get_nowait()
                    drained += 1
            except asyncio.QueueEmpty:
                pass
            if drained:
                self.log.debug("drained stale frames=%s before start", drained)

        # Wait briefly after playback to avoid recording speaker output.
        if self.last_playback_end.get("ts", 0):
            delay = (self.last_playback_end["ts"] + (self.cfg.capture_skip_playback_ms / 1000.0)) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        if prebuffer_frames is None:
            # Require a small window of silence before we start yielding frames to gRPC.
            if self.last_playback_end.get("ts", 0):
                skip_until = self.last_playback_end["ts"] + (self.cfg.capture_skip_playback_ms / 1000.0)

            async def _wait_for_settle():
                needed = max(1, int(self.cfg.settle_silence_ms / self.cfg.mic_frame_ms))
                max_s = max(0.1, float(getattr(self.cfg, "settle_max_ms", 2000)) / 1000.0)
                deadline = time.monotonic() + max_s
                consecutive = 0
                while True:
                    if time.monotonic() >= deadline:
                        self.log.debug("settle timeout after %.0fms; proceeding", max_s * 1000)
                        break
                    try:
                        frame = await asyncio.wait_for(self.capture_q.get(), timeout=0.25)
                    except asyncio.TimeoutError:
                        consecutive = 0
                        continue
                    if skip_until and time.monotonic() < skip_until:
                        continue
                    if self.vad.is_speech(frame):
                        consecutive = 0
                        continue
                    consecutive += 1
                    if consecutive >= needed:
                        break

            await _wait_for_settle()

        async def mic_iter() -> AsyncIterator[bytes]:
            start_ts = time.monotonic()
            min_cap_ms = max(1, int(self.cfg.min_capture_ms))
            followup_deadline = start_ts + (self.cfg.followup_idle_ms / 1000.0)
            first_committed_speech_ms = -1
            if self.prebuffer:
                # Snapshot the prebuffer to avoid concurrent mutation.
                for frame in list(self.prebuffer):
                    yield frame
            if prebuffer_frames:
                for frame in prebuffer_frames:
                    yield frame

            committed = False
            self.vad.reset()
            while True:
                try:
                    frame = await asyncio.wait_for(self.capture_q.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    elapsed_ms = (now - start_ts) * 1000.0
                    if elapsed_ms >= min_cap_ms and now > followup_deadline:
                        break
                    continue

                if skip_until and time.monotonic() < skip_until:
                    # Drop frames that are likely playback bleed
                    continue

                should_end, info = self.vad.accept_frame(frame)
                if info.get("speech", False):
                    speech_ms = info.get("speech_frames", 0) * self.cfg.mic_frame_ms
                    if speech_ms >= self.cfg.min_commit_speech_ms:
                        if not committed:
                            first_committed_speech_ms = int((time.monotonic() - start_ts) * 1000)
                        committed = True

                now = time.monotonic()
                elapsed_ms = (now - start_ts) * 1000.0
                if should_end and committed and elapsed_ms >= min_cap_ms:
                    yield frame
                    committed = True
                    self.frames_streamed += 1
                    break

                yield frame
                self.frames_streamed += 1
            if first_committed_speech_ms >= 0:
                self.log.info("turn stage first_committed_speech_ms=%s turn_id=%s", first_committed_speech_ms, turn_id)

        try:
            tts_stream = self.grpc.run_turn(
                mic_iter=mic_iter(),
                cancel_evt=turn_cancel_evt,
                turn_id=turn_id,
                reset_context=reset_context,
                session_id=self._conversation_id or None,
            )

            async for item in tts_stream:
                first_chunk = item
                break
            await self.fsm.force(SpeakerState.WAITING_TTS if first_chunk is None else SpeakerState.PLAYING)
            if first_chunk is not None:
                self.log.info(
                    "turn stage first_tts_audio_ms=%d turn_id=%s",
                    int((time.monotonic() - turn_start) * 1000),
                    turn_id,
                )

            async def audio_iter():
                pcm, sr, is_last = first_chunk
                yield turn_id, pcm, sr, is_last
                if is_last:
                    return
                async for pcm, sr, is_last in tts_stream:
                    yield turn_id, pcm, sr, is_last
                    if is_last:
                        return

            if first_chunk is None:
                self.log.warning("no audio from server turn_id=%s", turn_id)
            else:
                async for chunk in audio_iter():
                    # Abort immediately if barge-in fires while TTS is still streaming.
                    if self._barge_in_evt is not None and self._barge_in_evt.is_set():
                        self.log.info("barge-in during TTS stream; aborting turn_id=%s", turn_id)
                        break
                    await self._enqueue_playback_chunk(turn_id, chunk)

                await self.bus.publish(
                    SpeechStreamSentEvent(turn_id=turn_id, followup_count=self.followup_count),
                    topic="speech",
                )
                self.log.info(
                    "capture sent frames=%s turn_id=%s total_ms=%d",
                    self.frames_streamed,
                    turn_id,
                    int((time.monotonic() - turn_start) * 1000),
                )
        except Exception as exc:
            await self.fsm.force(SpeakerState.ERROR)
            self.log.exception("capture error: %r", exc)
        finally:
            barge_relay_task.cancel()
            await asyncio.gather(barge_relay_task, return_exceptions=True)
            self.capturing_evt.clear()
            asyncio.get_running_loop().run_in_executor(None, self.vad.save_calibration)

    async def _await_followup_speech(self) -> Optional[list[bytes]]:
        timeout_s = self.cfg.followup_idle_ms / 1000.0
        deadline = time.monotonic() + timeout_s
        needed = max(1, int(self.cfg.followup_min_consecutive_speech_frames))
        prebuffer_frames = max(1, int(self.cfg.followup_prebuffer_ms / self.cfg.mic_frame_ms))
        buf = deque(maxlen=prebuffer_frames)
        consecutive = 0
        skip_until = 0.0
        if self.last_playback_end.get("ts", 0):
            skip_until = self.last_playback_end["ts"] + (self.cfg.capture_skip_playback_ms / 1000.0)

        while time.monotonic() < deadline:
            try:
                frame = await asyncio.wait_for(self.capture_q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                consecutive = 0
                continue
            if skip_until and time.monotonic() < skip_until:
                continue
            buf.append(frame)
            if self.vad.is_speech(frame):
                consecutive += 1
                if consecutive >= needed:
                    return list(buf)
            else:
                consecutive = 0
        return None

    async def _barge_in_monitor(self) -> None:
        if self._barge_q is None or self._barge_in_evt is None:
            return
        min_frames = max(1, int(getattr(self.cfg, "barge_in_min_speech_frames", 4)))
        prebuf_ms = max(1, int(getattr(self.cfg, "barge_in_prebuffer_ms", 200)))
        prebuf_cap = max(1, prebuf_ms // self.cfg.mic_frame_ms)

        # Dedicated VAD so barge-in detection doesn't affect main utterance VAD state.
        barge_vad = EnergyVAD(VADConfig(
            frame_ms=self.vad.cfg.frame_ms,
            sample_rate_hz=self.vad.cfg.sample_rate_hz,
            rms_threshold=self.vad.cfg.rms_threshold,
            adaptive_enable=self.vad.cfg.adaptive_enable,
            adaptive_floor=self.vad.cfg.adaptive_floor,
            adaptive_ceiling=self.vad.cfg.adaptive_ceiling,
            adaptive_ceiling_high=self.vad.cfg.adaptive_ceiling_high,
            adaptive_high_noise_threshold=self.vad.cfg.adaptive_high_noise_threshold,
            adaptive_margin=self.vad.cfg.adaptive_margin,
            noise_ema_alpha=self.vad.cfg.noise_ema_alpha,
        ))

        settle_s = max(0.0, float(getattr(self.cfg, "barge_in_settle_ms", 600)) / 1000.0)
        buf: deque = deque(maxlen=prebuf_cap)
        consecutive = 0
        playback_start_ts = 0.0

        while True:
            # Only act during active TTS playback when not already capturing.
            if not self.playing_evt.is_set() or self.capturing_evt.is_set():
                consecutive = 0
                buf.clear()
                playback_start_ts = 0.0
                await asyncio.sleep(0.02)
                continue

            # Track when this playback session started; drain stale queued frames
            # that accumulated while playback was inactive so they don't cause an
            # immediate false-positive barge-in on the leading edge.
            if playback_start_ts == 0.0:
                playback_start_ts = time.monotonic()
                while True:
                    try:
                        self._barge_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

            try:
                frame = await asyncio.wait_for(self._barge_q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if not self.playing_evt.is_set():
                    consecutive = 0
                    buf.clear()
                continue

            # Re-check guards after the await
            if not self.playing_evt.is_set() or self.capturing_evt.is_set():
                consecutive = 0
                buf.clear()
                continue

            # Ignore mic frames during the initial settle window (guards against
            # chime bleed-through and residual wakeword audio in the buffer).
            if settle_s > 0 and (time.monotonic() - playback_start_ts) < settle_s:
                continue

            buf.append(frame)
            if barge_vad.is_speech(frame):
                consecutive += 1
                if consecutive >= min_frames:
                    self.log.info("barge-in detected consecutive_frames=%s", consecutive)
                    consecutive = 0
                    self._barge_in_prebuffer = list(buf)
                    buf.clear()
                    # Cancel ongoing playback.
                    self._barge_in_evt.set()
                    # Drain queued future chunks so playback stops promptly.
                    while not self.playback_q.empty():
                        try:
                            self.playback_q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await self.bus.publish(BargeinEvent(), topic="control")
                    # Back off until capturing starts to prevent re-triggering.
                    for _ in range(150):
                        await asyncio.sleep(0.02)
                        if self.capturing_evt.is_set():
                            break
            else:
                consecutive = 0

    def _load_conversation_id(self) -> str:
        path = str(getattr(self.cfg, "conversation_id_state_path", ""))
        if not path:
            return ""
        try:
            from pathlib import Path
            p = Path(path)
            if p.exists():
                cid = p.read_text(encoding="utf-8").strip()
                if cid:
                    self.log.debug("restored conversation_id=%s", cid) if hasattr(self, "log") else None
                    return cid
        except Exception:
            pass
        return ""

    def _save_conversation_id(self, cid: str) -> None:
        path = str(getattr(self.cfg, "conversation_id_state_path", ""))
        if not path:
            return
        try:
            from pathlib import Path
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(cid, encoding="utf-8")
        except Exception:
            pass
