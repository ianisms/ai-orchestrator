# Prototype A: RFC 8693 delegation for agents

The user consents to an agent. The agent exchanges the user's token for a
short-lived, audience-scoped token that carries an `act` claim. A tool server
checks both who the user is (`sub`) and which agents are acting (`act`), and
rejects any token without `act`. The chain can go one level deeper:
user > mcp-gateway > code-review-subagent, with access narrowing at each hop.

```
alice --consent--> user token (aud=mcp-gateway, may_act=mcp-gateway)
mcp-gateway --exchange + actor token--> handoff token (aud=sub-agent, may_act=sub-agent, act=[gateway])
code-review-subagent --exchange + actor token--> tool token (aud=tool-github, act=[gateway, sub-agent], 120 s)
tool-github --verify--> alice > mcp-gateway > code-review-subagent, scope repo:read
```

## Run

```
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
python demo.py          # chain, attack table and a revocation, no servers needed
python -m pytest -q     # 47 tests
python sts.py           # optional: STS on 127.0.0.1:8099; set STS_CLIENT_MCP_GATEWAY etc. first (secrets are never printed)
```

## Files

- `sts.py` - the STS: client credentials for actor tokens, token exchange with the delegation rules, JWKS, audit log of every decision with its reason. Dev only: key and client secrets live in memory.
- `verify.py` - the upstream check a tool server runs. Accepts tokens from this STS (key) or from Keycloak (JWKS URL). Optional `revocation=` hook, see below.
- `revocation.py` - the in-memory denylist (jti and grant_id with expiries), the STS's record of issued tokens, used to walk a token's descendants, and the `EscalationPolicy` defaults.
- `admin.py` - operator CLI for the admin routes: gets an ops-console actor token and calls them. `python admin.py revoke-grant <id>` from here, or `python -m delegation.admin ...` from `security/`.
- `demo.py` - end-to-end run and attack table.
- `tests/` - happy paths and attacks.
- `keycloak/` - Keycloak 26.8 compose and steps for hop 1 (preview feature).

## Rules the STS enforces

- Delegation needs an `actor_token`, and it must belong to the authenticated client.
- The subject token's `aud` and `may_act` must both name the actor.
- Access only narrows: scope must be held by the subject and registered for the actor (and for the next actor on a handoff).
- Audience must be registered for the actor; `delegate_to` must be on its allow list.
- A plain exchange of a token carrying `act` or `may_act` is refused, so the chain cannot be stripped.
- Chain depth is capped at 2 actors, counted when a handoff is issued.
- `exp` = min(now + actor TTL, subject exp). No refresh tokens.
- The subject token, its grant, and every token in its lineage must be unrevoked.

## Revocation

Every token carries three lineage claims: `jti` (unique per token), `grant_id`
(minted on the user token at consent and copied unchanged through every exchange)
and, on exchanged tokens, `parent_jti` (the jti of the subject token consumed).
That is enough to follow a chain downward from any token.

`POST /revoke` (also `STS.revoke`) follows RFC 7009:

- The client authenticates. A bad secret is 401 `invalid_client`.
- An unknown, malformed, forged or already-expired token gets 200 and changes
  nothing, so the response cannot be used to probe. A token signed by another
  issuer (Keycloak, for hop 1) falls in this bucket: this STS cannot revoke what
  it did not issue.
- `token_type_hint` is accepted and ignored; there is one token type and no refresh tokens.
- Who may revoke what: a client may revoke a token it holds (its `client_id` is in
  `aud`, or equals the token's `client_id` claim) or any token in whose `act` chain
  it appears. So the gateway can revoke the sub-agent's tool token, the sub-agent
  can revoke its own tool token, and neither notes-agent nor the sub-agent can
  revoke Alice's user token: a downstream, less trusted actor does not get to end
  the session above it. A genuine token outside that set is 400 `unauthorized_client`,
  as section 2.1 of the RFC says to inform the client.

Extension beyond RFC 7009 (which is silent on exchanged tokens): revoking a token
revokes every token exchanged from it, by lineage, and revoking a user token
revokes its `grant_id`, so every token in that family is dead whether or not the
STS still remembers it. Revoking one handoff leaves its siblings alone.

Admin paths, outside RFC 7009 and taking handles rather than token strings:
`STS.revoke_grant(grant_id)` kills a consent, `STS.revoke_client(client_id)`
kills every live token where that client is the actor or appears in `act` and
refuses it new actor tokens and exchanges until `enable_client`. They are
reachable over HTTP through the `ops-console` client; see "Admin revocation".

State is `revocation.Denylist`: revoked jtis and grant_ids, each with an expiry
equal to the latest `exp` in the revoked family, pruned as they expire. No
exchanged token outlives its user token (600 s), so the list stays bounded no
matter how many revocations arrive. Nothing persists; a restart also rotates the
signing key, so nothing outstanding survives it either.

Enforcement, two places:

- `sts.py` refuses an exchange when the subject token, its grant, or any jti in
  its lineage is revoked, and refuses a revoked client's actor token.
- `verify.py` verifies the JWT locally first (signature, issuer, audience, expiry,
  so a forged token never reaches the revocation state), then consults an optional
  `RevocationCheck`. Two implementations: the STS denylist shared in process (what
  `demo.py` does) and `CallbackRevocationCheck`, which wraps any callable and stands
  in for a deny list pushed to each PEP, the target in `DIAGRAMS.md` section 4.
  With no checker the verifier stays offline and unaware of revocations. A checker
  that raises is a refusal. Per-call introspection (RFC 7662) was not chosen: it adds
  a network hop to every tool call and makes the STS a central dependency for all
  tools, a second copy of the availability cost the PDP already carries (threat 9).

Every revocation decision (revoked, noop, deny) and every refusal on the verify
side is logged with its reason, in the same JSON form as the exchange audit log.

### What RFC 7009 says, what this STS does

| Topic | RFC 7009 | This STS |
|---|---|---|
| Endpoint and parameters | `POST` to the revocation endpoint; `token` required; `token_type_hint` optional, a wrong hint must not stop the server from finding the token | `POST /revoke`; `token` required (missing or empty is 400 `invalid_request`); `token_type_hint` accepted and ignored, since every token carries its own `token_use` and there is one token type |
| Client authentication and ownership | Client authenticates as for the token endpoint; the server verifies the token was issued to the requesting client, else refuses | Same client secret check as `/token` (401 `invalid_client`). "Issued to" is read as: the client is the holder (named in `aud` or in the `client_id` claim) or any client in the `act` chain. Downstream never revokes upstream |
| Invalid token | Section 2.2: respond 200 whether or not the token was valid, so the client cannot probe | 200 and no change for anything the STS cannot validate: malformed, forged, signed by another issuer, expired, no `jti` |
| Error codes | RFC 6749 section 5.2 codes plus `unsupported_token_type`; no code is bound to "not your token" | `invalid_request`, `invalid_client`, and `unauthorized_client` for a genuine token outside the caller's authority (our choice, the spec leaves it open); `unsupported_token_type` is never returned because hints are ignored |
| Related tokens | Revoking a refresh token SHOULD revoke the access tokens issued with it; revoking an access token MAY revoke its refresh token | No refresh tokens exist. Revoking a token revokes its lineage subtree, and revoking a user token revokes its `grant_id`. That is an extension for exchanged tokens, not a reading of 7009 |
| Immediate effect | Section 2.1: the token is invalidated immediately | Immediate at the STS and at any verifier wired to the denylist; anywhere that is not wired, the token lives until its TTL, at most 600 s |

### Where the denylist lives

In the STS process. `sts.py` writes it; `verify.py` never imports the STS and
reaches the state only through the `RevocationCheck` it was handed. In `demo.py`
and the tests that object is the STS's own `Denylist`, so the "callback" is a
method call in the same process. Across real PEPs the same seam becomes a push
from the STS to each verifier, or a shared store both read; that transport is the
part that is not built. RFC 7009 section 3 calls the interaction between the
authorization server and the resource servers for this purpose out of scope and
not standardized, which is why the spec leaves it open and why `verify.py` takes
a protocol rather than an address.

Size is bounded by lifetime, not by traffic: every entry expires at the latest
`exp` in its family, and no exchanged token outlives the user token it came
from, so the set holds at most one user-token lifetime (600 s) of revocations.

### Tool reports and escalation

A tool server is a registered client with no audiences and no scopes
(`tool-github`, `tool-jira`, `tool-fs`), so it can authenticate at `/revoke` but
can exchange nothing. By the ownership rule above it is the holder of exactly the
tool tokens addressed to it, so it may revoke the token it was presented and
nothing above it: the handoff, the user token and other tools' tokens are all
`unauthorized_client`, whatever reason it gives.

`/revoke` takes an optional `reason`: `cleanup`, `anomaly` or
`injection_suspected`; anything else is 400 `invalid_request`. The reason is
audited on every revocation. It never widens what the caller may revoke. What it
does is feed a strike counter the STS owns, keyed by (`grant_id`, actor chain of
the reported token), inside `EscalationPolicy.window_s` (default 10 minutes;
the clock is the STS clock, so tests can move it). `cleanup` never counts.

| Strikes in window | What the STS revokes, on top of the caller's own revocation |
|---|---|
| 1 | nothing more: the presented token only |
| 2 (`handoff_at`) | the immediate actor's delegation: the handoff the token descends from, or, for a one-actor chain, every token that actor exchanged under the grant |
| 3 (`grant_at`) | the grant: the user token and every token under it |

Every step is logged as `revocation_escalation` with the reason, the strike count,
the step and how many jtis were denied. Thresholds and window are the
`EscalationPolicy` dataclass passed to `STS(escalation=...)`.

The trade is deliberate and worth stating plainly: a hostile or buggy tool
server can cost the actor its handoff after two reports and the user their grant
after three, inside the window. Detection happens at the edge, where the tool
sees the call; authority stays at the center, where the STS applies a policy the
tool cannot change; and the blast radius is bounded by that policy, not by what
the reporter asks for. A tool that wants more than this gets `unauthorized_client`.
Repeated reports of the same token count as separate strikes; de-duplicating by
jti would be a reasonable tightening and is not done here.

### Admin revocation

`POST /admin/revoke/grant` (form `grant_id`), `POST /admin/revoke/client` and
`POST /admin/enable/client` (form `client_id`) take handles, not token strings,
and answer 200 with counts (`revoked_jtis`, `disabled`, `known`), never token
contents. An unknown handle is 200 with zero counts and is audited, so the routes
cannot be used to probe.

The credential is a bearer actor token from the `ops-console` client, the only
holder of scope `sts:admin`. That scope is refused by the dev login and by every
exchange, whether requested or merely held by the subject, so it can never land
in a user, handoff or tool token. No bearer, a user or tool token, a revoked
actor token or one from a disabled client is 401 `invalid_client`; a live actor
token from a client without `sts:admin` is 403 `insufficient_scope`. Both carry
RFC 6749 section 5.2 bodies. A user token presented here is treated as "not a
client credential" (401) rather than "a client without the scope" (403), because
the holder of a user token is not the entity this check is about.

Why the same client machinery rather than a shim: admin is just another caller
with a credential and a scope, so it gets the same authentication, the same
audit line (`by: ops-console`) and the same revocation (`revoke_client` on
`ops-console` locks the console out, recovery is `enable_client` on the host).
When agents move to SVIDs (prototype B) the console moves with them to an admin
SVID, with nothing special-cased to rewrite. Binding the routes to loopback
would be optional hardening on top; the scope check is the control.

`admin.py` is the thin CLI: `STS_URL` and `STS_CLIENT_OPS_CONSOLE` from the
environment, one actor token, one call, the JSON answer on stdout.

Keycloak caveat: Keycloak's `/revoke` only knows tokens Keycloak issued. For hop 1
to participate, this STS would have to introspect the subject token on every
exchange or consume Keycloak admin events (logout, consent withdrawn) and translate
them into grant revocations. Neither is built; today a Keycloak-issued user token
revoked at Keycloak still exchanges here until it expires.

## Attack results (from `demo.py`)

| Attack | Result |
|---|---|
| Expired tool token replayed | refused at tool: token expired |
| Exchange without `may_act` (no consent) | refused at STS: no may_act |
| Different agent presents the user's token | refused at STS: audience does not name this client |
| Sub-agent asks for `repo:write` (confused deputy) | refused at STS: scope exceeds what the subject holds |
| Sub-agent asks for an unregistered tool | refused at STS: audience not registered |
| Plain exchange to strip the `act` chain | refused at STS |
| Gateway uses another agent's actor token | refused at STS |
| Handoff token sent straight to a tool | refused at tool: not addressed to it |
| Prompt-injected write call | prototype C (policy check before `tools/call`) |
| Agent outside the chain revokes the user's token | refused at STS: unauthorized_client |
| Revoke a token this STS never issued | 200, nothing changes (no probing) |
| User token revoked, live tool token replayed | refused at tool: token revoked |
| User token revoked, handoff exchanged again | refused at STS: subject_token revoked |
| Another user's token after that revocation | still accepted (different grant) |
| Tool server revokes the handoff above it, any reason | refused at STS: unauthorized_client |
| Two reports from tool-github inside the window | never-reported sibling under the same handoff refused at tool; gateway's own token still accepted |
| Third report, fresh handoff, same chain | grant revoked; gateway's own token refused; user token refused on exchange |
| Gateway's actor token at an admin route | HTTP 403 insufficient_scope |
| ops-console retires an agent | its live tokens refused at tool; no new actor tokens |
| ops-console revokes an unknown grant | HTTP 200, zero counts (no probing) |

## Not covered here

- `delegate_to` is a non-standard parameter; RFC 8693 leaves how `may_act` is set to the issuer.
- No replay cache: within its lifetime a token can be presented more than once.
- Revocation is in process only. A tool server that does not share the denylist, or has no checker configured, does not see revocations; the pushed deny list across PEPs and the central audit sink are not built. A Keycloak-issued user token revoked at Keycloak is not revoked here (see the caveat above).
- Tool reports are trusted as far as the escalation policy allows and no further; nothing checks that the reported anomaly was real. The policy is static and global, not per tool or per grant.
- The ops-console is a client secret like every other agent; an admin SVID is the target, not built.
- No sender constraint (DPoP or mTLS-bound tokens); a stolen tool token works until it expires. Workload identity is prototype B.
- Keycloak hop 1 follows a preview feature; nested delegation in Keycloak is untested.
