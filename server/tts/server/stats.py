import json
import os
import threading
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest


class StatsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._lifetime: dict[str, Any] = {"total": 0}
        self._uptime: dict[str, Any] = {"total": 0}
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

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"lifetime": self._lifetime}
        self._path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

    def record(self) -> None:
        with self._lock:
            self._uptime["total"] = int(self._uptime.get("total", 0)) + 1
            self._lifetime["total"] = int(self._lifetime.get("total", 0)) + 1
            self._save()

    def reset_uptime(self) -> None:
        with self._lock:
            self._uptime = {"total": 0}

    def reset_all(self) -> None:
        with self._lock:
            self._uptime = {"total": 0}
            self._lifetime = {"total": 0}
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime": json.loads(json.dumps(self._uptime)),
                "lifetime": json.loads(json.dumps(self._lifetime)),
            }


def _default_stats_path() -> Path:
    env_path = os.getenv("TTS_STATS_PATH")
    if env_path:
        return Path(env_path)
    if Path("/ai/server/state").exists():
        return Path("/ai/server/state/tts_stats.json")
    return Path("/app/state/tts_stats.json")


_STORE = StatsStore(_default_stats_path())

TTS_REQUESTS_TOTAL = Gauge(
    "tts_requests_total",
    "TTS requests since uptime",
)
TTS_REQUESTS_LIFETIME_TOTAL = Gauge(
    "tts_requests_lifetime_total",
    "TTS requests lifetime",
)


def _sync_metrics(snapshot: dict[str, Any]) -> None:
    uptime = snapshot.get("uptime", {})
    lifetime = snapshot.get("lifetime", {})
    TTS_REQUESTS_TOTAL.set(float(uptime.get("total", 0)))
    TTS_REQUESTS_LIFETIME_TOTAL.set(float(lifetime.get("total", 0)))


def record_request() -> None:
    _STORE.record()
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
