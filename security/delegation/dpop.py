"""DPoP proofs (RFC 9449) for the delegation prototype.

A DPoP proof is a short JWT the client signs with a key it holds, naming the
HTTP method and URI it is about to call, a fresh jti and the time. The public
key rides in the JWT header. The server checks the proof, then binds the token
it issues to that key by putting the key's RFC 7638 thumbprint in `cnf.jkt`.
From then on the token is usable only by whoever can sign a fresh proof with
the same key: a copied token on its own is useless.

Three pieces here:

  make_proof     the client side: typ dpop+jwt, alg ES256, jwk header, claims
                 jti, htm, htu, iat, optional ath (hash of the access token the
                 proof accompanies) and nonce.
  verify_proof   the server side, STS or tool: typ, alg allowlist (ES256 only),
                 public-only jwk, signature against that jwk, htm and htu exact
                 match (htu without query or fragment), iat inside max_age and
                 not in the future beyond a small skew, jti not seen before, ath
                 when a token is bound, thumbprint equal to the expected one.
  ReplayCache    seen jtis with expiries, bounded by max_age the way the
                 denylist is bounded by token lifetime, pruned on every check.

Keys are EC P-256 held in memory by each client object (sts.Client). The
thumbprint is the same RFC 7638 computation as `key_thumbprint` in
`server/modules/orchestrator/core/device_identity.py` (canonical JSON over
crv, kty, x, y, SHA-256, base64url), so a token bound to an agent's software
key and the speaker's device credential bound to its YubiKey key carry the same
`cnf.jkt` shape. It is repeated here rather than imported because security/
does not import from server/.

Not here: server-issued nonces (RFC 9449 section 8). The STS does not send
DPoP-Nonce. The replay cache plus a 60 s max_age bound the window a captured
proof is good for, and every party in this prototype is in one process with
one clock. Nonces become worth their round trip when clients are remote and
their clocks are not trusted.

Prototype. Nothing here is hardened beyond what the tests exercise.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

PROOF_TYP = "dpop+jwt"
PROOF_ALG = "ES256"
# Any of these in the header jwk means private material was sent. Refuse it.
PRIVATE_JWK_MEMBERS = {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}


class DPoPError(Exception):
    """A proof was missing, malformed, stale, replayed or from the wrong key."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_jwk(key) -> dict:
    """Public JWK (kty, crv, x, y only) for a P-256 key or key pair."""
    pub = key.public_key() if hasattr(key, "public_key") else key
    nums = pub.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
    }


def thumbprint(key_or_jwk) -> str:
    """RFC 7638 thumbprint (SHA-256, base64url) of a P-256 public key or its JWK.

    Same canonical form as server/.../device_identity.key_thumbprint: the
    required members only, lexicographic order, no whitespace.
    """
    jwk = key_or_jwk if isinstance(key_or_jwk, dict) else public_jwk(key_or_jwk)
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"), sort_keys=True,
    ).encode("ascii")
    return b64url(hashlib.sha256(canonical).digest())


def ath(access_token: str) -> str:
    """The `ath` claim: base64url SHA-256 of the access token's ASCII bytes."""
    return b64url(hashlib.sha256(access_token.encode("ascii")).digest())


def normalize_htu(url: str) -> str:
    """htu comparison form (RFC 9449 section 4.3): scheme and host case-folded,
    query and fragment dropped, empty path read as '/'."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))


def make_proof(
    key: ec.EllipticCurvePrivateKey,
    htm: str,
    htu: str,
    nonce: str | None = None,
    ath: str | None = None,
    now: int | None = None,
) -> str:
    """Sign a DPoP proof for one request. `ath` is the hash from ath() when the
    request also carries an access token; `now` lets a test choose iat."""
    claims: dict = {
        "jti": uuid.uuid4().hex,
        "htm": htm,
        "htu": htu,
        "iat": int(time.time()) if now is None else int(now),
    }
    if ath is not None:
        claims["ath"] = ath
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(claims, key, algorithm=PROOF_ALG, headers={"typ": PROOF_TYP, "jwk": public_jwk(key)})


class ReplayCache:
    """Seen proof jtis with the time after which they can no longer be replayed
    (iat + max_age), pruned on every check. Bounded by max_age, not by traffic,
    the same way revocation.Denylist is bounded by token lifetime."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._seen: dict[str, int] = {}

    def prune(self) -> None:
        now = int(self.clock())
        for jti in [j for j, exp in self._seen.items() if exp <= now]:
            del self._seen[jti]

    def check_and_add(self, jti: str, expires: int) -> bool:
        """True if jti was unseen (and is now recorded), False on a replay."""
        self.prune()
        if jti in self._seen:
            return False
        self._seen[jti] = expires
        return True

    def __len__(self) -> int:
        self.prune()
        return len(self._seen)

    def __contains__(self, jti: str) -> bool:
        self.prune()
        return jti in self._seen


@dataclass(frozen=True)
class Proof:
    jkt: str
    jti: str
    iat: int
    htm: str
    htu: str
    claims: dict


def verify_proof(
    proof: str | None,
    htm: str,
    htu: str,
    expected_jkt: str | None = None,
    ath_of: str | None = None,
    nonce: str | None = None,
    *,
    clock: Callable[[], float] = time.time,
    max_age_s: int = 60,
    skew_s: int = 5,
    seen: ReplayCache | None = None,
) -> Proof:
    """Check a DPoP proof against the request it arrived with.

    expected_jkt  the thumbprint the token is bound to (cnf.jkt), when there is one
    ath_of        the access token presented alongside; its hash must be the ath claim
    nonce         a server nonce to require, when the caller issued one (the STS does not)
    seen          the replay cache; a jti already in it is a refusal
    Raises DPoPError with the reason on any failure; returns the verified Proof.
    """
    if not proof:
        raise DPoPError("DPoP proof required")
    try:
        header = jwt.get_unverified_header(proof)
    except jwt.InvalidTokenError as e:
        raise DPoPError(f"proof is not a JWT: {e}")
    if header.get("typ") != PROOF_TYP:
        raise DPoPError(f"proof typ must be {PROOF_TYP}")
    if header.get("alg") != PROOF_ALG:
        raise DPoPError(f"proof alg must be {PROOF_ALG}")
    jwk = header.get("jwk")
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise DPoPError("proof header jwk must be an EC P-256 public key")
    if PRIVATE_JWK_MEMBERS & set(jwk):
        raise DPoPError("proof header jwk carries private key material")
    try:
        pub = jwt.algorithms.ECAlgorithm.from_jwk(jwk)
        claims = jwt.decode(
            proof,
            pub,
            algorithms=[PROOF_ALG],
            options={
                "require": ["jti", "htm", "htu", "iat"],
                # iat and exp are checked below against the injected clock, not wall time.
                "verify_iat": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except (jwt.InvalidTokenError, ValueError) as e:
        raise DPoPError(f"proof signature or claims invalid: {e}")

    if claims["htm"] != htm:
        raise DPoPError(f"proof htm {claims['htm']!r} does not match request method {htm!r}")
    if normalize_htu(str(claims["htu"])) != normalize_htu(htu):
        raise DPoPError("proof htu does not match the request URI")
    now = int(clock())
    try:
        iat = int(claims["iat"])
    except (TypeError, ValueError):
        raise DPoPError("proof iat is not a number")
    if iat > now + skew_s:
        raise DPoPError("proof iat is in the future")
    if now - iat > max_age_s:
        raise DPoPError(f"proof is older than {max_age_s} s")
    if nonce is not None and claims.get("nonce") != nonce:
        raise DPoPError("proof nonce does not match")
    if ath_of is not None:
        if "ath" not in claims:
            raise DPoPError("proof carries no ath for the presented token")
        if claims["ath"] != ath(ath_of):
            raise DPoPError("proof ath does not match the presented token")
    jkt = thumbprint(jwk)
    if expected_jkt is not None and jkt != expected_jkt:
        raise DPoPError("proof key does not match the key the token is bound to (cnf.jkt)")
    jti = str(claims["jti"])
    if seen is not None and not seen.check_and_add(jti, iat + max_age_s):
        raise DPoPError("proof jti already seen (replay)")
    return Proof(jkt=jkt, jti=jti, iat=iat, htm=htm, htu=claims["htu"], claims=claims)


# ---- key files for the one out-of-process path (python sts.py + admin.py) -----

def private_key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def load_private_key_pem(pem: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("DPoP key must be an EC P-256 private key")
    return key


if __name__ == "__main__":
    # python dpop.py keygen <path>: write a P-256 private key (PEM, mode 600) to
    # path and print its thumbprint, which is what the STS registers (STS_JKT_*).
    import os
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "keygen":
        k = generate_key()
        fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem(k))
        print(thumbprint(k))
        sys.exit(0)
    print("usage: dpop.py keygen <private-key.pem>", file=sys.stderr)
    sys.exit(2)
