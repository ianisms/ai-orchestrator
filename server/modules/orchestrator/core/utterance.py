from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

_PARSE_LOG = logging.getLogger("orchestrator.tool_parse")

from common.logging import format_kv
from config.settings import settings
from llm.llm_client import stream_chat_completions
from llm.stream import stream_llm
from tts.stream import stream_tts

from .audio_tap import AudioTapQueue, CLIENT_AUDIO_DIR, cleanup_stale_audio
from .history import ChatHistory
from .metrics import (
    inc_turn_watchdog_timeouts,
    inc_turn_outcome,
    observe_ingest_seconds,
    observe_llm_seconds,
    observe_tool_call_seconds,
    observe_tts_seconds,
    observe_turn_seconds,
    record_tool_output,
    record_turn,
    reset_all as reset_turn_stats_all,
    reset_uptime as reset_turn_stats,
)
from tools.registry import list_tools, parse_tool_args
from orchestrator.tools.mcp_client import get_tools_client
from .turn import CancelToken, new_turn_id
from .types import SendEvent

_LOCAL_SEARCH_TOOL_NAMES = ("local_places_search",)
_LOCAL_PLACE_PHRASE_RE = re.compile(r"\bon\s+[^.!,;\n]+\s+in\s+[^.!,;\n]+", re.IGNORECASE)
_LOCAL_SEARCH_PATTERNS = (
    re.compile(r"\bnear me\b", re.IGNORECASE),
    re.compile(r"\bnearby\b", re.IGNORECASE),
    re.compile(r"\bclosest\b", re.IGNORECASE),
    re.compile(r"\bopen now\b", re.IGNORECASE),
    re.compile(r"\bbest\s+.+\s+in\s+[A-Za-z]", re.IGNORECASE),
    re.compile(
        r"\bbest\s+(?:italian|mexican|chinese|japanese|thai|indian|pizza|sushi|burger|bbq|barbecue|ramen|tacos?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:restaurant|restaurants|food|eat|dinner|lunch|breakfast|brunch)\b", re.IGNORECASE),
    re.compile(r"\b(?:restaurant|coffee|cafe|pharmacy|gas station|grocery|park|bar|hotel)\b", re.IGNORECASE),
)
_EXPLICIT_LOCATION_PATTERNS = (
    re.compile(r"\b(?:in|near|around|at|within)\s+[A-Za-z0-9]", re.IGNORECASE),
    re.compile(r"\b\d{5}(?:-\d{4})?\b"),
)
_WEATHER_QUERY_PATTERNS = (
    re.compile(r"\bweather\b", re.IGNORECASE),
    re.compile(r"\bforecast\b", re.IGNORECASE),
    re.compile(r"\btemperature\b", re.IGNORECASE),
    re.compile(r"\brain\b", re.IGNORECASE),
    re.compile(r"\bsnow\b", re.IGNORECASE),
)
_LOCATION_PREF_TOOL_NAME = "location_preferences"
_NON_LOCATION_INTENT_PATTERNS = (
    re.compile(r"\b(weather|forecast|time|stock|quote|news|timer|alarm)\b", re.IGNORECASE),
    re.compile(r"\b(best|find|search|restaurant|pizza|coffee|cafe|hotel|pharmacy|gas station)\b", re.IGNORECASE),
)
_LOCATION_FROM_QUERY_PATTERNS = (
    re.compile(r"\b(?:in|near|around|at|within)\s+(.+)$", re.IGNORECASE),
)
_CITY_STATE_SHORT_RE = re.compile(r"^[A-Za-z][A-Za-z\s.'-]{1,40},?\s+[A-Za-z]{2}$")
_SET_DEFAULT_LOCATION_PATTERNS = (
    re.compile(r"^\s*set\s+(?:my\s+)?default\s+location\s+to\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*set\s+(?:my\s+)?location\s+to\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*use\s+(.+?)\s+as\s+(?:my\s+)?default\s+location\s*$", re.IGNORECASE),
)
_CLEAR_DEFAULT_LOCATION_PATTERNS = (
    re.compile(r"^\s*(?:clear|remove|forget)\s+(?:my\s+)?default\s+location\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:clear|remove|forget)\s+(?:my\s+)?location\s*$", re.IGNORECASE),
)
_GET_DEFAULT_LOCATION_PATTERNS = (
    re.compile(r"^\s*(?:what(?:'s| is)|show)\s+(?:my\s+)?default\s+location\??\s*$", re.IGNORECASE),
    re.compile(r"^\s*my\s+default\s+location\??\s*$", re.IGNORECASE),
)
_TTS_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_TTS_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:[^)]+)\)")
_TTS_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|>]+?\|>")
_TTS_BULLET_PREFIX_RE = re.compile(r"(?m)^\s*(?:[-*+]|(?:\d+[.)]))\s+")
_TTS_HEADER_PREFIX_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_TTS_BLOCKQUOTE_PREFIX_RE = re.compile(r"(?m)^\s*>\s*")
_TTS_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_TTS_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_TTS_ASTERISK_CTRL_RE = re.compile(r"(?<!\w)\*[A-Za-z](?!\w)")
_TTS_MULTI_WS_RE = re.compile(r"\s+")
_TTS_URL_RE = re.compile(r"\bhttps?://\S+\b", re.IGNORECASE)
_TTS_ALLOWED_PUNCT = set(".,!?;:'\"()[]{}-–—/\\%&@#+=:")
_TTS_ALLOWED_SYMBOLS = {"°", "º", "±", "×", "÷", "‰", "µ", "μ"}
_TTS_NUMBER_RE = r"-?\d+(?:[.,]\d+)?"
_TTS_DEG_C_RE = re.compile(rf"(?i)\b({_TTS_NUMBER_RE})\s*°\s*c\b")
_TTS_DEG_F_RE = re.compile(rf"(?i)\b({_TTS_NUMBER_RE})\s*°\s*f\b")
_TTS_DEG_K_RE = re.compile(rf"(?i)\b({_TTS_NUMBER_RE})\s*°\s*k\b")
_TTS_DEG_RE = re.compile(rf"(?i)\b({_TTS_NUMBER_RE})\s*°\b")
_TTS_PERCENT_RE = re.compile(rf"(?i)\b({_TTS_NUMBER_RE})\s*%")
_TTS_PERMILLE_RE = re.compile(rf"(?i)\b({_TTS_NUMBER_RE})\s*‰")
_TTS_CURRENCY_NAMES = {
    "$": "dollars",
    "€": "euros",
    "£": "pounds",
    "¥": "yen",
    "₹": "rupees",
    "₩": "won",
    "₽": "rubles",
    "¢": "cents",
}

@dataclass
class _UtteranceState:
    last_text: str = ""
    sent_final: bool = False
    turn_id: str = ""
    prefetch_text: str = ""
    prefetch_task: Optional[asyncio.Task[str]] = None
    prefetch_cancel: Optional[CancelToken] = None
    stt_task: Optional[asyncio.Task[object]] = None
    text_task: Optional[asyncio.Task[object]] = None
    ingest_started_at: Optional[float] = None
    audio_tap: Optional[AudioTapQueue] = None


def _tool_calls_from_content(content: str, allowed_tools: set[str]) -> list[dict[str, object]]:
    stripped = content.strip()
    if not stripped:
        return []
    allowed_lookup = {str(name).strip().lower(): str(name).strip() for name in allowed_tools if str(name).strip()}

    def _resolve_allowed_name(raw_name: object) -> str:
        if not isinstance(raw_name, str):
            return ""
        key = raw_name.strip().lower()
        if not key:
            return ""
        return allowed_lookup.get(key, "")

    # Qwen-style wrappers sometimes embed one or more JSON tool calls in <tool_call>...</tool_call>.
    # Capture the whole block body (not brace-balanced regex) so nested argument objects parse correctly.
    xml_matches = re.findall(
        r"<tool_call\b[^>]*>\s*([\s\S]*?)\s*</tool_call\s*>",
        stripped,
        re.IGNORECASE,
    )
    if xml_matches:
        out: list[dict[str, object]] = []
        for block in xml_matches:
            payload_text = block.strip()
            if payload_text.startswith("```"):
                payload_text = re.sub(r"^```(?:json)?\s*", "", payload_text, flags=re.IGNORECASE)
                payload_text = re.sub(r"\s*```$", "", payload_text)
            try:
                payload = json.loads(payload_text)
            except Exception:
                _PARSE_LOG.debug("xml tool_call JSON parse failed: %.200s", payload_text)
                continue
            if not isinstance(payload, dict):
                continue
            cand = _resolve_allowed_name(payload.get("name") or payload.get("tool"))
            if not cand:
                raw_name = payload.get("name") or payload.get("tool")
                _PARSE_LOG.debug("xml tool_call name not in allowed set: %s", raw_name)
                continue
            maybe_params = payload.get("parameters")
            if not isinstance(maybe_params, dict):
                maybe_params = payload.get("arguments")
            params = maybe_params if isinstance(maybe_params, dict) else {}
            try:
                args = json.dumps(params, ensure_ascii=True)
            except Exception:
                args = "{}"
            out.append({"id": "", "type": "function", "function": {"name": cand, "arguments": args}})
        _PARSE_LOG.debug("xml parser matched %d tool call(s)", len(out))
        return out

    name = None
    params: dict[str, object] = {}

    if stripped.startswith("[") and "]" in stripped:
        end = stripped.find("]")
        name = _resolve_allowed_name(stripped[1:end].strip())
        if not name:
            name = None
            return []
        rest = stripped[end + 1 :].strip()
        if rest:
            try:
                payload = json.loads(rest)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                params = payload

    if not name:
        try:
            payload = json.loads(stripped)
        except Exception:
            payload = None
            # Content is not valid JSON — not a tool call, just regular text.
        if isinstance(payload, list):
            out: list[dict[str, object]] = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                item_name = _resolve_allowed_name(item.get("name") or item.get("tool"))
                if not item_name:
                    continue
                maybe_params = item.get("parameters")
                if not isinstance(maybe_params, dict):
                    maybe_params = item.get("arguments")
                params_obj = maybe_params if isinstance(maybe_params, dict) else {}
                try:
                    args = json.dumps(params_obj, ensure_ascii=True)
                except Exception:
                    args = "{}"
                out.append({"id": "", "type": "function", "function": {"name": item_name, "arguments": args}})
            if out:
                _PARSE_LOG.debug("json array parser matched %d tool call(s)", len(out))
                return out
        if isinstance(payload, dict):
            name = _resolve_allowed_name(payload.get("name") or payload.get("tool")) or None
            maybe_params = payload.get("parameters")
            if isinstance(maybe_params, dict):
                params = maybe_params
            else:
                maybe_params = payload.get("arguments")
                if isinstance(maybe_params, dict):
                    params = maybe_params

    if not name:
        if stripped.startswith("{") or stripped.startswith("["):
            _PARSE_LOG.debug("content looks like JSON but no tool call resolved: %.200s", stripped)
        return []

    _PARSE_LOG.debug("json object parser matched tool: %s", name)
    try:
        args = json.dumps(params or {}, ensure_ascii=True)
    except Exception:
        args = "{}"

    return [{"id": "", "type": "function", "function": {"name": name, "arguments": args}}]


def _truncate_text(value: str, max_chars: int) -> str:
    text = (value or "").strip()
    if max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " ...[truncated]"


def _normalize_tool_output(name: str, result: str, *, max_chars: int = 0) -> tuple[dict[str, object], str]:
    raw = (result or "").strip()
    is_error = raw.lower().startswith("tool error:")
    text = _truncate_text(raw, max_chars=max_chars) if raw else "(empty)"
    normalized = {
        "tool": name or "unknown",
        "status": "error" if is_error else "ok",
        "content": text,
    }
    # Compact textual form for LLMs that handle plain text better than JSON-only blobs.
    text_line = f"[{normalized['tool']}] status={normalized['status']} content={text}"
    return normalized, text_line


def _tool_name(tool: dict[str, object]) -> str:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str):
            return name
    name = tool.get("name")
    return name if isinstance(name, str) else ""


def _is_local_search_query(text: str) -> bool:
    query = str(text or "").strip()
    if not query:
        return False
    return any(pattern.search(query) for pattern in _LOCAL_SEARCH_PATTERNS)


def _has_explicit_location(text: str) -> bool:
    query = str(text or "").strip()
    if not query:
        return False
    if "," in query:
        return True
    return any(pattern.search(query) for pattern in _EXPLICIT_LOCATION_PATTERNS)


def _is_weather_query(text: str) -> bool:
    query = str(text or "").strip()
    if not query:
        return False
    return any(pattern.search(query) for pattern in _WEATHER_QUERY_PATTERNS)


def _is_location_required_query(text: str) -> bool:
    return _is_local_search_query(text) or _is_weather_query(text)


def _is_location_only_text(text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    if any(p.search(content) for p in _NON_LOCATION_INTENT_PATTERNS):
        return False
    words = [w for w in re.split(r"\s+", content) if w]
    if len(words) > 8:
        return False
    if _has_explicit_location(content):
        return True
    return bool(_CITY_STATE_SHORT_RE.match(content))


def _extract_location_from_query_text(text: str) -> str:
    content = str(text or "").strip()
    if not content:
        return ""
    if _is_location_only_text(content):
        return content
    for pattern in _LOCATION_FROM_QUERY_PATTERNS:
        m = pattern.search(content)
        if not m:
            continue
        loc = str(m.group(1) or "").strip(" .?!")
        if loc and len([w for w in loc.split() if w]) <= 10:
            return loc
    return ""


def _find_recent_location_hint(messages: list[dict[str, object]]) -> str:
    if not messages:
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        loc = _extract_location_from_query_text(content)
        if loc:
            return loc
    return ""


def _parse_set_default_location(text: str) -> str:
    content = str(text or "").strip()
    if not content:
        return ""
    for pattern in _SET_DEFAULT_LOCATION_PATTERNS:
        m = pattern.match(content)
        if not m:
            continue
        loc = str(m.group(1) or "").strip(" .?!")
        if loc:
            return loc
    return ""


def _is_clear_default_location_command(text: str) -> bool:
    content = str(text or "").strip()
    return any(p.match(content) is not None for p in _CLEAR_DEFAULT_LOCATION_PATTERNS)


def _is_get_default_location_command(text: str) -> bool:
    content = str(text or "").strip()
    return any(p.match(content) is not None for p in _GET_DEFAULT_LOCATION_PATTERNS)


def _extract_location_pref_payload(raw: str) -> dict[str, object]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sanitize_tts_text(text: str) -> str:
    def _expand_speakable_symbols(raw: str) -> str:
        out = str(raw or "")
        out = _TTS_DEG_C_RE.sub(r"\1 degrees Celsius", out)
        out = _TTS_DEG_F_RE.sub(r"\1 degrees Fahrenheit", out)
        out = _TTS_DEG_K_RE.sub(r"\1 degrees Kelvin", out)
        out = _TTS_DEG_RE.sub(r"\1 degrees", out)
        out = _TTS_PERCENT_RE.sub(r"\1 percent", out)
        out = _TTS_PERMILLE_RE.sub(r"\1 per mille", out)

        out = re.sub(r"\s*±\s*", " plus or minus ", out)
        out = re.sub(r"\s*×\s*", " times ", out)
        out = re.sub(r"\s*÷\s*", " divided by ", out)

        for sym, word in _TTS_CURRENCY_NAMES.items():
            out = re.sub(rf"(?<!\w){re.escape(sym)}\s*({_TTS_NUMBER_RE})", rf"\1 {word}", out)
            out = re.sub(rf"\b({_TTS_NUMBER_RE})\s*{re.escape(sym)}(?!\w)", rf"\1 {word}", out)
            out = out.replace(sym, f" {word} ")
        return out

    def _is_speakable_char(ch: str) -> bool:
        if ch.isspace():
            return True
        if ch in _TTS_ALLOWED_PUNCT or ch in _TTS_ALLOWED_SYMBOLS:
            return True
        cat = unicodedata.category(ch)
        if cat == "Sc":
            return True
        if cat[:1] in {"L", "N", "M"}:
            return True
        return False

    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    cleaned = _TTS_SPECIAL_TOKEN_RE.sub(" ", cleaned)
    cleaned = _TTS_CODE_FENCE_RE.sub(" ", cleaned)
    cleaned = _TTS_INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _TTS_MARKDOWN_IMAGE_RE.sub(r"\1", cleaned)
    cleaned = _TTS_MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = _TTS_HEADER_PREFIX_RE.sub("", cleaned)
    cleaned = _TTS_BLOCKQUOTE_PREFIX_RE.sub("", cleaned)
    cleaned = _TTS_BULLET_PREFIX_RE.sub("", cleaned)
    cleaned = _TTS_URL_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("**", " ").replace("__", " ").replace("~~", " ").replace("`", " ")
    cleaned = _TTS_ASTERISK_CTRL_RE.sub(" ", cleaned)
    cleaned = _expand_speakable_symbols(cleaned)
    cleaned = "".join(ch if _is_speakable_char(ch) else " " for ch in cleaned)
    cleaned = _TTS_MULTI_WS_RE.sub(" ", cleaned).strip()
    return cleaned


def _schema_type_ok(value: object, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True


def _validate_tool_args(name: str, args: dict[str, object], tools: list[dict[str, object]]) -> str | None:
    tool_schema = None
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        tool_name = fn.get("name") if isinstance(fn.get("name"), str) else ""
        if tool_name == name:
            params = fn.get("parameters")
            if isinstance(params, dict):
                tool_schema = params
            break
    if tool_schema is None:
        return f"schema missing for tool '{name}'"
    schema_type = tool_schema.get("type")
    if isinstance(schema_type, str) and schema_type != "object":
        return f"invalid schema type for tool '{name}': {schema_type}"
    if not isinstance(args, dict):
        return f"arguments for tool '{name}' must be an object"

    required = tool_schema.get("required")
    if isinstance(required, list):
        for req in required:
            if isinstance(req, str) and req not in args:
                return f"missing required argument '{req}' for tool '{name}'"

    props = tool_schema.get("properties") if isinstance(tool_schema.get("properties"), dict) else {}
    additional = tool_schema.get("additionalProperties", True)
    if additional is False:
        for k in args:
            if k not in props:
                return f"unknown argument '{k}' for tool '{name}'"

    for key, value in args.items():
        spec = props.get(key) if isinstance(props, dict) else None
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if isinstance(expected, list):
            if not any(isinstance(t, str) and _schema_type_ok(value, t) for t in expected):
                return f"argument '{key}' has wrong type for tool '{name}'"
        elif isinstance(expected, str):
            if not _schema_type_ok(value, expected):
                return f"argument '{key}' must be {expected} for tool '{name}'"

        enum = spec.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            return f"argument '{key}' has invalid value for tool '{name}'"

        if isinstance(value, list):
            item_spec = spec.get("items") if isinstance(spec.get("items"), dict) else {}
            item_type = item_spec.get("type")
            if isinstance(item_type, str):
                for idx, item in enumerate(value):
                    if not _schema_type_ok(item, item_type):
                        return f"argument '{key}[{idx}]' must be {item_type} for tool '{name}'"
            item_enum = item_spec.get("enum")
            if isinstance(item_enum, list) and item_enum:
                for item in value:
                    if item not in item_enum:
                        return f"argument '{key}' has invalid list item for tool '{name}'"

    return None


class UtteranceRunner:
    def __init__(
        self,
        stt: STTStream,
        llm_base_url: str,
        llm_model: str,
        tts_base_url: str,
        logger,
        *,
        log_fn,
        session_id: str,
        client_key: str,
        history: ChatHistory,
    ):
        self.stt = stt
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.tts_base_url = tts_base_url
        self.log = logger
        self._log = log_fn
        self._session_id = session_id
        self._client_key = client_key or ""
        self._history = history
        self._last_used_tools: set[str] = set()

    def _tools_base_url(self) -> str:
        url = str(settings.TOOLS_MCP_URL or "").strip()
        if not url:
            return ""
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def _location_pref_args(self, **kwargs: object) -> dict[str, object]:
        args: dict[str, object] = dict(kwargs)
        if self._client_key:
            args["client_id"] = self._client_key
        return args

    async def _reset_remote_stats(self, path: str) -> list[str]:
        errors: list[str] = []
        targets = []
        tools_base = self._tools_base_url()
        if tools_base:
            targets.append((tools_base, "tools"))
        if settings.TTS_BASE_URL:
            targets.append((settings.TTS_BASE_URL, "tts"))
        async with httpx.AsyncClient(timeout=3.0) as client:
            for base, name in targets:
                try:
                    resp = await client.post(f"{base}{path}")
                    if resp.status_code >= 400:
                        errors.append(f"{name} status={resp.status_code}")
                except Exception as exc:
                    errors.append(f"{name} error={exc!r}")
        return errors

    async def run(
        self,
        client_in_q,
        send: SendEvent,
        *,
        sample_rate_hz: int,
        request_partials: bool,
        language_code: str,
        eou_event: asyncio.Event,
        text_in_q: Optional["asyncio.Queue[str]"] = None,
        no_tts: bool = False,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> None:
        state = _UtteranceState()
        state.ingest_started_at = time.monotonic()
        stt_cancel = CancelToken(asyncio.Event())
        if interrupt_event is not None and interrupt_event.is_set():
            interrupt_event.clear()

        self._log("info", "utterance start")
        await send("state", {"phase": "LISTENING", "turn_id": ""})

        async def _handle_text_input(text: str) -> bool:
            stripped = text.strip()
            if not stripped:
                return False
            if stripped.lower() in {
                "clear all stats",
                "reset stats",
            }:
                turn_id = self._ensure_turn_id(state)
                self._log("info", "clear stats requested", turn_id=turn_id)
                await send("transcript", {"turn_id": turn_id, "is_final": True, "text": stripped})
                state.sent_final = True
                reset_turn_stats()
                errors = await self._reset_remote_stats("/stats/reset")
                stt_cancel.cancel()
                if errors:
                    reply_text = "Stats reset with warnings: " + "; ".join(errors)
                else:
                    reply_text = "Stats reset."
                await send("reply", {"turn_id": turn_id, "text": reply_text, "is_final": True})
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
                await self._cleanup_tasks(state)
                return True
            if stripped.lower() in {
                "clear all lifetime stats",
                "reset lifetime stats",
            }:
                turn_id = self._ensure_turn_id(state)
                self._log("info", "clear lifetime stats requested", turn_id=turn_id)
                await send("transcript", {"turn_id": turn_id, "is_final": True, "text": stripped})
                state.sent_final = True
                reset_turn_stats_all()
                errors = await self._reset_remote_stats("/stats/reset_lifetime")
                stt_cancel.cancel()
                if errors:
                    reply_text = "Lifetime stats reset with warnings: " + "; ".join(errors)
                else:
                    reply_text = "Lifetime stats reset."
                await send("reply", {"turn_id": turn_id, "text": reply_text, "is_final": True})
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
                await self._cleanup_tasks(state)
                return True
            if stripped.lower() in {
                "clear history",
                "clear the history",
                "remove history",
                "delete history",
                "no history",
            }:
                turn_id = self._ensure_turn_id(state)
                self._log("info", "clear history", turn_id=turn_id)
                await send("transcript", {"turn_id": turn_id, "is_final": True, "text": stripped})
                state.sent_final = True
                self._history.reset()
                stt_cancel.cancel()
                await send("reply", {"turn_id": turn_id, "text": "History cleared.", "is_final": True})
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
                await self._cleanup_tasks(state)
                return True
            if stripped.lower() in {
                "refresh tools",
                "reload tools",
            }:
                turn_id = self._ensure_turn_id(state)
                self._log("info", "refresh tools requested", turn_id=turn_id)
                await send("transcript", {"turn_id": turn_id, "is_final": True, "text": stripped})
                state.sent_final = True
                client = get_tools_client()
                if client is None:
                    reply_text = "Tools client unavailable."
                else:
                    try:
                        await client.refresh_tools()
                        reply_text = "Tools refreshed."
                    except Exception as exc:
                        reply_text = f"Tools refresh failed: {exc}"
                stt_cancel.cancel()
                await send("reply", {"turn_id": turn_id, "text": reply_text, "is_final": True})
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
                await self._cleanup_tasks(state)
                return True
            location_set = _parse_set_default_location(stripped)
            if location_set or _is_get_default_location_command(stripped) or _is_clear_default_location_command(stripped):
                turn_id = self._ensure_turn_id(state)
                self._log("info", "location preference command", turn_id=turn_id, text=stripped)
                await send("transcript", {"turn_id": turn_id, "is_final": True, "text": stripped})
                state.sent_final = True
                client = get_tools_client()
                if client is None:
                    reply_text = "Location preferences are unavailable right now."
                else:
                    try:
                        if location_set:
                            raw = await client.call_tool(
                                _LOCATION_PREF_TOOL_NAME,
                                self._location_pref_args(action="set_default", location=location_set),
                            )
                            payload = _extract_location_pref_payload(raw)
                            saved = str(payload.get("default_location") or location_set).strip()
                            reply_text = f"Saved your default location as {saved}."
                        elif _is_clear_default_location_command(stripped):
                            await client.call_tool(
                                _LOCATION_PREF_TOOL_NAME,
                                self._location_pref_args(action="clear_default"),
                            )
                            reply_text = "Cleared your default location."
                        else:
                            raw = await client.call_tool(
                                _LOCATION_PREF_TOOL_NAME,
                                self._location_pref_args(action="get_default"),
                            )
                            payload = _extract_location_pref_payload(raw)
                            saved = str(payload.get("default_location") or "").strip()
                            if saved:
                                reply_text = f"Your default location is {saved}."
                            else:
                                reply_text = "You do not have a default location set."
                    except Exception as exc:
                        reply_text = f"Location preference update failed: {exc}"
                stt_cancel.cancel()
                await send("reply", {"turn_id": turn_id, "text": reply_text, "is_final": True})
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
                await self._cleanup_tasks(state)
                return True
            turn_id = self._ensure_turn_id(state)
            self._log("info", "commit text", turn_id=turn_id, text=stripped)
            await send("transcript", {"turn_id": turn_id, "is_final": True, "text": stripped})
            state.sent_final = True
            state.last_text = stripped
            await self._commit_text(
                state,
                stripped,
                send,
                stt_cancel,
                no_tts=no_tts,
                interrupt_event=interrupt_event,
            )
            return True

        if text_in_q is not None:
            try:
                immediate_text = text_in_q.get_nowait()
            except asyncio.QueueEmpty:
                immediate_text = ""
            if await _handle_text_input(immediate_text):
                return
            state.text_task = asyncio.create_task(text_in_q.get(), name="text_input")

        audio_tap = AudioTapQueue(client_in_q, sample_rate_hz=sample_rate_hz)
        state.audio_tap = audio_tap
        stt_iter = self.stt.stream(
            audio_frames=audio_tap,
            sample_rate_hz=sample_rate_hz,
            request_partials=request_partials,
            language_code=language_code,
            cancel=stt_cancel,
            turn_id=state.turn_id,
            session_id=self._session_id,
        ).__aiter__()

        idle_timeout_s = float(settings.ORCH_PIPELINE_IDLE_TIMEOUT_S)
        idle_ms = float(settings.STT_EOS_IDLE_MS)
        idle_s = max(idle_ms, 0.0) / 1000.0

        while True:
            if eou_event.is_set():
                if state.text_task is not None and not state.text_task.done():
                    await asyncio.wait({state.text_task}, timeout=0)
                if state.text_task is not None and state.text_task.done():
                    try:
                        text = state.text_task.result()
                    except Exception as exc:
                        self._log("warning", "text input task failed", error=repr(exc))
                        text = ""
                    state.text_task = None
                    if await _handle_text_input(text):
                        return
                if state.last_text.strip():
                    self._log("info", "commit EOU", turn_id=self._ensure_turn_id(state), text=state.last_text.strip())
                    await self._commit_text(
                        state,
                        state.last_text.strip(),
                        send,
                        stt_cancel,
                        no_tts=no_tts,
                        interrupt_event=interrupt_event,
                    )
                    return
                self._log("info", "EOU with no transcript", turn_id=state.turn_id)
                stt_cancel.cancel()
                break

            timeout = self._next_timeout(state, idle_s, idle_timeout_s)
            try:
                source, payload = await self._await_input(state, stt_iter, timeout)
            except asyncio.TimeoutError:
                if state.last_text.strip():
                    self._log(
                        "info",
                        "commit idle",
                        turn_id=self._ensure_turn_id(state),
                        idle_ms=int(idle_ms),
                        text=state.last_text.strip(),
                    )
                    await self._commit_text(
                        state,
                        state.last_text.strip(),
                        send,
                        stt_cancel,
                        no_tts=no_tts,
                        interrupt_event=interrupt_event,
                    )
                    return
                if idle_timeout_s > 0.0:
                    self._log("info", "utterance idle timeout", turn_id=state.turn_id)
                    stt_cancel.cancel()
                    break
                continue
            except StopAsyncIteration:
                break

            if source == "text":
                text = payload
                if await _handle_text_input(text):
                    return
                if text_in_q is not None:
                    state.text_task = asyncio.create_task(text_in_q.get(), name="text_input")
                continue

            text, is_final = payload
            self._log(
                "info",
                "stt next",
                turn_id=state.turn_id,
                text=text,
                is_final=is_final,
                last_text=state.last_text,
                eou=eou_event.is_set(),
            )

            if text.strip():
                state.last_text = text
                if request_partials and not is_final:
                    self._start_prefetch(state, text.strip())

            await send("transcript", {"turn_id": self._ensure_turn_id(state), "is_final": is_final, "text": text})
            if is_final and text.strip():
                state.sent_final = True
                self._log(
                    "info",
                    "commit final",
                    turn_id=self._ensure_turn_id(state),
                    is_final=is_final,
                    eou=eou_event.is_set(),
                    text=text.strip(),
                )
                await self._commit_text(
                    state,
                    text.strip(),
                    send,
                    stt_cancel,
                    no_tts=no_tts,
                    interrupt_event=interrupt_event,
                )
                return

        if state.last_text.strip():
            self._log("info", "commit STT ended", turn_id=self._ensure_turn_id(state), text=state.last_text.strip())
            await self._commit_text(
                state,
                state.last_text.strip(),
                send,
                stt_cancel,
                no_tts=no_tts,
                interrupt_event=interrupt_event,
            )
        await self._cleanup_tasks(state)

    def _next_timeout(self, state: _UtteranceState, idle_s: float, idle_timeout_s: float) -> Optional[float]:
        if state.last_text.strip() and idle_s > 0.0:
            return idle_s
        if idle_timeout_s > 0.0:
            return idle_timeout_s
        return None

    async def _await_input(
        self,
        state: _UtteranceState,
        stt_iter,
        timeout: Optional[float],
    ) -> tuple[str, object]:
        if state.stt_task is None:
            state.stt_task = asyncio.create_task(stt_iter.__anext__(), name="stt_next")
            state.stt_task.add_done_callback(self._consume_stt_result)
        tasks = {state.stt_task}
        if state.text_task is not None:
            tasks.add(state.text_task)

        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError

        if state.text_task is not None and state.text_task in done:
            try:
                text = state.text_task.result()
            except Exception as exc:
                self._log("warning", "text input task failed", error=repr(exc))
                text = ""
            state.text_task = None
            if text.strip():
                return "text", text
            if state.stt_task in done:
                try:
                    result = state.stt_task.result()
                except (StopAsyncIteration, asyncio.CancelledError):
                    result = None
                except Exception as exc:
                    self._log("warning", "STT task error", error=repr(exc))
                    result = None
                state.stt_task = None
                if result is not None:
                    return "stt", result
            return "text", text

        if state.stt_task in done:
            try:
                result = state.stt_task.result()
            except (StopAsyncIteration, asyncio.CancelledError):
                result = None
            except Exception as exc:
                self._log("warning", "STT task error", error=repr(exc))
                result = None
            state.stt_task = None
            if result is not None:
                return "stt", result

        raise asyncio.TimeoutError

    def _ensure_turn_id(self, state: _UtteranceState) -> str:
        if not state.turn_id:
            state.turn_id = new_turn_id()
        return state.turn_id

    def _start_prefetch(self, state: _UtteranceState, text: str) -> None:
        if state.prefetch_text == text and state.prefetch_task and not state.prefetch_task.done():
            return
        if state.prefetch_task and not state.prefetch_task.done():
            if state.prefetch_cancel:
                state.prefetch_cancel.cancel()
            state.prefetch_task.cancel()
        state.prefetch_text = text
        state.prefetch_cancel = CancelToken(asyncio.Event())

        async def _prefetch() -> str:
            text_out, _, _ = await self._collect_llm(text, state.prefetch_cancel)
            return text_out

        state.prefetch_task = asyncio.create_task(_prefetch(), name="llm_prefetch")
        state.prefetch_task.add_done_callback(self._consume_prefetch_result)

    def _consume_prefetch_result(self, task: asyncio.Task[str]) -> None:
        try:
            _ = task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log("warning", "LLM prefetch task failed", error=repr(exc))

    async def _commit_text(
        self,
        state: _UtteranceState,
        text: str,
        send: SendEvent,
        stt_cancel: CancelToken,
        *,
        no_tts: bool = False,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> None:
        if state.ingest_started_at is not None:
            observe_ingest_seconds(time.monotonic() - state.ingest_started_at)
            state.ingest_started_at = None
        start = time.monotonic()
        try:
            await self._emit_final_if_needed(state, text, send)
            stt_cancel.cancel()
            # Finalise turn audio for speaker identification (best-effort).
            audio_ref: Optional[tuple[str, str]] = None
            if state.audio_tap is not None:
                tap = state.audio_tap
                state.audio_tap = None
                _loop = asyncio.get_running_loop()
                audio_ref = await _loop.run_in_executor(
                    None, tap.write_wav, CLIENT_AUDIO_DIR, self._session_id
                )
            await asyncio.get_running_loop().run_in_executor(
                None, cleanup_stale_audio, CLIENT_AUDIO_DIR
            )
            speak_cancel = (
                CancelToken(interrupt_event)
                if interrupt_event is not None
                else CancelToken(asyncio.Event())
            )
            turn_timeout = max(5.0, float(settings.ORCH_TURN_MAX_S))
            try:
                await asyncio.wait_for(
                    self._think_and_speak(
                        text,
                        send,
                        speak_cancel,
                        turn_id=self._ensure_turn_id(state),
                        prefetch_task=state.prefetch_task,
                        prefetch_text=state.prefetch_text,
                        prefetch_cancel=state.prefetch_cancel,
                        no_tts=no_tts,
                        audio_ref=audio_ref,
                    ),
                    timeout=turn_timeout,
                )
            except asyncio.TimeoutError:
                turn_id = self._ensure_turn_id(state)
                inc_turn_watchdog_timeouts("total")
                inc_turn_outcome("watchdog_timeout")
                self._log("warning", "turn watchdog timeout", turn_id=turn_id, max_s=turn_timeout)
                await send("error", {"message": f"Turn timed out after {turn_timeout:.1f}s"})
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
            await self._cleanup_tasks(state)
        finally:
            observe_turn_seconds(time.monotonic() - start)

    async def _emit_final_if_needed(
        self,
        state: _UtteranceState,
        text: str,
        send: SendEvent,
    ) -> None:
        if state.sent_final:
            return
        if not text.strip():
            return
        state.sent_final = True
        await send("transcript", {"turn_id": self._ensure_turn_id(state), "is_final": True, "text": text})

    async def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except StopAsyncIteration:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log("warning", "background task failed", error=repr(exc))

    def _consume_stt_result(self, task: asyncio.Task) -> None:
        try:
            _ = task.result()
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        except Exception as exc:
            self._log("warning", "STT task failed", error=repr(exc))

    async def _cleanup_tasks(self, state: _UtteranceState) -> None:
        if state.prefetch_cancel:
            state.prefetch_cancel.cancel()
        await self._cancel_task(state.prefetch_task)
        await self._cancel_task(state.text_task)
        await self._cancel_task(state.stt_task)

    async def _maybe_resolve_location_query(
        self,
        text: str,
        tools: list[dict[str, object]],
        *,
        history_messages: Optional[list[dict[str, object]]] = None,
        turn_id: str = "",
    ) -> tuple[str, str]:
        if not _is_location_required_query(text):
            return text, ""
        if _has_explicit_location(text):
            return text, ""
        tool_names = {_tool_name(tool) for tool in tools if isinstance(tool, dict)}
        if _LOCATION_PREF_TOOL_NAME not in tool_names:
            return text, ""
        client = get_tools_client()
        if client is None:
            return text, ""
        try:
            raw = await client.call_tool(
                _LOCATION_PREF_TOOL_NAME,
                self._location_pref_args(
                    action="resolve_query",
                    query=text,
                    use_default_when_missing=True,
                ),
            )
        except Exception as exc:
            self._log("warning", "location preference resolve failed", turn_id=turn_id, error=repr(exc))
            return text, ""
        payload = _extract_location_pref_payload(raw)
        if not payload:
            return text, ""
        status = str(payload.get("status") or "").strip().lower()
        if status == "resolved_with_default":
            resolved = payload.get("query")
            if isinstance(resolved, str) and resolved.strip():
                self._log("info", "location resolved with default", turn_id=turn_id, query=resolved)
                return resolved.strip(), ""
        if status == "needs_location":
            recent_loc = _find_recent_location_hint(history_messages or [])
            if recent_loc:
                resolved = f"{text.strip()} near {recent_loc}".strip()
                self._log("info", "location resolved with recent history", turn_id=turn_id, query=resolved)
                return resolved, ""
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return text, message.strip()
            return (
                text,
                "What location should I use for this search? Share a city or address and tell me if you want it saved as your default.",
            )
        return text, ""

    async def _collect_llm(
        self,
        text: str,
        cancel: CancelToken,
        *,
        send: Optional[SendEvent] = None,
        turn_id: str = "",
        emit_reply: bool = False,
        audio_ref: Optional[tuple[str, str]] = None,
    ) -> tuple[str, bool, list[dict]]:
        buf: list[str] = []
        did_emit = False
        raw_text = str(text or "").strip()
        history_snapshot = self._history.snapshot()
        tools = list_tools()

        awaiting_location = self._history.awaiting_location
        pending_location_query = self._history.pending_location_query
        self._history.clear_pending_location()

        text_candidate = raw_text
        if awaiting_location and _is_location_only_text(raw_text) and pending_location_query:
            text_candidate = f"{pending_location_query} in {raw_text}".strip()
            self._log(
                "info",
                "location follow-up expanded",
                turn_id=turn_id,
                original=raw_text,
                expanded=text_candidate,
            )

        text_for_llm, immediate_reply = await self._maybe_resolve_location_query(
            text_candidate,
            tools,
            history_messages=history_snapshot,
            turn_id=turn_id,
        )
        if immediate_reply:
            self._history.set_pending_location(text_candidate)
            if emit_reply and send:
                did_emit = True
                await send("reply", {"turn_id": turn_id, "text": immediate_reply, "is_final": True})
            return immediate_reply, did_emit, []
        messages = self._history.build_messages(text_for_llm)
        if audio_ref is not None:
            sid, aid = audio_ref
            ref_msg = {"role": "system", "content": f"[AUDIO_REF session_id={sid} audio_id={aid}]"}
            if messages and messages[-1].get("role") == "user":
                messages.insert(-1, ref_msg)
            else:
                messages.append(ref_msg)
        self._log(
            "info",
            "LLM request start",
            turn_id=turn_id,
            text=text_for_llm,
            tools=len(tools),
        )
        self._log("debug", "LLM request", turn_id=turn_id, text=text_for_llm, messages=messages)
        tool_names = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
            name = fn.get("name") if fn else tool.get("name")
            if isinstance(name, str) and name:
                tool_names.append(name)
        if tool_names:
            self._log("debug", "LLM tools", turn_id=turn_id, tool_count=len(tool_names), tools=",".join(tool_names))
        else:
            self._log("debug", "LLM tools", turn_id=turn_id, tool_count=0)
        if tools:
            tool_text, tool_msgs = await self._collect_llm_with_tools(
                messages,
                tools,
                user_text=text_for_llm,
                cancel=cancel,
                turn_id=turn_id,
                send=send,
            )
            if tool_text:
                if emit_reply and send:
                    did_emit = True
                    await send("reply", {"turn_id": turn_id, "text": tool_text, "is_final": True})
                return tool_text, did_emit, tool_msgs
        start = time.monotonic()
        try:
            async for delta in stream_llm(
                self.llm_base_url,
                self.llm_model,
                text_for_llm,
                cancel,
                messages=messages,
                tools=None,
                logger=self.log,
                turn_id=turn_id,
                session_id=self._session_id,
            ):
                buf.append(delta)
                if emit_reply and send:
                    did_emit = True
                    await send("reply", {"turn_id": turn_id, "text": delta, "is_final": False})
        except (RuntimeError, TimeoutError) as exc:
            self._log("warning", "LLM stream failed", turn_id=turn_id, error=repr(exc))
        finally:
            observe_llm_seconds(time.monotonic() - start, mode="stream")
        if emit_reply and send:
            did_emit = True
            await send("reply", {"turn_id": turn_id, "text": "", "is_final": True})
        return "".join(buf).strip(), did_emit, []

    async def _collect_llm_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]],
        user_text: str,
        cancel: CancelToken,
        *,
        send: Optional[SendEvent] = None,
        turn_id: str = "",
    ) -> tuple[str, list[dict]]:
        async def _stream_to_text(
            *,
            prompt_text: str,
            prompt_messages: list[dict[str, str]],
            temperature: float,
            top_p: float,
            mode: str,
            tools_payload: Optional[list[dict[str, object]]] = None,
            tool_choice_payload: object = None,
        ) -> str:
            parts: list[str] = []
            start_stream = time.monotonic()
            try:
                async for delta in stream_chat_completions(
                    self.llm_base_url,
                    self.llm_model,
                    prompt_text,
                    cancel,
                    temperature=temperature,
                    top_p=top_p,
                    messages=prompt_messages,
                    tools=tools_payload,
                    tool_choice=tool_choice_payload if isinstance(tool_choice_payload, str) else None,
                    emit_tool_calls_as_content=bool(tools_payload),
                    logger=self.log,
                ):
                    if isinstance(delta, str) and delta:
                        parts.append(delta)
            finally:
                observe_llm_seconds(time.monotonic() - start_stream, mode=mode)
            return "".join(parts).strip()

        tool_choice: object = "auto"
        working_messages = [dict(m) for m in messages]
        working_tools = [dict(t) for t in tools]
        max_tool_chars = max(0, int(settings.ORCH_TOOL_RESULT_MAX_CHARS))
        max_tool_summary_chars = max(0, int(settings.ORCH_TOOL_SUMMARY_MAX_CHARS))
        self._log("debug", "LLM tool choice auto", turn_id=turn_id)
        if send and turn_id:
            await send("state", {"phase": "THINKING", "turn_id": turn_id})

        allowed_tools = {
            name
            for name in (
                tool.get("function", {}).get("name")
                if isinstance(tool.get("function"), dict)
                else tool.get("name")
                for tool in working_tools
                if isinstance(tool, dict)
            )
            if isinstance(name, str) and name
        }
        tool_names_log = sorted(n for n in allowed_tools if n)
        self._log("info", "LLM tool-capable request", turn_id=turn_id, tools=",".join(tool_names_log))

        _MAX_TOOL_ROUNDS = 3
        storable_tool_msgs: list[dict] = []
        tool_outputs: list[str] = []
        used_tool_names: set[str] = set()
        local_places_raw_output = ""
        had_any_tool_calls = False
        parallelism = max(1, int(settings.ORCH_TOOL_PARALLELISM))
        tools_client = get_tools_client()
        sem = asyncio.Semaphore(parallelism)
        messages = working_messages

        _HA_RETRY_TOOLS = {"homeassistant__HassTurnOn", "homeassistant__HassTurnOff"}
        _HA_LOCK_SUFFIX = " Lock"

        _HA_NON_LOCK_CLASSES = {"light", "switch", "outlet"}

        def _ha_should_retry_with_lock(tool_name: str, args: dict, result: str) -> bool:
            if tool_name not in _HA_RETRY_TOOLS:
                return False
            device_name = str(args.get("name") or "")
            if not device_name or device_name.endswith(_HA_LOCK_SUFFIX):
                return False
            dc = args.get("device_classes")
            if isinstance(dc, list) and any(c in _HA_NON_LOCK_CLASSES for c in dc):
                return False
            if not result:
                return False
            lower = result.lower()
            return "error" in lower or "no match" in lower or "not found" in lower or "failed" in lower

        async def _execute_tool_call(idx: int, call: dict[str, object]) -> tuple[int, dict[str, str], str, str]:
            tool_id = call.get("id") or ""
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = fn.get("name") if isinstance(fn.get("name"), str) else ""
            args_raw = fn.get("arguments") if isinstance(fn.get("arguments"), str) else ""
            args = parse_tool_args(args_raw)
            result = ""
            start_call = time.monotonic()
            async with sem:
                self._log("debug", "tool execute", turn_id=turn_id, tool=name, args=args)
                validation_error = _validate_tool_args(name, args, working_tools)
                if validation_error:
                    result = f"Tool error: {validation_error}"
                    self._log("warning", "tool validation failed", turn_id=turn_id, tool=name, error=validation_error)
                elif tools_client is None:
                    result = "Tool error: tools client unavailable"
                else:
                    try:
                        result = await tools_client.call_tool(name, args)
                        self._log(
                            "debug",
                            "tool execute complete",
                            turn_id=turn_id,
                            tool=name,
                            chars=len(result or ""),
                        )
                        if _ha_should_retry_with_lock(name, args, result):
                            retry_args = dict(args)
                            retry_args["name"] = str(args["name"]) + _HA_LOCK_SUFFIX
                            self._log("info", "HA retry with Lock suffix", turn_id=turn_id, tool=name, name=retry_args["name"])
                            result = await tools_client.call_tool(name, retry_args)
                    except Exception as exc:
                        result = f"Tool error: {exc!r}"
            observe_tool_call_seconds(time.monotonic() - start_call)
            msg = {
                "role": "tool",
                "tool_call_id": tool_id,
                "content": _truncate_text(result, max_tool_chars),
            }
            return idx, msg, name, str(msg["content"])

        try:
            for round_idx in range(_MAX_TOOL_ROUNDS):
                try:
                    content = await _stream_to_text(
                        prompt_text=user_text,
                        prompt_messages=messages,
                        temperature=float(settings.LLM_TEMPERATURE),
                        top_p=float(settings.LLM_TOP_P),
                        mode="tool",
                        tools_payload=working_tools,
                        tool_choice_payload=tool_choice,
                    )
                except Exception as exc:
                    self._log("warning", "LLM tool call failed", turn_id=turn_id, round=round_idx, error=repr(exc))
                    return "", storable_tool_msgs

                tool_calls = _tool_calls_from_content(content, allowed_tools) if isinstance(content, str) else None
                if not tool_calls:
                    if not had_any_tool_calls:
                        # LLM chose not to use any tools — return direct response
                        return content.strip() if isinstance(content, str) else "", []
                    # LLM finished tool use; fall through to synthesis
                    break

                had_any_tool_calls = True
                try:
                    round_names = [c.get("function", {}).get("name", "") for c in tool_calls if isinstance(c, dict)]
                    round_names = [n for n in round_names if n]
                    if round_names:
                        self._log("info", "LLM tool request", turn_id=turn_id, round=round_idx, tools=",".join(round_names))
                except Exception:
                    pass

                messages.append({"role": "assistant", "content": content})
                storable_tool_msgs.append({"role": "assistant", "content": content})

                tasks = []
                for idx, call in enumerate(tool_calls):
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = fn.get("name") if isinstance(fn.get("name"), str) else ""
                    if not name:
                        continue
                    tasks.append(asyncio.create_task(_execute_tool_call(idx, call)))

                if tasks:
                    results = await asyncio.gather(*tasks)
                    for _, tool_msg, tool_name, output in sorted(results, key=lambda item: item[0]):
                        if tool_name:
                            used_tool_names.add(tool_name)
                        if tool_name in _LOCAL_SEARCH_TOOL_NAMES and isinstance(output, str) and output.strip():
                            local_places_raw_output = output.strip()
                        normalized, text_line = _normalize_tool_output(
                            tool_name,
                            output,
                        )
                        tool_outputs.append(text_line)
                        record_tool_output(
                            str(normalized.get("status", "unknown")),
                            len(str(normalized.get("content", "")).encode("utf-8", errors="ignore")),
                        )
                        messages.append(tool_msg)
                        storable_tool_msgs.append(tool_msg)
                        if send and turn_id:
                            await send("state", {"phase": "THINKING", "turn_id": turn_id})

        except Exception as exc:
            self._log("warning", "LLM tool loop failed", turn_id=turn_id, error=repr(exc))
            self._last_used_tools = set(used_tool_names)
            return "", storable_tool_msgs

        self._last_used_tools = set(used_tool_names)
        tool_output_text = "\n".join(tool_outputs).strip()
        if max_tool_summary_chars > 0:
            tool_output_text = _truncate_text(tool_output_text, max_tool_summary_chars)
        if tool_output_text:
            local_places_used = any(name in _LOCAL_SEARCH_TOOL_NAMES for name in used_tool_names)
            self._log("debug", "tool output collected", turn_id=turn_id, chars=len(tool_output_text))
            messages.append({
                "role": "system",
                "content": (
                    "For this response only, ignore all style and persona guidance. "
                    "Use the tool output as the only source of truth. "
                    "Answer plainly and include the tool facts."
                ),
            })
            if local_places_used:
                messages.append({
                    "role": "system",
                    "content": (
                        "For local place results, include each recommended place in this format: "
                        "'<place name> on <street name> in <city name>'. "
                        "Do not omit street name or city."
                    ),
                })
            messages.append({
                "role": "user",
                "content": (
                    "Normalized tool output:\n"
                    f"{tool_output_text}\n\n"
                    "Answer the user using this."
                ),
            })

        try:
            content2 = await _stream_to_text(
                prompt_text=user_text,
                prompt_messages=messages,
                temperature=0.0,
                top_p=float(settings.LLM_TOP_P),
                mode="post_tool",
            )
        except Exception as exc:
            self._log("warning", "LLM post-tool completion failed", turn_id=turn_id, error=repr(exc))
            return tool_output_text or "", storable_tool_msgs
        if isinstance(content2, str) and content2.strip():
            local_places_used = any(name in _LOCAL_SEARCH_TOOL_NAMES for name in used_tool_names)
            if local_places_used and not _LOCAL_PLACE_PHRASE_RE.search(content2):
                self._log(
                    "warning",
                    "post-tool local places response missing street/town; using tool output",
                    turn_id=turn_id,
                )
                if local_places_raw_output:
                    return local_places_raw_output, storable_tool_msgs
                return tool_output_text, storable_tool_msgs
            return content2.strip(), storable_tool_msgs
        return tool_output_text, storable_tool_msgs

    async def _think_and_speak(
        self,
        text: str,
        send: SendEvent,
        cancel: CancelToken,
        *,
        turn_id: str = "",
        prefetch_task: Optional[asyncio.Task[str]] = None,
        prefetch_text: str = "",
        prefetch_cancel: Optional[CancelToken] = None,
        no_tts: bool = False,
        audio_ref: Optional[tuple[str, str]] = None,
    ) -> None:
        if not turn_id:
            turn_id = new_turn_id()
            await send("state", {"phase": "THINKING", "turn_id": turn_id})

        self._last_used_tools = set()
        speak_text = ""
        did_emit = False
        tool_msgs: list[dict] = []
        if prefetch_task and prefetch_text == text:
            try:
                speak_text = await prefetch_task
            except asyncio.CancelledError:
                speak_text = ""
            except Exception as exc:
                self._log("warning", "LLM prefetch task failed", turn_id=turn_id, error=repr(exc))
            if not speak_text:
                self._log("debug", "LLM prefetch empty; falling back", turn_id=turn_id)
                speak_text, did_emit, tool_msgs = await self._collect_llm(
                    text,
                    cancel,
                    send=send,
                    turn_id=turn_id,
                    emit_reply=True,
                    audio_ref=audio_ref,
                )
        else:
            if prefetch_task and not prefetch_task.done():
                if prefetch_cancel:
                    prefetch_cancel.cancel()
                prefetch_task.cancel()
            speak_text, did_emit, tool_msgs = await self._collect_llm(
                text,
                cancel,
                send=send,
                turn_id=turn_id,
                emit_reply=True,
                audio_ref=audio_ref,
            )
        if speak_text:
            if not did_emit:
                await send("reply", {"turn_id": turn_id, "text": speak_text, "is_final": True})
            self._log("debug", "LLM response", turn_id=turn_id, text=speak_text)
            self._log("info", "LLM final response", turn_id=turn_id, text=speak_text)
            try:
                record_turn()
            except Exception:
                pass
            if tool_msgs:
                self._history.add_tool_turn(text, tool_msgs, speak_text)
            else:
                self._history.add_turn(text, speak_text)
            if no_tts:
                inc_turn_outcome("ok")
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
                self._log("info", "TTS disabled; idle", turn_id=turn_id)
                return
            await self._speak(turn_id, speak_text, send, cancel)
        else:
            fallback = "Okay."
            self._log("warning", "LLM produced no speakable text; speaking fallback", turn_id=turn_id)
            await send("reply", {"turn_id": turn_id, "text": fallback, "is_final": True})
            try:
                record_turn()
            except Exception:
                pass
            if no_tts:
                inc_turn_outcome("ok")
                await send("state", {"phase": "IDLE", "turn_id": turn_id})
                self._log("info", "TTS disabled; idle", turn_id=turn_id)
                return
            await self._speak(turn_id, fallback, send, cancel)

    _PLACES_TOOL_NAMES = frozenset({"local_places_search", "place_summary", "place_details"})

    def _expand_context(self) -> str:
        """Determine TTS abbreviation expansion groups based on tools used this turn."""
        base = str(settings.TTS_EXPAND or "general").strip()
        if self._last_used_tools & self._PLACES_TOOL_NAMES:
            if "address" not in base:
                return f"{base},address" if base else "address"
        return base

    async def _speak(self, turn_id: str, text: str, send: SendEvent, cancel: CancelToken) -> None:
        tts_text = _sanitize_tts_text(text)
        if not tts_text:
            tts_text = text.strip()
        expand = self._expand_context()
        self._log("info", "speak start", turn_id=turn_id, chars=len(tts_text), expand=expand)

        start = time.monotonic()
        sr = 0
        chunks = 0
        bytes_total = 0
        audio_started = False
        last_sent = False
        outcome = "ok"

        try:
            self._log("info", "TTS speaking", turn_id=turn_id, chars=len(tts_text))
            await send("state", {"phase": "SPEAKING", "turn_id": turn_id})
            sr, audio_iter = await stream_tts(self.tts_base_url, tts_text, expand=expand)
            self._log("info", "TTS streaming", turn_id=turn_id, chars=len(tts_text), sample_rate_hz=sr)

            async for chunk in audio_iter:
                if cancel.cancelled():
                    self._log("warning", "TTS cancelled", turn_id=turn_id)
                    outcome = "cancelled"
                    break
                chunks += 1
                bytes_total += len(chunk or b"")
                if settings.ORCH_LOG_STREAMING and (chunks <= 3 or chunks % 20 == 0):
                    self._log(
                        "debug",
                        "TTS chunk",
                        turn_id=turn_id,
                        idx=chunks,
                        bytes=len(chunk or b""),
                        sample_rate_hz=sr,
                    )
                await send("audio", {"turn_id": turn_id, "sample_rate_hz": sr, "pcm": chunk, "is_last": False})
                audio_started = True

            self._log(
                "info",
                "TTS completed",
                turn_id=turn_id,
                chars=len(tts_text),
                sample_rate_hz=sr,
                chunks=chunks,
                bytes=bytes_total,
            )

            if chunks == 0:
                self._log("warning", "TTS returned no audio", turn_id=turn_id)

            await send("audio", {"turn_id": turn_id, "sample_rate_hz": sr, "pcm": b"", "is_last": True})
            last_sent = True
        except Exception as exc:
            outcome = "tts_error"
            self.log.exception(
                "TTS failed%s",
                format_kv(
                    {"session_id": self._session_id, "turn_id": turn_id},
                    {"error": repr(exc)},
                    enabled=settings.ORCH_LOG_KV,
                ),
            )
            await send("error", {"message": f"TTS failed: {exc!r}"})
        finally:
            observe_tts_seconds(time.monotonic() - start)
            if audio_started and not last_sent:
                await send(
                    "audio",
                    {"turn_id": turn_id, "sample_rate_hz": sr or 22050, "pcm": b"", "is_last": True},
                )
            await send("state", {"phase": "IDLE", "turn_id": turn_id})
            self._log("info", "TTS idle", turn_id=turn_id)
            inc_turn_outcome(outcome)
