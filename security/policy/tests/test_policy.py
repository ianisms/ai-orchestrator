import httpx
import pytest
from fastapi.testclient import TestClient

import harness
from mcp_server import ToolServer
from pdp import create_app as create_pdp
from pep import PEP, ApprovalStore
from sts import STS
from verify import UpstreamVerifier

pytestmark = [
    pytest.mark.skipif(not harness.find_opa(), reason="opa binary not found (PATH or OPA_BIN)"),
    pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument"),
]
ISS = "http://sts.test"
PR = {"repo": "acme/widgets", "pr": 42}


@pytest.fixture(scope="module")
def opa():
    s = harness.OPAServer(harness.find_opa())
    yield s
    s.stop()


@pytest.fixture
def env(opa):
    sts = STS(issuer=ISS)
    approvals = ApprovalStore()
    pep = PEP(TestClient(create_pdp(opa.url)), harness.catalog(), session_resources=["acme/widgets"], approvals=approvals)
    server = ToolServer(UpstreamVerifier(ISS, "tool-github", key=sts.public_key), pep)

    def ex(cid, subject, **kw):
        act = sts.client_credentials(cid, sts.clients[cid].secret)["access_token"]
        return sts.exchange(client_id=cid, client_secret=sts.clients[cid].secret, subject_token=subject, actor_token=act, **kw)["access_token"]

    alice = sts.issue_user_token("alice", "mcp-gateway", {"repo:read", "repo:write"})
    toks = {
        "gateway": ex("mcp-gateway", alice, audience="tool-github", scope="repo:read repo:write"),
        "sub": ex("code-review-subagent", ex("mcp-gateway", alice, delegate_to="code-review-subagent", scope="repo:read"), audience="tool-github"),
    }
    return sts, server, pep, approvals, toks


def call(server, tok, tool, args=PR):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}, tok)


def reasons(resp):
    return resp["error"]["data"]["reasons"]


def test_subagent_read_allowed(env):
    _, server, *_, toks = env
    assert "result" in call(server, toks["sub"], "github.get_pr")


def test_injected_write_via_subagent_denied(env):
    _, server, *_, toks = env
    r = reasons(call(server, toks["sub"], "github.merge_pr"))
    assert "code-review-subagent is not granted github.merge_pr" in r
    assert "token lacks scope repo:write" in r


def test_injected_write_via_gateway_needs_approval(env):
    _, server, *_, toks = env
    assert reasons(call(server, toks["gateway"], "github.merge_pr")) == ["write-class tool needs the user's approval for this exact call"]


def test_approval_is_exact_and_single_use(env):
    _, server, _, approvals, toks = env
    approvals.approve("user:alice", "github.merge_pr", PR)
    assert "error" in call(server, toks["gateway"], "github.merge_pr", {"repo": "acme/widgets", "pr": 43})
    assert "result" in call(server, toks["gateway"], "github.merge_pr")
    assert "error" in call(server, toks["gateway"], "github.merge_pr")


def test_resource_outside_session_denied(env):
    _, server, *_, toks = env
    assert reasons(call(server, toks["gateway"], "github.get_pr", {"repo": "acme/secrets", "pr": 1})) == ["resource acme/secrets is outside this session"]


def test_fail_closed_when_opa_unreachable(env):
    _, server, pep, _, toks = env
    pep.pdp = TestClient(create_pdp("http://127.0.0.1:9"))
    r = call(server, toks["sub"], "github.get_pr")
    assert r["error"]["data"]["reasons"][0].startswith("fail closed")
    assert pep.audit_log[-1]["fail_closed"] is True


def test_fail_closed_on_pdp_timeout(env):
    _, server, pep, _, toks = env

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    pep.pdp = httpx.Client(base_url="http://pdp", transport=httpx.MockTransport(boom))
    assert reasons(call(server, toks["sub"], "github.get_pr"))[0].startswith("fail closed: ReadTimeout")


def test_fail_closed_on_malformed_answer(env):
    _, server, pep, _, toks = env
    pep.pdp = httpx.Client(base_url="http://pdp", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"decision": "yes"})))
    assert reasons(call(server, toks["sub"], "github.get_pr"))[0].startswith("fail closed")


def test_every_decision_logged_with_path_and_reasons(env):
    _, server, pep, _, toks = env
    call(server, toks["sub"], "github.merge_pr")
    e = pep.audit_log[-1]
    assert e["path"] == "user:alice > mcp-gateway > code-review-subagent"
    assert e["decision"] == "deny" and e["reasons"]


# ---- voice step-up ----------------------------------------------------------

VOICE_OK = {"confidence": 0.99, "local_network": True, "liveness": True}


def voice_call(server, tok, tool, args=PR, voice=VOICE_OK, prefs=None):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    return server.handle(rpc, tok, channel="voice", voice=voice, prefs=prefs)


def test_voice_read_allowed_when_checks_pass(env):
    _, server, *_, toks = env
    assert "result" in voice_call(server, toks["sub"], "github.get_pr")


@pytest.mark.parametrize("override, reason", [
    ({"confidence": 0.95}, "voice match below 0.98"),
    ({"local_network": False}, "voice request did not come from the home network"),
    ({"liveness": False}, "voice liveness check not passed"),
])
def test_voice_failed_check_denied(env, override, reason):
    _, server, *_, toks = env
    assert reasons(voice_call(server, toks["sub"], "github.get_pr", voice={**VOICE_OK, **override})) == [reason]


def test_voice_missing_signals_fail_closed(env):
    _, server, *_, toks = env
    assert len(reasons(voice_call(server, toks["sub"], "github.get_pr", voice={}))) == 3


def test_voice_high_risk_confirmation_cannot_be_waived(env):
    _, server, _, approvals, toks = env
    prefs = {"confirm_writes": False}
    assert reasons(voice_call(server, toks["gateway"], "github.merge_pr", prefs=prefs)) == [
        "write-class tool needs the user's approval for this exact call"]
    approvals.approve("user:alice", "github.merge_pr", PR)
    assert "result" in voice_call(server, toks["gateway"], "github.merge_pr", prefs=prefs)


def test_voice_normal_write_waivable_by_user(env):
    _, server, *_, toks = env
    assert "error" in voice_call(server, toks["gateway"], "github.comment")
    assert "result" in voice_call(server, toks["gateway"], "github.comment", prefs={"confirm_writes": False})
