"""Voice gate: decides whether a tool call may run, before the orchestrator dispatches it.

The model never supplies any input to this decision. Identity comes from the
orchestrator's own speaker check on this turn's audio, network origin from the
gRPC peer address, and approvals from channels outside the conversation.

Risk tiers
    read   no step-up
    write  needs a verified voice and a spoken confirmation (the user may turn
           confirmation off), or a phone approval when the voice is not verified
    high   needs a verified voice AND a phone approval; never waivable

A voice is verified when every signal passes: speaker match at or above the
calibrated threshold, request from a trusted speaker address, liveness passed.
Any missing signal counts as failed (fail closed).

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
    min_score: Optional[float] = None          # None = not calibrated: no voice can verify
    trusted_speaker_nets: tuple = ()           # CIDRs of the speaker devices
    tool_risk: dict = field(default_factory=dict)
    read_actions: dict = field(default_factory=dict)  # tool -> actions that only read
    default_risk: str = WRITE                  # unknown tools are treated as writes

    @staticmethod
    def build(
        mode: str,
        min_score: Optional[float],
        trusted_speaker_nets: Iterable[str],
        tool_risk: dict,
        default_risk: str = WRITE,
        read_actions: Optional[dict] = None,
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
        score = None
        if min_score is not None:
            try:
                score = float(min_score)
            except (TypeError, ValueError):
                score = None
        reads = {}
        for k, v in (read_actions or {}).items():
            if isinstance(v, (list, tuple, set)):
                reads[str(k)] = frozenset(str(a).strip().lower() for a in v)
        return GateConfig(mode=m, min_score=score, trusted_speaker_nets=tuple(nets), tool_risk=risks,
                          read_actions=reads, default_risk=dr)


@dataclass
class TurnSignals:
    """Facts about this turn, gathered by the orchestrator. Defaults are the failing value."""
    channel: str = ""                  # "voice" for speaker audio, "text" otherwise
    peer_host: str = ""                # from the gRPC peer, never from a header
    profile_id: str = ""
    score: Optional[float] = None
    liveness: Optional[bool] = None    # None until the liveness check exists
    confirm_writes: bool = True        # user preference; only explicit False waives
    spoken_confirmation: bool = False  # set by the confirmation flow (phase 2)
    phone_approval: bool = False       # set by the phone approval flow (phase 2)


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


def voice_failures(sig: TurnSignals, cfg: GateConfig) -> list:
    """Reasons the voice is not verified; empty list means verified."""
    out = []
    if sig.channel != "voice":
        out.append("request did not come from the speaker's voice channel")
        return out
    if cfg.min_score is None:
        out.append("speaker match threshold not calibrated")
    elif sig.score is None or not sig.profile_id:
        out.append("speaker not identified")
    elif sig.score < cfg.min_score:
        out.append(f"speaker match {sig.score:.3f} below {cfg.min_score:.3f}")
    if not _host_trusted(sig.peer_host, cfg):
        out.append("request did not come from a trusted speaker address")
    if sig.liveness is not True:
        out.append("liveness check not passed")
    return out


def decide(tool: str, args: Optional[dict], sig: TurnSignals, cfg: GateConfig) -> Decision:
    risk = classify(tool, args, cfg)
    enforced = cfg.mode == "enforce"
    base = dict(tool=tool, risk=risk, enforced=enforced, profile_id=sig.profile_id, score=sig.score)

    if risk == READ:
        return Decision(allow=True, reasons=[], **base)

    failures = voice_failures(sig, cfg)
    verified = not failures

    if risk == HIGH:
        reasons = list(failures)
        if not sig.phone_approval:
            reasons.append("high-risk action needs phone approval")
        return Decision(allow=not reasons, reasons=reasons,
                        needs="" if sig.phone_approval else "phone_approval", **base)

    # write
    if verified:
        if sig.confirm_writes is False or sig.spoken_confirmation:
            return Decision(allow=True, reasons=[], **base)
        return Decision(allow=False, reasons=["write needs a spoken confirmation"],
                        needs="spoken_confirmation", **base)
    if sig.phone_approval:
        return Decision(allow=True, reasons=[], **base)
    return Decision(allow=False, reasons=failures + ["voice not verified, so the write needs phone approval"],
                    needs="phone_approval", **base)


def audit_fields(d: Decision) -> dict:
    return {
        "gate_tool": d.tool,
        "gate_risk": d.risk,
        "gate_decision": "allow" if d.allow else ("deny" if d.enforced else "would_deny"),
        "gate_needs": d.needs,
        "gate_profile": d.profile_id,
        "gate_score": None if d.score is None else round(d.score, 4),
        "gate_reasons": "; ".join(d.reasons),
    }
