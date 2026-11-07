"""Device-bound user tokens (SESSION_BINDING.md section 3): a user token bound to
the speaker's hardware key, exchanged by the consented client that asserts the
thumbprint it verified on the live stream, with `device` and `assurance`
carried down the chain and device revocation killing the subtree.
"""
import json

import jwt
import pytest

from sts import STS, create_app
from fastapi.testclient import TestClient

from test_delegation import ISS, TOOL_URL, actor_token, exchange, tool  # noqa: F401
from test_escalation_admin import admin_call
from test_revocation import checked_tool

KITCHEN = "kitchen-jkt-0123456789abcdef"
OFFICE = "office-jkt-0123456789abcdef"
ASSURANCE = {"method": "voice", "verified": True, "profile_id": "alice", "score": 0.91, "threshold": 0.86,
             "threshold_source": "profile", "origin": "device", "device_id": "kitchen", "device_jkt": KITCHEN,
             "spoken_confirmation": True, "phone_approval": False}


@pytest.fixture
def sts():
    s = STS(issuer=ISS)
    s.register_device("kitchen", KITCHEN)
    s.register_device("office", OFFICE)
    return s


@pytest.fixture
def http(sts):
    return TestClient(create_app(sts))


def claims_of(token):
    return jwt.decode(token, options={"verify_signature": False})


def device_user_token(sts, jkt=KITCHEN, assurance=ASSURANCE, **kw):
    return sts.issue_user_token("alice", "mcp-gateway", {"repo:read", "repo:write"}, device_jkt=jkt,
                                assurance=assurance, **kw)


# ---- issuance -----------------------------------------------------------------

def test_user_token_bound_to_device_key_not_client_key(sts):
    c = claims_of(device_user_token(sts))
    assert c["cnf"] == {"jkt": KITCHEN}
    assert c["device"] == {"id": "kitchen", "jkt": KITCHEN}
    assert c["assurance"]["score"] == 0.91 and c["assurance"]["device_jkt"] == KITCHEN
    assert c["cnf"]["jkt"] != sts.clients["mcp-gateway"].jkt


def test_without_device_the_pr16_binding_stands(sts):
    c = claims_of(sts.issue_user_token("alice", "mcp-gateway", {"repo:read"}))
    assert c["cnf"] == {"jkt": sts.clients["mcp-gateway"].jkt} and "device" not in c


def test_unregistered_or_revoked_device_cannot_bind(sts):
    with pytest.raises(Exception, match="not registered"):
        device_user_token(sts, jkt="nobody-knows-this-key")
    sts.revoke_device(OFFICE)
    with pytest.raises(Exception, match="not registered or is revoked"):
        device_user_token(sts, jkt=OFFICE)


def test_registry_file_round_trip(sts, tmp_path):
    reg = tmp_path / "device_registry.json"
    reg.write_text(json.dumps({"devices": {
        "porch": {"device_id": "porch", "thumbprint": "porch-jkt", "revoked_at": None},
        "garage": {"device_id": "garage", "thumbprint": "garage-jkt", "revoked_at": 1700000000.0},
        "junk": {"device_id": "x"},
    }}))
    assert sts.load_device_registry(str(reg)) == 2
    assert sts._device_live("porch-jkt") and sts._device_live("garage-jkt") is None


def test_dev_login_takes_device_and_assurance(http):
    r = http.post("/dev/login", data={"user": "alice", "client_id": "mcp-gateway", "scope": "repo:read",
                                      "device_jkt": KITCHEN, "assurance": json.dumps(ASSURANCE)})
    assert r.status_code == 200, r.text
    c = claims_of(r.json()["access_token"])
    assert c["device"]["id"] == "kitchen" and c["assurance"]["method"] == "voice"
    bad = http.post("/dev/login", data={"user": "alice", "client_id": "mcp-gateway", "scope": "repo:read",
                                        "assurance": "{not json"})
    assert bad.status_code == 400


# ---- exchange -----------------------------------------------------------------

def test_consented_client_exchanges_by_asserting_the_stream_device(http, sts):
    r = exchange(http, sts, "mcp-gateway", device_user_token(sts), audience="tool-github", device_jkt=KITCHEN)
    assert r.status_code == 200, r.text
    c = claims_of(r.json()["access_token"])
    # The tool token is bound to the gateway's own key (it presents it), and carries the session facts.
    assert c["cnf"] == {"jkt": sts.clients["mcp-gateway"].jkt}
    assert c["device"] == {"id": "kitchen", "jkt": KITCHEN} and c["assurance"]["threshold"] == 0.86
    assert sts.audit_log[-1]["device_id"] == "kitchen"


def test_exchange_without_device_assertion_refused(http, sts):
    r = exchange(http, sts, "mcp-gateway", device_user_token(sts), audience="tool-github")
    assert r.status_code == 400 and "device_jkt from the live stream required" in r.json()["error_description"]


def test_exchange_with_another_device_refused(http, sts):
    r = exchange(http, sts, "mcp-gateway", device_user_token(sts), audience="tool-github", device_jkt=OFFICE)
    assert r.status_code == 400 and "does not match the key" in r.json()["error_description"]


def test_exchange_still_needs_the_actors_own_proof(http, sts):
    # Asserting the device is not a substitute for proving the actor key.
    r = exchange(http, sts, "mcp-gateway", device_user_token(sts), audience="tool-github", device_jkt=KITCHEN, dpop=None)
    assert r.status_code == 400 and r.json()["error"] == "invalid_dpop_proof"


def test_only_the_consented_client_can_exchange(http, sts):
    # notes-agent asserts the right device but the user never consented to it.
    r = exchange(http, sts, "notes-agent", device_user_token(sts), audience="tool-fs", device_jkt=KITCHEN)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert "may_act" in r.json()["error_description"] or "does not name this client" in r.json()["error_description"]


def test_device_claim_without_binding_is_refused(http, sts):
    # A user token that names a device but whose cnf is not that key is not device-bound.
    forged = claims_of(device_user_token(sts))
    forged["cnf"] = {"jkt": sts.clients["mcp-gateway"].jkt}
    token = sts._sign(forged)
    r = exchange(http, sts, "mcp-gateway", token, audience="tool-github", device_jkt=KITCHEN)
    assert r.status_code == 400 and "does not match" in r.json()["error_description"]


def test_session_facts_travel_to_the_handoff_and_the_subagent(http, sts):
    handoff = exchange(http, sts, "mcp-gateway", device_user_token(sts), delegate_to="code-review-subagent",
                       scope="repo:read", device_jkt=KITCHEN)
    assert handoff.status_code == 200, handoff.text
    h = claims_of(handoff.json()["access_token"])
    assert h["device"]["id"] == "kitchen" and h["assurance"]["method"] == "voice"
    # The handoff is bound to the delegate's key (PR #16); the sub-agent exchanges it as before, no device assertion.
    r = exchange(http, sts, "code-review-subagent", handoff.json()["access_token"], audience="tool-github")
    assert r.status_code == 200, r.text
    t = claims_of(r.json()["access_token"])
    assert t["device"]["id"] == "kitchen" and t["assurance"]["score"] == 0.91


def test_tool_verifier_exposes_device_and_assurance(http, sts):
    r = exchange(http, sts, "mcp-gateway", device_user_token(sts), audience="tool-github", device_jkt=KITCHEN)
    p = tool(sts, "tool-github").verify(r.json()["access_token"])
    assert p.device == {"id": "kitchen", "jkt": KITCHEN}
    assert p.assurance["verified"] is True and p.assurance["phone_approval"] is False
    plain = exchange(http, sts, "mcp-gateway", sts.issue_user_token("bob", "mcp-gateway", {"repo:read"}), audience="tool-github")
    q = tool(sts, "tool-github").verify(plain.json()["access_token"])
    assert q.device is None and q.assurance is None


def test_assurance_cannot_be_widened_at_exchange(http, sts):
    # The exchange ignores any assurance the client sends; the claim is copied from the subject.
    r = exchange(http, sts, "mcp-gateway", device_user_token(sts), audience="tool-github", device_jkt=KITCHEN,
                 assurance=json.dumps({**ASSURANCE, "phone_approval": True}))
    assert r.status_code == 200
    assert claims_of(r.json()["access_token"])["assurance"]["phone_approval"] is False


# ---- revocation ---------------------------------------------------------------

def test_revoking_the_device_kills_live_tokens_and_future_exchanges(http, sts):
    user = device_user_token(sts)
    r = exchange(http, sts, "mcp-gateway", user, audience="tool-github", device_jkt=KITCHEN)
    live = r.json()["access_token"]
    assert checked_tool(sts).verify(live)
    n = sts.revoke_device(KITCHEN, by="ops")
    assert n >= 2  # the user token and the tool token at least
    with pytest.raises(Exception, match="revoked"):
        checked_tool(sts).verify(live)
    again = exchange(http, sts, "mcp-gateway", user, audience="tool-github", device_jkt=KITCHEN)
    assert again.status_code == 400 and "revoked" in again.json()["error_description"]
    with pytest.raises(Exception, match="revoked"):
        device_user_token(sts)
    assert sts.audit_log[-1]["event"] in ("device_revocation", "token_exchange")


def test_admin_route_revokes_a_device(http, sts):
    user = device_user_token(sts)
    exchange(http, sts, "mcp-gateway", user, audience="tool-github", device_jkt=KITCHEN)
    ops = actor_token(http, sts, "ops-console")
    r = admin_call(http, sts, "/admin/revoke/device", bearer=ops, jkt=KITCHEN)
    assert r.status_code == 200 and r.json()["revoked_jtis"] >= 2
    denied = admin_call(http, sts, "/admin/revoke/device", bearer=actor_token(http, sts, "mcp-gateway"), jkt=OFFICE)
    assert denied.status_code == 403
    assert sts._device_live(OFFICE) is not None
