"""Policy enforcement point for MCP tools/call (prototype C).

Runs after the delegated token is verified (prototype A) and before dispatch.
Builds one AuthZEN evaluation request for the whole delegation chain, asks the
PDP, and fails closed on anything other than an explicit allow: timeout, PDP
down, malformed answer. Every decision is logged with its reasons.

Write-class tools need the user's approval for that exact call (tool plus
arguments). Approvals come from the user through a channel the model cannot
write to, and each one is good for a single call.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("pep.audit")


def call_fingerprint(user: str, tool: str, args: dict) -> str:
    blob = json.dumps({"user": user, "tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class ApprovalStore:
    """Out-of-band user approvals. One approval covers one exact call, once."""
    _pending: set[str] = field(default_factory=set)

    def approve(self, user: str, tool: str, args: dict) -> None:
        self._pending.add(call_fingerprint(user, tool, args))

    def consume(self, user: str, tool: str, args: dict) -> bool:
        fp = call_fingerprint(user, tool, args)
        if fp in self._pending:
            self._pending.discard(fp)
            return True
        return False


@dataclass
class Decision:
    allow: bool
    reasons: list[str]
    request_id: str
    fail_closed: bool = False


class PEP:
    def __init__(
        self,
        pdp: httpx.Client,
        catalog: dict,
        session_resources: list[str] | None = None,
        approvals: ApprovalStore | None = None,
        timeout_s: float = 0.5,
        clock=time.time,
    ):
        self.pdp = pdp
        self.catalog = catalog  # tools section of data.json, used only to find the resource argument
        self.session_resources = session_resources or []
        self.approvals = approvals or ApprovalStore()
        self.timeout_s = timeout_s
        self.clock = clock
        self.audit_log: list[dict] = []

    def build_request(
        self,
        principal,
        tool: str,
        args: dict,
        approved: bool,
        channel: str = "api",
        voice: dict | None = None,
        prefs: dict | None = None,
    ) -> dict:
        resource_arg = self.catalog.get(tool, {}).get("resource_arg")
        context: dict = {
            "channel": channel,
            "token_age_s": int(self.clock()) - principal.issued_at,
            "user_approved": approved,
            "session_resources": self.session_resources,
        }
        if voice is not None:
            # Signals from the speaker front end, never from the model.
            context["voice"] = {k: voice[k] for k in ("confidence", "local_network", "liveness") if k in voice}
        if prefs is not None:
            context["prefs"] = {k: prefs[k] for k in ("confirm_writes",) if k in prefs}
        return {
            "subject": {
                "type": "user",
                "id": principal.user,
                "properties": {"actors": list(principal.actors), "scopes": sorted(principal.scopes)},
            },
            "action": {"name": tool},
            "resource": {"type": resource_arg or "unknown", "id": str(args.get(resource_arg, "")) if resource_arg else ""},
            "context": context,
        }

    def authorize(
        self,
        principal,
        tool: str,
        args: dict,
        channel: str = "api",
        voice: dict | None = None,
        prefs: dict | None = None,
    ) -> Decision:
        request_id = uuid.uuid4().hex[:12]
        approved = False
        if self.catalog.get(tool, {}).get("class") == "write":
            approved = self.approvals.consume(principal.user, tool, args)
        body = self.build_request(principal, tool, args, approved, channel, voice, prefs)
        started = time.perf_counter()
        try:
            r = self.pdp.post("/access/v1/evaluation", json=body, timeout=self.timeout_s)
            if r.status_code != 200:
                raise ValueError(f"PDP status {r.status_code}")
            out = r.json()
            if not isinstance(out.get("decision"), bool):
                raise ValueError("PDP answer has no boolean decision")
            reasons = out.get("context", {}).get("reason_admin", {}).get("reasons", [])
            d = Decision(out["decision"], list(reasons), request_id)
        except (httpx.HTTPError, ValueError) as e:
            d = Decision(False, [f"fail closed: {type(e).__name__}: {e}"], request_id, fail_closed=True)
        self._audit(principal, tool, body, d, time.perf_counter() - started)
        return d

    def _audit(self, principal, tool: str, body: dict, d: Decision, elapsed: float) -> None:
        event = {
            "ts": int(self.clock()),
            "event": "tool_call_decision",
            "request_id": d.request_id,
            "path": principal.path,
            "tool": tool,
            "resource": body["resource"]["id"],
            "decision": "allow" if d.allow else "deny",
            "reasons": d.reasons,
            "fail_closed": d.fail_closed,
            "ms": round(elapsed * 1000, 1),
        }
        self.audit_log.append(event)
        log.info(json.dumps(event, sort_keys=True))
