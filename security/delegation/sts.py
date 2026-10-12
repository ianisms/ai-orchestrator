"""Minimal RFC 8693 security token service for agent delegation (prototype A).

Dev only. The signing key and client secrets are generated in memory at startup;
nothing secret is read from or written to the repo.

Token kinds this STS issues:
  user token     sub=user:<name>, aud=[agent], may_act names the agent the user consented to
  actor token    sub=client:<id>, aud=[issuer], token_use=actor (client_credentials)
  tool token     sub=user, act=chain, aud=[tool], scope narrowed, short-lived, no refresh
  handoff token  sub=user, act=chain, aud=[next agent], may_act names the next agent

The act claim nests per RFC 8693 section 4.1: the outermost act is the current
caller, and each nested act is the actor before it. For user > gateway > sub-agent:

  {"sub": "user:alice",
   "act": {"sub": "client:code-review-subagent", "client_id": "code-review-subagent",
           "act": {"sub": "client:mcp-gateway", "client_id": "mcp-gateway"}}}

Rules enforced on every exchange (each denial is logged with its reason):
  - delegation requires an actor_token; a plain exchange of a token that carries
    act or may_act is refused (it would strip the chain)
  - the actor_token must belong to the authenticated client
  - the subject_token's aud must name the actor, and its may_act must name the actor
  - access only narrows: scope must be held by the subject AND registered for the actor
  - audience must be registered for the actor; delegate_to must be on its allow list
  - chain depth is capped (default 2 actors)
  - exp = min(now + actor ttl, subject exp); no refresh tokens
"""
from __future__ import annotations

import json
import logging
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

log = logging.getLogger("sts.audit")


@dataclass
class Client:
    client_id: str
    audiences: set[str]
    scopes: set[str]
    may_delegate_to: set[str] = field(default_factory=set)
    ttl: int = 300
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)

    @property
    def sub(self) -> str:
        return f"client:{self.client_id}"


def default_clients() -> list[Client]:
    return [
        Client(
            "mcp-gateway",
            audiences={"tool-github", "tool-jira", "tool-fs"},
            scopes={"repo:read", "repo:write", "issues:read", "issues:write", "fs:read"},
            may_delegate_to={"code-review-subagent"},
            ttl=300,
        ),
        Client(
            "code-review-subagent",
            audiences={"tool-github"},
            scopes={"repo:read"},
            ttl=120,
        ),
        Client(
            "notes-agent",
            audiences={"tool-fs"},
            scopes={"fs:read"},
            ttl=120,
        ),
    ]


class ExchangeError(Exception):
    def __init__(self, error: str, reason: str, status: int = 400):
        super().__init__(reason)
        self.error = error
        self.reason = reason
        self.status = status


def act_chain(act: dict | None) -> list[str]:
    """Actors earliest first, current caller last."""
    out: list[str] = []
    while isinstance(act, dict):
        out.append(act.get("client_id") or act.get("sub", "?"))
        act = act.get("act")
    return list(reversed(out))


class STS:
    def __init__(
        self,
        issuer: str = "http://sts.local",
        clients: list[Client] | None = None,
        max_depth: int = 2,
        user_ttl: int = 600,
        actor_ttl: int = 60,
        clock: Callable[[], float] = time.time,
    ):
        self.issuer = issuer
        self.clients = {c.client_id: c for c in (clients or default_clients())}
        self.max_depth = max_depth
        self.user_ttl = user_ttl
        self.actor_ttl = actor_ttl
        self.clock = clock
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex[:16]
        self.audit_log: list[dict] = []

    # ---- keys -------------------------------------------------------------
    @property
    def public_key(self):
        return self._key.public_key()

    def jwks(self) -> dict:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.public_key, as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def _now(self) -> int:
        return int(self.clock())

    def _sign(self, claims: dict) -> str:
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})

    def _decode(self, name: str, token: str, audience: str | None) -> dict:
        try:
            return jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=audience,
                options={"require": ["exp", "iat", "sub", "iss"], "verify_aud": audience is not None},
            )
        except jwt.ExpiredSignatureError:
            raise ExchangeError("invalid_grant", f"{name} expired")
        except jwt.InvalidAudienceError:
            raise ExchangeError("invalid_grant", f"{name} audience does not name this client")
        except jwt.InvalidTokenError as e:
            raise ExchangeError("invalid_grant", f"{name} invalid: {e}")

    def _audit(self, decision: str, reason: str, **ctx) -> None:
        event = {"ts": self._now(), "event": "token_exchange", "decision": decision, "reason": reason, **ctx}
        self.audit_log.append(event)
        log.info(json.dumps(event, sort_keys=True))

    # ---- issuance ---------------------------------------------------------
    def issue_user_token(self, user: str, client_id: str, scopes: set[str], consent: bool = True) -> str:
        """Stands in for the AS login. consent=True means the user approved client_id acting for them."""
        client = self.clients[client_id]
        now = self._now()
        claims = {
            "iss": self.issuer,
            "sub": f"user:{user}",
            "aud": [client.client_id],
            "iat": now,
            "exp": now + self.user_ttl,
            "jti": uuid.uuid4().hex,
            "scope": " ".join(sorted(scopes)),
        }
        if consent:
            claims["may_act"] = {"sub": client.sub, "client_id": client.client_id, "iss": self.issuer}
        return self._sign(claims)

    def _authenticate(self, client_id: str, client_secret: str) -> Client:
        client = self.clients.get(client_id)
        if not client or not secrets.compare_digest(client.secret, client_secret or ""):
            raise ExchangeError("invalid_client", "client authentication failed", status=401)
        return client

    def client_credentials(self, client_id: str, client_secret: str) -> dict:
        client = self._authenticate(client_id, client_secret)
        now = self._now()
        token = self._sign({
            "iss": self.issuer,
            "sub": client.sub,
            "client_id": client.client_id,
            "aud": [self.issuer],
            "iat": now,
            "exp": now + self.actor_ttl,
            "jti": uuid.uuid4().hex,
            "token_use": "actor",
        })
        return {"access_token": token, "token_type": "Bearer", "expires_in": self.actor_ttl}

    def exchange(
        self,
        *,
        client_id: str,
        client_secret: str,
        subject_token: str,
        actor_token: str | None = None,
        audience: str | None = None,
        scope: str | None = None,
        delegate_to: str | None = None,
    ) -> dict:
        ctx: dict = {"client_id": client_id, "audience": audience, "delegate_to": delegate_to, "requested_scope": scope}
        try:
            actor = self._authenticate(client_id, client_secret)

            if not actor_token:
                subj = self._decode("subject_token", subject_token, audience=None)
                if "act" in subj or "may_act" in subj:
                    raise ExchangeError("invalid_request", "plain exchange refused: subject carries delegation claims and would lose its act chain")
                raise ExchangeError("invalid_request", "delegation requires an actor_token")

            act_claims = self._decode("actor_token", actor_token, audience=self.issuer)
            if act_claims.get("token_use") != "actor" or act_claims.get("client_id") != actor.client_id:
                raise ExchangeError("invalid_grant", "actor_token does not belong to the authenticated client")

            subj = self._decode("subject_token", subject_token, audience=actor.client_id)
            ctx["sub"] = subj["sub"]
            prior = subj.get("act")
            ctx["chain_in"] = act_chain(prior)

            may_act = subj.get("may_act")
            if not isinstance(may_act, dict):
                raise ExchangeError("invalid_grant", "subject_token has no may_act: no consent for any actor")
            if (
                may_act.get("sub") != act_claims["sub"]
                or may_act.get("client_id") != actor.client_id
                or may_act.get("iss") != self.issuer
            ):
                raise ExchangeError("invalid_grant", "may_act does not name this actor")
            if subj["sub"] == act_claims["sub"]:
                raise ExchangeError("invalid_grant", "actor cannot act for itself")

            if (audience is None) == (delegate_to is None):
                raise ExchangeError("invalid_request", "send exactly one of audience or delegate_to")

            held = set(subj.get("scope", "").split())
            nxt = None
            if delegate_to is not None:
                nxt = self.clients.get(delegate_to)
                if nxt is None or delegate_to not in actor.may_delegate_to:
                    raise ExchangeError("invalid_target", f"{actor.client_id} may not delegate to {delegate_to}")
            elif audience not in actor.audiences:
                raise ExchangeError("invalid_target", f"audience {audience} is not registered for {actor.client_id}")

            # A handoff commits to one more actor, so count it now rather than at the next hop.
            depth = len(act_chain(prior)) + 1 + (1 if nxt else 0)
            if depth > self.max_depth:
                raise ExchangeError("invalid_grant", f"delegation chain too deep ({depth} > {self.max_depth})")

            if scope:
                requested = set(scope.split())
            else:
                requested = held & actor.scopes & (nxt.scopes if nxt else actor.scopes)
            if extra := requested - held:
                raise ExchangeError("invalid_scope", f"scope exceeds what the subject holds: {' '.join(sorted(extra))}")
            if extra := requested - actor.scopes:
                raise ExchangeError("invalid_scope", f"scope exceeds what {actor.client_id} is registered for: {' '.join(sorted(extra))}")
            if nxt and (extra := requested - nxt.scopes):
                raise ExchangeError("invalid_scope", f"scope exceeds what {nxt.client_id} is registered for: {' '.join(sorted(extra))}")
            if not requested:
                raise ExchangeError("invalid_scope", "no scope left after narrowing")

            now = self._now()
            exp = min(now + actor.ttl, int(subj["exp"]))
            act = {"sub": act_claims["sub"], "client_id": actor.client_id}
            if prior:
                act["act"] = prior
            claims = {
                "iss": self.issuer,
                "sub": subj["sub"],
                "aud": [nxt.client_id if nxt else audience],
                "iat": now,
                "exp": exp,
                "jti": uuid.uuid4().hex,
                "client_id": actor.client_id,
                "scope": " ".join(sorted(requested)),
                "act": act,
            }
            if nxt:
                claims["may_act"] = {"sub": nxt.sub, "client_id": nxt.client_id, "iss": self.issuer}
            token = self._sign(claims)
            self._audit("allow", "issued", **ctx, chain_out=act_chain(act), scope=claims["scope"], exp=exp)
            return {
                "access_token": token,
                "issued_token_type": ACCESS_TOKEN_TYPE,
                "token_type": "N_A" if nxt else "Bearer",
                "expires_in": exp - now,
                "scope": claims["scope"],
            }
        except ExchangeError as e:
            self._audit("deny", e.reason, error=e.error, **ctx)
            raise


def create_app(sts: STS) -> FastAPI:
    app = FastAPI(title="delegation-sts (dev)")

    def err(e: ExchangeError) -> JSONResponse:
        return JSONResponse({"error": e.error, "error_description": e.reason}, status_code=e.status)

    @app.get("/.well-known/jwks.json")
    def jwks():
        return sts.jwks()

    @app.post("/dev/login")
    def dev_login(user: str = Form(...), client_id: str = Form(...), scope: str = Form(...), consent: bool = Form(True)):
        """Dev stand-in for the AS login and consent screen."""
        if client_id not in sts.clients:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        return {"access_token": sts.issue_user_token(user, client_id, set(scope.split()), consent)}

    @app.post("/token")
    def token(
        grant_type: str = Form(...),
        client_id: str = Form(...),
        client_secret: str = Form(...),
        subject_token: str | None = Form(None),
        subject_token_type: str | None = Form(None),
        actor_token: str | None = Form(None),
        actor_token_type: str | None = Form(None),
        audience: str | None = Form(None),
        scope: str | None = Form(None),
        delegate_to: str | None = Form(None),  # non-standard: names the next actor for a handoff token
    ):
        try:
            if grant_type == "client_credentials":
                return sts.client_credentials(client_id, client_secret)
            if grant_type != TOKEN_EXCHANGE:
                raise ExchangeError("unsupported_grant_type", grant_type)
            for name, typ in (("subject_token_type", subject_token_type), ("actor_token_type", actor_token_type if actor_token else ACCESS_TOKEN_TYPE)):
                if typ != ACCESS_TOKEN_TYPE:
                    raise ExchangeError("invalid_request", f"{name} must be {ACCESS_TOKEN_TYPE}")
            if not subject_token:
                raise ExchangeError("invalid_request", "subject_token required")
            return sts.exchange(
                client_id=client_id,
                client_secret=client_secret,
                subject_token=subject_token,
                actor_token=actor_token,
                audience=audience,
                scope=scope,
                delegate_to=delegate_to,
            )
        except ExchangeError as e:
            return err(e)

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sts = STS(issuer="http://127.0.0.1:8099")
    print("dev client secrets (in memory, regenerated each start):", file=sys.stderr)
    for c in sts.clients.values():
        print(f"  {c.client_id}: {c.secret}", file=sys.stderr)
    uvicorn.run(create_app(sts), host="127.0.0.1", port=8099)
