import json
import logging
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, List

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _state_path(config: dict[str, Any]) -> Path:
    raw = str(config.get("state_file") or "/ai/server/tools_server/state/timer_alarm.json")
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


def _save_state(path: Path, timers: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(timers, ensure_ascii=True, indent=2), encoding="utf-8")


def _purge_expired(timers: list[dict[str, Any]], now_ts: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in timers:
        end_epoch = t.get("end_epoch")
        if isinstance(end_epoch, (int, float)) and float(end_epoch) > now_ts:
            out.append(t)
    return out


def _format_remaining(end_epoch: float, now_ts: float) -> str:
    left = max(0, int(round(end_epoch - now_ts)))
    mins, secs = divmod(left, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def register(server) -> List[str]:
    log = logging.getLogger("tools.timer_alarm")
    config = _load_config()
    state_file = _state_path(config)

    tool_description, param_meta = get_tool_config(
        config,
        "timer_alarm",
        "Manage lightweight household timers and alarms.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: set, status, list, cancel, clear.",
    )
    name_desc, name_alias, name_title = get_param_meta(
        param_meta,
        "name",
        "Optional timer label.",
    )
    minutes_desc, minutes_alias, minutes_title = get_param_meta(
        param_meta,
        "minutes",
        "Optional duration in minutes for set.",
    )
    seconds_desc, seconds_alias, seconds_title = get_param_meta(
        param_meta,
        "seconds",
        "Optional duration in seconds for set.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="timer_alarm",
        description=tool_description,
    )
    def timer_alarm(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "status",
        name: Annotated[str | None, Field(**_field_kwargs(name_desc, name_alias, name_title))] = None,
        minutes: Annotated[int | None, Field(**_field_kwargs(minutes_desc, minutes_alias, minutes_title))] = None,
        seconds: Annotated[int | None, Field(**_field_kwargs(seconds_desc, seconds_alias, seconds_title))] = None,
    ) -> str:
        log_tool_call(
            log,
            "timer_alarm",
            action=action,
            name=name,
            minutes=minutes,
            seconds=seconds,
        )
        now_ts = time.time()
        timers = _purge_expired(_load_state(state_file), now_ts)
        action_value = str(action or "status").strip().lower()
        name_value = str(name or "").strip()

        if action_value == "set":
            total_seconds = 0
            if isinstance(minutes, int):
                total_seconds += max(0, minutes) * 60
            if isinstance(seconds, int):
                total_seconds += max(0, seconds)
            if total_seconds <= 0:
                return log_tool_result(log, "timer_alarm", "Set requires minutes or seconds greater than zero.")
            timer_name = name_value or "timer"
            timer = {
                "id": str(uuid.uuid4())[:8],
                "name": timer_name,
                "created_epoch": now_ts,
                "end_epoch": now_ts + total_seconds,
            }
            timers.append(timer)
            _save_state(state_file, timers)
            eta = _format_remaining(float(timer["end_epoch"]), now_ts)
            return log_tool_result(log, "timer_alarm", f"Set {timer_name} ({timer['id']}) for {eta}.")

        if action_value == "cancel":
            if not name_value:
                return log_tool_result(log, "timer_alarm", "Cancel requires a timer name or ID.")
            remaining: list[dict[str, Any]] = []
            removed = 0
            target = name_value.lower()
            for timer in timers:
                tid = str(timer.get("id") or "").lower()
                tname = str(timer.get("name") or "").lower()
                if tid == target or tname == target:
                    removed += 1
                    continue
                remaining.append(timer)
            _save_state(state_file, remaining)
            if removed == 0:
                return log_tool_result(log, "timer_alarm", f"No timer matched '{name_value}'.")
            return log_tool_result(log, "timer_alarm", f"Canceled {removed} timer(s).")

        if action_value == "clear":
            _save_state(state_file, [])
            return log_tool_result(log, "timer_alarm", "Cleared all timers.")

        if action_value in {"status", "list"}:
            if not timers:
                return log_tool_result(log, "timer_alarm", "No active timers.")
            lines: list[str] = []
            for timer in sorted(timers, key=lambda t: float(t.get("end_epoch") or 0.0)):
                tid = str(timer.get("id") or "")
                tname = str(timer.get("name") or "timer")
                end_epoch = float(timer.get("end_epoch") or now_ts)
                left = _format_remaining(end_epoch, now_ts)
                lines.append(f"- {tname} ({tid}): {left} remaining")
            if action_value == "status" and name_value:
                target = name_value.lower()
                for line, timer in zip(lines, sorted(timers, key=lambda t: float(t.get("end_epoch") or 0.0))):
                    tid = str(timer.get("id") or "").lower()
                    tname = str(timer.get("name") or "").lower()
                    if target in {tid, tname}:
                        return log_tool_result(log, "timer_alarm", line.lstrip("- ").strip())
                return log_tool_result(log, "timer_alarm", f"No timer matched '{name_value}'.")
            return log_tool_result(log, "timer_alarm", "Active timers:\n" + "\n".join(lines))

        return log_tool_result(
            log,
            "timer_alarm",
            "Unsupported action. Use one of: set, status, list, cancel, clear.",
        )

    return ["timer_alarm"]

