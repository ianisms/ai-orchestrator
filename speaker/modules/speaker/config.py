from __future__ import annotations

from dataclasses import dataclass
import logging
import os

import yaml


@dataclass
class AppConfig:
    # Server
    server_target: str = "ai-server.local:7000"
    session_id_base: str = "speaker-dev-1"
    locale: str = "en-US"
    request_partials: bool = True
    stt_send_timeout_ms: int = 12000
    benchmark_profile: str = "balanced"
    benchmark_profiles_path: str = "/ai/speaker/config/benchmarks.yaml"

    # Mic
    audio_driver: str = "pulse"  # pulse | alsa
    prefer_native_alsa: bool = True
    mic_device: str = "RDPSource"
    mic_channels: int = 1
    mic_sample_rate_hz: int = 16000
    mic_frame_ms: int = 20
    mic_latency_msec: int = 30
    alsa_period_time_us: int = 0
    alsa_buffer_time_us: int = 0
    playback_device: str = ""
    playback_channels: int = 1
    wake_preroll_ms: int = 400

    # Wakeword
    wake_model_path: str = "hey_jarvis"
    wake_threshold: float = 0.55
    wake_refractory_ms: int = 1200
    wake_score_log_every_n: int = 50
    wake_infer_chunk_ms: int = 80
    wake_chime_freq_hz: float = 880.0
    wake_chime_ms: int = 140
    wake_chime_enable: bool = True

    # VAD / capture
    vad_rms_threshold: float = 120.0
    vad_silence_hangover_ms: int = 650
    vad_max_utterance_ms: int = 12000
    min_commit_speech_ms: int = 200
    min_capture_ms: int = 600
    vad_adaptive_enable: bool = True
    vad_adaptive_floor: float = 60.0
    vad_adaptive_ceiling: float = 500.0
    vad_adaptive_ceiling_high: float = 900.0
    vad_adaptive_high_noise_threshold: float = 260.0
    vad_adaptive_margin: float = 1.8
    vad_noise_ema_alpha: float = 0.03
    vad_calibration_path: str = "/ai/speaker/state/vad_calibration.json"

    # Barge-in (interrupt TTS by speaking)
    barge_in_enable: bool = True
    barge_in_min_speech_frames: int = 4    # N * frame_ms must be sustained speech to trigger
    barge_in_prebuffer_ms: int = 200       # prebuffer to pass to new capture turn
    barge_in_settle_ms: int = 600          # ignore mic during first N ms of playback (chime bleed-through guard)

    # Conversation persistence
    conversation_id_state_path: str = "/ai/speaker/state/conversation_id.txt"

    # Follow-up
    followup_enable: bool = True
    followup_max: int = 3
    followup_idle_ms: int = 2000
    followup_min_consecutive_speech_frames: int = 2
    followup_prebuffer_ms: int = 200

    # Capture queue handling
    drain_max_frames: int = 8

    # Playback bleed-through guard
    playback_guard_ms: int = 300
    capture_skip_playback_ms: int = 1200
    settle_silence_ms: int = 400
    settle_max_ms: int = 2000
    stop_capture_on_first_tts_audio: bool = True
    stop_on_thinking: bool = True
    grpc_keepalive_time_ms: int = 5000
    grpc_keepalive_timeout_ms: int = 3000
    grpc_connect_max_attempts: int = 8
    grpc_connect_ready_timeout_ms: int = 4000
    playback_backpressure_high_watermark: int = 384
    playback_backpressure_drop_nonfinal: bool = True
    echo_suppress_enable: bool = False
    echo_suppress_max_attenuation: float = 0.35

    # Logging
    log_level: str = "INFO"
    verbose: bool = False
    stream_log_every_n: int = 50

    # Runtime scheduling
    audio_cpu_affinity: str = ""
    audio_nice: int = 0
    audio_rt_priority: int = 0

    # Health/log
    health_path: str = "/tmp/speaker_health.json"
    health_check_interval_s: float = 10.0
    health_check_timeout_s: float = 3.0
    log_path: str = "/ai/speaker/logs/client.log"

    @classmethod
    def from_yaml(cls, path: str = "/ai/speaker/config/config.yaml") -> "AppConfig":
        cfg = cls()
        if not os.path.exists(path):
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return cfg
        legacy_map = {
            "max_followups": "followup_max",
            "dialogue_silence_ms": "followup_idle_ms",
            "followup_settle_silence_ms": "settle_silence_ms",
            "followup_min_consecutive_speech_frames": "followup_min_consecutive_speech_frames",
        }
        _cfg_log = logging.getLogger("speaker.config")
        for key, value in data.items():
            mapped = legacy_map.get(key, key)
            if hasattr(cfg, mapped):
                setattr(cfg, mapped, value)
            else:
                _cfg_log.warning("unknown config key ignored: %s", key)
        profile = str(getattr(cfg, "benchmark_profile", "") or "").strip().lower()
        profiles_path = str(getattr(cfg, "benchmark_profiles_path", "") or "").strip()
        if profile and profile != "custom" and profiles_path and os.path.exists(profiles_path):
            try:
                with open(profiles_path, "r", encoding="utf-8") as f:
                    prof_data = yaml.safe_load(f) or {}
                profiles = prof_data.get("profiles") if isinstance(prof_data, dict) else {}
                selected = profiles.get(profile) if isinstance(profiles, dict) else None
                if isinstance(selected, dict):
                    for k, v in selected.items():
                        if hasattr(cfg, k):
                            setattr(cfg, k, v)
            except Exception:
                pass
        return cfg
