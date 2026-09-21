import logging
import os
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from utils.stats_store import StatsStore

_LOG = logging.getLogger("tools.stats")


def _default_stats_path() -> Path:
    env_path = os.getenv("TOOLS_STATS_PATH")
    if env_path:
        return Path(env_path)
    if Path("/ai/server/state").exists():
        return Path("/ai/server/state/tools_stats.json")
    return Path("/app/state/tools_stats.json")


_STORE = StatsStore(_default_stats_path())

TOOLS_CALLS_TOTAL = Gauge(
    "tools_tool_calls_total",
    "Tool calls since uptime by tool",
    ["tool"],
)
TOOLS_CALLS_LIFETIME_TOTAL = Gauge(
    "tools_tool_calls_lifetime_total",
    "Tool calls lifetime by tool",
    ["tool"],
)
TOOLS_CALLS_ALL_TOTAL = Gauge(
    "tools_tool_calls_all_total",
    "Tool calls since uptime across all tools",
)
TOOLS_CALLS_ALL_LIFETIME_TOTAL = Gauge(
    "tools_tool_calls_all_lifetime_total",
    "Tool calls lifetime across all tools",
)


def _sync_metrics(snapshot: dict[str, Any]) -> None:
    uptime = snapshot.get("uptime", {})
    lifetime = snapshot.get("lifetime", {})
    TOOLS_CALLS_ALL_TOTAL.set(float(uptime.get("total", 0)))
    TOOLS_CALLS_ALL_LIFETIME_TOTAL.set(float(lifetime.get("total", 0)))

    TOOLS_CALLS_TOTAL.clear()
    TOOLS_CALLS_LIFETIME_TOTAL.clear()
    for tool, count in (uptime.get("tools", {}) or {}).items():
        TOOLS_CALLS_TOTAL.labels(tool=str(tool)).set(float(count))
    for tool, count in (lifetime.get("tools", {}) or {}).items():
        TOOLS_CALLS_LIFETIME_TOTAL.labels(tool=str(tool)).set(float(count))


def record_tool_call(tool_name: str) -> None:
    _STORE.record(tool_name)
    _sync_metrics(_STORE.snapshot())


def reset_uptime() -> None:
    _STORE.reset_uptime()
    _sync_metrics(_STORE.snapshot())


def reset_all() -> None:
    _STORE.reset_all()
    _sync_metrics(_STORE.snapshot())


def metrics_payload() -> bytes:
    return generate_latest()


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST


_sync_metrics(_STORE.snapshot())
