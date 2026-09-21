import asyncio
import logging
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import AsyncIterator, Optional

try:
    import alsaaudio  # type: ignore
except Exception:  # pragma: no cover
    alsaaudio = None


@dataclass
class PlaybackFormat:
    sample_rate_hz: int
    channels: int = 1
    sample_format: str = "s16le"


class AudioPlayback:
    def __init__(
        self,
        driver: str = "pulse",
        device: str = "",
        log_prefix: str = "play",
        prefer_native_alsa: bool = True,
        alsa_period_time_us: int = 0,
        alsa_buffer_time_us: int = 0,
        cpu_affinity: str = "",
        nice: int = 0,
        rt_priority: int = 0,
    ):
        self.log_prefix = log_prefix
        self.driver = driver
        self.device = device
        self.prefer_native_alsa = bool(prefer_native_alsa)
        self._proc: Optional[subprocess.Popen] = None
        self._alsa_pcm = None
        self.alsa_period_time_us = int(alsa_period_time_us)
        self.alsa_buffer_time_us = int(alsa_buffer_time_us)
        self.cpu_affinity = str(cpu_affinity or "")
        self.nice = int(nice)
        self.rt_priority = int(rt_priority)
        self._fmt_key: tuple[int, int, str] | None = None
        self._writer_q: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
        self._writer_thread: threading.Thread | None = None
        self.log = logging.getLogger(f"speaker.{log_prefix}")

    def _start_writer_thread(self) -> None:
        self._writer_q = queue.Queue(maxsize=256)

        def _writer():
            while True:
                data = self._writer_q.get()
                if data is None:
                    self._writer_q.task_done()
                    return
                if self._alsa_pcm is not None:
                    try:
                        self._alsa_pcm.write(data)
                    except Exception as exc:
                        self.log.warning("ALSA write failed: %r", exc)
                        self._writer_q.task_done()
                        return
                    self._writer_q.task_done()
                    continue
                if not self._proc or self._proc.poll() is not None or not self._proc.stdin:
                    self._writer_q.task_done()
                    return
                try:
                    self._proc.stdin.write(data)
                except Exception as exc:
                    self.log.warning("playback stdin write failed: %r", exc)
                    self._writer_q.task_done()
                    return
                self._writer_q.task_done()

        self._writer_thread = threading.Thread(target=_writer, name=f"{self.log_prefix}_writer", daemon=True)
        self._writer_thread.start()

    def _preexec(self):
        try:
            if self.cpu_affinity.strip():
                cpus = {
                    int(p.strip())
                    for p in self.cpu_affinity.split(",")
                    if p.strip().isdigit()
                }
                if cpus:
                    os.sched_setaffinity(0, cpus)
            if self.nice != 0:
                os.nice(self.nice)
            if self.rt_priority > 0:
                param = os.sched_param(self.rt_priority)
                os.sched_setscheduler(0, os.SCHED_FIFO, param)
        except Exception:
            pass

    async def stop(self, reason: str = "stop"):
        try:
            self._writer_q.put_nowait(None)
        except Exception:
            try:
                while True:
                    _ = self._writer_q.get_nowait()
                    self._writer_q.task_done()
            except Exception:
                pass
            try:
                self._writer_q.put_nowait(None)
            except Exception:
                pass
        if self._writer_thread and self._writer_thread.is_alive():
            try:
                self._writer_thread.join(timeout=0.3)
            except Exception:
                pass
        self._writer_thread = None
        if self._proc and self._proc.poll() is None:
            self.log.info("stopping (%s)", reason)
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.to_thread(self._proc.wait, 1)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._proc:
            rc = self._proc.returncode
            tail = b""
            try:
                tail = (self._proc.stderr.read() if self._proc.stderr else b"")[-400:]
            except Exception:
                pass
            self.log.info("stopped rc=%s err_tail=%s", rc, tail.decode(errors="ignore"))
        else:
            self.log.info("stopped")
        self._proc = None
        if self._alsa_pcm is not None:
            try:
                self._alsa_pcm.close()
            except Exception:
                pass
            self._alsa_pcm = None
        self._fmt_key = None

    def _build_cmd(self, fmt: PlaybackFormat) -> list[str]:
        if self.driver == "alsa":
            dev = self.device or "default"
            fmt_str = fmt.sample_format
            fmt_alsa = fmt_str.upper().replace("-", "_")
            if "_" not in fmt_alsa and fmt_alsa.endswith("LE"):
                fmt_alsa = fmt_alsa[:-2] + "_LE"
            cmd = [
                "aplay",
                "-D",
                dev,
                "-f",
                fmt_alsa,
                "-c",
                str(fmt.channels),
                "-r",
                str(fmt.sample_rate_hz),
                "-t",
                "raw",
            ]
            if self.alsa_period_time_us > 0:
                cmd.extend(["--period-time", str(self.alsa_period_time_us)])
            if self.alsa_buffer_time_us > 0:
                cmd.extend(["--buffer-time", str(self.alsa_buffer_time_us)])
            return cmd
        if self.driver == "pulse":
            cmd = [
                "pacat",
                "--raw",
                f"--format={fmt.sample_format}",
                f"--channels={fmt.channels}",
                f"--rate={fmt.sample_rate_hz}",
            ]
            if self.device:
                cmd.extend(["-d", self.device])
            return cmd
        raise ValueError(f"Unsupported audio driver: {self.driver}")

    async def _ensure_proc(self, fmt: PlaybackFormat) -> None:
        key = (int(fmt.sample_rate_hz), int(fmt.channels), str(fmt.sample_format))
        if self.driver == "alsa" and self.prefer_native_alsa and alsaaudio is not None:
            if self._alsa_pcm is not None and self._fmt_key == key:
                return
            # Close old PCM and stop old writer thread before opening a new one.
            if self._alsa_pcm is not None:
                try:
                    self._alsa_pcm.close()
                except Exception:
                    pass
                self._alsa_pcm = None
            if self._writer_thread is not None and self._writer_thread.is_alive():
                try:
                    self._writer_q.put_nowait(None)
                except Exception:
                    pass
                self._writer_thread.join(timeout=0.3)
                self._writer_thread = None
            self._alsa_pcm = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK,
                mode=alsaaudio.PCM_NORMAL,
                device=(self.device or "default"),
            )
            self._alsa_pcm.setchannels(int(fmt.channels))
            self._alsa_pcm.setrate(int(fmt.sample_rate_hz))
            self._alsa_pcm.setformat(alsaaudio.PCM_FORMAT_S16_LE)
            if self.alsa_period_time_us > 0:
                frames = max(1, int((self.alsa_period_time_us / 1_000_000.0) * fmt.sample_rate_hz))
            else:
                frames = max(256, int(fmt.sample_rate_hz * 0.02))
            self._alsa_pcm.setperiodsize(frames)
            self._fmt_key = key
            self.log.info("start native ALSA playback device=%s", self.device or "default")
            self._start_writer_thread()
            return
        if self._proc and self._proc.poll() is None and self._fmt_key == key:
            return
        if self._proc:
            await self.stop("format-change")
        cmd = self._build_cmd(fmt)
        self.log.info("start raw %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            preexec_fn=self._preexec,
        )
        self._fmt_key = key
        self._start_writer_thread()

    async def play_raw_stream(self, pcm_iter: AsyncIterator[bytes], fmt: PlaybackFormat, cancel_evt: asyncio.Event):
        await self._ensure_proc(fmt)
        if self._alsa_pcm is None:
            if not self._proc or self._proc.stdin is None:
                raise RuntimeError("audio playback subprocess stdin not available")

        chunks = 0
        total_bytes = 0

        try:
            async for chunk in pcm_iter:
                if cancel_evt.is_set():
                    raise asyncio.CancelledError()

                if not chunk:
                    continue

                if self._proc is not None and self._proc.poll() is not None:
                    err = b""
                    try:
                        err = self._proc.stderr.read() if self._proc.stderr else b""
                    except Exception:
                        pass
                    raise RuntimeError(f"playback process exited early rc={self._proc.returncode} err={err[-400:].decode(errors='ignore')}")
                while True:
                    try:
                        self._writer_q.put_nowait(chunk)
                        break
                    except queue.Full:
                        if cancel_evt.is_set():
                            raise asyncio.CancelledError()
                        await asyncio.sleep(0.001)
                chunks += 1
                total_bytes += len(chunk)

            self.log.info(
                "finished playback chunks=%s bytes=%s rc=%s",
                chunks,
                total_bytes,
                self._proc.returncode if self._proc is not None else 0,
            )
            try:
                await asyncio.wait_for(asyncio.to_thread(self._writer_q.join), timeout=1.0)
            except asyncio.TimeoutError:
                self.log.warning("writer queue drain timed out")

        except asyncio.CancelledError:
            self.log.info("cancel during playback chunks=%s bytes=%s", chunks, total_bytes)
            await self.stop("cancel")
            raise
        except BrokenPipeError:
            err = b""
            try:
                err = self._proc.stderr.read() if self._proc and self._proc.stderr else b""
            except Exception:
                pass
            await self.stop("broken-pipe")
            raise RuntimeError(f"BrokenPipeError (playback) err={err[-400:].decode(errors='ignore')}")
