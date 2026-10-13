# security/ - identity and authorization for agents

Prototypes for how agents act on a user's behalf: who is acting, for whom,
under which grant, for how long, and what stops them.

- `delegation/` - A: RFC 8693 token exchange with a nested `act` chain (user > gateway > sub-agent), short-lived audience-scoped tokens, and a tool-side verifier that rejects any token without `act`. Keycloak 26.8 setup for the first hop.
- `policy/` - C: AuthZEN-shaped policy check (OPA) before every MCP `tools/call`. The whole chain is evaluated once, access only narrows, write-class tools need the user's approval for the exact call (on by default, never waivable for high-risk tools), voice requests also need a strong speaker match, home-network origin and a passed liveness check, and any missing signal denies. Every decision is logged with its reasons.
- `THREAT_MODEL.md` - E: 15 threats, the control for each, whether it was tested, and the residual risk.

## Run

```
cd delegation && pip install -r requirements.txt
python demo.py && python -m pytest -q          # 17 tests
cd ../policy                                   # needs the opa binary on PATH or OPA_BIN
opa test rego && python demo.py && python -m pytest -q   # 22 policy tests, 16 Python tests
```

## Status

Prototypes, not production: keys and secrets live in memory, and the consent and approval channels are simulated. They run on their own with tests and are not wired into the orchestrator yet. In particular, the voice rules in `policy/` define what the speaker front end must supply; the live assistant does not send those signals today.
