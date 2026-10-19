# security/ - identity and authorization for agents

Prototypes for how agents act on a user's behalf: who is acting, for whom,
under which grant, for how long, and what stops them.

- `delegation/` - A: RFC 8693 token exchange with a nested `act` chain (user > gateway > sub-agent), short-lived audience-scoped tokens, every token DPoP-bound (RFC 9449) to the key of the client that presents it with a proof on every call, lineage claims and RFC 7009 revocation that follows the chain (in-memory denylist) plus key revocation, tool-server reports with an STS-owned escalation policy, admin routes behind an `sts:admin` actor token, and a tool-side verifier that checks the proof for a bound token, rejects any token without `act` and, when it shares the denylist, any revoked token. Keycloak 26.8 setup for the first hop.
- `policy/` - C: AuthZEN-shaped policy check (OPA) before every MCP `tools/call`. The whole chain is evaluated once, access only narrows, write-class tools need the user's approval for the exact call (on by default, never waivable for high-risk tools), voice requests also need a strong speaker match, home-network origin and a passed liveness check, and any missing signal denies. Every decision is logged with its reasons.
- `THREAT_MODEL.md` - E: 22 threats (15 for the prototypes, 7 for the orchestrator's voice gate), the control for each, whether it was tested, and the residual risk.
- `DIAGRAMS.md` - current state and target state for attestation, token exchange and revocation, with each gap tied to its threat.
- `DEVICE_IDENTITY.md` - the speaker's device identity: today (network origin plus voice), this change (a hardware-held key on a plugged-in YubiKey, boot-time challenge, short-lived key-bound credential, revocable enrollment), what it does not prove (no measured boot, so device identity and not attestation), and the target (a secure element or SPI TPM on the next carrier-board revision).

## Run

```
cd delegation && pip install -r requirements.txt
python demo.py && python -m pytest -q          # 71 tests
cd ../policy                                   # needs the opa binary on PATH or OPA_BIN
opa test rego && python demo.py && python -m pytest -q   # 22 policy tests, 16 Python tests
```

CI (`.github/workflows/tests.yml`) runs these suites, the voice gate tests in `server/`, and the device identity tests in `server/` and `speaker/` (with an in-memory signer, no YubiKey) on every pull request. OPA is installed in CI so the policy tests run instead of skipping. `tests` is a required check on `main`.

## Status

Prototypes, not production: keys and secrets live in memory, and the consent and approval channels are simulated. `delegation/` and `policy/` run on their own with tests and are not wired into the orchestrator.

The voice rules are the exception. `server/modules/orchestrator/core/voice_gate.py` applies the same tiers inside the live orchestrator, in Python rather than OPA. The orchestrator identifies the speaker for each turn itself, never the model; network origin comes from the gRPC peer address, never from headers; and a stated name no longer identifies anyone. It ships in monitor mode, logging `allow` or `would_deny` for every non-read tool call, because the liveness check and the spoken and phone approval flows are not built yet. Enforce mode before they land would block every write.

## Attestation, in short

- Today the speaker is trusted by network origin (gRPC peer on a trusted subnet) plus voice match. That is location plus voice, not device identity.
- With the device identity change (`DEVICE_IDENTITY.md`), a non-extractable P-256 key in PIV slot 9a of a YubiKey 5 plugged into the CM5 answers a boot-time challenge and gets a 10 minute credential carrying the key thumbprint in `cnf`. Enrollment is a host-side admin step and is revocable. The orchestrator verifies and logs the credential at connect; the speaker does not attach it to the stream yet and nothing is gated on it. Built with tests on both sides, off by default.
- What it does not do: measure the software. No PCRs, no measured boot. A stolen key plugged into another host passes, which is why key-bound tokens stay at low-risk scopes and high-risk actions keep voice plus challenge.
- Target: a secure element (ATECC608 over I2C) or an SPI TPM footprint on the next carrier-board revision. The TPM variant gives measured boot and the full attestation chain. A header-mounted TPM 2.0 module on the CM5 would also work as a stopgap; I am not taking that path.
- Agents: still client secrets at the STS, now paired with a per-client P-256 key that every token is bound to (DPoP, RFC 9449), so a copied token is useless without the key. The key lives in the agent's process memory, which stops theft of the token but not compromise of the process; workload identity (SPIFFE, prototype B) is not built.
