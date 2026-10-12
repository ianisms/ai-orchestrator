import logging
import os
from typing import Annotated, Any, List

import httpx
from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_PRIORITY_MAP = {
    "min": 1, "minimum": 1,
    "low": 2,
    "default": 3, "normal": 3, "medium": 3,
    "high": 4,
    "urgent": 5, "max": 5, "maximum": 5, "critical": 5,
}


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _resolve_priority(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return max(1, min(5, value))
    s = str(value).strip().lower()
    if s.isdigit():
        return max(1, min(5, int(s)))
    return _PRIORITY_MAP.get(s, default)


def register(server) -> List[str]:
    log = logging.getLogger("tools.push_notify")
    config = _load_config()

    ntfy_url = str(config.get("ntfy_url") or os.getenv("NTFY_URL") or "https://ntfy.sh").rstrip("/")
    ntfy_topic = str(config.get("ntfy_topic") or os.getenv("NTFY_TOPIC") or "").strip()
    ntfy_token = str(config.get("ntfy_token") or os.getenv("NTFY_TOKEN") or "").strip()
    default_priority = int(config.get("default_priority") or 3)

    tool_description, param_meta = get_tool_config(
        config,
        "push_notify",
        (
            "Send a push notification to the user's phone via ntfy. "
            "Use this to alert the user about timers, reminders, important events, or anything "
            "they'd want to know even when away from the speaker."
        ),
    )
    message_desc, message_alias, message_title = get_param_meta(
        param_meta, "message", "The notification body text. Keep it concise."
    )
    title_desc, title_alias, title_title = get_param_meta(
        param_meta, "title", "Optional notification title. Defaults to 'Assistant'."
    )
    priority_desc, priority_alias, priority_title = get_param_meta(
        param_meta,
        "priority",
        "Notification priority: min, low, default, high, or urgent. Defaults to default.",
    )
    tags_desc, tags_alias, tags_title = get_param_meta(
        param_meta,
        "tags",
        "Optional comma-separated ntfy tag names (maps to emoji icons, e.g. 'warning,alarm_clock').",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="push_notify",
        description=tool_description,
    )
    def push_notify(
        message: Annotated[str, Field(**_field_kwargs(message_desc, message_alias, message_title))],
        title: Annotated[str | None, Field(**_field_kwargs(title_desc, title_alias, title_title))] = None,
        priority: Annotated[str | None, Field(**_field_kwargs(priority_desc, priority_alias, priority_title))] = None,
        tags: Annotated[str | None, Field(**_field_kwargs(tags_desc, tags_alias, tags_title))] = None,
    ) -> str:
        log_tool_call(log, "push_notify", message=message, title=title, priority=priority, tags=tags)

        if not ntfy_topic:
            return log_tool_result(log, "push_notify", "Push notification topic is not configured (NTFY_TOPIC).")

        msg_text = str(message or "").strip()
        if not msg_text:
            return log_tool_result(log, "push_notify", "Message is required.")

        priority_int = _resolve_priority(priority, default_priority)
        notification_title = str(title or "").strip() or "Assistant"
        url = f"{ntfy_url}/{ntfy_topic}"

        headers: dict[str, str] = {
            "Title": notification_title,
            "Priority": str(priority_int),
            "Content-Type": "text/plain",
        }
        if ntfy_token:
            headers["Authorization"] = f"Bearer {ntfy_token}"
        if tags:
            tag_list = ",".join(t.strip() for t in str(tags).split(",") if t.strip())
            if tag_list:
                headers["Tags"] = tag_list

        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.post(url, content=msg_text.encode("utf-8"), headers=headers)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return log_tool_result(
                log, "push_notify", f"Push notification failed: HTTP {exc.response.status_code}."
            )
        except Exception as exc:
            return log_tool_result(log, "push_notify", f"Push notification failed: {exc!r}")

        return log_tool_result(log, "push_notify", f"Notification sent: {notification_title!r} → {ntfy_topic}.")

    return ["push_notify"]
