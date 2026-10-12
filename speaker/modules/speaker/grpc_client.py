# grpc client module
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Tuple

import grpc
from modules.speaker.protos import orchestrator_pb2 as pb
from modules.speaker.protos import orchestrator_pb2_grpc as pb_grpc


@dataclass
class OrchestratorGrpcConfig:
    target: str
    session_id: str
    locale: str = "en-US"
    request_partials: bool = True
    client_sample_rate_hz: int = 16000
    stt_send_timeout_ms: int = 12000
    stop_capture_on_first_tts_audio: bool = True
    verbose: bool = False
    stream_log_every_n: int = 50
    connect_initial_backoff_ms: int = 200
    connect_max_backoff_ms: int = 3000
    connect_max_attempts: int = 8
    connect_ready_timeout_ms: int = 4000
    keepalive_time_ms: int = 5000
    keepalive_timeout_ms: int = 3000
    keepalive_permit_without_calls: bool = True
    stop_on_thinking: bool = True


class OrchestratorGrpcClient:
    def __init__(self, cfg: OrchestratorGrpcConfig):
        self.cfg = cfg
        self.log = logging.getLogger("speaker.grpc")
        self.channel: Optional[grpc.aio.Channel] = None
        self.stub: Optional[pb_grpc.OrchestratorStub] = None

        self.tx_q: asyncio.Queue[Optional[pb.ClientMessage]] = asyncio.Queue(maxsize=2048)
        self.rx_q: asyncio.Queue[Optional[pb.ServerMessage]] = asyncio.Queue(maxsize=2048)

        self._rx_task: Optional[asyncio.Task] = None
        self._connected = False
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _drain_queue_nowait(self, q: asyncio.Queue) -> None:
        try:
            while True:
                q.get_nowait()
        except asyncio.QueueEmpty:
            return

    async def connect(self):
        if self._connected:
            return

        # Cancel any still-running rx_loop from a previous connection and wait for
        # its finally block to execute.  That finally block puts a None sentinel
        # into rx_q; we must let it run before we drain so the sentinel is
        # included in the drain and cannot arrive later to silently abort run_turn().
        if self._rx_task is not None and not self._rx_task.done():
            self._rx_task.cancel()
            await asyncio.gather(self._rx_task, return_exceptions=True)
            self._rx_task = None

        # Close any stale channel left over from a previous (now-dead) connection.
        if self.channel is not None:
            try:
                await self.channel.close()
            except Exception:
                pass
            self.channel = None
            self.stub = None

        # Drain queues after the old rx_loop is fully done so its None sentinel
        # (if any) is already present and gets flushed here.
        self._drain_queue_nowait(self.tx_q)
        self._drain_queue_nowait(self.rx_q)

        max_attempts = max(1, int(self.cfg.connect_max_attempts))
        delay_s = max(0.05, float(self.cfg.connect_initial_backoff_ms) / 1000.0)
        max_delay_s = max(delay_s, float(self.cfg.connect_max_backoff_ms) / 1000.0)
        ready_timeout_s = max(0.5, float(self.cfg.connect_ready_timeout_ms) / 1000.0)
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            options = [
                ("grpc.keepalive_time_ms", int(self.cfg.keepalive_time_ms)),
                ("grpc.keepalive_timeout_ms", int(self.cfg.keepalive_timeout_ms)),
                ("grpc.keepalive_permit_without_calls", 1 if bool(self.cfg.keepalive_permit_without_calls) else 0),
                ("grpc.http2.max_pings_without_data", 0),
                ("grpc.http2.min_time_between_pings_ms", int(self.cfg.keepalive_time_ms)),
                ("grpc.http2.min_ping_interval_without_data_ms", int(self.cfg.keepalive_time_ms)),
            ]
            self.channel = grpc.aio.insecure_channel(self.cfg.target, options=options)
            self.stub = pb_grpc.OrchestratorStub(self.channel)
            self.log.info("connect target=%s attempt=%s/%s", self.cfg.target, attempt, max_attempts)
            try:
                await asyncio.wait_for(self.channel.channel_ready(), timeout=ready_timeout_s)
            except Exception as exc:
                last_exc = exc if isinstance(exc, Exception) else RuntimeError(repr(exc))
                self.log.warning("channel not ready: %r", exc)
                try:
                    await self.channel.close()
                except Exception:
                    pass
                self.channel = None
                self.stub = None
                if attempt >= max_attempts:
                    break
                await asyncio.sleep(delay_s)
                delay_s = min(max_delay_s, delay_s * 2.0)
                continue
            break
        else:
            raise RuntimeError("failed to connect to orchestrator")

        if self.channel is None or self.stub is None:
            raise RuntimeError(f"failed to connect to orchestrator: {last_exc!r}")

        async def req_iter():
            while True:
                msg = await self.tx_q.get()
                if msg is None:
                    return
                yield msg

        stream = self.stub.Converse(req_iter())

        async def rx_loop():
            try:
                async for msg in stream:
                    await self.rx_q.put(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    self.rx_q.put_nowait(exc)
                except Exception:
                    pass
                self.log.warning("rx_loop error: %r", exc)
            finally:
                try:
                    self.rx_q.put_nowait(None)
                except Exception:
                    pass
                # allow next turn to reconnect if the stream ends
                self._connected = False
                self._rx_task = None
                self.log.info("rx_loop ended; marking disconnected")

        self._rx_task = asyncio.create_task(rx_loop(), name="grpc_rx")
        self._connected = True

    async def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            try:
                self.tx_q.put_nowait(None)
            except Exception:
                pass

            if self._rx_task and not self._rx_task.done():
                self._rx_task.cancel()
                await asyncio.gather(self._rx_task, return_exceptions=True)

            if self.channel is not None:
                await self.channel.close()
        finally:
            self._connected = False
            self._closing = False
            self._rx_task = None
            self.channel = None
            self.stub = None
            try:
                self.rx_q.put_nowait(None)
            except Exception:
                pass
            self._drain_queue_nowait(self.tx_q)
            self._drain_queue_nowait(self.rx_q)

    async def run_turn(
        self,
        *,
        mic_iter: AsyncIterator[bytes],
        cancel_evt: asyncio.Event,
        turn_id: Optional[str] = None,
        reset_context: bool = False,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Tuple[bytes, int, bool]]:
        if not self._connected:
            await self.connect()

        session_id = session_id or self.cfg.session_id
        tid = turn_id or f"{session_id}-{int(time.monotonic() * 1000)}"
        self.log.info(
            "send Start session_id=%s turn_id=%s sr=%s locale=%s",
            session_id,
            tid,
            self.cfg.client_sample_rate_hz,
            self.cfg.locale,
        )
        await self.tx_q.put(
            pb.ClientMessage(
                start=pb.Start(
                    session_id=session_id,
                    turn_id=tid,
                    reset_context=reset_context,
                    sample_rate_hz=self.cfg.client_sample_rate_hz,
                    locale=self.cfg.locale,
                    request_partials=self.cfg.request_partials,
                )
            )
        )

        current_turn_id: Optional[str] = tid
        turn_rx_start = time.monotonic()
        first_transcript_ms = -1
        first_reply_ms = -1
        first_audio_ms = -1
        rx_audio_chunks = 0
        rx_audio_bytes = 0
        tx_stop_evt = asyncio.Event()
        eou_sent = False

        async def tx_audio():
            nonlocal eou_sent
            frames_sent = 0
            bytes_sent = 0
            start_ts = time.monotonic()
            max_ms = max(0, getattr(self.cfg, "stt_send_timeout_ms", 0))
            self.log.debug("tx_audio start turn_id=%s", tid)
            try:
                async for frame in mic_iter:
                    if cancel_evt.is_set():
                        await self.tx_q.put(pb.ClientMessage(cancel=pb.Cancel(reason="barge-in")))
                        return
                    if tx_stop_evt.is_set():
                        break
                    if max_ms and (time.monotonic() - start_ts) * 1000 >= max_ms:
                        tx_stop_evt.set()
                        self.log.info("tx_audio timeout after %sms; sending EOU", max_ms)
                        break
                    await self.tx_q.put(
                        pb.ClientMessage(
                            audio=pb.AudioFrame(
                                pcm_s16le=frame,
                                timestamp_ms=int(time.time() * 1000),
                            )
                        )
                    )
                    frames_sent += 1
                    bytes_sent += len(frame)
                    if self.cfg.verbose and (frames_sent == 1 or frames_sent % max(1, int(self.cfg.stream_log_every_n)) == 0):
                        self.log.debug("tx_audio frames=%s bytes=%s", frames_sent, bytes_sent)
                if not eou_sent:
                    await self.tx_q.put(pb.ClientMessage(eou=pb.EndOfUtterance()))
                    eou_sent = True
                self.log.debug("tx_audio done frames=%s bytes=%s", frames_sent, bytes_sent)
            except Exception as exc:
                self.log.warning("tx_audio error: %r", exc)
                try:
                    await self.tx_q.put(pb.ClientMessage(cancel=pb.Cancel(reason="tx_error")))
                except Exception:
                    pass

        tx_task = asyncio.create_task(tx_audio(), name="tx_audio")

        try:
            while True:
                # Check for barge-in cancellation before (re-)blocking on the queue.
                # tx_audio only checks cancel_evt while consuming mic frames; once EOU
                # is sent it exits, so we must send the Cancel from here instead.
                if cancel_evt.is_set():
                    try:
                        self.tx_q.put_nowait(pb.ClientMessage(cancel=pb.Cancel(reason="barge-in")))
                    except Exception:
                        pass
                    self.log.info("run_turn late-cancel (post-EOU barge-in) turn_id=%s", tid)
                    return
                try:
                    msg = await asyncio.wait_for(self.rx_q.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    return
                if isinstance(msg, Exception):
                    raise msg

                if msg.HasField("state") and msg.state.turn_id and current_turn_id is None:
                    current_turn_id = msg.state.turn_id
                if msg.HasField("state"):
                    s = msg.state
                    if self.cfg.verbose:
                        self.log.debug(
                            "rx state phase=%s turn_id=%s",
                            pb.State.Phase.Name(s.phase),
                            getattr(s, "turn_id", ""),
                        )
                    if (
                        bool(self.cfg.stop_on_thinking)
                        and s.phase == pb.State.THINKING
                        and not tx_stop_evt.is_set()
                    ):
                        tx_stop_evt.set()
                        if not eou_sent:
                            try:
                                await self.tx_q.put(pb.ClientMessage(eou=pb.EndOfUtterance()))
                                eou_sent = True
                                self.log.debug("tx stopped on THINKING state; sent EOU")
                            except Exception as exc:
                                self.log.warning("failed to send EOU on THINKING: %r", exc)

                if msg.HasField("transcript") and msg.transcript.turn_id and current_turn_id is None:
                    current_turn_id = msg.transcript.turn_id
                if first_transcript_ms < 0 and msg.HasField("transcript"):
                    first_transcript_ms = int((time.monotonic() - turn_rx_start) * 1000)
                    self.log.info("turn latency first_transcript_ms=%s turn_id=%s", first_transcript_ms, tid)
                if msg.HasField("transcript") and self.cfg.verbose:
                    t = msg.transcript
                    self.log.debug("rx transcript turn_id=%s final=%s text=%r", t.turn_id, t.is_final, t.text)

                if msg.HasField("reply") and msg.reply.turn_id and current_turn_id is None:
                    current_turn_id = msg.reply.turn_id
                if first_reply_ms < 0 and msg.HasField("reply"):
                    first_reply_ms = int((time.monotonic() - turn_rx_start) * 1000)
                    self.log.info("turn latency first_reply_ms=%s turn_id=%s", first_reply_ms, tid)
                if msg.HasField("reply") and self.cfg.verbose:
                    r = msg.reply
                    self.log.debug("rx reply turn_id=%s final=%s text=%r", r.turn_id, r.is_final, r.text)
                    if not rx_audio_chunks:
                        self.log.debug("reply arrived before any audio (turn_id=%s)", r.turn_id)

                if msg.HasField("error"):
                    err = msg.error.message
                    self.log.warning(
                        "rx error turn_id=%s message=%r",
                        getattr(msg.error, "turn_id", ""),
                        err,
                    )
                    if current_turn_id is None or not msg.error.turn_id or msg.error.turn_id == current_turn_id:
                        raise RuntimeError(err)

                if msg.HasField("audio"):
                    a = msg.audio
                    if first_audio_ms < 0:
                        first_audio_ms = int((time.monotonic() - turn_rx_start) * 1000)
                        self.log.info("turn latency first_audio_ms=%s turn_id=%s", first_audio_ms, tid)
                    if current_turn_id is None and a.turn_id:
                        current_turn_id = a.turn_id

                    rx_audio_chunks += 1
                    rx_audio_bytes += len(a.pcm_s16le or b"")
                    if self.cfg.verbose and (
                        rx_audio_chunks <= 3 or rx_audio_chunks % max(1, int(self.cfg.stream_log_every_n)) == 0
                    ):
                        self.log.debug(
                            "rx_audio chunk=%s bytes=%s sr=%s is_last=%s",
                            rx_audio_chunks,
                            len(a.pcm_s16le),
                            a.sample_rate_hz,
                            a.is_last,
                        )

                    if bool(self.cfg.stop_capture_on_first_tts_audio) and not tx_stop_evt.is_set():
                        tx_stop_evt.set()
                        if not eou_sent:
                            try:
                                await self.tx_q.put(pb.ClientMessage(eou=pb.EndOfUtterance()))
                                eou_sent = True
                                self.log.debug("tx_audio stopped on first tts audio; sent EOU")
                            except Exception as exc:
                                self.log.warning("failed to send EOU after tts start: %r", exc)

                    yield a.pcm_s16le, a.sample_rate_hz, a.is_last
                    if a.is_last:
                        self.log.info(
                            "rx_audio summary chunks=%s bytes=%s first_transcript_ms=%s first_reply_ms=%s first_audio_ms=%s",
                            rx_audio_chunks,
                            rx_audio_bytes,
                            first_transcript_ms,
                            first_reply_ms,
                            first_audio_ms,
                        )
                        return
        finally:
            if not tx_task.done():
                tx_task.cancel()
            await asyncio.gather(tx_task, return_exceptions=True)
