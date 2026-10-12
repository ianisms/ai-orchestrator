"""get_person_id — meta-tool for transparent person identification.

Flow
----
1. Orchestrator tees client turn audio into a WAV file and injects
   ``[AUDIO_REF session_id=X audio_id=Y]`` into the LLM system context.

2. LLM calls this tool when it needs to know who is speaking, passing the
   session_id and audio_id from the current [AUDIO_REF].

3. Tool first checks the SQLite session cache.  If there is a live hit it
   returns the person's name immediately — no audio processing needed.

4. On a cache miss, it reads the WAV file and runs NeMo speaker embedding
   + cosine similarity against enrolled person profiles.

5. If confidence >= threshold, the result is cached and the name returned.

6. If no biometric match, the tool returns a sentinel string telling the LLM
   to ask the user their name, then call again with stated_name set.

7. On a stated_name call the tool enrolls the voice sample, updates the cache,
   and returns a confirmation.

Container paths
---------------
  audio:   /app/state/client_audio/{session_id}_{audio_id}.wav
  profile: /app/state/speaker_identity.json  (shared with speaker_identity)
  cache:   /app/state/person_sessions.sqlite3
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import sqlite3
import time
import wave
from pathlib import Path
from typing import Annotated, Any, List, Optional

from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config

_DEFAULT_AUDIO_DIR = Path("/app/state/client_audio")
_DEFAULT_STATE_FILE = Path("/app/state/speaker_identity.json")
_DEFAULT_SESSION_DB = Path("/app/state/person_sessions.sqlite3")
_DEFAULT_THRESHOLD = 0.78
_DEFAULT_TTL_S = 3600

_MODEL_CACHE: dict[str, object] = {}


# ------------------------------------------------------------------ #
# Config                                                               #
# ------------------------------------------------------------------ #

def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


# ------------------------------------------------------------------ #
# Person profile store (mirrors speaker_identity internals)           #
# ------------------------------------------------------------------ #

def _load_profiles(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        profiles = data.get("profiles") if isinstance(data, dict) else None
        return profiles if isinstance(profiles, dict) else {}
    except Exception:
        return {}


def _save_profiles(state_file: Path, profiles: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"profiles": profiles}, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


# ------------------------------------------------------------------ #
# NeMo embedding                                                       #
# ------------------------------------------------------------------ #

def _cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = na = nb = 0.0
    for i in range(n):
        av, bv = float(a[i]), float(b[i])
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


def _get_nemo_model(config: dict[str, Any]):
    import os
    import sys
    aliases = {"small": "titanet_small", "medium": "ecapa_tdnn", "large": "titanet_large"}
    raw_name = str(config.get("model_name") or os.getenv("SPEAKER_NEMO_MODEL_NAME") or "titanet_large").strip()
    model_name = aliases.get(raw_name.lower(), raw_name)
    device = str(config.get("device") or os.getenv("SPEAKER_NEMO_DEVICE") or "auto").strip().lower()
    cache_key = f"{model_name}::{device}"

    # Reuse model already loaded by speaker_identity if present.
    try:
        si_mod = sys.modules.get("tools.speaker_identity.speaker_identity")
        if si_mod is not None:
            si_cache = getattr(si_mod, "_MODEL_CACHE", {})
            cached = si_cache.get(cache_key)
            if isinstance(cached, tuple) and len(cached) == 2:
                return cached
    except Exception:
        pass

    cached = _MODEL_CACHE.get(cache_key)
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached

    import torch
    from nemo.collections.asr.models import EncDecSpeakerLabelModel

    target = "cuda" if (device in {"auto", ""} and torch.cuda.is_available()) else (
        device if device not in {"auto", ""} else "cpu"
    )
    model = EncDecSpeakerLabelModel.from_pretrained(model_name=model_name)
    model = model.to(target)
    model.eval()
    result = (model, torch)
    _MODEL_CACHE[cache_key] = result
    return result


_EMBEDDING_TIMEOUT_S = 10


def _extract_embedding(wav_path: Path, config: dict[str, Any]) -> list[float]:
    model, torch = _get_nemo_model(config)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(model.get_embedding, str(wav_path))
        try:
            emb_tensor = future.result(timeout=_EMBEDDING_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            raise RuntimeError(f"NeMo embedding timed out after {_EMBEDDING_TIMEOUT_S}s")
    if hasattr(emb_tensor, "detach"):
        emb_tensor = emb_tensor.detach()
    if hasattr(emb_tensor, "cpu"):
        emb_tensor = emb_tensor.cpu()
    if hasattr(emb_tensor, "flatten"):
        emb_tensor = emb_tensor.flatten()
    emb = emb_tensor.tolist() if hasattr(emb_tensor, "tolist") else list(emb_tensor)
    if not emb:
        raise RuntimeError("NeMo returned empty embedding")
    vals = [float(v) for v in emb]
    norm = float(torch.linalg.vector_norm(torch.tensor(vals)))
    if norm > 0:
        vals = [v / norm for v in vals]
    return vals


# ------------------------------------------------------------------ #
# SQLite session cache                                                 #
# ------------------------------------------------------------------ #

def _open_session_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS person_sessions (
            session_id    TEXT PRIMARY KEY,
            profile_id    TEXT NOT NULL,
            display_name  TEXT NOT NULL,
            method        TEXT NOT NULL,
            confidence    REAL,
            identified_at INTEGER NOT NULL,
            expires_at    INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def _cache_get(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    now = int(time.time())
    row = conn.execute(
        "SELECT display_name FROM person_sessions WHERE session_id=? AND expires_at>?",
        (session_id, now),
    ).fetchone()
    return str(row["display_name"]) if row else None


def _cache_set(
    conn: sqlite3.Connection,
    session_id: str,
    profile_id: str,
    display_name: str,
    method: str,
    confidence: Optional[float],
    ttl_s: int,
) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO person_sessions
            (session_id, profile_id, display_name, method, confidence, identified_at, expires_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            profile_id=excluded.profile_id,
            display_name=excluded.display_name,
            method=excluded.method,
            confidence=excluded.confidence,
            identified_at=excluded.identified_at,
            expires_at=excluded.expires_at
        """,
        (session_id, profile_id, display_name, method, confidence, now, now + ttl_s),
    )
    conn.commit()


def _cache_purge(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM person_sessions WHERE expires_at<=?", (int(time.time()),))
    conn.commit()


# ------------------------------------------------------------------ #
# Tool registration                                                    #
# ------------------------------------------------------------------ #

def register(server) -> List[str]:
    log = logging.getLogger("tools.get_person_id")
    config = _load_config()

    audio_dir = Path(str(config.get("audio_dir") or _DEFAULT_AUDIO_DIR))
    state_file = Path(str(config.get("state_file") or _DEFAULT_STATE_FILE))
    session_db_path = Path(str(config.get("session_db") or _DEFAULT_SESSION_DB))
    threshold = float(config.get("confidence_threshold") or _DEFAULT_THRESHOLD)
    ttl_s = int(config.get("session_ttl_s") or _DEFAULT_TTL_S)
    max_samples = 8

    tool_description, param_meta = get_tool_config(
        config,
        "get_person_id",
        (
            "Identify the person speaking in the current turn using audio captured by the client. "
            "Call with session_id and audio_id from the current [AUDIO_REF] context. "
            "If the person cannot be identified, the tool instructs you to ask their name "
            "and call again with stated_name."
        ),
    )
    sid_desc, sid_alias, sid_title = get_param_meta(
        param_meta, "session_id", "The session_id from the current [AUDIO_REF] context tag."
    )
    aid_desc, aid_alias, aid_title = get_param_meta(
        param_meta, "audio_id", "The audio_id from the current [AUDIO_REF] context tag."
    )
    name_desc, name_alias, name_title = get_param_meta(
        param_meta,
        "stated_name",
        "The person's name as they just stated it. Pass this to enroll their voice.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    session_db_conn: Optional[sqlite3.Connection] = None

    def _get_db() -> sqlite3.Connection:
        nonlocal session_db_conn
        if session_db_conn is None:
            session_db_conn = _open_session_db(session_db_path)
        return session_db_conn

    @server.tool(name="get_person_id", description=tool_description)
    def get_person_id(
        session_id: Annotated[str, Field(**_field_kwargs(sid_desc, sid_alias, sid_title))],
        audio_id: Annotated[str, Field(**_field_kwargs(aid_desc, aid_alias, aid_title))],
        stated_name: Annotated[
            str | None,
            Field(**_field_kwargs(name_desc, name_alias, name_title)),
        ] = None,
    ) -> str:
        log_tool_call(log, "get_person_id", session_id=session_id, audio_id=audio_id,
                      stated_name=stated_name)

        sid = str(session_id or "").strip()
        aid = str(audio_id or "").strip()
        if not sid or not aid:
            return log_tool_result(log, "get_person_id",
                                   "session_id and audio_id are required (read them from [AUDIO_REF]).")

        db = _get_db()
        _cache_purge(db)

        # ---- Fast path: session cache hit (no audio processing needed) ----
        if not stated_name:
            cached_name = _cache_get(db, sid)
            if cached_name:
                log.debug("get_person_id cache hit session_id=%s name=%s", sid, cached_name)
                return log_tool_result(log, "get_person_id", cached_name)

        # ---- Locate WAV file ----
        wav_path = audio_dir / f"{sid}_{aid}.wav"
        if not wav_path.exists():
            return log_tool_result(
                log, "get_person_id",
                "Audio reference not found or expired. Ask the user who is speaking."
            )

        # ---- Validate WAV ----
        try:
            with wave.open(str(wav_path), "rb") as wf:
                if wf.getnframes() < 1:
                    raise ValueError("empty WAV")
        except Exception as exc:
            return log_tool_result(log, "get_person_id",
                                   f"Audio file unreadable: {exc}. Ask who is speaking.")

        # ---- Enrollment path (stated_name provided) ----
        if stated_name:
            name_clean = str(stated_name).strip()
            profile_id = name_clean.lower().replace(" ", "_")
            display_name = name_clean.title()
            try:
                emb = _extract_embedding(wav_path, config)
            except Exception as exc:
                log.warning("get_person_id enrollment embedding failed: %r", exc)
                return log_tool_result(log, "get_person_id",
                                       f"Could not process audio for enrollment: {exc}")

            profiles = _load_profiles(state_file)
            entry = profiles.get(profile_id) if isinstance(profiles.get(profile_id), dict) else {}
            samples = entry.get("samples") if isinstance(entry.get("samples"), list) else []
            samples.append(emb)
            if len(samples) > max_samples:
                samples = samples[-max_samples:]
            mean_emb = _average_embeddings(samples)
            profiles[profile_id] = {"samples": samples, "mean_embedding": mean_emb}
            _save_profiles(state_file, profiles)

            _cache_set(db, sid, profile_id, display_name, "stated", None, ttl_s)
            log.info("get_person_id enrolled profile_id=%s samples=%d", profile_id, len(samples))
            return log_tool_result(log, "get_person_id", display_name)

        # ---- Biometric identification path ----
        try:
            emb = _extract_embedding(wav_path, config)
        except Exception as exc:
            log.warning("get_person_id embedding failed: %r", exc)
            return log_tool_result(
                log, "get_person_id",
                "Person identification unavailable right now. Ask who is speaking."
            )

        profiles = _load_profiles(state_file)
        if not profiles:
            return log_tool_result(
                log, "get_person_id",
                "No person profiles enrolled yet. Ask who is speaking, then call get_person_id "
                "again with stated_name to register them."
            )

        best_id = ""
        best_score = -1.0
        for pid, entry in profiles.items():
            if not isinstance(entry, dict):
                continue
            mean_emb = entry.get("mean_embedding")
            if not isinstance(mean_emb, list):
                continue
            score = float(_cosine(emb, [float(x) for x in mean_emb]))
            if score > best_score:
                best_score = score
                best_id = str(pid)

        if best_score >= threshold and best_id:
            display_name = best_id.replace("_", " ").title()
            _cache_set(db, sid, best_id, display_name, "biometric", best_score, ttl_s)
            log.info("get_person_id identified profile_id=%s score=%.3f", best_id, best_score)
            return log_tool_result(log, "get_person_id", display_name)

        return log_tool_result(
            log, "get_person_id",
            "Person not recognised. Ask who is speaking, then call get_person_id again "
            "with stated_name set to their name."
        )

    return ["get_person_id"]
