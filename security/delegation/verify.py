"""Upstream (tool server) check for delegated tokens.

A tool server accepts a token only if it is signed by the STS, addressed to this
tool, unexpired, and carries an act claim. The result names both the user whose
data is involved (sub) and every agent in the chain (act), so the tool and the
policy check in C can see who is acting for whom.

Works with tokens from sts.py (pass key=) or from Keycloak 26.8 (pass jwks_url=).

Revocation. Local JWT verification runs first and needs no network. After it, an
optional RevocationCheck is consulted with the token's lineage claims (jti,
parent_jti, grant_id). Two implementations ship:

  revocation.Denylist      the STS's own denylist, shared in process. The demo
                           and tests use this.
  CallbackRevocationCheck  wraps any callable, standing in for a deny list pushed
                           to each PEP (DIAGRAMS.md section 4): the verifier asks
                           a local structure, never a remote service, per call.

With no checker configured the verifier stays fully offline and does not know
about revocations; that is the pre-revocation behaviour, kept so a tool server
with only the public key still works. The alternative, RFC 7662 introspection
on every call, was not chosen: it adds a network hop to every tool call and
makes the STS a central availability dependency for all tools, which is the
same cost the PDP already imposes (threat 9) paid a second time. A pushed deny
list keeps the per-call check local and bounded (600 s of entries at most).

A checker that raises is treated as a refusal (fail closed), and every refusal
from the revocation check is logged with its reason in the same JSON form as
the STS audit log.

Sender constraint (RFC 9449, section 7.1 is the resource-server side). A token
that carries cnf.jkt is bound to a key, and verify() then needs the DPoP proof
that came with the request (the DPoP header), the method and the URI. The proof
must be signed by the key whose thumbprint is cnf.jkt, name this method and
URI, be no older than dpop_max_age_s, carry an ath that hashes this exact
token, and have a jti this verifier has not seen. A bound token with no proof,
a proof from another key, a stale or replayed proof, or a wrong ath is refused
with the reason logged, and the revocation check runs only after the proof is
good, so a thief with a copied token cannot even probe the deny list. The jkt
is part of the Principal and of every audit line. A token without cnf is a
bearer token and needs no proof unless require_dpop=True.

Order inside verify(): signature, issuer, audience, expiry; then DPoP; then
revocation; then the act chain.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import jwt

from dpop import DPoPError, ReplayCache, verify_proof

log = logging.getLogger("verify.audit")


class DelegationRejected(Exception):
    pass


class RevocationCheck(Protocol):
    def revoked(self, claims: dict) -> str | None:
        """Return the reason the token is revoked, or None if it is not."""
        ...


class CallbackRevocationCheck:
    """RevocationCheck over a callable that answers for a pushed deny list.

    The callable receives (jti, parent_jti, grant_id), any of which may be None,
    and returns True when any of them has been revoked. What populates the list
    on the PEP side is not built; this is the seam for it.
    """

    def __init__(self, is_revoked: Callable[[str | None, str | None, str | None], bool]):
        self.is_revoked = is_revoked

    def revoked(self, claims: dict) -> str | None:
        jti, parent, grant = claims.get("jti"), claims.get("parent_jti"), claims.get("grant_id")
        if self.is_revoked(jti, parent, grant):
            return f"token {(jti or '?')[:8]} is on the deny list"
        return None


@dataclass(frozen=True)
class Principal:
    user: str
    actors: tuple[str, ...]  # earliest first; the caller is last
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    jti: str | None
    grant_id: str | None = None
    jkt: str | None = None  # the key the token is bound to and the caller proved, or None for a bearer token

    @property
    def caller(self) -> str:
        return self.actors[-1]

    @property
    def path(self) -> str:
        return " > ".join((self.user, *self.actors))


def bound_jkt(claims: dict) -> str | None:
    cnf = claims.get("cnf")
    return cnf.get("jkt") if isinstance(cnf, dict) and cnf.get("jkt") else None


class UpstreamVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        key=None,
        jwks_url: str | None = None,
        max_depth: int = 2,
        leeway: int = 0,
        revocation: RevocationCheck | None = None,
        require_dpop: bool = False,
        dpop_max_age_s: int = 60,
        clock: Callable[[], float] = time.time,
    ):
        if (key is None) == (jwks_url is None):
            raise ValueError("pass exactly one of key or jwks_url")
        self.issuer = issuer
        self.audience = audience
        self.key = key
        self.jwks = jwt.PyJWKClient(jwks_url) if jwks_url else None
        self.max_depth = max_depth
        self.leeway = leeway
        self.revocation = revocation
        # DPoP. require_dpop=True refuses bearer (unbound) tokens outright; the
        # default accepts them so a verifier for Keycloak hop 1 tokens still works.
        self.require_dpop = require_dpop
        self.dpop_max_age_s = dpop_max_age_s
        self.clock = clock
        self.dpop_seen = ReplayCache(clock=lambda: self.clock())

    def _refuse(self, claims: dict, reason: str) -> None:
        log.info(json.dumps({
            "event": "tool_token_refused",
            "audience": self.audience,
            "reason": reason,
            "sub": claims.get("sub"),
            "jti": claims.get("jti"),
            "grant_id": claims.get("grant_id"),
            "jkt": bound_jkt(claims),
        }, sort_keys=True))
        raise DelegationRejected(reason)

    def _refuse_if_revoked(self, claims: dict) -> None:
        if self.revocation is None:
            return
        try:
            reason = self.revocation.revoked(claims)
        except Exception as e:  # the check itself failed: fail closed
            reason = f"revocation check failed: {type(e).__name__}"
        if reason:
            self._refuse(claims, reason)

    def _check_dpop(self, token: str, claims: dict, dpop_proof: str | None, htm: str | None, htu: str | None) -> str | None:
        """Returns the proved jkt for a bound token, None for a bearer token."""
        jkt = bound_jkt(claims)
        if jkt is None:
            if self.require_dpop:
                self._refuse(claims, "token is not sender-constrained (no cnf.jkt) and this tool requires DPoP")
            return None
        if not dpop_proof or not htm or not htu:
            self._refuse(claims, "token is key-bound (cnf.jkt) and no DPoP proof was presented")
        try:
            verify_proof(dpop_proof, htm, htu, expected_jkt=jkt, ath_of=token,
                         clock=self.clock, max_age_s=self.dpop_max_age_s, seen=self.dpop_seen)
        except DPoPError as e:
            self._refuse(claims, f"DPoP: {e}")
        return jkt

    def verify(self, token: str, dpop_proof: str | None = None, htm: str | None = None, htu: str | None = None) -> Principal:
        """Verify a tool token. For a key-bound token pass the request's DPoP
        header as dpop_proof together with its method (htm) and URI (htu)."""
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key if self.jwks else self.key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise DelegationRejected("token expired")
        except jwt.InvalidAudienceError:
            raise DelegationRejected(f"token is not addressed to {self.audience}")
        except jwt.InvalidTokenError as e:
            raise DelegationRejected(f"invalid token: {e}")

        # Signature, issuer, audience and expiry are settled locally, then the
        # sender constraint, and only then is the revocation state consulted, so
        # neither a forged token nor a copied one reaches it.
        jkt = self._check_dpop(token, claims, dpop_proof, htm, htu)
        self._refuse_if_revoked(claims)

        if "may_act" in claims:
            raise DelegationRejected("handoff token presented to a tool")
        act = claims.get("act")
        if not isinstance(act, dict):
            raise DelegationRejected("no act claim: refusing a token that does not say which agent is calling")

        actors: list[str] = []
        node = act
        while isinstance(node, dict):
            name = node.get("client_id") or node.get("sub")
            if not name:
                raise DelegationRejected("act entry names no actor")
            actors.append(name)
            node = node.get("act")
        if len(actors) > self.max_depth:
            raise DelegationRejected(f"delegation chain too deep ({len(actors)} > {self.max_depth})")
        actors.reverse()

        return Principal(
            user=claims["sub"],
            actors=tuple(actors),
            scopes=frozenset(claims.get("scope", "").split()),
            issued_at=int(claims["iat"]),
            expires_at=int(claims["exp"]),
            jti=claims.get("jti"),
            grant_id=claims.get("grant_id"),
            jkt=jkt,
        )
