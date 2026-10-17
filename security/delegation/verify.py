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
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Protocol

import jwt

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

    @property
    def caller(self) -> str:
        return self.actors[-1]

    @property
    def path(self) -> str:
        return " > ".join((self.user, *self.actors))


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

    def _refuse_if_revoked(self, claims: dict) -> None:
        if self.revocation is None:
            return
        try:
            reason = self.revocation.revoked(claims)
        except Exception as e:  # the check itself failed: fail closed
            reason = f"revocation check failed: {type(e).__name__}"
        if reason:
            log.info(json.dumps({
                "event": "tool_token_refused",
                "audience": self.audience,
                "reason": reason,
                "sub": claims.get("sub"),
                "jti": claims.get("jti"),
                "grant_id": claims.get("grant_id"),
            }, sort_keys=True))
            raise DelegationRejected(reason)

    def verify(self, token: str) -> Principal:
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

        # Signature, issuer, audience and expiry are settled locally. Only then is
        # the revocation state consulted, so a forged token never reaches it.
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
        )
