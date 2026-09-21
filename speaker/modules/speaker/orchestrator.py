#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import numpy as np
from collections import deque

from modules.speaker.audio_capture import AudioCapture, MicConfig
from modules.speaker.config import AppConfig
from modules.speaker.events_bus import EventBus
from modules.speaker.wake_listener import WakeListener
from modules.speaker.capture_streamer import CaptureStreamer
from modules.speaker.playback_worker import PlaybackWorker
from modules.speaker.logging_utils import setup_logger
from modules.speaker.reload import watch_for_reload
from modules.speaker.wakeword_loop import WakewordConfig
from modules.speaker.state_machine import SpeakerStateMachine


async def _supervise(
    name: str,
    runner_factory,
    stop_evt: asyncio.Event,
    *,
    restart_delay_s: float = 0.5,
    max_restart_delay_s: float = 5.0,
):
    log = logging.getLogger("speaker.supervisor")
    delay = restart_delay_s
    while not stop_evt.is_set():
        task = asyncio.create_task(runner_factory(), name=name)
        try:
            await task
            if stop_evt.is_set():
                return
            log.warning("%s exited unexpectedly; restarting", name)
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        except Exception:
            log.exception("%s crashed; restarting", name)
        await asyncio.sleep(delay)
        delay = min(max_restart_delay_s, delay * 2.0)


async def tee_mic(
    mic_q: asyncio.Queue[bytes],
    wake_q: asyncio.Queue[bytes],
    utt_q: asyncio.Queue[bytes],
    stop_evt: asyncio.Event,
    playing_evt: asyncio.Event,
    last_playback_end: dict,
    playback_guard_ms: int,
    capturing_evt: asyncio.Event,
    mic_channels: int = 1,
    prebuffer: deque[bytes] | None = None,
    echo_suppress_enable: bool = False,
    echo_suppress_max_attenuation: float = 0.35,
    playback_state: dict | None = None,
    barge_q: asyncio.Queue[bytes] | None = None,
):
    log = logging.getLogger("speaker.tee")
    log_every = 100
    frame_count = 0
    wake_drops = 0
    utt_drops = 0
    while not stop_evt.is_set():
        frame = await mic_q.get()
        if not frame:
            continue

        # During playback, feed barge-in monitor but skip normal utterance path.
        if playing_evt.is_set():
            if barge_q is not None:
                try:
                    barge_q.put_nowait(frame)
                except asyncio.QueueFull:
                    try:
                        barge_q.get_nowait()
                        barge_q.put_nowait(frame)
                    except Exception:
                        pass
            continue

        # Briefly ignore audio right after playback finishes to avoid
        # capturing lingering speaker output.
        if last_playback_end.get("ts", 0) > 0 and not capturing_evt.is_set():
            guard_s = playback_guard_ms / 1000.0
            if time.monotonic() - last_playback_end["ts"] < guard_s:
                continue

        # Downmix to mono if capture provided multi-channel
        if mic_channels > 1:
            try:
                s = np.frombuffer(frame, dtype=np.int16).reshape(-1, mic_channels).astype(np.float32)
                frame = np.clip(s.mean(axis=1), -32768, 32767).astype(np.int16).tobytes()
            except Exception:
                pass

        if prebuffer is not None:
            prebuffer.append(frame)

        if echo_suppress_enable and playback_state:
            try:
                recent_play_ts = float(playback_state.get("ts", 0.0))
                play_rms = float(playback_state.get("rms", 0.0))
                now = time.monotonic()
                if now - recent_play_ts < 0.25 and play_rms > 0:
                    s = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                    mic_rms = float(np.sqrt(np.mean(s ** 2))) if s.size > 0 else 0.0
                    if mic_rms <= play_rms * 1.5:
                        atten = min(max(float(echo_suppress_max_attenuation), 0.0), 0.95)
                        frame = np.clip(s * (1.0 - atten), -32768, 32767).astype(np.int16).tobytes()
            except Exception:
                pass

        frame_count += 1
        if frame_count % log_every == 0:
            try:
                s = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(s ** 2))) if s.size > 0 else 0.0
                log.debug("mic frame=%s rms=%s", frame_count, rms)
            except Exception:
                pass

        # Always feed wakeword
        try:
            wake_q.put_nowait(frame)
        except asyncio.QueueFull:
            wake_drops += 1
            try:
                _ = wake_q.get_nowait()
                wake_q.put_nowait(frame)
            except Exception:
                pass

        # Feed utterance only when not playing (to avoid capturing playback)
        if not playing_evt.is_set():
            try:
                utt_q.put_nowait(frame)
            except asyncio.QueueFull:
                utt_drops += 1
                try:
                    _ = utt_q.get_nowait()
                    utt_q.put_nowait(frame)
                except Exception:
                    pass

        if frame_count % log_every == 0 and (wake_drops or utt_drops):
            log.warning("tee frame drops wake=%s utt=%s", wake_drops, utt_drops)


async def _health_monitor(cfg: AppConfig, stop_evt: asyncio.Event):
    """Periodically probe the gRPC server and log prominently when unreachable."""
    import grpc
    log = logging.getLogger("speaker.health")
    interval_s = float(getattr(cfg, "health_check_interval_s", 10.0))
    timeout_s = float(getattr(cfg, "health_check_timeout_s", 3.0))
    health_path = str(getattr(cfg, "health_path", ""))

    def _write_health(reachable: bool) -> None:
        if not health_path:
            return
        try:
            payload = {
                "ts": time.time(),
                "server_reachable": reachable,
                "server_target": cfg.server_target,
            }
            with open(health_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            pass

    # Create a single persistent channel reused across all health probes.
    # gRPC keepalives handle failure detection; no need to reconnect each interval.
    channel = grpc.aio.insecure_channel(cfg.server_target)
    was_reachable: bool | None = None  # None = not yet probed; forces log+write on first check
    try:
        while not stop_evt.is_set():
            try:
                await asyncio.wait_for(channel.channel_ready(), timeout=timeout_s)
                reachable = True
            except Exception:
                reachable = False

            if was_reachable is None:
                # First probe: always log and write so the health file is valid from startup.
                if reachable:
                    log.info("server health startup: REACHABLE %s", cfg.server_target)
                else:
                    log.error("SERVER UNREACHABLE: %s — check that the server is running", cfg.server_target)
                was_reachable = reachable
                _write_health(reachable)
            elif reachable and not was_reachable:
                log.warning("SERVER REACHABLE: %s", cfg.server_target)
                was_reachable = reachable
                _write_health(reachable)
            elif not reachable and was_reachable:
                log.error("SERVER UNREACHABLE: %s — check that the server is running", cfg.server_target)
                was_reachable = reachable
                _write_health(reachable)

            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await channel.close()
        except Exception:
            pass


async def main():
    cfg = AppConfig.from_yaml()
    setup_logger("speaker", cfg.log_path, level=str(getattr(cfg, "log_level", "INFO")))

    stop_evt = asyncio.Event()
    reload_evt = asyncio.Event()
    playing_evt = asyncio.Event()
    capturing_evt = asyncio.Event()

    mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
    wake_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
    utt_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
    barge_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
    playback_q: asyncio.Queue[tuple] = asyncio.Queue(maxsize=512)
    barge_in_evt: asyncio.Event = asyncio.Event()
    bus = EventBus()
    fsm = SpeakerStateMachine()
    last_playback_end = {"ts": 0.0}
    playback_state = {"ts": 0.0, "rms": 0.0}
    preroll_frames = max(1, int(getattr(cfg, "wake_preroll_ms", 400) / max(1, int(cfg.mic_frame_ms))))
    prebuffer: deque[bytes] = deque(maxlen=preroll_frames)

    try:
        if str(getattr(cfg, "audio_cpu_affinity", "")).strip():
            cpus = {
                int(p.strip())
                for p in str(getattr(cfg, "audio_cpu_affinity", "")).split(",")
                if p.strip().isdigit()
            }
            if cpus:
                os.sched_setaffinity(0, cpus)
        nice_delta = int(getattr(cfg, "audio_nice", 0))
        if nice_delta != 0:
            os.nice(nice_delta)
        rt_prio = int(getattr(cfg, "audio_rt_priority", 0))
        if rt_prio > 0:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(rt_prio))
    except Exception:
        logging.getLogger("speaker").debug("runtime tuning skipped", exc_info=True)

    capture = AudioCapture(
        MicConfig(
            device=cfg.mic_device,
            sample_rate_hz=cfg.mic_sample_rate_hz,
            latency_msec=cfg.mic_latency_msec,
            frame_ms=cfg.mic_frame_ms,
            channels=getattr(cfg, "mic_channels", 1),
            driver=cfg.audio_driver,
            prefer_native_alsa=bool(getattr(cfg, "prefer_native_alsa", True)),
            alsa_period_time_us=getattr(cfg, "alsa_period_time_us", 0),
            alsa_buffer_time_us=getattr(cfg, "alsa_buffer_time_us", 0),
            cpu_affinity=str(getattr(cfg, "audio_cpu_affinity", "")),
            nice=int(getattr(cfg, "audio_nice", 0)),
            rt_priority=int(getattr(cfg, "audio_rt_priority", 0)),
        ),
        frames_q=mic_q,
        log_prefix="mic",
    )

    wake_listener = WakeListener(
        WakewordConfig(
            model_path=cfg.wake_model_path,
            threshold=cfg.wake_threshold,
            refractory_ms=cfg.wake_refractory_ms,
            score_log_every_n=cfg.wake_score_log_every_n,
            sample_rate_hz=cfg.mic_sample_rate_hz,
            infer_chunk_ms=cfg.wake_infer_chunk_ms,
            channels=1,  # wakeword runs on mono
        ),
        wake_q=wake_q,
        bus=bus,
        playback_q=playback_q,
        sample_rate=cfg.mic_sample_rate_hz,
        playing_evt=playing_evt,
        fsm=fsm,
        chime_freq_hz=float(getattr(cfg, "wake_chime_freq_hz", 880.0)),
        chime_ms=int(getattr(cfg, "wake_chime_ms", 140)),
        chime_enable=bool(getattr(cfg, "wake_chime_enable", True)),
    )

    capture_streamer = CaptureStreamer(
        cfg=cfg,
        capture_q=utt_q,
        playback_q=playback_q,
        bus=bus,
        prebuffer=prebuffer,
        playing_evt=playing_evt,
        capturing_evt=capturing_evt,
        last_playback_end=last_playback_end,
        fsm=fsm,
        barge_q=barge_q if bool(getattr(cfg, "barge_in_enable", True)) else None,
        barge_in_evt=barge_in_evt if bool(getattr(cfg, "barge_in_enable", True)) else None,
    )

    playback_worker = PlaybackWorker(
        playback_q=playback_q,
        bus=bus,
        playing_evt=playing_evt,
        audio_driver=cfg.audio_driver,
        playback_device=cfg.playback_device,
        prefer_native_alsa=bool(getattr(cfg, "prefer_native_alsa", True)),
        playback_channels=getattr(cfg, "playback_channels", 1),
        alsa_period_time_us=getattr(cfg, "alsa_period_time_us", 0),
        alsa_buffer_time_us=getattr(cfg, "alsa_buffer_time_us", 0),
        cpu_affinity=str(getattr(cfg, "audio_cpu_affinity", "")),
        nice=int(getattr(cfg, "audio_nice", 0)),
        rt_priority=int(getattr(cfg, "audio_rt_priority", 0)),
        playback_state=playback_state,
        barge_in_evt=barge_in_evt if bool(getattr(cfg, "barge_in_enable", True)) else None,
    )

    async def _run_mic():
        await capture.run()

    async def _run_tee():
        await tee_mic(
            mic_q,
            wake_q,
            utt_q,
            stop_evt,
            playing_evt,
            last_playback_end=last_playback_end,
            playback_guard_ms=getattr(cfg, "playback_guard_ms", 400),
            capturing_evt=capturing_evt,
            mic_channels=getattr(cfg, "mic_channels", 1),
            prebuffer=prebuffer,
            echo_suppress_enable=bool(getattr(cfg, "echo_suppress_enable", False)),
            echo_suppress_max_attenuation=float(getattr(cfg, "echo_suppress_max_attenuation", 0.35)),
            playback_state=playback_state,
            barge_q=barge_q if bool(getattr(cfg, "barge_in_enable", True)) else None,
        )

    async def _run_wake():
        await wake_listener.run()

    async def _run_capture():
        await capture_streamer.run()

    async def _run_playback():
        await playback_worker.run(last_playback_end)

    tasks = [
        asyncio.create_task(_supervise("mic", _run_mic, stop_evt), name="sup_mic"),
        asyncio.create_task(_supervise("tee", _run_tee, stop_evt), name="sup_tee"),
        asyncio.create_task(_supervise("wake", _run_wake, stop_evt), name="sup_wake"),
        asyncio.create_task(_supervise("capture", _run_capture, stop_evt), name="sup_capture"),
        asyncio.create_task(_supervise("playback", _run_playback, stop_evt), name="sup_playback"),
        asyncio.create_task(_health_monitor(cfg, stop_evt), name="health_monitor"),
        asyncio.create_task(
            watch_for_reload(
                ["/ai/speaker/config", "/ai/speaker/modules"],
                stop_evt,
                reload_evt,
            ),
            name="reload_watcher",
        ),
    ]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_evt.set)
        except NotImplementedError:
            pass

    try:
        await stop_evt.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await capture_streamer.grpc.close()


def entrypoint():
    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()
