# Prototype C: policy check before every tool call

The MCP server verifies the delegated token (prototype A), then asks a policy
decision point about the call before dispatching it. The request uses the
OpenID AuthZEN evaluation shape (subject, action, resource, context), and the
PDP is a small facade over OPA. The whole delegation chain is evaluated once.

```
tools/call --> verify token (A) --> PEP builds AuthZEN request --> PDP /access/v1/evaluation --> OPA
                                        |                                   |
                                   fail closed on                   allow only when no
                              timeout, 5xx, bad answer               reason to deny exists
```

## What the policy enforces (`rego/agent_tools.rego`)

- Every actor in the chain must be granted the tool, so access only narrows.
- The token must hold the tool's scope.
- Write-class tools need the user's approval for that exact call (tool plus arguments), single use, from a channel the model cannot write to.
- The resource must be in the session's resource list.
- The chain needs at least one actor and at most two; the token must be fresh.
- Unknown tools and unknown request channels are denied.
- Confirmation is on by default. A user can turn it off for ordinary writes; high-risk tools (merge) always need it.
- Voice requests also need a speaker match of 0.98 or better, origin on the home network, and a passed liveness check. These signals come from the speaker front end, never from the model.
- Fails closed: any missing signal counts as a failed check.

## Run

Needs the OPA binary (Windows: `opa_windows_amd64.exe` from the OPA GitHub releases, renamed `opa.exe` on PATH, or set `OPA_BIN`).

```
pip install -r ../delegation/requirements.txt
opa test rego -v        # 22 policy tests
python demo.py          # A and C end to end: prompt-injection scenario plus fail-closed cases
python -m pytest -q     # 16 tests, skipped if opa is missing
```

## Demo result

| Call | Result |
|---|---|
| Sub-agent reads PR 42 (the real task) | allowed |
| Injected: sub-agent merges PR 42 | denied: sub-agent not granted merge; token lacks repo:write; no approval |
| Injected: gateway merges PR 42 (holds repo:write) | denied: needs the user's approval for this exact call |
| Injected: gateway reads acme/secrets | denied: outside this session |
| User approves that exact merge, gateway retries | allowed |
| Same approved merge replayed | denied |
| Approval for PR 42 used on PR 43 | denied |
| Token with no act chain | rejected before policy |
| Voice: all checks pass, read | allowed |
| Voice: match at 0.91 | denied |
| Voice: replayed recording (liveness fails) | denied |
| Voice: request from off the home network | denied |
| Voice: front end sent no signals | denied on all three checks |
| Voice: merge with confirmations turned off | denied: high-risk always confirms |
| Voice: comment with confirmations turned off | allowed |
| Policy engine down | denied, fail closed |
| PDP timeout | denied, fail closed |

## Not covered here

- The approval store is in memory and the approval channel is simulated. In the real gateway, approval has to come from a UI the model cannot drive.
- Session resources are set by the caller of the PEP; in the real gateway they come from the user's task, not from the model.
- Policy and data are loaded at OPA start; no bundle signing or versioned rollout.
- Liveness is an input here, not an implementation: the policy trusts the front end's result. How good the liveness check is decides how much the voice path is worth.
- Tool output is not inspected. A read the policy allows can still return injected text; the defense is that the next call goes through the same check.
