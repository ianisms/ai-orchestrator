import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np
import yaml
from fastapi import FastAPI, Query
from fastapi.responses import Response, StreamingResponse

from server.engine import NemoTTSEngine
from server.chunking import smart_chunks, expand_abbreviations
from server.normalize import normalize_text
from server.stats import metrics_content_type, metrics_payload, record_request, reset_all, reset_uptime

app = FastAPI(title="Custom-voice TTS")


def _read_yaml(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): v for k, v in data.items()}


def _apply_config_env() -> None:
    global_path = Path(os.getenv("TTS_GLOBAL_CONFIG_PATH", "/ai/server/config/config.yaml"))
    default_component = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    component_path = Path(os.getenv("TTS_CONFIG_PATH", str(default_component)))
    global_cfg = _read_yaml(global_path)
    component_cfg = _read_yaml(component_path)
    config = {**global_cfg, **component_cfg}

    if "TTS_LOG_LEVEL" not in os.environ and "LOG_LEVEL" in config:
        os.environ["TTS_LOG_LEVEL"] = str(config["LOG_LEVEL"])

    for key, value in config.items():
        if not isinstance(key, str):
            continue
        upper_key = key.upper()
        if upper_key in os.environ:
            continue
        if not (upper_key.startswith("TTS_") or upper_key in {"FASTPITCH_NEMO", "HIFIGAN_NEMO"}):
            continue
        if isinstance(value, (dict, list)):
            continue
        os.environ[upper_key] = str(value)


def _apply_logging_level() -> None:
    level_name = os.getenv("TTS_LOG_LEVEL", "").strip().upper()
    if not level_name:
        return
    level = getattr(logging, level_name, None)
    if level is None:
        return
    logger_names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    for name in logger_names:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        for handler in logger.handlers:
            handler.setLevel(level)


_apply_config_env()
_apply_logging_level()

engine = NemoTTSEngine()
gpu_lock = asyncio.Lock()


@app.get("/healthz")
def healthz():
    return {"ok": True, "sample_rate": engine.sample_rate}


@app.get("/metrics")
def metrics():
    return Response(metrics_payload(), media_type=metrics_content_type())


@app.post("/stats/reset")
def stats_reset():
    reset_uptime()
    return {"status": "ok"}


@app.post("/stats/reset_lifetime")
def stats_reset_lifetime():
    reset_all()
    return {"status": "ok"}


@app.get("/api/tts/stream")
async def tts_stream(
    say: str = Query(...),
    expand: str = Query(""),

    # Chunking
    max_chars: int = Query(900, ge=100, le=20000),
    min_chunk_chars: Optional[int] = Query(None, ge=10, le=20000),
    min_chunk_words: Optional[int] = Query(None, ge=1, le=2000),

    # Audio
    target_peak: Optional[float] = Query(None),
    max_gain: Optional[float] = Query(None),
    fade_ms: Optional[float] = Query(None),
    tail_ms: Optional[float] = Query(None),
    highpass_hz: Optional[float] = Query(None),

    # Noise gate / expander
    gate_threshold_db: Optional[float] = Query(None),
    gate_ratio: Optional[float] = Query(None),
    gate_attack_ms: Optional[float] = Query(None, ge=0.1, le=2000.0),
    gate_release_ms: Optional[float] = Query(None, ge=0.1, le=4000.0),

    # Streaming
    sentence_pause_ms: int = Query(100, ge=0, le=1000),
    crossfade_ms: int = Query(16, ge=0, le=50),

    # Mood-ish controls
    pitch_semitones: Optional[float] = Query(None, ge=-6.0, le=6.0),
    speaking_rate: Optional[float] = Query(None, ge=0.5, le=1.5),
):
    record_request()
    text = await normalize_text(say or "")
    expand_str = str(expand or "").strip()
    if expand_str and expand_str not in ("0", "none"):
        text = expand_abbreviations(text, groups=expand_str)

    chunk_opts = {
        "min_chunk_chars": min_chunk_chars,
        "min_chunk_words": min_chunk_words,
    }

    audio_opts = {
        "target_peak": target_peak,
        "max_gain": max_gain,
        "fade_ms": fade_ms,
        "tail_ms": tail_ms,
        "highpass_hz": highpass_hz,
        "gate_threshold_db": gate_threshold_db,
        "gate_ratio": gate_ratio,
        "gate_attack_ms": gate_attack_ms,
        "gate_release_ms": gate_release_ms,
        "pitch_semitones": pitch_semitones,
        "speaking_rate": speaking_rate,
    }

    items = smart_chunks(text, max_chars=max_chars, opts=chunk_opts)
    sr = engine.sample_rate
    xf_n = int(sr * (crossfade_ms / 1000))

    async def gen():
        async with gpu_lock:
            prev = None

            for chunk, _ in items:
                cur = engine.synthesize_pcm16(chunk, opts=audio_opts)

                if prev is not None and xf_n > 0:
                    n = min(xf_n, len(prev), len(cur))
                    t = np.linspace(0, 1, n)
                    wa = np.cos(t * np.pi / 2)
                    wb = np.sin(t * np.pi / 2)
                    mix = (prev[-n:] * wa + cur[:n] * wb).astype(np.int16)
                    yield prev[:-n].tobytes()
                    yield mix.tobytes()
                elif prev is not None:
                    yield prev.tobytes()

                prev = cur

                if sentence_pause_ms > 0 and chunk.rstrip().endswith((".", "!", "?")):
                    yield prev.tobytes()
                    prev = None
                    yield engine.silence_pcm16(sr, sentence_pause_ms).tobytes()

            if prev is not None:
                yield prev.tobytes()

    return StreamingResponse(
        gen(),
        media_type="audio/L16",
        headers={"X-Audio-Sample-Rate": str(sr)},
    )
