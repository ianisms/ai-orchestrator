"""Tool reports with STS-owned escalation, and admin revocation through the
ops-console client. Builds on the helpers in test_delegation and test_revocation."""
import time

import jwt
import pytest
from fastapi.testclient import TestClient

import admin as admin_cli
from dpop import ath, make_proof
from revocation import EscalationPolicy
from sts import ADMIN_SCOPE, STS, create_app
from test_delegation import ISS, actor_token, exchange, holder_of, secret, user_token
from test_revocation import checked_tool, claims, full_chain, revoke
from verify import DelegationRejected


@pytest.fixture
def sts():
    return STS(issuer=ISS)


@pytest.fixture
def http(sts):
    return TestClient(create_app(sts))


def report(http, sts, cid, token, reason):
    return http.post("/revoke", data={"token": token, "reason": reason, "client_id": cid, "client_secret": secret(sts, cid)})


def subagent_tools(http, sts, user, n):
    """One handoff gateway > sub-agent and n sibling tool tokens under it."""
    handoff = exchange(http, sts, "mcp-gateway", user, delegate_to="code-review-subagent", scope="repo:read").json()["access_token"]
    tools = [exchange(http, sts, "code-review-subagent", handoff, audience="tool-github").json()["access_token"] for _ in range(n)]
    return handoff, tools


def admin_call(http, sts, path, bearer=None, **form):
    """Call an admin route with a token. A key-bound token whose holder we are
    goes under the DPoP scheme with a proof over it; anything else goes as Bearer."""
    headers = {}
    if bearer:
        holder = holder_of(sts, bearer)
        if holder is not None:
            htu = str(http.base_url).rstrip("/") + path
            headers = {"Authorization": f"DPoP {bearer}", "DPoP": make_proof(holder.key, "POST", htu, ath=ath(bearer))}
        else:
            headers = {"Authorization": f"Bearer {bearer}"}
    return http.post(path, data=form, headers=headers)


def escalations(sts):
    return [(e["strikes"], e["step"]) for e in sts.audit_log if e["event"] == "revocation_escalation"]


# ---- A. tool reports and escalation ------------------------------------------

def test_tool_server_can_revoke_only_the_token_it_was_presented(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    github = checked_tool(sts)
    # Even with the strongest reason, authority does not widen: the handoff is above the tool.
    r = report(http, sts, "tool-github", handoff, "injection_suspected")
    assert r.status_code == 400 and r.json()["error"] == "unauthorized_client"
    assert report(http, sts, "tool-github", user, "injection_suspected").status_code == 400
    assert exchange(http, sts, "code-review-subagent", handoff, audience="tool-github").status_code == 200
    # Its own tool token: yes. tool-jira, which was never presented this token: no.
    assert report(http, sts, "tool-jira", tool_tok, "cleanup").status_code == 400
    assert report(http, sts, "tool-github", tool_tok, "cleanup").status_code == 200
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(tool_tok)


def test_cleanup_revokes_only_the_presented_token_and_never_counts(http, sts):
    user = user_token(sts)
    handoff, (a, b, c) = subagent_tools(http, sts, user, 3)
    github = checked_tool(sts)
    for tok in (a, b, c):
        assert report(http, sts, "tool-github", tok, "cleanup").status_code == 200
    assert escalations(sts) == [] and sts._strikes == {}
    # Siblings died one by one by the caller's own authority; the handoff and grant are live.
    assert exchange(http, sts, "code-review-subagent", handoff, audience="tool-github").status_code == 200
    assert claims(user)["grant_id"] not in sts.denylist
    assert all(e["report_reason"] == "cleanup" for e in sts.audit_log if e["event"] == "token_revocation")


def test_second_strike_kills_the_handoff_subtree_but_not_another_actors_handoff(http, sts):
    user = user_token(sts)
    handoff, (a, b, c) = subagent_tools(http, sts, user, 3)
    gw_direct = exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").json()["access_token"]
    sts.clients["mcp-gateway"].may_delegate_to.add("notes-agent")
    sts.clients["notes-agent"].audiences.add("tool-github")
    sts.clients["notes-agent"].scopes.add("repo:read")
    other_handoff = exchange(http, sts, "mcp-gateway", user, delegate_to="notes-agent", scope="repo:read").json()["access_token"]
    other_tool = exchange(http, sts, "notes-agent", other_handoff, audience="tool-github").json()["access_token"]
    github = checked_tool(sts)

    assert report(http, sts, "tool-github", a, "injection_suspected").status_code == 200
    github.verify(b)  # strike 1: only a is dead
    assert report(http, sts, "tool-github", b, "injection_suspected").status_code == 200
    assert escalations(sts) == [(1, "token"), (2, "handoff")]
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(c)  # sibling under the same handoff, never reported
    assert exchange(http, sts, "code-review-subagent", handoff, audience="tool-github").status_code == 400
    # The gateway's own token, the other actor's handoff and the grant survive.
    assert github.verify(gw_direct).path == "user:alice > mcp-gateway"
    assert github.verify(other_tool).path == "user:alice > mcp-gateway > notes-agent"
    assert exchange(http, sts, "notes-agent", other_handoff, audience="tool-github").status_code == 200
    assert claims(user)["grant_id"] not in sts.denylist


def test_third_strike_kills_the_grant(http, sts):
    user = user_token(sts)
    handoff, (a, b) = subagent_tools(http, sts, user, 2)
    github = checked_tool(sts)
    report(http, sts, "tool-github", a, "anomaly")
    report(http, sts, "tool-github", b, "injection_suspected")
    # The sub-agent gets a fresh handoff from the same grant and keeps misbehaving.
    handoff2, (c,) = subagent_tools(http, sts, user, 1)
    assert report(http, sts, "tool-github", c, "injection_suspected").status_code == 200
    assert escalations(sts)[-1] == (3, "grant")
    assert claims(user)["grant_id"] in sts.denylist
    assert exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").status_code == 400
    assert exchange(http, sts, "code-review-subagent", handoff2, audience="tool-github").status_code == 400
    last = [e for e in sts.audit_log if e["event"] == "revocation_escalation"][-1]
    assert last["report_reason"] == "injection_suspected" and last["revoked_jtis"] >= 5


def test_strikes_outside_the_window_do_not_accumulate():
    now = [time.time()]
    # Window shorter than the 120 s tool token TTL, so the second token is still live when the clock moves.
    sts = STS(issuer=ISS, clock=lambda: now[0], escalation=EscalationPolicy(window_s=60))
    http = TestClient(create_app(sts))
    user = user_token(sts)
    handoff, (a, b, c) = subagent_tools(http, sts, user, 3)
    github = checked_tool(sts)
    report(http, sts, "tool-github", a, "anomaly")
    now[0] += 61
    report(http, sts, "tool-github", b, "anomaly")
    assert escalations(sts) == [(1, "token"), (1, "token")]
    github.verify(c)  # the handoff is still live: the two strikes never met
    # (No new exchange here: a token minted after the clock jump has a future iat for PyJWT.)
    assert claims(handoff)["jti"] not in sts.denylist and claims(user)["grant_id"] not in sts.denylist


def test_second_strike_on_a_one_hop_chain_kills_that_actors_delegation(http, sts):
    user = user_token(sts)
    a = exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").json()["access_token"]
    b = exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").json()["access_token"]
    c = exchange(http, sts, "mcp-gateway", user, audience="tool-jira", scope="issues:read").json()["access_token"]
    report(http, sts, "tool-github", a, "anomaly")
    report(http, sts, "tool-github", b, "anomaly")
    assert escalations(sts)[-1] == (2, "delegation")
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts, "tool-jira").verify(c)  # same actor, same grant, never reported
    assert claims(user)["grant_id"] not in sts.denylist  # the grant itself survives until strike 3


def test_invalid_reason_is_invalid_request(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    r = report(http, sts, "tool-github", tool_tok, "because")
    assert r.status_code == 400 and r.json()["error"] == "invalid_request" and "reason" in r.json()["error_description"]
    checked_tool(sts).verify(tool_tok)  # nothing happened
    assert sts.audit_log[-1]["decision"] == "deny" and sts.audit_log[-1]["report_reason"] == "because"


# ---- B. admin revocation through the client machinery ------------------------

def test_admin_scope_cannot_be_obtained_through_exchange_or_login(http, sts):
    with pytest.raises(Exception, match="never issued to a user"):
        sts.issue_user_token("alice", "mcp-gateway", {ADMIN_SCOPE, "repo:read"})
    assert http.post("/dev/login", data={"user": "alice", "client_id": "mcp-gateway", "scope": ADMIN_SCOPE}).status_code == 400
    # Even a user token that somehow carries the scope, and an actor registered for it, cannot exchange it.
    sts.clients["mcp-gateway"].scopes.add(ADMIN_SCOPE)
    now = int(time.time())
    forged_login = sts._sign({"iss": ISS, "sub": "user:alice", "aud": ["mcp-gateway"], "iat": now, "exp": now + 60, "jti": "x",
                              "scope": f"{ADMIN_SCOPE} repo:read", "cnf": {"jkt": sts.clients["mcp-gateway"].jkt},
                              "may_act": {"sub": "client:mcp-gateway", "client_id": "mcp-gateway", "iss": ISS}})
    for scope in (ADMIN_SCOPE, None):
        r = exchange(http, sts, "mcp-gateway", forged_login, audience="tool-github", **({"scope": scope} if scope else {}))
        assert r.status_code == 400 and r.json()["error"] == "invalid_scope" and "never issued by exchange" in r.json()["error_description"]
    assert exchange(http, sts, "ops-console", user_token(sts), audience="tool-github").status_code == 400


def test_admin_routes_need_an_admin_actor_token(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    gid = claims(user)["grant_id"]
    # No bearer, a user token, and a tool token are all 401 invalid_client: not actor tokens.
    for bearer in (None, user, tool_tok, "garbage"):
        r = admin_call(http, sts, "/admin/revoke/grant", bearer, grant_id=gid)
        assert r.status_code == 401 and r.json()["error"] == "invalid_client", bearer
    # A real actor token from a client without sts:admin is 403 insufficient_scope.
    r = admin_call(http, sts, "/admin/revoke/grant", actor_token(http, sts, "mcp-gateway"), grant_id=gid)
    assert r.status_code == 403 and r.json()["error"] == "insufficient_scope"
    assert gid not in sts.denylist
    checked_tool(sts).verify(tool_tok)


def test_admin_revoke_grant_over_http_kills_live_tool_tokens(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    other_user, _, other_tool = full_chain(http, sts)
    gid = claims(user)["grant_id"]
    ops = actor_token(http, sts, "ops-console")
    r = admin_call(http, sts, "/admin/revoke/grant", ops, grant_id=gid)
    assert r.status_code == 200 and r.json() == {"grant_id": gid, "revoked_jtis": 3}
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(tool_tok)
    assert exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").status_code == 400
    checked_tool(sts).verify(other_tool)
    # Unknown handle: 200, zero counts, audited.
    r = admin_call(http, sts, "/admin/revoke/grant", ops, grant_id="no-such-grant")
    assert r.status_code == 200 and r.json()["revoked_jtis"] == 0
    assert sts.audit_log[-1]["event"] == "grant_revocation" and sts.audit_log[-1]["by"] == "ops-console"


def test_admin_revoke_and_enable_client_over_http(http, sts):
    user, handoff, sub_tool = full_chain(http, sts)
    ops = actor_token(http, sts, "ops-console")
    r = admin_call(http, sts, "/admin/revoke/client", ops, client_id="code-review-subagent")
    assert r.status_code == 200 and r.json()["disabled"] is True and r.json()["revoked_jtis"] >= 2
    tok = http.post("/token", data={"grant_type": "client_credentials", "client_id": "code-review-subagent", "client_secret": secret(sts, "code-review-subagent")})
    assert tok.status_code == 401
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(sub_tool)
    r = admin_call(http, sts, "/admin/enable/client", ops, client_id="code-review-subagent")
    assert r.status_code == 200 and r.json() == {"client_id": "code-review-subagent", "known": True, "disabled": False}
    assert actor_token(http, sts, "code-review-subagent")
    # Unknown client: nothing is disabled, zero counts, 200.
    r = admin_call(http, sts, "/admin/revoke/client", ops, client_id="ghost")
    assert r.status_code == 200 and r.json() == {"client_id": "ghost", "revoked_jtis": 0, "disabled": False}
    assert "ghost" not in sts.disabled_clients


def test_admin_cli_drives_the_routes(http, sts, capsys):
    user, handoff, tool_tok = full_chain(http, sts)
    gid = claims(user)["grant_id"]
    key = sts.clients["ops-console"].key
    assert admin_cli.run(["revoke-grant", gid], http, secret(sts, "ops-console"), key) == 0
    assert '"revoked_jtis": 3' in capsys.readouterr().out
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(tool_tok)
    assert admin_cli.run(["revoke-grant", gid], http, "wrong-secret", key) == 1
    assert admin_cli.run(["frobnicate", gid], http, secret(sts, "ops-console"), key) == 2


def test_revoked_ops_console_loses_admin(http, sts):
    ops = actor_token(http, sts, "ops-console")
    assert admin_call(http, sts, "/admin/revoke/client", ops, client_id="ops-console").status_code == 200
    r = admin_call(http, sts, "/admin/enable/client", ops, client_id="ops-console")
    assert r.status_code == 401  # its own live actor token is now revoked, and the client is disabled
    assert sts.enable_client("ops-console") is True  # recovery is a method call on the host, not a route


def test_lineage_claims_unchanged_by_admin_and_tool_clients(http, sts):
    # Tool and ops clients exist in the registry but cannot take part in a chain.
    for cid in ("tool-github", "ops-console"):
        a = jwt.decode(actor_token(http, sts, cid), options={"verify_signature": False})
        assert a["token_use"] == "actor" and "scope" not in a
        assert exchange(http, sts, cid, user_token(sts), audience="tool-github").status_code == 400
