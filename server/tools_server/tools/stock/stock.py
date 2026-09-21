import logging
import os
from datetime import datetime, time as dt_time, timezone
import re
from typing import Annotated, Any, List
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

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


def _get_api_key(config: dict[str, Any]) -> str:
    return str(config.get("api_key") or os.getenv("FINNHUB_API_KEY") or "")


def _format_money(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _market_status_text(last_trade_epoch: object, market_status: dict[str, Any] | None = None) -> str:
    try:
        trade_epoch = int(float(last_trade_epoch))
    except (TypeError, ValueError):
        trade_epoch = 0

    status = market_status or {}
    is_open = status.get("isOpen")
    holiday = str(status.get("holiday") or "").strip()
    session = str(status.get("session") or "").strip()
    tz_label = str(status.get("timezone") or "").strip() or "America/New_York"

    if isinstance(is_open, bool):
        base = "Market session: Open." if is_open else "Market session: Closed."
        details: list[str] = []
        if session:
            details.append(f"Session: {session}.")
        if holiday:
            details.append(f"Holiday: {holiday}.")
        elif is_open is False:
            details.append("Likely outside regular trading hours.")

        trade_text = ""
        if trade_epoch > 0:
            try:
                trade_tz = ZoneInfo(tz_label)
            except Exception:
                trade_tz = ZoneInfo("America/New_York")
            trade_at = datetime.fromtimestamp(trade_epoch, tz=timezone.utc).astimezone(trade_tz)
            trade_text = f" Last trade: {trade_at.strftime('%Y-%m-%d %H:%M:%S %Z')}."
        return " ".join([base] + details).strip() + trade_text

    if trade_epoch <= 0:
        return "Market session: Unknown."

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    trade_et = datetime.fromtimestamp(trade_epoch, tz=timezone.utc).astimezone(et)
    is_weekday = now_et.weekday() < 5
    regular_open = dt_time(9, 30)
    regular_close = dt_time(16, 0)
    in_regular_session = is_weekday and regular_open <= now_et.time() <= regular_close

    session = "Regular market open" if in_regular_session else "Regular market closed"
    return f"Market session: {session}. Last trade: {trade_et.strftime('%Y-%m-%d %H:%M:%S %Z')}."


def register(server) -> List[str]:
    log = logging.getLogger("tools.stock")
    config = _load_config()
    provider = str(config.get("provider") or "stub")
    default_exchange = str(config.get("exchange") or "US")
    search_url = str(
        config.get("search_url")
        or "https://finnhub.io/api/v1/search?q={query}&exchange={exchange}&token={api_key}"
    )
    quote_url = str(
        config.get("quote_url")
        or "https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}"
    )
    market_status_url = str(
        config.get("market_status_url")
        or "https://finnhub.io/api/v1/stock/market-status?exchange={exchange}&token={api_key}"
    )
    user_agent = str(config.get("user_agent") or "home-ai-tools/1.0")
    # Only treat input as a raw ticker if it's uppercase letters/dots, 1-5 chars
    # (e.g., AAPL, MSFT, BRK.B).  Everything else goes through search first.
    _ticker_re = re.compile(r"^[A-Z][A-Z0-9.]{0,4}$")

    tool_description, param_meta = get_tool_config(
        config,
        "stock_quote",
        "Returns the latest stock price for a symbol. Use this when the user asks about a stock price or quote.",
    )
    query_desc, query_alias, query_title = get_param_meta(
        param_meta,
        "query",
        "Company name or stock symbol, for example 'Apple' or 'AAPL'.",
    )
    exchange_desc, exchange_alias, exchange_title = get_param_meta(
        param_meta,
        "exchange",
        "Optional exchange code, defaults to the configured exchange (e.g., 'US').",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="stock_quote",
        description=tool_description,
    )
    def stock_quote(
        query: Annotated[str, Field(**_field_kwargs(query_desc, query_alias, query_title))],
        exchange: Annotated[
            str | None,
            Field(**_field_kwargs(exchange_desc, exchange_alias, exchange_title)),
        ] = None,
    ) -> str:
        log_tool_call(log, "stock_quote", query=query, exchange=exchange)
        user_query = str(query or "").strip()
        if not user_query:
            return log_tool_result(log, "stock_quote", "Stock quote requires a company name or symbol.")
        if provider.lower() != "finnhub":
            return log_tool_result(log, "stock_quote", f"Stock tool is configured for provider={provider}.")

        api_key = _get_api_key(config)
        if not api_key:
            return log_tool_result(log, "stock_quote", "Stock API key is not configured.")

        exch = str(exchange or "").strip().upper() or default_exchange or "US"
        headers = {"User-Agent": user_agent}

        try:
            raw_upper = user_query.strip().upper()
            symbol = None

            # If it looks like a raw ticker (e.g. AAPL, MSFT, BRK.B), use directly.
            if _ticker_re.match(raw_upper):
                symbol = raw_upper
            else:
                # Search by company name / partial symbol.
                search_request = _build_url(
                    search_url,
                    query=quote_plus(user_query),
                    exchange=exch,
                    api_key=api_key,
                )
                with httpx.Client(timeout=10.0) as client:
                    search_response = client.get(search_request, headers=headers)
                    search_response.raise_for_status()
                    search_data = search_response.json() or {}

                results = search_data.get("result") or []
                if not results:
                    return log_tool_result(
                        log, "stock_quote", f"No companies found matching '{user_query}'."
                    )

                # Auto-pick when there's a single result or the top result is a clear match.
                first = results[0] or {}
                first_sym = first.get("symbol") or first.get("displaySymbol") or ""
                first_desc = str(first.get("description") or "").lower()
                query_lower = user_query.lower()

                if (
                    len(results) == 1
                    or query_lower in first_desc
                    or first_desc.startswith(query_lower)
                ):
                    symbol = first_sym
                else:
                    lines = [f"Multiple companies found matching '{user_query}':"]
                    for idx, item in enumerate(results[:5]):
                        sym = item.get("symbol") or item.get("displaySymbol") or "N/A"
                        desc = item.get("description") or "Unknown"
                        lines.append(f"{idx + 1}. {sym} - {desc}")
                    return log_tool_result(log, "stock_quote", "\n".join(lines))

                if not symbol:
                    return log_tool_result(
                        log, "stock_quote", f"No companies found matching '{user_query}'."
                    )

            quote_request = _build_url(
                quote_url,
                symbol=symbol,
                api_key=api_key,
            )
            market_status_request = _build_url(
                market_status_url,
                exchange=quote_plus(exch),
                api_key=api_key,
            )
            with httpx.Client(timeout=10.0) as client:
                quote_response = client.get(quote_request, headers=headers)
                quote_response.raise_for_status()
                quote_data = quote_response.json() or {}
                try:
                    market_status_response = client.get(market_status_request, headers=headers)
                    market_status_response.raise_for_status()
                    market_status_data = market_status_response.json() or {}
                    if not isinstance(market_status_data, dict):
                        market_status_data = {}
                except httpx.HTTPError:
                    market_status_data = {}

            current_price = quote_data.get("c")
            prev_close = quote_data.get("pc")
            low = quote_data.get("l")
            high = quote_data.get("h")
            trade_time = quote_data.get("t")

            if current_price in (None, 0) and prev_close in (None, 0):
                return log_tool_result(log, "stock_quote", f"Stock quote unavailable for {symbol}.")

            change_percent = None
            if isinstance(current_price, (int, float)) and isinstance(prev_close, (int, float)) and prev_close:
                change_percent = (current_price - prev_close) / prev_close * 100

            change_label = ""
            if change_percent is not None:
                direction = "up" if change_percent >= 0 else "down"
                change_label = f" ({direction} {abs(change_percent):.2f}%)"

            current_out = _format_money(current_price)
            prev_out = _format_money(prev_close)
            low_out = _format_money(low)
            high_out = _format_money(high)

            response = log_tool_result(
                log,
                "stock_quote",
                (
                f"Current price (latest trade): ${current_out}{change_label}\n"
                f"Today's range: ${low_out} to ${high_out}\n"
                f"Previous close (reference): ${prev_out}\n"
                f"{_market_status_text(trade_time, market_status_data)}"
                ),
            )
            log.info(f"stock_quote response: {response}")
            return response
        except httpx.TimeoutException:
            log.warning("stock_quote timed out for %s", symbol)
            return log_tool_result(log, "stock_quote", "Stock service timed out.")
        except httpx.HTTPError as exc:
            log.warning("stock_quote HTTP error: %s", exc)
            return log_tool_result(log, "stock_quote", "Stock lookup failed.")

    return ["stock_quote"]
