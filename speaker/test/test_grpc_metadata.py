"""The gRPC client sends the device credential as call metadata at every connect.

Pure unit test on OrchestratorGrpcClient._call_metadata and the config wiring:
no server, no channel. The provider is what DeviceAttestClient.grpc_metadata
returns; its failure must never take the call down with it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

grpc = pytest.importorskip("grpc")

from modules.speaker.grpc_client import OrchestratorGrpcClient, OrchestratorGrpcConfig  # noqa: E402


def client(provider):
    return OrchestratorGrpcClient(OrchestratorGrpcConfig(target="127.0.0.1:1", session_id="s", metadata_provider=provider))


def test_no_provider_means_no_metadata():
    assert asyncio.run(client(None)._call_metadata()) == ()


def test_provider_pairs_become_lowercased_metadata():
    md = asyncio.run(client(lambda: [("X-Device-Credential", "tok.en.sig")])._call_metadata())
    assert md == (("x-device-credential", "tok.en.sig"),)


def test_provider_called_at_every_connect_not_cached():
    calls = []

    def provider():
        calls.append(1)
        return [("x-device-credential", f"t{len(calls)}")]

    c = client(provider)
    assert asyncio.run(c._call_metadata()) == (("x-device-credential", "t1"),)
    assert asyncio.run(c._call_metadata()) == (("x-device-credential", "t2"),)


def test_empty_values_and_failures_send_nothing():
    assert asyncio.run(client(lambda: [("x-device-credential", "")])._call_metadata()) == ()

    def boom():
        raise RuntimeError("yubikey unplugged")

    assert asyncio.run(client(boom)._call_metadata()) == ()


def test_attest_client_metadata_shape_matches_what_the_server_reads(tmp_path):
    """DeviceAttestClient.grpc_metadata yields the header the orchestrator reads."""
    pytest.importorskip("cryptography")
    from modules.speaker.device_identity import CREDENTIAL_HEADER, DeviceAttestClient, FakeSigner

    class T:
        def post(self, path, payload):
            if path.endswith("/challenge"):
                return 200, {"nonce": "AAAA", "exp": 1}
            return 200, {"credential": "a.b.c", "exp": 2_000_000_000, "device_id": payload["device_id"]}

    c = DeviceAttestClient("kitchen", FakeSigner(), T(), credential_path=tmp_path / "cred.json")
    assert c.grpc_metadata() == [(CREDENTIAL_HEADER, "a.b.c")]
    assert CREDENTIAL_HEADER == "x-device-credential"
