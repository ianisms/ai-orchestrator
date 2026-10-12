from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

try:
    import alsaaudio  # type: ignore
except Exception:  # pragma: no cover
    alsaaudio = None


@dataclass
class MicConfig:
    device: str
    sample_rate_hz: int
    latency_msec: int
    frame_ms: int
    channels: int = 1
    driver: str = "pulse"  # pulse | alsa
    prefer_native_alsa: bool = True
    alsa_period_time_us: int = 0
    alsa_buffer_time_us: int = 0
    cpu_affinity: str = ""
    nice: int = 0
    rt_priority: int = 0


class AudioCapture:
    def __init__(self, cfg: MicConfig, frames_q: asyncio.Queue, log_prefix="mic"):
        self.cfg = cfg
        self.q = frames_q
        self.log_prefix = log_prefix
        self._proc: Optional[subprocess.Popen] = None
        self.log = logging.getLogger(f"speaker.{log_prefix}")
        self._reader_stop = threading.Event()

    def _preexec(self):
        try:
            if self.cfg.cpu_affinity.strip():
                cpus = {
                    int(p.strip())
                    for p in self.cfg.cpu_affinity.split(",")
                    if p.strip().isdigit()
                }
                if cpus:
                    os.sched_setaffinity(0, cpus)
            if int(self.cfg.nice) != 0:
                os.nice(int(self.cfg.nice))
            if int(self.cfg.rt_priority) > 0:
                param = os.sched_param(int(self.cfg.rt_priority))
                os.sched_setscheduler(0, os.SCHED_FIFO, param)
        except Exception:
            pass

    async def run(self):
        if self.cfg.driver == "alsa" and bool(self.cfg.prefer_native_alsa) and alsaaudio is not None:
            await self._run_native_alsa()
            return

        if self.cfg.driver == "alsa":
            device = self.cfg.device or "default"
            period_us = int(self.cfg.alsa_period_time_us) if int(self.cfg.alsa_period_time_us) > 0 else int(self.cfg.frame_ms * 1000)
            buffer_us = int(self.cfg.alsa_buffer_time_us) if int(self.cfg.alsa_buffer_time_us) > 0 else int(period_us * 4)
            cmd = [
                "arecord",
                "-D",
                device,
                "-f",
                "S16_LE",
                "-c",
                str(self.cfg.channels),
                "-r",
                str(self.cfg.sample_rate_hz),
                "-t",
                "raw",
                "--period-time",
                str(period_us),
                "--buffer-time",
                str(buffer_us),
            ]
        elif self.cfg.driver == "pulse":
            cmd = [
                "parec",
                "-d",
                self.cfg.device,
                "--format=s16le",
                f"--channels={self.cfg.channels}",
                f"--rate={self.cfg.sample_rate_hz}",
                f"--latency-msec={self.cfg.latency_msec}",
            ]
        else:
            raise ValueError(f"Unsupported audio driver: {self.cfg.driver}")

        self.log.info("capture started (%s): %s", self.cfg.driver, " ".join(cmd))

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            preexec_fn=self._preexec,
        )
        if self._proc.stdout is None:
            raise RuntimeError("audio capture subprocess stdout not available")
        err_tail: bytes = b""

        # bytes per frame chunk (duration = frame_ms, includes all channels)
        frame_bytes = int(self.cfg.sample_rate_hz * (self.cfg.frame_ms / 1000.0) * 2 * self.cfg.channels)
        buf = bytearray()
        target = frame_bytes

        loop = asyncio.get_running_loop()
        raw_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)

        def _reader():
            try:
                while not self._reader_stop.is_set():
                    chunk = self._proc.stdout.read(target)
                    if not chunk:
                        break
                    def _push(c=chunk):
                        try:
                            raw_q.put_nowait(c)
                        except asyncio.QueueFull:
                            try:
                                _ = raw_q.get_nowait()
                                raw_q.put_nowait(c)
                            except Exception:
                                pass
                    loop.call_soon_threadsafe(_push)
            finally:
                def _push_none():
                    try:
                        raw_q.put_nowait(None)
                    except asyncio.QueueFull:
                        try:
                            _ = raw_q.get_nowait()
                            raw_q.put_nowait(None)
                        except Exception:
                            pass
                loop.call_soon_threadsafe(_push_none)

        reader_thread = threading.Thread(target=_reader, name=f"{self.log_prefix}_reader", daemon=True)
        reader_thread.start()

        try:
            while True:
                chunk = await raw_q.get()
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= frame_bytes:
                    frame = bytes(buf[:frame_bytes])
                    del buf[:frame_bytes]
                    # ensure fixed-size frames to keep downstream timing stable
                    try:
                        self.q.put_nowait(frame)
                    except asyncio.QueueFull:
                        try:
                            _ = self.q.get_nowait()
                            self.q.put_nowait(frame)
                        except Exception:
                            pass
            # capture any stderr once the process stops producing data
            try:
                if self._proc.stderr:
                    err_tail = self._proc.stderr.read()[-400:]
            except Exception:
                pass
            rc = self._proc.poll()
            self.log.warning("capture exited rc=%s err_tail=%s", rc, err_tail.decode(errors="ignore"))
        finally:
            self._reader_stop.set()
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    await asyncio.to_thread(self._proc.wait, 1)
                except Exception:
                    pass
            try:
                reader_thread.join(timeout=0.2)
            except Exception:
                pass
            self._proc = None

    async def _run_native_alsa(self) -> None:
        self.log.info("capture starting native ALSA device=%s", self.cfg.device or "default")
        frame_bytes = int(self.cfg.sample_rate_hz * (self.cfg.frame_ms / 1000.0) * 2 * self.cfg.channels)
        period_frames = max(1, frame_bytes // (2 * self.cfg.channels))
        loop = asyncio.get_running_loop()
        raw_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        stop = threading.Event()

        def _reader():
            pcm = None
            try:
                pcm = alsaaudio.PCM(
                    type=alsaaudio.PCM_CAPTURE,
                    mode=alsaaudio.PCM_NORMAL,
                    device=(self.cfg.device or "default"),
                )
                pcm.setchannels(int(self.cfg.channels))
                pcm.setrate(int(self.cfg.sample_rate_hz))
                pcm.setformat(alsaaudio.PCM_FORMAT_S16_LE)
                pcm.setperiodsize(period_frames)
                while not stop.is_set():
                    n, data = pcm.read()
                    if n <= 0 or not data:
                        continue
                    def _push(c=data):
                        try:
                            raw_q.put_nowait(c)
                        except asyncio.QueueFull:
                            try:
                                _ = raw_q.get_nowait()
                                raw_q.put_nowait(c)
                            except Exception:
                                pass
                    loop.call_soon_threadsafe(_push)
            except Exception:
                pass
            finally:
                if pcm is not None:
                    try:
                        del pcm
                    except Exception:
                        pass
                def _push_none():
                    try:
                        raw_q.put_nowait(None)
                    except asyncio.QueueFull:
                        try:
                            _ = raw_q.get_nowait()
                            raw_q.put_nowait(None)
                        except Exception:
                            pass
                loop.call_soon_threadsafe(_push_none)

        t = threading.Thread(target=_reader, name=f"{self.log_prefix}_alsa_reader", daemon=True)
        t.start()
        try:
            while True:
                data = await raw_q.get()
                if data is None:
                    break
                if len(data) < frame_bytes:
                    data = data + (b"\x00" * (frame_bytes - len(data)))
                if len(data) > frame_bytes:
                    data = data[:frame_bytes]
                try:
                    self.q.put_nowait(data)
                except asyncio.QueueFull:
                    try:
                        _ = self.q.get_nowait()
                        self.q.put_nowait(data)
                    except Exception:
                        pass
        finally:
            stop.set()
            try:
                t.join(timeout=0.5)
            except Exception:
                pass
