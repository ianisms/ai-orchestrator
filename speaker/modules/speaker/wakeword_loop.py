# wakeword_loop.py
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import numpy as np
from openwakeword.model import Model


@dataclass
class WakewordConfig:
    model_path: str
    threshold: float = 0.5
    refractory_ms: int = 1200
    score_log_every_n: int = 50
    sample_rate_hz: int = 16000
    infer_chunk_ms: int = 80
    channels: int = 1


class WakewordLoop:
    """
    Reads PCM frames from ww_q and emits wake events to events_q.
    Emits {"type": "wake", "score": float, "ts": float} and {"type": "ww_score", ...}
    """

    def __init__(self, cfg: WakewordConfig, ww_q: asyncio.Queue, events_q: asyncio.Queue):
        self.cfg = cfg
        self.ww_q = ww_q
        self.events_q = events_q

        self._stop_evt = asyncio.Event()
        self._model = Model(
            wakeword_models=[cfg.model_path],
            inference_framework="onnx",
        )

        self._model_key = None
        self._last_wake_ts = 0.0
        self._score_count = 0
        self.log = logging.getLogger("speaker.ww")

        # Bytes needed for one inference chunk (all channels)
        chunk_samples_mono = int(cfg.sample_rate_hz * cfg.infer_chunk_ms / 1000)
        self._chunk_bytes = chunk_samples_mono * cfg.channels * 2
        self._chunk_samples_mono = chunk_samples_mono

    def stop(self):
        self._stop_evt.set()

    async def run(self):
        buf = bytearray()
        refractory_s = self.cfg.refractory_ms / 1000.0

        self.log.info("wakeword init model=%s", self.cfg.model_path)

        while not self._stop_evt.is_set():
            frame = await self.ww_q.get()
            if not frame:
                continue

            buf.extend(frame)

            while len(buf) >= self._chunk_bytes:
                chunk = bytes(buf[: self._chunk_bytes])
                del buf[: self._chunk_bytes]

                x = np.frombuffer(chunk, dtype=np.int16)
                if self.cfg.channels > 1:
                    # Downmix interleaved channels to mono for wakeword model
                    try:
                        x = x.reshape(-1, self.cfg.channels).mean(axis=1).astype(np.int16)
                    except Exception:
                        # Fallback: if reshape fails, just take every Nth sample
                        x = x[:: self.cfg.channels]
                pred = await asyncio.get_running_loop().run_in_executor(
                    None, self._model.predict, x
                )

                if self._model_key is None:
                    self._model_key = next(iter(pred.keys()))
                    self.log.info("wakeword model_key=%s", self._model_key)

                score = float(pred[self._model_key])
                self._score_count += 1
                now = time.monotonic()

                if self.cfg.score_log_every_n and self._score_count % self.cfg.score_log_every_n == 0:
                    self.log.debug("wakeword score=%.3f", score)
                    await self._emit({"type": "ww_score", "score": score, "ts": now})

                if score >= self.cfg.threshold and (now - self._last_wake_ts) >= refractory_s:
                    self._last_wake_ts = now
                    self.log.info("wakeword fired score=%.3f", score)
                    await self._emit({"type": "wake", "score": score, "ts": now})

    async def _emit(self, evt: dict):
        try:
            self.events_q.put_nowait(evt)
        except asyncio.QueueFull:
            if evt.get("type") == "wake":
                self.log.warning("wakeword event queue full; wake event dropped score=%.3f", evt.get("score", 0.0))
