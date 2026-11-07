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
a verifier can share in process.

Tool reports: /revoke takes an optional reason (cleanup, anomaly,
injection_suspected). A tool server is a registered client with no audiences
and no scopes, so it can authenticate and revoke exactly the tool token it was
presented (it is the holder named in aud) and nothing above it. The STS, not
the tool, decides whether a report widens: strikes per (grant_id, actor chain)
inside EscalationPolicy.window_s escalate from the presented token to the
immediate actor's delegation to the grant. Detection at the edge, authority at
the center, blast radius bounded by policy the tool does not control.

Admin: the ops-console client holds scope sts:admin, which no exchange ever
issues. Its actor token is the credential for /admin/revoke/grant,
/admin/revoke/client, /admin/enable/client and /admin/revoke/key. Same client
machinery as every other caller, so it maps onto an admin SVID later instead
of a side door.

Sender constraint (RFC 9449 DPoP, threat 1): every registered client holds an
EC P-256 key, generated at startup next to its secret. With require_dpop=True
(the default) /token needs a DPoP proof on every call and every token this STS
issues carries cnf.jkt, the RFC 7638 thumbprint of the key that may present it:

  actor token    the proof key, which must be the client's registered key
  user token     the key of the client the user consented to (it is the holder)
  tool token     the requesting actor's key (the proof key)
  handoff token  the delegate's registered key (the next actor), chosen at
                 issuance rather than at pickup; see the README for the alternative

At exchange the proof key must equal cnf.jkt of both the actor token and the
subject token, and a token without cnf is refused, so binding cannot be
downgraded by presenting an unbound token. A missing, malformed, stale or
replayed proof is 400 invalid_dpop_proof (RFC 9449 section 5). The verifier
in verify.py does the matching check at the tool with ath over the token.
revoke_key(jkt) denylists every token bound to a key and refuses new proofs
from it. Server nonces are not implemented (see dpop.py). require_dpop=False
restores the previous bearer behaviour, kept so the diff is a flag.

Nothing here is production-grade; it is the smallest thing that closes threats
1 and 11 in the prototype.
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
from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import JSONResponse, Response

from dpop import DPoPError, Proof, ReplayCache, generate_key, thumbprint, verify_proof
from revocation import Denylist, EscalationPolicy, Issued

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
ADMIN_SCOPE = "sts:admin"  # held only by ops-console; never issued by exchange or login
REVOKE_REASONS = {"cleanup", "anomaly", "injection_suspected"}
STRIKE_REASONS = {"anomaly", "injection_suspected"}
DPOP_PROOF_MAX_AGE_S = 60

log = logging.getLogger("sts.audit")


@dataclass
class Client:
    client_id: str
    audiences: set[str]
    scopes: set[str]
    may_delegate_to: set[str] = field(default_factory=set)
    ttl: int = 300
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    # DPoP key. In process (demo, tests) the client object holds the private key
    # and signs its own proofs. The STS only ever needs the thumbprint: a
    # standalone sts.py registers jkt from the environment and leaves key None.
    key: object | None = field(default_factory=generate_key, repr=False)
    jkt: str = ""

    def __post_init__(self) -> None:
        if not self.jkt and self.key is not None:
            self.jkt = thumbprint(self.key)

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
        # Tool servers: no audiences, no scopes, so they can exchange nothing. Being
        # a client lets a tool authenticate at /revoke and report the token it was
        # presented (it is the holder named in aud).
        Client("tool-github", audiences=set(), scopes=set()),
        Client("tool-jira", audiences=set(), scopes=set()),
        Client("tool-fs", audiences=set(), scopes=set()),
        # Operator console: the only holder of sts:admin. Its actor token is the
        # bearer credential for the /admin routes.
        Client("ops-console", audiences=set(), scopes={ADMIN_SCOPE}),
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
        escalation: EscalationPolicy | None = None,
        require_dpop: bool = True,
        dpop_max_age_s: int = DPOP_PROOF_MAX_AGE_S,
    ):
        self.issuer = issuer
        self.clients = {c.client_id: c for c in (clients or default_clients())}
        self.max_depth = max_depth
        self.user_ttl = user_ttl
        self.actor_ttl = actor_ttl
        self.clock = clock
        self.escalation = escalation or EscalationPolicy()
        # Sender constraint. True: /token needs a proof and every token is bound.
        # False: the pre-DPoP bearer behaviour (a proof, if sent, is still honoured).
        self.require_dpop = require_dpop
        self.dpop_max_age_s = dpop_max_age_s
        self.dpop_seen = ReplayCache(clock=lambda: self.clock())
        self.revoked_keys: set[str] = set()  # thumbprints refused on every future proof
        # Strikes from tool reports: (grant_id, actor chain) -> timestamps inside the window.
        self._strikes: dict[tuple[str, tuple[str, ...]], list[int]] = {}
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
            jkt=(claims.get("cnf") or {}).get("jkt"),
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

        With require_dpop the token is bound (cnf.jkt) to the registered key of
        client_id: the user hands this token to that agent, so the agent is its
        holder and the only party that can prove possession at the exchange. A
        separate key for the user's own device is not modelled here.
        """
        client = self.clients[client_id]
        if ADMIN_SCOPE in scopes:
            raise ExchangeError("invalid_scope", f"{ADMIN_SCOPE} is never issued to a user")
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
        if self.require_dpop:
            claims["cnf"] = {"jkt": client.jkt}
        self._record(claims, "user", client_id=None)
        return self._sign(claims)

    # ---- DPoP -------------------------------------------------------------
    def _check_proof(self, dpop: str | None, htm: str, htu: str, expected_jkt: str | None = None) -> Proof | None:
        """Verify the proof on a /token call. None means the flag is off and no
        proof was sent (bearer mode). Any failure is 400 invalid_dpop_proof."""
        if dpop is None and not self.require_dpop:
            return None
        try:
            proof = verify_proof(
                dpop, htm, htu, expected_jkt=expected_jkt,
                clock=self.clock, max_age_s=self.dpop_max_age_s, seen=self.dpop_seen,
            )
        except DPoPError as e:
            raise ExchangeError("invalid_dpop_proof", str(e))
        if proof.jkt in self.revoked_keys:
            raise ExchangeError("invalid_dpop_proof", f"key {proof.jkt[:8]} is revoked")
        return proof

    @staticmethod
    def _check_binding(name: str, claims: dict, proof: Proof | None) -> None:
        """A token presented at /token must be bound to the proof key. In bearer
        mode (proof None) nothing is checked; in DPoP mode an unbound token is
        refused too, so binding cannot be stepped down by presenting one."""
        if proof is None:
            return
        jkt = (claims.get("cnf") or {}).get("jkt") if isinstance(claims.get("cnf"), dict) else None
        if not jkt:
            raise ExchangeError("invalid_grant", f"{name} is not sender-constrained (no cnf.jkt)")
        if jkt != proof.jkt:
            raise ExchangeError("invalid_grant", f"{name} is bound to another key")

    def _authenticate(self, client_id: str, client_secret: str) -> Client:
        client = self.clients.get(client_id)
        if not client or not secrets.compare_digest(client.secret, client_secret or ""):
            raise ExchangeError("invalid_client", "client authentication failed", status=401)
        if client_id in self.disabled_clients:
            raise ExchangeError("invalid_client", f"client {client_id} is revoked", status=401)
        return client

    def client_credentials(
        self,
        client_id: str,
        client_secret: str,
        dpop: str | None = None,
        htm: str = "POST",
        htu: str | None = None,
    ) -> dict:
        """Actor token. With require_dpop the proof must come from the client's
        registered key (secret and key are both needed, so a leaked secret alone
        mints nothing usable) and the token is bound to it."""
        ctx: dict = {"client_id": client_id}
        try:
            client = self._authenticate(client_id, client_secret)
            proof = self._check_proof(dpop, htm, htu or f"{self.issuer}/token")
            if proof is not None and proof.jkt != client.jkt:
                raise ExchangeError("invalid_dpop_proof", f"proof key is not the registered key for {client_id}")
        except ExchangeError as e:
            self._audit("deny", e.reason, event="client_credentials", error=e.error, **ctx)
            raise
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
        if proof is not None:
            claims["cnf"] = {"jkt": proof.jkt}
        self._record(claims, "actor", client_id=client.client_id)
        return {
            "access_token": self._sign(claims),
            "token_type": "DPoP" if proof is not None else "Bearer",
            "expires_in": self.actor_ttl,
        }

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
        dpop: str | None = None,
        htm: str = "POST",
        htu: str | None = None,
    ) -> dict:
        ctx: dict = {"client_id": client_id, "audience": audience, "delegate_to": delegate_to, "requested_scope": scope}
        try:
            actor = self._authenticate(client_id, client_secret)
            # The proof is checked before anything about the tokens, so a bad proof
            # never learns whether the tokens it came with would have been accepted.
            proof = self._check_proof(dpop, htm, htu or f"{self.issuer}/token")
            if proof is not None:
                ctx["jkt"] = proof.jkt

            if not actor_token:
                subj = self._decode("subject_token", subject_token, audience=None)
                if "act" in subj or "may_act" in subj:
                    raise ExchangeError("invalid_request", "plain exchange refused: subject carries delegation claims and would lose its act chain")
                raise ExchangeError("invalid_request", "delegation requires an actor_token")

            act_claims = self._decode("actor_token", actor_token, audience=self.issuer)
            if act_claims.get("token_use") != "actor" or act_claims.get("client_id") != actor.client_id:
                raise ExchangeError("invalid_grant", "actor_token does not belong to the authenticated client")
            self._refuse_if_revoked("actor_token", act_claims)
            self._check_binding("actor_token", act_claims, proof)

            subj = self._decode("subject_token", subject_token, audience=actor.client_id)
            ctx["sub"] = subj["sub"]
            ctx["grant_id"] = subj.get("grant_id")
            self._refuse_if_revoked("subject_token", subj)
            # The subject (user token or handoff) was bound to this actor's key when
            # it was issued; the same proof must cover it.
            self._check_binding("subject_token", subj, proof)
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
            if ADMIN_SCOPE in requested or ADMIN_SCOPE in held:
                raise ExchangeError("invalid_scope", f"{ADMIN_SCOPE} is never issued by exchange")
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
            if proof is not None:
                # Binding choice: a tool token is bound to the key that just proved
                # itself (the requesting actor presents it at the tool). A handoff is
                # bound at issuance to the delegate's registered key, because the
                # delegate, not the gateway, presents it at the next exchange; the
                # gateway can carry it but cannot use it. The alternative, binding at
                # pickup (the delegate exchanges a gateway-bound handoff with its own
                # proof and the STS re-binds), is discussed in the README.
                claims["cnf"] = {"jkt": nxt.jkt if nxt else proof.jkt}
            self._record(claims, "handoff" if nxt else "tool", client_id=actor.client_id)
            token = self._sign(claims)
            self._audit("allow", "issued", **ctx, chain_out=act_chain(act), scope=claims["scope"], exp=exp,
                        bound_to=claims.get("cnf", {}).get("jkt"))
            return {
                "access_token": token,
                "issued_token_type": ACCESS_TOKEN_TYPE,
                # RFC 8693 section 2.2.1: N_A when the issued token is not for direct
                # use at a resource; the handoff goes back to this STS as a subject.
                "token_type": "N_A" if nxt else ("DPoP" if proof is not None else "Bearer"),
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

    def revoke(
        self,
        token: str,
        token_type_hint: str | None = None,
        *,
        client_id: str,
        client_secret: str,
        reason: str | None = None,
    ) -> None:
        """RFC 7009 revocation, with one extension and an optional report reason.

        Per RFC 7009: the client authenticates; an unknown, malformed, forged or
        already-expired token returns success and changes nothing (no probing
        through the response); the client may revoke only a token it holds or a
        token in a chain it participates in (see _may_revoke), and a genuine token
        outside that set is refused with unauthorized_client, as section 2.1 of the
        RFC says to inform the client. token_type_hint is accepted and ignored: this
        STS issues one token type, no refresh tokens, and tells actor tokens apart
        by their own token_use claim. A missing or empty token is invalid_request
        (RFC 6749 section 5.2), the one shape the endpoint will not accept silently.

        Extension beyond RFC 7009 (the RFC is silent on exchanged tokens): revoking
        a token revokes every token exchanged from it, by lineage, and revoking a
        user token revokes its grant_id, so every token in the family is dead even
        if the registry no longer has it. Every outcome is audited with its reason.

        reason (not in RFC 7009) is one of cleanup, anomaly, injection_suspected;
        anything else is invalid_request. It never widens what the caller may
        revoke. anomaly and injection_suspected add a strike for (grant_id, actor
        chain) and the STS applies EscalationPolicy on top of the caller's
        revocation: see _escalate. cleanup never counts.
        """
        ctx: dict = {"client_id": client_id, "token_type_hint": token_type_hint, "report_reason": reason}
        try:
            client = self._authenticate(client_id, client_secret)
            if reason is not None and reason not in REVOKE_REASONS:
                raise ExchangeError("invalid_request", f"reason must be one of {', '.join(sorted(REVOKE_REASONS))}")
            if not token:
                raise ExchangeError("invalid_request", "token required")
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
            if reason in STRIKE_REASONS and claims.get("grant_id"):
                self._escalate(claims, reason)
        except ExchangeError as e:
            self._audit("deny", e.reason, event="token_revocation", error=e.error, **ctx)
            raise

    # ---- escalation (STS-owned policy over tool reports) ------------------
    def _strike(self, grant_id: str, chain: tuple[str, ...]) -> int:
        """Record a strike and return the count inside the window for this key."""
        now = self._now()
        floor = now - self.escalation.window_s
        for key in [k for k, ts in self._strikes.items() if not ts or ts[-1] <= floor]:
            del self._strikes[key]  # bounded by the window, like the denylist
        hits = [t for t in self._strikes.get((grant_id, chain), []) if t > floor]
        hits.append(now)
        self._strikes[(grant_id, chain)] = hits
        return len(hits)

    def _escalate(self, claims: dict, reason: str) -> None:
        """Widen a reported revocation by policy, never by the caller's authority.

        strike 1               the presented token (already revoked by revoke())
        strike >= handoff_at   the immediate actor's delegation: the handoff the
                               token descends from when there is one, otherwise
                               every token that actor exchanged under this grant
        strike >= grant_at     the grant
        A hostile or buggy tool can therefore cost the actor its handoff after
        handoff_at reports and the user the grant after grant_at reports inside
        the window. That is the deliberate trade: detection at the edge, authority
        at the center, blast radius bounded by policy the tool does not control.
        """
        grant_id = claims["grant_id"]
        chain = tuple(act_chain(claims.get("act")))
        strikes = self._strike(grant_id, chain)
        step, denied = "token", set()
        if strikes >= self.escalation.grant_at:
            step, denied = "grant", self._revoke_grant(grant_id)
        elif strikes >= self.escalation.handoff_at:
            parent = self._issued.get(claims.get("parent_jti") or "")
            if parent and parent.kind == "handoff":
                step, denied = "handoff", self._revoke_subtree(parent.jti, parent.exp)
            else:
                step = "delegation"
                for r in list(self._issued.values()):
                    if r.grant_id == grant_id and r.client_id == (chain[0] if chain else None):
                        denied |= self._revoke_subtree(r.jti, r.exp)
        self._audit("escalated" if step != "token" else "noted", f"strike {strikes}: {step}",
                    event="revocation_escalation", report_reason=reason, strikes=strikes, step=step,
                    grant_id=grant_id, chain=list(chain), revoked_jtis=len(denied))

    # ---- admin (handles, not token strings; outside RFC 7009) -------------
    def _revoke_grant(self, grant_id: str) -> set[str]:
        matching = [r for r in self._issued.values() if r.grant_id == grant_id]
        exp = max((r.exp for r in matching), default=self._now() + self.user_ttl)
        self.denylist.add_grant(grant_id, exp)
        for r in matching:
            self.denylist.add_jti(r.jti, exp)
        return {r.jti for r in matching}

    def revoke_grant(self, grant_id: str, by: str = "admin") -> int:
        """Admin: revoke a consent by its handle. Kills every live token carrying
        this grant_id. An unknown grant_id is a no-op with count 0, still audited.
        Returns how many issued tokens were denied."""
        count = len(self._revoke_grant(grant_id))
        self._audit("revoked", "grant revoked by admin", event="grant_revocation", by=by, grant_id=grant_id, revoked_jtis=count)
        return count

    def revoke_client(self, client_id: str, by: str = "admin") -> int:
        """Admin: retire an agent by its client_id handle.

        Kills every live token where the client is the actor (its actor tokens and
        the tokens it requested) or appears anywhere in act, and refuses the client
        new actor tokens and exchanges until enable_client. An unknown client_id
        changes nothing and returns 0, still audited.
        """
        if client_id not in self.clients:
            self._audit("noop", "unknown client", event="client_revocation", by=by, client_id=client_id, revoked_jtis=0)
            return 0
        self.disabled_clients.add(client_id)
        denied: set[str] = set()
        for r in list(self._issued.values()):
            if r.client_id == client_id or client_id in r.actors:
                denied |= self._revoke_subtree(r.jti, r.exp)
        self._audit("revoked", "client revoked by admin", event="client_revocation", by=by, client_id=client_id, revoked_jtis=len(denied))
        return len(denied)

    def enable_client(self, client_id: str, by: str = "admin") -> bool:
        """Admin: lift revoke_client. Tokens denied while it was revoked stay denied.
        Returns whether the client is known."""
        self.disabled_clients.discard(client_id)
        self._audit("allow" if client_id in self.clients else "noop", "client re-enabled by admin",
                    event="client_revocation", by=by, client_id=client_id)
        return client_id in self.clients

    def revoke_key(self, jkt: str, by: str = "admin") -> int:
        """Admin: revoke a DPoP key by its thumbprint.

        Denylists every live token bound to the key (cnf.jkt in the issuance
        registry) together with each one's descendants, and refuses every future
        proof from the key at /token. A client whose registered key this is can
        no longer obtain actor tokens until it is re-registered with a new key
        (rotation is not built). An unknown thumbprint is recorded and returns 0,
        so the answer does not reveal whether the key was ever seen.
        """
        self.revoked_keys.add(jkt)
        denied: set[str] = set()
        for r in list(self._issued.values()):
            if r.jkt == jkt:
                denied |= self._revoke_subtree(r.jti, r.exp)
        self._audit("revoked", "key revoked by admin", event="key_revocation", by=by, jkt=jkt, revoked_jtis=len(denied))
        return len(denied)

    def authorize_admin(
        self,
        authorization: str | None,
        dpop: str | None = None,
        htm: str = "POST",
        htu: str | None = None,
    ) -> Client:
        """Resolve an actor token to a client holding sts:admin.

        401 invalid_client: no token, not a token this STS issued, not an actor
        token (a user or tool token presented here is refused this way), revoked,
        or from a disabled client. 401 invalid_dpop_proof: a key-bound actor token
        without the DPoP scheme or without a fresh proof from its key, with ath
        over the token (RFC 9449 section 7.1, the resource-server side). 403
        insufficient_scope: a real actor token whose client does not hold sts:admin.
        """
        scheme, _, token = (authorization or "").partition(" ")
        scheme = scheme.lower()
        if scheme not in ("bearer", "dpop") or not token.strip():
            raise ExchangeError("invalid_client", "actor token required", status=401)
        token = token.strip()
        try:
            claims = self._decode("actor_token", token, audience=self.issuer)
            self._refuse_if_revoked("actor_token", claims)
        except ExchangeError as e:
            raise ExchangeError("invalid_client", e.reason, status=401)
        client = self.clients.get(claims.get("client_id") or "")
        if claims.get("token_use") != "actor" or client is None or client.client_id in self.disabled_clients:
            raise ExchangeError("invalid_client", "not a live actor token", status=401)
        jkt = (claims.get("cnf") or {}).get("jkt") if isinstance(claims.get("cnf"), dict) else None
        if jkt:
            if scheme != "dpop":
                raise ExchangeError("invalid_dpop_proof", "key-bound token must use the DPoP scheme", status=401)
            try:
                verify_proof(dpop, htm, htu or f"{self.issuer}/admin", expected_jkt=jkt, ath_of=token,
                             clock=self.clock, max_age_s=self.dpop_max_age_s, seen=self.dpop_seen)
            except DPoPError as e:
                raise ExchangeError("invalid_dpop_proof", str(e), status=401)
            if jkt in self.revoked_keys:
                raise ExchangeError("invalid_dpop_proof", f"key {jkt[:8]} is revoked", status=401)
        elif self.require_dpop:
            raise ExchangeError("invalid_client", "actor token is not sender-constrained", status=401)
        if ADMIN_SCOPE not in client.scopes:
            raise ExchangeError("insufficient_scope", f"{client.client_id} does not hold {ADMIN_SCOPE}", status=403)
        return client


def env_var_for(client_id: str) -> str:
    """Environment variable that holds a client's secret, e.g. STS_CLIENT_MCP_GATEWAY."""
    return "STS_CLIENT_" + client_id.upper().replace("-", "_")


def jkt_env_var_for(client_id: str) -> str:
    """Environment variable that holds a client's registered DPoP key thumbprint,
    e.g. STS_JKT_MCP_GATEWAY. The STS never holds a client's private key; the
    client makes one with `python dpop.py keygen <file>` and registers the printed jkt."""
    return "STS_JKT_" + client_id.upper().replace("-", "_")


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
        try:
            return {"access_token": sts.issue_user_token(user, client_id, set(scope.split()), consent)}
        except ExchangeError as e:
            return err(e)

    @app.post("/token")
    def token(
        request: Request,
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
        dpop: str | None = Header(None),  # RFC 9449 section 4.1: the DPoP header; htm and htu come from the request itself
    ):
        htm, htu = request.method, str(request.url)
        try:
            if grant_type == "client_credentials":
                return sts.client_credentials(client_id, client_secret, dpop=dpop, htm=htm, htu=htu)
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
                dpop=dpop,
                htm=htm,
                htu=htu,
            )
        except ExchangeError as e:
            return err(e)

    @app.post("/revoke")
    def revoke(
        client_id: str = Form(...),
        client_secret: str = Form(...),
        token: str | None = Form(None),  # required by RFC 7009; checked in STS.revoke so the error body is 6749-shaped, not a 422
        token_type_hint: str | None = Form(None),
        reason: str | None = Form(None),  # non-standard: cleanup | anomaly | injection_suspected
    ):
        """RFC 7009 endpoint. 200 with an empty body on success, including for tokens
        this STS does not know; 400 invalid_request when token is missing or empty or
        reason is unknown; 401 when the client fails to authenticate; 400
        unauthorized_client when the token is genuine but not the client's to revoke."""
        try:
            sts.revoke(token, token_type_hint, client_id=client_id, client_secret=client_secret, reason=reason)
        except ExchangeError as e:
            return err(e)
        return Response(status_code=200)

    # Admin routes: actor token from a client holding sts:admin, presented with the
    # DPoP scheme and a proof over the token when the token is key-bound. Handles
    # in, counts out; an unknown handle is 200 with zero counts, so nothing can be probed.
    def admin(request: Request, authorization: str | None, dpop: str | None) -> Client:
        return sts.authorize_admin(authorization, dpop=dpop, htm=request.method, htu=str(request.url))

    @app.post("/admin/revoke/grant")
    def admin_revoke_grant(request: Request, grant_id: str = Form(...), authorization: str | None = Header(None), dpop: str | None = Header(None)):
        try:
            who = admin(request, authorization, dpop)
        except ExchangeError as e:
            return err(e)
        return {"grant_id": grant_id, "revoked_jtis": sts.revoke_grant(grant_id, by=who.client_id)}

    @app.post("/admin/revoke/client")
    def admin_revoke_client(request: Request, client_id: str = Form(...), authorization: str | None = Header(None), dpop: str | None = Header(None)):
        try:
            who = admin(request, authorization, dpop)
        except ExchangeError as e:
            return err(e)
        count = sts.revoke_client(client_id, by=who.client_id)
        return {"client_id": client_id, "revoked_jtis": count, "disabled": client_id in sts.disabled_clients}

    @app.post("/admin/enable/client")
    def admin_enable_client(request: Request, client_id: str = Form(...), authorization: str | None = Header(None), dpop: str | None = Header(None)):
        try:
            who = admin(request, authorization, dpop)
        except ExchangeError as e:
            return err(e)
        known = sts.enable_client(client_id, by=who.client_id)
        return {"client_id": client_id, "known": known, "disabled": client_id in sts.disabled_clients}

    @app.post("/admin/revoke/key")
    def admin_revoke_key(request: Request, jkt: str = Form(...), authorization: str | None = Header(None), dpop: str | None = Header(None)):
        try:
            who = admin(request, authorization, dpop)
        except ExchangeError as e:
            return err(e)
        return {"jkt": jkt, "revoked_jtis": sts.revoke_key(jkt, by=who.client_id)}

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sts = STS(issuer="http://127.0.0.1:8099")
    # Client secrets and registered key thumbprints come from the environment and
    # are never printed or logged. A client with no variable set keeps a random
    # secret nobody knows and a random in-process key nobody else holds, so it
    # cannot authenticate. Out of process the STS holds only the thumbprint.
    unset: list[str] = []
    for c in sts.clients.values():
        var = env_var_for(c.client_id)
        if os.environ.get(var):
            c.secret = os.environ[var]
        else:
            unset.append(var)
        jvar = jkt_env_var_for(c.client_id)
        if os.environ.get(jvar):
            c.key, c.jkt = None, os.environ[jvar].strip()
        else:
            unset.append(jvar)
    if unset:
        print("clients disabled until these are set: " + ", ".join(unset), file=sys.stderr)
    uvicorn.run(create_app(sts), host="127.0.0.1", port=8099)
