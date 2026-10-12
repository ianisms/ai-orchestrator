import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any, List
from urllib.parse import urlencode

import httpx
from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_BASE_URL = "https://newsdata.io/api/1/latest"

# Default categories for general "latest news" requests
_DEFAULT_CATEGORIES = "breaking,business,technology,top"

# Static params that are always sent
_STATIC_PARAMS = {
    "timezone": "america/new_york",
    "prioritydomain": "top",
    "image": "0",
    "video": "0",
    "removeduplicate": "1",
    "excludefield": "source_icon,source_url,image_url,video_url,link",
}


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _get_api_key(config: dict[str, Any]) -> str:
    return str(config.get("api_key") or os.getenv("NEWSDATA_API_KEY") or "")


def _strip_source_suffix(title: str, source_name: str) -> str:
    if not source_name:
        return title
    pattern = r"\s*[\W_]*\s*" + re.escape(source_name) + r"\s*$"
    return re.sub(pattern, "", title, flags=re.IGNORECASE).strip()


def _relative_time(pub_date: str) -> str:
    """Convert a pubDate string to a relative time like 'published 2 hours ago'."""
    if not pub_date:
        return ""
    try:
        dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return ""
        if seconds < 3600:
            mins = max(1, seconds // 60)
            return f"published {mins} minute{'s' if mins != 1 else ''} ago"
        if seconds < 86400:
            hours = seconds // 3600
            return f"published {hours} hour{'s' if hours != 1 else ''} ago"
        days = seconds // 86400
        if days == 1:
            return "published yesterday"
        if days < 7:
            return f"published {days} days ago"
        return f"published {dt.strftime('%b %d')}"
    except (ValueError, TypeError):
        return ""


def register(server) -> List[str]:
    log = logging.getLogger("tools.news")
    config = _load_config()
    provider = str(config.get("provider") or "stub")
    language_default = str(config.get("language") or "en")
    country_default = str(config.get("country") or "us")
    default_categories = str(config.get("default_categories") or _DEFAULT_CATEGORIES)
    user_agent = str(config.get("user_agent") or "home-ai-tools/1.0")

    raw_blocked = config.get("blocked_sources") or []
    blocked_sources = frozenset(
        str(s).strip().lower() for s in raw_blocked if isinstance(s, str) and s.strip()
    )

    # Extra categories that pass the post-filter even though they aren't in the API request
    raw_allowed = config.get("allowed_categories") or []
    extra_allowed_cats = frozenset(
        str(c).strip().lower() for c in raw_allowed if isinstance(c, str) and c.strip()
    )

    tool_description, param_meta = get_tool_config(
        config,
        "news_search",
        (
            "Returns recent news headlines. Defaults to latest headlines. "
            "Use this when the user asks for news about a topic, company, or event."
        ),
    )
    query_desc, query_alias, query_title = get_param_meta(
        param_meta,
        "query",
        "Optional topic or keywords. Leave empty for latest headlines.",
    )
    limit_desc, limit_alias, limit_title = get_param_meta(
        param_meta,
        "limit",
        "Number of headlines to return. Min 5, max 10. Default is 10.",
    )
    category_desc, category_alias, category_title = get_param_meta(
        param_meta,
        "category",
        (
            "Optional category filter. Valid values: breaking, business, crime, "
            "domestic, education, entertainment, environment, food, health, "
            "lifestyle, politics, science, sports, technology, top, tourism, world."
        ),
    )
    country_desc, country_alias, country_title = get_param_meta(
        param_meta,
        "country",
        "Optional 2-letter country code, for example 'us' or 'gb'. Pass 'global' for worldwide headlines.",
    )
    language_desc, language_alias, language_title = get_param_meta(
        param_meta,
        "language",
        "Optional 2-letter language code, for example 'en' or 'es'.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="news_search",
        description=tool_description,
    )
    def news_search(
        query: Annotated[str | None, Field(**_field_kwargs(query_desc, query_alias, query_title))] = None,
        limit: Annotated[int, Field(**_field_kwargs(limit_desc, limit_alias, limit_title))] = 10,
        category: Annotated[str | None, Field(**_field_kwargs(category_desc, category_alias, category_title))] = None,
        country: Annotated[str | None, Field(**_field_kwargs(country_desc, country_alias, country_title))] = None,
        language: Annotated[str | None, Field(**_field_kwargs(language_desc, language_alias, language_title))] = None,
    ) -> str:
        log_tool_call(
            log,
            "news_search",
            query=query,
            limit=limit,
            category=category,
            country=country,
            language=language,
        )

        q = str(query or "").strip()
        if provider.lower() != "newsdata":
            return log_tool_result(log, "news_search", f"News tool is configured for provider={provider}.")

        api_key = _get_api_key(config)
        if not api_key:
            return log_tool_result(log, "news_search", "News API key is not configured.")

        cap = max(5, min(int(limit or 10), 10))
        lang_value = str(language or "").strip() or language_default
        country_value = str(country or "").strip() or country_default
        category_value = str(category or "").strip() or default_categories

        # Build URL
        params: dict[str, str] = {
            "apikey": api_key,
            "language": lang_value,
            "category": category_value,
            **_STATIC_PARAMS,
        }
        if country_value and country_value.lower() != "global":
            params["country"] = country_value
        if q:
            params["q"] = q

        news_url = f"{_BASE_URL}?{urlencode(params)}"
        log.debug("news_search URL: %s", news_url.replace(api_key, "***"))

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(news_url, headers={"User-Agent": user_agent})
                response.raise_for_status()
                data = response.json() or {}
        except httpx.TimeoutException:
            log.warning("news_search timed out")
            return log_tool_result(log, "news_search", "News service timed out.")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("news_search error: %s", exc)
            return log_tool_result(log, "news_search", "News lookup failed.")

        if data.get("status") != "success":
            message = str(data.get("results", {}).get("message", "")) if isinstance(data.get("results"), dict) else ""
            return log_tool_result(log, "news_search", f"News lookup failed. {message}".strip())

        articles = data.get("results") or []
        if not isinstance(articles, list):
            return log_tool_result(log, "news_search", "No headlines found.")

        # Filter blocked sources
        if blocked_sources:
            articles = [
                a for a in articles
                if str(a.get("source_name") or "").strip().lower() not in blocked_sources
            ]

        # Filter articles with categories outside the allowed set.
        # NewsData.io returns multi-category articles (e.g. ["sports", "breaking"]);
        # drop any article that has a category not in the allowed set.
        # The allowed set = requested API categories + extra allowed categories from config.
        allowed_cats = frozenset(c.strip().lower() for c in category_value.split(",") if c.strip()) | extra_allowed_cats
        if allowed_cats:
            articles = [
                a for a in articles
                if all(
                    str(c).strip().lower() in allowed_cats
                    for c in (a.get("category") or [])
                    if isinstance(c, str) and c.strip()
                )
            ]

        if not articles:
            return log_tool_result(log, "news_search", "No headlines found.")

        # Sort: pubDate descending (newest first), then breaking articles bubble to top
        articles.sort(key=lambda a: str(a.get("pubDate") or ""), reverse=True)
        articles.sort(key=lambda a: 0 if "breaking" in [
            str(c).strip().lower() for c in (a.get("category") or []) if isinstance(c, str)
        ] else 1)

        headline_label = f"News for {q}:" if q else "Latest headlines:"
        lines = [headline_label]
        for article in articles[:cap]:
            title = str(article.get("title") or "Untitled").strip()
            source = str(article.get("source_name") or "").strip()
            description = str(article.get("description") or "").strip()
            pub_date = str(article.get("pubDate") or "").strip()
            title = _strip_source_suffix(title, source)
            rel_time = _relative_time(pub_date)
            time_part = f", {rel_time}" if rel_time else ""
            if source and description:
                lines.append(f"- From {source}{time_part}: {title}: {description}")
            elif source:
                lines.append(f"- From {source}{time_part}: {title}")
            elif description:
                lines.append(f"- {title}: {description}")
            else:
                lines.append(f"- {title}")
        return log_tool_result(log, "news_search", "\n".join(lines))

    return ["news_search"]
