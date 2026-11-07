import time

import jwt
import pytest
from fastapi.testclient import TestClient

from dpop import ath, make_proof
from sts import ACCESS_TOKEN_TYPE, STS, TOKEN_EXCHANGE, create_app
from verify import DelegationRejected, UpstreamVerifier

ISS = "http://sts.test"
TOOL_URL = "https://tools.test/mcp"  # the URI tool calls are addressed to; proofs name it as htu


@pytest.fixture
def sts():
    return STS(issuer=ISS)


@pytest.fixture
def http(sts):
    return TestClient(create_app(sts))


def secret(sts, cid):
    return sts.clients[cid].secret


def token_url(http):
    return str(http.base_url).rstrip("/") + "/token"


def proof_for(sts, cid, htm, htu, token=None, **kw):
    """A DPoP proof signed with a registered client's key, iat on the STS clock."""
    kw.setdefault("now", int(sts.clock()))
    return make_proof(sts.clients[cid].key, htm, htu, ath=ath(token) if token else None, **kw)


def holder_of(sts, token):
    """The registered client whose key a token is bound to (cnf.jkt), or None."""
    try:
        jkt = (jwt.decode(token, options={"verify_signature": False}).get("cnf") or {}).get("jkt")
    except jwt.InvalidTokenError:
        return None
    return next((c for c in sts.clients.values() if c.jkt == jkt), None)


def actor_token(http, sts, cid):
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret(sts, cid)},
                  headers={"DPoP": proof_for(sts, cid, "POST", token_url(http))})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def exchange(http, sts, cid, subject, actor=True, dpop=True, **extra):
    """Exchange as client cid. dpop=True signs a proof with cid's key; a string is
    sent as the DPoP header verbatim; None sends no header."""
    data = {
        "grant_type": TOKEN_EXCHANGE,
        "client_id": cid,
        "client_secret": secret(sts, cid),
        "subject_token": subject,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        **extra,
    }
    if actor:
        data["actor_token"] = actor_token(http, sts, cid) if actor is True else actor
        data["actor_token_type"] = ACCESS_TOKEN_TYPE
    headers = {}
    if dpop is True:
        headers["DPoP"] = proof_for(sts, cid, "POST", token_url(http))
    elif dpop:
        headers["DPoP"] = dpop
    return http.post("/token", data=data, headers=headers)


class ProvingVerifier(UpstreamVerifier):
    """Test stand-in for 'the holder calls the tool': verify(token) signs a fresh
    proof with the key the token is bound to, as the legitimate holder would,
    so the pre-DPoP tests keep their shape and now run the bound path. A token
    with no cnf, or bound to a key no registered client holds, is presented
    bare, which is what a thief without the key can do."""

    def __init__(self, sts, aud, **kw):
        super().__init__(ISS, aud, key=sts.public_key, **kw)
        self.sts = sts

    def verify(self, token, dpop_proof=None, htm=None, htu=None):
        if dpop_proof is None:
            holder = holder_of(self.sts, token)
            if holder is not None:
                dpop_proof = make_proof(holder.key, "POST", TOOL_URL, ath=ath(token), now=int(self.clock()))
                htm, htu = "POST", TOOL_URL
        return super().verify(token, dpop_proof=dpop_proof, htm=htm, htu=htu)


def tool(sts, aud):
    return ProvingVerifier(sts, aud)


def user_token(sts, consent=True, scopes=("repo:read", "repo:write", "issues:read")):
    return sts.issue_user_token("alice", "mcp-gateway", set(scopes), consent=consent)


def chain_to_subagent(http, sts):
    handoff = exchange(http, sts, "mcp-gateway", user_token(sts), delegate_to="code-review-subagent", scope="repo:read")
    assert handoff.status_code == 200, handoff.text
    return handoff.json()["access_token"]


# ---- happy paths ------------------------------------------------------------

def test_one_hop_user_to_gateway(http, sts):
    r = exchange(http, sts, "mcp-gateway", user_token(sts), audience="tool-github", scope="repo:read")
    assert r.status_code == 200, r.text
    p = tool(sts, "tool-github").verify(r.json()["access_token"])
    assert p.path == "user:alice > mcp-gateway"
    assert p.scopes == {"repo:read"}
    assert p.expires_at - time.time() <= 300
    assert "refresh_token" not in r.json()


def test_nested_chain_user_gateway_subagent(http, sts):
    handoff = chain_to_subagent(http, sts)
    r = exchange(http, sts, "code-review-subagent", handoff, audience="tool-github")
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    p = tool(sts, "tool-github").verify(tok)
    assert p.path == "user:alice > mcp-gateway > code-review-subagent"
    assert p.caller == "code-review-subagent"
    assert p.scopes == {"repo:read"}
    claims = jwt.decode(tok, options={"verify_signature": False})
    assert claims["act"]["client_id"] == "code-review-subagent"
    assert claims["act"]["act"]["client_id"] == "mcp-gateway"
    assert claims["exp"] - claims["iat"] <= 120


def test_default_scope_is_intersection(http, sts):
    r = exchange(http, sts, "mcp-gateway", user_token(sts), audience="tool-jira")
    assert r.json()["scope"] == "issues:read repo:read repo:write"


# ---- attacks ----------------------------------------------------------------

def test_expired_tool_token_replay_rejected(http, sts):
    sts.clients["mcp-gateway"].ttl = 1
    r = exchange(http, sts, "mcp-gateway", user_token(sts), audience="tool-github", scope="repo:read")
    assert r.status_code == 200
    tok = r.json()["access_token"]
    tool(sts, "tool-github").verify(tok)  # valid while fresh
    time.sleep(2.1)
    with pytest.raises(DelegationRejected, match="expired"):
        tool(sts, "tool-github").verify(tok)


def test_expired_subject_token_exchange_rejected(http, sts):
    sts.clock = lambda: time.time() - 3600
    old = user_token(sts)
    sts.clock = time.time
    r = exchange(http, sts, "mcp-gateway", old, audience="tool-github")
    assert r.status_code == 400
    assert r.json()["error_description"] == "subject_token expired"


def test_exchange_without_may_act_rejected(http, sts):
    r = exchange(http, sts, "mcp-gateway", user_token(sts, consent=False), audience="tool-github")
    assert r.status_code == 400
    assert "no may_act" in r.json()["error_description"]


def test_other_agent_cannot_use_users_token(http, sts):
    r = exchange(http, sts, "notes-agent", user_token(sts), audience="tool-fs")
    assert r.status_code == 400
    assert "audience does not name this client" in r.json()["error_description"]


def test_confused_deputy_subagent_asks_for_write(http, sts):
    handoff = chain_to_subagent(http, sts)
    r = exchange(http, sts, "code-review-subagent", handoff, audience="tool-github", scope="repo:write")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_scope"


def test_confused_deputy_subagent_asks_for_unregistered_tool(http, sts):
    handoff = chain_to_subagent(http, sts)
    r = exchange(http, sts, "code-review-subagent", handoff, audience="tool-jira")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_target"


def test_gateway_cannot_hand_subagent_more_than_it_is_registered_for(http, sts):
    r = exchange(http, sts, "mcp-gateway", user_token(sts), delegate_to="code-review-subagent", scope="repo:read repo:write")
    assert r.status_code == 400
    assert "code-review-subagent is registered for" in r.json()["error_description"]


def test_handoff_token_rejected_at_tool(http, sts):
    handoff = chain_to_subagent(http, sts)
    with pytest.raises(DelegationRejected):
        tool(sts, "tool-github").verify(handoff)


def test_plain_exchange_cannot_strip_act(http, sts):
    handoff = chain_to_subagent(http, sts)
    r = exchange(http, sts, "code-review-subagent", handoff, actor=False, audience="tool-github")
    assert r.status_code == 400
    assert "would lose its act chain" in r.json()["error_description"]


def test_actor_token_from_another_client_rejected(http, sts):
    stolen = actor_token(http, sts, "notes-agent")
    r = exchange(http, sts, "mcp-gateway", user_token(sts), actor=stolen, audience="tool-github")
    assert r.status_code == 400
    assert "does not belong" in r.json()["error_description"]


def test_chain_depth_capped(http, sts):
    sts.clients["code-review-subagent"].may_delegate_to.add("notes-agent")
    handoff = chain_to_subagent(http, sts)
    r = exchange(http, sts, "code-review-subagent", handoff, delegate_to="notes-agent", scope="repo:read")
    assert r.status_code == 400
    assert "too deep" in r.json()["error_description"]


def test_user_token_without_act_rejected_at_tool(sts):
    raw = sts._sign({"iss": ISS, "sub": "user:alice", "aud": ["tool-github"], "iat": int(time.time()), "exp": int(time.time()) + 60})
    with pytest.raises(DelegationRejected, match="no act claim"):
        tool(sts, "tool-github").verify(raw)


def test_forged_signature_rejected(sts):
    other = STS(issuer=ISS)
    forged = other._sign({"iss": ISS, "sub": "user:alice", "aud": ["tool-github"], "iat": int(time.time()), "exp": int(time.time()) + 60, "act": {"client_id": "mcp-gateway"}})
    with pytest.raises(DelegationRejected, match="invalid token"):
        tool(sts, "tool-github").verify(forged)


def test_every_decision_is_audited(http, sts):
    exchange(http, sts, "mcp-gateway", user_token(sts), audience="tool-github")
    exchange(http, sts, "mcp-gateway", user_token(sts, consent=False), audience="tool-github")
    decisions = [(e["decision"], e["reason"]) for e in sts.audit_log]
    assert decisions[0] == ("allow", "issued")
    assert decisions[1][0] == "deny" and "no may_act" in decisions[1][1]
