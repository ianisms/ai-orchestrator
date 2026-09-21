import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest, start_http_server


class StatsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._lifetime: dict[str, Any] = {"turns": 0}
        self._uptime: dict[str, Any] = {"turns": 0}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, dict):
            lifetime = data.get("lifetime")
            if isinstance(lifetime, dict):
                self._lifetime = lifetime

    def record_turn(self) -> None:
        with self._lock:
            self._uptime["turns"] = int(self._uptime.get("turns", 0)) + 1
            self._lifetime["turns"] = int(self._lifetime.get("turns", 0)) + 1
            payload = json.dumps({"lifetime": self._lifetime}, ensure_ascii=True)

        # Snapshot captured under lock; write outside to avoid blocking event loop.
        def _do() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(payload, encoding="utf-8")

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _do)
        except RuntimeError:
            _do()

    def reset_uptime(self) -> None:
        with self._lock:
            self._uptime = {"turns": 0}

    def reset_all(self) -> None:
        with self._lock:
            self._uptime = {"turns": 0}
            self._lifetime = {"turns": 0}
            payload = json.dumps({"lifetime": self._lifetime}, ensure_ascii=True)

        def _do() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(payload, encoding="utf-8")

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _do)
        except RuntimeError:
            _do()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime": dict(self._uptime),
                "lifetime": dict(self._lifetime),
            }


def _default_stats_path() -> Path:
    env_path = os.getenv("ORCH_STATS_PATH")
    if env_path:
        return Path(env_path)
    return Path("/ai/server/state/orchestrator_stats.json")


_STORE = StatsStore(_default_stats_path())

ORCH_TURNS_TOTAL = Gauge(
    "orchestrator_turns_total",
    "Orchestrator turns since uptime",
)
ORCH_TURNS_LIFETIME_TOTAL = Gauge(
    "orchestrator_turns_lifetime_total",
    "Orchestrator turns lifetime",
)
ORCH_LLM_SECONDS = Histogram(
    "orchestrator_llm_seconds",
    "LLM request duration in seconds",
    ["mode"],
)
ORCH_INGEST_SECONDS = Histogram(
    "orchestrator_ingest_seconds",
    "Ingest (audio/text to commit) duration in seconds",
)
ORCH_TOOL_CALL_SECONDS = Histogram(
    "orchestrator_tool_call_seconds",
    "Tool call duration in seconds",
)
ORCH_TTS_SECONDS = Histogram(
    "orchestrator_tts_seconds",
    "TTS streaming duration in seconds",
)
ORCH_TURN_SECONDS = Histogram(
    "orchestrator_turn_seconds",
    "End-to-end turn duration in seconds (commit to completion)",
)
ORCH_BARGE_IN_INTERRUPTS_TOTAL = Counter(
    "orchestrator_barge_in_interrupts_total",
    "Number of barge-in interruptions detected while speaking",
    ["source"],
)
ORCH_TURN_WATCHDOG_TIMEOUTS_TOTAL = Counter(
    "orchestrator_turn_watchdog_timeouts_total",
    "Number of turns terminated by watchdog timeout",
    ["phase"],
)
ORCH_SESSION_EVICTIONS_TOTAL = Counter(
    "orchestrator_session_evictions_total",
    "Number of session histories evicted by TTL/max-session policy",
    ["cause"],
)
ORCH_QUEUE_DEPTH = Gauge(
    "orchestrator_queue_depth",
    "Current queue depth for internal orchestrator queues",
    ["queue"],
)
ORCH_QUEUE_DROPS_TOTAL = Counter(
    "orchestrator_queue_drops_total",
    "Dropped items per queue due to backpressure/overflow",
    ["queue"],
)
ORCH_TOOL_OUTPUTS_TOTAL = Counter(
    "orchestrator_tool_outputs_total",
    "Count of normalized tool outputs by status",
    ["status"],
)
ORCH_TOOL_OUTPUT_SIZE_BYTES = Histogram(
    "orchestrator_tool_output_size_bytes",
    "Normalized tool output size in bytes",
    ["status"],
)
ORCH_TURN_OUTCOMES_TOTAL = Counter(
    "orchestrator_turn_outcomes_total",
    "Turn outcomes by result type",
    ["outcome"],
)
ORCH_SESSIONS_ACTIVE = Gauge(
    "orchestrator_sessions_active",
    "Current number of active session histories",
)
ORCH_SESSIONS_CREATED_TOTAL = Counter(
    "orchestrator_sessions_created_total",
    "Total number of session histories created",
)
ORCH_SESSIONS_DESTROYED_TOTAL = Counter(
    "orchestrator_sessions_destroyed_total",
    "Total number of session histories destroyed",
    ["reason"],
)
ORCH_BARGE_IN_TO_AUDIO_LAST_SECONDS = Histogram(
    "orchestrator_barge_in_to_audio_last_seconds",
    "Seconds from barge-in interrupt detection to final TTS audio chunk",
)


def _sync_metrics(snapshot: dict[str, Any]) -> None:
    uptime = snapshot.get("uptime", {})
    lifetime = snapshot.get("lifetime", {})
    ORCH_TURNS_TOTAL.set(float(uptime.get("turns", 0)))
    ORCH_TURNS_LIFETIME_TOTAL.set(float(lifetime.get("turns", 0)))


def record_turn() -> None:
    _STORE.record_turn()
    _sync_metrics(_STORE.snapshot())


def reset_uptime() -> None:
    _STORE.reset_uptime()
    _sync_metrics(_STORE.snapshot())


def reset_all() -> None:
    _STORE.reset_all()
    _sync_metrics(_STORE.snapshot())


def observe_llm_seconds(seconds: float, *, mode: str) -> None:
    try:
        ORCH_LLM_SECONDS.labels(mode=mode).observe(seconds)
    except Exception:
        pass


def observe_ingest_seconds(seconds: float) -> None:
    try:
        ORCH_INGEST_SECONDS.observe(seconds)
    except Exception:
        pass


def observe_tool_call_seconds(seconds: float) -> None:
    try:
        ORCH_TOOL_CALL_SECONDS.observe(seconds)
    except Exception:
        pass


def observe_tts_seconds(seconds: float) -> None:
    try:
        ORCH_TTS_SECONDS.observe(seconds)
    except Exception:
        pass


def observe_turn_seconds(seconds: float) -> None:
    try:
        ORCH_TURN_SECONDS.observe(seconds)
    except Exception:
        pass


def inc_barge_in_interrupts(source: str = "audio") -> None:
    try:
        ORCH_BARGE_IN_INTERRUPTS_TOTAL.labels(source=source).inc()
    except Exception:
        pass


def inc_turn_watchdog_timeouts(phase: str = "total") -> None:
    try:
        ORCH_TURN_WATCHDOG_TIMEOUTS_TOTAL.labels(phase=phase).inc()
    except Exception:
        pass


def inc_session_evictions(cause: str, count: int = 1) -> None:
    try:
        ORCH_SESSION_EVICTIONS_TOTAL.labels(cause=cause).inc(max(0, int(count)))
    except Exception:
        pass


def set_queue_depth(queue: str, depth: int) -> None:
    try:
        ORCH_QUEUE_DEPTH.labels(queue=queue).set(max(0, int(depth)))
    except Exception:
        pass


def inc_queue_drops(queue: str, count: int = 1) -> None:
    try:
        ORCH_QUEUE_DROPS_TOTAL.labels(queue=queue).inc(max(0, int(count)))
    except Exception:
        pass


def record_tool_output(status: str, size_bytes: int) -> None:
    try:
        ORCH_TOOL_OUTPUTS_TOTAL.labels(status=status).inc()
    except Exception:
        pass
    try:
        ORCH_TOOL_OUTPUT_SIZE_BYTES.labels(status=status).observe(max(0, int(size_bytes)))
    except Exception:
        pass


def inc_turn_outcome(outcome: str) -> None:
    try:
        ORCH_TURN_OUTCOMES_TOTAL.labels(outcome=outcome).inc()
    except Exception:
        pass


def set_sessions_active(count: int) -> None:
    try:
        ORCH_SESSIONS_ACTIVE.set(max(0, int(count)))
    except Exception:
        pass


def inc_sessions_created(count: int = 1) -> None:
    try:
        ORCH_SESSIONS_CREATED_TOTAL.inc(max(0, int(count)))
    except Exception:
        pass


def inc_sessions_destroyed(reason: str, count: int = 1) -> None:
    try:
        ORCH_SESSIONS_DESTROYED_TOTAL.labels(reason=reason).inc(max(0, int(count)))
    except Exception:
        pass


def observe_barge_in_to_audio_last_seconds(seconds: float) -> None:
    try:
        ORCH_BARGE_IN_TO_AUDIO_LAST_SECONDS.observe(max(0.0, float(seconds)))
    except Exception:
        pass


def metrics_payload() -> bytes:
    return generate_latest()


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST


def start_metrics_server(port: int) -> None:
    start_http_server(port)


_sync_metrics(_STORE.snapshot())
