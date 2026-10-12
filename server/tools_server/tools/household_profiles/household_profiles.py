import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any, List

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _state_path(config: dict[str, Any]) -> Path:
    raw = str(config.get("state_file") or "/ai/server/tools_server/state/household_profiles.json")
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"active_profile_id": "", "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active_profile_id": "", "profiles": {}}
    if not isinstance(data, dict):
        return {"active_profile_id": "", "profiles": {}}
    active = data.get("active_profile_id")
    profiles = data.get("profiles")
    return {
        "active_profile_id": str(active or ""),
        "profiles": profiles if isinstance(profiles, dict) else {},
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True)


def _parse_prefs(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def register(server) -> List[str]:
    log = logging.getLogger("tools.household_profiles")
    config = _load_config()
    state_file = _state_path(config)

    tool_description, param_meta = get_tool_config(
        config,
        "household_profiles",
        "Manage household profiles and voice preferences.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: upsert, get, delete, list, set_active, get_active.",
    )
    profile_id_desc, profile_id_alias, profile_id_title = get_param_meta(
        param_meta,
        "profile_id",
        "Profile identifier.",
    )
    display_name_desc, display_name_alias, display_name_title = get_param_meta(
        param_meta,
        "display_name",
        "Display name for the profile.",
    )
    prefs_desc, prefs_alias, prefs_title = get_param_meta(
        param_meta,
        "preferences_json",
        "JSON object string for preferences.",
    )
    role_desc, role_alias, role_title = get_param_meta(
        param_meta,
        "role",
        "Optional role value.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(name="household_profiles", description=tool_description)
    def household_profiles(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "list",
        profile_id: Annotated[str | None, Field(**_field_kwargs(profile_id_desc, profile_id_alias, profile_id_title))] = None,
        display_name: Annotated[
            str | None,
            Field(**_field_kwargs(display_name_desc, display_name_alias, display_name_title)),
        ] = None,
        preferences_json: Annotated[str | None, Field(**_field_kwargs(prefs_desc, prefs_alias, prefs_title))] = None,
        role: Annotated[str | None, Field(**_field_kwargs(role_desc, role_alias, role_title))] = None,
    ) -> str:
        log_tool_call(log, "household_profiles", action=action, profile_id=profile_id)
        state = _load_state(state_file)
        profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
        action_value = str(action or "list").strip().lower()
        profile_key = str(profile_id or "").strip().lower()
        now_ts = int(time.time())

        if action_value == "upsert":
            if not profile_key:
                return log_tool_result(log, "household_profiles", _as_json({"status": "error", "message": "profile_id required"}))
            entry = profiles.get(profile_key) if isinstance(profiles.get(profile_key), dict) else {}
            prefs = _parse_prefs(preferences_json)
            if preferences_json and not prefs and str(preferences_json).strip() not in {"{}", ""}:
                return log_tool_result(
                    log,
                    "household_profiles",
                    _as_json({"status": "error", "message": "preferences_json must be a JSON object"}),
                )
            entry["profile_id"] = profile_key
            entry["display_name"] = str(display_name or entry.get("display_name") or profile_key).strip()
            entry["role"] = str(role or entry.get("role") or "member").strip().lower()
            entry["preferences"] = prefs if preferences_json is not None else entry.get("preferences") or {}
            entry["updated_epoch"] = now_ts
            profiles[profile_key] = entry
            if not str(state.get("active_profile_id") or "").strip():
                state["active_profile_id"] = profile_key
            state["profiles"] = profiles
            _save_state(state_file, state)
            return log_tool_result(log, "household_profiles", _as_json({"status": "ok", "profile": entry}))

        if action_value == "get":
            if not profile_key:
                return log_tool_result(log, "household_profiles", _as_json({"status": "error", "message": "profile_id required"}))
            entry = profiles.get(profile_key)
            if not isinstance(entry, dict):
                return log_tool_result(log, "household_profiles", _as_json({"status": "not_found", "profile_id": profile_key}))
            return log_tool_result(log, "household_profiles", _as_json({"status": "ok", "profile": entry}))

        if action_value == "delete":
            if not profile_key:
                return log_tool_result(log, "household_profiles", _as_json({"status": "error", "message": "profile_id required"}))
            existed = profile_key in profiles
            profiles.pop(profile_key, None)
            if str(state.get("active_profile_id") or "") == profile_key:
                state["active_profile_id"] = ""
            state["profiles"] = profiles
            _save_state(state_file, state)
            return log_tool_result(
                log,
                "household_profiles",
                _as_json({"status": "ok", "deleted": bool(existed), "profile_id": profile_key}),
            )

        if action_value == "set_active":
            if not profile_key:
                return log_tool_result(log, "household_profiles", _as_json({"status": "error", "message": "profile_id required"}))
            if not isinstance(profiles.get(profile_key), dict):
                return log_tool_result(log, "household_profiles", _as_json({"status": "not_found", "profile_id": profile_key}))
            state["active_profile_id"] = profile_key
            _save_state(state_file, state)
            return log_tool_result(log, "household_profiles", _as_json({"status": "ok", "active_profile_id": profile_key}))

        if action_value == "get_active":
            active = str(state.get("active_profile_id") or "").strip().lower()
            entry = profiles.get(active) if active else None
            return log_tool_result(
                log,
                "household_profiles",
                _as_json({"status": "ok", "active_profile_id": active, "profile": entry if isinstance(entry, dict) else None}),
            )

        if action_value != "list":
            return log_tool_result(
                log,
                "household_profiles",
                _as_json({"status": "error", "message": "unsupported action: use upsert,get,delete,list,set_active,get_active"}),
            )

        items = []
        for key in sorted(profiles.keys())[:100]:
            entry = profiles.get(key)
            if not isinstance(entry, dict):
                continue
            items.append(
                {
                    "profile_id": key,
                    "display_name": str(entry.get("display_name") or key),
                    "role": str(entry.get("role") or "member"),
                    "updated_epoch": int(entry.get("updated_epoch") or 0),
                }
            )
        return log_tool_result(
            log,
            "household_profiles",
            _as_json({"status": "ok", "active_profile_id": str(state.get("active_profile_id") or ""), "profiles": items}),
        )

    return ["household_profiles"]

