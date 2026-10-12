import json
import threading
from pathlib import Path
from typing import Any


class StatsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._lifetime: dict[str, Any] = {"total": 0, "tools": {}}
        self._uptime: dict[str, Any] = {"total": 0, "tools": {}}
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

    def record(self, tool: str) -> None:
        with self._lock:
            self._uptime["total"] = int(self._uptime.get("total", 0)) + 1
            self._lifetime["total"] = int(self._lifetime.get("total", 0)) + 1
            uptime_tools = self._uptime.setdefault("tools", {})
            lifetime_tools = self._lifetime.setdefault("tools", {})
            uptime_tools[tool] = int(uptime_tools.get(tool, 0)) + 1
            lifetime_tools[tool] = int(lifetime_tools.get(tool, 0)) + 1
            self._save()

    def reset_uptime(self) -> None:
        with self._lock:
            self._uptime = {"total": 0, "tools": {}}

    def reset_all(self) -> None:
        with self._lock:
            self._uptime = {"total": 0, "tools": {}}
            self._lifetime = {"total": 0, "tools": {}}
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime": json.loads(json.dumps(self._uptime)),
                "lifetime": json.loads(json.dumps(self._lifetime)),
            }
