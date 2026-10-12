import base64
import io
import json
import logging
import math
import wave
from typing import Annotated, Any, List

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True)


def _decode_audio(audio_b64: str) -> tuple[list[float], int]:
    raw = base64.b64decode(audio_b64, validate=True)
    with wave.open(io.BytesIO(raw), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        frame_count = int(wf.getnframes())
        frames = wf.readframes(frame_count)
    if sample_width != 2:
        raise ValueError("WAV must be PCM16")
    if channels < 1 or frame_count <= 0:
        raise ValueError("Invalid WAV payload")

    ints: list[int] = []
    for i in range(0, len(frames), 2):
        ints.append(int.from_bytes(frames[i : i + 2], byteorder="little", signed=True))

    if channels > 1:
        mono: list[int] = []
        for i in range(0, len(ints), channels):
            chunk = ints[i : i + channels]
            if not chunk:
                continue
            mono.append(int(round(sum(chunk) / len(chunk))))
        ints = mono

    samples = [float(v) / 32768.0 for v in ints]
    return samples, sample_rate


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round((len(s) - 1) * p))
    idx = max(0, min(len(s) - 1, idx))
    return s[idx]


def _analyze(samples: list[float], sample_rate: int) -> dict[str, float]:
    n = max(1, len(samples))
    duration_s = n / max(1, sample_rate)
    peak = max(abs(v) for v in samples) if samples else 0.0
    clipping_ratio = sum(1 for v in samples if abs(v) >= 0.995) / n
    rms = math.sqrt(sum(v * v for v in samples) / n)
    rms_dbfs = 20.0 * math.log10(max(rms, 1e-9))
    silence_ratio = sum(1 for v in samples if abs(v) < 0.01) / n

    frame = max(160, int(sample_rate * 0.02))
    hop = max(80, int(sample_rate * 0.01))
    frame_rms: list[float] = []
    for i in range(0, max(0, len(samples) - frame), hop):
        chunk = samples[i : i + frame]
        if not chunk:
            continue
        r = math.sqrt(sum(v * v for v in chunk) / len(chunk))
        frame_rms.append(r)

    if not frame_rms:
        noise = rms
        speech = rms
    else:
        noise = _percentile(frame_rms, 0.20)
        speech = _percentile(frame_rms, 0.90)
    snr_db = 20.0 * math.log10(max(speech, 1e-9) / max(noise, 1e-9))

    return {
        "duration_s": round(duration_s, 3),
        "sample_rate_hz": float(sample_rate),
        "peak": round(peak, 5),
        "clipping_ratio": round(clipping_ratio, 5),
        "rms_dbfs": round(rms_dbfs, 2),
        "silence_ratio": round(silence_ratio, 5),
        "estimated_snr_db": round(snr_db, 2),
    }


def register(server) -> List[str]:
    log = logging.getLogger("tools.speaker_enrollment_coach")
    config = _load_config()
    min_dur = float(config.get("duration_target_min_s") or 2.0)
    max_dur = float(config.get("duration_target_max_s") or 8.0)
    max_clip = float(config.get("max_clipping_ratio") or 0.01)
    min_rms = float(config.get("min_rms_dbfs") or -38.0)
    min_snr = float(config.get("min_estimated_snr_db") or 10.0)
    max_sil = float(config.get("max_silence_ratio") or 0.65)

    tool_description, param_meta = get_tool_config(
        config,
        "speaker_enrollment_coach",
        "Analyze enrollment clips and coach for speaker enrollment quality.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: analyze_clip, enrollment_readiness.",
    )
    audio_desc, audio_alias, audio_title = get_param_meta(
        param_meta,
        "audio_b64",
        "Base64-encoded WAV PCM16 clip.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(name="speaker_enrollment_coach", description=tool_description)
    def speaker_enrollment_coach(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "analyze_clip",
        audio_b64: Annotated[str | None, Field(**_field_kwargs(audio_desc, audio_alias, audio_title))] = None,
    ) -> str:
        log_tool_call(log, "speaker_enrollment_coach", action=action)
        action_value = str(action or "").strip().lower()
        if action_value not in {"analyze_clip", "enrollment_readiness"}:
            return log_tool_result(
                log,
                "speaker_enrollment_coach",
                _as_json({"status": "error", "message": "unsupported action: use analyze_clip,enrollment_readiness"}),
            )
        clip = str(audio_b64 or "").strip()
        if not clip:
            return log_tool_result(log, "speaker_enrollment_coach", _as_json({"status": "error", "message": "audio_b64 required"}))
        try:
            samples, rate = _decode_audio(clip)
            metrics = _analyze(samples, rate)
        except Exception as exc:
            return log_tool_result(log, "speaker_enrollment_coach", _as_json({"status": "error", "message": f"audio decode failed: {exc}"}))

        issues: list[str] = []
        tips: list[str] = []
        if metrics["duration_s"] < min_dur:
            issues.append("clip_too_short")
            tips.append("Record 2 to 8 seconds of natural speech.")
        if metrics["duration_s"] > max_dur:
            issues.append("clip_too_long")
            tips.append("Use a shorter clip around 3 to 6 seconds.")
        if metrics["clipping_ratio"] > max_clip:
            issues.append("clipping_detected")
            tips.append("Move farther from the mic or lower input gain.")
        if metrics["rms_dbfs"] < min_rms:
            issues.append("too_quiet")
            tips.append("Speak louder or move closer to the mic.")
        if metrics["estimated_snr_db"] < min_snr:
            issues.append("low_snr")
            tips.append("Reduce background noise before enrollment.")
        if metrics["silence_ratio"] > max_sil:
            issues.append("too_much_silence")
            tips.append("Trim silence and keep active speech in the clip.")

        ok = len(issues) == 0
        payload = {
            "status": "ok",
            "action": action_value,
            "ready": ok,
            "issues": issues,
            "tips": tips,
            "metrics": metrics,
            "target": {
                "duration_s": [min_dur, max_dur],
                "max_clipping_ratio": max_clip,
                "min_rms_dbfs": min_rms,
                "min_estimated_snr_db": min_snr,
                "max_silence_ratio": max_sil,
            },
        }
        return log_tool_result(log, "speaker_enrollment_coach", _as_json(payload))

    return ["speaker_enrollment_coach"]

