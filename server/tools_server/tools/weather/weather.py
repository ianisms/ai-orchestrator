import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Any, List

import httpx
from pydantic import Field

from services.geocoding import geocode_location
from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _build_url(template: str, **params: object) -> str:
    url = template
    for key, value in params.items():
        url = url.replace("{" + key + "}", str(value))
    return url


def _units_meta(units: str) -> tuple[str, str, str]:
    normalized = units.strip().lower()
    if normalized in {"metric", "c", "celsius"}:
        return "metric", "C", "m/s"
    if normalized in {"standard", "k", "kelvin"}:
        return "standard", "K", "m/s"
    return "imperial", "F", "mph"


def _round_number(value: object) -> str:
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return "N/A"


def _day_label(index: int, timestamp: int, offset_seconds: int) -> str:
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"
    local_time = datetime.fromtimestamp(timestamp + offset_seconds, tz=timezone.utc)
    return local_time.strftime("%A")


def _format_alerts(alerts: list[dict[str, Any]], timezone_offset: int) -> list[str]:
    """Format weather alerts into emphasized text lines."""
    if not alerts:
        return []
    lines = ["", "⚠ WEATHER ALERTS:"]
    for alert in alerts:
        event = str(alert.get("event") or "Unknown alert").strip()
        sender = str(alert.get("sender_name") or "").strip()
        desc = str(alert.get("description") or "").strip()
        start_ts = alert.get("start")
        end_ts = alert.get("end")
        time_range = ""
        if start_ts and end_ts:
            start_dt = datetime.fromtimestamp(int(start_ts) + timezone_offset, tz=timezone.utc)
            end_dt = datetime.fromtimestamp(int(end_ts) + timezone_offset, tz=timezone.utc)
            start_str = start_dt.strftime("%a %b %d %I:%M %p")
            end_str = end_dt.strftime("%a %b %d %I:%M %p")
            time_range = f" ({start_str} to {end_str})"
        header = f"  ⚠ {event}{time_range}"
        if sender:
            header += f" — {sender}"
        lines.append(header)
        if desc:
            for para in desc.strip().split("\n"):
                para = para.strip()
                if para:
                    lines.append(f"    {para}")
    lines.append("")
    return lines


def _get_api_key(config: dict[str, Any]) -> str:
    return str(config.get("api_key") or os.getenv("OPENWEATHERMAP_API_KEY") or "")


def _get_geocode_key(config: dict[str, Any]) -> str:
    return str(config.get("geocode_api_key") or os.getenv("GOOGLE_API_KEY") or "")


def register(server) -> List[str]:
    log = logging.getLogger("tools.weather")
    config = _load_config()
    units = str(config.get("units") or "imperial")
    provider = str(config.get("provider") or "stub")
    geocode_url = str(
        config.get("geocode_url")
        or "https://nominatim.openstreetmap.org/search?q={location}&format=json&limit=1"
    )
    one_call_url = str(
        config.get("one_call_url")
        or (
            "https://api.openweathermap.org/data/3.0/onecall"
            "?lat={lat}&lon={lon}&appid={api_key}&units={units}"
        )
    )
    user_agent = str(config.get("user_agent") or "home-ai-tools/1.0")
    default_days = int(config.get("days") or 3)

    tool_description, param_meta = get_tool_config(
        config,
        "weather_forecast",
        (
            "Returns the weather forecast for a given location. "
            "Use this when the user asks about the weather in a specific location."
        ),
    )
    location_desc, location_alias, location_title = get_param_meta(
        param_meta,
        "location",
        "Location name or address, for example 'London, UK' or 'Springfield, IL'.",
    )
    days_desc, days_alias, days_title = get_param_meta(
        param_meta,
        "days",
        "Number of forecast days (1-8). Default is 3. Only increase when the user asks about a day beyond the 3-day window.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="weather_forecast",
        description=tool_description,
    )
    def weather_forecast(
        location: Annotated[str, Field(**_field_kwargs(location_desc, location_alias, location_title))],
        days: Annotated[int, Field(default=0, description=days_desc)] = 0,
    ) -> str:
        log_tool_call(log, "weather_forecast", location=location, days=days)
        loc = str(location or "").strip()
        if not loc:
            return log_tool_result(log, "weather_forecast", "Location is required for the weather forecast.")
        if provider.lower() != "openweathermap":
            return log_tool_result(log, "weather_forecast", f"Weather tool is configured for provider={provider}.")

        api_key = _get_api_key(config)
        if not api_key:
            return log_tool_result(log, "weather_forecast", "Weather API key is not configured.")
        geocode_key = _get_geocode_key(config)
        if "{api_key}" in geocode_url and not geocode_key:
            return log_tool_result(log, "weather_forecast", "Geocoding API key is not configured.")

        try:
            units_param, temp_unit, wind_unit = _units_meta(units)
            day_count = max(1, min(days or default_days, 8))
            headers = {"User-Agent": user_agent}

            with httpx.Client(timeout=10.0) as client:
                geo_result = geocode_location(
                    client,
                    loc,
                    geocode_url,
                    user_agent,
                    api_key=geocode_key,
                )
                if not geo_result:
                    return log_tool_result(log, "weather_forecast", f"No geocoding results for {loc}.")

                weather_url = _build_url(
                    one_call_url,
                    lat=geo_result.lat,
                    lon=geo_result.lon,
                    api_key=api_key,
                    units=units_param,
                )
                weather_response = client.get(weather_url, headers=headers)
                weather_response.raise_for_status()
                weather_data = weather_response.json()

            current = weather_data.get("current") or {}
            weather_list = current.get("weather") or []
            description = ""
            if weather_list and isinstance(weather_list, list):
                description = str(weather_list[0].get("description") or "").strip()
            description = description or "Conditions unavailable"

            temp = _round_number(current.get("temp"))
            feels = _round_number(current.get("feels_like"))
            humidity = _round_number(current.get("humidity"))
            wind_speed = _round_number(current.get("wind_speed"))

            timezone_offset = int(weather_data.get("timezone_offset") or 0)

            lines = [
                f"Weather for {geo_result.display_name}:",
                f"{description}, {temp}{temp_unit}, feels like {feels}{temp_unit}",
                f"Humidity: {humidity}%, Wind: {wind_speed} {wind_unit}",
            ]

            # Alerts — placed prominently before the forecast
            alerts = weather_data.get("alerts") or []
            if isinstance(alerts, list) and alerts:
                lines.extend(_format_alerts(alerts, timezone_offset))

            lines.append("")
            lines.append(f"{day_count}-day forecast:")

            daily = weather_data.get("daily") or []
            if not daily:
                lines.append("Daily forecast data not available.")
                return log_tool_result(log, "weather_forecast", "\n".join(lines).strip())

            # Next-hour precipitation from minutely data (if available)
            minutely = weather_data.get("minutely") or []
            if minutely and isinstance(minutely, list):
                next_hour_precip = sum(
                    float(m.get("precipitation") or 0)
                    for m in minutely[:60]
                    if isinstance(m, dict)
                )
                if next_hour_precip > 0.05:
                    lines.append(f"Next hour: {next_hour_precip:.1f} mm precipitation expected.")
                else:
                    lines.append("Next hour: No precipitation expected.")
                lines.append("")

            for idx, day in enumerate(daily[:day_count]):
                temp_info = day.get("temp") or {}
                high = _round_number(temp_info.get("max"))
                low = _round_number(temp_info.get("min"))
                pop = day.get("pop")
                try:
                    pop_pct = int(round(float(pop or 0) * 100))
                except (TypeError, ValueError):
                    pop_pct = 0
                day_weather = day.get("weather") or []
                summary = ""
                if day_weather and isinstance(day_weather, list):
                    summary = str(day_weather[0].get("description") or "").strip()
                summary = summary or "Forecast unavailable"
                label = _day_label(idx, int(day.get("dt") or 0), timezone_offset)
                # Include snow volume if present
                snow_mm = float(day.get("snow") or 0)
                rain_mm = float(day.get("rain") or 0)
                precip_detail = ""
                if snow_mm > 0:
                    precip_detail = f", snow {snow_mm:.0f} mm"
                elif rain_mm > 0:
                    precip_detail = f", rain {rain_mm:.0f} mm"
                lines.append(
                    f"{label}: {summary}, High {high}{temp_unit}, "
                    f"Low {low}{temp_unit}, {pop_pct}% chance of precipitation{precip_detail}"
                )

            return log_tool_result(log, "weather_forecast", "\n".join(lines).strip())
        except httpx.TimeoutException:
            log.warning("weather_forecast timed out for %s", loc)
            return log_tool_result(log, "weather_forecast", "Weather service timed out.")
        except httpx.HTTPError as exc:
            log.warning("weather_forecast HTTP error: %s", exc)
            return log_tool_result(log, "weather_forecast", "Weather lookup failed.")

    return ["weather_forecast"]
