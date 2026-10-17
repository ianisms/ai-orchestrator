"""Speaker-side device identity: FakeSigner, PIN file rules, enrollment script.

The cross-side challenge flow is tested in server/test/test_device_identity.py
with the same FakeSigner. These tests cover what is only on the speaker.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

pytest.importorskip("cryptography")

from modules.speaker.device_identity import (  # noqa: E402
    DeviceAttestClient,
    DeviceIdentityError,
    FakeSigner,
    challenge_message,
    read_pin,
)
import enroll_device_key  # noqa: E402


def test_fake_signer_signature_verifies_over_the_prefixed_message():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    s = FakeSigner()
    nonce = os.urandom(32)
    sig = s.sign(nonce)
    pub = serialization.load_pem_public_key(s.public_key_pem().encode())
    pub.verify(sig, challenge_message(nonce), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(Exception):
        pub.verify(sig, nonce, ec.ECDSA(hashes.SHA256()))  # the bare nonce is not what was signed


def test_read_pin_requires_mode_600(tmp_path):
    p = tmp_path / "device_pin"
    assert read_pin(p) is None
    p.write_text("123456\n")
    os.chmod(p, 0o644)
    with pytest.raises(DeviceIdentityError, match="chmod 600"):
        read_pin(p)
    os.chmod(p, 0o600)
    assert read_pin(p) == "123456"


def test_attest_client_fails_soft_when_server_refuses():
    class Refusing:
        def post(self, path, payload):
            return 403, {"error": "device is not enrolled"}

    c = DeviceAttestClient("speaker-1", FakeSigner(), Refusing(), credential_path=None)
    with pytest.raises(DeviceIdentityError, match="not enrolled"):
        c.attest()
    assert c.current() is None
    assert c.grpc_metadata() == []


def test_enroll_script_writes_request_with_fake_signer(tmp_path, capsys):
    out = tmp_path / "device_enrollment.json"
    signer = FakeSigner()
    rc = enroll_device_key.main(
        ["generate", "--device-id", "speaker-1", "--room", "kitchen", "--household", "home",
         "--out", str(out), "--yes"],
        signer=signer,
    )
    assert rc == 0
    req = json.loads(out.read_text())
    assert req["device_id"] == "speaker-1"
    assert req["room"] == "kitchen" and req["household"] == "home"
    assert req["public_key_pem"] == signer.public_key_pem()
    assert req["requested_at"] > 0
    assert "enroll_device.py add" in capsys.readouterr().err


def test_enroll_script_registers_directly_when_registry_given(tmp_path):
    out = tmp_path / "device_enrollment.json"
    reg = tmp_path / "device_registry.json"
    signer = FakeSigner()
    rc = enroll_device_key.main(
        ["generate", "--device-id", "speaker-1", "--room", "kitchen", "--household", "home",
         "--out", str(out), "--registry", str(reg), "--yes"],
        signer=signer,
    )
    assert rc == 0
    entry = json.loads(reg.read_text())["devices"]["speaker-1"]
    assert entry["public_key_pem"].strip() == signer.public_key_pem().strip()
    assert entry["room"] == "kitchen" and entry["thumbprint"]


def test_enroll_script_show_prints_public_key(capsys):
    signer = FakeSigner()
    assert enroll_device_key.main(["show"], signer=signer) == 0
    assert capsys.readouterr().out == signer.public_key_pem()


def test_enroll_script_needs_device_id():
    assert enroll_device_key.main(["generate", "--yes"], signer=FakeSigner()) == 2
