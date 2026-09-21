import json
import logging
import re
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from config.settings import settings
from common.logging import get_logger

class LLMStreamError(RuntimeError):
    pass


_PROMPT_CACHE: dict[str, object] = {"path": None, "mtime": None, "text": "", "tools_version": None}
_LOG = get_logger("llm.prompt")
_REQ_LOG = get_logger("llm.request")
_PAYLOAD_LOG = get_logger("llm.payload")
VERBOSE_LEVEL = 15
logging.addLevelName(VERBOSE_LEVEL, "VERBOSE")
_AUTO_MODEL_CACHE: str | None = None
_HTTP_CLIENT: httpx.AsyncClient | None = None


def _is_verbose() -> bool:
    return str(settings.LOG_LEVEL).strip().upper() == "VERBOSE"


def _log_json(label: str, data: object) -> None:
    if not _is_verbose():
        return
    try:
        dumped = json.dumps(data, ensure_ascii=True)
    except TypeError:
        dumped = str(data)
    _PAYLOAD_LOG.log(VERBOSE_LEVEL, "%s %s", label, dumped)


def _format_tool_descriptions() -> str:
    try:
        from tools.registry import list_tools
    except Exception:
        return ""

    lines = []
    try:
        max_tools = int(settings.LLM_TOOL_DESCRIPTIONS_MAX or 0)
    except Exception:
        max_tools = 0
    if max_tools == 0:
        return ""
    for tool in list_tools():
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        name = fn.get("name") if fn else tool.get("name")
        desc = fn.get("description") if fn else tool.get("description")
        params = fn.get("parameters") if fn else None
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(desc, str) or not desc:
            desc = "No description."
        param_names = []
        if isinstance(params, dict):
            props = params.get("properties")
            if isinstance(props, dict):
                param_names = [str(k) for k in props.keys()]
        if param_names:
            lines.append(f"- {name}({', '.join(param_names)}): {desc}")
        else:
            lines.append(f"- {name}: {desc}")
        if max_tools > 0 and len(lines) >= max_tools:
            break
    return "\n".join(lines)


def _load_system_prompt() -> str:
    path = settings.LLM_PROMPT_PATH
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return ""

    mtime = stat.st_mtime
    try:
        from tools.registry import get_tools_version
    except Exception:
        tools_version = None
    else:
        tools_version = get_tools_version()

    if (
        _PROMPT_CACHE["path"] == path
        and _PROMPT_CACHE["mtime"] == mtime
        and _PROMPT_CACHE["tools_version"] == tools_version
    ):
        return str(_PROMPT_CACHE["text"] or "")

    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    if "{TOOL DESCRIPTIONS}" in text:
        desc = _format_tool_descriptions() or "None."
        text = text.replace("{TOOL DESCRIPTIONS}", desc)
    _PROMPT_CACHE["path"] = path
    _PROMPT_CACHE["mtime"] = mtime
    _PROMPT_CACHE["text"] = text
    _PROMPT_CACHE["tools_version"] = tools_version
    if text:
        _LOG.info("LLM system prompt loaded path=%s chars=%d", path, len(text))
    else:
        _LOG.warning("LLM system prompt empty path=%s", path)
    return text


def get_system_prompt() -> str:
    return _load_system_prompt()


def _sse_data(line: str) -> Optional[str]:
    """
    Extract SSE data payload from a single line.
    We ignore comments (': ...') and other SSE fields (event:, id:, retry:).
    """
    if not line:
        return None
    if line.startswith(":"):
        return None
    if line.startswith("data:"):
        return line[5:].lstrip()
    return None


def _log_http_error(logger, *, status_code: int, url: str, body: bytes) -> None:
    if not logger:
        logger = _REQ_LOG
    text = body.decode("utf-8", errors="replace") if body else ""
    clipped = text[:2000] if text else ""
    logger.warning("LLM HTTP error status=%s url=%s body=%s", status_code, url, clipped)


def _is_context_overflow(body: bytes) -> bool:
    if not body:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    return "max_tokens must be at least 1" in text or "context length" in text


def _strip_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return []
    return [msg for msg in messages if msg.get("role") != "system"]


_CONTEXT_LEN_RE = re.compile(
    r"maximum context length is\s*(\d+)\s*tokens.*?has\s*(\d+)\s*input tokens",
    re.IGNORECASE | re.DOTALL,
)


def _estimate_message_tokens(msg: dict[str, Any]) -> int:
    content = msg.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    return max(1, (len(content) + 3) // 4) + 4


def _total_message_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_message_tokens(m) for m in messages if isinstance(m, dict))


def _parse_context_limits(body: bytes) -> tuple[int, int] | None:
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    match = _CONTEXT_LEN_RE.search(text)
    if not match:
        return None
    try:
        max_ctx = int(match.group(1))
        got_ctx = int(match.group(2))
    except Exception:
        return None
    if max_ctx <= 0 or got_ctx <= 0:
        return None
    return max_ctx, got_ctx


def _prune_messages_for_context(messages: list[dict[str, Any]], body: bytes) -> list[dict[str, Any]]:
    if not messages:
        return []

    preserved: list[dict[str, Any]] = []
    remainder: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if idx == 0 and msg.get("role") == "system":
            preserved.append(dict(msg))
            continue
        remainder.append(dict(msg))

    if not remainder:
        return preserved

    limits = _parse_context_limits(body)
    if limits:
        target_tokens = max(128, int(limits[0] * 0.85))
    else:
        target_tokens = max(128, _total_message_tokens(messages) // 2)

    budget = max(64, target_tokens - _total_message_tokens(preserved))

    kept_rev: list[dict[str, Any]] = []
    used = 0
    for msg in reversed(remainder):
        t = _estimate_message_tokens(msg)
        if kept_rev and used + t > budget:
            break
        kept_rev.append(msg)
        used += t
        if used >= budget:
            break

    kept = list(reversed(kept_rev))

    # Always keep the latest user/tool context if possible.
    last_msg = remainder[-1]
    if not kept or kept[-1] != last_msg:
        while kept and (_total_message_tokens(kept) + _estimate_message_tokens(last_msg) > budget):
            kept.pop(0)
        if _estimate_message_tokens(last_msg) <= budget:
            kept.append(last_msg)

    if not kept:
        kept = [last_msg]
    return preserved + kept


def _should_send_max_tokens(model: Optional[str]) -> bool:
    try:
        max_tokens = int(settings.LLM_MAX_TOKENS)
    except Exception:
        return False
    if max_tokens <= 0:
        return False
    return True


def _use_json_response_format(*, tools: Optional[list[dict[str, Any]]] = None) -> bool:
    if not bool(getattr(settings, "LLM_RESPONSE_FORMAT_JSON", False)):
        return False
    return bool(tools)


def _is_model_not_found(body: bytes) -> bool:
    if not body:
        return False
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return False
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return False
    err_type = str(error.get("type") or "").lower()
    message = str(error.get("message") or "").lower()
    if "notfounderror" in err_type or "not found" in message or "does not exist" in message:
        return True
    return False


async def _auto_detect_model(base_url: str) -> str:
    url = base_url.rstrip("/") + "/v1/models"
    try:
        client = _get_http_client()
        resp = await client.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data") if isinstance(data, dict) else None
        if isinstance(models, list):
            for item in models:
                model_id = item.get("id") if isinstance(item, dict) else None
                if isinstance(model_id, str) and model_id.strip():
                    return model_id.strip()
    except Exception:
        return ""
    return ""


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        _HTTP_CLIENT = httpx.AsyncClient(timeout=None, limits=limits)
    return _HTTP_CLIENT


async def close_llm_client() -> None:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        return
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if not client.is_closed:
        await client.aclose()


async def _resolve_model(base_url: str, model: str) -> str:
    global _AUTO_MODEL_CACHE
    if str(settings.LLM_MODEL or "").strip():
        return model
    if _AUTO_MODEL_CACHE:
        return _AUTO_MODEL_CACHE
    if model:
        return model
    detected = await _auto_detect_model(base_url)
    if detected:
        _AUTO_MODEL_CACHE = detected
        return detected
    return model


async def stream_chat_completions(
    base_url: str,
    model: str,
    user_text: str,
    cancel,
    temperature: float = 0.0,
    top_p: float = 1.0,
    messages: Optional[list[dict[str, str]]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[str] = None,
    emit_tool_calls_as_content: bool = False,
    *,
    logger=None,
) -> AsyncIterator[str]:
    """
    OpenAI-compatible /v1/chat/completions streaming parser (SSE).

    Guarantees:
      - Yields ONLY assistant natural-language tokens from choices[0].delta.content
      - Ignores reasoning/reasoning_content and empty deltas
      - Stops cleanly on [DONE] or finish_reason
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    model = await _resolve_model(base_url, model)
    if messages is None:
        system_prompt = _load_system_prompt()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})
    else:
        messages = [dict(m) for m in messages]
        system_prompt = _load_system_prompt()
        if system_prompt and (not messages or messages[0].get("role") != "system"):
            messages.insert(0, {"role": "system", "content": system_prompt})

    payload = {
        "model": model,
        "stream": True,
        "temperature": temperature,
        "top_p": top_p,
        "messages": messages,
    }
    if _should_send_max_tokens(model):
        payload["max_tokens"] = int(settings.LLM_MAX_TOKENS)
    if _use_json_response_format(tools=tools):
        payload["response_format"] = {"type": "json_object"}
    if tools:
        if logger:
            tool_names = []
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
                name = fn.get("name") if fn else tool.get("name")
                if isinstance(name, str) and name:
                    tool_names.append(name)
            if tool_names:
                logger.debug("LLM tools sent count=%d names=%s", len(tool_names), ",".join(tool_names))
            else:
                logger.debug("LLM tools sent count=%d", len(tools))
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    def cancelled() -> bool:
        return bool(cancel) and hasattr(cancel, "cancelled") and cancel.cancelled()

    headers = {"Accept": "text/event-stream"}
    _log_json("LLM stream request", payload)

    client = _get_http_client()
    payload_no_tools = None
    if tools:
        payload_no_tools = dict(payload)
        payload_no_tools.pop("tools", None)
        payload_no_tools.pop("tool_choice", None)
        payload_no_tools.pop("response_format", None)

    async def _stream_lines(payload_obj: dict[str, Any]) -> AsyncIterator[str]:
        attempted_model_refresh = False
        attempted_strip_system = False
        attempted_prune_messages = False
        _max_retries = 5
        _retries = 0
        while True:
            if _retries >= _max_retries:
                _REQ_LOG.error("LLM stream exceeded max retries (%d)", _max_retries)
                raise httpx.HTTPStatusError(
                    f"LLM request failed after {_max_retries} retries",
                    request=httpx.Request("POST", url),
                    response=httpx.Response(500),
                )
            async with client.stream("POST", url, json=payload_obj, headers=headers) as r:
                if r.status_code >= 400:
                    body = await r.aread()
                    _retries += 1
                    if "max_tokens" in payload_obj:
                        try:
                            payload_obj = dict(payload_obj)
                            payload_obj.pop("max_tokens", None)
                            _REQ_LOG.warning("LLM stream retrying without max_tokens")
                            attempted_model_refresh = False
                            continue
                        except Exception:
                            pass
                    if "response_format" in payload_obj:
                        try:
                            payload_obj = dict(payload_obj)
                            payload_obj.pop("response_format", None)
                            _REQ_LOG.warning("LLM stream retrying without response_format")
                            attempted_model_refresh = False
                            continue
                        except Exception:
                            pass
                    if _is_context_overflow(body) and not attempted_strip_system:
                        stripped = _strip_system_messages(payload_obj.get("messages") or [])
                        if stripped and stripped != payload_obj.get("messages"):
                            payload_obj = dict(payload_obj)
                            payload_obj["messages"] = stripped
                            attempted_strip_system = True
                            _REQ_LOG.warning("LLM stream retrying without system prompt")
                            continue
                    if _is_context_overflow(body) and not attempted_prune_messages:
                        pruned = _prune_messages_for_context(payload_obj.get("messages") or [], body)
                        if pruned and pruned != payload_obj.get("messages"):
                            payload_obj = dict(payload_obj)
                            payload_obj["messages"] = pruned
                            attempted_prune_messages = True
                            _REQ_LOG.warning("LLM stream retrying with pruned message history")
                            continue
                    if (
                        r.status_code == 404
                        and not attempted_model_refresh
                        and not str(settings.LLM_MODEL or "").strip()
                        and _is_model_not_found(body)
                    ):
                        new_model = await _auto_detect_model(base_url)
                        if new_model and new_model != payload_obj.get("model"):
                            global _AUTO_MODEL_CACHE
                            _AUTO_MODEL_CACHE = new_model
                            payload_obj = dict(payload_obj)
                            payload_obj["model"] = new_model
                            attempted_model_refresh = True
                            _REQ_LOG.info("LLM model refreshed to %s after 404", new_model)
                            continue
                    _log_http_error(logger, status_code=r.status_code, url=str(r.request.url), body=body)
                    r.raise_for_status()
                async for line in r.aiter_lines():
                    yield line
            break

    async def _process_lines(line_iter: AsyncIterator[str]) -> AsyncIterator[str]:
        saw_content = False
        saw_tool_calls = False
        tool_calls_acc: dict[int, dict[str, str]] = {}

        def _coerce_text(value: Any) -> str:
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                parts: list[str] = []
                for item in value:
                    if isinstance(item, str):
                        parts.append(item)
                        continue
                    if not isinstance(item, dict):
                        continue
                    # OpenAI-style rich content parts.
                    if item.get("type") in ("text", "output_text"):
                        txt = item.get("text")
                        if isinstance(txt, str) and txt:
                            parts.append(txt)
                return "".join(parts)
            return ""

        def _flush_tool_calls_as_text() -> str:
            if not tool_calls_acc:
                return ""
            blocks: list[str] = []
            for idx in sorted(tool_calls_acc.keys()):
                entry = tool_calls_acc.get(idx) or {}
                name = (entry.get("name") or "").strip()
                if not name:
                    continue
                args_text = entry.get("arguments") or ""
                args_obj: dict[str, Any] = {}
                if isinstance(args_text, str) and args_text.strip():
                    try:
                        parsed = json.loads(args_text)
                        if isinstance(parsed, dict):
                            args_obj = parsed
                    except Exception:
                        args_obj = {}
                payload = {"name": name, "arguments": args_obj}
                blocks.append(json.dumps(payload, ensure_ascii=True))
            if not blocks:
                return ""
            if len(blocks) == 1:
                return blocks[0]
            return "[" + ",".join(blocks) + "]"

        async for line in line_iter:
            if cancelled():
                return

            data = _sse_data(line)
            if data is None:
                continue

            if data == "[DONE]":
                if emit_tool_calls_as_content and saw_tool_calls:
                    tool_text = _flush_tool_calls_as_text()
                    if tool_text:
                        yield tool_text
                return

            if not data:
                continue

            try:
                obj = json.loads(data)
            except json.JSONDecodeError as e:
                raise LLMStreamError(f"Invalid JSON in SSE data: {data[:200]}") from e
            _log_json("LLM stream response", obj)

            choices = obj.get("choices") or []
            if not choices:
                continue

            c0 = choices[0] or {}

            # Some servers emit finish_reason on a final JSON chunk (in addition to or instead of [DONE]).
            if c0.get("finish_reason"):
                if emit_tool_calls_as_content and saw_tool_calls:
                    tool_text = _flush_tool_calls_as_text()
                    if tool_text:
                        yield tool_text
                return

            delta = c0.get("delta") or {}
            if not isinstance(delta, dict):
                continue

            if "tool_calls" in delta or "function_call" in delta:
                saw_tool_calls = True
                if emit_tool_calls_as_content:
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for i, call in enumerate(tool_calls):
                            if not isinstance(call, dict):
                                continue
                            idx_raw = call.get("index")
                            idx = idx_raw if isinstance(idx_raw, int) and idx_raw >= 0 else i
                            entry = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            call_id = call.get("id")
                            if isinstance(call_id, str) and call_id:
                                entry["id"] = call_id
                            fn = call.get("function")
                            if isinstance(fn, dict):
                                name = fn.get("name")
                                if isinstance(name, str) and name:
                                    entry["name"] = name
                                args = fn.get("arguments")
                                if isinstance(args, str) and args:
                                    entry["arguments"] = (entry.get("arguments") or "") + args
                    fn_call = delta.get("function_call")
                    if isinstance(fn_call, dict):
                        entry = tool_calls_acc.setdefault(0, {"id": "", "name": "", "arguments": ""})
                        name = fn_call.get("name")
                        if isinstance(name, str) and name:
                            entry["name"] = name
                        args = fn_call.get("arguments")
                        if isinstance(args, str) and args:
                            entry["arguments"] = (entry.get("arguments") or "") + args
                if logger:
                    tool_names: list[str] = []
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for call in tool_calls:
                            fn = call.get("function") if isinstance(call, dict) else None
                            name = fn.get("name") if isinstance(fn, dict) else None
                            if isinstance(name, str) and name:
                                tool_names.append(name)
                    fn_call = delta.get("function_call")
                    if isinstance(fn_call, dict):
                        name = fn_call.get("name")
                        if isinstance(name, str) and name:
                            tool_names.append(name)
                    if tool_names:
                        logger.debug("LLM tool call delta names=%s", ",".join(tool_names))
                    else:
                        logger.debug("LLM tool call delta received")
                continue

            # Speak ONLY assistant content.
            txt = _coerce_text(delta.get("content"))
            if not txt:
                # Some backends send text in alternate delta fields.
                txt = _coerce_text(delta.get("text"))
            if not txt:
                txt = _coerce_text(c0.get("text"))
            if not txt:
                continue

            saw_content = True
            yield txt

        # Some servers close SSE without explicit [DONE]/finish_reason.
        if emit_tool_calls_as_content and saw_tool_calls:
            tool_text = _flush_tool_calls_as_text()
            if tool_text:
                yield tool_text

    try:
        async for out in _process_lines(_stream_lines(payload)):
            yield out
        return
    except httpx.HTTPStatusError:
        if payload_no_tools is None:
            raise
        if logger:
            logger.warning("LLM stream retrying without tools")
        _log_json("LLM stream request (retry no tools)", payload_no_tools)
        async for out in _process_lines(_stream_lines(payload_no_tools)):
            yield out
        return


async def chat_completion(
    base_url: str,
    model: str,
    user_text: str,
    temperature: float = 0.0,
    top_p: float = 1.0,
    messages: Optional[list[dict[str, str]]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[str] = None,
) -> str:
    """
    Non-streaming /v1/chat/completions for fallback when streaming yields nothing.
    Returns assistant content text, or raises LLMStreamError.
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    model = await _resolve_model(base_url, model)
    if messages is None:
        system_prompt = _load_system_prompt()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})
    else:
        messages = [dict(m) for m in messages]
        system_prompt = _load_system_prompt()
        if system_prompt and (not messages or messages[0].get("role") != "system"):
            messages.insert(0, {"role": "system", "content": system_prompt})

    payload = {
        "model": model,
        "stream": False,
        "temperature": temperature,
        "top_p": top_p,
        "messages": messages,
    }
    if _should_send_max_tokens(model):
        payload["max_tokens"] = int(settings.LLM_MAX_TOKENS)
    if _use_json_response_format(tools=tools):
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    headers = {"Accept": "application/json"}
    _log_json("LLM request", payload)

    client = _get_http_client()
    attempted_strip_system = False
    attempted_prune_messages = False
    attempted_model_refresh = False
    while True:
        r = await client.post(url, json=payload, headers=headers, timeout=30.0)
        if r.status_code < 400:
            break

        body = r.content or b""
        if "max_tokens" in payload:
            payload = dict(payload)
            payload.pop("max_tokens", None)
            _REQ_LOG.warning("LLM completion retrying without max_tokens")
            continue
        if "response_format" in payload:
            payload = dict(payload)
            payload.pop("response_format", None)
            _REQ_LOG.warning("LLM completion retrying without response_format")
            continue
        if _is_context_overflow(body) and not attempted_strip_system:
            stripped = _strip_system_messages(payload.get("messages") or [])
            if stripped and stripped != payload.get("messages"):
                payload = dict(payload)
                payload["messages"] = stripped
                attempted_strip_system = True
                _REQ_LOG.warning("LLM completion retrying without system prompt")
                continue
        if _is_context_overflow(body) and not attempted_prune_messages:
            pruned = _prune_messages_for_context(payload.get("messages") or [], body)
            if pruned and pruned != payload.get("messages"):
                payload = dict(payload)
                payload["messages"] = pruned
                attempted_prune_messages = True
                _REQ_LOG.warning("LLM completion retrying with pruned message history")
                continue
        if (
            r.status_code == 404
            and not attempted_model_refresh
            and not str(settings.LLM_MODEL or "").strip()
            and _is_model_not_found(body)
        ):
            new_model = await _auto_detect_model(base_url)
            if new_model and new_model != payload.get("model"):
                global _AUTO_MODEL_CACHE
                _AUTO_MODEL_CACHE = new_model
                payload = dict(payload)
                payload["model"] = new_model
                attempted_model_refresh = True
                _REQ_LOG.info("LLM model refreshed to %s after 404", new_model)
                continue
        break

    if r.status_code >= 400:
        _log_http_error(logger=None, status_code=r.status_code, url=str(r.request.url), body=r.content)
        if tools:
            _REQ_LOG.warning("LLM completion retrying without tools")
            payload_no_tools = dict(payload)
            payload_no_tools.pop("tools", None)
            payload_no_tools.pop("tool_choice", None)
            payload_no_tools.pop("response_format", None)
            _log_json("LLM request (retry no tools)", payload_no_tools)
            r = await client.post(url, json=payload_no_tools, headers=headers, timeout=30.0)
            if r.status_code >= 400:
                _log_http_error(logger=None, status_code=r.status_code, url=str(r.request.url), body=r.content)
                r.raise_for_status()
        else:
            r.raise_for_status()
    obj = r.json()
    _log_json("LLM response", obj)

    choices = obj.get("choices") or []
    if not choices:
        raise LLMStreamError("No choices in completion response")

    c0 = choices[0] or {}
    message = c0.get("message") or {}
    text = message.get("content") if isinstance(message, dict) else None
    if not text:
        text = c0.get("text")

    if not isinstance(text, str) or not text.strip():
        raise LLMStreamError("No content in completion response")

    return text


async def chat_completion_raw(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    top_p: float = 1.0,
    tools: Optional[list[dict[str, Any]]] = None,
    tool_choice: Optional[str] = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    model = await _resolve_model(base_url, model)
    messages = [dict(m) for m in messages]
    system_prompt = _load_system_prompt()
    if system_prompt and (not messages or messages[0].get("role") != "system"):
        messages.insert(0, {"role": "system", "content": system_prompt})
    payload = {
        "model": model,
        "stream": False,
        "temperature": temperature,
        "top_p": top_p,
        "messages": messages,
    }
    if _should_send_max_tokens(model):
        payload["max_tokens"] = int(settings.LLM_MAX_TOKENS)
    if _use_json_response_format(tools=tools):
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    headers = {"Accept": "application/json"}
    _log_json("LLM request", payload)

    client = _get_http_client()
    attempted_strip_system = False
    attempted_prune_messages = False
    attempted_model_refresh = False
    while True:
        r = await client.post(url, json=payload, headers=headers, timeout=30.0)
        if r.status_code < 400:
            break

        body = r.content or b""
        if "max_tokens" in payload:
            payload = dict(payload)
            payload.pop("max_tokens", None)
            _REQ_LOG.warning("LLM request retrying without max_tokens")
            continue
        if "response_format" in payload:
            payload = dict(payload)
            payload.pop("response_format", None)
            _REQ_LOG.warning("LLM request retrying without response_format")
            continue
        if _is_context_overflow(body) and not attempted_strip_system:
            stripped = _strip_system_messages(payload.get("messages") or [])
            if stripped and stripped != payload.get("messages"):
                payload = dict(payload)
                payload["messages"] = stripped
                attempted_strip_system = True
                _REQ_LOG.warning("LLM request retrying without system prompt")
                continue
        if _is_context_overflow(body) and not attempted_prune_messages:
            pruned = _prune_messages_for_context(payload.get("messages") or [], body)
            if pruned and pruned != payload.get("messages"):
                payload = dict(payload)
                payload["messages"] = pruned
                attempted_prune_messages = True
                _REQ_LOG.warning("LLM request retrying with pruned message history")
                continue
        if (
            r.status_code == 404
            and not attempted_model_refresh
            and not str(settings.LLM_MODEL or "").strip()
            and _is_model_not_found(body)
        ):
            new_model = await _auto_detect_model(base_url)
            if new_model and new_model != payload.get("model"):
                global _AUTO_MODEL_CACHE
                _AUTO_MODEL_CACHE = new_model
                payload = dict(payload)
                payload["model"] = new_model
                attempted_model_refresh = True
                _REQ_LOG.info("LLM model refreshed to %s after 404", new_model)
                continue
        break

    if r.status_code >= 400:
        _log_http_error(logger=None, status_code=r.status_code, url=str(r.request.url), body=r.content)
        if tools:
            payload_no_tools = dict(payload)
            payload_no_tools.pop("tools", None)
            payload_no_tools.pop("tool_choice", None)
            payload_no_tools.pop("response_format", None)
            _log_json("LLM request (retry no tools)", payload_no_tools)
            r = await client.post(url, json=payload_no_tools, headers=headers, timeout=30.0)
            if r.status_code >= 400:
                _log_http_error(logger=None, status_code=r.status_code, url=str(r.request.url), body=r.content)
                r.raise_for_status()
        else:
            r.raise_for_status()
    obj = r.json()
    _log_json("LLM response", obj)
    return obj
