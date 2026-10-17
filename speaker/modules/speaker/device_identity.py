"""Device identity on the speaker: a signer behind a hardware-held key, and the
boot-time challenge client that turns it into a short-lived device credential.

Signer
    sign(nonce) -> bytes        ECDSA P-256 / SHA-256, DER encoded, over
                                challenge_message(nonce)
    public_key_pem() -> str     SubjectPublicKeyInfo PEM

YubiKeyPivSigner    the key in PIV slot 9a of a plugged-in YubiKey 5. Touch
                    policy never (nobody is there to touch it); PIN policy
                    once, with the PIN read from a root-only file, so the
                    container can sign after a reboot without a person. The
                    private key never leaves the YubiKey. yubikey-manager is
                    imported lazily; nothing else in the speaker needs it.
FakeSigner          an in-memory P-256 key for tests and CI. Same bytes on the
                    wire as the YubiKey; needs `cryptography`.

The orchestrator half is server/modules/orchestrator/core/device_identity.py.
CHALLENGE_PREFIX must match it byte for byte; the test suite checks that.

What this proves and what it does not: the device holds the enrolled key. It
does not prove what software is running (no measured boot, no PCRs). See
security/DEVICE_IDENTITY.md.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

log = logging.getLogger("speaker.device_identity")

CHALLENGE_PREFIX = b"ai-orchestrator device challenge v1\n"
CREDENTIAL_HEADER = "x-device-credential"
DEFAULT_PIN_PATH = "/ai/speaker/state/device_pin"
DEFAULT_CREDENTIAL_PATH = "/ai/speaker/state/device_credential.json"
PIV_SLOT_AUTHENTICATION = 0x9A


class DeviceIdentityError(Exception):
    pass


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def challenge_message(nonce: bytes) -> bytes:
    return CHALLENGE_PREFIX + nonce


# ---- signers ----------------------------------------------------------------

class Signer(Protocol):
    def sign(self, nonce: bytes) -> bytes:
        """DER-encoded ECDSA P-256 / SHA-256 signature over challenge_message(nonce)."""
        ...

    def public_key_pem(self) -> str:
        ...


class FakeSigner:
    """In-memory P-256 key. For tests and CI only; the key lives in process memory."""

    def __init__(self, private_key=None):
        from cryptography.hazmat.primitives.asymmetric import ec

        self._key = private_key or ec.generate_private_key(ec.SECP256R1())

    def sign(self, nonce: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        return self._key.sign(challenge_message(nonce), ec.ECDSA(hashes.SHA256()))

    def public_key_pem(self) -> str:
        from cryptography.hazmat.primitives import serialization

        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")


def read_pin(path: str | os.PathLike = DEFAULT_PIN_PATH) -> Optional[str]:
    """The PIV PIN from a file readable only by the speaker user, or None.
    Refuses a world-readable file: a PIN anyone on the host can read is not a
    second factor against someone who takes the YubiKey."""
    p = Path(path)
    if not p.exists():
        return None
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        raise DeviceIdentityError(f"{p} is readable by group or others (mode {mode:03o}); chmod 600 it")
    pin = p.read_text(encoding="utf-8").strip()
    return pin or None


class YubiKeyPivSigner:
    """Signs with the ECCP256 key in PIV slot 9a of the first attached YubiKey.

    Session handling: a connection is opened per call and closed after it. Boot
    attestation signs once every few minutes at most, so the cost does not
    matter and a stale smart card session after a USB hiccup cannot strand
    the client.
    """

    def __init__(self, pin: Optional[str] = None, pin_path: str | os.PathLike = DEFAULT_PIN_PATH,
                 serial: Optional[int] = None):
        self._pin = pin
        self._pin_path = pin_path
        self._serial = serial

    # -- hardware access, all lazy

    @staticmethod
    def _imports():
        try:
            from yubikit.core.smartcard import SmartCardConnection
            from yubikit.piv import KEY_TYPE, PIN_POLICY, SLOT, TOUCH_POLICY, PivSession
            from ykman.device import list_all_devices
        except ImportError as e:  # pragma: no cover - exercised only on hardware hosts
            raise DeviceIdentityError(
                "yubikey-manager is not installed; pip install yubikey-manager (speaker/test/requirements.txt lists it)"
            ) from e
        return SmartCardConnection, PivSession, SLOT, KEY_TYPE, PIN_POLICY, TOUCH_POLICY, list_all_devices

    def _device(self):
        SmartCardConnection, _, _, _, _, _, list_all_devices = self._imports()
        devices = list_all_devices([SmartCardConnection])
        for dev, info in devices:
            if self._serial is None or info.serial == self._serial:
                return dev, info
        raise DeviceIdentityError("no YubiKey with a smart card interface is attached" if self._serial is None
                                  else f"YubiKey serial {self._serial} is not attached")

    def _session(self):
        SmartCardConnection, PivSession, *_ = self._imports()
        dev, _info = self._device()
        conn = dev.open_connection(SmartCardConnection)
        return conn, PivSession(conn)

    # -- Signer

    def public_key_pem(self) -> str:
        from cryptography.hazmat.primitives import serialization

        _, _, SLOT, *_ = self._imports()
        conn, piv = self._session()
        try:
            meta = piv.get_slot_metadata(SLOT.AUTHENTICATION)
        except Exception as e:
            raise DeviceIdentityError(f"no key in PIV slot 9a: {e}") from e
        finally:
            conn.close()
        return meta.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")

    def sign(self, nonce: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes

        _, _, SLOT, KEY_TYPE, PIN_POLICY, _, _ = self._imports()
        conn, piv = self._session()
        try:
            meta = piv.get_slot_metadata(SLOT.AUTHENTICATION)
            if meta.key_type != KEY_TYPE.ECCP256:
                raise DeviceIdentityError(f"slot 9a holds {meta.key_type.name}, expected ECCP256")
            if meta.pin_policy != PIN_POLICY.NEVER:
                pin = self._pin if self._pin is not None else read_pin(self._pin_path)
                if not pin:
                    raise DeviceIdentityError(f"slot 9a needs a PIN and none was found at {self._pin_path}")
                piv.verify_pin(pin)
            return piv.sign(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256, challenge_message(nonce), hashes.SHA256())
        finally:
            conn.close()

    # -- enrollment helpers (used by scripts/enroll_device_key.py)

    def generate_key(self, management_key: bytes, pin_policy: str = "once") -> str:
        """Generate a fresh ECCP256 key in slot 9a with touch policy never.
        Overwrites whatever is in the slot. Returns the public key PEM."""
        from cryptography.hazmat.primitives import serialization

        _, _, SLOT, KEY_TYPE, PIN_POLICY, TOUCH_POLICY, _ = self._imports()
        policies = {"once": PIN_POLICY.ONCE, "never": PIN_POLICY.NEVER, "always": PIN_POLICY.ALWAYS}
        if pin_policy not in policies:
            raise DeviceIdentityError(f"pin_policy must be one of {sorted(policies)}")
        conn, piv = self._session()
        try:
            piv.authenticate(management_key)
            pub = piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256,
                                   pin_policy=policies[pin_policy], touch_policy=TOUCH_POLICY.NEVER)
        finally:
            conn.close()
        return pub.public_bytes(serialization.Encoding.PEM,
                                serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")

    def serial(self) -> Optional[int]:
        _, info = self._device()
        return info.serial


# ---- boot-time challenge client ---------------------------------------------

@dataclass
class DeviceCredential:
    token: str
    exp: int
    device_id: str

    def expired(self, now: Optional[float] = None, skew_s: int = 15) -> bool:
        return (now if now is not None else time.time()) >= self.exp - skew_s


class _Transport(Protocol):
    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        ...


class HttpTransport:
    """POST JSON to the orchestrator status server (default port 9111)."""

    def __init__(self, base_url: str, timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.getcode(), json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read() or b"{}")
            except ValueError:
                data = {}
            return e.code, data if isinstance(data, dict) else {}


class DeviceAttestClient:
    """Runs the challenge: ask for a nonce, sign it, exchange the signature for
    a credential. Keeps the credential in memory and optionally on disk so a
    quick restart does not need the YubiKey again before the credential expires.
    """

    def __init__(self, device_id: str, signer: Signer, transport: _Transport,
                 credential_path: Optional[str | os.PathLike] = DEFAULT_CREDENTIAL_PATH,
                 clock: Callable[[], float] = time.time):
        self.device_id = device_id
        self.signer = signer
        self.transport = transport
        self.credential_path = Path(credential_path) if credential_path else None
        self.credential: Optional[DeviceCredential] = None
        self.clock = clock

    def attest(self) -> DeviceCredential:
        status, challenge = self.transport.post("/device/challenge", {"device_id": self.device_id})
        if status != 200 or not challenge.get("nonce"):
            raise DeviceIdentityError(f"challenge refused: {status} {challenge.get('error', '')}".strip())
        nonce = b64url_decode(str(challenge["nonce"]))
        try:
            signature = self.signer.sign(nonce)
        except DeviceIdentityError:
            raise
        except Exception as e:
            raise DeviceIdentityError(f"device key unavailable: {type(e).__name__}: {e}") from e
        status, answer = self.transport.post("/device/attest", {
            "device_id": self.device_id,
            "nonce": challenge["nonce"],
            "signature": b64url(signature),
        })
        if status != 200 or not answer.get("credential"):
            raise DeviceIdentityError(f"attest refused: {status} {answer.get('error', '')}".strip())
        cred = DeviceCredential(token=str(answer["credential"]), exp=int(answer.get("exp") or 0),
                                device_id=str(answer.get("device_id") or self.device_id))
        self.credential = cred
        self._save(cred)
        log.info("device credential issued device_id=%s exp=%s", cred.device_id, cred.exp)
        return cred

    def current(self) -> Optional[DeviceCredential]:
        """A live credential, re-attesting when the held one is missing or near expiry.
        Returns None, never raises, when attestation fails: the caller decides
        whether to proceed without one (today: yes, and the server logs it)."""
        if self.credential is None:
            self.credential = self._load()
        if self.credential is not None and not self.credential.expired(self.clock()):
            return self.credential
        try:
            return self.attest()
        except DeviceIdentityError as e:
            log.warning("device attestation failed: %s", e)
            self.credential = None
            return None

    def grpc_metadata(self) -> list[tuple[str, str]]:
        """INTEGRATION POINT, not wired: the metadata pair the gRPC client would add
        to its Converse call so the orchestrator can bind the stream to this device.
        grpc_client.py does not call this yet; see security/DEVICE_IDENTITY.md."""
        cred = self.current()
        return [(CREDENTIAL_HEADER, cred.token)] if cred else []

    # -- persistence

    def _save(self, cred: DeviceCredential) -> None:
        if self.credential_path is None:
            return
        try:
            self.credential_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.credential_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"token": cred.token, "exp": cred.exp, "device_id": cred.device_id}),
                           encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.credential_path)
        except OSError as e:
            log.debug("device credential not persisted: %s", e)

    def _load(self) -> Optional[DeviceCredential]:
        if self.credential_path is None or not self.credential_path.exists():
            return None
        try:
            data = json.loads(self.credential_path.read_text(encoding="utf-8"))
            return DeviceCredential(token=str(data["token"]), exp=int(data["exp"]), device_id=str(data["device_id"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
