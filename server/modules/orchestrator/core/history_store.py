from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from typing import Any


class HistoryStore:
    def __init__(self, path: str, logger=None) -> None:
        self._path = path
        self._log = logger
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS history (session_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def load(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            return []
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM history WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
        if not row:
            return []
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if not session_id:
            return
        payload = json.dumps(messages, ensure_ascii=True)
        now = int(time.time())

        def _do() -> None:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO history (session_id, payload, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                    (session_id, payload, now),
                )
                self._conn.commit()

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _do)
        except RuntimeError:
            # No running event loop (e.g., called from tests or shutdown).
            _do()

    def delete(self, session_id: str) -> None:
        if not session_id:
            return

        def _do() -> None:
            with self._lock:
                self._conn.execute("DELETE FROM history WHERE session_id = ?", (session_id,))
                self._conn.commit()

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _do)
        except RuntimeError:
            _do()
