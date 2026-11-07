"""Admin-run voice enrollment: sessions, acceptance criteria, profile writes, revocation.

No NeMo here: embeddings are small synthetic vectors, one direction per person
with a little noise, so genuine and impostor scores behave like the real thing.
"""
import json
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "modules", "orchestrator", "core"))

from voice_enrollment import (  # noqa: E402
    ABORTED, COMPLETE, EVALUATING, EXPIRED, FAILED, OPEN,
    DEFAULT_PROMPTS, EnrollmentError, EnrollmentPolicy, VoiceEnrollmentService, evaluate, prompt_match,
)


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def voice(seed, dim=16):
    """A person's base direction."""
    rng = random.Random(seed)
    return unit([rng.gauss(0, 1) for _ in range(dim)])


def sample_of(base, noise=0.08, seed=0):
    rng = random.Random(seed)
    return unit([b + rng.gauss(0, noise) for b in base])


GOOD_Q = {"duration_s": 3.5, "clipping_ratio": 0.0, "rms_dbfs": -20.0, "estimated_snr_db": 25.0, "silence_ratio": 0.2}
ALICE, BOB, CAROL = voice(1), voice(2), voice(3)


@pytest.fixture
def svc(tmp_path):
    clock = Clock()
    s = VoiceEnrollmentService(tmp_path / "sessions.json", tmp_path / "speaker_identity.json",
                               policy=EnrollmentPolicy(min_samples=4, max_samples=6, floor=0.80), clock=clock)
    s.clock_obj = clock
    return s


def seed_profile(svc, pid, base, n=4):
    profiles = svc._profiles()
    samples = [sample_of(base, seed=i) for i in range(n)]
    from voice_enrollment import mean_embedding
    profiles[pid] = {"samples": samples, "mean_embedding": mean_embedding(samples), "revoked_at": None}
    svc._save_profiles(profiles)


def run_session(svc, session, base, device=("kitchen", "jkt-k"), n=None, seed0=100):
    """Feed accepted samples until the session leaves OPEN or n samples are in."""
    said = []
    i = 0
    while session.open and (n is None or i < n):
        prompt = session.current_prompt()
        session, say, ok = svc.add_sample(session.session_id, device_id=device[0], device_jkt=device[1],
                                          transcript=prompt, embedding=sample_of(base, seed=seed0 + i),
                                          quality=GOOD_Q)
        said.append(say)
        assert ok
        i += 1
    return session, said


# ---- helpers ------------------------------------------------------------------

def test_prompt_match_is_order_free_and_lenient_on_fillers():
    assert prompt_match("set a timer for twelve minutes", "please set a timer for twelve minutes now") == 1.0
    assert prompt_match("set a timer for twelve minutes", "what is the weather") < 0.2
    assert prompt_match("", "anything") == 0.0


def test_evaluate_fixes_threshold_above_worst_impostor():
    alice = [sample_of(ALICE, seed=i) for i in range(4)]
    others = {"bob": unit(BOB), "carol": unit(CAROL)}
    ev = evaluate(alice, others, EnrollmentPolicy(min_samples=4, margin=0.03, floor=0.2))
    assert ev.ok and ev.threshold == round(max(ev.impostor) + 0.03, 4)
    assert all(g >= ev.threshold for g in ev.genuine)


def test_evaluate_floor_wins_when_higher():
    alice = [sample_of(ALICE, seed=i) for i in range(4)]
    ev = evaluate(alice, {"bob": unit(BOB)}, EnrollmentPolicy(min_samples=4, margin=0.03, floor=0.9))
    assert ev.threshold == 0.9


def test_evaluate_refuses_too_few_samples():
    ev = evaluate([sample_of(ALICE, seed=i) for i in range(3)], {}, EnrollmentPolicy(min_samples=4, floor=0.8))
    assert not ev.ok and "need 4 samples" in ev.reasons[0]


def test_evaluate_no_other_profile_uses_floor_and_says_so():
    ev = evaluate([sample_of(ALICE, seed=i) for i in range(4)], {}, EnrollmentPolicy(min_samples=4, floor=0.8))
    assert ev.ok and ev.threshold == 0.8 and any("separation not measurable" in r for r in ev.reasons)


def test_evaluate_no_other_profile_and_no_floor_cannot_fix_a_threshold():
    ev = evaluate([sample_of(ALICE, seed=i) for i in range(4)], {}, EnrollmentPolicy(min_samples=4, floor=None))
    assert not ev.ok and ev.threshold is None


def test_evaluate_indistinguishable_voice_fails():
    # Bob is already enrolled; "new" samples are Bob's own voice.
    bob = [sample_of(BOB, seed=i) for i in range(4)]
    ev = evaluate(bob, {"bob": unit(BOB)}, EnrollmentPolicy(min_samples=4, margin=0.03, floor=0.5))
    assert not ev.ok and ev.threshold is not None and ev.threshold > 0.95


def test_evaluate_inconsistent_samples_fail():
    mixed = [sample_of(ALICE, seed=0), sample_of(ALICE, seed=1), sample_of(CAROL, seed=0), sample_of(CAROL, seed=1)]
    ev = evaluate(mixed, {"bob": unit(BOB)}, EnrollmentPolicy(min_samples=4, margin=0.03, floor=0.5))
    assert not ev.ok and any("fall below" in r for r in ev.reasons)


# ---- sessions ----------------------------------------------------------------

def test_open_requires_device_and_admin_and_plain_profile_id(svc):
    with pytest.raises(EnrollmentError, match="device_id"):
        svc.open("alice", "Alice", "ian", "")
    with pytest.raises(EnrollmentError, match="opened_by"):
        svc.open("alice", "Alice", "", "kitchen")
    with pytest.raises(EnrollmentError, match="plain token"):
        svc.open("!!!", "x", "ian", "kitchen")
    s = svc.open("Alice B", "Alice", "ian", "kitchen", "jkt-k", consent="spoken, 2026-11-07")
    assert s.profile_id == "alice_b" and s.status == OPEN and s.device_jkt == "jkt-k" and len(s.prompts) == len(DEFAULT_PROMPTS)


def test_open_refuses_existing_profile_unless_replace(svc):
    seed_profile(svc, "alice", ALICE)
    with pytest.raises(EnrollmentError, match="exists"):
        svc.open("alice", "Alice", "ian", "kitchen")
    assert svc.open("alice", "Alice", "ian", "kitchen", replace=True).replace


def test_one_open_session_per_device_or_profile(svc):
    svc.open("alice", "Alice", "ian", "kitchen")
    with pytest.raises(EnrollmentError, match="already open"):
        svc.open("bob", "Bob", "ian", "kitchen")
    with pytest.raises(EnrollmentError, match="already open"):
        svc.open("alice", "Alice", "ian", "office")


def test_open_needs_enough_prompts(svc):
    with pytest.raises(EnrollmentError, match="prompts"):
        svc.open("alice", "Alice", "ian", "kitchen", prompts=["one", "two"])


def test_open_for_device_matches_id_and_thumbprint(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen", "jkt-k")
    assert svc.open_for_device("kitchen", "jkt-k").session_id == s.session_id
    assert svc.open_for_device("kitchen", "jkt-other") is None
    assert svc.open_for_device("office", "jkt-k") is None
    assert svc.open_for_device("kitchen").session_id == s.session_id  # no thumbprint known yet


def test_session_expires_on_read(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen", ttl_s=60)
    svc.clock_obj.t += 61
    assert svc.get(s.session_id).status == EXPIRED
    assert svc.open_for_device("kitchen") is None
    with pytest.raises(EnrollmentError, match="expired"):
        svc.add_sample(s.session_id, device_id="kitchen", device_jkt="", transcript="x", embedding=[1.0], quality=GOOD_Q)


def test_abort(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen")
    assert svc.abort(s.session_id, by="ian").status == ABORTED
    with pytest.raises(EnrollmentError, match="already aborted"):
        svc.abort(s.session_id)


# ---- the per-turn path -------------------------------------------------------

def test_sample_from_another_device_is_refused_and_session_untouched(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen", "jkt-k")
    with pytest.raises(EnrollmentError, match="other than the session's"):
        svc.add_sample(s.session_id, device_id="office", device_jkt="jkt-o", transcript=s.current_prompt(),
                       embedding=sample_of(ALICE), quality=GOOD_Q)
    with pytest.raises(EnrollmentError, match="other than the session's"):
        svc.add_sample(s.session_id, device_id="kitchen", device_jkt="jkt-stolen", transcript=s.current_prompt(),
                       embedding=sample_of(ALICE), quality=GOOD_Q)
    assert svc.get(s.session_id).samples == []


def test_bad_quality_repeats_the_prompt_with_a_tip(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen")
    prompt = s.current_prompt()
    s, say, ok = svc.add_sample(s.session_id, device_id="kitchen", device_jkt="", transcript=prompt,
                                embedding=sample_of(ALICE), quality={**GOOD_Q, "rms_dbfs": -50.0})
    assert not ok and "speak up" in say.lower() and prompt in say and s.samples == []


def test_prompt_mismatch_repeats_the_prompt(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen")
    prompt = s.current_prompt()
    s, say, ok = svc.add_sample(s.session_id, device_id="kitchen", device_jkt="", transcript="something else entirely",
                                embedding=sample_of(ALICE), quality=GOOD_Q)
    assert not ok and "did not match" in say and prompt in say and s.samples == []


def test_missing_embedding_repeats_the_prompt(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen")
    s, say, ok = svc.add_sample(s.session_id, device_id="kitchen", device_jkt="", transcript=s.current_prompt(),
                                embedding=None, quality=GOOD_Q, error="embedding failed")
    assert not ok and "could not hear" in say


def test_accepted_sample_moves_to_next_prompt(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen")
    first = s.current_prompt()
    s, say, ok = svc.add_sample(s.session_id, device_id="kitchen", device_jkt="", transcript=first,
                                embedding=sample_of(ALICE), quality=GOOD_Q)
    assert ok and len(s.samples) == 1 and s.current_prompt() != first and s.current_prompt() in say


def test_complete_writes_profile_with_threshold_and_record(svc):
    seed_profile(svc, "bob", BOB)
    s = svc.open("alice", "Alice", "ian", "kitchen", "jkt-k", consent="spoken")
    s, said = run_session(svc, s, ALICE)
    assert s.status == COMPLETE and "Enrollment complete for Alice" in said[-1]
    prof = json.loads(svc.profiles_path.read_text())["profiles"]["alice"]
    assert prof["threshold"] == s.evaluation["threshold"] and prof["threshold"] >= 0.80
    assert prof["enrolled_by"] == "ian" and prof["device_id"] == "kitchen" and prof["device_jkt"] == "jkt-k"
    assert prof["sample_count"] == 4 and len(prof["samples"]) == 4 and prof["consent"] == "spoken"
    assert prof["revoked_at"] is None and "genuine" not in prof["evaluation"]
    # bob was not touched
    assert json.loads(svc.profiles_path.read_text())["profiles"]["bob"]["revoked_at"] is None


def test_enrolling_a_duplicate_of_an_existing_voice_fails_at_max_samples(svc):
    seed_profile(svc, "bob", BOB)
    s = svc.open("bob_again", "Bob", "ian", "kitchen")
    s, said = run_session(svc, s, BOB)
    assert s.status == FAILED and "could not get a reliable match" in said[-1]
    assert "bob_again" not in json.loads(svc.profiles_path.read_text())["profiles"]


def test_needs_more_then_completes(svc):
    seed_profile(svc, "bob", BOB)
    s = svc.open("alice", "Alice", "ian", "kitchen")
    # Four samples with one noisy outlier: criteria not met at min, met after more.
    clean = [sample_of(ALICE, seed=i) for i in range(10)]
    noisy = sample_of(ALICE, noise=0.6, seed=99)
    seq = clean[:3] + [noisy] + clean[3:]
    said = []
    for emb in seq:
        if not s.open:
            break
        s, say, ok = svc.add_sample(s.session_id, device_id="kitchen", device_jkt="", transcript=s.current_prompt(),
                                    embedding=emb, quality=GOOD_Q)
        said.append(say)
    assert s.status in (COMPLETE, FAILED)
    assert any("A few more" in x for x in said) or s.status == COMPLETE


def test_revoke_profile_keeps_record_and_blocks_identification(svc):
    seed_profile(svc, "bob", BOB)
    assert svc.revoke_profile("bob", by="ian")
    prof = json.loads(svc.profiles_path.read_text())["profiles"]["bob"]
    assert prof["revoked_at"] is not None and prof["revoked_by"] == "ian"
    assert svc._other_means("alice") == {}  # a revoked profile is not an impostor reference either
    assert not svc.revoke_profile("nobody")


def test_evaluating_status_is_transient(svc):
    s = svc.open("alice", "Alice", "ian", "kitchen")
    s, _ = run_session(svc, s, ALICE, n=4)
    assert s.status != EVALUATING
