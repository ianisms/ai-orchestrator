import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "modules", "orchestrator", "core"))

import pytest

from voice_gate import HIGH, READ, WRITE, GateConfig, TurnSignals, audit_fields, classify, decide

RISK = {
    "weather": READ,
    "homeassistant__HassTurnOn": WRITE,
    "homeassistant__HassTurnOff": WRITE,
    "push_notify": HIGH,
    "timer_alarm": WRITE,
}


READS = {"timer_alarm": ["status", "list"], "speaker_identity": ["verify", "list_enrollments"]}


def cfg(mode="enforce", min_score=0.80, nets=("192.0.2.50/32",)):
    return GateConfig.build(mode, min_score, nets, {**RISK, "speaker_identity": HIGH}, read_actions=READS)


def good(**kw):
    s = dict(channel="voice", peer_host="192.0.2.50", profile_id="alice", score=0.86, liveness=True)
    s.update(kw)
    return TurnSignals(**s)


# ---- classification ---------------------------------------------------------

def test_read_tool_allowed_without_identity():
    assert decide("weather", {}, TurnSignals(), cfg()).allow


def test_unknown_tool_defaults_to_write():
    assert classify("brand_new_tool", {}, cfg()) == WRITE


@pytest.mark.parametrize("args", [
    {"name": "Front Door Lock"},
    {"name": "Garage"},
    {"name": "Kitchen Side Door Lock"},
    {"device_classes": ["lock"]},
    {"device_classes": ["garage"]},
    {"area": "garage"},
])
def test_ha_locks_garage_doors_are_high(args):
    assert classify("homeassistant__HassTurnOff", args, cfg()) == HIGH


def test_read_action_on_write_tool_is_read():
    assert classify("timer_alarm", {"action": "list"}, cfg()) == READ
    assert classify("timer_alarm", {"action": "set"}, cfg()) == WRITE
    assert classify("timer_alarm", {}, cfg()) == WRITE


def test_speaker_enrollment_by_tool_is_high():
    assert classify("speaker_identity", {"action": "enroll"}, cfg()) == HIGH
    assert classify("speaker_identity", {"action": "verify"}, cfg()) == READ


def test_ha_lights_stay_write():
    assert classify("homeassistant__HassTurnOn", {"name": "Hallway Lights", "device_classes": ["light"]}, cfg()) == WRITE


# ---- writes -----------------------------------------------------------------

def test_verified_write_needs_spoken_confirmation_by_default():
    d = decide("timer_alarm", {}, good(), cfg())
    assert not d.allow and d.needs == "spoken_confirmation"


def test_verified_write_allowed_after_spoken_confirmation():
    assert decide("timer_alarm", {}, good(spoken_confirmation=True), cfg()).allow


def test_verified_write_allowed_when_user_turned_confirmation_off():
    assert decide("timer_alarm", {}, good(confirm_writes=False), cfg()).allow


@pytest.mark.parametrize("override, reason", [
    ({"score": 0.70}, "below 0.800"),
    ({"peer_host": "192.0.2.99"}, "trusted speaker address"),
    ({"liveness": None}, "liveness check not passed"),
    ({"liveness": False}, "liveness check not passed"),
    ({"profile_id": "", "score": None}, "speaker not identified"),
    ({"channel": "text"}, "voice channel"),
])
def test_unverified_write_falls_back_to_phone_approval(override, reason):
    d = decide("timer_alarm", {}, good(**override), cfg())
    assert not d.allow and d.needs == "phone_approval"
    assert any(reason in r for r in d.reasons)


def test_unverified_write_cannot_be_waived_by_preference():
    d = decide("timer_alarm", {}, good(liveness=None, confirm_writes=False), cfg())
    assert not d.allow


def test_unverified_write_allowed_with_phone_approval():
    assert decide("timer_alarm", {}, good(liveness=None, phone_approval=True), cfg()).allow


# ---- high risk --------------------------------------------------------------

def test_high_risk_needs_phone_even_when_voice_verified_and_confirmations_off():
    d = decide("push_notify", {}, good(confirm_writes=False, spoken_confirmation=True), cfg())
    assert not d.allow and d.needs == "phone_approval"


def test_high_risk_needs_verified_voice_even_with_phone_approval():
    d = decide("homeassistant__HassTurnOff", {"name": "Front Door Lock"}, good(liveness=None, phone_approval=True), cfg())
    assert not d.allow
    assert d.reasons == ["liveness check not passed"]


def test_high_risk_allowed_with_verified_voice_and_phone_approval():
    assert decide("homeassistant__HassTurnOff", {"name": "Front Door Lock"}, good(phone_approval=True), cfg()).allow


# ---- fail closed and config -------------------------------------------------

def test_uncalibrated_threshold_never_verifies():
    d = decide("timer_alarm", {}, good(), cfg(min_score=None))
    assert "speaker match threshold not calibrated" in d.reasons


def test_no_trusted_networks_never_verifies():
    d = decide("timer_alarm", {}, good(), cfg(nets=()))
    assert any("trusted speaker address" in r for r in d.reasons)


def test_bad_peer_host_does_not_crash():
    d = decide("timer_alarm", {}, good(peer_host="not-an-ip"), cfg())
    assert not d.allow


def test_unknown_mode_falls_back_to_monitor_not_off():
    assert GateConfig.build("disabled-ish", 0.8, [], RISK).mode == "monitor"


def test_bad_cidr_ignored():
    c = GateConfig.build("enforce", 0.8, ["nope", "10.0.0.0/8"], RISK)
    assert len(c.trusted_speaker_nets) == 1


def test_monitor_mode_never_blocks_but_reports_would_deny():
    d = decide("push_notify", {}, TurnSignals(), cfg(mode="monitor"))
    assert not d.allow and not d.blocked
    assert audit_fields(d)["gate_decision"] == "would_deny"


def test_enforce_mode_blocks():
    d = decide("push_notify", {}, TurnSignals(), cfg())
    assert d.blocked and audit_fields(d)["gate_decision"] == "deny"


# ---- device origin, per-user thresholds, assurance (SESSION_BINDING.md) ------

from voice_gate import assurance, effective_threshold, origin_trusted  # noqa: E402


def dcfg(**kw):
    """Gate that takes the device credential as origin; no trusted nets at all."""
    kw.setdefault("mode", "enforce")
    kw.setdefault("min_score", 0.80)
    kw.setdefault("nets", ())
    return GateConfig.build(kw["mode"], kw["min_score"], kw["nets"], {**RISK, "speaker_identity": HIGH},
                            read_actions=READS, require_device=True)


def dev(**kw):
    s = dict(channel="voice", peer_host="203.0.113.9", profile_id="alice", score=0.86, liveness=True,
             device_id="kitchen", device_jkt="jkt-kitchen", device_verified=True, spoken_confirmation=True)
    s.update(kw)
    return TurnSignals(**s)


def test_device_origin_verifies_without_trusted_address():
    d = decide("timer_alarm", {"action": "set"}, dev(), dcfg())
    assert d.allow and d.assurance["origin"] == "device" and d.assurance["origin_trusted"]


def test_device_origin_required_fails_without_credential():
    d = decide("timer_alarm", {"action": "set"}, dev(device_verified=False), dcfg())
    assert not d.allow and any("device credential" in r for r in d.reasons)


def test_device_origin_ignores_address_when_required():
    # A trusted address is not enough once the device credential is the origin signal.
    c = GateConfig.build("enforce", 0.80, ("203.0.113.9/32",), RISK, read_actions=READS, require_device=True)
    d = decide("timer_alarm", {"action": "set"}, dev(device_verified=False), c)
    assert not d.allow


def test_device_origin_needs_thumbprint_not_just_flag():
    assert not origin_trusted(dev(device_jkt=""), dcfg())[0]


def test_address_origin_unchanged_when_device_not_required():
    assert origin_trusted(good(), cfg()) == (True, "address")
    assert origin_trusted(good(device_verified=True), cfg(nets=()))[0] is False


def test_profile_threshold_floored_by_global():
    # Alice enrolled at 0.90; the global floor is 0.80; 0.90 applies.
    assert effective_threshold(dev(profile_threshold=0.90), dcfg(min_score=0.80)) == (0.90, "profile")
    # A profile threshold below the floor is raised to the floor.
    assert effective_threshold(dev(profile_threshold=0.70), dcfg(min_score=0.80)) == (0.80, "profile")


def test_profile_threshold_alone_calibrates_the_gate():
    # No global floor yet, but this person was enrolled with a threshold: the gate works for them.
    assert effective_threshold(dev(profile_threshold=0.84), dcfg(min_score=None)) == (0.84, "profile")
    d = decide("timer_alarm", {"action": "set"}, dev(profile_threshold=0.84, score=0.85), dcfg(min_score=None))
    assert d.allow
    d = decide("timer_alarm", {"action": "set"}, dev(profile_threshold=0.84, score=0.83), dcfg(min_score=None))
    assert not d.allow and any("below 0.840 (profile)" in r for r in d.reasons)


def test_nothing_calibrated_never_verifies():
    assert effective_threshold(dev(), dcfg(min_score=None)) == (None, "")
    d = decide("timer_alarm", {"action": "set"}, dev(), dcfg(min_score=None))
    assert not d.allow and "speaker match threshold not calibrated" in d.reasons


def test_bad_profile_threshold_is_ignored():
    assert effective_threshold(dev(profile_threshold="nope"), dcfg(min_score=0.80)) == (0.80, "global")


def test_assurance_reports_what_was_measured():
    d = decide("timer_alarm", {"action": "set"}, dev(profile_threshold=0.9, score=0.95), dcfg())
    a = d.assurance
    assert a["method"] == "voice" and a["verified"] is True
    assert a["score"] == 0.95 and a["threshold"] == 0.9 and a["threshold_source"] == "profile"
    assert a["origin"] == "device" and a["device_id"] == "kitchen" and a["device_jkt"] == "jkt-kitchen"
    assert a["spoken_confirmation"] is True and a["phone_approval"] is False and a["liveness"] is True


def test_assurance_never_claims_verification_it_did_not_measure():
    d = decide("timer_alarm", {"action": "set"}, dev(liveness=None), dcfg())
    assert d.assurance["verified"] is False and d.assurance["liveness"] is None
    # Reads carry the measurement too, marked unverified: nothing was checked for them.
    r = decide("weather", {}, dev(), dcfg())
    assert r.allow and r.assurance["verified"] is False and r.assurance["device_id"] == "kitchen"


def test_assurance_text_channel():
    a = assurance(TurnSignals(channel="text"), dcfg(), False)
    assert a["method"] == "text" and a["origin_trusted"] is False


def test_audit_fields_carry_device_and_threshold():
    d = decide("timer_alarm", {"action": "set"}, dev(profile_threshold=0.9), dcfg())
    f = audit_fields(d)
    assert f["gate_device"] == "kitchen" and f["gate_device_jkt"] == "jkt-kitchen"
    assert f["gate_threshold"] == 0.9 and f["gate_threshold_source"] == "profile" and f["gate_origin"] == "device"
