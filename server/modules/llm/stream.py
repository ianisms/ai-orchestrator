from __future__ import annotations

import asyncio
from typing import AsyncIterator

from llm.llm_client import stream_chat_completions
from config.settings import settings
from common.logging import format_kv


async def stream_llm(
    base_url: str,
    model: str,
    user_text: str,
    cancel,
    *,
    messages: list[dict[str, str]] | None = None,
    tools: list[dict[str, object]] | None = None,
    logger=None,
    turn_id: str = "",
    session_id: str = "",
) -> AsyncIterator[str]:
    """
    Streams LLM deltas. Guarantees:
      - If backend yields nothing, we raise (no non-stream fallback).
      - Won't hang forever (idle timeout).
    """
    idle_timeout_s = float(settings.LLM_IDLE_TIMEOUT_S)
    idle_timeout = idle_timeout_s if idle_timeout_s and idle_timeout_s > 0 else None
    got_any = False
    temperature = float(settings.LLM_TEMPERATURE)
    top_p = float(settings.LLM_TOP_P)

    def _kv(extra: dict[str, object] | None = None) -> str:
        return format_kv(
            {"session_id": session_id, "turn_id": turn_id},
            extra or {},
            enabled=settings.ORCH_LOG_KV,
        )

    async def _aiter():
        async for delta in stream_chat_completions(
            base_url,
            model,
            user_text,
            cancel,
            temperature=temperature,
            top_p=top_p,
            messages=messages,
            tools=tools,
            logger=logger,
        ):
            yield delta

    it = _aiter().__aiter__()

    while True:
        if cancel and cancel.cancelled():
            if logger:
                logger.info("LLM cancelled%s", _kv())
            return

        try:
            if idle_timeout is None:
                delta = await it.__anext__()
            else:
                delta = await asyncio.wait_for(it.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            # LLM is not producing tokens
            if not got_any:
                if logger:
                    logger.error("LLM produced no deltas within %ss%s", idle_timeout_s, _kv())
                raise TimeoutError(f"LLM produced no deltas within {idle_timeout_s}s")
            # If we already have some output, just stop streaming
            if logger:
                logger.warning("LLM stream idle >%ss; stopping%s", idle_timeout_s, _kv())
            break
        except Exception as exc:
            raise

        if not delta:
            continue

        got_any = True
        yield delta

    if not got_any:
        raise RuntimeError("LLM returned zero deltas (nothing to speak)")
