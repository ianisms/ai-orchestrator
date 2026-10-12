from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

_CONFIG_PATH_ENV = "ORCH_CONFIG_PATH"
_DEFAULT_CONFIG_PATH = "/ai/server/config/config.yaml"
_COMPONENT_CONFIG_PATH_ENV = "ORCH_COMPONENT_CONFIG_PATH"
_DEFAULT_COMPONENT_CONFIG_PATH = "/ai/server/modules/orchestrator/config/config.yaml"


class Settings(BaseModel):
    ORCH_GRPC_BIND: str = "[::]:7000"

    STT_GRPC_TARGET: str = "127.0.0.1:50051"
    LLM_BASE_URL: str = "http://127.0.0.1:8000"
    LLM_MODEL: str | None = None
    TTS_BASE_URL: str = "http://127.0.0.1:9001"
    LLM_PROMPT_PATH: str = "/ai/server/config/prompt.md"
    TOOLS_MCP_URL: str = "http://127.0.0.1:9102/mcp"

    DEFAULT_SAMPLE_RATE_HZ: int = 16000
    ENERGY_BARGE_IN: float = 0.02
    ORCH_BARGE_IN_MIN_BYTES: int = 640
    STT_EOS_IDLE_MS: int = 600

    BATCH_MAX_CHARS: int = 180
    BATCH_MAX_MS: int = 300

    LOG_DIR: str = "/ai/server/modules/orchestrator/logs"
    LOG_LEVEL: str = "INFO"
    ORCH_HOT_RELOAD: bool = False
    ORCH_SHUTDOWN_GRACE_S: float = 1.0
    ORCH_START_TIMEOUT_S: float = 2.0
    ORCH_OUTQ_MAX: int = 500
    ORCH_INQ_AUDIO_MAX: int = 300
    ORCH_INQ_TEXT_MAX: int = 50
    ORCH_PIPELINE_IDLE_TIMEOUT_S: float = 8.0
    ORCH_TURN_MAX_S: float = 90.0
    ORCH_LOG_KV: bool = True
    ORCH_LOG_STREAMING: bool = False
    ORCH_LOG_STREAM_EVERY_N: int = 50
    ORCH_METRICS_PORT: int = 9110
    ORCH_STATUS_ENABLED: bool = True
    ORCH_STATUS_PORT: int = 9111
    ENABLE_STORED_HISTORY: bool = False
    ORCH_HISTORY_DB: str = "/ai/server/state/orchestrator_history.sqlite3"
    ORCH_SESSION_TTL_S: float = 3600.0
    ORCH_SESSION_MAX: int = 200
    ORCH_SESSION_DEGRADE_THRESHOLD: float = 0.85
    ORCH_SESSION_HARD_EVICT_FACTOR: float = 1.25
    ORCH_HISTORY_DEGRADED_TOKENS: int = 512
    ORCH_TOOL_PARALLELISM: int = 1
    ORCH_TOOL_TIMEOUT_S: float = 20.0
    ORCH_TOOL_RESULT_MAX_CHARS: int = 3000
    ORCH_TOOL_SUMMARY_MAX_CHARS: int = 6000
    ORCH_READINESS_WAIT_S: float = 120.0
    ORCH_READINESS_POLL_S: float = 2.0

    STT_GRPC_TIMEOUT_S: float = 90.0
    STT_REQ_TIMEOUT_S: float = 0.25
    LLM_IDLE_TIMEOUT_S: float = 10.0
    LLM_TEMPERATURE: float = 0.3
    LLM_TOP_P: float = 0.95
    LLM_HISTORY_TOKENS: int = 3072
    LLM_MAX_TOKENS: int = 200
    LLM_RESPONSE_FORMAT_JSON: bool = False
    LLM_WARMUP_DELAY_MS: int = 0
    LLM_WARMUP_ENABLED: bool = True
    LLM_TOOL_DESCRIPTIONS_MAX: int = 0
    TTS_HTTP_TIMEOUT_S: float = 30.0

    TTS_EXPAND: str = "general"

    @field_validator("TTS_EXPAND", mode="before")
    @classmethod
    def _coerce_tts_expand(cls, v: Any) -> str:
        if isinstance(v, int):
            return "general" if v else "none"
        return str(v)

    TTS_SENTENCE_PAUSE_MS: int = 60
    TTS_CROSSFADE_MS: int = 16
    TTS_MIN_CHUNK_CHARS: int = 25
    TTS_MIN_CHUNK_WORDS: int = 4
    TTS_HIGHPASS_HZ: float = 100.0
    TTS_TARGET_PEAK: float = 0.65
    TTS_MAX_GAIN: float = 4.0
    TTS_FADE_MS: float = 3.0
    TTS_TAIL_MS: float = 10.0
    TTS_PITCH_SEMITONES: float = 0.8
    TTS_SPEAKING_RATE: float = 1.08
    TTS_MOOD: str = "neutral"
    TTS_MOODS: dict = {}
    TTS_SPEAKER_ID: int | None = None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}

    if not isinstance(data, dict):
        return {}

    return {str(k).upper(): v for k, v in data.items()}


def _load_settings(global_path: Path, component_path: Path) -> Settings:
    global_data = _read_yaml(global_path)
    component_data = _read_yaml(component_path)
    values: dict[str, Any] = {}

    for name in Settings.model_fields:
        env_val = os.getenv(name)
        if env_val is not None:
            values[name] = env_val
            continue
        if name in component_data:
            values[name] = component_data[name]
            continue
        if name in global_data:
            values[name] = global_data[name]

    return Settings(**values)


class SettingsProxy:
    def __init__(self) -> None:
        self._path: Path | None = None
        self._mtime: float | None = None
        self._component_path: Path | None = None
        self._component_mtime: float | None = None
        self._settings = Settings()

    def _config_path(self) -> Path:
        return Path(os.getenv(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH))

    def _component_config_path(self) -> Path:
        return Path(os.getenv(_COMPONENT_CONFIG_PATH_ENV, _DEFAULT_COMPONENT_CONFIG_PATH))

    def _load_if_needed(self) -> None:
        path = self._config_path()
        component_path = self._component_config_path()
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        try:
            component_mtime = component_path.stat().st_mtime
        except FileNotFoundError:
            component_mtime = None

        if (
            self._path == path
            and self._mtime == mtime
            and self._component_path == component_path
            and self._component_mtime == component_mtime
        ):
            return

        self._settings = _load_settings(path, component_path)
        self._path = path
        self._mtime = mtime
        self._component_path = component_path
        self._component_mtime = component_mtime

    def __getattr__(self, name: str) -> Any:
        self._load_if_needed()
        return getattr(self._settings, name)


settings = SettingsProxy()
