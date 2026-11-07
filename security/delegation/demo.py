"""End-to-end run of prototype A, in process. No servers or network needed.

    python demo.py

Prints the user > gateway > sub-agent chain as the tool sees it, then each
attack and the reason it was refused, then revocation: the user's token is
revoked at /revoke and every token exchanged from it dies with it; a tool
server reports tokens it was presented and the STS escalates by its own policy;
the ops-console retires an agent and revokes a key through the admin routes.

Every call to the STS and to the tool carries a DPoP proof (RFC 9449) signed
with the caller's key, and every token is bound to a key (cnf.jkt). The attack
table shows what a copied token is worth without that key.
"""
from __future__ import annotations

import json
import time

import jwt
from fastapi.testclient import TestClient

from dpop import ath, make_proof
from sts import ACCESS_TOKEN_TYPE, STS, TOKEN_EXCHANGE, create_app
from verify import DelegationRejected, UpstreamVerifier

ISS = "http://sts.local"
TOOL_URL = "https://tools.local/mcp"
sts = STS(issuer=ISS)
http = TestClient(create_app(sts))
TOKEN_URL = str(http.base_url).rstrip("/") + "/token"
# The tool verifier shares the STS denylist in process (the pushed deny list in
# DIAGRAMS.md section 4 is the target; this is the demo stand-in).
github = UpstreamVerifier(ISS, "tool-github", key=sts.public_key, revocation=sts.denylist)


def proof(cid: str, htu: str = TOKEN_URL, token: str | None = None, **kw) -> str:
    """A DPoP proof from cid's key for one request; ath binds it to the token it accompanies."""
    return make_proof(sts.clients[cid].key, "POST", htu, ath=ath(token) if token else None, **kw)


def holder(token: str) -> str:
    """Which client's key the token is bound to."""
    jkt = jwt.decode(token, options={"verify_signature": False}).get("cnf", {}).get("jkt")
    return next(c.client_id for c in sts.clients.values() if c.jkt == jkt)


def actor(cid: str) -> str:
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sts.clients[cid].secret},
                  headers={"DPoP": proof(cid)})
    return r.json()["access_token"]


def exchange(cid: str, subject: str, with_actor: bool = True, actor_tok: str | None = None, dpop: str | None = "", **extra) -> dict:
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
    headers = {} if dpop is None else {"DPoP": dpop or proof(cid)}
    return http.post("/token", data=data, headers=headers).json()


def call(token: str, as_client: str | None = None, dpop: str | None = "", htu: str = TOOL_URL, **kw):
    """Present a token at tool-github the way its holder would: with a fresh
    proof over this token for this URI. as_client signs with another client's
    key (a thief with its own key); dpop=None sends no proof at all."""
    if dpop == "":
        dpop = proof(as_client or holder(token), htu, token, **kw)
    return github.verify(token, dpop_proof=dpop, htm="POST", htu=TOOL_URL)


def revoke(cid: str, token: str) -> int:
    r = http.post("/revoke", data={"token": token, "client_id": cid, "client_secret": sts.clients[cid].secret})
    return r.status_code


def show(title: str, token: str) -> None:
    c = jwt.decode(token, options={"verify_signature": False})
    keep = {k: c[k] for k in ("sub", "aud", "scope", "act", "may_act") if k in c}
    keep["ttl_s"] = c["exp"] - c["iat"]
    keep["bound_to"] = f"{holder(token)} (cnf.jkt {c['cnf']['jkt'][:8]})"
    keep["lineage"] = {k: c[k][:8] for k in ("jti", "parent_jti", "grant_id") if k in c}
    print(f"\n{title}\n{json.dumps(keep, indent=2)}")


# ---- happy path -------------------------------------------------------------
alice = sts.issue_user_token("alice", "mcp-gateway", {"repo:read", "repo:write", "issues:read"})
show("1. Alice's token after consenting to mcp-gateway", alice)

handoff = exchange("mcp-gateway", alice, delegate_to="code-review-subagent", scope="repo:read")["access_token"]
show("2. Gateway's handoff token for the sub-agent (not usable at a tool)", handoff)

tool_tok = exchange("code-review-subagent", handoff, audience="tool-github")["access_token"]
show("3. Sub-agent's tool token for tool-github", tool_tok)

p = call(tool_tok)
print(f"\ntool-github accepts: {p.path}  scopes={sorted(p.scopes)}  caller={p.caller}  key={p.jkt[:8]}")
print("(each call above carried a DPoP proof: typ dpop+jwt, ES256, the caller's public key in the header, htm, htu, iat, jti, and ath over the token at the tool)")

# ---- attacks ----------------------------------------------------------------
results: list[tuple[str, str]] = []


def tool_attempt(name: str, token: str, **how) -> None:
    try:
        call(token, **how)
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

# Sender constraint: the tool token is copied out of the sub-agent's process.
tool_attempt("Stolen tool token, no proof (plain bearer use)", tool_tok, dpop=None)
tool_attempt("Stolen tool token, notes-agent signs with its own key", tool_tok, as_client="notes-agent")
captured = proof("code-review-subagent", TOOL_URL, tool_tok)
call(tool_tok, dpop=captured)  # the legitimate call the thief was watching
tool_attempt("Stolen token and its captured proof replayed", tool_tok, dpop=captured)
tool_attempt("Proof minted for tool-jira's URI, sent to tool-github", tool_tok, htu="https://tools.local/jira")
tool_attempt("Proof older than 60 s", tool_tok, now=int(time.time()) - 90)
sts_attempt("Exchange with no DPoP proof", exchange("mcp-gateway", alice, audience="tool-github", dpop=None))
sts_attempt("Gateway's actor token exchanged under notes-agent's key", exchange("mcp-gateway", alice, audience="tool-github", dpop=proof("notes-agent")))

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
    call(tool_tok)
    revs.append(("Sub-agent's live tool token replayed", "ACCEPTED (bad)"))
except DelegationRejected as e:
    revs.append(("Sub-agent's live tool token replayed", f"refused at tool: {e}"))
again = exchange("code-review-subagent", handoff, audience="tool-github")
revs.append(("Sub-agent exchanges the handoff again", f"refused at STS: {again['error_description']}" if "error" in again else "ISSUED (bad)"))
revs.append(("Bob's token, exchanged from a different grant", f"still accepted: {call(bob_direct).path}"))

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
        call(token)
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
# The actor token is key-bound, so it goes under the DPoP scheme with a proof
# over it (ath) for the admin route's URI.
adm: list[tuple[str, str]] = []
ops = actor("ops-console")
gw_actor = actor("mcp-gateway")


def admin_post(path: str, token: str, as_client: str, **form) -> object:
    htu = str(http.base_url).rstrip("/") + path
    return http.post(path, data=form, headers={"Authorization": f"DPoP {token}", "DPoP": proof(as_client, htu, token)})


r = admin_post("/admin/revoke/client", gw_actor, "mcp-gateway", client_id="mcp-gateway")
adm.append(("Gateway's own actor token at /admin/revoke/client", f"HTTP {r.status_code} {r.json()['error']}"))
r = http.post("/admin/revoke/client", data={"client_id": "mcp-gateway"}, headers={"Authorization": f"Bearer {ops}"})
adm.append(("ops-console's bound token sent as a plain Bearer", f"HTTP {r.status_code} {r.json()['error']}"))

# Revoke a key: the sub-agent's. Every token bound to it dies and its proofs are refused.
carol = sts.issue_user_token("carol", "mcp-gateway", {"repo:read"})
carol_handoff = exchange("mcp-gateway", carol, delegate_to="code-review-subagent", scope="repo:read")["access_token"]
carol_tool = exchange("code-review-subagent", carol_handoff, audience="tool-github")["access_token"]
carol_direct = exchange("mcp-gateway", carol, audience="tool-github")["access_token"]
sa_actor = actor("code-review-subagent")  # minted before the revocation, still unexpired
sa_jkt = sts.clients["code-review-subagent"].jkt
r = admin_post("/admin/revoke/key", ops, "ops-console", jkt=sa_jkt)
adm.append(("ops-console revokes the sub-agent's key", f"HTTP {r.status_code} {json.dumps(r.json(), sort_keys=True)}"))
adm.append(("Sub-agent's tool token, right key, after key revocation", live(carol_tool)))
again = exchange("code-review-subagent", carol_handoff, actor_tok=sa_actor, audience="tool-github")
adm.append(("Sub-agent exchanges its handoff with the revoked key", f"refused at STS: {again['error']}: {again['error_description']}" if "error" in again else "ISSUED (bad)"))
r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "code-review-subagent", "client_secret": sts.clients["code-review-subagent"].secret},
              headers={"DPoP": proof("code-review-subagent")})
adm.append(("Sub-agent asks for a new actor token with that key", f"refused at STS: {r.json()['error_description']}"))
adm.append(("Gateway's own token for the same user, other key", live(carol_direct)))

r = admin_post("/admin/revoke/client", ops, "ops-console", client_id="mcp-gateway")
adm.append(("ops-console retires mcp-gateway", f"HTTP {r.status_code} {json.dumps(r.json(), sort_keys=True)}"))
r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": sts.clients["mcp-gateway"].secret},
              headers={"DPoP": proof("mcp-gateway")})
adm.append(("mcp-gateway asks for a new actor token", f"refused at STS: {r.json()['error_description']}"))
r = admin_post("/admin/revoke/grant", ops, "ops-console", grant_id="no-such-grant")
adm.append(("ops-console revokes an unknown grant", f"HTTP {r.status_code} {json.dumps(r.json(), sort_keys=True)} (no probing)"))

print("\nAdmin (ops-console actor token under the DPoP scheme)")
w = max(len(n) for n, _ in adm)
for name, outcome in adm:
    print(f"  {name:<{w}}  {outcome}")

exchanges = [e for e in sts.audit_log if e["event"] == "token_exchange"]
revocations = [e for e in sts.audit_log if e["event"].endswith("_revocation")]
steps = [e for e in sts.audit_log if e["event"] == "revocation_escalation"]
dpop_denials = [e for e in sts.audit_log if e.get("error") == "invalid_dpop_proof"]
print(f"\nAudit log: {len(exchanges)} exchange decisions ({sum(e['decision'] == 'deny' for e in exchanges)} denials), "
      f"{len(revocations)} revocation decisions and {len(steps)} escalation steps recorded, each with a reason; "
      f"{len(dpop_denials)} DPoP refusals at the STS.")
