import base64
import io
import json
import logging
import math
import os
import tempfile
import wave
from pathlib import Path
from typing import Annotated, Any, List

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_MODEL_CACHE: dict[str, object] = {}
_MODEL_ALIASES = {
    "small": "titanet_small",
    "medium": "ecapa_tdnn",
    "large": "titanet_large",
}


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _state_path(config: dict[str, Any]) -> Path:
    raw = str(config.get("state_file") or "/ai/server/tools_server/state/speaker_identity.json")
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"profiles": {}}
    if not isinstance(data, dict):
        return {"profiles": {}}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    return {"profiles": profiles}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def _as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True)


def _decode_wav(audio_b64: str) -> tuple[bytes, int]:
    raw = base64.b64decode(audio_b64, validate=True)
    with wave.open(io.BytesIO(raw), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        frame_count = int(wf.getnframes())
    if channels < 1:
        raise ValueError("WAV must include at least one channel")
    if sample_width != 2:
        raise ValueError("WAV must be PCM16")
    if frame_count <= 0:
        raise ValueError("WAV has no audio frames")
    return raw, sample_rate


def _cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        av = float(a[i])
        bv = float(b[i])
        dot += av * bv
        na += av * av
        nb += bv * bv
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _average_embeddings(items: list[list[float]]) -> list[float]:
    if not items:
        return []
    dim = min(len(x) for x in items if x)
    if dim <= 0:
        return []
    avg = [0.0] * dim
    count = 0
    for emb in items:
        if len(emb) < dim:
            continue
        for i in range(dim):
            avg[i] += float(emb[i])
        count += 1
    if count == 0:
        return []
    avg = [v / count for v in avg]
    norm = math.sqrt(sum(v * v for v in avg))
    if norm > 0:
        avg = [v / norm for v in avg]
    return avg


def _load_nemo_speaker_model(config: dict[str, Any]) -> tuple[object, object]:
    raw_model_name = str(config.get("model_name") or os.getenv("SPEAKER_NEMO_MODEL_NAME") or "titanet_large").strip()
    model_name = _MODEL_ALIASES.get(raw_model_name.lower(), raw_model_name)
    device = str(config.get("device") or os.getenv("SPEAKER_NEMO_DEVICE") or "auto").strip().lower()
    cache_key = f"{model_name}::{device}"
    cached = _MODEL_CACHE.get(cache_key)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached[0], cached[1]
    try:
        import torch
        from nemo.collections.asr.models import EncDecSpeakerLabelModel
    except Exception as exc:
        raise RuntimeError(
            "NeMo speaker dependencies are missing. Install nemo_toolkit[asr], torch, and torchaudio."
        ) from exc

    if device in {"auto", ""}:
        target = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        target = device
    model = EncDecSpeakerLabelModel.from_pretrained(model_name=model_name)
    model = model.to(target)
    model.eval()
    _MODEL_CACHE[cache_key] = (model, torch)
    return model, torch


def _extract_embedding_nemo(audio_b64: str, config: dict[str, Any]) -> list[float]:
    wav_bytes, _sample_rate = _decode_wav(audio_b64)
    model, torch = _load_nemo_speaker_model(config)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        tmp.write(wav_bytes)
        tmp.flush()
        # NeMo returns embedding tensor for the provided audio path.
        emb_tensor = model.get_embedding(tmp.name)
        if hasattr(emb_tensor, "detach"):
            emb_tensor = emb_tensor.detach()
        if hasattr(emb_tensor, "cpu"):
            emb_tensor = emb_tensor.cpu()
        if hasattr(emb_tensor, "flatten"):
            emb_tensor = emb_tensor.flatten()
        if hasattr(emb_tensor, "tolist"):
            emb = emb_tensor.tolist()
        else:
            emb = list(emb_tensor)
    if not isinstance(emb, list) or not emb:
        raise RuntimeError("NeMo embedding extraction returned no data")
    # Normalize for stable cosine comparisons.
    vals = [float(v) for v in emb]
    if torch is not None:
        norm = float(torch.linalg.vector_norm(torch.tensor(vals)))
    else:
        norm = math.sqrt(sum(v * v for v in vals))
    if norm > 0:
        vals = [v / norm for v in vals]
    return vals


def register(server) -> List[str]:
    log = logging.getLogger("tools.speaker_identity")
    config = _load_config()
    state_file = _state_path(config)
    default_threshold = float(config.get("default_threshold") or 0.78)
    max_samples = int(config.get("max_enrollment_samples") or 8)

    tool_description, param_meta = get_tool_config(
        config,
        "speaker_identity",
        "Enroll and identify household speakers using NeMo speaker embeddings.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: enroll, identify, verify, delete_enrollment, list_enrollments, clear_enrollments.",
    )
    profile_desc, profile_alias, profile_title = get_param_meta(
        param_meta,
        "profile_id",
        "Profile identifier for enroll/verify/delete.",
    )
    audio_desc, audio_alias, audio_title = get_param_meta(
        param_meta,
        "audio_b64",
        "Base64-encoded WAV audio bytes.",
    )
    threshold_desc, threshold_alias, threshold_title = get_param_meta(
        param_meta,
        "threshold",
        "Cosine similarity threshold.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(name="speaker_identity", description=tool_description)
    def speaker_identity(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "list_enrollments",
        profile_id: Annotated[str | None, Field(**_field_kwargs(profile_desc, profile_alias, profile_title))] = None,
        audio_b64: Annotated[str | None, Field(**_field_kwargs(audio_desc, audio_alias, audio_title))] = None,
        threshold: Annotated[float | None, Field(**_field_kwargs(threshold_desc, threshold_alias, threshold_title))] = None,
    ) -> str:
        log_tool_call(log, "speaker_identity", action=action, profile_id=profile_id)
        state = _load_state(state_file)
        profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
        action_value = str(action or "").strip().lower()
        pid = str(profile_id or "").strip().lower()
        threshold_value = float(default_threshold if threshold is None else threshold)

        if action_value == "list_enrollments":
            items = []
            for key in sorted(profiles.keys()):
                data = profiles.get(key)
                if not isinstance(data, dict):
                    continue
                items.append({"profile_id": key, "samples": int(len(data.get("samples") or []))})
            return log_tool_result(log, "speaker_identity", _as_json({"status": "ok", "profiles": items}))

        if action_value == "clear_enrollments":
            _save_state(state_file, {"profiles": {}})
            return log_tool_result(log, "speaker_identity", _as_json({"status": "ok", "cleared": True}))

        if action_value == "delete_enrollment":
            if not pid:
                return log_tool_result(log, "speaker_identity", _as_json({"status": "error", "message": "profile_id required"}))
            existed = pid in profiles
            profiles.pop(pid, None)
            state["profiles"] = profiles
            _save_state(state_file, state)
            return log_tool_result(log, "speaker_identity", _as_json({"status": "ok", "deleted": bool(existed), "profile_id": pid}))

        if action_value not in {"enroll", "identify", "verify"}:
            return log_tool_result(
                log,
                "speaker_identity",
                _as_json(
                    {
                        "status": "error",
                        "message": "unsupported action: use enroll, identify, verify, delete_enrollment, list_enrollments, clear_enrollments",
                    }
                ),
            )

        audio_text = str(audio_b64 or "").strip()
        if not audio_text:
            return log_tool_result(log, "speaker_identity", _as_json({"status": "error", "message": "audio_b64 required"}))

        try:
            emb = _extract_embedding_nemo(audio_text, config)
        except Exception as exc:
            return log_tool_result(
                log,
                "speaker_identity",
                _as_json({"status": "error", "message": f"NeMo embedding failed: {exc}"}),
            )

        if action_value == "enroll":
            if not pid:
                return log_tool_result(log, "speaker_identity", _as_json({"status": "error", "message": "profile_id required"}))
            entry = profiles.get(pid) if isinstance(profiles.get(pid), dict) else {}
            samples_list = entry.get("samples") if isinstance(entry.get("samples"), list) else []
            samples_list.append(emb)
            if len(samples_list) > max_samples:
                samples_list = samples_list[-max_samples:]
            mean_emb = _average_embeddings(samples_list)
            profiles[pid] = {"samples": samples_list, "mean_embedding": mean_emb}
            state["profiles"] = profiles
            _save_state(state_file, state)
            return log_tool_result(log, "speaker_identity", _as_json({"status": "ok", "profile_id": pid, "samples": len(samples_list)}))

        if not profiles:
            return log_tool_result(log, "speaker_identity", _as_json({"status": "error", "message": "no enrollments"}))

        if action_value == "verify":
            if not pid:
                return log_tool_result(log, "speaker_identity", _as_json({"status": "error", "message": "profile_id required"}))
            entry = profiles.get(pid)
            if not isinstance(entry, dict):
                return log_tool_result(log, "speaker_identity", _as_json({"status": "not_found", "profile_id": pid}))
            mean_emb = entry.get("mean_embedding")
            if not isinstance(mean_emb, list):
                return log_tool_result(log, "speaker_identity", _as_json({"status": "error", "message": "invalid enrollment"}))
            score = float(_cosine(emb, [float(x) for x in mean_emb]))
            match = score >= threshold_value
            return log_tool_result(
                log,
                "speaker_identity",
                _as_json(
                    {
                        "status": "ok",
                        "profile_id": pid,
                        "score": round(score, 4),
                        "threshold": threshold_value,
                        "match": match,
                    }
                ),
            )

        best_id = ""
        best_score = -1.0
        for key, entry in profiles.items():
            if not isinstance(entry, dict):
                continue
            mean_emb = entry.get("mean_embedding")
            if not isinstance(mean_emb, list):
                continue
            score = float(_cosine(emb, [float(x) for x in mean_emb]))
            if score > best_score:
                best_score = score
                best_id = str(key)
        return log_tool_result(
            log,
            "speaker_identity",
            _as_json(
                {
                    "status": "ok",
                    "profile_id": best_id if best_score >= threshold_value else "",
                    "score": round(best_score, 4),
                    "threshold": threshold_value,
                    "match": bool(best_score >= threshold_value),
                }
            ),
        )

    return ["speaker_identity"]
