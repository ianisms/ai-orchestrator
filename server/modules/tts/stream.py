from __future__ import annotations

from typing import AsyncIterator, Tuple
from urllib.parse import urlencode, quote

import httpx

from config.settings import settings

_MOOD_PRESETS = {
    "neutral": {
        "pitch_semitones": 0.0,
        "speaking_rate": 1.0,
        "sentence_pause_ms": 100,
        "highpass_hz": 0.0,
        "target_peak": 0.55,
        "max_gain": 3.5,
    },
    "upbeat": {
        "pitch_semitones": 0.8,
        "speaking_rate": 1.08,
        "sentence_pause_ms": 60,
        "highpass_hz": 100.0,
        "target_peak": 0.65,
        "max_gain": 4.0,
    },
    "warm": {
        "pitch_semitones": 0.2,
        "speaking_rate": 1.02,
        "sentence_pause_ms": 70,
        "highpass_hz": 60.0,
        "target_peak": 0.6,
        "max_gain": 3.8,
    },
    "warm": {
        "pitch_semitones": 0.4,
        "speaking_rate": 1.04,
        "sentence_pause_ms": 80,
        "highpass_hz": 80.0,
        "target_peak": 0.6,
        "max_gain": 3.8,
    },
}

_HTTP_CLIENT: httpx.AsyncClient | None = None

def _load_mood_presets() -> dict:
    presets = {k: v.copy() for k, v in _MOOD_PRESETS.items()}
    overrides = settings.TTS_MOODS
    if isinstance(overrides, dict):
        for name, values in overrides.items():
            if not isinstance(values, dict):
                continue
            key = str(name).strip().lower()
            base = presets.get(key, {}).copy()
            base.update(values)
            presets[key] = base
    return presets

def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=50)
        _HTTP_CLIENT = httpx.AsyncClient(timeout=None, limits=limits)
    return _HTTP_CLIENT

async def close_tts_client() -> None:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        return
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if not client.is_closed:
        await client.aclose()

async def stream_tts(tts_base_url: str, text: str, *, expand: str = "") -> Tuple[int, AsyncIterator[bytes]]:
    """
    Calls Custom-voice TTS:
      GET {tts_base_url}/api/tts/stream?say=...  (streams raw PCM16LE)
    Returns: (sample_rate_hz, async iterator of PCM bytes)

    expand: abbreviation group names (e.g. "general", "general,address").
            Empty string uses settings.TTS_EXPAND default.
    """
    params = {
        "say": text,
        "expand": expand or settings.TTS_EXPAND,
        "sentence_pause_ms": settings.TTS_SENTENCE_PAUSE_MS,
        "crossfade_ms": settings.TTS_CROSSFADE_MS,
        "min_chunk_chars": settings.TTS_MIN_CHUNK_CHARS,
        "min_chunk_words": settings.TTS_MIN_CHUNK_WORDS,
        "highpass_hz": settings.TTS_HIGHPASS_HZ,
        "target_peak": settings.TTS_TARGET_PEAK,
        "max_gain": settings.TTS_MAX_GAIN,
        "fade_ms": settings.TTS_FADE_MS,
        "tail_ms": settings.TTS_TAIL_MS,
        "pitch_semitones": settings.TTS_PITCH_SEMITONES,
        "speaking_rate": settings.TTS_SPEAKING_RATE,
    }
    if settings.TTS_SPEAKER_ID is not None:
        params["speaker_id"] = int(settings.TTS_SPEAKER_ID)
    mood = str(settings.TTS_MOOD or "").strip().lower()
    if mood and mood != "custom":
        preset = _load_mood_presets().get(mood)
        if preset:
            params.update(preset)
    query = urlencode(params, quote_via=quote)
    url = f"{tts_base_url.rstrip('/')}/api/tts/stream?{query}"

    timeout_s = float(settings.TTS_HTTP_TIMEOUT_S)
    timeout = None if timeout_s <= 0 else httpx.Timeout(timeout_s)
    client = _get_http_client()

    # httpx.AsyncClient.stream() returns an *async context manager* (do NOT await it)
    cm = client.stream("GET", url, timeout=timeout)
    resp = await cm.__aenter__()

    if resp.status_code != 200:
        body = await resp.aread()
        await cm.__aexit__(None, None, None)
        raise RuntimeError(f"TTS HTTP {resp.status_code}: {body[:400]!r}")

    sr = int(resp.headers.get("X-Audio-Sample-Rate", "22050") or 22050)
    _cm_closed = False

    async def it() -> AsyncIterator[bytes]:
        nonlocal _cm_closed
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            if not _cm_closed:
                _cm_closed = True
                await cm.__aexit__(None, None, None)

    return sr, it()
