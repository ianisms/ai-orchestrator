"""AudioTapQueue — transparent asyncio.Queue wrapper that records turn audio.

The STT client only calls ``await audio_frames.get()``, so this class only
needs to implement that one method (plus minimal compatibility shims).

After STT produces its final transcript the orchestrator calls
``write_wav(out_dir, session_id)`` to flush the accumulated PCM frames into a
WAV file.  The returned ``(session_id, audio_id)`` tuple is injected into the
LLM context as ``[AUDIO_REF ...]`` so that the ``get_speaker_id`` tool can
locate the audio without the audio ever passing through the MCP protocol.

Container path note
-------------------
Orchestrator writes to:  /ai/server/state/speaker_audio/
tools_server reads from: /app/state/speaker_audio/
(same physical directory via Docker volume mount /ai/server/state:/app/state)
"""

from __future__ import annotations

import asyncio
import io
import uuid
import wave
from pathlib import Path
from typing import Optional

# Minimum raw PCM bytes to consider the clip worth saving (~160 ms @ 16 kHz / 16-bit mono)
_MIN_FRAME_BYTES = 5120

CLIENT_AUDIO_DIR = Path("/ai/server/state/client_audio")
CLIENT_AUDIO_TTL_S = 300  # 5-minute temp file lifetime


class AudioTapQueue:
    """Wraps an ``asyncio.Queue[bytes | None]`` and records every non-None
    frame so a WAV file can be written once STT has finished.

    Only ``get()`` is forwarded because that is the sole method the Parakeet
    gRPC client calls on the queue.  All other queue operations are delegated
    transparently so the object behaves as a drop-in replacement.
    """

    __slots__ = ("_source", "_sample_rate", "_frames")

    def __init__(
        self,
        source: "asyncio.Queue[Optional[bytes]]",
        *,
        sample_rate_hz: int = 16000,
    ) -> None:
        self._source = source
        self._sample_rate = sample_rate_hz
        self._frames: list[bytes] = []

    # ------------------------------------------------------------------ #
    # asyncio.Queue interface — only get() is called by the STT client    #
    # ------------------------------------------------------------------ #

    async def get(self) -> Optional[bytes]:
        frame = await self._source.get()
        if frame is not None:
            self._frames.append(frame)
        return frame

    def get_nowait(self) -> Optional[bytes]:
        frame = self._source.get_nowait()
        if frame is not None:
            self._frames.append(frame)
        return frame

    def empty(self) -> bool:
        return self._source.empty()

    def qsize(self) -> int:
        return self._source.qsize()

    @property
    def maxsize(self) -> int:
        return self._source.maxsize

    def task_done(self) -> None:
        self._source.task_done()

    # ------------------------------------------------------------------ #
    # Audio capture                                                        #
    # ------------------------------------------------------------------ #

    def write_wav(
        self,
        out_dir: Path,
        session_id: str,
    ) -> Optional[tuple[str, str]]:
        """Encode accumulated PCM16LE frames as a WAV file and return the
        ``(session_id, audio_id)`` reference tuple, or ``None`` when there is
        too little audio to be useful.
        """
        raw = b"".join(self._frames)
        if len(raw) < _MIN_FRAME_BYTES:
            return None

        audio_id = uuid.uuid4().hex[:16]
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{session_id}_{audio_id}.wav"
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)   # PCM16LE
                wf.setframerate(self._sample_rate)
                wf.writeframes(raw)
            path.write_bytes(buf.getvalue())
        except Exception:
            return None

        return (session_id, audio_id)


def cleanup_stale_audio(out_dir: Path, ttl_s: float = float(CLIENT_AUDIO_TTL_S)) -> None:
    """Delete WAV files older than *ttl_s* seconds.  Called opportunistically;
    errors are silently ignored.
    """
    import time
    if not out_dir.is_dir():
        return
    cutoff = time.time() - ttl_s
    try:
        for p in out_dir.glob("*.wav"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
