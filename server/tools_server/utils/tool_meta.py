from __future__ import annotations

from typing import Any


def get_tool_config(
    config: dict[str, Any],
    tool_name: str,
    default_description: str,
) -> tuple[str, dict[str, Any]]:
    tools_cfg = config.get("tools")
    tool_cfg: dict[str, Any] = {}
    if isinstance(tools_cfg, dict):
        tool_cfg = tools_cfg.get(tool_name, {}) if isinstance(tools_cfg.get(tool_name), dict) else {}
    else:
        single = config.get("tool")
        if isinstance(single, dict):
            tool_cfg = single
    description = str(tool_cfg.get("description") or default_description)
    params = tool_cfg.get("params") if isinstance(tool_cfg.get("params"), dict) else {}
    return description, params


def get_param_meta(
    params: dict[str, Any],
    param_name: str,
    default_description: str,
) -> tuple[str, str | None, str | None]:
    entry = params.get(param_name)
    if not isinstance(entry, dict):
        entry = {}
    description = str(entry.get("description") or default_description)
    alias = entry.get("name")
    title = entry.get("title")
    alias_value = str(alias).strip() if isinstance(alias, str) and alias.strip() else None
    title_value = str(title).strip() if isinstance(title, str) and title.strip() else None
    return description, alias_value, title_value
