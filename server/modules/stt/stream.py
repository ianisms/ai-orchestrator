from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

from stt.parakeet_grpc import ParakeetGrpcClient, ParakeetConfig
from config.settings import settings
from common.logging import format_kv


class CancelToken:
    def __init__(self, event: asyncio.Event):
        self.event = event

    def cancelled(self) -> bool:
        return self.event.is_set()


class STTStream:
    def __init__(
        self,
        grpc_target: str,
        logger,
        *,
        grpc_timeout_s: Optional[float] = None,
        req_timeout_s: Optional[float] = None,
    ):
        timeout = settings.STT_GRPC_TIMEOUT_S if grpc_timeout_s is None else grpc_timeout_s
        req_timeout = settings.STT_REQ_TIMEOUT_S if req_timeout_s is None else req_timeout_s
        self._client = ParakeetGrpcClient(grpc_target, timeout_s=float(timeout), req_timeout_s=float(req_timeout))
        self.log = logger

    async def stream(
        self,
        audio_frames: "asyncio.Queue[bytes | None]",
        *,
        sample_rate_hz: int,
        request_partials: bool,
        language_code: str = "en-US",
        cancel: Optional[CancelToken] = None,
        turn_id: str = "",
        session_id: str = "",
    ) -> AsyncIterator[tuple[str, bool]]:
        """
        audio_frames: queue of raw PCM16LE mono chunks
        sentinel None => end-of-utterance (closes request stream)
        Yields: (text, is_final)
        """
        cancel_event = cancel.event if cancel is not None else asyncio.Event()

        self.log.info(
            "STT stream start sr=%s partials=%s lang=%s%s",
            sample_rate_hz,
            request_partials,
            language_code,
            format_kv(
                {"session_id": session_id, "turn_id": turn_id},
                {},
                enabled=settings.ORCH_LOG_KV,
            ),
        )

        cfg = ParakeetConfig(
            sample_rate_hz=sample_rate_hz,
            language_code=language_code,
            interim_results=request_partials,
        )

        last_text = ""
        saw_final = False

        async for text, is_final in self._client.stream_recognize(
            audio_frames, cfg, cancel_event=cancel_event
        ):
            if text and text.strip():
                last_text = text.strip()
            if is_final:
                saw_final = True
            yield text, is_final

        # If the upstream never marked final, force one on stream end
        if last_text and not saw_final:
            self.log.info(
                "STT forcing FINAL on stream end: %r%s",
                last_text,
                format_kv(
                    {"session_id": session_id, "turn_id": turn_id},
                    {},
                    enabled=settings.ORCH_LOG_KV,
                ),
            )
            yield last_text, True

        self.log.info(
            "STT stream end (saw_final=%s last=%r)%s",
            saw_final,
            last_text,
            format_kv(
                {"session_id": session_id, "turn_id": turn_id},
                {},
                enabled=settings.ORCH_LOG_KV,
            ),
        )

    async def close(self) -> None:
        await self._client.close()
