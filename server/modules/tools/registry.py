from __future__ import annotations

import copy
import json
import threading
from typing import Any

_LOCK = threading.RLock()
_TOOLS: list[dict[str, Any]] = []
_HIDDEN: set[str] = set()
_VERSION = 0


def _normalize_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
    if fn is None:
        name = tool.get("name")
        description = tool.get("description")
        parameters = tool.get("parameters")
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(description, str):
            description = "No description."
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": name.strip(),
                "description": description,
                "parameters": parameters,
            },
        }

    name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    description = fn.get("description")
    if not isinstance(description, str):
        description = "No description."
    parameters = fn.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name.strip(),
            "description": description,
            "parameters": parameters,
        },
    }


def _canonical_tools(tools: list[dict[str, Any]]) -> str:
    try:
        return json.dumps(tools, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    except Exception:
        return repr(tools)


def set_tools(tools: list[dict[str, Any]]) -> bool:
    """Replace registry tools. Returns True if content changed."""
    global _TOOLS, _VERSION
    normalized: list[dict[str, Any]] = []
    if isinstance(tools, list):
        for tool in tools:
            fixed = _normalize_tool(tool)
            if fixed is not None:
                normalized.append(fixed)

    with _LOCK:
        prev_key = _canonical_tools(_TOOLS)
        new_key = _canonical_tools(normalized)
        if prev_key == new_key:
            return False
        _TOOLS = normalized
        _VERSION += 1
        return True


def set_hidden_tools(names: set[str]) -> None:
    """Set tool names that are hidden from LLM but still callable."""
    global _HIDDEN
    with _LOCK:
        _HIDDEN = set(names)


def list_tools() -> list[dict[str, Any]]:
    with _LOCK:
        if not _HIDDEN:
            return copy.deepcopy(_TOOLS)
        return copy.deepcopy([
            t for t in _TOOLS
            if t.get("function", {}).get("name") not in _HIDDEN
        ])


def get_tools_version() -> int:
    with _LOCK:
        return int(_VERSION)


def parse_tool_args(args_raw: Any) -> dict[str, Any]:
    if isinstance(args_raw, dict):
        return dict(args_raw)
    if not isinstance(args_raw, str):
        return {}
    text = args_raw.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}

