from __future__ import annotations

import asyncio
from typing import Optional

from common.logging import format_kv
from config.settings import settings
from stt.stream import STTStream

from .history import ChatHistory
from .types import SendEvent
from .utterance import UtteranceRunner


class OrchestratorSession:
    def __init__(
        self,
        stt: STTStream,
        llm_base_url: str,
        llm_model: str,
        tts_base_url: str,
        batch_max_chars: int,
        batch_max_ms: int,
        logger,
        history: ChatHistory | None = None,
    ):
        self.stt = stt
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.tts_base_url = tts_base_url
        self.batch_max_chars = batch_max_chars
        self.batch_max_ms = batch_max_ms
        self.log = logger
        self._session_id = ""
        self._client_key = ""
        self._history = history or ChatHistory(settings.LLM_HISTORY_TOKENS)

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id or ""

    def set_client_key(self, client_key: str) -> None:
        self._client_key = client_key or ""

    def reset_context(self) -> None:
        self._history.reset()

    def set_history(self, history: ChatHistory) -> None:
        self._history = history

    def _log(self, level: str, msg: str, *, turn_id: str = "", **optional) -> None:
        trace_id = f"{self._session_id}:{turn_id}" if self._session_id and turn_id else (turn_id or self._session_id)
        suffix = format_kv(
            {"session_id": self._session_id, "turn_id": turn_id, "trace_id": trace_id},
            optional,
            enabled=settings.ORCH_LOG_KV,
        )
        getattr(self.log, level)(f"{msg}{suffix}")

    async def run_session(
        self,
        client_in_q: "asyncio.Queue[bytes | None]",
        send: SendEvent,
        *,
        sample_rate_hz: int,
        request_partials: bool,
        language_code: str,
        eou_event: asyncio.Event,
        text_in_q: Optional["asyncio.Queue[str]"] = None,
        no_tts: bool = False,
        client_done: Optional[asyncio.Event] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> None:
        while True:
            runner = UtteranceRunner(
                stt=self.stt,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
                tts_base_url=self.tts_base_url,
                logger=self.log,
                log_fn=self._log,
                session_id=self._session_id,
                client_key=self._client_key,
                history=self._history,
            )
            await runner.run(
                client_in_q,
                send,
                sample_rate_hz=sample_rate_hz,
                request_partials=request_partials,
                language_code=language_code,
                eou_event=eou_event,
                text_in_q=text_in_q,
                no_tts=no_tts,
                interrupt_event=interrupt_event,
            )

            eou_event.clear()
            if client_done is not None and client_done.is_set() and client_in_q.empty():
                self._log("info", "client done; ending session")
                break


OrchestratorPipeline = OrchestratorSession
