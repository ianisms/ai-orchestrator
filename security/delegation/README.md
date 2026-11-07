# Prototype A: RFC 8693 delegation for agents

The user consents to an agent. The agent exchanges the user's token for a
short-lived, audience-scoped token that carries an `act` claim. A tool server
checks both who the user is (`sub`) and which agents are acting (`act`), and
rejects any token without `act`. The chain can go one level deeper:
user > mcp-gateway > code-review-subagent, with access narrowing at each hop.

```
alice --consent--> user token (aud=mcp-gateway, may_act=mcp-gateway, cnf.jkt=gateway key)
mcp-gateway --exchange + actor token + DPoP proof--> handoff token (aud=sub-agent, may_act=sub-agent, act=[gateway], cnf.jkt=sub-agent key)
code-review-subagent --exchange + actor token + DPoP proof--> tool token (aud=tool-github, act=[gateway, sub-agent], 120 s, cnf.jkt=sub-agent key)
tool-github --verify token + DPoP proof--> alice > mcp-gateway > code-review-subagent, scope repo:read, key sub-agent
```

Every token is bound to a key (`cnf.jkt`) and every call carries a DPoP proof
signed with that key, so a copied token is useless on its own. See "Sender
constraint (DPoP)" below.

## Run

```
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
python demo.py          # chain, attack table and a revocation, no servers needed
python -m pytest -q     # 71 tests
python sts.py           # optional: STS on 127.0.0.1:8099; set STS_CLIENT_MCP_GATEWAY etc. (secrets) and STS_JKT_MCP_GATEWAY etc. (key thumbprints) first; nothing is printed
python dpop.py keygen gateway.pem   # a client's DPoP key (PEM, mode 600); prints the thumbprint to register as STS_JKT_*
```

## Files

- `sts.py` - the STS: client credentials for actor tokens, token exchange with the delegation rules, DPoP binding of every token it issues, JWKS, audit log of every decision with its reason. Dev only: signing key, client secrets and client DPoP keys live in memory.
- `dpop.py` - RFC 9449 proofs: `make_proof` (client side), `verify_proof` (STS and tool side), the bounded `ReplayCache`, the RFC 7638 thumbprint, and `keygen` for the one out-of-process path.
- `verify.py` - the upstream check a tool server runs. Accepts tokens from this STS (key) or from Keycloak (JWKS URL). Takes the request's DPoP proof for a key-bound token. Optional `revocation=` hook, see below.
- `revocation.py` - the in-memory denylist (jti and grant_id with expiries), the STS's record of issued tokens (with the key each is bound to), used to walk a token's descendants, and the `EscalationPolicy` defaults.
- `admin.py` - operator CLI for the admin routes: gets an ops-console actor token with a DPoP proof and calls them under the DPoP scheme. `python admin.py revoke-grant <id>` from here, or `python -m delegation.admin ...` from `security/`.
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
- Every `/token` call carries a DPoP proof; the proof key must be the one the actor token and the subject token are bound to (`cnf.jkt`), and a token without `cnf` is refused. The issued token is bound to the key of whoever will present it next.

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
`POST /admin/enable/client` (form `client_id`) and `POST /admin/revoke/key`
(form `jkt`) take handles, not token strings, and answer 200 with counts
(`revoked_jtis`, `disabled`, `known`), never token contents. An unknown handle is
200 with zero counts and is audited, so the routes cannot be used to probe.

The credential is an actor token from the `ops-console` client, the only
holder of scope `sts:admin`, presented under the `DPoP` scheme with a proof
over it (the resource-server side of RFC 9449, section 7.1); the same bound
token under `Bearer`, or without a fresh proof from its key, is 401
`invalid_dpop_proof`. That scope is refused by the dev login and by every
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

`admin.py` is the thin CLI: `STS_URL`, `STS_CLIENT_OPS_CONSOLE` (secret) and
`STS_KEY_OPS_CONSOLE` (path to the console's DPoP key) from the environment,
one actor token, one call, the JSON answer on stdout.

Keycloak caveat: Keycloak's `/revoke` only knows tokens Keycloak issued. For hop 1
to participate, this STS would have to introspect the subject token on every
exchange or consume Keycloak admin events (logout, consent withdrawn) and translate
them into grant revocations. Neither is built; today a Keycloak-issued user token
revoked at Keycloak still exchanges here until it expires.

## Sender constraint (DPoP)

A bearer token is good for whoever holds the bytes. Threat 1 in
`THREAT_MODEL.md` is exactly that: a tool token copied out of an agent's
process works at the tool until it expires, 120 to 300 s. RFC 9449 (DPoP)
closes it by binding a token to a key the client holds and requiring, on every
request, a short signed proof that the client still holds it.

How it works here, end to end:

1. Each registered client (`mcp-gateway`, `code-review-subagent`, `notes-agent`,
   the tool servers, `ops-console`) has an EC P-256 key next to its secret,
   generated at startup and kept in the client object. The STS registers the
   key's RFC 7638 thumbprint (`jkt`); the private key stays with the client.
   The thumbprint is the same computation as `key_thumbprint` in
   `server/modules/orchestrator/core/device_identity.py`, so an agent's token
   and the speaker's device credential bind to a key the same way (`cnf.jkt`).
2. Every call to `/token` carries a `DPoP` header: a JWT with `typ: dpop+jwt`,
   `alg: ES256`, the public key in the header `jwk`, and claims `jti`, `htm`,
   `htu`, `iat`. The STS verifies it before it looks at any token in the
   request. A missing, malformed, stale (older than 60 s), replayed (jti seen)
   or wrong-URI proof is 400 `invalid_dpop_proof`.
3. The STS binds what it issues. The actor token carries `cnf.jkt` of the proof
   key, which must be the client's registered key (a leaked secret alone mints
   nothing). The user token is bound to the key of the agent the user
   consented to, since that agent is its holder. On an exchange the proof key
   must equal `cnf.jkt` of both the actor token and the subject token, and a
   token without `cnf` is refused, so binding cannot be stepped down. The tool
   token is bound to the requesting actor's key; the handoff is bound to the
   delegate's registered key (below).
4. At the tool, `verify.py` sees `cnf.jkt` and requires the request's proof:
   same checks as the STS plus `ath`, the hash of this exact token, and the
   thumbprint must equal `cnf.jkt`. Then, and only then, the revocation check
   runs. The proved `jkt` is on the `Principal` and in every audit line.
5. Admin routes take the actor token under the `DPoP` scheme with a proof
   carrying `ath`, and `revoke_key(jkt)` (`/admin/revoke/key`, `admin.py
   revoke-key`) denylists every live token bound to a key, with its
   descendants, and refuses every future proof from it.

Token responses say `token_type: DPoP` for actor and tool tokens; the handoff
stays `N_A` as RFC 8693 section 2.2.1 prescribes for a token that is not for
direct use at a resource (it goes back to the STS as a subject token).

### What RFC 9449 says, what this STS does

| Topic | RFC 9449 | This STS |
|---|---|---|
| Proof shape (section 4.2) | JWT, `typ` `dpop+jwt`, `alg` an asymmetric algorithm, header `jwk` public key; claims `jti`, `htm`, `htu`, `iat`, and `ath` when an access token is presented, `nonce` when the server issued one | Same. `alg` allowlist is ES256 only; a header `jwk` with any private member (`d`, `p`, `q`, ...) is refused; `htu` is compared without query and fragment with scheme and host case-folded |
| Checks (section 4.3) | typ, alg, jwk, signature, htm, htu, iat within an acceptable window, jti unique, nonce, ath, and the key matches the bound token | Same list. The window is 60 s back and 5 s forward on the verifier's own clock (the injectable STS clock, so tests can move it). jti uniqueness is a per-verifier replay cache keyed by jti, expiring at `iat + max_age`, pruned on every check, so it holds at most 60 s of proofs |
| Binding at the token endpoint (section 5) | The server binds the issued token to the proof key by `cnf.jkt`; `token_type` is `DPoP`; a bad proof is `invalid_dpop_proof` | Same for actor and tool tokens. Beyond the RFC: the proof key at `client_credentials` must be the client's registered key, and at exchange it must match the subject token's binding as well as the actor token's. The RFC binds to whatever key proves itself; this STS also requires that it be the key on file, because handoffs are bound to registered keys and a registry that any key can bypass would be decoration |
| Exchanged tokens | RFC 9449 does not address RFC 8693; RFC 8693 says nothing about `cnf` | The handoff is bound at issuance to the delegate's registered key; the tool token to the requesting actor's. That is this STS's choice, not a reading of either RFC |
| Resource server (section 7.1) | Checks the proof with `ath`, the `DPoP` authorization scheme, and the key against `cnf.jkt`; a bound token must not be accepted as Bearer | `verify.py` and the admin routes do this. A bound token under `Bearer` is 401 `invalid_dpop_proof`. `WWW-Authenticate: DPoP` challenge headers are not emitted |
| Server nonces (section 8) | A server MAY require a nonce it issued (`DPoP-Nonce`, `use_dpop_nonce`) to bound the window for pre-computed proofs | Not implemented. `verify_proof` will check a nonce it is handed, but nothing issues one. The replay cache plus the 60 s window bound the same thing in a closed system where every party shares one clock; nonces earn their round trip when clients are remote with clocks the server does not trust |
| Public clients and refresh tokens (section 5) | Refresh tokens issued to public clients are bound to the key | No refresh tokens, no public clients |

### Why the handoff is bound to the delegate

The gateway asks for a handoff, but the sub-agent is the party that presents
it (as `subject_token` at the next exchange). Two ways to bind it:

- At issuance, to the delegate's registered key, which is what is built. The
  STS already knows the delegate (`delegate_to` must be on the gateway's allow
  list and in the registry), so it knows the key. The gateway can carry the
  handoff but cannot use it, and a handoff copied in transit is dead on
  arrival anywhere but in the delegate's process. Cost: the delegate's key
  must be registered ahead of time, which this prototype does anyway.
- At pickup: bind the handoff to the gateway's key, let the delegate present
  it with its own proof plus some evidence the gateway meant it for this
  delegate, and re-bind on the exchange. That fits a world where delegates
  are not pre-registered (dynamic sub-agents) at the cost of a window in
  which the handoff is a bearer token between the two, and an extra claim or
  signature to carry the gateway's intent. Not built; the registry makes it
  unnecessary here.

### Why DPoP first, not mTLS-bound tokens

RFC 8705 binds a token to the client certificate on the TLS connection. That
needs certificates for every agent and tool, which is what prototype B's
X.509-SVIDs are, and it is the stated end state: once SPIRE issues SVIDs, the
mTLS connection the gateway and tools already use for the SVID becomes the
binding, with `cnf.x5t#S256` in place of `cnf.jkt` and no proof JWT per
request. DPoP needs no infrastructure beyond a key in each process and a
header on each call, so it lands now and closes the same threat at the
application layer. The verifier's `cnf` check is the seam: a token bound by
either method is "bound to a key the caller must prove", and the revocation
tie-in (`revoke_key`) is the same.

### The honest limit

DPoP binds a token to a software key in the agent's process. It stops token
theft: a copied token, a captured proof, a proof for another URI or another
tool, a stale proof, all refused. It does not stop process compromise: an
attacker inside the sub-agent's process has the key and signs proofs like the
sub-agent does. That is the layer the hardware key and SPIFFE address, not
this one. The speaker's device credential in `DEVICE_IDENTITY.md` is the
hardware-key case of the same `cnf.jkt` binding: the key is in a YubiKey
rather than a Python object, so it cannot be copied out of the process,
only used from it. Agents will get there when their keys move to an SVID
backed by attestation (prototype B). Until then, "bound" means "bound to this
process's memory".

Keys are generated at startup and die with the process, like the signing key.
Rotation is not built: to replace a client's key, revoke the old thumbprint
and register the new one.

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
| Stolen tool token presented with no proof | refused at tool: key-bound and no DPoP proof |
| Stolen tool token, thief signs with its own key | refused at tool: proof key does not match cnf.jkt |
| Stolen token and its captured proof replayed | refused at tool: proof jti already seen |
| Proof minted for another tool's URI | refused at tool: htu does not match |
| Proof older than 60 s | refused at tool: too old |
| Exchange with no proof | refused at STS: invalid_dpop_proof |
| Gateway's actor token exchanged under another client's key | refused at STS: actor_token is bound to another key |
| ops-console's bound token sent as a plain Bearer | HTTP 401 invalid_dpop_proof |
| ops-console revokes the sub-agent's key | every token bound to it refused at tool; its handoff exchange and new actor tokens refused at STS; the gateway's tokens untouched |

## Not covered here

- `delegate_to` is a non-standard parameter; RFC 8693 leaves how `may_act` is set to the issuer.
- A token can still be presented more than once within its lifetime by its holder; the replay cache is for proofs, not tokens, and a holder minting fresh proofs is the normal case.
- Revocation is in process only. A tool server that does not share the denylist, or has no checker configured, does not see revocations; the pushed deny list across PEPs and the central audit sink are not built. A Keycloak-issued user token revoked at Keycloak is not revoked here (see the caveat above).
- Tool reports are trusted as far as the escalation policy allows and no further; nothing checks that the reported anomaly was real. The policy is static and global, not per tool or per grant.
- The ops-console is a client secret like every other agent; an admin SVID is the target, not built.
- Sender constraint is DPoP to a software key in the agent's process (see the limit below); mTLS-bound tokens (RFC 8705) wait for SVIDs. Workload identity is prototype B.
- DPoP server nonces are not implemented; key rotation for a client is not built (revoke the key, register a new thumbprint).
- Keycloak hop 1 follows a preview feature; nested delegation in Keycloak is untested.
