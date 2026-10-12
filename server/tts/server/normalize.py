from __future__ import annotations

import asyncio
import os
import logging
import re
import threading
import time
from typing import Optional

_NORMALIZER = None
_NORMALIZER_LOCK = threading.Lock()
_NORMALIZE_SEM = asyncio.Semaphore(int(os.getenv("TTS_NORMALIZE_CONCURRENCY", "1")))
_NORMALIZE_CACHE_TTL_S = 5.0
_NORMALIZE_CACHE = {
    "enabled": True,
    "timeout_s": 0.15,
    "checked_at": 0.0,
}
_LOG = logging.getLogger("tts.normalize")
_HYPHENS = {
    "\u2010",  # hyphen
    "\u2011",  # non-breaking hyphen
    "\u2012",  # figure dash
    "\u2013",  # en dash
    "\u2014",  # em dash
    "\u2212",  # minus sign
}

_RANGE_RE = re.compile(r"(?<!\\w)([$€£]?\\d+(?:\\.\\d+)?)(\\s*-\\s*)([$€£]?\\d+(?:\\.\\d+)?)")

def _replace_numeric_ranges(text: str) -> str:
    if "-" not in text:
        return text
    return _RANGE_RE.sub(r"\\1 to \\3", text)

def _pre_clean(text: str) -> str:
    if any(ch in text for ch in _HYPHENS):
        for ch in _HYPHENS:
            text = text.replace(ch, "-")
    return text


def _load_normalizer() -> Optional[object]:
    try:
        from nemo_text_processing.text_normalization.normalize import Normalizer
    except Exception:
        return None
    return Normalizer(input_case="cased", lang="en")


def _get_normalizer() -> Optional[object]:
    global _NORMALIZER
    if _NORMALIZER is not None:
        return _NORMALIZER
    with _NORMALIZER_LOCK:
        if _NORMALIZER is not None:
            return _NORMALIZER
        _NORMALIZER = _load_normalizer()
        return _NORMALIZER

def _additional_normalization(text: str) -> str:
    # Additional normalization rules can be added here
    return _replace_numeric_ranges(text)


def _normalize_sync(text: str) -> str:
    normalizer = _get_normalizer()
    if normalizer is None:
        return text
    try:
        _LOG.debug("normalize input=%r", text)
        text = _pre_clean(text)
        normalized = normalizer.normalize(text, verbose=False, punct_post_process=True)
        normalized = _additional_normalization(normalized)
        _LOG.debug("normalize nemo output=%r", normalized)
        return normalized
    except Exception:
        return text


async def normalize_text(text: str) -> str:
    now = time.monotonic()
    if now - _NORMALIZE_CACHE["checked_at"] > _NORMALIZE_CACHE_TTL_S:
        enabled = os.getenv("TTS_NORMALIZE_ENABLED", "true").strip().lower()
        _NORMALIZE_CACHE["enabled"] = enabled in {"1", "true", "yes", "on"}
        try:
            timeout_ms = int(os.getenv("TTS_NORMALIZE_TIMEOUT_MS", "150"))
        except Exception:
            timeout_ms = 150
        _NORMALIZE_CACHE["timeout_s"] = max(0.05, timeout_ms / 1000.0)
        _NORMALIZE_CACHE["checked_at"] = now
    if not _NORMALIZE_CACHE["enabled"]:
        return text
    timeout_s = float(_NORMALIZE_CACHE["timeout_s"])
    try:
        async with _NORMALIZE_SEM:
            return await asyncio.wait_for(asyncio.to_thread(_normalize_sync, text), timeout=timeout_s)
    except Exception:
        return text
