# security/ - identity and authorization for agents

Prototypes for how agents act on a user's behalf: who is acting, for whom,
under which grant, for how long, and what stops them.

- `delegation/` - A: RFC 8693 token exchange with a nested `act` chain (user > gateway > sub-agent), short-lived audience-scoped tokens, and a tool-side verifier that rejects any token without `act`. Keycloak 26.8 setup for the first hop.
- `policy/` - C: AuthZEN-shaped policy check (OPA) before every MCP `tools/call`. The whole chain is evaluated once, access only narrows, write-class tools need the user's approval for the exact call (on by default, never waivable for high-risk tools), voice requests also need a strong speaker match, home-network origin and a passed liveness check, and any missing signal denies. Every decision is logged with its reasons.
- `THREAT_MODEL.md` - E: 22 threats (15 for the prototypes, 7 for the orchestrator's voice gate), the control for each, whether it was tested, and the residual risk.

## Run

```
cd delegation && pip install -r requirements.txt
python demo.py && python -m pytest -q          # 17 tests
cd ../policy                                   # needs the opa binary on PATH or OPA_BIN
opa test rego && python demo.py && python -m pytest -q   # 22 policy tests, 16 Python tests
```

CI (`.github/workflows/tests.yml`) runs these suites and the voice gate tests in `server/` on every pull request. OPA is installed in CI so the policy tests run instead of skipping. `tests` is a required check on `main`.

## Status

Prototypes, not production: keys and secrets live in memory, and the consent and approval channels are simulated. `delegation/` and `policy/` run on their own with tests and are not wired into the orchestrator.

The voice rules are the exception. `server/modules/orchestrator/core/voice_gate.py` applies the same tiers inside the live orchestrator, in Python rather than OPA. The orchestrator identifies the speaker for each turn itself, never the model; network origin comes from the gRPC peer address, never from headers; and a stated name no longer identifies anyone. It ships in monitor mode, logging `allow` or `would_deny` for every non-read tool call, because the liveness check and the spoken and phone approval flows are not built yet. Enforce mode before they land would block every write.
