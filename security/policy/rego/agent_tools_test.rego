package agent.tools_test

import rego.v1

import data.agent.tools

base := {
	"subject": {"type": "user", "id": "user:alice", "properties": {
		"actors": ["mcp-gateway", "code-review-subagent"],
		"scopes": ["repo:read"],
	}},
	"action": {"name": "github.get_pr"},
	"resource": {"type": "repo", "id": "acme/widgets"},
	"context": {"channel": "api", "token_age_s": 5, "user_approved": false, "session_resources": ["acme/widgets"]},
}

gateway_write := object.union(base, {"subject": {"properties": {"actors": ["mcp-gateway"], "scopes": ["repo:read", "repo:write"]}}})

voice_ok := {"confidence": 0.99, "local_network": true, "liveness": true}

with_ctx(inp, extra) := object.union(inp, {"context": extra})

# ---- delegation and scope ---------------------------------------------------

test_subagent_read_allowed if {
	tools.decision.allow with input as base
}

test_subagent_merge_denied_by_chain_and_scope if {
	d := tools.decision with input as object.union(base, {"action": {"name": "github.merge_pr"}})
	not d.allow
	"code-review-subagent is not granted github.merge_pr" in d.reasons
	"token lacks scope repo:write" in d.reasons
}

test_gateway_write_needs_approval if {
	d := tools.decision with input as object.union(gateway_write, {"action": {"name": "github.merge_pr"}})
	d.reasons == ["write-class tool needs the user's approval for this exact call"]
}

test_gateway_write_with_approval_allowed if {
	inp := with_ctx(object.union(gateway_write, {"action": {"name": "github.merge_pr"}}), {"user_approved": true})
	tools.decision.allow with input as inp
}

test_resource_outside_session_denied if {
	d := tools.decision with input as object.union(base, {"resource": {"id": "acme/secrets"}})
	"resource acme/secrets is outside this session" in d.reasons
}

test_no_chain_denied if {
	d := tools.decision with input as object.union(base, {"subject": {"properties": {"actors": []}}})
	"token carries no agent chain" in d.reasons
}

test_unknown_tool_denied if {
	d := tools.decision with input as object.union(base, {"action": {"name": "shell.exec"}})
	"unknown tool" in d.reasons
}

test_old_token_denied if {
	d := tools.decision with input as with_ctx(base, {"token_age_s": 900})
	"token too old for a tool call" in d.reasons
}

test_missing_input_denies if {
	not tools.decision.allow with input as {}
}

# ---- fail closed on missing signals -----------------------------------------

test_missing_token_age_denies if {
	inp := {"subject": base.subject, "action": base.action, "resource": base.resource, "context": {"channel": "api"}}
	d := tools.decision with input as inp
	"token too old for a tool call" in d.reasons
}

test_missing_channel_denies if {
	inp := {"subject": base.subject, "action": base.action, "resource": base.resource, "context": {"token_age_s": 5}}
	d := tools.decision with input as inp
	"unknown request channel \"\"" in d.reasons
}

# ---- confirmation preference ------------------------------------------------

test_user_can_waive_confirmation_for_normal_write if {
	inp := with_ctx(object.union(gateway_write, {"action": {"name": "github.comment"}}), {"prefs": {"confirm_writes": false}})
	tools.decision.allow with input as inp
}

test_user_cannot_waive_confirmation_for_high_risk if {
	inp := with_ctx(object.union(gateway_write, {"action": {"name": "github.merge_pr"}}), {"prefs": {"confirm_writes": false}})
	d := tools.decision with input as inp
	d.reasons == ["write-class tool needs the user's approval for this exact call"]
}

test_confirmation_on_by_default if {
	d := tools.decision with input as object.union(gateway_write, {"action": {"name": "github.comment"}})
	d.reasons == ["write-class tool needs the user's approval for this exact call"]
}

test_non_boolean_pref_keeps_confirmation_on if {
	inp := with_ctx(object.union(gateway_write, {"action": {"name": "github.comment"}}), {"prefs": {"confirm_writes": "no"}})
	not tools.decision.allow with input as inp
}

# ---- voice ------------------------------------------------------------------

test_voice_read_allowed_when_all_checks_pass if {
	tools.decision.allow with input as with_ctx(base, {"channel": "voice", "voice": voice_ok})
}

test_voice_low_confidence_denied if {
	d := tools.decision with input as with_ctx(base, {"channel": "voice", "voice": object.union(voice_ok, {"confidence": 0.91})})
	d.reasons == ["voice match below 0.98"]
}

test_voice_off_network_denied if {
	d := tools.decision with input as with_ctx(base, {"channel": "voice", "voice": object.union(voice_ok, {"local_network": false})})
	d.reasons == ["voice request did not come from the home network"]
}

test_voice_failed_liveness_denied if {
	d := tools.decision with input as with_ctx(base, {"channel": "voice", "voice": object.union(voice_ok, {"liveness": false})})
	d.reasons == ["voice liveness check not passed"]
}

test_voice_with_no_signals_fails_every_check if {
	d := tools.decision with input as with_ctx(base, {"channel": "voice"})
	count(d.reasons) == 3
}

test_voice_high_risk_needs_confirmation_even_when_waived if {
	inp := with_ctx(object.union(gateway_write, {"action": {"name": "github.merge_pr"}}), {"channel": "voice", "voice": voice_ok, "prefs": {"confirm_writes": false}})
	d := tools.decision with input as inp
	d.reasons == ["write-class tool needs the user's approval for this exact call"]
}

test_voice_high_risk_allowed_after_confirmation if {
	inp := with_ctx(object.union(gateway_write, {"action": {"name": "github.merge_pr"}}), {"channel": "voice", "voice": voice_ok, "user_approved": true})
	tools.decision.allow with input as inp
}
