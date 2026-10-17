"""Device identity: registry, boot-time challenge, device credential.

Runs with the speaker's FakeSigner so CI needs no YubiKey. The speaker module is
imported from the sibling tree so the wire format is checked across both sides.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "modules", "orchestrator", "core"))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "speaker"))

pytest.importorskip("cryptography")

import device_identity as server_side  # noqa: E402
import enroll_device  # noqa: E402
from device_identity import (  # noqa: E402
    CREDENTIAL_HEADER,
    DeviceIdentityError,
    DeviceIdentityHttp,
    DeviceIdentityService,
    EnrollmentRegistry,
    b64url,
    b64url_decode,
    key_thumbprint,
)
from modules.speaker import device_identity as speaker_side  # noqa: E402
from modules.speaker.device_identity import DeviceAttestClient, FakeSigner  # noqa: E402


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


class LocalTransport:
    """Speaker-side transport that calls the server-side HTTP glue in process."""

    def __init__(self, http: DeviceIdentityHttp):
        self.http = http

    def post(self, path, payload):
        return self.http.post(path, json.dumps(payload).encode())


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry(tmp_path, clock):
    return EnrollmentRegistry(tmp_path / "state" / "device_registry.json", clock=clock)


@pytest.fixture
def service(registry, clock):
    return DeviceIdentityService(registry=registry, clock=clock, secret=b"test" * 8)


@pytest.fixture
def signer():
    return FakeSigner()


@pytest.fixture
def enrolled(registry, signer):
    return registry.enroll("speaker-1", "kitchen", "home", signer.public_key_pem())


def client(service, signer, device_id="speaker-1", credential_path=None):
    return DeviceAttestClient(device_id, signer, LocalTransport(DeviceIdentityHttp(service)),
                              credential_path=credential_path, clock=service.clock)


# ---- enrollment ---------------------------------------------------------------

def test_enroll_writes_registry_entry(registry, signer, clock):
    entry = registry.enroll("speaker-1", "kitchen", "home", signer.public_key_pem())
    on_disk = json.loads(registry.path.read_text())["devices"]["speaker-1"]
    assert on_disk["device_id"] == "speaker-1"
    assert on_disk["room"] == "kitchen" and on_disk["household"] == "home"
    assert on_disk["public_key_pem"].startswith("-----BEGIN PUBLIC KEY-----")
    assert on_disk["enrolled_at"] == clock.t
    assert on_disk["thumbprint"] == entry.thumbprint == key_thumbprint(signer.public_key_pem())
    assert on_disk["revoked_at"] is None
    assert registry.get("speaker-1").thumbprint == entry.thumbprint


def test_enroll_refuses_silent_key_swap(registry, signer):
    registry.enroll("speaker-1", "kitchen", "home", signer.public_key_pem())
    other = FakeSigner()
    with pytest.raises(DeviceIdentityError, match="already enrolled"):
        registry.enroll("speaker-1", "kitchen", "home", other.public_key_pem())
    rotated = registry.enroll("speaker-1", "kitchen", "home", other.public_key_pem(), replace=True)
    assert rotated.thumbprint == key_thumbprint(other.public_key_pem())


def test_enroll_rejects_bad_ids_and_keys(registry, signer):
    with pytest.raises(DeviceIdentityError, match="plain token"):
        registry.enroll("../etc", "kitchen", "home", signer.public_key_pem())
    with pytest.raises((DeviceIdentityError, ValueError)):
        registry.enroll("speaker-1", "kitchen", "home", "not a key")


def test_enroll_script_add_list_revoke(tmp_path, signer, capsys):
    reg = tmp_path / "device_registry.json"
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"device_id": "speaker-2", "room": "office", "household": "home",
                               "public_key_pem": signer.public_key_pem()}))
    assert enroll_device.main(["add", "--request", str(req), "--registry", str(reg)]) == 0
    assert enroll_device.main(["add", "--request", str(req), "--registry", str(reg)]) == 1  # no silent swap
    assert enroll_device.main(["list", "--registry", str(reg)]) == 0
    out = capsys.readouterr().out
    assert "speaker-2" in out and "active" in out
    assert enroll_device.main(["revoke", "speaker-2", "--registry", str(reg)]) == 0
    assert EnrollmentRegistry(reg).get("speaker-2").revoked
    assert enroll_device.main(["remove", "speaker-2", "--registry", str(reg)]) == 0
    assert EnrollmentRegistry(reg).get("speaker-2") is None


# ---- challenge --------------------------------------------------------------

def test_challenge_succeeds_with_the_enrolled_key(service, signer, enrolled, clock):
    cred = client(service, signer).attest()
    assert cred.device_id == "speaker-1"
    verified = service.verify_credential(cred.token)
    assert verified.claims["sub"] == "speaker-1"
    assert verified.claims["room"] == "kitchen" and verified.claims["household"] == "home"
    assert verified.thumbprint == enrolled.thumbprint


def test_challenge_fails_with_the_wrong_key(service, enrolled):
    with pytest.raises(speaker_side.DeviceIdentityError, match="signature does not verify"):
        client(service, FakeSigner()).attest()


def test_challenge_fails_with_an_expired_nonce(service, signer, enrolled, clock):
    challenge = service.issue_challenge("speaker-1")
    sig = signer.sign(b64url_decode(challenge["nonce"]))
    clock.tick(service.challenge_ttl_s + 1)
    with pytest.raises(DeviceIdentityError, match="expired"):
        service.attest("speaker-1", challenge["nonce"], b64url(sig))


def test_challenge_fails_on_replay(service, signer, enrolled):
    challenge = service.issue_challenge("speaker-1")
    sig = b64url(signer.sign(b64url_decode(challenge["nonce"])))
    service.attest("speaker-1", challenge["nonce"], sig)
    with pytest.raises(DeviceIdentityError, match="already used"):
        service.attest("speaker-1", challenge["nonce"], sig)


def test_failed_attempt_also_consumes_the_nonce(service, signer, enrolled):
    challenge = service.issue_challenge("speaker-1")
    with pytest.raises(DeviceIdentityError, match="signature does not verify"):
        service.attest("speaker-1", challenge["nonce"], b64url(FakeSigner().sign(b64url_decode(challenge["nonce"]))))
    with pytest.raises(DeviceIdentityError, match="already used"):
        service.attest("speaker-1", challenge["nonce"], b64url(signer.sign(b64url_decode(challenge["nonce"]))))


def test_challenge_fails_for_a_device_with_no_enrolled_key(service, signer):
    with pytest.raises(speaker_side.DeviceIdentityError, match="not enrolled"):
        client(service, signer, device_id="speaker-9").attest()


def test_challenge_fails_when_the_device_key_is_missing(service, enrolled):
    class NoKey:
        def sign(self, nonce):
            raise speaker_side.DeviceIdentityError("no YubiKey with a smart card interface is attached")

        def public_key_pem(self):
            raise speaker_side.DeviceIdentityError("no YubiKey")

    c = client(service, NoKey())
    with pytest.raises(speaker_side.DeviceIdentityError, match="no YubiKey"):
        c.attest()
    assert c.current() is None  # fails soft at boot, logged, no credential


def test_challenge_fails_for_a_revoked_device(service, registry, signer, enrolled):
    registry.revoke("speaker-1")
    with pytest.raises(speaker_side.DeviceIdentityError, match="revoked"):
        client(service, signer).attest()


def test_nonce_is_bound_to_the_device_that_asked(service, registry, signer, enrolled):
    other = FakeSigner()
    registry.enroll("speaker-2", "office", "home", other.public_key_pem())
    challenge = service.issue_challenge("speaker-2")
    with pytest.raises(DeviceIdentityError, match="another device"):
        service.attest("speaker-1", challenge["nonce"], b64url(signer.sign(b64url_decode(challenge["nonce"]))))


def test_unknown_nonce_and_garbage_are_refused(service, signer, enrolled):
    with pytest.raises(DeviceIdentityError, match="unknown"):
        service.attest("speaker-1", "nope", "nope")
    challenge = service.issue_challenge("speaker-1")
    with pytest.raises(DeviceIdentityError, match="signature does not verify"):
        service.attest("speaker-1", challenge["nonce"], b64url(b"\x30\x00"))


def test_every_denial_is_logged_with_a_reason(service, signer, enrolled, caplog):
    caplog.set_level("INFO", logger="device_identity.audit")
    with pytest.raises(DeviceIdentityError):
        service.attest("speaker-1", "nope", "nope")
    events = [json.loads(r.message) for r in caplog.records if r.name == "device_identity.audit"]
    assert events and events[-1]["event"] == "device_attest_refused"
    assert events[-1]["reason"] and events[-1]["device_id"] == "speaker-1"


def test_pending_challenges_are_bounded(service):
    for i in range(server_side.MAX_PENDING_CHALLENGES + 50):
        service.issue_challenge(f"flood-{i}")
    assert len(service._pending) <= server_side.MAX_PENDING_CHALLENGES


# ---- credential -------------------------------------------------------------

def test_credential_carries_cnf_and_expires(service, signer, enrolled, clock):
    cred = client(service, signer).attest()
    claims = service.verify_credential(cred.token).claims
    assert claims["cnf"] == {"jkt": enrolled.thumbprint}
    assert claims["exp"] - claims["iat"] == service.credential_ttl_s
    assert 300 <= claims["exp"] - claims["iat"] <= 900
    assert cred.exp == claims["exp"]
    clock.tick(service.credential_ttl_s + 1)
    with pytest.raises(DeviceIdentityError, match="expired"):
        service.verify_credential(cred.token)


def test_credential_ttl_outside_window_is_rejected(registry):
    with pytest.raises(ValueError):
        DeviceIdentityService(registry=registry, credential_ttl_s=60)
    with pytest.raises(ValueError):
        DeviceIdentityService(registry=registry, credential_ttl_s=3600)


def test_credential_dies_with_revocation_and_tamper(service, registry, signer, enrolled):
    cred = client(service, signer).attest()
    head, body, sig = cred.token.split(".")
    with pytest.raises(DeviceIdentityError, match="signature"):
        service.verify_credential(head + "." + body + "." + sig[:-2] + "AA")
    forged = DeviceIdentityService(registry=registry, secret=b"other" * 8)
    with pytest.raises(DeviceIdentityError, match="signature"):
        forged.verify_credential(cred.token)
    registry.revoke("speaker-1")
    with pytest.raises(DeviceIdentityError, match="revoked"):
        service.verify_credential(cred.token)


def test_credential_hook_reads_grpc_metadata(service, signer, enrolled):
    c = client(service, signer)
    md = dict(c.grpc_metadata())
    assert CREDENTIAL_HEADER in md
    assert service.device_from_metadata(md).device_id == "speaker-1"
    assert service.device_from_metadata({}) is None
    assert service.device_from_metadata({CREDENTIAL_HEADER: "junk"}) is None


def test_client_reattests_near_expiry(service, signer, enrolled, clock):
    c = client(service, signer)
    first = c.current()
    assert first is not None
    clock.tick(service.credential_ttl_s - 5)  # inside the skew window: re-attest
    second = c.current()
    assert second is not None and second.token != first.token
    assert service.verify_credential(second.token).device_id == "speaker-1"


def test_client_persists_credential(service, signer, enrolled, tmp_path):
    path = tmp_path / "device_credential.json"
    c = client(service, signer, credential_path=path)
    cred = c.attest()
    assert path.exists() and (path.stat().st_mode & 0o077) == 0
    again = client(service, signer, credential_path=path)
    assert again.current().token == cred.token  # loaded from disk, no new challenge


# ---- wire format ------------------------------------------------------------

def test_both_sides_agree_on_the_challenge_message():
    assert speaker_side.CHALLENGE_PREFIX == server_side.CHALLENGE_PREFIX
    assert speaker_side.CREDENTIAL_HEADER == server_side.CREDENTIAL_HEADER
    assert speaker_side.challenge_message(b"x") == server_side.challenge_message(b"x")


def test_http_glue_rejects_bad_bodies(service):
    http = DeviceIdentityHttp(service)
    assert http.post("/device/challenge", b"not json")[0] == 400
    assert http.post("/device/challenge", b"[]")[0] == 400
    assert http.post("/device/challenge", b"{}")[0] == 403  # missing device_id
    assert http.post("/device/other", b"{}")[0] == 404
    assert http.post("/device/attest", b"x" * (http.max_body + 1))[0] == 413
    code, payload = http.post("/device/attest", json.dumps({"device_id": "speaker-1", "nonce": "n", "signature": "s"}).encode())
    assert code == 403 and "nonce" in payload["error"]
