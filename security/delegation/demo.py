"""End-to-end run of prototype A, in process. No servers or network needed.

    python demo.py

Prints the user > gateway > sub-agent chain as the tool sees it, then each
attack and the reason it was refused, then revocation: the user's token is
revoked at /revoke and every token exchanged from it dies with it; a tool
server reports tokens it was presented and the STS escalates by its own policy;
the ops-console retires an agent through the admin routes.
"""
from __future__ import annotations

import json
import time

import jwt
from fastapi.testclient import TestClient

from sts import ACCESS_TOKEN_TYPE, STS, TOKEN_EXCHANGE, create_app
from verify import DelegationRejected, UpstreamVerifier

ISS = "http://sts.local"
sts = STS(issuer=ISS)
http = TestClient(create_app(sts))
# The tool verifier shares the STS denylist in process (the pushed deny list in
# DIAGRAMS.md section 4 is the target; this is the demo stand-in).
github = UpstreamVerifier(ISS, "tool-github", key=sts.public_key, revocation=sts.denylist)


def actor(cid: str) -> str:
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sts.clients[cid].secret})
    return r.json()["access_token"]


def exchange(cid: str, subject: str, with_actor: bool = True, actor_tok: str | None = None, **extra) -> dict:
    data = {
        "grant_type": TOKEN_EXCHANGE,
        "client_id": cid,
        "client_secret": sts.clients[cid].secret,
        "subject_token": subject,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        **extra,
    }
    if with_actor:
        data["actor_token"] = actor_tok or actor(cid)
        data["actor_token_type"] = ACCESS_TOKEN_TYPE
    return http.post("/token", data=data).json()


def revoke(cid: str, token: str) -> int:
    r = http.post("/revoke", data={"token": token, "client_id": cid, "client_secret": sts.clients[cid].secret})
    return r.status_code


def show(title: str, token: str) -> None:
    c = jwt.decode(token, options={"verify_signature": False})
    keep = {k: c[k] for k in ("sub", "aud", "scope", "act", "may_act") if k in c}
    keep["ttl_s"] = c["exp"] - c["iat"]
    keep["lineage"] = {k: c[k][:8] for k in ("jti", "parent_jti", "grant_id") if k in c}
    print(f"\n{title}\n{json.dumps(keep, indent=2)}")


# ---- happy path -------------------------------------------------------------
alice = sts.issue_user_token("alice", "mcp-gateway", {"repo:read", "repo:write", "issues:read"})
show("1. Alice's token after consenting to mcp-gateway", alice)

handoff = exchange("mcp-gateway", alice, delegate_to="code-review-subagent", scope="repo:read")["access_token"]
show("2. Gateway's handoff token for the sub-agent (not usable at a tool)", handoff)

tool_tok = exchange("code-review-subagent", handoff, audience="tool-github")["access_token"]
show("3. Sub-agent's tool token for tool-github", tool_tok)

p = github.verify(tool_tok)
print(f"\ntool-github accepts: {p.path}  scopes={sorted(p.scopes)}  caller={p.caller}")

# ---- attacks ----------------------------------------------------------------
results: list[tuple[str, str]] = []


def tool_attempt(name: str, token: str) -> None:
    try:
        github.verify(token)
        results.append((name, "ACCEPTED (bad)"))
    except DelegationRejected as e:
        results.append((name, f"refused at tool: {e}"))


def sts_attempt(name: str, resp: dict) -> None:
    if "access_token" in resp:
        results.append((name, "ISSUED (bad)"))
    else:
        results.append((name, f"refused at STS: {resp['error']}: {resp['error_description']}"))


sts.clients["mcp-gateway"].ttl = 1
short = exchange("mcp-gateway", alice, audience="tool-github", scope="repo:read")["access_token"]
sts.clients["mcp-gateway"].ttl = 300
time.sleep(2.1)
tool_attempt("Expired tool token replayed", short)

no_consent = sts.issue_user_token("alice", "mcp-gateway", {"repo:read"}, consent=False)
sts_attempt("Exchange without may_act (no consent)", exchange("mcp-gateway", no_consent, audience="tool-github"))
sts_attempt("Different agent presents Alice's token", exchange("notes-agent", alice, audience="tool-fs"))
sts_attempt("Confused deputy: sub-agent asks for repo:write", exchange("code-review-subagent", handoff, audience="tool-github", scope="repo:write"))
sts_attempt("Confused deputy: sub-agent asks for tool-jira", exchange("code-review-subagent", handoff, audience="tool-jira"))
sts_attempt("Plain exchange to strip the act chain", exchange("code-review-subagent", handoff, with_actor=False, audience="tool-github"))
sts_attempt("Gateway uses notes-agent's actor token", exchange("mcp-gateway", alice, actor_tok=actor("notes-agent"), audience="tool-github"))
tool_attempt("Handoff token sent straight to a tool", handoff)
results.append(("Prompt-injected write call", "covered by C (policy check before tools/call)"))

print("\nAttacks")
w = max(len(n) for n, _ in results)
for name, outcome in results:
    print(f"  {name:<{w}}  {outcome}")

# ---- revocation -------------------------------------------------------------
# RFC 7009 /revoke plus the lineage extension: revoking the user's token revokes
# its grant, so the handoff and the tool token exchanged from it are dead too.
# A sibling branch from a different user token is untouched.
revs: list[tuple[str, str]] = []
bob = sts.issue_user_token("bob", "mcp-gateway", {"repo:read"})
bob_direct = exchange("mcp-gateway", bob, audience="tool-github")["access_token"]

revs.append(("notes-agent revokes Alice's token (not in her chain)", f"HTTP {revoke('notes-agent', alice)} unauthorized_client"))
revs.append(("Gateway revokes a token this STS never issued", f"HTTP {revoke('mcp-gateway', 'not-a-token')} (no probing)"))
revs.append(("Gateway revokes Alice's user token", f"HTTP {revoke('mcp-gateway', alice)}, denylist now {len(sts.denylist)} entries"))
try:
    github.verify(tool_tok)
    revs.append(("Sub-agent's live tool token replayed", "ACCEPTED (bad)"))
except DelegationRejected as e:
    revs.append(("Sub-agent's live tool token replayed", f"refused at tool: {e}"))
again = exchange("code-review-subagent", handoff, audience="tool-github")
revs.append(("Sub-agent exchanges the handoff again", f"refused at STS: {again['error_description']}" if "error" in again else "ISSUED (bad)"))
revs.append(("Bob's token, exchanged from a different grant", f"still accepted: {github.verify(bob_direct).path}"))

print("\nRevocation")
w = max(len(n) for n, _ in revs)
for name, outcome in revs:
    print(f"  {name:<{w}}  {outcome}")

# ---- tool reports and escalation ----------------------------------------------
# tool-github is a client with no audiences and no scopes: it can authenticate at
# /revoke and report the token it was presented, nothing above it. The STS owns
# the escalation: strikes per (grant, actor chain) inside a 10 minute window.
reps: list[tuple[str, str]] = []


def report(token: str, reason: str) -> int:
    r = http.post("/revoke", data={"token": token, "reason": reason, "client_id": "tool-github", "client_secret": sts.clients["tool-github"].secret})
    return r.status_code


def live(token: str) -> str:
    try:
        github.verify(token)
        return "live"
    except DelegationRejected as e:
        return f"refused: {e}"


bob_handoff = exchange("mcp-gateway", bob, delegate_to="code-review-subagent", scope="repo:read")["access_token"]
b1, b2, b3 = (exchange("code-review-subagent", bob_handoff, audience="tool-github")["access_token"] for _ in range(3))
reps.append(("tool-github tries to revoke the handoff above it", f"HTTP {report(bob_handoff, 'injection_suspected')} unauthorized_client"))
reps.append(("Strike 1: injection_suspected on sub-agent token b1", f"HTTP {report(b1, 'injection_suspected')}; b1 {live(b1)}; sibling b2 {live(b2)}"))
reps.append(("Strike 2: anomaly on b2", f"HTTP {report(b2, 'anomaly')}; never-reported sibling b3 {live(b3)}; gateway's own token {live(bob_direct)}"))
bob_handoff2 = exchange("mcp-gateway", bob, delegate_to="code-review-subagent", scope="repo:read")["access_token"]
b4 = exchange("code-review-subagent", bob_handoff2, audience="tool-github")["access_token"]
reps.append(("Strike 3: injection_suspected on b4 (fresh handoff, same chain)", f"HTTP {report(b4, 'injection_suspected')}; gateway's own token {live(bob_direct)}"))
again = exchange("mcp-gateway", bob, audience="tool-github")
reps.append(("Gateway exchanges Bob's token again", f"refused at STS: {again['error_description']}" if "error" in again else "ISSUED (bad)"))

print("\nTool reports and escalation")
w = max(len(n) for n, _ in reps)
for name, outcome in reps:
    print(f"  {name:<{w}}  {outcome}")

# ---- admin through the ops-console client --------------------------------------
adm: list[tuple[str, str]] = []
ops = actor("ops-console")
gw_actor = actor("mcp-gateway")
r = http.post("/admin/revoke/client", data={"client_id": "mcp-gateway"}, headers={"Authorization": f"Bearer {gw_actor}"})
adm.append(("Gateway's own actor token at /admin/revoke/client", f"HTTP {r.status_code} {r.json()['error']}"))
r = http.post("/admin/revoke/client", data={"client_id": "mcp-gateway"}, headers={"Authorization": f"Bearer {ops}"})
adm.append(("ops-console retires mcp-gateway", f"HTTP {r.status_code} {json.dumps(r.json(), sort_keys=True)}"))
r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": sts.clients["mcp-gateway"].secret})
adm.append(("mcp-gateway asks for a new actor token", f"refused at STS: {r.json()['error_description']}"))
r = http.post("/admin/revoke/grant", data={"grant_id": "no-such-grant"}, headers={"Authorization": f"Bearer {ops}"})
adm.append(("ops-console revokes an unknown grant", f"HTTP {r.status_code} {json.dumps(r.json(), sort_keys=True)} (no probing)"))

print("\nAdmin (ops-console actor token as bearer)")
w = max(len(n) for n, _ in adm)
for name, outcome in adm:
    print(f"  {name:<{w}}  {outcome}")

exchanges = [e for e in sts.audit_log if e["event"] == "token_exchange"]
revocations = [e for e in sts.audit_log if e["event"].endswith("_revocation")]
steps = [e for e in sts.audit_log if e["event"] == "revocation_escalation"]
print(f"\nAudit log: {len(exchanges)} exchange decisions ({sum(e['decision'] == 'deny' for e in exchanges)} denials), "
      f"{len(revocations)} revocation decisions and {len(steps)} escalation steps recorded, each with a reason.")
