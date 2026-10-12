import logging
from datetime import datetime, timezone
from typing import Annotated, Any, List
from zoneinfo import ZoneInfo

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_TZ_ABBREVIATIONS: dict[str, str] = {
    # North America
    "EDT": "America/New_York",
    "EST": "America/New_York",
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "MDT": "America/Denver",
    "MST": "America/Denver",
    "PDT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "AKDT": "America/Anchorage",
    "AKST": "America/Anchorage",
    "HST": "Pacific/Honolulu",
    # Europe
    "GMT": "Europe/London",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "EET": "Europe/Helsinki",
    "EEST": "Europe/Helsinki",
    "WET": "Europe/Lisbon",
    "WEST": "Europe/Lisbon",
    # Asia Pacific
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
    "IST": "Asia/Kolkata",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "AWST": "Australia/Perth",
    # Others
    "UTC": "UTC",
    "GST": "Asia/Dubai",
}


def _resolve_timezone(value: str) -> ZoneInfo:
    trimmed = value.strip()
    if not trimmed:
        raise KeyError("empty timezone")
    upper = trimmed.upper()
    if upper in _TZ_ABBREVIATIONS:
        return ZoneInfo(_TZ_ABBREVIATIONS[upper])
    if "/" in trimmed:
        return ZoneInfo(trimmed)
    raise KeyError(trimmed)


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def register(server) -> List[str]:
    log = logging.getLogger("tools.time")
    config = _load_config()
    tool_description, param_meta = get_tool_config(
        config,
        "current_time",
        (
            "Get the current time for an optional timezone abbreviation "
            "(e.g., 'EDT', 'PST', 'GMT') or server time if no timezone is provided."
        ),
    )
    tz_desc, tz_alias, tz_title = get_param_meta(
        param_meta,
        "timezone_abbr",
        "Optional timezone abbreviation or IANA timezone name.",
    )
    include_date_desc, include_date_alias, include_date_title = get_param_meta(
        param_meta,
        "include_date",
        "Optional. Include the date in the response (default false).",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="current_time",
        description=tool_description,
    )
    def current_time(
        timezone_abbr: Annotated[
            str | None,
            Field(**_field_kwargs(tz_desc, tz_alias, tz_title)),
        ] = None,
        include_date: Annotated[
            bool,
            Field(**_field_kwargs(include_date_desc, include_date_alias, include_date_title)),
        ] = False,
    ) -> str:
        log_tool_call(log, "current_time", timezone_abbr=timezone_abbr, include_date=include_date)
        time_fmt = "%Y-%m-%d %H:%M:%S" if include_date else "%H:%M:%S"
        if timezone_abbr is None or not str(timezone_abbr).strip():
            now_local = datetime.now().astimezone()
            tz_label = now_local.tzname() or "local"
            return log_tool_result(
                log,
                "current_time",
                (
                "No timezone was provided, using server time. The current time is: "
                f"{now_local:{time_fmt}} {tz_label}"
                ),
            )
        try:
            tz = _resolve_timezone(str(timezone_abbr))
        except KeyError:
            return log_tool_result(
                log,
                "current_time",
                (
                "Unable to find timezone for abbreviation: "
                f"{timezone_abbr}. Supported abbreviations: "
                + ", ".join(sorted(_TZ_ABBREVIATIONS.keys()))
                ),
            )
        now_local = datetime.now(timezone.utc).astimezone(tz)
        raw_label = str(timezone_abbr).strip()
        tz_spoken = {
            "EST": "eastern standard time",
            "EDT": "eastern daylight time",
            "CST": "central standard time",
            "CDT": "central daylight time",
            "MST": "mountain standard time",
            "MDT": "mountain daylight time",
            "PST": "pacific standard time",
            "PDT": "pacific daylight time",
            "GMT": "greenwich mean time",
            "UTC": "coordinated universal time",
        }
        if "/" in raw_label:
            label = raw_label.split("/")[-1].replace("_", " ")
        else:
            label = raw_label.upper()
            label = tz_spoken.get(label, label)
        return log_tool_result(
            log,
            "current_time",
            f"The current time in {tz.key} is: {now_local:{time_fmt}} {label}",
        )

    return ["current_time"]
