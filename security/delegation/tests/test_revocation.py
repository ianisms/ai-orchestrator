"""Revocation: lineage claims, RFC 7009 /revoke with the subtree extension,
admin revocation by handle, and the verify-side deny check."""
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from sts import STS, create_app
from test_delegation import ISS, actor_token, exchange, secret, tool, user_token
from verify import CallbackRevocationCheck, DelegationRejected, UpstreamVerifier


@pytest.fixture
def sts():
    return STS(issuer=ISS)


@pytest.fixture
def http(sts):
    return TestClient(create_app(sts))


def claims(token):
    return jwt.decode(token, options={"verify_signature": False})


def revoke(http, sts, cid, token, hint=None):
    data = {"token": token, "client_id": cid, "client_secret": secret(sts, cid)}
    if hint:
        data["token_type_hint"] = hint
    return http.post("/revoke", data=data)


def checked_tool(sts, aud="tool-github"):
    """A tool verifier that shares the STS denylist in process."""
    return UpstreamVerifier(ISS, aud, key=sts.public_key, revocation=sts.denylist)


def full_chain(http, sts):
    """user token > handoff (gateway) > tool token (sub-agent)."""
    user = user_token(sts)
    handoff = exchange(http, sts, "mcp-gateway", user, delegate_to="code-review-subagent", scope="repo:read")
    assert handoff.status_code == 200, handoff.text
    handoff = handoff.json()["access_token"]
    tool_tok = exchange(http, sts, "code-review-subagent", handoff, audience="tool-github")
    assert tool_tok.status_code == 200, tool_tok.text
    return user, handoff, tool_tok.json()["access_token"]


# ---- lineage claims ---------------------------------------------------------

def test_lineage_claims_follow_the_chain(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    u, h, t = claims(user), claims(handoff), claims(tool_tok)
    assert u["jti"] and u["grant_id"] and "parent_jti" not in u
    assert h["grant_id"] == u["grant_id"] and h["parent_jti"] == u["jti"]
    assert t["grant_id"] == u["grant_id"] and t["parent_jti"] == h["jti"]
    assert len({u["jti"], h["jti"], t["jti"]}) == 3
    a = claims(actor_token(http, sts, "mcp-gateway"))
    assert a["jti"] and "grant_id" not in a and "parent_jti" not in a
    assert checked_tool(sts).verify(tool_tok).grant_id == u["grant_id"]


# ---- RFC 7009 /revoke -------------------------------------------------------

def test_revoke_user_token_kills_issued_tool_token_and_new_exchange(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    github = checked_tool(sts)
    github.verify(tool_tok)  # live before revocation

    r = revoke(http, sts, "mcp-gateway", user)
    assert r.status_code == 200 and r.content == b""

    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(tool_tok)
    r = exchange(http, sts, "code-review-subagent", handoff, audience="tool-github")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant" and "revoked" in r.json()["error_description"]
    r = exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read")
    assert r.status_code == 400 and "revoked" in r.json()["error_description"]
    assert claims(user)["grant_id"] in sts.denylist


def test_revoke_one_handoff_sibling_still_works(http, sts):
    user = user_token(sts)
    handoff_a = exchange(http, sts, "mcp-gateway", user, delegate_to="code-review-subagent", scope="repo:read").json()["access_token"]
    handoff_b = exchange(http, sts, "mcp-gateway", user, delegate_to="code-review-subagent", scope="repo:read").json()["access_token"]
    tool_a = exchange(http, sts, "code-review-subagent", handoff_a, audience="tool-github").json()["access_token"]
    tool_b = exchange(http, sts, "code-review-subagent", handoff_b, audience="tool-github").json()["access_token"]

    assert revoke(http, sts, "mcp-gateway", handoff_a).status_code == 200
    github = checked_tool(sts)
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(tool_a)  # descendant of the revoked handoff
    assert github.verify(tool_b).path == "user:alice > mcp-gateway > code-review-subagent"
    assert exchange(http, sts, "code-review-subagent", handoff_a, audience="tool-github").status_code == 400
    assert exchange(http, sts, "code-review-subagent", handoff_b, audience="tool-github").status_code == 200
    # The user token and its grant are untouched: only the one branch is dead.
    assert exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").status_code == 200
    assert claims(user)["grant_id"] not in sts.denylist


def test_revoke_unknown_malformed_or_expired_token_succeeds_silently(http, sts):
    other = STS(issuer=ISS)
    forged = other._sign({"iss": ISS, "sub": "user:alice", "aud": ["mcp-gateway"], "iat": int(time.time()), "exp": int(time.time()) + 60, "jti": "x"})
    sts.clock = lambda: time.time() - 3600
    expired = user_token(sts)
    sts.clock = time.time
    for tok in ("not-a-jwt", forged, expired):
        r = revoke(http, sts, "mcp-gateway", tok, hint="refresh_token")
        assert r.status_code == 200 and r.content == b"", tok
    assert len(sts.denylist) == 0
    assert [e["decision"] for e in sts.audit_log if e["event"] == "token_revocation"] == ["noop"] * 3


def test_revoke_requires_client_authentication(http, sts):
    user = user_token(sts)
    r = http.post("/revoke", data={"token": user, "client_id": "mcp-gateway", "client_secret": "wrong"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    assert len(sts.denylist) == 0


def test_client_outside_the_chain_cannot_revoke(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    github = checked_tool(sts)

    # notes-agent is in no part of alice's chain.
    r = revoke(http, sts, "notes-agent", user)
    assert r.status_code == 400 and r.json()["error"] == "unauthorized_client"
    assert revoke(http, sts, "notes-agent", tool_tok).status_code == 400
    # The sub-agent is downstream of the user token: it may not end the session above it.
    assert revoke(http, sts, "code-review-subagent", user).status_code == 400
    github.verify(tool_tok)
    assert len(sts.denylist) == 0
    assert sts.audit_log[-1]["event"] == "token_revocation" and sts.audit_log[-1]["decision"] == "deny"

    # The sub-agent holds the tool token, so it may revoke that one.
    assert revoke(http, sts, "code-review-subagent", tool_tok).status_code == 200
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(tool_tok)


def test_ancestor_in_act_chain_may_revoke_descendant(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    # The gateway is not the holder of the sub-agent's tool token, but it is in its act chain.
    assert revoke(http, sts, "mcp-gateway", tool_tok).status_code == 200
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(tool_tok)
    # The handoff above it is untouched.
    assert exchange(http, sts, "code-review-subagent", handoff, audience="tool-github").status_code == 200


def test_denylist_entry_expires_with_the_family():
    # The STS clock is injectable; PyJWT's own exp check still uses wall time,
    # so the clock starts at now and only the denylist's view of time moves.
    now = [time.time()]
    sts = STS(issuer=ISS, clock=lambda: now[0])
    http = TestClient(create_app(sts))
    user, handoff, tool_tok = full_chain(http, sts)
    assert revoke(http, sts, "mcp-gateway", user).status_code == 200
    assert len(sts.denylist) == 4  # user, handoff, tool jtis plus the grant
    now[0] += 599
    assert len(sts.denylist) == 4
    now[0] += 1  # user token exp reached: the whole family is dead on its own
    assert len(sts.denylist) == 0
    assert claims(user)["grant_id"] not in sts.denylist


# ---- admin revocation by handle ---------------------------------------------

def test_revoke_grant_by_handle(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    assert sts.revoke_grant(claims(user)["grant_id"]) == 3
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(tool_tok)
    assert exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").status_code == 400


def test_revoke_client_blocks_new_actor_tokens_and_kills_live_tool_tokens(http, sts):
    user, handoff, sub_tool = full_chain(http, sts)
    gw_tool = exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").json()["access_token"]
    live_actor = actor_token(http, sts, "code-review-subagent")
    github = checked_tool(sts)

    assert sts.revoke_client("code-review-subagent") >= 3  # handoff, tool token, actor token

    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "code-review-subagent", "client_secret": secret(sts, "code-review-subagent")})
    assert r.status_code == 401 and "revoked" in r.json()["error_description"]
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(sub_tool)  # sub-agent in act
    r = exchange(http, sts, "code-review-subagent", handoff, actor=live_actor, audience="tool-github")
    assert r.status_code == 401  # a still-valid actor token does not help a revoked client
    # The gateway's own direct token has no sub-agent in act and keeps working.
    assert github.verify(gw_tool).path == "user:alice > mcp-gateway"
    assert exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").status_code == 200

    sts.enable_client("code-review-subagent")
    assert actor_token(http, sts, "code-review-subagent")
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(sub_tool)  # re-enabling does not resurrect what was killed


# ---- verify-side hook -------------------------------------------------------

def test_verifier_without_checker_stays_offline_and_unaware(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    revoke(http, sts, "mcp-gateway", user)
    assert tool(sts, "tool-github").verify(tool_tok).caller == "code-review-subagent"
    with pytest.raises(DelegationRejected):
        checked_tool(sts).verify(tool_tok)


def test_callback_check_stands_in_for_a_pushed_deny_list(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    pushed: set[str] = set()
    github = UpstreamVerifier(ISS, "tool-github", key=sts.public_key,
                              revocation=CallbackRevocationCheck(lambda jti, parent, grant: bool(pushed & {jti, parent, grant})))
    github.verify(tool_tok)
    pushed.add(claims(user)["grant_id"])
    with pytest.raises(DelegationRejected, match="deny list"):
        github.verify(tool_tok)

    def boom(jti, parent, grant):
        raise RuntimeError("list unavailable")

    github.revocation = CallbackRevocationCheck(boom)
    with pytest.raises(DelegationRejected, match="revocation check failed"):
        github.verify(tool_tok)


def test_forged_token_never_reaches_the_revocation_check(sts):
    calls = []
    github = UpstreamVerifier(ISS, "tool-github", key=sts.public_key,
                              revocation=CallbackRevocationCheck(lambda *a: calls.append(a) or False))
    other = STS(issuer=ISS)
    forged = other._sign({"iss": ISS, "sub": "user:alice", "aud": ["tool-github"], "iat": int(time.time()), "exp": int(time.time()) + 60, "act": {"client_id": "mcp-gateway"}})
    with pytest.raises(DelegationRejected, match="invalid token"):
        github.verify(forged)
    assert calls == []


def test_revocation_and_pdp_fail_closed_are_independent(http, sts):
    """A revocation needs no PDP, and a PDP outage needs no revocation."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "policy"))
    from pep import PEP

    def pdp_down(request):
        raise httpx.ConnectError("no PDP", request=request)

    pep = PEP(httpx.Client(base_url="http://pdp", transport=httpx.MockTransport(pdp_down)), catalog={})
    github = checked_tool(sts)
    user, handoff, tool_tok = full_chain(http, sts)
    _, _, other_tok = full_chain(http, sts)

    # Live token: verified locally, then the PDP outage denies it (fail closed).
    d = pep.authorize(github.verify(other_tok), "github.get_pr", {"repo": "acme/app", "pr": 1})
    assert d.allow is False and d.fail_closed is True

    # Revoked token: refused by the verifier with no PDP involved; the PEP never sees it.
    revoke(http, sts, "mcp-gateway", user)
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(tool_tok)
    assert len(pep.audit_log) == 1


def test_every_revocation_is_audited(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    revoke(http, sts, "mcp-gateway", handoff)
    revoke(http, sts, "mcp-gateway", "garbage")
    revoke(http, sts, "notes-agent", user)
    events = [(e["event"], e["decision"]) for e in sts.audit_log if e["event"] == "token_revocation"]
    assert events == [("token_revocation", "revoked"), ("token_revocation", "noop"), ("token_revocation", "deny")]
    assert all(e["reason"] for e in sts.audit_log)
    assert sts.audit_log[-3]["revoked_jtis"] == 2  # the handoff and the tool token under it
