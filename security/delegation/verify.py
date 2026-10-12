"""Upstream (tool server) check for delegated tokens.

A tool server accepts a token only if it is signed by the STS, addressed to this
tool, unexpired, and carries an act claim. The result names both the user whose
data is involved (sub) and every agent in the chain (act), so the tool and the
policy check in C can see who is acting for whom.

Works with tokens from sts.py (pass key=) or from Keycloak 26.8 (pass jwks_url=).
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt


class DelegationRejected(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user: str
    actors: tuple[str, ...]  # earliest first; the caller is last
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    jti: str | None

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
    ):
        if (key is None) == (jwks_url is None):
            raise ValueError("pass exactly one of key or jwks_url")
        self.issuer = issuer
        self.audience = audience
        self.key = key
        self.jwks = jwt.PyJWKClient(jwks_url) if jwks_url else None
        self.max_depth = max_depth
        self.leeway = leeway

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
        )
