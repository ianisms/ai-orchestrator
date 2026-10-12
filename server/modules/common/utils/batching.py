import time

class TextBatcher:
    """Transport batching only. TTS owns prosody chunking."""

    def __init__(self, max_chars: int = 180, max_ms: int = 300):
        self._buf: list[str] = []
        self._max_chars = max_chars
        self._max_ms = max_ms
        self._last = time.monotonic()

    def push(self, s: str) -> str | None:
        self._buf.append(s)
        now = time.monotonic()
        joined = "".join(self._buf)
        if len(joined) >= self._max_chars or (now - self._last) * 1000 >= self._max_ms:
            self._buf.clear()
            self._last = now
            return joined
        return None

    def flush(self) -> str | None:
        if not self._buf:
            return None
        joined = "".join(self._buf)
        self._buf.clear()
        self._last = time.monotonic()
        return joined
