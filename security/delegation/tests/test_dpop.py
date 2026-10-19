"""Sender constraint (RFC 9449 DPoP): proofs, binding through the exchange
chain, the verifier's checks, key revocation, and the bearer fallback flag."""
import json
import logging
import time

import jwt
import pytest
from fastapi.testclient import TestClient

import admin as admin_cli
from dpop import (
    DPoPError,
    ReplayCache,
    ath,
    generate_key,
    make_proof,
    normalize_htu,
    public_jwk,
    thumbprint,
    verify_proof,
)
from sts import STS, create_app
from test_delegation import (
    ISS,
    TOOL_URL,
    ProvingVerifier,
    actor_token,
    exchange,
    holder_of,
    proof_for,
    secret,
    token_url,
    user_token,
)
from test_escalation_admin import admin_call
from test_revocation import checked_tool, claims, full_chain
from verify import DelegationRejected, UpstreamVerifier


@pytest.fixture
def sts():
    return STS(issuer=ISS)


@pytest.fixture
def http(sts):
    return TestClient(create_app(sts))


def raw_tool(sts, aud="tool-github", **kw):
    """A plain verifier: no proof is added for the caller."""
    return UpstreamVerifier(ISS, aud, key=sts.public_key, revocation=sts.denylist, **kw)


def present(verifier, sts, token, cid=None, htu=TOOL_URL, htm="POST", **proof_kw):
    """Call the tool as client cid (default: the token's holder) with a fresh proof."""
    key = sts.clients[cid].key if cid else holder_of(sts, token).key
    proof_kw.setdefault("ath", ath(token))
    return verifier.verify(token, dpop_proof=make_proof(key, "POST", htu, **proof_kw), htm=htm, htu=htu)


# ---- dpop.py on its own -------------------------------------------------------

def test_proof_happy_path_and_thumbprint():
    key = generate_key()
    seen = ReplayCache()
    p = make_proof(key, "POST", "https://sts.test/token")
    header = jwt.get_unverified_header(p)
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    assert set(header["jwk"]) == {"kty", "crv", "x", "y"}  # public members only
    out = verify_proof(p, "POST", "https://sts.test/token", expected_jkt=thumbprint(key), seen=seen)
    assert out.jkt == thumbprint(key) == thumbprint(public_jwk(key))
    assert len(thumbprint(key)) == 43  # base64url SHA-256, no padding
    # The same proof again is a replay; a fresh one from the same key is fine.
    with pytest.raises(DPoPError, match="replay"):
        verify_proof(p, "POST", "https://sts.test/token", seen=seen)
    verify_proof(make_proof(key, "POST", "https://sts.test/token"), "POST", "https://sts.test/token", seen=seen)


def test_proof_refusals():
    key, other = generate_key(), generate_key()
    url = "https://sts.test/token"
    now = int(time.time())
    cases = {
        "htm": (make_proof(key, "GET", url), dict(htm="POST", htu=url), "htm"),
        "htu": (make_proof(key, "POST", "https://sts.test/other"), dict(htm="POST", htu=url), "htu"),
        "too old": (make_proof(key, "POST", url, now=now - 61), dict(htm="POST", htu=url), "older than 60 s"),
        "future": (make_proof(key, "POST", url, now=now + 30), dict(htm="POST", htu=url), "in the future"),
        "wrong key": (make_proof(other, "POST", url), dict(htm="POST", htu=url, expected_jkt=thumbprint(key)), "does not match the key"),
        "no ath": (make_proof(key, "POST", url), dict(htm="POST", htu=url, ath_of="tok"), "no ath"),
        "wrong ath": (make_proof(key, "POST", url, ath=ath("other")), dict(htm="POST", htu=url, ath_of="tok"), "ath does not match"),
        "nonce": (make_proof(key, "POST", url, nonce="a"), dict(htm="POST", htu=url, nonce="b"), "nonce"),
        "missing": (None, dict(htm="POST", htu=url), "required"),
        "garbage": ("not.a.jwt", dict(htm="POST", htu=url), "not a JWT"),
    }
    for name, (proof, kw, msg) in cases.items():
        with pytest.raises(DPoPError, match=msg):
            verify_proof(proof, **kw)
    # Edge of the window: 60 s old passes, 5 s ahead passes.
    verify_proof(make_proof(key, "POST", url, now=now - 60), "POST", url)
    verify_proof(make_proof(key, "POST", url, now=now + 5), "POST", url)


def test_header_typ_alg_and_private_jwk_rejected():
    key = generate_key()
    url = "https://sts.test/token"
    body = {"jti": "1", "htm": "POST", "htu": url, "iat": int(time.time())}
    # Wrong typ.
    with pytest.raises(DPoPError, match="typ"):
        verify_proof(jwt.encode(body, key, algorithm="ES256", headers={"typ": "JWT", "jwk": public_jwk(key)}), "POST", url)
    # Wrong alg (HS256 with the thumbprint as the secret, the classic confusion).
    hs = jwt.encode(body, "secret", algorithm="HS256", headers={"typ": "dpop+jwt", "jwk": public_jwk(key)})
    with pytest.raises(DPoPError, match="alg"):
        verify_proof(hs, "POST", url)
    # Private material in the header jwk.
    leaky = dict(public_jwk(key), d="AAAA")
    with pytest.raises(DPoPError, match="private key material"):
        verify_proof(jwt.encode(body, key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": leaky}), "POST", url)
    # No jwk, or a non-EC one.
    with pytest.raises(DPoPError, match="jwk"):
        verify_proof(jwt.encode(body, key, algorithm="ES256", headers={"typ": "dpop+jwt"}), "POST", url)
    # Signature by a key other than the header jwk.
    other = generate_key()
    forged = jwt.encode(body, other, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": public_jwk(key)})
    with pytest.raises(DPoPError, match="signature"):
        verify_proof(forged, "POST", url)


def test_htu_normalisation():
    assert normalize_htu("HTTPS://Tools.Test/mcp?x=1#frag") == "https://tools.test/mcp"
    assert normalize_htu("https://tools.test") == "https://tools.test/"
    key = generate_key()
    # The proof names the bare URI; the request arrived with a query and a fragment.
    verify_proof(make_proof(key, "POST", "https://tools.test/mcp"), "POST", "https://tools.test/mcp?session=1#top")
    # Path differences still refuse: the path is part of the resource.
    with pytest.raises(DPoPError, match="htu"):
        verify_proof(make_proof(key, "POST", "https://tools.test/mcp"), "POST", "https://tools.test/mcp/")


def test_replay_cache_expires_with_max_age():
    now = [1_000_000.0]
    cache = ReplayCache(clock=lambda: now[0])
    key = generate_key()
    p = make_proof(key, "POST", "https://x/t", now=int(now[0]))
    verify_proof(p, "POST", "https://x/t", clock=lambda: now[0], max_age_s=60, seen=cache)
    assert len(cache) == 1
    now[0] += 59
    with pytest.raises(DPoPError, match="replay"):
        verify_proof(p, "POST", "https://x/t", clock=lambda: now[0], max_age_s=60, seen=cache)
    now[0] += 2  # past iat + max_age: the entry is pruned, and the proof is now too old anyway
    assert len(cache) == 0
    with pytest.raises(DPoPError, match="older than"):
        verify_proof(p, "POST", "https://x/t", clock=lambda: now[0], max_age_s=60, seen=cache)


# ---- STS binding --------------------------------------------------------------

def test_token_endpoint_requires_a_proof(http, sts):
    gw = secret(sts, "mcp-gateway")
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": gw})
    assert r.status_code == 400 and r.json()["error"] == "invalid_dpop_proof"
    # Client authentication still comes first.
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": "wrong"})
    assert r.status_code == 401
    # A proof for another URI, a stale proof, and a replayed proof are all invalid_dpop_proof.
    for proof in (proof_for(sts, "mcp-gateway", "POST", "http://elsewhere/token"),
                  proof_for(sts, "mcp-gateway", "POST", token_url(http), now=int(time.time()) - 120)):
        r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": gw}, headers={"DPoP": proof})
        assert r.status_code == 400 and r.json()["error"] == "invalid_dpop_proof", r.text
    good = proof_for(sts, "mcp-gateway", "POST", token_url(http))
    first = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": gw}, headers={"DPoP": good})
    again = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": gw}, headers={"DPoP": good})
    assert first.status_code == 200 and first.json()["token_type"] == "DPoP"
    assert again.status_code == 400 and "replay" in again.json()["error_description"]
    assert claims(first.json()["access_token"])["cnf"] == {"jkt": sts.clients["mcp-gateway"].jkt}
    # Exchange without a proof is refused the same way, before anything about the tokens.
    r = exchange(http, sts, "mcp-gateway", user_token(sts), audience="tool-github", dpop=None)
    assert r.status_code == 400 and r.json()["error"] == "invalid_dpop_proof"
    assert [e["error"] for e in sts.audit_log if e["decision"] == "deny"].count("invalid_dpop_proof") == 5


def test_actor_token_needs_the_registered_key_not_just_the_secret(http, sts):
    """A leaked client secret plus an attacker's own key mints nothing."""
    intruder = generate_key()
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": secret(sts, "mcp-gateway")},
                  headers={"DPoP": make_proof(intruder, "POST", token_url(http))})
    assert r.status_code == 400 and r.json()["error"] == "invalid_dpop_proof"
    assert "registered key" in r.json()["error_description"]


def test_cnf_survives_the_chain_and_each_token_is_bound_to_its_presenter(http, sts):
    user, handoff, tool_tok = full_chain(http, sts)
    gw, sa = sts.clients["mcp-gateway"].jkt, sts.clients["code-review-subagent"].jkt
    assert claims(user)["cnf"] == {"jkt": gw}  # the agent the user consented to holds it
    assert claims(handoff)["cnf"] == {"jkt": sa}  # bound at issuance to the delegate
    assert claims(tool_tok)["cnf"] == {"jkt": sa}  # bound to the sub-agent that requested it
    a = claims(actor_token(http, sts, "mcp-gateway"))
    assert a["cnf"] == {"jkt": gw}
    p = checked_tool(sts).verify(tool_tok)
    assert p.jkt == sa and p.caller == "code-review-subagent"
    issued = [e for e in sts.audit_log if e["event"] == "token_exchange" and e["decision"] == "allow"]
    assert [e["bound_to"] for e in issued] == [sa, sa] and all(e["jkt"] for e in issued)


def test_handoff_is_usable_by_the_delegate_and_not_by_the_gateway(http, sts):
    user = user_token(sts)
    handoff = exchange(http, sts, "mcp-gateway", user, delegate_to="code-review-subagent", scope="repo:read").json()["access_token"]
    # The gateway holds the handoff bytes but it is bound to the sub-agent's key. Even
    # if the gateway could pass the audience check it would fail the binding; here the
    # audience check (aud=code-review-subagent) refuses it first, as before.
    r = exchange(http, sts, "mcp-gateway", handoff, audience="tool-github")
    assert r.status_code == 400
    # The sub-agent presenting the gateway's user token: audience refuses it too. Force the
    # binding check to be the one that fires by minting a token addressed to the sub-agent
    # but bound to the gateway's key.
    wrong_bind = dict(claims(handoff), cnf={"jkt": sts.clients["mcp-gateway"].jkt}, jti="wb")
    wrong_bind = sts._sign(wrong_bind)
    r = exchange(http, sts, "code-review-subagent", wrong_bind, audience="tool-github")
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert r.json()["error_description"] == "subject_token is bound to another key"
    # The legitimate delegate can.
    assert exchange(http, sts, "code-review-subagent", handoff, audience="tool-github").status_code == 200


def test_exchange_refuses_an_unbound_actor_or_subject_token(http, sts):
    """Binding cannot be stepped down by presenting a token without cnf."""
    now = int(time.time())
    bare_actor = sts._sign({"iss": ISS, "sub": "client:mcp-gateway", "client_id": "mcp-gateway", "aud": [ISS],
                            "iat": now, "exp": now + 60, "jti": "ba", "token_use": "actor"})
    r = exchange(http, sts, "mcp-gateway", user_token(sts), actor=bare_actor, audience="tool-github")
    assert r.status_code == 400 and r.json()["error_description"] == "actor_token is not sender-constrained (no cnf.jkt)"
    bare_user = sts._sign({"iss": ISS, "sub": "user:alice", "aud": ["mcp-gateway"], "iat": now, "exp": now + 60, "jti": "bu",
                           "scope": "repo:read", "may_act": {"sub": "client:mcp-gateway", "client_id": "mcp-gateway", "iss": ISS}})
    r = exchange(http, sts, "mcp-gateway", bare_user, audience="tool-github")
    assert r.status_code == 400 and r.json()["error_description"] == "subject_token is not sender-constrained (no cnf.jkt)"


def test_proof_from_another_clients_key_at_exchange(http, sts):
    """notes-agent steals the gateway's secret and actor token and signs with its own key."""
    user = user_token(sts)
    gw_actor = actor_token(http, sts, "mcp-gateway")
    r = exchange(http, sts, "mcp-gateway", user, actor=gw_actor, audience="tool-github",
                 dpop=proof_for(sts, "notes-agent", "POST", token_url(http)))
    assert r.status_code == 400 and r.json()["error_description"] == "actor_token is bound to another key"


# ---- verifier -----------------------------------------------------------------

def test_tool_refuses_a_bound_token_without_a_proof(http, sts):
    _, _, tool_tok = full_chain(http, sts)
    github = raw_tool(sts)
    with pytest.raises(DelegationRejected, match="no DPoP proof"):
        github.verify(tool_tok)
    # With the proof, the same verifier accepts and reports the key.
    assert present(github, sts, tool_tok).jkt == sts.clients["code-review-subagent"].jkt


def test_stolen_tool_token_with_another_key_is_refused(http, sts):
    _, _, tool_tok = full_chain(http, sts)
    github = raw_tool(sts)
    with pytest.raises(DelegationRejected, match="does not match the key the token is bound to"):
        present(github, sts, tool_tok, cid="notes-agent")
    # A key nobody registered is no better.
    with pytest.raises(DelegationRejected, match="does not match"):
        github.verify(tool_tok, dpop_proof=make_proof(generate_key(), "POST", TOOL_URL, ath=ath(tool_tok)), htm="POST", htu=TOOL_URL)


def test_stolen_token_and_proof_replayed_is_refused(http, sts):
    _, _, tool_tok = full_chain(http, sts)
    github = raw_tool(sts)
    proof = make_proof(sts.clients["code-review-subagent"].key, "POST", TOOL_URL, ath=ath(tool_tok))
    github.verify(tool_tok, dpop_proof=proof, htm="POST", htu=TOOL_URL)
    with pytest.raises(DelegationRejected, match="replay"):
        github.verify(tool_tok, dpop_proof=proof, htm="POST", htu=TOOL_URL)
    # Another verifier (another tool process) has its own cache: a proof captured
    # on the way to tool A is good at tool B only if it names tool B, which it does not.
    jira = raw_tool(sts, "tool-jira")
    with pytest.raises(DelegationRejected, match="not addressed"):
        jira.verify(tool_tok, dpop_proof=proof, htm="POST", htu="https://jira.test/mcp")


def test_proof_for_another_uri_method_age_or_token_is_refused(http, sts):
    _, _, tool_tok = full_chain(http, sts)
    _, _, other_tok = full_chain(http, sts)
    github = raw_tool(sts)
    with pytest.raises(DelegationRejected, match="htu"):  # proof names another tool's URI
        github.verify(tool_tok, dpop_proof=make_proof(holder_of(sts, tool_tok).key, "POST", "https://tools.test/other", ath=ath(tool_tok)), htm="POST", htu=TOOL_URL)
    with pytest.raises(DelegationRejected, match="htm"):
        github.verify(tool_tok, dpop_proof=make_proof(holder_of(sts, tool_tok).key, "GET", TOOL_URL, ath=ath(tool_tok)), htm="POST", htu=TOOL_URL)
    with pytest.raises(DelegationRejected, match="older than"):
        present(github, sts, tool_tok, now=int(time.time()) - 120)
    with pytest.raises(DelegationRejected, match="ath does not match"):
        present(github, sts, tool_tok, ath=ath(other_tok))  # a proof minted for a sibling token
    with pytest.raises(DelegationRejected, match="no ath"):
        github.verify(tool_tok, dpop_proof=make_proof(holder_of(sts, tool_tok).key, "POST", TOOL_URL), htm="POST", htu=TOOL_URL)
    present(github, sts, tool_tok)  # nothing above poisoned the legitimate holder


def test_dpop_runs_before_revocation_and_refusals_log_the_jkt(http, sts, caplog):
    user, _, tool_tok = full_chain(http, sts)
    calls = []

    class Spy:
        def revoked(self, c):
            calls.append(c.get("jti"))
            return None

    github = raw_tool(sts)
    github.revocation = Spy()
    with caplog.at_level(logging.INFO, logger="verify.audit"):
        with pytest.raises(DelegationRejected):
            github.verify(tool_tok)  # no proof: never reaches the deny list
    assert calls == []
    line = json.loads(caplog.records[-1].message)
    assert line["event"] == "tool_token_refused" and line["jkt"] == sts.clients["code-review-subagent"].jkt
    present(github, sts, tool_tok)
    assert calls == [claims(tool_tok)["jti"]]


def test_verifier_can_insist_on_binding(http, sts):
    now = int(time.time())
    bare = sts._sign({"iss": ISS, "sub": "user:alice", "aud": ["tool-github"], "iat": now, "exp": now + 60, "jti": "b",
                      "act": {"client_id": "mcp-gateway"}})
    assert raw_tool(sts).verify(bare).jkt is None  # default: a bearer token is still a token
    with pytest.raises(DelegationRejected, match="requires DPoP"):
        raw_tool(sts, require_dpop=True).verify(bare)


# ---- key revocation ----------------------------------------------------------

def test_revoke_key_kills_every_token_for_that_key_and_new_proofs(http, sts):
    user, handoff, sub_tool = full_chain(http, sts)
    gw_tool = exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").json()["access_token"]
    sa_actor = actor_token(http, sts, "code-review-subagent")
    github = checked_tool(sts)
    sa = sts.clients["code-review-subagent"].jkt

    # Everything bound to the key: the handoff, the tool token, the actor token full_chain
    # minted for the exchange and sa_actor. Nothing of the gateway's.
    assert sts.revoke_key(sa) == 4
    with pytest.raises(DelegationRejected, match="revoked"):
        github.verify(sub_tool)
    r = exchange(http, sts, "code-review-subagent", handoff, actor=sa_actor, audience="tool-github")
    assert r.status_code == 400 and r.json()["error"] == "invalid_dpop_proof" and "revoked" in r.json()["error_description"]
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "code-review-subagent", "client_secret": secret(sts, "code-review-subagent")},
                  headers={"DPoP": proof_for(sts, "code-review-subagent", "POST", token_url(http))})
    assert r.status_code == 400 and "revoked" in r.json()["error_description"]
    # The gateway's key and tokens are untouched, including the user token bound to it.
    assert github.verify(gw_tool).path == "user:alice > mcp-gateway"
    assert exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").status_code == 200
    assert sts.audit_log[-1]["event"] != "key_revocation"
    rec = [e for e in sts.audit_log if e["event"] == "key_revocation"][0]
    assert rec == {**rec, "decision": "revoked", "jkt": sa, "revoked_jtis": 4, "by": "admin"}


def test_revoking_the_gateway_key_takes_the_user_token_and_its_subtree(http, sts):
    user, handoff, sub_tool = full_chain(http, sts)
    n = sts.revoke_key(sts.clients["mcp-gateway"].jkt)
    assert n >= 4  # the user token (bound to the gateway), the gateway's actor token, and by lineage the handoff and tool token
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(sub_tool)
    assert claims(handoff)["jti"] in sts.denylist


def test_admin_revoke_key_over_http_and_cli(http, sts, capsys):
    user, handoff, sub_tool = full_chain(http, sts)
    gw_tool = exchange(http, sts, "mcp-gateway", user, audience="tool-github", scope="repo:read").json()["access_token"]
    ops = actor_token(http, sts, "ops-console")
    sa = sts.clients["code-review-subagent"].jkt
    # A non-admin actor is 403 as on the other routes.
    assert admin_call(http, sts, "/admin/revoke/key", actor_token(http, sts, "mcp-gateway"), jkt=sa).status_code == 403
    r = admin_call(http, sts, "/admin/revoke/key", ops, jkt=sa)
    assert r.status_code == 200 and r.json() == {"jkt": sa, "revoked_jtis": 3}  # handoff, tool token, the sub-agent's actor token
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(sub_tool)
    # Unknown thumbprint: 200, zero, audited, and still refused from then on.
    r = admin_call(http, sts, "/admin/revoke/key", ops, jkt="nope")
    assert r.status_code == 200 and r.json()["revoked_jtis"] == 0 and "nope" in sts.revoked_keys
    assert sts.audit_log[-1]["event"] == "key_revocation" and sts.audit_log[-1]["by"] == "ops-console"
    # CLI verb, on the gateway's key this time. (The sub-agent's key is gone, so no
    # new chain through it can be minted: that is the point.)
    gw = sts.clients["mcp-gateway"].jkt
    checked_tool(sts).verify(gw_tool)
    assert admin_cli.run(["revoke-key", gw], http, secret(sts, "ops-console"), sts.clients["ops-console"].key) == 0
    assert f'"jkt": "{gw}"' in capsys.readouterr().out
    with pytest.raises(DelegationRejected, match="revoked"):
        checked_tool(sts).verify(gw_tool)


def test_admin_routes_need_the_dpop_scheme_and_a_proof_over_the_token(http, sts):
    user, _, _ = full_chain(http, sts)
    gid = claims(user)["grant_id"]
    ops = actor_token(http, sts, "ops-console")
    key = sts.clients["ops-console"].key
    htu = str(http.base_url).rstrip("/") + "/admin/revoke/grant"
    # Bound token under the Bearer scheme.
    r = http.post("/admin/revoke/grant", data={"grant_id": gid}, headers={"Authorization": f"Bearer {ops}"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_dpop_proof" and "scheme" in r.json()["error_description"]
    # DPoP scheme, no proof; proof without ath; proof from another key; proof for /token.
    bad = [
        {},
        {"DPoP": make_proof(key, "POST", htu)},
        {"DPoP": make_proof(sts.clients["mcp-gateway"].key, "POST", htu, ath=ath(ops))},
        {"DPoP": make_proof(key, "POST", token_url(http), ath=ath(ops))},
    ]
    for headers in bad:
        r = http.post("/admin/revoke/grant", data={"grant_id": gid}, headers={"Authorization": f"DPoP {ops}", **headers})
        assert r.status_code == 401 and r.json()["error"] == "invalid_dpop_proof", headers
    assert gid not in sts.denylist
    r = http.post("/admin/revoke/grant", data={"grant_id": gid}, headers={"Authorization": f"DPoP {ops}", "DPoP": make_proof(key, "POST", htu, ath=ath(ops))})
    assert r.status_code == 200 and r.json()["revoked_jtis"] == 3


# ---- the flag ----------------------------------------------------------------

def test_require_dpop_off_restores_bearer_behaviour():
    sts = STS(issuer=ISS, require_dpop=False)
    http = TestClient(create_app(sts))
    gw = secret(sts, "mcp-gateway")
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": gw})
    assert r.status_code == 200 and r.json()["token_type"] == "Bearer"
    actor = r.json()["access_token"]
    assert "cnf" not in claims(actor)
    user = user_token(sts)
    assert "cnf" not in claims(user)
    r = exchange(http, sts, "mcp-gateway", user, actor=actor, audience="tool-github", scope="repo:read", dpop=None)
    assert r.status_code == 200 and r.json()["token_type"] == "Bearer"
    tool_tok = r.json()["access_token"]
    assert "cnf" not in claims(tool_tok)
    # The verifier takes it bare, and so does the admin route under Bearer.
    p = UpstreamVerifier(ISS, "tool-github", key=sts.public_key).verify(tool_tok)
    assert p.path == "user:alice > mcp-gateway" and p.jkt is None
    ops = http.post("/token", data={"grant_type": "client_credentials", "client_id": "ops-console", "client_secret": secret(sts, "ops-console")}).json()["access_token"]
    r = http.post("/admin/revoke/grant", data={"grant_id": claims(user)["grant_id"]}, headers={"Authorization": f"Bearer {ops}"})
    assert r.status_code == 200 and r.json()["revoked_jtis"] == 2
    # A client that sends a proof anyway gets a bound token: the flag makes DPoP optional, not ignored.
    r = http.post("/token", data={"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": gw},
                  headers={"DPoP": proof_for(sts, "mcp-gateway", "POST", token_url(http))})
    assert r.json()["token_type"] == "DPoP" and claims(r.json()["access_token"])["cnf"]["jkt"] == sts.clients["mcp-gateway"].jkt


def test_sts_replay_cache_and_proof_age_follow_the_injected_clock():
    now = [time.time()]
    sts = STS(issuer=ISS, clock=lambda: now[0])
    http = TestClient(create_app(sts))
    proof = proof_for(sts, "mcp-gateway", "POST", token_url(http))
    data = {"grant_type": "client_credentials", "client_id": "mcp-gateway", "client_secret": secret(sts, "mcp-gateway")}
    assert http.post("/token", data=data, headers={"DPoP": proof}).status_code == 200
    assert len(sts.dpop_seen) == 1
    now[0] += 61
    assert len(sts.dpop_seen) == 0  # pruned: a proof this old is refused on age, not on memory
    r = http.post("/token", data=data, headers={"DPoP": proof})
    assert r.status_code == 400 and "older than" in r.json()["error_description"]
    # A fresh proof on the moved clock works: the STS judges age by its own clock.
    assert http.post("/token", data=data, headers={"DPoP": proof_for(sts, "mcp-gateway", "POST", token_url(http))}).status_code == 200


def test_proving_verifier_matches_the_raw_verifier(http, sts):
    """The test wrapper adds nothing a real holder could not do: the same token and
    proof shape pass a plain UpstreamVerifier."""
    _, _, tool_tok = full_chain(http, sts)
    assert ProvingVerifier(sts, "tool-github").verify(tool_tok).jkt == present(raw_tool(sts), sts, tool_tok).jkt
