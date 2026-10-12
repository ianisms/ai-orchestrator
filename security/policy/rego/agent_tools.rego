# Policy decision for one MCP tools/call, evaluated once over the whole delegation chain.
#
# Input is an AuthZEN evaluation request:
#   subject  {type: "user", id, properties: {actors: [earliest .. caller], scopes: [...]}}
#   action   {name: "<tool>"}
#   resource {type, id}
#   context  {channel: "api" | "voice", token_age_s, user_approved, session_resources: [...],
#             voice: {confidence, local_network, liveness}, prefs: {confirm_writes}}
#
# Access only narrows: every actor in the chain must hold the tool, the token must
# hold the tool's scope, and write-class tools need the user's approval for that
# exact call. Voice requests must also pass the voice checks.
#
# Fails closed: allow only when no reason to deny exists, and any missing signal
# counts as a failed check, never as a pass.
package agent.tools

import rego.v1

actors := object.get(input, ["subject", "properties", "actors"], [])

scopes := object.get(input, ["subject", "properties", "scopes"], [])

ctx := object.get(input, "context", {})

channel := object.get(ctx, "channel", "")

voice := object.get(ctx, "voice", {})

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

# ---- confirmation -----------------------------------------------------------
# On by default. A user may turn it off for ordinary writes; high-risk tools
# always need it. Anything other than an explicit false keeps it on.

confirmation_waived if {
	object.get(tool, "risk", "high") != "high"
	object.get(ctx, ["prefs", "confirm_writes"], true) == false
}

reasons contains "write-class tool needs the user's approval for this exact call" if {
	tool.class == "write"
	object.get(ctx, "user_approved", false) != true
	not confirmation_waived
}

# ---- voice ------------------------------------------------------------------

reasons contains sprintf("unknown request channel %q", [channel]) if not channel in {"api", "voice"}

reasons contains sprintf("voice match below %v", [data.limits.voice_min_confidence]) if {
	channel == "voice"
	not object.get(voice, "confidence", 0) >= data.limits.voice_min_confidence
}

reasons contains "voice request did not come from the home network" if {
	channel == "voice"
	object.get(voice, "local_network", false) != true
}

reasons contains "voice liveness check not passed" if {
	channel == "voice"
	object.get(voice, "liveness", false) != true
}

# ---- session and freshness --------------------------------------------------

reasons contains sprintf("resource %s is outside this session", [input.resource.id]) if {
	allowed := object.get(ctx, "session_resources", [])
	count(allowed) > 0
	not input.resource.id in allowed
}

reasons contains "token too old for a tool call" if {
	not object.get(ctx, "token_age_s", data.limits.max_token_age_s + 1) <= data.limits.max_token_age_s
}

decision := {"allow": count(reasons) == 0, "reasons": sort(reasons)}
