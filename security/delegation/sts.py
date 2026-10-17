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
  - the subject token, its grant, and every token in its lineage must be unrevoked

Lineage claims on every issued token, so a revocation can follow the chain:
  jti         unique per token (already present)
  grant_id    minted on the user token at login/consent and copied unchanged
              through every exchange; names the user's consent to this agent
  parent_jti  the subject token's jti, on exchanged tokens only

Revocation (RFC 7009 on /revoke, plus a documented extension): see STS.revoke,
STS.revoke_grant and STS.revoke_client. State lives in revocation.Denylist, which
a verifier can share in process. Nothing here is production-grade; it is the
smallest thing that closes threat 11 in the prototype.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, Response

from revocation import Denylist, Issued

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
        # Revocation state. The denylist reads self.clock through a lambda so a test
        # that swaps sts.clock moves the denylist's clock too.
        self.denylist = Denylist(clock=lambda: self.clock())
        self._issued: dict[str, Issued] = {}  # jti -> record; pruned alongside the denylist
        self.disabled_clients: set[str] = set()

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

    def _audit(self, decision: str, reason: str, event: str = "token_exchange", **ctx) -> None:
        record = {"ts": self._now(), "event": event, "decision": decision, "reason": reason, **ctx}
        self.audit_log.append(record)
        log.info(json.dumps(record, sort_keys=True))

    # ---- issuance ---------------------------------------------------------
    def _record(self, claims: dict, kind: str, client_id: str | None) -> None:
        """Remember an issued token so revocation can walk its family later."""
        self._prune_issued()
        rec = Issued(
            jti=claims["jti"],
            kind=kind,
            sub=claims["sub"],
            exp=int(claims["exp"]),
            grant_id=claims.get("grant_id"),
            parent_jti=claims.get("parent_jti"),
            client_id=client_id,
            aud=tuple(claims.get("aud", ())),
            actors=tuple(act_chain(claims.get("act"))),
        )
        self._issued[rec.jti] = rec
        if rec.parent_jti and (parent := self._issued.get(rec.parent_jti)):
            parent.children.append(rec.jti)

    def _prune_issued(self) -> None:
        # Bounded by the 600 s user lifetime: no descendant outlives its user token.
        now = self._now()
        for jti in [j for j, r in self._issued.items() if r.exp <= now]:
            del self._issued[jti]

    def issue_user_token(self, user: str, client_id: str, scopes: set[str], consent: bool = True) -> str:
        """Stands in for the AS login. consent=True means the user approved client_id acting for them.

        Mints grant_id: the handle for this consent, carried unchanged by every
        token exchanged from this one, so revoking the consent kills them all.
        """
        client = self.clients[client_id]
        now = self._now()
        claims = {
            "iss": self.issuer,
            "sub": f"user:{user}",
            "aud": [client.client_id],
            "iat": now,
            "exp": now + self.user_ttl,
            "jti": uuid.uuid4().hex,
            "grant_id": uuid.uuid4().hex,
            "scope": " ".join(sorted(scopes)),
        }
        if consent:
            claims["may_act"] = {"sub": client.sub, "client_id": client.client_id, "iss": self.issuer}
        self._record(claims, "user", client_id=None)
        return self._sign(claims)

    def _authenticate(self, client_id: str, client_secret: str) -> Client:
        client = self.clients.get(client_id)
        if not client or not secrets.compare_digest(client.secret, client_secret or ""):
            raise ExchangeError("invalid_client", "client authentication failed", status=401)
        if client_id in self.disabled_clients:
            raise ExchangeError("invalid_client", f"client {client_id} is revoked", status=401)
        return client

    def client_credentials(self, client_id: str, client_secret: str) -> dict:
        client = self._authenticate(client_id, client_secret)
        now = self._now()
        claims = {
            "iss": self.issuer,
            "sub": client.sub,
            "client_id": client.client_id,
            "aud": [self.issuer],
            "iat": now,
            "exp": now + self.actor_ttl,
            "jti": uuid.uuid4().hex,
            "token_use": "actor",
        }
        self._record(claims, "actor", client_id=client.client_id)
        return {"access_token": self._sign(claims), "token_type": "Bearer", "expires_in": self.actor_ttl}

    def _refuse_if_revoked(self, name: str, claims: dict) -> None:
        """Deny when the token, its grant, or any ancestor by lineage is revoked."""
        if reason := self.denylist.revoked(claims):
            raise ExchangeError("invalid_grant", f"{name} revoked: {reason}")
        # The denylist already holds every descendant of a revoked token, so this
        # walk only matters if an ancestor expired out of the registry before the
        # revocation landed. Cheap, and it keeps the rule honest.
        seen: set[str] = set()
        parent = claims.get("parent_jti")
        while parent and parent not in seen:
            seen.add(parent)
            if parent in self.denylist:
                raise ExchangeError("invalid_grant", f"{name} revoked: ancestor {parent[:8]} revoked")
            rec = self._issued.get(parent)
            parent = rec.parent_jti if rec else None

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
            self._refuse_if_revoked("actor_token", act_claims)

            subj = self._decode("subject_token", subject_token, audience=actor.client_id)
            ctx["sub"] = subj["sub"]
            ctx["grant_id"] = subj.get("grant_id")
            self._refuse_if_revoked("subject_token", subj)
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
            # Lineage: the grant travels unchanged; the parent is the subject just consumed.
            if subj.get("grant_id"):
                claims["grant_id"] = subj["grant_id"]
            if subj.get("jti"):
                claims["parent_jti"] = subj["jti"]
            if nxt:
                claims["may_act"] = {"sub": nxt.sub, "client_id": nxt.client_id, "iss": self.issuer}
            self._record(claims, "handoff" if nxt else "tool", client_id=actor.client_id)
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

    # ---- revocation -------------------------------------------------------
    def _family_exp(self, jti: str) -> int:
        """Latest exp in the subtree rooted at jti, so a denylist entry outlives every token it covers."""
        latest = 0
        stack = [jti]
        while stack:
            rec = self._issued.get(stack.pop())
            if rec:
                latest = max(latest, rec.exp)
                stack.extend(rec.children)
        return latest

    def _revoke_subtree(self, jti: str, exp: int) -> set[str]:
        """Deny jti and every descendant by lineage. Returns the jtis denied."""
        exp = max(exp, self._family_exp(jti))
        stack = [jti]
        seen: set[str] = set()
        while stack:
            j = stack.pop()
            if j in seen:
                continue
            seen.add(j)
            self.denylist.add_jti(j, exp)
            if rec := self._issued.get(j):
                stack.extend(rec.children)
        return seen

    @staticmethod
    def _may_revoke(client: Client, claims: dict) -> bool:
        """Who may revoke what. A client may revoke a token when it is the token's
        holder (its client_id is in aud, or equals the token's client_id claim) or
        it appears anywhere in the token's act chain. Ancestors can cut off what they
        delegated; a downstream agent cannot kill the token above it, because a less
        trusted actor should not be able to end the user's whole session."""
        aud = claims.get("aud", [])
        if isinstance(aud, str):
            aud = [aud]
        return (
            client.client_id in aud
            or claims.get("client_id") == client.client_id
            or client.client_id in act_chain(claims.get("act"))
        )

    def revoke(self, token: str, token_type_hint: str | None = None, *, client_id: str, client_secret: str) -> None:
        """RFC 7009 revocation, with one extension.

        Per RFC 7009: the client authenticates; an unknown, malformed, forged or
        already-expired token returns success and changes nothing (no probing
        through the response); the client may revoke only a token it holds or a
        token in a chain it participates in (see _may_revoke), and a genuine token
        outside that set is refused with unauthorized_client, as section 2.1 of the
        RFC says to inform the client. token_type_hint is accepted and ignored: this
        STS issues one token type and no refresh tokens.

        Extension beyond RFC 7009 (the RFC is silent on exchanged tokens): revoking
        a token revokes every token exchanged from it, by lineage, and revoking a
        user token revokes its grant_id, so every token in the family is dead even
        if the registry no longer has it. Every outcome is audited with its reason.
        """
        ctx: dict = {"client_id": client_id, "token_type_hint": token_type_hint}
        try:
            client = self._authenticate(client_id, client_secret)
            try:
                claims = jwt.decode(
                    token,
                    self.public_key,
                    algorithms=["RS256"],
                    issuer=self.issuer,
                    options={"verify_aud": False, "verify_exp": False, "require": ["exp", "sub", "iss"]},
                )
            except jwt.InvalidTokenError as e:
                # Includes tokens signed by someone else (for example Keycloak for hop 1):
                # this STS cannot revoke what it did not issue. Success, no change.
                self._audit("noop", f"token unknown or malformed: {e}", event="token_revocation", **ctx)
                return
            jti = claims.get("jti")
            ctx.update(jti=jti, grant_id=claims.get("grant_id"), sub=claims.get("sub"))
            if int(claims["exp"]) <= self._now():
                self._audit("noop", "token already expired", event="token_revocation", **ctx)
                return
            if not jti:
                self._audit("noop", "token has no jti", event="token_revocation", **ctx)
                return
            if not self._may_revoke(client, claims):
                raise ExchangeError("unauthorized_client", f"{client_id} is not a holder of or an actor in this token's chain")
            is_user_token = "act" not in claims and not claims.get("parent_jti") and claims.get("grant_id")
            count = len(self._revoke_subtree(jti, int(claims["exp"])))
            if is_user_token:
                self.denylist.add_grant(claims["grant_id"], max(int(claims["exp"]), self._family_exp(jti)))
            self._audit("revoked", "grant revoked" if is_user_token else "token and descendants revoked",
                        event="token_revocation", revoked_jtis=count, **ctx)
        except ExchangeError as e:
            self._audit("deny", e.reason, event="token_revocation", error=e.error, **ctx)
            raise

    def revoke_grant(self, grant_id: str) -> int:
        """Admin: revoke a consent by its handle, not by a token string. Outside RFC 7009.

        Kills every live token carrying this grant_id (the user token and all
        tokens exchanged from it). Returns how many issued tokens were denied.
        There is no admin authentication in the prototype, so this is a method,
        not an HTTP route.
        """
        matching = [r for r in self._issued.values() if r.grant_id == grant_id]
        exp = max((r.exp for r in matching), default=self._now() + self.user_ttl)
        self.denylist.add_grant(grant_id, exp)
        for r in matching:
            self.denylist.add_jti(r.jti, exp)
        self._audit("revoked", "grant revoked by admin", event="grant_revocation", grant_id=grant_id, revoked_jtis=len(matching))
        return len(matching)

    def revoke_client(self, client_id: str) -> int:
        """Admin: retire an agent by its client_id handle. Outside RFC 7009.

        Kills every live token where the client is the actor (its actor tokens and
        the tokens it requested) or appears anywhere in act, and refuses the client
        new actor tokens and exchanges until enable_client. Returns the number of
        issued tokens denied. Method only: no admin authentication exists here.
        """
        self.disabled_clients.add(client_id)
        denied: set[str] = set()
        for r in list(self._issued.values()):
            if r.client_id == client_id or client_id in r.actors:
                denied |= self._revoke_subtree(r.jti, r.exp)
        self._audit("revoked", "client revoked by admin", event="client_revocation", client_id=client_id, revoked_jtis=len(denied))
        return len(denied)

    def enable_client(self, client_id: str) -> None:
        """Admin: lift revoke_client. Tokens denied while it was revoked stay denied."""
        self.disabled_clients.discard(client_id)
        self._audit("allow", "client re-enabled by admin", event="client_revocation", client_id=client_id)


def env_var_for(client_id: str) -> str:
    """Environment variable that holds a client's secret, e.g. STS_CLIENT_MCP_GATEWAY."""
    return "STS_CLIENT_" + client_id.upper().replace("-", "_")


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

    @app.post("/revoke")
    def revoke(
        token: str = Form(...),
        client_id: str = Form(...),
        client_secret: str = Form(...),
        token_type_hint: str | None = Form(None),
    ):
        """RFC 7009 endpoint. 200 with an empty body on success, including for tokens
        this STS does not know; 401 when the client fails to authenticate; 400
        unauthorized_client when the token is genuine but not the client's to revoke."""
        try:
            sts.revoke(token, token_type_hint, client_id=client_id, client_secret=client_secret)
        except ExchangeError as e:
            return err(e)
        return Response(status_code=200)

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sts = STS(issuer="http://127.0.0.1:8099")
    # Client secrets come from the environment and are never printed or logged.
    # A client with no variable set keeps a random value nobody knows, so it cannot authenticate.
    unset: list[str] = []
    for c in sts.clients.values():
        var = env_var_for(c.client_id)
        if os.environ.get(var):
            c.secret = os.environ[var]
        else:
            unset.append(var)
    if unset:
        print("clients disabled until these are set: " + ", ".join(unset), file=sys.stderr)
    uvicorn.run(create_app(sts), host="127.0.0.1", port=8099)
