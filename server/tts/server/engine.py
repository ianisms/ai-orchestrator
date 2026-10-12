import os
import io
import wave
from typing import Optional, Dict

import numpy as np
import torch
from nemo.collections.tts.models import FastPitchModel, HifiGanModel


class NemoTTSEngine:
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.fastpitch = FastPitchModel.restore_from(
            os.getenv("FASTPITCH_NEMO", "/models/assistant.fastpitch.nemo"),
            map_location=self.device,
        ).eval()

        self.hifigan = HifiGanModel.restore_from(
            os.getenv("HIFIGAN_NEMO", "/models/assistant.hifigan.nemo"),
            map_location=self.device,
        ).eval()

        self.sample_rate = int(getattr(self.hifigan, "sample_rate", 22050) or 22050)

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    @staticmethod
    def _highpass_1pole(audio: np.ndarray, sr: int, cutoff_hz: float) -> np.ndarray:
        if cutoff_hz <= 0:
            return audio

        dt = 1.0 / sr
        rc = 1.0 / (2.0 * np.pi * cutoff_hz)
        alpha = rc / (rc + dt)

        y = np.empty_like(audio)
        y[0] = audio[0]
        for i in range(1, len(audio)):
            y[i] = alpha * (y[i - 1] + audio[i] - audio[i - 1])
        return y

    @staticmethod
    def _resample(audio: np.ndarray, new_len: int) -> np.ndarray:
        if new_len <= 1 or len(audio) <= 1:
            return audio
        x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)

    @staticmethod
    def _time_stretch(audio: np.ndarray, rate: float) -> np.ndarray:
        if rate <= 0:
            return audio
        if abs(rate - 1.0) < 1e-3:
            return audio
        new_len = max(1, int(len(audio) / rate))
        return NemoTTSEngine._resample(audio, new_len)

    @staticmethod
    def _pitch_shift(audio: np.ndarray, semitones: float) -> np.ndarray:
        if abs(semitones) < 1e-3:
            return audio
        factor = 2.0 ** (semitones / 12.0)
        shifted = NemoTTSEngine._resample(audio, max(1, int(len(audio) / factor)))
        return NemoTTSEngine._resample(shifted, len(audio))

    @staticmethod
    def _expander_smoothed_gain(
        audio: np.ndarray,
        sr: int,
        threshold_db: float,
        ratio: float,
        attack_ms: float,
        release_ms: float,
    ) -> np.ndarray:
        """
        Gentle downward expander with attack/release smoothing.

        - threshold_db: e.g. -58
        - ratio: e.g. 2.0
        - attack_ms: e.g. 8
        - release_ms: e.g. 120
        """
        eps = 1e-9
        x = audio.astype(np.float32, copy=False)

        # Desired gain in dB (negative below threshold, 0 above)
        mag = np.abs(x) + eps
        mag_db = 20.0 * np.log10(mag)

        desired_gain_db = np.zeros_like(mag_db, dtype=np.float32)
        below = mag_db < threshold_db
        desired_gain_db[below] = (mag_db[below] - threshold_db) * (ratio - 1.0)  # negative

        # Convert to linear gain
        desired_gain = np.power(10.0, desired_gain_db / 20.0).astype(np.float32)

        # Attack/release smoothing on gain (one-pole)
        attack_ms = max(0.1, float(attack_ms))
        release_ms = max(0.1, float(release_ms))

        a_a = np.exp(-1.0 / (sr * (attack_ms / 1000.0))).astype(np.float32)
        a_r = np.exp(-1.0 / (sr * (release_ms / 1000.0))).astype(np.float32)

        g = np.empty_like(desired_gain, dtype=np.float32)
        g_prev = 1.0

        for i in range(len(desired_gain)):
            g_t = desired_gain[i]

            # If gain is decreasing (more attenuation) -> attack
            # If gain is increasing (less attenuation) -> release
            if g_t < g_prev:
                a = a_a
            else:
                a = a_r

            g_cur = a * g_prev + (1.0 - a) * g_t
            g[i] = g_cur
            g_prev = g_cur

        return x * g

    @staticmethod
    def _f_or_default(val, default):
        return default if val is None else float(val)

    # ------------------------------------------------------------
    # Main synthesis
    # ------------------------------------------------------------
    @torch.no_grad()
    def synthesize_pcm16(self, text: str, opts: Optional[Dict] = None) -> np.ndarray:
        opts = opts or {}

        text = (text or "").strip()
        if not text:
            print("NemoTTSEngine: synthesize_pcm16 called with empty text; returning silence")
            return self.silence_pcm16(self.sample_rate, ms=50)
        else:
            print(f"NemoTTSEngine: synthesizing text {text} of length {len(text)}")

        tokens = self.fastpitch.parse(text).to(self.device)
        spec = self.fastpitch.generate_spectrogram(tokens=tokens)

        try:
            audio = self.hifigan.convert_spectrogram_to_audio(spec=spec)
        except TypeError:
            audio = self.hifigan.convert_spectrogram_to_audio(spectrogram=spec)

        audio = audio.detach().cpu().numpy().squeeze().astype(np.float32)

        # ---- DC offset ----
        if len(audio) >= int(self.sample_rate * 0.15):
            audio -= float(np.mean(audio))

        # ---- Mood-ish controls (simple DSP) ----
        pitch_semitones = self._f_or_default(opts.get("pitch_semitones"), float(os.getenv("TTS_PITCH_SEMITONES", "0.0")))
        speaking_rate = self._f_or_default(opts.get("speaking_rate"), float(os.getenv("TTS_SPEAKING_RATE", "1.0")))
        audio = self._pitch_shift(audio, pitch_semitones)
        audio = self._time_stretch(audio, speaking_rate)

        # ---- High-pass ----
        hp = self._f_or_default(opts.get("highpass_hz"), float(os.getenv("TTS_HIGHPASS_HZ", "0")))
        if hp > 0:
            audio = self._highpass_1pole(audio, self.sample_rate, hp)

        # ---- Noise gate / expander (smoothed) ----
        gate_db = self._f_or_default(opts.get("gate_threshold_db"), float(os.getenv("TTS_GATE_DB", "-58")))
        gate_ratio = self._f_or_default(opts.get("gate_ratio"), float(os.getenv("TTS_GATE_RATIO", "2.0")))
        gate_attack_ms = self._f_or_default(opts.get("gate_attack_ms"), float(os.getenv("TTS_GATE_ATTACK_MS", "8")))
        gate_release_ms = self._f_or_default(opts.get("gate_release_ms"), float(os.getenv("TTS_GATE_RELEASE_MS", "120")))

        audio = self._expander_smoothed_gain(
            audio,
            sr=self.sample_rate,
            threshold_db=gate_db,
            ratio=gate_ratio,
            attack_ms=gate_attack_ms,
            release_ms=gate_release_ms,
        )

        # ---- Gain control ----
        target_peak = self._f_or_default(opts.get("target_peak"), float(os.getenv("TTS_TARGET_PEAK", "0.55")))
        max_gain = self._f_or_default(opts.get("max_gain"), float(os.getenv("TTS_MAX_GAIN", "3.5")))

        peak = float(np.max(np.abs(audio)) + 1e-9)
        if peak < target_peak:
            audio *= min(max_gain, target_peak / peak)

        # ---- Fade ----
        fade_ms = self._f_or_default(opts.get("fade_ms"), float(os.getenv("TTS_FADE_MS", "4")))
        fade_n = min(int(self.sample_rate * fade_ms / 1000.0), len(audio) // 10)
        if fade_n > 0:
            ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
            audio[:fade_n] *= ramp
            audio[-fade_n:] *= ramp[::-1]

        # ---- Tail ----
        tail_ms = self._f_or_default(opts.get("tail_ms"), float(os.getenv("TTS_TAIL_MS", "16")))
        if tail_ms > 0:
            audio = np.concatenate([audio, np.zeros(int(self.sample_rate * tail_ms / 1000.0), dtype=np.float32)])

        audio = np.clip(audio, -1.0, 1.0)
        return (audio * 32767.0).astype(np.int16)

    def synthesize_wav(self, text: str, opts: Optional[Dict] = None) -> bytes:
        pcm16 = self.synthesize_pcm16(text, opts)
        return self.wav_bytes_from_pcm16(pcm16, self.sample_rate)

    @staticmethod
    def silence_pcm16(sr: int, ms: int) -> np.ndarray:
        return np.zeros(max(1, int(sr * ms / 1000.0)), dtype=np.int16)

    @staticmethod
    def wav_bytes_from_pcm16(pcm16: np.ndarray, sr: int) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm16.tobytes())
        return buf.getvalue()
