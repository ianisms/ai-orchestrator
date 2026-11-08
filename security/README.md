# security/ - identity and authorization for agents

Prototypes for how agents act on a user's behalf: who is acting, for whom,
under which grant, for how long, and what stops them.

- `delegation/` - A: RFC 8693 token exchange with a nested `act` chain (user > gateway > sub-agent), short-lived audience-scoped tokens, every token DPoP-bound (RFC 9449) to the key of the client that presents it with a proof on every call, lineage claims and RFC 7009 revocation that follows the chain (in-memory denylist) plus key revocation, tool-server reports with an STS-owned escalation policy, admin routes behind an `sts:admin` actor token, and a tool-side verifier that checks the proof for a bound token, rejects any token without `act` and, when it shares the denylist, any revoked token. Keycloak 26.8 setup for the first hop.
- `policy/` - C: AuthZEN-shaped policy check (OPA) before every MCP `tools/call`. The whole chain is evaluated once, access only narrows, write-class tools need the user's approval for the exact call (on by default, never waivable for high-risk tools), voice requests also need a strong speaker match, home-network origin and a passed liveness check, and any missing signal denies. Every decision is logged with its reasons.
- `THREAT_MODEL.md` - E: 22 threats (15 for the prototypes, 7 for the orchestrator's voice gate), the control for each, whether it was tested, and the residual risk.
- `DEVICE_IDENTITY.md` - the speaker's device identity. Current state: a hardware-held key on a plugged-in YubiKey, a boot-time challenge, a short-lived key-bound credential and revocable enrollment, which proves which key is present but not what software is running. Future state: a TPM 2.0 on the next carrier-board revision.
- `SESSION_BINDING.md` - device-bound sessions and voice enrollment: the credential on the Converse stream, the device as the gate's origin signal, user tokens bound to the device key with the orchestrator asserting the thumbprint at the exchange, an assurance record on every decision, and enrollment as an admin-run, device-bound, measured session. Reasoning, flows, failure modes, limits.
- `VOICE_ENROLLMENT.md` - the enrollment procedure, criteria (clip quality, prompt match, consistency, separation, per-user threshold) and the record each profile carries.

Status words used in these docs: built means it runs with tests; partial
means it runs with a documented gap; not built means design only.

## Run

```
cd delegation && pip install -r requirements.txt
python demo.py && python -m pytest -q          # 87 tests
cd ../policy                                   # needs the opa binary on PATH or OPA_BIN
opa test rego && python demo.py && python -m pytest -q   # 22 policy tests, 16 Python tests
```

CI (`.github/workflows/tests.yml`) runs these suites, the voice gate, enrollment and script tests in `server/`, and the device identity and stream metadata tests in `server/` and `speaker/` (with an in-memory signer, no YubiKey) on every pull request. OPA is installed in CI so the policy tests run instead of skipping. `tests` is a required check on `main`.

## Current state

Prototypes, not production: keys and secrets live in memory, and the consent and approval channels are simulated. `delegation/` and `policy/` run on their own with tests and are not wired into the orchestrator.

The voice rules are the exception. `server/modules/orchestrator/core/voice_gate.py` applies the same tiers inside the live orchestrator, in Python rather than OPA. The orchestrator identifies the speaker for each turn itself, never the model; origin comes from the verified device credential on the stream (or, until `VOICE_GATE_REQUIRE_DEVICE` is on, the gRPC peer address), never from headers; each enrolled person has a threshold fixed at enrollment, floored by the global one; and a stated name identifies no one. Every decision carries an assurance record of what was measured. It ships in monitor mode, logging `allow` or `would_deny` for every non-read tool call, because the liveness check and the spoken and phone approval flows are not built yet. Enforce mode before they land would block every write.

## Where each piece lives

| Piece | Status | Where | Threats |
|---|---|---|---|
| Network-origin check for the speaker | built | `server/modules/orchestrator/core/voice_gate.py` | 18 |
| Speaker identification on the turn's audio | built | `server/modules/orchestrator/core/audio_tap.py`, identity tool | 16 |
| Liveness check | not built | rollout step 3 | 15, 21 |
| Spoken confirmation and phone approval | not built | rollout step 2 | 17, 19 |
| Hardware device key, enrollment registry, credential on the stream, gate origin from the device, user token bound to the device key | built (YubiKey; TPM on the carrier board not built; mTLS from device not taken, see `SESSION_BINDING.md`, "Bind per connection, not per request") | `DEVICE_IDENTITY.md`, `SESSION_BINDING.md` | 18, 1 |
| Admin-run voice enrollment with per-user thresholds | built | `VOICE_ENROLLMENT.md` | 17, 15 |
| Client-secret actor credentials | built (prototype) | `security/delegation/sts.py` | 6 |
| SPIFFE/SPIRE workload identity | not built (prototype B) | "Attestation, future state" | 6, 13 |
| RFC 8693 exchange with nested `act` chain | built | `security/delegation/sts.py` | 2, 3, 4, 5 |
| Tool-side verifier | built | `security/delegation/verify.py` | 5, 10 |
| AuthZEN-shaped PEP and OPA PDP, fail closed | built | `security/policy/` | 7, 8, 9 |
| Sender-constrained tokens (DPoP, RFC 9449) | built (prototype, partial): software key in process, no nonces, no rotation; mTLS-bound waits for SVIDs | `security/delegation/dpop.py`, `sts.py`, `verify.py` | 1, 6 |
| Lineage claims, RFC 7009 `/revoke` with subtree extension, in-memory denylist, verify-side check | built (prototype) | `security/delegation/sts.py`, `revocation.py`, `verify.py` | 11 |
| Tool reports with STS-owned escalation (token, delegation, grant) | built (prototype) | `security/delegation/sts.py: _escalate`, `revocation.EscalationPolicy` | 11, 7 |
| Admin revocation routes behind an `sts:admin` actor token | built (prototype) | `security/delegation/sts.py: authorize_admin`, `admin.py` | 11 |
| Revocation beyond lifetimes | partial | "Revocation, current and future state"; key revocation built (`sts.py: revoke_key`); pushed deny list across PEPs, SVID revocation and an admin SVID not built | 11 |
| Append-only audit sink | not built | next controls item 5 | 12 |

## Attestation, in short

- Speaker (`DEVICE_IDENTITY.md`, `SESSION_BINDING.md`): a non-extractable P-256 key in PIV slot 9a of a YubiKey 5 on the CM5 answers a boot-time challenge and gets a 10 minute credential with the key thumbprint in `cnf`. The credential rides on every connect, the orchestrator can refuse a stream without one, the gate can take it as the origin signal, and the session's user token is bound to the device key. Enrollment is a host-side admin step and is revocable. Built with tests on both sides; the enforcement switches are off until every speaker attests.
- What it does not do: measure the software. No PCRs, no measured boot. A stolen key on another host passes, which is why key-bound tokens stay at low-risk scopes and high-risk actions keep voice plus a step-up.
- Future state: a TPM 2.0 on the PCB of the next carrier-board revision, over SPI, for measured boot and the full attestation chain.
- Agents: client secrets at the STS, each paired with a P-256 key that every token is bound to (DPoP, RFC 9449), so a copied token is useless without the key. The key lives in the agent's process, which stops theft of the token but not compromise of the process. Workload identity (SPIFFE, prototype B) is not built.
