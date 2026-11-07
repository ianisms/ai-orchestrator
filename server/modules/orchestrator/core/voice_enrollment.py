"""Admin-run voice enrollment: device-bound, time-boxed, measured.

A voice profile is the thing the voice gate trusts, so how it gets made
matters. Before this module a stated name ("I'm Alice") recorded one pending
sample for an admin to approve, with no quality bar, no check that the new
voice is distinguishable from the others in the house, and no record of who
approved what on which device. This module replaces that for new voices.

The procedure (security/SESSION_BINDING.md section 3.6, VOICE_ENROLLMENT.md):

  1. An admin on the host opens a session for one person on one device, with
     a time limit and a set of prompt sentences (scripts/enroll_voice.py open).
  2. The orchestrator, on every voice turn from that device while the session
     is open, speaks the next prompt, takes the turn's audio, and asks the tool
     server for an embedding plus clip quality (enrollment_sample, hidden from
     the model). The model is not involved at any point.
  3. A clip is accepted only if its quality passes and its transcript matches
     the prompt. A rejected clip is spoken back as a tip and the prompt repeats.
  4. Once enough samples are in, the session is evaluated: every sample against
     the mean of the others (genuine), and every sample against every other
     enrolled profile's mean (impostor). The person's threshold is the highest
     impostor score plus a margin, never below the global floor. The session
     completes when every genuine score clears that threshold.
  5. The profile is written with its samples, mean, threshold and a record of
     who enrolled whom, where, when, with which scores. Embeddings only; the
     clips follow the normal client-audio cleanup.

Fail closed: a sample from another device, an expired session, a clip that
fails quality or the prompt check, or a profile that cannot be separated from
the others all leave the session unchanged (or mark it failed) with a logged
reason. The service never touches a profile that is not the session's own.

State lives in two JSON files, re-read on every call so the admin script and
the orchestrator see the same thing without a restart: the sessions file
(this module's own) and the profiles file (speaker_identity.json, shared with
the tools server, same shape as before plus the new per-profile fields).
"""
from __future__ import annotations

import json
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("voice_enrollment.audit")

OPEN, EVALUATING, COMPLETE, FAILED, EXPIRED, ABORTED = "open", "evaluating", "complete", "failed", "expired", "aborted"
TERMINAL = {COMPLETE, FAILED, EXPIRED, ABORTED}

DEFAULT_PROMPTS = (
    "The kitchen light is on and the back door is locked.",
    "Please set a timer for twelve minutes starting now.",
    "What is the weather going to be like tomorrow afternoon?",
    "Turn the volume down a little and play something quiet.",
    "Remind me to call the dentist on Thursday morning.",
    "How long does it take to drive to the airport from here?",
    "Add milk, eggs and coffee to the shopping list.",
    "Is the garage closed and is the alarm set for tonight?",
    "Read me the headlines from this morning's news.",
    "Switch off every light downstairs except the hallway.",
    "What time does the sun set here in late October?",
    "Start a five minute countdown and tell me when it ends.",
)

_WORD = re.compile(r"[a-z0-9']+")


class EnrollmentError(Exception):
    """A refused enrollment action, with the reason that was logged."""


@dataclass(frozen=True)
class EnrollmentPolicy:
    """Acceptance criteria. Defaults are deliberately conservative for a home."""
    min_samples: int = 6
    max_samples: int = 12
    margin: float = 0.03            # per-user threshold = max impostor score + margin
    floor: Optional[float] = None   # global VOICE_GATE_MIN_SCORE; the threshold never goes below it
    min_prompt_match: float = 0.6   # fraction of prompt words that must appear in the transcript
    session_ttl_s: int = 900
    # clip quality, same measures as the enrollment coach
    duration_min_s: float = 2.0
    duration_max_s: float = 8.0
    max_clipping_ratio: float = 0.01
    min_rms_dbfs: float = -38.0
    min_estimated_snr_db: float = 10.0
    max_silence_ratio: float = 0.65

    def quality_issues(self, q: dict) -> list[str]:
        """Why a clip is not usable; empty means usable. Missing metrics fail."""
        out = []
        try:
            dur = float(q["duration_s"])
            if dur < self.duration_min_s:
                out.append("clip too short: record two to eight seconds of natural speech")
            if dur > self.duration_max_s:
                out.append("clip too long: keep it under eight seconds")
            if float(q["clipping_ratio"]) > self.max_clipping_ratio:
                out.append("clipping: move a little farther from the speaker")
            if float(q["rms_dbfs"]) < self.min_rms_dbfs:
                out.append("too quiet: speak up or move closer")
            if float(q["estimated_snr_db"]) < self.min_estimated_snr_db:
                out.append("too much background noise")
            if float(q["silence_ratio"]) > self.max_silence_ratio:
                out.append("mostly silence: say the whole sentence")
        except (KeyError, TypeError, ValueError):
            out.append("clip quality could not be measured")
        return out


def prompt_match(prompt: str, transcript: str) -> float:
    """Fraction of the prompt's words present in the transcript (order-free).

    A replayed recording of the person saying something else fails this; the
    prompts are chosen per session and the clip has to say them. Not a liveness
    check: synthesis of the prompt would pass.
    """
    want = _WORD.findall(str(prompt or "").lower())
    have = set(_WORD.findall(str(transcript or "").lower()))
    if not want:
        return 0.0
    return sum(1 for w in want if w in have) / len(want)


def cosine(a: list, b: list) -> float:
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    na = math.sqrt(sum(float(a[i]) ** 2 for i in range(n)))
    nb = math.sqrt(sum(float(b[i]) ** 2 for i in range(n)))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def mean_embedding(vectors: list) -> list[float]:
    vectors = [v for v in vectors if isinstance(v, list) and v]
    if not vectors:
        return []
    dim = min(len(v) for v in vectors)
    out = [sum(float(v[i]) for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in out))
    return [x / norm for x in out] if norm > 0 else out


@dataclass
class Evaluation:
    genuine: list = field(default_factory=list)      # each sample vs the mean of the others
    impostor: list = field(default_factory=list)     # each sample vs every other live profile's mean
    threshold: Optional[float] = None
    ok: bool = False
    reasons: list = field(default_factory=list)

    def to_json(self) -> dict:
        r = lambda xs: [round(float(x), 4) for x in xs]  # noqa: E731
        return {
            "genuine_min": round(min(self.genuine), 4) if self.genuine else None,
            "genuine_mean": round(sum(self.genuine) / len(self.genuine), 4) if self.genuine else None,
            "impostor_max": round(max(self.impostor), 4) if self.impostor else None,
            "impostor_pairs": len(self.impostor),
            "threshold": None if self.threshold is None else round(float(self.threshold), 4),
            "ok": self.ok,
            "reasons": list(self.reasons),
            "genuine": r(self.genuine),
        }


def evaluate(samples: list, others: dict, policy: EnrollmentPolicy) -> Evaluation:
    """Consistency and separation for a candidate profile.

    samples: this session's embeddings. others: profile_id -> mean embedding of
    every other live profile. With no other profile enrolled there is no
    impostor score, so the threshold is the floor; the record says so.
    """
    ev = Evaluation()
    if len(samples) < policy.min_samples:
        ev.reasons.append(f"need {policy.min_samples} samples, have {len(samples)}")
        return ev
    for i, s in enumerate(samples):
        rest = samples[:i] + samples[i + 1:]
        ev.genuine.append(cosine(s, mean_embedding(rest)))
        for other_mean in others.values():
            ev.impostor.append(cosine(s, other_mean))
    if ev.impostor:
        threshold = max(ev.impostor) + policy.margin
        if policy.floor is not None:
            threshold = max(threshold, policy.floor)
    elif policy.floor is not None:
        threshold = policy.floor
        ev.reasons.append("no other profile enrolled: separation not measurable, threshold is the global floor")
    else:
        ev.reasons.append("no other profile enrolled and no global floor: no threshold can be fixed")
        return ev
    ev.threshold = round(float(threshold), 4)
    if ev.threshold >= 1.0:
        ev.reasons.append("another enrolled voice is indistinguishable from this one at any threshold")
        return ev
    low = [g for g in ev.genuine if g < ev.threshold]
    if low:
        ev.reasons.append(f"{len(low)} of {len(ev.genuine)} samples fall below the threshold {ev.threshold:.3f}")
        return ev
    ev.ok = not any("no threshold" in r for r in ev.reasons)
    return ev


# ---- sessions -----------------------------------------------------------------

@dataclass
class Sample:
    index: int
    prompt: str
    transcript: str
    match: float
    embedding: list
    quality: dict
    taken_at: float

    def to_json(self) -> dict:
        return {"index": self.index, "prompt": self.prompt, "transcript": self.transcript,
                "match": round(self.match, 3), "embedding": self.embedding, "quality": self.quality,
                "taken_at": self.taken_at}

    @staticmethod
    def from_json(d: dict) -> "Sample":
        return Sample(int(d["index"]), str(d.get("prompt", "")), str(d.get("transcript", "")),
                      float(d.get("match", 0.0)), list(d["embedding"]), dict(d.get("quality") or {}),
                      float(d.get("taken_at", 0.0)))


@dataclass
class Session:
    session_id: str
    profile_id: str
    display_name: str
    opened_by: str
    device_id: str
    device_jkt: str
    opened_at: float
    expires_at: float
    prompts: list
    consent: str
    status: str = OPEN
    samples: list = field(default_factory=list)
    next_prompt: int = 0
    evaluation: Optional[dict] = None
    closed_at: Optional[float] = None
    closed_reason: str = ""
    replace: bool = False

    @property
    def open(self) -> bool:
        return self.status in (OPEN, EVALUATING)

    def current_prompt(self) -> str:
        if not self.prompts:
            return ""
        return self.prompts[self.next_prompt % len(self.prompts)]

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id, "profile_id": self.profile_id, "display_name": self.display_name,
            "opened_by": self.opened_by, "device_id": self.device_id, "device_jkt": self.device_jkt,
            "opened_at": self.opened_at, "expires_at": self.expires_at, "prompts": list(self.prompts),
            "consent": self.consent, "status": self.status, "samples": [s.to_json() for s in self.samples],
            "next_prompt": self.next_prompt, "evaluation": self.evaluation, "closed_at": self.closed_at,
            "closed_reason": self.closed_reason, "replace": self.replace,
        }

    @staticmethod
    def from_json(d: dict) -> "Session":
        return Session(
            session_id=str(d["session_id"]), profile_id=str(d["profile_id"]),
            display_name=str(d.get("display_name", "")), opened_by=str(d.get("opened_by", "")),
            device_id=str(d.get("device_id", "")), device_jkt=str(d.get("device_jkt", "")),
            opened_at=float(d.get("opened_at", 0.0)), expires_at=float(d.get("expires_at", 0.0)),
            prompts=[str(p) for p in d.get("prompts") or []], consent=str(d.get("consent", "")),
            status=str(d.get("status", OPEN)), samples=[Sample.from_json(s) for s in d.get("samples") or []],
            next_prompt=int(d.get("next_prompt", 0)), evaluation=d.get("evaluation"),
            closed_at=d.get("closed_at"), closed_reason=str(d.get("closed_reason", "")),
            replace=bool(d.get("replace", False)),
        )


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, key: str) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


class VoiceEnrollmentService:
    """Sessions and the profile writes they end in. One instance per orchestrator."""

    def __init__(self, sessions_path: str | Path, profiles_path: str | Path,
                 policy: EnrollmentPolicy = EnrollmentPolicy(),
                 clock: Callable[[], float] = time.time):
        self.sessions_path = Path(sessions_path)
        self.profiles_path = Path(profiles_path)
        self.policy = policy
        self.clock = clock

    # -- audit

    def _audit(self, event: str, session: Optional[Session], **extra) -> None:
        rec = {"event": event, "ts": int(self.clock()), **extra}
        if session is not None:
            rec.update({"session_id": session.session_id, "profile_id": session.profile_id,
                        "device_id": session.device_id, "device_jkt": session.device_jkt,
                        "opened_by": session.opened_by, "samples": len(session.samples)})
        log.info(json.dumps(rec, sort_keys=True))

    def _refuse(self, reason: str, session: Optional[Session] = None, **extra) -> None:
        self._audit("enrollment_refused", session, reason=reason, **extra)
        raise EnrollmentError(reason)

    # -- storage

    def _sessions(self) -> dict:
        return _load_json(self.sessions_path, "sessions")

    def _save_sessions(self, sessions: dict) -> None:
        _atomic_write(self.sessions_path, {"sessions": sessions})

    def _profiles(self) -> dict:
        return _load_json(self.profiles_path, "profiles")

    def _save_profiles(self, profiles: dict) -> None:
        _atomic_write(self.profiles_path, {"profiles": profiles})

    def _put(self, session: Session) -> None:
        sessions = self._sessions()
        sessions[session.session_id] = session.to_json()
        self._save_sessions(sessions)

    def get(self, session_id: str) -> Optional[Session]:
        raw = self._sessions().get(str(session_id))
        if not isinstance(raw, dict):
            return None
        try:
            return self._expire(Session.from_json(raw))
        except (KeyError, TypeError, ValueError):
            return None

    def list(self) -> list[Session]:
        out = []
        for raw in self._sessions().values():
            try:
                out.append(self._expire(Session.from_json(raw)))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda s: s.opened_at)

    def _expire(self, session: Session) -> Session:
        if session.open and session.expires_at <= self.clock():
            session.status = EXPIRED
            session.closed_at = self.clock()
            session.closed_reason = "time limit reached"
            self._put(session)
            self._audit("enrollment_expired", session)
        return session

    def open_for_device(self, device_id: str, device_jkt: str = "") -> Optional[Session]:
        """The open session bound to this device, if any. The thumbprint, when
        given, must match too: a re-enrolled device key is a different device."""
        for s in self.list():
            if s.open and s.device_id == device_id and (not device_jkt or not s.device_jkt or s.device_jkt == device_jkt):
                return s
        return None

    # -- admin actions

    def open(self, profile_id: str, display_name: str, opened_by: str, device_id: str,
             device_jkt: str = "", consent: str = "", prompts: Optional[list] = None,
             ttl_s: Optional[int] = None, replace: bool = False) -> Session:
        pid = re.sub(r"[^a-z0-9_]", "", str(profile_id or "").strip().lower().replace(" ", "_"))
        if not pid:
            self._refuse("profile_id must be a plain token")
        if not str(device_id or "").strip():
            self._refuse("device_id required: a session is bound to one speaker")
        if not str(opened_by or "").strip():
            self._refuse("opened_by required: the record names the admin")
        profiles = self._profiles()
        if pid in profiles and not replace:
            self._refuse(f"profile {pid} exists; pass replace to re-enroll it", profile_id=pid)
        for s in self.list():
            if s.open and (s.device_id == device_id or s.profile_id == pid):
                self._refuse(f"session {s.session_id} is already open for this device or profile", s)
        now = self.clock()
        chosen = [str(p) for p in (prompts or DEFAULT_PROMPTS) if str(p).strip()]
        if len(chosen) < self.policy.min_samples:
            self._refuse(f"need at least {self.policy.min_samples} prompts, have {len(chosen)}")
        session = Session(
            session_id=secrets.token_hex(8), profile_id=pid, display_name=str(display_name or pid),
            opened_by=str(opened_by), device_id=str(device_id), device_jkt=str(device_jkt or ""),
            opened_at=now, expires_at=now + int(ttl_s or self.policy.session_ttl_s),
            prompts=chosen, consent=str(consent or ""), replace=bool(replace),
        )
        self._put(session)
        self._audit("enrollment_opened", session, display_name=session.display_name,
                    expires_at=int(session.expires_at), prompts=len(chosen), replace=replace)
        return session

    def abort(self, session_id: str, by: str = "admin", reason: str = "aborted by admin") -> Session:
        session = self.get(session_id)
        if session is None:
            self._refuse("unknown session", session_id=session_id)
        if not session.open:
            self._refuse(f"session is already {session.status}", session)
        session.status = ABORTED
        session.closed_at = self.clock()
        session.closed_reason = reason
        self._put(session)
        self._audit("enrollment_aborted", session, by=by, reason=reason)
        return session

    def revoke_profile(self, profile_id: str, by: str = "admin") -> bool:
        """Stop a profile from identifying anyone. The record stays, so the
        audit trail of what it did while live is not lost."""
        profiles = self._profiles()
        entry = profiles.get(str(profile_id))
        if not isinstance(entry, dict):
            return False
        if entry.get("revoked_at") is None:
            entry["revoked_at"] = float(self.clock())
            entry["revoked_by"] = str(by)
            profiles[str(profile_id)] = entry
            self._save_profiles(profiles)
        self._audit("profile_revoked", None, profile_id=profile_id, by=by)
        return True

    # -- the per-turn path (called by the orchestrator, never by the model)

    def add_sample(self, session_id: str, *, device_id: str, device_jkt: str, transcript: str,
                   embedding: Optional[list], quality: Optional[dict], error: str = "") -> tuple[Session, str, bool]:
        """One turn of the enrollment conversation.

        Returns (session, what to say next, accepted). The sentence is what the
        orchestrator speaks: a tip and the same prompt again, the next prompt,
        or the completion or failure message. The session file is updated here.
        """
        session = self.get(session_id)
        if session is None:
            self._refuse("unknown session", session_id=session_id)
        if not session.open:
            self._refuse(f"session is {session.status}", session)
        if device_id != session.device_id or (session.device_jkt and device_jkt != session.device_jkt):
            self._refuse("sample came from a device other than the session's", session,
                         sample_device=device_id, sample_jkt=device_jkt)
        prompt = session.current_prompt()
        if error or not isinstance(embedding, list) or not embedding:
            self._audit("enrollment_sample_rejected", session, reason=error or "no embedding")
            return session, f"I could not hear that clearly. Please say: {prompt}", False
        issues = self.policy.quality_issues(quality or {})
        if issues:
            self._audit("enrollment_sample_rejected", session, reason="quality", issues=issues)
            return session, f"{issues[0].split(':')[-1].strip().capitalize()}. Please say: {prompt}", False
        match = prompt_match(prompt, transcript)
        if match < self.policy.min_prompt_match:
            self._audit("enrollment_sample_rejected", session, reason="prompt mismatch", match=round(match, 3))
            return session, f"That did not match the sentence. Please say: {prompt}", False

        session.samples.append(Sample(index=len(session.samples), prompt=prompt, transcript=str(transcript),
                                      match=match, embedding=[float(x) for x in embedding],
                                      quality=dict(quality or {}), taken_at=self.clock()))
        session.next_prompt += 1
        self._audit("enrollment_sample_accepted", session, match=round(match, 3))

        if len(session.samples) < self.policy.min_samples:
            self._put(session)
            return session, f"Thank you. Next: {session.current_prompt()}", True

        session.status = EVALUATING
        ev = evaluate([s.embedding for s in session.samples], self._other_means(session.profile_id), self.policy)
        session.evaluation = ev.to_json()
        if ev.ok:
            self._complete(session, ev)
            return session, (f"Enrollment complete for {session.display_name}: {len(session.samples)} samples, "
                             f"threshold {ev.threshold:.2f}."), True
        if len(session.samples) >= self.policy.max_samples:
            session.status = FAILED
            session.closed_at = self.clock()
            session.closed_reason = "; ".join(ev.reasons)
            self._put(session)
            self._audit("enrollment_failed", session, reasons=ev.reasons, evaluation=session.evaluation)
            return session, ("I could not get a reliable match for this voice. Enrollment stopped; "
                             "an admin can open a new session."), True
        session.status = OPEN
        self._put(session)
        self._audit("enrollment_needs_more", session, reasons=ev.reasons)
        return session, f"A few more. Next: {session.current_prompt()}", True

    def _other_means(self, profile_id: str) -> dict:
        out = {}
        for pid, entry in self._profiles().items():
            if pid == profile_id or not isinstance(entry, dict) or entry.get("revoked_at") is not None:
                continue
            m = entry.get("mean_embedding")
            if isinstance(m, list) and m:
                out[pid] = m
        return out

    def _complete(self, session: Session, ev: Evaluation) -> None:
        profiles = self._profiles()
        samples = [s.embedding for s in session.samples]
        now = self.clock()
        profiles[session.profile_id] = {
            "display_name": session.display_name,
            "samples": samples,
            "mean_embedding": mean_embedding(samples),
            "threshold": ev.threshold,
            "enrolled_at": now,
            "enrolled_by": session.opened_by,
            "enrollment_session": session.session_id,
            "device_id": session.device_id,
            "device_jkt": session.device_jkt,
            "consent": session.consent,
            "sample_count": len(samples),
            "evaluation": {k: v for k, v in ev.to_json().items() if k != "genuine"},
            "revoked_at": None,
        }
        self._save_profiles(profiles)
        session.status = COMPLETE
        session.closed_at = now
        session.closed_reason = "criteria met"
        self._put(session)
        self._audit("enrollment_complete", session, threshold=ev.threshold, evaluation=session.evaluation)
