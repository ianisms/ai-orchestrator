# Threat model: agent delegation path (prototypes A and C)

Path: user consents at the STS > gateway exchanges for a tool token or a handoff >
sub-agent exchanges the handoff > tool server verifies the token > PEP asks the PDP > tool runs > output returns to the model.

Assume breach: any agent can be steered by text it reads, and any token an agent holds can leak.
Goal: a steered or compromised agent can do no more than the user approved, for no longer than a few minutes, and every attempt leaves a record.

| # | Threat | Control | Tested | Residual risk |
|---|---|---|---|---|
| 1 | Replay of an expired or stolen tool token | Tool tokens live 120-300 s, no refresh; tool rejects expired tokens; token age checked again in policy | Yes: expired replay refused at tool | Within its lifetime a stolen token works; no sender constraint (DPoP or mTLS binding) yet |
| 2 | Agent acts without the user's consent | Exchange requires `may_act` naming this actor, and `aud` naming it | Yes: no-consent exchange refused; another agent presenting the user's token refused | Consent screen is simulated in the Python STS; Keycloak hop 1 has the real one |
| 3 | Confused deputy: sub-agent gets more than its parent | Scope must be held by the subject and registered for the actor; audience must be registered; policy requires every actor in the chain to hold the tool | Yes: sub-agent asking for repo:write and for an unregistered tool refused at STS; merge refused by policy | Registration data is static; a wrong grant in config is a wrong grant everywhere |
| 4 | Scope or audience expansion through chained exchanges | Each hop intersects scope; handoff scope capped by the next actor's registration; depth capped at 2, counted at handoff | Yes: over-wide handoff and a third hop refused | Depth cap is a blunt limit; no per-chain policy beyond it |
| 5 | Act-chain stripping | Plain exchange of a token carrying `act` or `may_act` refused; tool rejects tokens without `act`; handoff tokens are addressed to an agent, never a tool | Yes: all three refused | None known in the prototype |
| 6 | Actor impersonation | Actor token must belong to the authenticated client; `may_act.sub` and `client_id` must match it | Yes: gateway using another agent's actor token refused | Client secrets; workload identity (SPIFFE, prototype B) replaces them |
| 7 | Prompt injection drives a write | Write-class tools need the user's approval for that exact call, single use; sub-agents are not granted writes | Yes: injected merge refused via sub-agent and via gateway; approval bound to arguments and not replayable | Approval UX: a user who approves everything defeats it; channel simulated |
| 8 | Exfiltration through a read tool | Resource must be in the session's list | Partly: read of an out-of-session repo refused | Tool output is not inspected; data the user did allow can still flow back to the model |
| 9 | PDP outage or slow PDP | PEP fails closed on timeout, 5xx and malformed answers; 500 ms budget | Yes: OPA down and PDP timeout both denied and logged | Availability cost: outage stops all tool calls, by design |
| 10 | Forged tokens | RS256 signature checked against the STS key or JWKS; issuer and audience checked | Yes: token signed by another key refused | Key held in memory in the prototype; no rotation |
| 11 | Revocation gap | Short lifetimes | No | Revoking the user's token does not revoke exchanged tokens (same as Keycloak 26.8) |
| 12 | Audit gaps | Every exchange and every tool decision logged with path, decision and reasons | Yes: counts checked in tests | Logs are in process; no tamper evidence or central sink |
| 13 | Stolen workload credential, Docker socket exposure | Not built (prototype B) | No | SPIRE's Docker attestor needs the Docker socket and pid: host, which is root-equivalent |
| 14 | Device posture spoofing; device-code phishing on headless agents | Not built (prototype D) | No | Posture would be a signed, short-lived input to the same policy |

## Attacks run (demo.py in delegation/ and policy/)

Expired-token replay; exchange without `may_act`; another agent presenting the user's token; sub-agent asking for write scope; sub-agent asking for an unregistered tool; plain exchange to strip `act`; borrowed actor token; handoff token sent to a tool; injected merge via sub-agent; injected merge via gateway; out-of-session read; approval replay and reuse on a different PR; token without `act`; forged signature; OPA down; PDP timeout. All refused, each with a logged reason. 26 Python tests and 9 policy tests pass.

## Next controls, in order

1. Sender-constrained tokens (DPoP, or mTLS-bound with B's SVIDs) to close #1.
2. Workload identity for agents instead of client secrets (B) to close #6 and #13.
3. A real approval channel with the call summary shown to the user (#7).
4. Device posture as a signed policy input for write-class tools (D).
5. Central, append-only audit sink (#12).
