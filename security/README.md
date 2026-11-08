# security/ - identity and authorization for agents

Prototypes for how agents act on a user's behalf: who is acting, for whom,
under which grant, for how long, and what stops them.

- `delegation/` - A: RFC 8693 token exchange with a nested `act` chain (user > gateway > sub-agent), short-lived audience-scoped tokens, every token DPoP-bound (RFC 9449) to the key of the client that presents it with a proof on every call, lineage claims and RFC 7009 revocation that follows the chain (in-memory denylist) plus key revocation, tool-server reports with an STS-owned escalation policy, admin routes behind an `sts:admin` actor token, and a tool-side verifier that checks the proof for a bound token, rejects any token without `act` and, when it shares the denylist, any revoked token. Keycloak 26.8 setup for the first hop.
- `policy/` - C: AuthZEN-shaped policy check (OPA) before every MCP `tools/call`. The whole chain is evaluated once, access only narrows, write-class tools need the user's approval for the exact call (on by default, never waivable for high-risk tools), voice requests also need a strong speaker match, home-network origin and a passed liveness check, and any missing signal denies. Every decision is logged with its reasons.
- `THREAT_MODEL.md` - E: 22 threats (15 for the prototypes, 7 for the orchestrator's voice gate), the control for each, whether it was tested, and the residual risk.
- `DIAGRAMS.md` - current state and target state for attestation, token exchange and revocation, with each gap tied to its threat.
- `DEVICE_IDENTITY.md` - the speaker's device identity. Current state: a hardware-held key on a plugged-in YubiKey, a boot-time challenge, a short-lived key-bound credential and revocable enrollment, which proves which key is present but not what software is running. Future state: a TPM 2.0 on the next carrier-board revision.
- `SESSION_BINDING.md` - device-bound sessions and voice enrollment. The credential rides on the Converse stream, the voice gate takes the device as its origin signal, user tokens are bound to the device key with the orchestrator asserting the thumbprint at the exchange, every decision carries an assurance record, and enrollment is an admin-run, device-bound, measured session. The reasoning, four flows, failure modes and limits.
- `VOICE_ENROLLMENT.md` - the enrollment procedure, the acceptance criteria (clip quality, prompt match, consistency, separation, per-user threshold) and the record each profile carries.

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

## Attestation, in short

- Today the speaker is trusted by network origin (gRPC peer on a trusted subnet) plus voice match. That is location plus voice, not device identity.
- Speaker (`DEVICE_IDENTITY.md`): a non-extractable P-256 key in PIV slot 9a of a YubiKey 5 plugged into the CM5 answers a boot-time challenge and gets a 10 minute credential carrying the key thumbprint in `cnf`. Enrollment is a host-side admin step and is revocable. With v2.0 (`SESSION_BINDING.md`) the speaker sends the credential on every connect, the orchestrator can refuse a stream without one, the gate takes it as the origin signal, and the user token for the session is bound to the device key. Built with tests on both sides; the enforcement switches are off by default until every speaker attests.
- What it does not do: measure the software. No PCRs, no measured boot. A stolen key plugged into another host passes, which is why key-bound tokens stay at low-risk scopes and high-risk actions keep voice plus challenge.
- Future state: a TPM 2.0 on the PCB of the next carrier-board revision, over SPI. It gives measured boot and the full attestation chain.
- Agents: still client secrets at the STS, paired with a per-client P-256 key that every token is bound to (DPoP, RFC 9449), so a copied token is useless without the key. The key lives in the agent's process memory, which stops theft of the token but not compromise of the process; workload identity (SPIFFE, prototype B) is not built. The user token is the one leg that reaches hardware: bound to the speaker's key, with the orchestrator asserting the thumbprint at the exchange.
