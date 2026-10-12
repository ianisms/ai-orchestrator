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
	"context": {"token_age_s": 5, "user_approved": false, "session_resources": ["acme/widgets"]},
}

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
	inp := object.union(base, {"subject": {"properties": {"actors": ["mcp-gateway"], "scopes": ["repo:read", "repo:write"]}}, "action": {"name": "github.merge_pr"}})
	d := tools.decision with input as inp
	d.reasons == ["write-class tool needs the user's approval for this exact call"]
}

test_gateway_write_with_approval_allowed if {
	inp := object.union(base, {
		"subject": {"properties": {"actors": ["mcp-gateway"], "scopes": ["repo:read", "repo:write"]}},
		"action": {"name": "github.merge_pr"},
		"context": {"user_approved": true},
	})
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
	d := tools.decision with input as object.union(base, {"context": {"token_age_s": 900}})
	"token too old for a tool call" in d.reasons
}

test_missing_input_denies if {
	not tools.decision.allow with input as {}
}
