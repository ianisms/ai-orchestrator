# Threat model: agent delegation path (prototypes A and C)

Path: user consents at the STS > gateway exchanges for a tool token or a handoff >
sub-agent exchanges the handoff > tool server verifies the token > PEP asks the PDP > tool runs > output returns to the model.

Scope: the prototypes in delegation/ and policy/ (threats 1-15), and the voice gate in the live orchestrator (threats 16-22). The live assistant does not use the delegation and policy prototypes yet.

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
| 15 | Voice spoofing: replayed recording, cloned voice, request from outside the home | Voice requests need a match of 0.98 or better, home-network origin and a passed liveness check; missing signals fail closed; writes confirm by default and high-risk tools always confirm | Yes: low match, failed liveness, off-network and missing signals all denied; high-risk merge denied with confirmations off | The policy trusts the front end's liveness result; a real-time voice clone that passes liveness still gets read access, so writes keep the confirmation. The orchestrator's voice gate (16-22) gathers these signals itself; liveness is not built yet |

## Voice gate in the live orchestrator (server/)

Path: speaker audio > orchestrator commits the turn > model requests a tool > gate decides > tool runs.

Mode: monitor. Every non-read decision is logged as `allow` or `would_deny`; nothing is blocked until the liveness check and the approval flows exist.

| # | Threat | Control | Tested | Residual risk |
|---|---|---|---|---|
| 16 | Model-driven identity: a steered model claims or picks who is speaking | The orchestrator identifies the speaker from the turn's own audio through `identify_speaker`, which is hidden from the model; the model supplies no input to the decision | Partly: decision logic unit tested; the hidden-tool wiring is reviewed, not tested | `get_person_id` still gives the model a session-scoped identity for per-person reads, and reads are not gated |
| 17 | Identity takeover by claiming a name ("I'm Alice") | A stated name records a pending enrollment only; it never joins a profile or sets the session identity; approval is out of band (`scripts/approve_enrollment.py`) | Partly: approval script tested; tool path reviewed | Approval is a host-side script until the phone flow exists |
| 18 | Spoofed origin through client-asserted headers | Trusted speaker addresses are matched against the gRPC peer address; a request relayed through a proxy arrives from the proxy's address and is not trusted unless that address is configured | Yes: untrusted and malformed addresses fail | IPv4-mapped IPv6 peers need that form configured; a compromised device on a trusted address passes this check |
| 19 | Lock, garage or alarm operated as an ordinary write, or through the automatic " Lock" retry | Home Assistant on/off calls naming a lock, garage, door or alarm (name, area or device class) are high risk; the retry gets its own decision | Partly: classification tested; retry wiring reviewed | A device named without those words is classified as a write |
| 20 | New or unknown tool treated as read | Unlisted tools default to write; read-only actions on write tools are listed explicitly | Yes | A tool misclassified as read in config is not gated |
| 21 | Gate failure or misconfiguration | Gate errors fail closed in enforce; an unknown mode falls back to monitor, never off; an unset threshold verifies no voice; any missing signal fails | Partly: mode, threshold and missing-signal cases tested; error handling reviewed | Monitor mode blocks nothing by design until rollout |
| 22 | Path injection through the client-supplied session id | Session and audio ids are validated to a plain token before they reach a file path | No | Sessions are still keyed by a client-supplied id |

Not gated yet: explicit location-preference commands are handled before the model and do not pass the gate. 38 voice gate tests (`server/test/test_voice_gate.py`, `server/test/test_voice_scripts.py`) run in CI.

## Attacks run (demo.py in delegation/ and policy/)

Expired-token replay; exchange without `may_act`; another agent presenting the user's token; sub-agent asking for write scope; sub-agent asking for an unregistered tool; plain exchange to strip `act`; borrowed actor token; handoff token sent to a tool; injected merge via sub-agent; injected merge via gateway; out-of-session read; approval replay and reuse on a different PR; token without `act`; forged signature; OPA down; PDP timeout; voice match below threshold; replayed voice (liveness fail); off-network voice; voice with no signals; high-risk voice write with confirmations off. All refused, each with a logged reason. 33 Python tests and 22 policy tests pass.

## Next controls, in order

1. Sender-constrained tokens (DPoP, or mTLS-bound with B's SVIDs) to close #1.
2. Workload identity for agents instead of client secrets (B) to close #6 and #13.
3. A real approval channel with the call summary shown to the user (#7).
4. Device posture as a signed policy input for write-class tools (D).
5. Central, append-only audit sink (#12).

## Voice gate rollout, in order

1. Calibrate the match threshold from enrolled voices (`scripts/calibrate_voice_threshold.py`), then check the logged scores from real turns in monitor mode.
2. Spoken confirmation for writes and phone approval through the Home Assistant app for high-risk calls and enrollment (#17, #19).
3. A spoken liveness challenge, then an anti-spoof model (#15).
4. Switch to enforce.
5. Gate the direct location-preference commands and move identity for per-person reads onto the gate (#16).
