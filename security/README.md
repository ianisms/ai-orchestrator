# security/ - identity and authorization for agents

Prototypes for how agents act on a user's behalf: who is acting, for whom,
under which grant, for how long, and what stops them.

- `delegation/` - A: RFC 8693 token exchange with a nested `act` chain (user > gateway > sub-agent), short-lived audience-scoped tokens, and a tool-side verifier. Keycloak 26.8 setup for the first hop.
- `policy/` - C: AuthZEN-shaped policy check (OPA) before every MCP `tools/call`, whole chain evaluated once, access only narrows, fail closed, every decision logged with reasons.
- `THREAT_MODEL.md` - E: threats, controls, what was tested, residual risk.

Prototypes, not production: keys and secrets live in memory, and the consent and approval channels are simulated.
