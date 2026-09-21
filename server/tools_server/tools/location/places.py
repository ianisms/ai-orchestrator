import logging
import os
import re
from typing import Annotated, Any, List
from urllib.parse import quote

import httpx
from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _get_api_key(config: dict[str, Any]) -> str:
    return str(config.get("api_key") or os.getenv("GOOGLE_API_KEY") or "")


def _build_url(template: str, **params: object) -> str:
    url = template
    for key, value in params.items():
        url = url.replace("{" + key + "}", str(value))
    return url


def _extract_summary_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "overview", "description", "summary"):
            if key in value:
                candidate = _extract_summary_text(value.get(key))
                if candidate:
                    return candidate
        text = value.get("text")
        if isinstance(text, dict):
            inner = text.get("text")
            if isinstance(inner, str):
                return inner.strip()
        if isinstance(text, str):
            return text.strip()
    return ""


def _address_component_text(component: dict[str, Any]) -> str:
    if not isinstance(component, dict):
        return ""
    for key in ("shortText", "longText", "text"):
        value = component.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("text")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _pick_address_component(components: list[dict[str, Any]], wanted: tuple[str, ...]) -> str:
    wanted_set = set(wanted)
    for component in components:
        if not isinstance(component, dict):
            continue
        types = component.get("types")
        if not isinstance(types, list):
            continue
        if not any(isinstance(t, str) and t in wanted_set for t in types):
            continue
        text = _address_component_text(component)
        if text:
            return text
    return ""


def _street_and_town(place: dict[str, Any]) -> str:
    components = place.get("addressComponents")
    comp_list = components if isinstance(components, list) else []
    street_number = _pick_address_component(comp_list, ("street_number",))
    route = _pick_address_component(comp_list, ("route",))
    premise = _pick_address_component(comp_list, ("premise", "subpremise", "establishment"))
    town = _pick_address_component(
        comp_list,
        ("locality", "postal_town", "sublocality_level_1", "administrative_area_level_3", "administrative_area_level_2"),
    )

    street = " ".join(part for part in (street_number, route) if part).strip()
    if not street:
        street = route or premise

    formatted = str(place.get("formattedAddress") or "").strip()
    if formatted:
        parts = [p.strip() for p in formatted.split(",") if p and p.strip()]
        if not street and parts:
            street = parts[0]
        if not town and len(parts) > 1:
            town = parts[1]

    if street and town:
        return f"{street}, {town}"
    if street:
        return street
    if town:
        return town
    return formatted or "Address unavailable"


def _place_name_with_street_town(name: str, place: dict[str, Any]) -> str:
    components = place.get("addressComponents")
    comp_list = components if isinstance(components, list) else []
    street_number = _pick_address_component(comp_list, ("street_number",))
    route = _pick_address_component(comp_list, ("route",))
    premise = _pick_address_component(comp_list, ("premise", "subpremise", "establishment"))
    town = _pick_address_component(
        comp_list,
        ("locality", "postal_town", "sublocality_level_1", "administrative_area_level_3", "administrative_area_level_2"),
    )

    street = " ".join(part for part in (street_number, route) if part).strip()
    if not street:
        street = route or premise

    formatted = str(place.get("formattedAddress") or "").strip()
    if formatted:
        parts = [p.strip() for p in formatted.split(",") if p and p.strip()]
        if not street and parts:
            street = parts[0]
        if not town and len(parts) > 1:
            town = parts[1]

    safe_name = str(name or "Unknown place").strip() or "Unknown place"
    if street and town:
        return f"{safe_name} on {street} in {town}"
    if town:
        return f"{safe_name} in {town}"
    if street:
        return f"{safe_name} on {street}"
    return safe_name


_LOCATION_HINT_PATTERNS = (
    re.compile(r"\bnear me\b", re.IGNORECASE),
    re.compile(r"\bnearby\b", re.IGNORECASE),
    re.compile(r"\bclosest\b", re.IGNORECASE),
    re.compile(r"\bopen now\b", re.IGNORECASE),
    re.compile(r"\b(?:in|near|around|at|within)\s+[A-Za-z0-9]", re.IGNORECASE),
)


def _apply_default_location(query_text: str, default_location: str, enabled: bool) -> str:
    text = str(query_text or "").strip()
    if not text or not enabled:
        return text
    anchor = str(default_location or "").strip()
    if not anchor:
        return text
    if any(p.search(text) for p in _LOCATION_HINT_PATTERNS):
        return text
    if "," in text:
        return text
    return f"{text} near {anchor}"


_PRICE_LABELS = {
    "PRICE_LEVEL_FREE": "Free",
    "PRICE_LEVEL_INEXPENSIVE": "Inexpensive",
    "PRICE_LEVEL_MODERATE": "Moderate",
    "PRICE_LEVEL_EXPENSIVE": "Expensive",
    "PRICE_LEVEL_VERY_EXPENSIVE": "Very expensive",
}


def _format_place_details(data: dict[str, Any]) -> str:
    lines: list[str] = []
    display = data.get("displayName") or {}
    name = _extract_summary_text(display) or "Unknown place"
    primary_type = _extract_summary_text(data.get("primaryTypeDisplayName") or {})
    if primary_type:
        lines.append(f"{name} ({primary_type})")
    else:
        lines.append(name)

    address = str(data.get("formattedAddress") or "").strip()
    if address:
        lines.append(f"Address: {address}")

    rating = data.get("rating")
    rating_count = data.get("userRatingCount")
    if isinstance(rating, (int, float)):
        rating_text = f"Rating: {float(rating):.1f} stars"
        if isinstance(rating_count, int):
            rating_text += f" ({rating_count} reviews)"
        lines.append(rating_text)

    price_level = str(data.get("priceLevel") or "").strip()
    price_range = data.get("priceRange")
    if price_level and price_level in _PRICE_LABELS:
        lines.append(f"Price: {_PRICE_LABELS[price_level]}")
    elif isinstance(price_range, dict):
        low = price_range.get("startPrice", {})
        high = price_range.get("endPrice", {})
        low_val = low.get("units") if isinstance(low, dict) else None
        high_val = high.get("units") if isinstance(high, dict) else None
        if low_val and high_val:
            lines.append(f"Price range: {low_val} to {high_val}")

    phone = str(data.get("nationalPhoneNumber") or "").strip()
    if phone:
        lines.append(f"Phone: {phone}")

    website = str(data.get("websiteUri") or "").strip()
    if website:
        lines.append(f"Website: {website}")

    # Hours
    hours = data.get("regularOpeningHours") or {}
    weekday_descs = hours.get("weekdayDescriptions")
    open_now = data.get("currentOpeningHours", {}).get("openNow") if data.get("currentOpeningHours") else hours.get("openNow")
    if isinstance(weekday_descs, list) and weekday_descs:
        lines.append("")
        lines.append("Hours:")
        for day_str in weekday_descs:
            if isinstance(day_str, str):
                lines.append(f"  {day_str}")
        if isinstance(open_now, bool):
            lines.append(f"Currently: {'Open' if open_now else 'Closed'}")

    # Services
    services = []
    if data.get("dineIn"):
        services.append("dine-in")
    if data.get("takeout"):
        services.append("takeout")
    if data.get("delivery"):
        services.append("delivery")
    if data.get("curbsidePickup"):
        services.append("curbside pickup")
    if data.get("reservable"):
        services.append("reservations accepted")
    if services:
        lines.append("")
        lines.append(f"Services: {', '.join(services)}")

    # Food / cuisine
    food_flags = []
    if data.get("servesBreakfast"):
        food_flags.append("breakfast")
    if data.get("servesLunch"):
        food_flags.append("lunch")
    if data.get("servesDinner"):
        food_flags.append("dinner")
    if data.get("servesBeer"):
        food_flags.append("beer")
    if data.get("servesWine"):
        food_flags.append("wine")
    if data.get("servesVegetarianFood"):
        food_flags.append("vegetarian options")
    if food_flags:
        lines.append(f"Serves: {', '.join(food_flags)}")

    # Atmosphere
    atmosphere = []
    if data.get("goodForGroups"):
        atmosphere.append("good for groups")
    if data.get("goodForChildren"):
        atmosphere.append("good for children")
    if data.get("outdoorSeating"):
        atmosphere.append("outdoor seating")
    if data.get("liveMusic"):
        atmosphere.append("live music")
    if atmosphere:
        lines.append(f"Features: {', '.join(atmosphere)}")

    # Description / summaries
    editorial = _extract_summary_text(data.get("editorialSummary"))
    generative = _extract_summary_text(data.get("generativeSummary"))
    review_summary = _extract_summary_text(data.get("reviewSummary"))

    description = editorial or generative
    if description:
        lines.append("")
        lines.append(f"Description: {description}")

    if review_summary:
        lines.append("")
        lines.append(f"Review summary: {review_summary}")

    # Individual reviews
    reviews = data.get("reviews")
    if isinstance(reviews, list) and reviews:
        lines.append("")
        lines.append("Reviews:")
        for review in reviews:
            if not isinstance(review, dict):
                continue
            r_rating = review.get("rating")
            r_time = str(review.get("relativePublishTimeDescription") or "").strip()
            r_author = ""
            author_attr = review.get("authorAttribution")
            if isinstance(author_attr, dict):
                r_author = str(author_attr.get("displayName") or "").strip()
            r_text_obj = review.get("originalText") or review.get("text")
            r_text = _extract_summary_text(r_text_obj) if r_text_obj else ""
            if not r_text:
                continue
            header_parts = []
            if isinstance(r_rating, (int, float)):
                header_parts.append(f"{int(r_rating)} stars")
            if r_author:
                header_parts.append(r_author)
            if r_time:
                header_parts.append(r_time)
            header = " - ".join(header_parts) if header_parts else "Review"
            lines.append(f"  [{header}] {r_text}")

    if len(lines) <= 1:
        return "No details available for that place."

    return "\n".join(lines)


def register(server) -> List[str]:
    log = logging.getLogger("tools.places")
    config = _load_config()
    provider = str(config.get("provider") or "stub")
    default_limit = int(config.get("limit") or 5)
    max_api_results = int(config.get("max_api_results") or 20)
    text_search_url = str(config.get("text_search_url") or "https://places.googleapis.com/v1/places:searchText")
    base_url = str(config.get("places_base_url") or "https://places.googleapis.com/v1/places")
    place_summary_url = str(config.get("place_summary_url") or f"{base_url}/{{place_id}}")
    user_agent = str(config.get("user_agent") or "home-ai-tools/1.0")
    default_location = str(
        config.get("default_location")
        or os.getenv("HOME_DEFAULT_LOCATION")
        or os.getenv("HOMEASSISTANT_DEFAULT_LOCATION")
        or ""
    )
    append_default_location = bool(config.get("append_default_location_when_missing", True))

    location_desc, location_params = get_tool_config(
        config,
        "places",
        (
            "Find places using Google Places text search. Provide a freeform query "
            "like 'Spicy vegetarian food in Sydney, Australia'. Results are enhanced "
            "with AI summaries and reviews."
        ),
    )
    query_desc, query_alias, query_title = get_param_meta(
        location_params,
        "query",
        "Freeform text query, for example 'Spicy vegetarian food in Sydney, Australia'.",
    )
    max_desc, max_alias, max_title = get_param_meta(
        location_params,
        "max_results",
        "Maximum number of results.",
    )
    sort_desc, sort_alias, sort_title = get_param_meta(
        location_params,
        "sort_order",
        "Sort by relevance (default) or rating.",
    )

    place_desc, place_params = get_tool_config(
        config,
        "place_summary",
        (
            "Gets an AI summary for a Google Place by place ID. "
            "Use this after places when you need more details."
        ),
    )
    place_id_desc, place_id_alias, place_id_title = get_param_meta(
        place_params,
        "place_id",
        "Place ID returned by places.",
    )
    details_desc, details_params = get_tool_config(
        config,
        "place_details",
        (
            "Get full details for a specific place by Google Place ID. "
            "Returns hours, reviews, description, cuisines, services, and a comprehensive summary. "
            "Use this after local_places_search when the user wants to know more about a specific place."
        ),
    )
    details_pid_desc, details_pid_alias, details_pid_title = get_param_meta(
        details_params,
        "place_id",
        "Google Place ID returned by local_places_search.",
    )
    local_desc, local_params = get_tool_config(
        config,
        "local_places_search",
        location_desc,
    )
    local_query_desc, local_query_alias, local_query_title = get_param_meta(
        local_params,
        "query",
        query_desc,
    )
    local_max_desc, local_max_alias, local_max_title = get_param_meta(
        local_params,
        "max_results",
        max_desc,
    )
    local_sort_desc, local_sort_alias, local_sort_title = get_param_meta(
        local_params,
        "sort_order",
        sort_desc,
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    def _run_places(
        tool_name: str,
        query: str,
        max_results: int,
        sort_order: str,
    ) -> str:
        log_tool_call(log, tool_name, query=query, max_results=max_results, sort_order=sort_order)
        query_text = str(query or "").strip()
        if not query_text:
            return log_tool_result(
                log,
                tool_name,
                "Query is required, e.g. 'Spicy vegetarian food in Sydney, Australia'.",
            )
        query_text = _apply_default_location(query_text, default_location, append_default_location)
        if provider.lower() not in {"google_places", "google"}:
            return log_tool_result(log, tool_name, f"Places tool is configured for provider={provider}.")

        api_key = _get_api_key(config)
        if not api_key:
            return log_tool_result(log, tool_name, "Location API key is not configured.")

        max_api = max(1, min(max_api_results, 20))
        cap = max(1, min(int(max_results or default_limit), max_api))
        api_limit = max(cap, max_api)
        sort_value = str(sort_order or "").strip().lower()
        if sort_value not in {"rating", "relevance"}:
            sort_value = "relevance"
        search_headers = {
            "User-Agent": user_agent,
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": (
                "places.id,"
                "places.displayName,"
                "places.formattedAddress,"
                "places.addressComponents,"
                "places.location,"
                "places.rating,"
                "places.userRatingCount,"
                "places.types"
            ),
        }
        summary_headers = {
            "User-Agent": user_agent,
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "generativeSummary,reviewSummary",
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                payload: dict[str, object] = {
                    "textQuery": query_text,
                    "maxResultCount": api_limit,
                }
                search_url = _build_url(
                    text_search_url,
                    api_key=api_key,
                    query=query_text,
                    text_query=query_text,
                    max_results=api_limit,
                    limit=cap,
                    sort_order=sort_value,
                )
                response = client.post(search_url, headers=search_headers, json=payload)
                response.raise_for_status()
                data = response.json() or {}
                places = data.get("places") or []
                if not places:
                    return log_tool_result(log, tool_name, "No places found.")

                if sort_value == "rating":
                    places = list(places)
                    places.sort(
                        key=lambda p: (
                            float((p or {}).get("rating") or 0),
                            int((p or {}).get("userRatingCount") or 0),
                        ),
                        reverse=True,
                    )

                lines = [f"Found {min(cap, len(places))} places", ""]
                for idx, place in enumerate(places[:cap]):
                    display = place.get("displayName") or {}
                    name = display.get("text") or "Unknown place"
                    address = _street_and_town(place)
                    phrased_name = _place_name_with_street_town(name, place)
                    rating = place.get("rating")
                    rating_count = place.get("userRatingCount")
                    place_id = place.get("id") or ""
                    summary_text = ""
                    reviews_summary = ""
                    if place_id:
                        summary_url = _build_url(
                            place_summary_url,
                            place_id=quote(str(place_id), safe=""),
                            api_key=api_key,
                        )
                        try:
                            summary_response = client.get(
                                summary_url,
                                headers=summary_headers,
                                timeout=10.0,
                            )
                            summary_response.raise_for_status()
                            summary_data = summary_response.json() or {}
                            summary_text = _extract_summary_text(summary_data.get("generativeSummary"))
                            reviews_summary = _extract_summary_text(summary_data.get("reviewSummary"))
                        except (httpx.HTTPError, ValueError):
                            summary_text = ""
                            reviews_summary = ""

                    if not summary_text:
                        summary_text = "Summary unavailable."
                    if not reviews_summary:
                        reviews_summary = "Review summary unavailable."
                    place_line = f"{idx + 1}. {phrased_name}"
                    if isinstance(rating, (int, float)):
                        stars_text = f"{float(rating):.1f} stars"
                        if isinstance(rating_count, int):
                            stars_text += f" with {rating_count} reviews"
                        place_line = f"{place_line} - {stars_text}"
                    lines.append(place_line)
                    lines.append(f"Address: {address}")
                    if place_id:
                        lines.append(f"Place ID: {place_id}")
                    lines.append(f"Summary: {summary_text}")
                    lines.append(f"Review Summary: {reviews_summary}")
                    lines.append("")

                return log_tool_result(log, tool_name, "\n".join(lines))
        except httpx.TimeoutException:
            log.warning("%s timed out", tool_name)
            return log_tool_result(log, tool_name, "Location service timed out.")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s error: %s", tool_name, exc)
            return log_tool_result(log, tool_name, "Location lookup failed.")

    @server.tool(
        name="local_places_search",
        description=local_desc,
    )
    def local_places_search(
        query: Annotated[str, Field(**_field_kwargs(local_query_desc, local_query_alias, local_query_title))],
        max_results: Annotated[int, Field(**_field_kwargs(local_max_desc, local_max_alias, local_max_title))] = 5,
        sort_order: Annotated[str, Field(**_field_kwargs(local_sort_desc, local_sort_alias, local_sort_title))] = "rating",
    ) -> str:
        return _run_places("local_places_search", query, max_results, sort_order)

    @server.tool(
        name="place_summary",
        description=place_desc,
    )
    def place_summary(
        place_id: Annotated[str, Field(**_field_kwargs(place_id_desc, place_id_alias, place_id_title))]
    ) -> str:
        log_tool_call(log, "place_summary", place_id=place_id)
        pid = str(place_id or "").strip()
        if not pid:
            return log_tool_result(log, "place_summary", "Place summary requires a place ID.")
        if provider.lower() not in {"google_places", "google"}:
            return log_tool_result(log, "place_summary", f"Places tool is configured for provider={provider}.")

        api_key = _get_api_key(config)
        if not api_key:
            return log_tool_result(log, "place_summary", "Location API key is not configured.")

        url = _build_url(place_summary_url, place_id=quote(pid, safe=""))
        headers = {
            "User-Agent": user_agent,
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "generativeSummary,reviewSummary",
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json() or {}
        except httpx.TimeoutException:
            log.warning("place_summary timed out")
            return log_tool_result(log, "place_summary", "Place summary service timed out.")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("place_summary error: %s", exc)
            return log_tool_result(log, "place_summary", "Place summary lookup failed.")

        summary = _extract_summary_text(data.get("generativeSummary"))
        reviews_summary = _extract_summary_text(data.get("reviewSummary"))
        if not summary and not reviews_summary:
            return log_tool_result(log, "place_summary", "No summary available for that place.")
        if summary and reviews_summary:
            return log_tool_result(log, "place_summary", f"{summary} {reviews_summary}".strip())
        return log_tool_result(log, "place_summary", summary or reviews_summary)

    @server.tool(
        name="place_details",
        description=details_desc,
    )
    def place_details(
        place_id: Annotated[str, Field(**_field_kwargs(details_pid_desc, details_pid_alias, details_pid_title))]
    ) -> str:
        log_tool_call(log, "place_details", place_id=place_id)
        pid = str(place_id or "").strip()
        if not pid:
            return log_tool_result(log, "place_details", "Place details requires a place ID.")
        if provider.lower() not in {"google_places", "google"}:
            return log_tool_result(log, "place_details", f"Places tool is configured for provider={provider}.")

        api_key = _get_api_key(config)
        if not api_key:
            return log_tool_result(log, "place_details", "Location API key is not configured.")

        url = _build_url(place_summary_url, place_id=quote(pid, safe=""))
        headers = {
            "User-Agent": user_agent,
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": (
                "displayName,formattedAddress,rating,userRatingCount,"
                "priceLevel,priceRange,"
                "regularOpeningHours,currentOpeningHours,"
                "reviews,generativeSummary,reviewSummary,editorialSummary,"
                "primaryType,primaryTypeDisplayName,"
                "websiteUri,nationalPhoneNumber,"
                "servesBreakfast,servesLunch,servesDinner,"
                "servesBeer,servesWine,servesVegetarianFood,"
                "dineIn,takeout,delivery,curbsidePickup,"
                "reservable,goodForGroups,goodForChildren,"
                "liveMusic,outdoorSeating"
            ),
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json() or {}
        except httpx.TimeoutException:
            log.warning("place_details timed out for %s", pid)
            return log_tool_result(log, "place_details", "Place details service timed out.")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("place_details error for %s: %s", pid, exc)
            return log_tool_result(log, "place_details", "Place details lookup failed.")

        return log_tool_result(log, "place_details", _format_place_details(data))

    return ["local_places_search", "place_summary", "place_details"]
