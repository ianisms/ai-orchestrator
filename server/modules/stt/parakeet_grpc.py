from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import grpc

from stt.protos import riva_asr_pb2_grpc as rasr_grpc
from stt.protos import riva_asr_pb2 as rasr_pb2


@dataclass
class ParakeetConfig:
    sample_rate_hz: int = 16000
    language_code: str = "en-US"
    interim_results: bool = True


class ParakeetGrpcClient:
    def __init__(self, target: str, *, timeout_s: float = 90.0, req_timeout_s: float = 0.25):
        self._target = target
        self._timeout_s = timeout_s
        self._req_timeout_s = req_timeout_s
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[rasr_grpc.RivaSpeechRecognitionStub] = None

    async def _get_stub(self) -> rasr_grpc.RivaSpeechRecognitionStub:
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self._target)
            self._stub = rasr_grpc.RivaSpeechRecognitionStub(self._channel)
        return self._stub

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._stub = None

    @staticmethod
    def _linear_pcm_enum() -> int:
        enc = rasr_pb2.RecognitionConfig.AudioEncoding
        if hasattr(enc, "ENCODING_LINEAR_PCM"):
            return enc.ENCODING_LINEAR_PCM
        if hasattr(enc, "LINEAR_PCM"):
            return enc.LINEAR_PCM
        return enc.ENCODING_UNSPECIFIED

    async def stream_recognize(
        self,
        audio_frames: "asyncio.Queue[bytes | None]",
        cfg: ParakeetConfig,
        *,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[tuple[str, bool]]:
        stub = await self._get_stub()

        async def req_iter() -> AsyncIterator[rasr_pb2.StreamingRecognizeRequest]:
            rcfg = rasr_pb2.RecognitionConfig(
                encoding=self._linear_pcm_enum(),
                sample_rate_hertz=cfg.sample_rate_hz,
                language_code=cfg.language_code,
            )
            yield rasr_pb2.StreamingRecognizeRequest(
                streaming_config=rasr_pb2.StreamingRecognitionConfig(
                    config=rcfg,
                    interim_results=cfg.interim_results,
                )
            )

            while not cancel_event.is_set():
                timeout_s = self._req_timeout_s
                get_task = asyncio.create_task(audio_frames.get(), name="stt_audio_get")
                cancel_task = asyncio.create_task(cancel_event.wait(), name="stt_cancel_wait")
                done, pending = await asyncio.wait(
                    {get_task, cancel_task},
                    timeout=timeout_s if timeout_s and timeout_s > 0 else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if cancel_task in done:
                    await asyncio.gather(get_task, return_exceptions=True)
                    break
                if get_task not in done:
                    await asyncio.gather(get_task, return_exceptions=True)
                    continue
                chunk = get_task.result()

                # EOU sentinel: CLOSE stream
                if chunk is None:
                    break

                if chunk:
                    yield rasr_pb2.StreamingRecognizeRequest(audio_content=chunk)

        call = stub.StreamingRecognize(req_iter(), timeout=self._timeout_s)

        async for resp in call:
            for r in resp.results:
                if not r.alternatives:
                    continue
                txt = (r.alternatives[0].transcript or "").strip()
                if not txt:
                    continue
                is_final = bool(getattr(r, "is_final", False))
                yield txt, is_final
