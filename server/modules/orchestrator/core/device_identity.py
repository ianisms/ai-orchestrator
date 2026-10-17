"""Device identity: enrollment registry, boot-time challenge, short-lived device credential.

The speaker proves what it is with a key it cannot export (a YubiKey 5 PIV key
in slot 9a today; a secure element on the carrier board later). This module is
the orchestrator side of that: it keeps the registry of enrolled device keys,
issues a nonce, checks the device's signature over it, and mints a short-lived
device credential whose `cnf` claim names the key by thumbprint.

Flow (speaker/modules/speaker/device_identity.py is the other half):

  speaker  > POST /device/challenge {device_id}            > orchestrator
  speaker  < {nonce, exp}                                  < orchestrator
  speaker    signs challenge_message(nonce) with the device key
  speaker  > POST /device/attest {device_id, nonce, signature} > orchestrator
  speaker  < {credential, exp}   or 403 {error}            < orchestrator

Fail closed. Unknown device, revoked device, unknown nonce, expired nonce,
replayed nonce, nonce issued to another device, bad signature and a missing or
unparseable key all deny, and every denial is logged with its reason in the
same JSON form as security/delegation/verify.py.

What this is not: attestation. Nothing here measures the software on the
device. A stolen YubiKey plugged into another host would pass this check, which
is why the credential is meant for low-risk scopes only and why enrollment is
revocable (security/DEVICE_IDENTITY.md).

Crypto: ECDSA P-256 with SHA-256 and DER signatures, which is what the YubiKey
returns for an ECCP256 key. `cryptography` is imported lazily so this module
loads without it; signature checks and thumbprints need it.

The device credential is signed with an HMAC key the orchestrator holds. Only
the orchestrator issues and verifies it, so there is no third party that needs
a public key. If no secret is configured one is generated per process, which
means a restart invalidates outstanding credentials; the speaker re-attests.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("device_identity.audit")

CHALLENGE_PREFIX = b"ai-orchestrator device challenge v1\n"
CREDENTIAL_TYP = "device+jwt"
CREDENTIAL_ISSUER = "ai-orchestrator"
CREDENTIAL_HEADER = "x-device-credential"

DEFAULT_CHALLENGE_TTL_S = 30
DEFAULT_CREDENTIAL_TTL_S = 600  # 10 minutes; keep between 300 and 900
MAX_PENDING_CHALLENGES = 1024


class DeviceIdentityError(Exception):
    """A denied challenge or credential, with the reason that was logged."""


# ---- encoding helpers -------------------------------------------------------

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def challenge_message(nonce: bytes) -> bytes:
    """The bytes the device signs. Domain-separated so a device signature can
    never double as a signature over something else."""
    return CHALLENGE_PREFIX + nonce


def key_thumbprint(public_key_pem: str) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url) of a P-256 public key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise DeviceIdentityError("device key is not a P-256 EC key")
    nums = key.public_numbers()
    jwk = {
        "crv": "P-256",
        "kty": "EC",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
    }
    canonical = json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode("ascii")
    return b64url(hashlib.sha256(canonical).digest())


def verify_signature(public_key_pem: str, message: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        if not isinstance(key, ec.EllipticCurvePublicKey):
            return False
        key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---- registry ---------------------------------------------------------------

@dataclass
class DeviceEntry:
    device_id: str
    room: str
    household: str
    public_key_pem: str
    thumbprint: str
    enrolled_at: float
    revoked_at: Optional[float] = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def to_json(self) -> dict:
        return {
            "device_id": self.device_id,
            "room": self.room,
            "household": self.household,
            "public_key_pem": self.public_key_pem,
            "thumbprint": self.thumbprint,
            "enrolled_at": self.enrolled_at,
            "revoked_at": self.revoked_at,
        }

    @staticmethod
    def from_json(data: dict) -> "DeviceEntry":
        return DeviceEntry(
            device_id=str(data["device_id"]),
            room=str(data.get("room") or ""),
            household=str(data.get("household") or ""),
            public_key_pem=str(data["public_key_pem"]),
            thumbprint=str(data.get("thumbprint") or ""),
            enrolled_at=float(data.get("enrolled_at") or 0.0),
            revoked_at=None if data.get("revoked_at") is None else float(data["revoked_at"]),
        )


class EnrollmentRegistry:
    """Enrolled device keys in one JSON file: {"devices": {device_id: entry}}.

    Same shape and the same atomic write as the voice enrollment state
    (server/scripts/approve_enrollment.py). The file is re-read on every lookup
    so an admin edit or a revocation from the script takes effect without a
    restart; the file is small and lookups happen once per boot per device.
    """

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        devices = data.get("devices") if isinstance(data, dict) else None
        return devices if isinstance(devices, dict) else {}

    def _save(self, devices: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"devices": devices}, ensure_ascii=True, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(self.path)

    def get(self, device_id: str) -> Optional[DeviceEntry]:
        raw = self._load().get(str(device_id))
        if not isinstance(raw, dict):
            return None
        try:
            return DeviceEntry.from_json(raw)
        except (KeyError, TypeError, ValueError):
            return None

    def list(self) -> list[DeviceEntry]:
        out = []
        for raw in self._load().values():
            try:
                out.append(DeviceEntry.from_json(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda e: e.device_id)

    def enroll(self, device_id: str, room: str, household: str, public_key_pem: str,
               replace: bool = False) -> DeviceEntry:
        """Register a device key. Refuses to overwrite an existing entry unless
        replace=True, so a second enrollment cannot silently swap a key."""
        device_id = str(device_id).strip()
        if not device_id or not device_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise DeviceIdentityError("device_id must be a plain token")
        thumbprint = key_thumbprint(public_key_pem)  # also validates the key
        devices = self._load()
        if device_id in devices and not replace:
            raise DeviceIdentityError(f"device {device_id} is already enrolled; pass replace to rotate its key")
        entry = DeviceEntry(
            device_id=device_id,
            room=str(room or ""),
            household=str(household or ""),
            public_key_pem=public_key_pem.strip() + "\n",
            thumbprint=thumbprint,
            enrolled_at=float(self.clock()),
        )
        devices[device_id] = entry.to_json()
        self._save(devices)
        return entry

    def revoke(self, device_id: str) -> bool:
        devices = self._load()
        raw = devices.get(str(device_id))
        if not isinstance(raw, dict):
            return False
        if raw.get("revoked_at") is None:
            raw["revoked_at"] = float(self.clock())
            self._save(devices)
        return True


# ---- challenge and credential -----------------------------------------------

@dataclass(frozen=True)
class DeviceCredential:
    token: str
    claims: dict

    @property
    def device_id(self) -> str:
        return str(self.claims.get("sub", ""))

    @property
    def expires_at(self) -> int:
        return int(self.claims.get("exp", 0))

    @property
    def thumbprint(self) -> str:
        cnf = self.claims.get("cnf")
        return str(cnf.get("jkt", "")) if isinstance(cnf, dict) else ""


@dataclass
class _Pending:
    device_id: str
    exp: float


@dataclass
class DeviceIdentityService:
    """Issues challenges, verifies responses, mints and verifies device credentials."""

    registry: EnrollmentRegistry
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    challenge_ttl_s: int = DEFAULT_CHALLENGE_TTL_S
    credential_ttl_s: int = DEFAULT_CREDENTIAL_TTL_S
    clock: Callable[[], float] = time.time
    issuer: str = CREDENTIAL_ISSUER

    def __post_init__(self) -> None:
        if not 300 <= int(self.credential_ttl_s) <= 900:
            raise ValueError("credential_ttl_s must be between 300 and 900 seconds")
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    # -- audit

    def _refuse(self, device_id: str, reason: str, **extra) -> None:
        log.info(json.dumps({"event": "device_attest_refused", "device_id": device_id,
                             "reason": reason, **extra}, sort_keys=True))
        raise DeviceIdentityError(reason)

    # -- challenge

    def _prune(self, now: float) -> None:
        dead = [n for n, p in self._pending.items() if p.exp <= now]
        for n in dead:
            del self._pending[n]
        while len(self._pending) >= MAX_PENDING_CHALLENGES:
            # make room for the incoming nonce, oldest first; a flood of challenge
            # requests cannot grow this without bound
            oldest = min(self._pending, key=lambda n: self._pending[n].exp)
            del self._pending[oldest]

    def issue_challenge(self, device_id: str) -> dict:
        """A fresh nonce bound to this device_id. Issued whether or not the
        device is enrolled, so the challenge endpoint does not enumerate the
        registry; an unknown device is refused at attest."""
        device_id = str(device_id or "").strip()
        if not device_id:
            self._refuse(device_id, "missing device_id")
        now = self.clock()
        nonce = b64url(secrets.token_bytes(32))
        with self._lock:
            self._prune(now)
            self._pending[nonce] = _Pending(device_id=device_id, exp=now + self.challenge_ttl_s)
        return {"nonce": nonce, "exp": int(now + self.challenge_ttl_s)}

    def attest(self, device_id: str, nonce: str, signature_b64: str) -> DeviceCredential:
        """Verify the device's signature over the challenge and mint a credential.

        The nonce is consumed on the first attempt, success or failure, so a
        captured response cannot be replayed and a guess cannot be retried."""
        device_id = str(device_id or "").strip()
        now = self.clock()
        with self._lock:
            pending = self._pending.pop(str(nonce or ""), None)
        if pending is None:
            self._refuse(device_id, "unknown or already used nonce")
        if pending.exp <= now:
            self._refuse(device_id, "challenge expired")
        if pending.device_id != device_id:
            self._refuse(device_id, "nonce was issued to another device")

        entry = self.registry.get(device_id)
        if entry is None:
            self._refuse(device_id, "device is not enrolled")
        if entry.revoked:
            self._refuse(device_id, "device enrollment is revoked")

        try:
            signature = b64url_decode(str(signature_b64 or ""))
            nonce_bytes = b64url_decode(str(nonce))
        except (ValueError, TypeError):
            self._refuse(device_id, "signature or nonce is not base64url")
        try:
            ok = verify_signature(entry.public_key_pem, challenge_message(nonce_bytes), signature)
        except ImportError:
            self._refuse(device_id, "signature check unavailable: cryptography is not installed")
        if not ok:
            self._refuse(device_id, "signature does not verify against the enrolled key")

        cred = self._mint(entry, now)
        log.info(json.dumps({"event": "device_attested", "device_id": device_id, "room": entry.room,
                             "household": entry.household, "jkt": entry.thumbprint,
                             "exp": cred.expires_at}, sort_keys=True))
        return cred

    # -- credential

    def _sign(self, signing_input: bytes) -> str:
        return b64url(hmac.new(self.secret, signing_input, hashlib.sha256).digest())

    def _mint(self, entry: DeviceEntry, now: float) -> DeviceCredential:
        iat = int(now)
        claims = {
            "iss": self.issuer,
            "sub": entry.device_id,
            "room": entry.room,
            "household": entry.household,
            "iat": iat,
            "exp": iat + int(self.credential_ttl_s),
            "jti": b64url(secrets.token_bytes(16)),
            "cnf": {"jkt": entry.thumbprint},
        }
        header = {"alg": "HS256", "typ": CREDENTIAL_TYP}
        signing_input = (b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode()) + "." +
                         b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())).encode("ascii")
        return DeviceCredential(token=signing_input.decode("ascii") + "." + self._sign(signing_input), claims=claims)

    def verify_credential(self, token: str) -> DeviceCredential:
        """Check a credential this orchestrator issued. Fail closed on anything odd."""
        parts = str(token or "").split(".")
        if len(parts) != 3 or not all(parts):
            raise DeviceIdentityError("credential is not a three-part token")
        signing_input = (parts[0] + "." + parts[1]).encode("ascii")
        if not hmac.compare_digest(self._sign(signing_input), parts[2]):
            raise DeviceIdentityError("credential signature does not verify")
        try:
            header = json.loads(b64url_decode(parts[0]))
            claims = json.loads(b64url_decode(parts[1]))
        except (ValueError, TypeError):
            raise DeviceIdentityError("credential is not decodable")
        if not isinstance(header, dict) or header.get("typ") != CREDENTIAL_TYP or header.get("alg") != "HS256":
            raise DeviceIdentityError("credential header is not a device credential")
        if not isinstance(claims, dict) or claims.get("iss") != self.issuer:
            raise DeviceIdentityError("credential issuer mismatch")
        try:
            exp = int(claims["exp"])
        except (KeyError, TypeError, ValueError):
            raise DeviceIdentityError("credential has no usable exp")
        if exp <= self.clock():
            raise DeviceIdentityError("credential expired")
        cnf = claims.get("cnf")
        if not isinstance(cnf, dict) or not cnf.get("jkt"):
            raise DeviceIdentityError("credential carries no cnf.jkt")
        entry = self.registry.get(str(claims.get("sub", "")))
        if entry is None or entry.revoked:
            raise DeviceIdentityError("device enrollment missing or revoked")
        if entry.thumbprint != cnf.get("jkt"):
            raise DeviceIdentityError("credential cnf.jkt does not match the enrolled key")
        return DeviceCredential(token=token, claims=claims)

    def device_from_metadata(self, md: dict) -> Optional[DeviceCredential]:
        """gRPC metadata hook: the verified credential if the client presented a
        valid one, else None. A present but invalid credential is logged and
        treated as absent. Enforcement (refusing the stream, or feeding the voice
        gate) is not wired yet; see security/DEVICE_IDENTITY.md."""
        token = (md or {}).get(CREDENTIAL_HEADER, "")
        if not token:
            return None
        try:
            return self.verify_credential(token)
        except DeviceIdentityError as e:
            log.info(json.dumps({"event": "device_credential_refused", "reason": str(e)}, sort_keys=True))
            return None


# ---- HTTP glue --------------------------------------------------------------

class DeviceIdentityHttp:
    """Request handling for the two POST routes on the orchestrator status server.

    Transport-free so it can be tested without a socket: the server's handler
    passes (path, body) and writes back (status, payload). Challenge and attest
    answer the device; nothing here enrolls or revokes, that is an admin action
    (server/scripts/enroll_device.py).
    """

    ROUTES = ("/device/challenge", "/device/attest")

    def __init__(self, service: DeviceIdentityService, max_body: int = 8192):
        self.service = service
        self.max_body = max_body

    def post(self, path: str, body: bytes) -> tuple[int, dict]:
        if path not in self.ROUTES:
            return 404, {"error": "not_found"}
        if len(body or b"") > self.max_body:
            return 413, {"error": "body too large"}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": "body is not JSON"}
        if not isinstance(data, dict):
            return 400, {"error": "body is not an object"}
        try:
            if path == "/device/challenge":
                return 200, self.service.issue_challenge(str(data.get("device_id", "")))
            cred = self.service.attest(str(data.get("device_id", "")), str(data.get("nonce", "")),
                                       str(data.get("signature", "")))
            return 200, {"credential": cred.token, "exp": cred.expires_at, "device_id": cred.device_id}
        except DeviceIdentityError as e:
            return 403, {"error": str(e)}
