#!/usr/bin/env python3
"""Suggest VOICE_GATE_MIN_SCORE from the enrolled voice samples.

    python scripts/calibrate_voice_threshold.py [--state /ai/server/state/speaker_identity.json]

Genuine scores: each enrolled sample against the mean of that person's OTHER samples.
Impostor scores: each sample against every other person's mean.
The suggested threshold sits just above the highest impostor score, and the report
shows how many genuine samples would still pass at that threshold.

Enrollment samples are cleaner than real turns. Run the gate in monitor mode for a
few days, check the logged gate_score values for real requests, and recalibrate.
Read-only: never changes the profile file.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


def cosine(a, b):
    n = min(len(a), len(b))
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    na = math.sqrt(sum(float(a[i]) ** 2 for i in range(n)))
    nb = math.sqrt(sum(float(b[i]) ** 2 for i in range(n)))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def mean(vectors):
    dim = min(len(v) for v in vectors)
    out = [sum(float(v[i]) for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in out))
    return [x / norm for x in out] if norm > 0 else out


def pct(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def scores(profiles):
    genuine, impostor = [], []
    means = {}
    for pid, entry in profiles.items():
        samples = entry.get("samples") if isinstance(entry, dict) else None
        if isinstance(samples, list) and samples:
            means[pid] = mean(samples)
    for pid, entry in profiles.items():
        samples = entry.get("samples") if isinstance(entry, dict) else None
        if not isinstance(samples, list):
            continue
        for i, s in enumerate(samples):
            others = samples[:i] + samples[i + 1:]
            if others:
                genuine.append(cosine(s, mean(others)))
            for other_id, other_mean in means.items():
                if other_id != pid:
                    impostor.append(cosine(s, other_mean))
    return genuine, impostor


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", default="/ai/server/state/speaker_identity.json")
    ap.add_argument("--margin", type=float, default=0.02, help="added above the highest impostor score")
    args = ap.parse_args(argv)

    path = Path(args.state)
    if not path.exists():
        print(f"profile file not found: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        print("no enrolled profiles", file=sys.stderr)
        return 2

    genuine, impostor = scores(profiles)
    print(f"profiles: {len(profiles)}  genuine pairs: {len(genuine)}  impostor pairs: {len(impostor)}")
    for name, vals in (("genuine", genuine), ("impostor", impostor)):
        if vals:
            print(f"{name:9s} min {min(vals):.3f}  p10 {pct(vals, .1):.3f}  median {pct(vals, .5):.3f}  "
                  f"p90 {pct(vals, .9):.3f}  max {max(vals):.3f}")
    if not genuine:
        print("need at least two samples for one person to measure genuine scores", file=sys.stderr)
        return 1
    if not impostor:
        print("only one person enrolled: impostor scores cannot be measured; enroll a second voice first",
              file=sys.stderr)
        return 1

    threshold = round(max(impostor) + args.margin, 3)
    passing = sum(1 for g in genuine if g >= threshold)
    print(f"\nsuggested VOICE_GATE_MIN_SCORE: {threshold}")
    print(f"genuine samples passing at that threshold: {passing}/{len(genuine)} ({passing / len(genuine):.0%})")
    if passing / len(genuine) < 0.8:
        print("warning: fewer than 80% of genuine samples pass; add more enrollment samples before enforcing")
    for t in (0.70, 0.78, 0.85, 0.90, 0.95, 0.98):
        ga = sum(1 for g in genuine if g >= t) / len(genuine)
        fa = sum(1 for x in impostor if x >= t) / len(impostor)
        print(f"  at {t:.2f}: genuine accepted {ga:.0%}, impostors accepted {fa:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
