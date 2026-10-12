"""End-to-end run of prototype A, in process. No servers or network needed.

    python demo.py

Prints the user > gateway > sub-agent chain as the tool sees it, then each
attack and the reason it was refused.
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
github = UpstreamVerifier(ISS, "tool-github", key=sts.public_key)


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


def show(title: str, token: str) -> None:
    c = jwt.decode(token, options={"verify_signature": False})
    keep = {k: c[k] for k in ("sub", "aud", "scope", "act", "may_act") if k in c}
    keep["ttl_s"] = c["exp"] - c["iat"]
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

print(f"\nAudit log: {len(sts.audit_log)} exchange decisions recorded, "
      f"{sum(e['decision'] == 'deny' for e in sts.audit_log)} denials, each with a reason.")
