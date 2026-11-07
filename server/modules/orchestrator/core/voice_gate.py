"""Voice gate: decides whether a tool call may run, before the orchestrator dispatches it.

The model never supplies any input to this decision. Identity comes from the
orchestrator's own speaker check on this turn's audio, origin from the device
credential the speaker presented on the stream (or, until that is required,
the gRPC peer address), and approvals from channels outside the conversation.

Risk tiers
    read   no step-up
    write  needs a verified voice and a spoken confirmation (the user may turn
           confirmation off), or a phone approval when the voice is not verified
    high   needs a verified voice AND a phone approval; never waivable

A voice is verified when every signal passes: speaker match at or above the
threshold that applies to that person, request from a trusted origin, liveness
passed. Any missing signal counts as failed (fail closed).

Thresholds
    A profile enrolled through the admin-run session (voice_enrollment.py)
    carries its own threshold, fixed by its separation from the other voices
    in the house. The global VOICE_GATE_MIN_SCORE is the floor: the effective
    threshold is the higher of the two. With neither set, no voice verifies.

Origin
    require_device=False  the peer address must be inside trusted_speaker_nets
    require_device=True   the turn must carry a verified device credential
                          (security/DEVICE_IDENTITY.md); the address is ignored

Assurance
    Every decision carries what the gate measured, so nothing downstream can
    mistake a voice match for more than it is: method, score, the threshold
    that applied and where it came from, the device, the confirmation state.

Modes
    off       gate not evaluated
    monitor   decision computed and logged; the call always runs
    enforce   denied calls do not run
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

READ, WRITE, HIGH = "read", "write", "high"
_RISKS = {READ, WRITE, HIGH}
MODES = {"off", "monitor", "enforce"}

# Home Assistant: which device classes make a turn-on/turn-off call high-risk.
# In this deployment HassTurnOff on a lock unlocks it; HassTurnOn on a garage opens it.
_HA_HIGH_RISK_NAME = re.compile(r"\b(lock|garage|alarm|door)\b", re.IGNORECASE)
_HA_HIGH_RISK_CLASSES = {"lock", "door", "garage", "alarm"}
_HA_SWITCH_TOOLS = {"homeassistant__HassTurnOn", "homeassistant__HassTurnOff"}


@dataclass(frozen=True)
class GateConfig:
    mode: str = "monitor"
    min_score: Optional[float] = None          # global floor; None = not calibrated
    trusted_speaker_nets: tuple = ()           # CIDRs of the speaker devices (require_device=False)
    tool_risk: dict = field(default_factory=dict)
    read_actions: dict = field(default_factory=dict)  # tool -> actions that only read
    default_risk: str = WRITE                  # unknown tools are treated as writes
    require_device: bool = False               # origin = verified device credential, not address

    @staticmethod
    def build(
        mode: str,
        min_score: Optional[float],
        trusted_speaker_nets: Iterable[str],
        tool_risk: dict,
        default_risk: str = WRITE,
        read_actions: Optional[dict] = None,
        require_device: bool = False,
    ) -> "GateConfig":
        m = str(mode or "").strip().lower()
        if m not in MODES:
            m = "monitor"  # an unknown mode never silently disables the gate
        nets = []
        for raw in trusted_speaker_nets or ():
            try:
                nets.append(ipaddress.ip_network(str(raw).strip(), strict=False))
            except ValueError:
                continue
        risks = {str(k): str(v).lower() for k, v in (tool_risk or {}).items() if str(v).lower() in _RISKS}
        dr = str(default_risk).lower()
        if dr not in _RISKS:
            dr = WRITE
        score = _as_score(min_score)
        reads = {}
        for k, v in (read_actions or {}).items():
            if isinstance(v, (list, tuple, set)):
                reads[str(k)] = frozenset(str(a).strip().lower() for a in v)
        return GateConfig(mode=m, min_score=score, trusted_speaker_nets=tuple(nets), tool_risk=risks,
                          read_actions=reads, default_risk=dr, require_device=bool(require_device))


def _as_score(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class TurnSignals:
    """Facts about this turn, gathered by the orchestrator. Defaults are the failing value."""
    channel: str = ""                  # "voice" for speaker audio, "text" otherwise
    peer_host: str = ""                # from the gRPC peer, never from a header
    profile_id: str = ""
    score: Optional[float] = None
    profile_threshold: Optional[float] = None  # the enrolled person's own threshold, if any
    liveness: Optional[bool] = None    # None until the liveness check exists
    confirm_writes: bool = True        # user preference; only explicit False waives
    spoken_confirmation: bool = False  # set by the confirmation flow (phase 2)
    phone_approval: bool = False       # set by the phone approval flow (phase 2)
    device_id: str = ""                # from the verified device credential on the stream
    device_jkt: str = ""               # the device key's thumbprint (cnf.jkt), same source
    device_verified: bool = False      # credential verified at connect and not expired at this turn


@dataclass
class Decision:
    tool: str
    risk: str
    allow: bool
    enforced: bool
    reasons: list
    needs: str = ""                    # "", "spoken_confirmation" or "phone_approval"
    profile_id: str = ""
    score: Optional[float] = None
    assurance: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.enforced and not self.allow


def classify(tool: str, args: Optional[dict], cfg: GateConfig) -> str:
    """Risk tier for this call. Configured tiers win, then HA device rules, then the default."""
    if tool in cfg.tool_risk:
        risk = cfg.tool_risk[tool]
    else:
        risk = cfg.default_risk
    if isinstance(args, dict) and tool in cfg.read_actions:
        action = str(args.get("action") or "").strip().lower()
        if action and action in cfg.read_actions[tool]:
            return READ
    if tool in _HA_SWITCH_TOOLS and isinstance(args, dict):
        classes = args.get("device_classes")
        if isinstance(classes, list) and any(str(c).lower() in _HA_HIGH_RISK_CLASSES for c in classes):
            return HIGH
        for key in ("name", "area"):
            if _HA_HIGH_RISK_NAME.search(str(args.get(key) or "")):
                return HIGH
    return risk


def _host_trusted(host: str, cfg: GateConfig) -> bool:
    try:
        ip = ipaddress.ip_address(str(host).strip())
    except ValueError:
        return False
    return any(ip in net for net in cfg.trusted_speaker_nets)


def effective_threshold(sig: TurnSignals, cfg: GateConfig) -> tuple[Optional[float], str]:
    """The match threshold for this turn and where it came from.

    "profile": the person's own threshold from enrollment, at or above the floor.
    "global": the floor alone (no per-user threshold recorded).
    "": nothing calibrated; no voice can verify.
    """
    own = _as_score(sig.profile_threshold)
    floor = cfg.min_score
    if own is not None and floor is not None:
        return max(own, floor), "profile"
    if own is not None:
        return own, "profile"
    if floor is not None:
        return floor, "global"
    return None, ""


def origin_trusted(sig: TurnSignals, cfg: GateConfig) -> tuple[bool, str]:
    """Whether the request came from a trusted speaker, and which check decided it."""
    if cfg.require_device:
        return bool(sig.device_verified and sig.device_jkt), "device"
    return _host_trusted(sig.peer_host, cfg), "address"


def voice_failures(sig: TurnSignals, cfg: GateConfig) -> list:
    """Reasons the voice is not verified; empty list means verified."""
    out = []
    if sig.channel != "voice":
        out.append("request did not come from the speaker's voice channel")
        return out
    threshold, source = effective_threshold(sig, cfg)
    if threshold is None:
        out.append("speaker match threshold not calibrated")
    elif sig.score is None or not sig.profile_id:
        out.append("speaker not identified")
    elif sig.score < threshold:
        out.append(f"speaker match {sig.score:.3f} below {threshold:.3f} ({source})")
    trusted, check = origin_trusted(sig, cfg)
    if not trusted:
        if check == "device":
            out.append("request did not carry a verified device credential")
        else:
            out.append("request did not come from a trusted speaker address")
    if sig.liveness is not True:
        out.append("liveness check not passed")
    return out


def assurance(sig: TurnSignals, cfg: GateConfig, verified: bool) -> dict:
    """What the gate measured, in a form a token or an audit line can carry.

    Never more than was measured: a voice match is reported as a voice match
    with its score and threshold, the device as the device credential that was
    on the stream, and the confirmation state as it stood at this decision.
    """
    threshold, source = effective_threshold(sig, cfg)
    trusted, check = origin_trusted(sig, cfg)
    return {
        "method": "voice" if sig.channel == "voice" else sig.channel or "none",
        "verified": bool(verified),
        "profile_id": sig.profile_id,
        "score": None if sig.score is None else round(float(sig.score), 4),
        "threshold": threshold,
        "threshold_source": source,
        "origin": check,
        "origin_trusted": bool(trusted),
        "device_id": sig.device_id,
        "device_jkt": sig.device_jkt,
        "liveness": sig.liveness,
        "spoken_confirmation": bool(sig.spoken_confirmation),
        "phone_approval": bool(sig.phone_approval),
    }


def decide(tool: str, args: Optional[dict], sig: TurnSignals, cfg: GateConfig) -> Decision:
    risk = classify(tool, args, cfg)
    enforced = cfg.mode == "enforce"
    base = dict(tool=tool, risk=risk, enforced=enforced, profile_id=sig.profile_id, score=sig.score)

    if risk == READ:
        return Decision(allow=True, reasons=[], assurance=assurance(sig, cfg, False), **base)

    failures = voice_failures(sig, cfg)
    verified = not failures
    assured = assurance(sig, cfg, verified)

    if risk == HIGH:
        reasons = list(failures)
        if not sig.phone_approval:
            reasons.append("high-risk action needs phone approval")
        return Decision(allow=not reasons, reasons=reasons,
                        needs="" if sig.phone_approval else "phone_approval", assurance=assured, **base)

    # write
    if verified:
        if sig.confirm_writes is False or sig.spoken_confirmation:
            return Decision(allow=True, reasons=[], assurance=assured, **base)
        return Decision(allow=False, reasons=["write needs a spoken confirmation"],
                        needs="spoken_confirmation", assurance=assured, **base)
    if sig.phone_approval:
        return Decision(allow=True, reasons=[], assurance=assured, **base)
    return Decision(allow=False, reasons=failures + ["voice not verified, so the write needs phone approval"],
                    needs="phone_approval", assurance=assured, **base)


def audit_fields(d: Decision) -> dict:
    a = d.assurance or {}
    return {
        "gate_tool": d.tool,
        "gate_risk": d.risk,
        "gate_decision": "allow" if d.allow else ("deny" if d.enforced else "would_deny"),
        "gate_needs": d.needs,
        "gate_profile": d.profile_id,
        "gate_score": None if d.score is None else round(d.score, 4),
        "gate_threshold": a.get("threshold"),
        "gate_threshold_source": a.get("threshold_source", ""),
        "gate_origin": a.get("origin", ""),
        "gate_device": a.get("device_id", ""),
        "gate_device_jkt": a.get("device_jkt", ""),
        "gate_reasons": "; ".join(d.reasons),
    }
