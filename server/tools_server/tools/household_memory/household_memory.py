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
    raw = str(config.get("state_file") or "/ai/server/tools_server/state/household_memory.json")
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
    return out


def _save_state(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=True, indent=2), encoding="utf-8")


def _norm_key(key: str) -> str:
    return "_".join(str(key or "").strip().lower().split())


def _parse_tags(tags: str | None) -> list[str]:
    if not tags:
        return []
    out = []
    for chunk in str(tags).split(","):
        value = chunk.strip().lower()
        if value:
            out.append(value)
    return out


def register(server) -> List[str]:
    log = logging.getLogger("tools.household_memory")
    config = _load_config()
    state_file = _state_path(config)

    tool_description, param_meta = get_tool_config(
        config,
        "household_memory",
        "Store and retrieve simple household memory entries.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: upsert, get, delete, list, search, clear.",
    )
    key_desc, key_alias, key_title = get_param_meta(
        param_meta,
        "key",
        "Memory key.",
    )
    value_desc, value_alias, value_title = get_param_meta(
        param_meta,
        "value",
        "Memory value for upsert.",
    )
    tags_desc, tags_alias, tags_title = get_param_meta(
        param_meta,
        "tags",
        "Optional comma-separated tags.",
    )
    query_desc, query_alias, query_title = get_param_meta(
        param_meta,
        "query",
        "Freeform search query.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="household_memory",
        description=tool_description,
    )
    def household_memory(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "list",
        key: Annotated[str | None, Field(**_field_kwargs(key_desc, key_alias, key_title))] = None,
        value: Annotated[str | None, Field(**_field_kwargs(value_desc, value_alias, value_title))] = None,
        tags: Annotated[str | None, Field(**_field_kwargs(tags_desc, tags_alias, tags_title))] = None,
        query: Annotated[str | None, Field(**_field_kwargs(query_desc, query_alias, query_title))] = None,
    ) -> str:
        log_tool_call(log, "household_memory", action=action, key=key, tags=tags, query=query)
        entries = _load_state(state_file)
        action_value = str(action or "list").strip().lower()
        key_norm = _norm_key(str(key or ""))
        now_ts = int(time.time())

        if action_value == "upsert":
            if not key_norm:
                return log_tool_result(log, "household_memory", "Upsert requires key.")
            text = str(value or "").strip()
            if not text:
                return log_tool_result(log, "household_memory", "Upsert requires value.")
            tag_list = _parse_tags(tags)
            updated = False
            for entry in entries:
                if _norm_key(str(entry.get("key") or "")) == key_norm:
                    entry["value"] = text
                    entry["tags"] = tag_list
                    entry["updated_epoch"] = now_ts
                    updated = True
                    break
            if not updated:
                entries.append(
                    {
                        "key": key_norm,
                        "value": text,
                        "tags": tag_list,
                        "updated_epoch": now_ts,
                    }
                )
            _save_state(state_file, entries)
            return log_tool_result(log, "household_memory", f"Saved memory '{key_norm}'.")

        if action_value == "get":
            if not key_norm:
                return log_tool_result(log, "household_memory", "Get requires key.")
            for entry in entries:
                if _norm_key(str(entry.get("key") or "")) == key_norm:
                    tags_text = ", ".join(entry.get("tags") or [])
                    if tags_text:
                        return log_tool_result(
                            log,
                            "household_memory",
                            f"{entry.get('key')}: {entry.get('value')} (tags: {tags_text})",
                        )
                    return log_tool_result(log, "household_memory", f"{entry.get('key')}: {entry.get('value')}")
            return log_tool_result(log, "household_memory", f"No memory found for '{key_norm}'.")

        if action_value == "delete":
            if not key_norm:
                return log_tool_result(log, "household_memory", "Delete requires key.")
            kept = [e for e in entries if _norm_key(str(e.get("key") or "")) != key_norm]
            if len(kept) == len(entries):
                return log_tool_result(log, "household_memory", f"No memory found for '{key_norm}'.")
            _save_state(state_file, kept)
            return log_tool_result(log, "household_memory", f"Deleted memory '{key_norm}'.")

        if action_value == "clear":
            _save_state(state_file, [])
            return log_tool_result(log, "household_memory", "Cleared all memories.")

        if action_value == "search":
            query_text = str(query or "").strip().lower()
            tags_filter = set(_parse_tags(tags))
            if not query_text and not tags_filter:
                return log_tool_result(log, "household_memory", "Search requires query or tags.")
            results: list[dict[str, Any]] = []
            for entry in entries:
                key_text = str(entry.get("key") or "").lower()
                value_text = str(entry.get("value") or "").lower()
                entry_tags = {str(t).lower() for t in (entry.get("tags") or []) if isinstance(t, str)}
                query_match = not query_text or query_text in key_text or query_text in value_text
                tags_match = not tags_filter or tags_filter.issubset(entry_tags)
                if query_match and tags_match:
                    results.append(entry)
            if not results:
                return log_tool_result(log, "household_memory", "No matching memories found.")
            lines = [f"Found {len(results)} memories:"]
            for entry in results[:20]:
                tags_text = ", ".join(entry.get("tags") or [])
                if tags_text:
                    lines.append(f"- {entry.get('key')}: {entry.get('value')} (tags: {tags_text})")
                else:
                    lines.append(f"- {entry.get('key')}: {entry.get('value')}")
            return log_tool_result(log, "household_memory", "\n".join(lines))

        if action_value != "list":
            return log_tool_result(
                log,
                "household_memory",
                "Unsupported action. Use one of: upsert, get, delete, list, search, clear.",
            )

        if not entries:
            return log_tool_result(log, "household_memory", "No memories stored.")
        lines = [f"Stored memories ({len(entries)}):"]
        for entry in sorted(entries, key=lambda e: int(e.get("updated_epoch") or 0), reverse=True)[:30]:
            tags_text = ", ".join(entry.get("tags") or [])
            if tags_text:
                lines.append(f"- {entry.get('key')}: {entry.get('value')} (tags: {tags_text})")
            else:
                lines.append(f"- {entry.get('key')}: {entry.get('value')}")
        return log_tool_result(log, "household_memory", "\n".join(lines))

    return ["household_memory"]

