import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import approve_enrollment
import calibrate_voice_threshold


def _voice(seed, n, dim=16, noise=0.15):
    rnd = random.Random(seed)
    center = [rnd.gauss(0, 1) for _ in range(dim)]
    return [[c + rnd.gauss(0, noise) for c in center] for _ in range(n)]


def _write_profiles(path, people):
    profiles = {pid: {"samples": s, "mean_embedding": calibrate_voice_threshold.mean(s)} for pid, s in people.items()}
    path.write_text(json.dumps({"profiles": profiles}))


def test_calibration_separates_two_voices(tmp_path, capsys):
    state = tmp_path / "speaker_identity.json"
    _write_profiles(state, {"alice": _voice(1, 6), "guest": _voice(2, 6)})
    assert calibrate_voice_threshold.main(["--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "suggested VOICE_GATE_MIN_SCORE" in out
    genuine, impostor = calibrate_voice_threshold.scores(json.loads(state.read_text())["profiles"])
    assert min(genuine) > max(impostor)


def test_calibration_refuses_single_person(tmp_path):
    state = tmp_path / "speaker_identity.json"
    _write_profiles(state, {"alice": _voice(1, 4)})
    assert calibrate_voice_threshold.main(["--state", str(state)]) == 1


def test_approve_moves_sample_into_profile(tmp_path):
    (tmp_path / "pending_enrollments.json").write_text(json.dumps({"pending": [
        {"profile_id": "sam", "display_name": "Sam", "embedding": [1.0, 0.0, 0.0], "created_at": 0, "session_id": "s"},
    ]}))
    assert approve_enrollment.main(["approve", "0", "--state-dir", str(tmp_path)]) == 0
    profiles = json.loads((tmp_path / "speaker_identity.json").read_text())["profiles"]
    assert profiles["sam"]["samples"] == [[1.0, 0.0, 0.0]]
    assert json.loads((tmp_path / "pending_enrollments.json").read_text())["pending"] == []


def test_approve_keeps_existing_samples_and_caps_at_eight(tmp_path):
    existing = [[float(i), 1.0] for i in range(8)]
    (tmp_path / "speaker_identity.json").write_text(json.dumps({"profiles": {"alice": {"samples": existing}}}))
    (tmp_path / "pending_enrollments.json").write_text(json.dumps({"pending": [
        {"profile_id": "alice", "display_name": "Alice", "embedding": [9.0, 1.0], "created_at": 0, "session_id": "s"},
    ]}))
    assert approve_enrollment.main(["approve", "0", "--state-dir", str(tmp_path)]) == 0
    samples = json.loads((tmp_path / "speaker_identity.json").read_text())["profiles"]["alice"]["samples"]
    assert len(samples) == 8 and samples[-1] == [9.0, 1.0] and samples[0] == [1.0, 1.0]


def test_reject_leaves_profiles_untouched(tmp_path):
    (tmp_path / "pending_enrollments.json").write_text(json.dumps({"pending": [
        {"profile_id": "alice", "display_name": "Alice", "embedding": [0.0, 1.0], "created_at": 0, "session_id": "s"},
    ]}))
    assert approve_enrollment.main(["reject", "0", "--state-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "speaker_identity.json").exists()


def test_bad_index_changes_nothing(tmp_path):
    (tmp_path / "pending_enrollments.json").write_text(json.dumps({"pending": []}))
    assert approve_enrollment.main(["approve", "3", "--state-dir", str(tmp_path)]) == 2


# ---- enroll_voice.py (admin-run enrollment sessions) -------------------------

import enroll_voice  # noqa: E402


def test_enroll_voice_open_list_status_abort(tmp_path, capsys):
    sd = str(tmp_path)
    assert enroll_voice.main(["--state-dir", sd, "open", "alice", "--name", "Alice", "--device", "kitchen",
                              "--by", "ian", "--consent", "spoken"]) == 0
    out = capsys.readouterr().out
    assert "opened" in out and "on kitchen by ian" in out and "first prompt:" in out
    sid = out.split()[1].rstrip(":")
    assert enroll_voice.main(["--state-dir", sd, "list"]) == 0
    assert "alice" in capsys.readouterr().out
    assert enroll_voice.main(["--state-dir", sd, "status", sid]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "open" and status["opened_by"] == "ian" and status["consent"] == "spoken"
    # a second session on the same speaker is refused while the first is open
    assert enroll_voice.main(["--state-dir", sd, "open", "bob", "--device", "kitchen", "--by", "ian"]) == 1
    assert "refused" in capsys.readouterr().err
    assert enroll_voice.main(["--state-dir", sd, "abort", sid, "--by", "ian"]) == 0
    assert "aborted" in capsys.readouterr().out
    assert enroll_voice.main(["--state-dir", sd, "status", sid]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "aborted"


def test_enroll_voice_revoke_profile(tmp_path, capsys):
    state = tmp_path / "speaker_identity.json"
    _write_profiles(state, {"alice": _voice(1, 4)})
    assert enroll_voice.main(["--state-dir", str(tmp_path), "revoke-profile", "alice", "--by", "ian"]) == 0
    assert json.loads(state.read_text())["profiles"]["alice"]["revoked_by"] == "ian"
    assert enroll_voice.main(["--state-dir", str(tmp_path), "revoke-profile", "nobody"]) == 1


def test_enroll_voice_custom_prompts_need_min_count(tmp_path, capsys):
    f = tmp_path / "p.txt"
    f.write_text("only one\n")
    assert enroll_voice.main(["--state-dir", str(tmp_path), "open", "alice", "--device", "k", "--by", "ian",
                              "--prompts", str(f)]) == 1
