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
python -m pytest -q     # 32 tests
python sts.py           # optional: STS on 127.0.0.1:8099; set STS_CLIENT_MCP_GATEWAY etc. first (secrets are never printed)
```

## Files

- `sts.py` - the STS: client credentials for actor tokens, token exchange with the delegation rules, JWKS, audit log of every decision with its reason. Dev only: key and client secrets live in memory.
- `verify.py` - the upstream check a tool server runs. Accepts tokens from this STS (key) or from Keycloak (JWKS URL). Optional `revocation=` hook, see below.
- `revocation.py` - the in-memory denylist (jti and grant_id with expiries) and the STS's record of issued tokens, used to walk a token's descendants.
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
refuses it new actor tokens and exchanges until `enable_client`. These are
methods, not HTTP routes, because the prototype has no admin authentication.

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
| Admin retires an agent | its live tokens refused at tool; no new actor tokens |

## Not covered here

- `delegate_to` is a non-standard parameter; RFC 8693 leaves how `may_act` is set to the issuer.
- No replay cache: within its lifetime a token can be presented more than once.
- Revocation is in process only. A tool server that does not share the denylist, or has no checker configured, does not see revocations; the pushed deny list across PEPs and the central audit sink are not built. A Keycloak-issued user token revoked at Keycloak is not revoked here (see the caveat above).
- No sender constraint (DPoP or mTLS-bound tokens); a stolen tool token works until it expires. Workload identity is prototype B.
- Keycloak hop 1 follows a preview feature; nested delegation in Keycloak is untested.
