# Keycloak 26.8 hop 1 (timebox: 45 minutes)

Goal: Keycloak issues the user > mcp-gateway delegated token, and the same
`UpstreamVerifier` from `verify.py` accepts it through Keycloak's JWKS.
The nested sub-agent hop stays in the Python STS. Whether Keycloak's delegation
exchange accepts a subject token that already carries `act` is untested; record
the result either way, it is a useful line in the threat model.

Delegation is a preview feature in 26.8 (off by default, not for production).
Setup below follows the Keycloak 26.8 token exchange guide as summarized by
Skycloak (2 Oct 2026); confirm names in the admin console as you go.

## Setup

1. `cp .env.example .env`, set a password, `docker compose up -d`, open http://127.0.0.1:8080.
2. Create realm `agents` and user `alice` with a password.
3. Client `mcp-gateway`: confidential, Service accounts ON, Standard Token Exchange ON.
   Give it only the client scopes and audience mappers for the tools it should reach.
4. Client `login-app` (public, PKCE, redirect `http://127.0.0.1:8765/*`): Consent required ON.
5. Realm settings > Admin Permissions: enable (FGAP V2).
6. Permissions: on the Users resource type, scope `delegate`, all users, Client policy selecting `mcp-gateway`.
   Without this the delegation scope is silently dropped (logged at WARN) and login still succeeds.
7. Optional: Client policies > `may-act` executor; "Reject any may_act claim" on clients outside the agent flow.

## Run hop 1

1. Sign in as alice through `login-app` with `scope=openid delegation:client:mcp-gateway`
   (authorization code + PKCE in a browser; the consent screen is the point).
   Check the access token: `may_act` names mcp-gateway and `aud` includes it.
2. Actor token:
   `POST /realms/agents/protocol/openid-connect/token` with
   `grant_type=client_credentials&client_id=mcp-gateway&client_secret=...`
3. Exchange:
   `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`,
   `client_id`, `client_secret`, `subject_token=<alice>`, `subject_token_type=urn:ietf:params:oauth:token-type:access_token`,
   `actor_token=<gateway>`, `actor_token_type=urn:ietf:params:oauth:token-type:access_token`.
   Expect an access token only (no refresh token) with `act.client_id = mcp-gateway`.
4. Verify:
   ```python
   from verify import UpstreamVerifier
   v = UpstreamVerifier("http://127.0.0.1:8080/realms/agents", "<tool audience>",
                        jwks_url="http://127.0.0.1:8080/realms/agents/protocol/openid-connect/certs")
   print(v.verify(token).path)
   ```

## Checks worth recording
- Same exchange without `actor_token`, subject = the delegated token: should be refused (26.8 hardening).
- Delegation exchange where the subject already carries `act` (the nested hop): result unknown.
- Revoking alice's token does not revoke exchanged tokens; keep access token lifetimes short.

Troubleshooting: no `may_act` = missing `delegate` permission, misspelled client, or no service account.
`invalid_request` on exchange = actor does not match `may_act`. `access_denied` = requesting client outside a token's audience.
