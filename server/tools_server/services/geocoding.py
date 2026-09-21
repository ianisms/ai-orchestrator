from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import quote_plus

import httpx


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    display_name: str


_LOG = logging.getLogger("tools.geocoding")


def geocode_location(
    client: httpx.Client,
    location: str,
    geocode_url: str,
    user_agent: str,
    api_key: str | None = None,
) -> GeocodeResult | None:
    if not location:
        return None
    url = geocode_url.replace("{location}", quote_plus(location))
    if "{api_key}" in url:
        if not api_key:
            return None
        url = url.replace("{api_key}", quote_plus(api_key))
    headers = {"User-Agent": user_agent}
    _LOG.info("geocoding request url=%s user_agent=%s", url, user_agent)
    response = client.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and "results" in data:
        results = data.get("results") or []
        if not results or not isinstance(results, list):
            return None
        entry = results[0] if isinstance(results[0], dict) else {}
        geometry = entry.get("geometry") if isinstance(entry.get("geometry"), dict) else {}
        loc = geometry.get("location") if isinstance(geometry.get("location"), dict) else {}
        lat = loc.get("lat")
        lon = loc.get("lng")
        display_name = entry.get("formatted_address") or entry.get("name") or location
    elif isinstance(data, list) and data:
        entry = data[0] if isinstance(data[0], dict) else {}
        lat = entry.get("lat")
        lon = entry.get("lon")
        display_name = entry.get("display_name") or entry.get("name") or location
    else:
        return None

    if lat is None or lon is None:
        return None
    try:
        return GeocodeResult(float(lat), float(lon), str(display_name))
    except (TypeError, ValueError):
        return None
