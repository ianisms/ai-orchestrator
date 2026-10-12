# Policy decision for one MCP tools/call, evaluated once over the whole delegation chain.
#
# Input is an AuthZEN evaluation request:
#   subject  {type: "user", id, properties: {actors: [earliest .. caller], scopes: [...]}}
#   action   {name: "<tool>"}
#   resource {type, id}
#   context  {token_age_s, user_approved, session_resources: [...]}
#
# Access only narrows: every actor in the chain must hold the tool, the token must
# hold the tool's scope, and write-class tools need the user's approval for that
# exact call. Allow only when no reason to deny exists.
package agent.tools

import rego.v1

actors := object.get(input, ["subject", "properties", "actors"], [])

scopes := object.get(input, ["subject", "properties", "scopes"], [])

tool := data.tools[input.action.name]

reasons contains "unknown tool" if not data.tools[input.action.name]

reasons contains "token carries no agent chain" if count(actors) == 0

reasons contains sprintf("chain too long (%d > %d)", [count(actors), data.limits.max_chain]) if {
	count(actors) > data.limits.max_chain
}

reasons contains sprintf("%s is not granted %s", [a, input.action.name]) if {
	some a in actors
	not input.action.name in object.get(data.agents, [a, "tools"], [])
}

reasons contains sprintf("token lacks scope %s", [tool.scope]) if {
	not tool.scope in scopes
}

reasons contains "write-class tool needs the user's approval for this exact call" if {
	tool.class == "write"
	object.get(input, ["context", "user_approved"], false) != true
}

reasons contains sprintf("resource %s is outside this session", [input.resource.id]) if {
	allowed := object.get(input, ["context", "session_resources"], [])
	count(allowed) > 0
	not input.resource.id in allowed
}

reasons contains "token too old for a tool call" if {
	object.get(input, ["context", "token_age_s"], 0) > data.limits.max_token_age_s
}

decision := {"allow": count(reasons) == 0, "reasons": sort(reasons)}
