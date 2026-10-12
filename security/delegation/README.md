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
python demo.py          # chain plus attack table, no servers needed
python -m pytest -q     # 17 tests
python sts.py           # optional: STS on 127.0.0.1:8099, dev secrets printed to stderr
```

## Files

- `sts.py` - the STS: client credentials for actor tokens, token exchange with the delegation rules, JWKS, audit log of every decision with its reason. Dev only: key and client secrets live in memory.
- `verify.py` - the upstream check a tool server runs. Accepts tokens from this STS (key) or from Keycloak (JWKS URL).
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

## Not covered here

- `delegate_to` is a non-standard parameter; RFC 8693 leaves how `may_act` is set to the issuer.
- No token revocation or replay cache: short lifetimes are the control. Revoking the user's token does not revoke exchanged tokens.
- No sender constraint (DPoP or mTLS-bound tokens); a stolen tool token works until it expires. Workload identity is prototype B.
- Keycloak hop 1 follows a preview feature; nested delegation in Keycloak is untested.
