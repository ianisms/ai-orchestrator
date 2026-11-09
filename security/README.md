# security/ - identity and authorization for agents

Prototypes for how agents act on a user's behalf. They answer who is acting,
for whom, under which grant, for how long, and what stops them.

| Doc | What it covers |
|---|---|
| [`delegation/`](delegation/README.md) | RFC 8693 token exchange with a nested `act` chain (user > gateway > sub-agent) and short-lived, audience-scoped tokens. Every token is bound to the key of the client that presents it (DPoP, RFC 9449). Lineage claims and RFC 7009 revocation follow the chain. Tool servers report tokens and the STS decides whether a report escalates. Admin routes sit behind an `sts:admin` actor token. The tool-side verifier checks the proof and rejects any token without `act`. [Keycloak 26.8 setup](delegation/keycloak/README.md) covers the first hop. |
| [`policy/`](policy/README.md) | An AuthZEN-shaped policy check in OPA before every MCP `tools/call`. The whole chain is evaluated once and access only narrows. Write-class tools need the user's approval for the exact call, on by default and never waivable for high-risk tools. Voice requests also need a strong speaker match, home-network origin and a passed liveness check. Any missing signal denies. Every decision is logged with its reasons. |
| [`THREAT_MODEL.md`](THREAT_MODEL.md) | 22 threats, 15 for the prototypes and 7 for the orchestrator's voice gate. For each, the control, whether it was tested, and the residual risk. |
| [`DEVICE_IDENTITY.md`](DEVICE_IDENTITY.md) | How the speaker proves which device it is. A key in a plugged-in YubiKey answers a boot-time challenge and gets a short-lived credential. Enrollment is revocable. The future state is a TPM 2.0 on the next carrier-board revision. |
| [`SESSION_BINDING.md`](SESSION_BINDING.md) | What the session does with the device identity. The credential on the Converse stream, the device as the gate's origin signal, user tokens bound to the device key, an assurance record on every decision, and enrollment as an admin-run session. Reasoning, flows, failure modes and limits. |
| [`VOICE_ENROLLMENT.md`](VOICE_ENROLLMENT.md) | The enrollment procedure, the criteria (clip quality, prompt match, consistency, separation, per-user threshold) and the record each profile carries. |

## Run

```
cd delegation && pip install -r requirements.txt
python demo.py && python -m pytest -q          # 87 tests
cd ../policy                                   # needs the opa binary on PATH or OPA_BIN
opa test rego && python demo.py && python -m pytest -q   # 22 policy tests, 16 Python tests
```

CI ([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)) runs these suites on every pull request. It also runs the voice gate, enrollment and script tests in `server/`. The device identity and stream metadata tests in `server/` and `speaker/` run with an in-memory signer and no YubiKey. OPA is installed in CI so the policy tests run instead of skipping. `tests` is a required check on `main`.

## Current state

These are prototypes and are not production code. Keys and secrets live in memory, and the consent and approval channels are simulated. `delegation/` and `policy/` run on their own with tests and are not wired into the orchestrator.

The voice rules are the exception. [`server/modules/orchestrator/core/voice_gate.py`](../server/modules/orchestrator/core/voice_gate.py) applies the same tiers inside the live orchestrator, in Python. The orchestrator identifies the speaker for each turn itself, never the model. Origin comes from the verified device credential on the stream, never from headers. Until `VOICE_GATE_REQUIRE_DEVICE` is on, the gRPC peer address stands in for it. Each enrolled person has a threshold fixed at enrollment, floored by the global one. A stated name identifies no one. Every decision carries an assurance record of what was measured. The gate ships in monitor mode and logs `allow` or `would_deny` for every non-read tool call. It stays there because the liveness check and the spoken and phone approval flows are not built yet. Enforce mode before they land would block every write.

## Controls

| Control | Status | Where | Threats |
|---|---|---|---|
| Network-origin check for the speaker | built | [`server/modules/orchestrator/core/voice_gate.py`](../server/modules/orchestrator/core/voice_gate.py) | 18 |
| Speaker identification on the turn's audio | built | [`server/modules/orchestrator/core/audio_tap.py`](../server/modules/orchestrator/core/audio_tap.py), [`server/tools_server/tools/get_person_id/`](../server/tools_server/tools/get_person_id/) | 16 |
| Liveness check | not built | [`security/THREAT_MODEL.md`](THREAT_MODEL.md#voice-gate-rollout) | 15, 21 |
| Spoken confirmation and phone approval | not built | [`security/THREAT_MODEL.md`](THREAT_MODEL.md#voice-gate-rollout) | 17, 19 |
| Hardware device key, enrollment registry, credential on the stream, gate origin from the device, user token bound to the device key | built (YubiKey; TPM on the carrier board not built; mTLS from the device not taken) | [`security/DEVICE_IDENTITY.md`](DEVICE_IDENTITY.md), [`security/SESSION_BINDING.md`](SESSION_BINDING.md) | 18, 1 |
| Admin-run voice enrollment with per-user thresholds | built | [`security/VOICE_ENROLLMENT.md`](VOICE_ENROLLMENT.md) | 17, 15 |
| Client-secret actor credentials | built (prototype) | [`security/delegation/sts.py`](delegation/sts.py) | 6 |
| SPIFFE/SPIRE workload identity | not built | [`security/README.md`](README.md#attestation) | 6, 13 |
| RFC 8693 exchange with nested `act` chain | built | [`security/delegation/sts.py`](delegation/sts.py) | 2, 3, 4, 5 |
| Tool-side verifier | built | [`security/delegation/verify.py`](delegation/verify.py) | 5, 10 |
| Policy check before every tool call: the enforcement point (PEP) in the tool server asks the decision point (PDP) over OPA, fail closed | built | [`security/policy/`](policy/) | 7, 8, 9 |
| Sender-constrained tokens (DPoP, RFC 9449) | built (prototype, partial): software key in process, no nonces, no rotation; mTLS-bound waits for SVIDs | [`security/delegation/dpop.py`](delegation/dpop.py), [`security/delegation/sts.py`](delegation/sts.py), [`security/delegation/verify.py`](delegation/verify.py) | 1, 6 |
| Lineage claims, RFC 7009 `/revoke` with subtree extension, in-memory denylist, verify-side check | built (prototype) | [`security/delegation/sts.py`](delegation/sts.py), [`security/delegation/revocation.py`](delegation/revocation.py), [`security/delegation/verify.py`](delegation/verify.py) | 11 |
| Tool reports with STS-owned escalation (token, delegation, grant) | built (prototype) | [`security/delegation/sts.py`](delegation/sts.py), [`security/delegation/revocation.py`](delegation/revocation.py) | 11, 7 |
| Admin revocation routes behind an `sts:admin` actor token | built (prototype) | [`security/delegation/sts.py`](delegation/sts.py), [`security/delegation/admin.py`](delegation/admin.py) | 11 |
| Revocation beyond lifetimes | partial: key revocation built; pushed deny list across PEPs, SVID revocation and an admin SVID not built | [`security/delegation/README.md`](delegation/README.md#revocation) | 11 |
| Append-only audit sink | not built | [`security/THREAT_MODEL.md`](THREAT_MODEL.md#next-controls) | 12 |

## Attestation

The speaker ([`DEVICE_IDENTITY.md`](DEVICE_IDENTITY.md), [`SESSION_BINDING.md`](SESSION_BINDING.md)) holds a key that cannot leave its YubiKey. The key answers a boot-time challenge and gets a 10 minute credential with the key thumbprint in `cnf`. The credential rides on every connect. The orchestrator can refuse a stream without one. The gate can take it as the origin signal. The session's user token is bound to the device key. Enrollment is a host-side admin step and is revocable. Both sides are built with tests. The enforcement switches are off until every speaker attests.

This does not measure the software. There are no PCRs and no measured boot. A stolen key on another host passes. That is why key-bound tokens stay at low-risk scopes and high-risk actions keep voice plus a step-up. The future state is a TPM 2.0 on the PCB of the next carrier-board revision, over SPI, for measured boot and the full attestation chain.

Agents use client secrets at the STS, each paired with a key that every token is bound to (DPoP, RFC 9449). A copied token is useless without the key. The key lives in the agent's process. That stops theft of the token and does nothing against compromise of the process. Workload identity (SPIFFE) is not built.
