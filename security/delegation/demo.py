"""End-to-end run of prototype A, in process. No servers or network needed.

    python demo.py

Prints the user > gateway > sub-agent chain as the tool sees it, then each
attack and the reason it was refused, then a revocation: the user's token is
revoked at /revoke and every token exchanged from it dies with it.
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
bob_tool = exchange("mcp-gateway", bob, audience="tool-github")["access_token"]

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
revs.append(("Bob's token, exchanged from a different grant", f"still accepted: {github.verify(bob_tool).path}"))
sts.revoke_client("mcp-gateway")
try:
    github.verify(bob_tool)
    revs.append(("Admin retires mcp-gateway: Bob's token", "ACCEPTED (bad)"))
except DelegationRejected as e:
    revs.append(("Admin retires mcp-gateway: Bob's token", f"refused at tool: {e}"))
r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": sts.clients["mcp-gateway"].secret})
revs.append(("Admin retires mcp-gateway: new actor token", f"refused at STS: {r.json()['error_description']}"))

print("\nRevocation")
w = max(len(n) for n, _ in revs)
for name, outcome in revs:
    print(f"  {name:<{w}}  {outcome}")

exchanges = [e for e in sts.audit_log if e["event"] == "token_exchange"]
revocations = [e for e in sts.audit_log if e["event"].endswith("_revocation")]
print(f"\nAudit log: {len(exchanges)} exchange decisions ({sum(e['decision'] == 'deny' for e in exchanges)} denials) "
      f"and {len(revocations)} revocation decisions recorded, each with a reason.")
