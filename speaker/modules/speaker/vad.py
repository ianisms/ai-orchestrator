# vad.py
from __future__ import annotations

import json
from pathlib import Path
import logging
from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np

_log = logging.getLogger("speaker.vad")


@dataclass
class VADConfig:
    frame_ms: int = 20
    sample_rate_hz: int = 16000
    rms_threshold: float = 165.0
    silence_hangover_ms: int = 650
    max_utterance_ms: int = 12000
    min_speech_ms: int = 300
    adaptive_enable: bool = True
    adaptive_floor: float = 60.0
    adaptive_ceiling: float = 500.0
    # When the tracked noise floor exceeds adaptive_high_noise_threshold, the
    # threshold is capped at adaptive_ceiling_high instead of adaptive_ceiling.
    # Set to 0 to disable (always use adaptive_ceiling).
    adaptive_ceiling_high: float = 900.0
    adaptive_high_noise_threshold: float = 260.0
    adaptive_margin: float = 1.8
    noise_ema_alpha: float = 0.03
    calibration_path: str = ""


class EnergyVAD:
    def __init__(self, cfg: VADConfig):
        self.cfg = cfg
        self._cal_path = Path(cfg.calibration_path) if cfg.calibration_path else None
        # Initialize noise floor here so reset() can safely omit it — the
        # adaptive noise floor must persist across utterances within a session,
        # only the per-utterance counters should reset between turns.
        self._noise_floor = float(cfg.rms_threshold)
        self.reset()
        self.load_calibration()

    def reset(self):
        """Reset per-utterance state. Does NOT reset _noise_floor so adaptive
        calibration is preserved across turns within a session."""
        self._frames = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._speech_seen = False

    def _threshold(self) -> float:
        if not self.cfg.adaptive_enable:
            return float(self.cfg.rms_threshold)
        dyn = self._noise_floor * float(self.cfg.adaptive_margin)
        high_threshold = float(self.cfg.adaptive_high_noise_threshold)
        if high_threshold > 0 and self._noise_floor >= high_threshold:
            ceiling = float(self.cfg.adaptive_ceiling_high)
        else:
            ceiling = float(self.cfg.adaptive_ceiling)
        return min(max(dyn, float(self.cfg.adaptive_floor)), ceiling)

    def load_calibration(self) -> None:
        if self._cal_path is None:
            return
        try:
            if not self._cal_path.exists():
                return
            data = json.loads(self._cal_path.read_text(encoding="utf-8"))
            nf = float(data.get("noise_floor", self._noise_floor))
            # Clamp to the highest possible ceiling so noisy-environment floors
            # (which may exceed adaptive_ceiling) are preserved correctly.
            max_ceiling = max(float(self.cfg.adaptive_ceiling), float(self.cfg.adaptive_ceiling_high))
            self._noise_floor = min(max(nf, float(self.cfg.adaptive_floor)), max_ceiling)
            thr = self._threshold()
            _log.info(
                "vad calibration loaded noise_floor=%.1f threshold=%.1f path=%s",
                self._noise_floor, thr, self._cal_path,
            )
        except Exception:
            return

    def save_calibration(self) -> None:
        if self._cal_path is None:
            return
        try:
            self._cal_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"noise_floor": float(self._noise_floor)}
            self._cal_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
            thr = self._threshold()
            _log.debug(
                "vad calibration saved noise_floor=%.1f threshold=%.1f ceiling=%.1f",
                self._noise_floor, thr,
                float(self.cfg.adaptive_ceiling_high)
                if (self.cfg.adaptive_high_noise_threshold > 0
                    and self._noise_floor >= self.cfg.adaptive_high_noise_threshold)
                else float(self.cfg.adaptive_ceiling),
            )
        except Exception:
            return

    def is_speech(self, frame: bytes) -> bool:
        rms = self._rms(frame)
        thr = self._threshold()
        speech = rms >= thr
        if not speech:
            alpha = min(max(float(self.cfg.noise_ema_alpha), 0.001), 0.5)
            self._noise_floor = (1.0 - alpha) * self._noise_floor + alpha * rms
        return speech

    def accept_frame(self, frame: bytes) -> Tuple[bool, Dict]:
        self._frames += 1
        rms = self._rms(frame)
        threshold = self._threshold()
        speech = rms >= threshold
        if not speech:
            alpha = min(max(float(self.cfg.noise_ema_alpha), 0.001), 0.5)
            self._noise_floor = (1.0 - alpha) * self._noise_floor + alpha * rms

        should_end = False
        reason = "cont"

        if speech:
            self._speech_seen = True
            self._speech_frames += 1
            self._silence_frames = 0
        else:
            if self._speech_seen:
                self._silence_frames += 1

        if self._speech_seen:
            silence_ms = self._silence_frames * self.cfg.frame_ms
            if silence_ms >= self.cfg.silence_hangover_ms:
                should_end = True
                reason = "silence"

        total_ms = self._frames * self.cfg.frame_ms
        if total_ms >= self.cfg.max_utterance_ms:
            should_end = True
            reason = "max_utterance"

        return should_end, {
            "rms": rms,
            "threshold": threshold,
            "noise_floor": self._noise_floor,
            "speech": speech,
            "reason": reason,
            "frames": self._frames,
            "speech_frames": self._speech_frames,
        }

    def _rms(self, pcm16le: bytes) -> float:
        if not pcm16le or len(pcm16le) < 2:
            return 0.0
        try:
            samples = np.frombuffer(pcm16le, dtype=np.int16).astype(np.float32)
            return float(np.sqrt(np.mean(samples ** 2))) if samples.size > 0 else 0.0
        except Exception:
            return 0.0
