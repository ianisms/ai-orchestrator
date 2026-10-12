import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any, List

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_LOCATION_HINT_PATTERNS = (
    re.compile(r"\bnear me\b", re.IGNORECASE),
    re.compile(r"\bnearby\b", re.IGNORECASE),
    re.compile(r"\bclosest\b", re.IGNORECASE),
    re.compile(r"\bopen now\b", re.IGNORECASE),
    re.compile(r"\b(?:in|near|around|at|within)\s+[A-Za-z0-9]", re.IGNORECASE),
)


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _state_path(config: dict[str, Any]) -> Path:
    raw = str(
        os.getenv("LOCATION_PREFS_DB_FILE")
        or config.get("db_file")
        or config.get("state_file")
        or "/app/state/location_preferences.sqlite3"
    )
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(path: Path) -> None:
    with _connect_db(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS location_preferences (
                client_id TEXT PRIMARY KEY,
                default_location TEXT NOT NULL,
                updated_epoch INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_location_preferences_updated
            ON location_preferences(updated_epoch)
            """
        )
        conn.commit()


def _get_client_default(path: Path, client_id: str) -> str:
    key = str(client_id or "").strip()
    with _connect_db(path) as conn:
        if key:
            row = conn.execute(
                "SELECT default_location FROM location_preferences WHERE client_id = ?",
                (key,),
            ).fetchone()
            if row:
                loc = str(row["default_location"] or "").strip()
                if loc:
                    return loc
        row = conn.execute(
            "SELECT default_location FROM location_preferences WHERE client_id = ''"
        ).fetchone()
        if row:
            return str(row["default_location"] or "").strip()
    return ""


def _set_client_default(path: Path, client_id: str, location: str) -> None:
    key = str(client_id or "").strip()
    loc = str(location or "").strip()
    now = int(time.time())
    with _connect_db(path) as conn:
        conn.execute(
            """
            INSERT INTO location_preferences (client_id, default_location, updated_epoch)
            VALUES (?, ?, ?)
            ON CONFLICT(client_id)
            DO UPDATE SET
                default_location = excluded.default_location,
                updated_epoch = excluded.updated_epoch
            """,
            (key, loc, now),
        )
        conn.commit()


def _clear_client_default(path: Path, client_id: str) -> None:
    key = str(client_id or "").strip()
    with _connect_db(path) as conn:
        conn.execute("DELETE FROM location_preferences WHERE client_id = ?", (key,))
        conn.commit()


def _has_location_hint(query_text: str) -> bool:
    text = str(query_text or "").strip()
    if not text:
        return False
    if "," in text:
        return True
    if re.search(r"\b\d{5}(?:-\d{4})?\b", text):
        return True
    return any(p.search(text) for p in _LOCATION_HINT_PATTERNS)


def _as_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=True)


def register(server) -> List[str]:
    log = logging.getLogger("tools.location_preferences")
    config = _load_config()
    db_path = _state_path(config)
    _init_db(db_path)

    tool_description, param_meta = get_tool_config(
        config,
        "location_preferences",
        "Manage and resolve default location preferences for local search.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: resolve_query, set_default, get_default, clear_default.",
    )
    query_desc, query_alias, query_title = get_param_meta(
        param_meta,
        "query",
        "Query to resolve.",
    )
    location_desc, location_alias, location_title = get_param_meta(
        param_meta,
        "location",
        "Location string for set_default.",
    )
    use_default_desc, use_default_alias, use_default_title = get_param_meta(
        param_meta,
        "use_default_when_missing",
        "For resolve_query, append default location when missing.",
    )
    client_desc, client_alias, client_title = get_param_meta(
        param_meta,
        "client_id",
        "Optional client identity key (for example client IP) for per-client default location.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="location_preferences",
        description=tool_description,
    )
    def location_preferences(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "get_default",
        query: Annotated[str | None, Field(**_field_kwargs(query_desc, query_alias, query_title))] = None,
        location: Annotated[str | None, Field(**_field_kwargs(location_desc, location_alias, location_title))] = None,
        use_default_when_missing: Annotated[
            bool,
            Field(**_field_kwargs(use_default_desc, use_default_alias, use_default_title)),
        ] = True,
        client_id: Annotated[str | None, Field(**_field_kwargs(client_desc, client_alias, client_title))] = None,
    ) -> str:
        log_tool_call(
            log,
            "location_preferences",
            action=action,
            query=query,
            location=location,
            use_default_when_missing=use_default_when_missing,
            client_id=client_id,
        )
        client_key = str(client_id or "").strip()
        default_location = _get_client_default(db_path, client_key)
        action_value = str(action or "get_default").strip().lower()

        if action_value == "get_default":
            return log_tool_result(
                log,
                "location_preferences",
                _as_json(
                    {
                        "status": "ok",
                        "default_location": default_location,
                        "has_default_location": bool(default_location),
                        "client_id": client_key,
                    }
                ),
            )

        if action_value == "set_default":
            loc = str(location or "").strip()
            if not loc:
                return log_tool_result(
                    log,
                    "location_preferences",
                    _as_json({"status": "error", "message": "location is required for set_default"}),
                )
            _set_client_default(db_path, client_key, loc)
            return log_tool_result(
                log,
                "location_preferences",
                _as_json(
                    {
                        "status": "ok",
                        "message": "default location saved",
                        "default_location": loc,
                        "client_id": client_key,
                    }
                ),
            )

        if action_value == "clear_default":
            _clear_client_default(db_path, client_key)
            return log_tool_result(
                log,
                "location_preferences",
                _as_json(
                    {
                        "status": "ok",
                        "message": "default location cleared",
                        "default_location": "",
                        "client_id": client_key,
                    }
                ),
            )

        if action_value == "resolve_query":
            query_text = str(query or "").strip()
            if not query_text:
                return log_tool_result(
                    log,
                    "location_preferences",
                    _as_json({"status": "error", "message": "query is required for resolve_query"}),
                )
            if _has_location_hint(query_text):
                return log_tool_result(
                    log,
                    "location_preferences",
                    _as_json(
                        {
                            "status": "query_has_location",
                            "query": query_text,
                            "default_location": default_location,
                            "has_default_location": bool(default_location),
                            "client_id": client_key,
                        }
                    ),
                )
            if bool(use_default_when_missing) and default_location:
                resolved = f"{query_text} near {default_location}"
                return log_tool_result(
                    log,
                    "location_preferences",
                    _as_json(
                        {
                            "status": "resolved_with_default",
                            "query": resolved,
                            "original_query": query_text,
                            "default_location": default_location,
                            "has_default_location": True,
                            "client_id": client_key,
                        }
                    ),
                )
            return log_tool_result(
                log,
                "location_preferences",
                _as_json(
                    {
                        "status": "needs_location",
                        "query": query_text,
                        "has_default_location": False,
                        "client_id": client_key,
                        "message": (
                            "What location should I use for this search? "
                            "Please share a city or address, and tell me if you want me to save it as your default."
                        ),
                    }
                ),
            )

        return log_tool_result(
            log,
            "location_preferences",
            _as_json(
                {
                    "status": "error",
                    "message": "unsupported action, use resolve_query, set_default, get_default, clear_default",
                }
            ),
        )

    return ["location_preferences"]
