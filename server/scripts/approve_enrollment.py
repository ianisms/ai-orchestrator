#!/usr/bin/env python3
"""List, approve or reject pending voice enrollments.

    python scripts/approve_enrollment.py list
    python scripts/approve_enrollment.py approve <index>
    python scripts/approve_enrollment.py reject <index>

A stated name ("I'm Alice") only records a pending sample. Nothing joins a voice
profile until it is approved here (or, later, from the phone). Run on the server
host; paths default to the host side of the /app/state mount.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

MAX_SAMPLES = 8  # matches speaker_identity max_enrollment_samples and get_person_id


def load(path: Path, key: str, default):
    if not path.exists():
        return default
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, type(default)) else default


def save(path: Path, key: str, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({key: value}, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def average(vectors):
    dim = min(len(v) for v in vectors)
    out = [sum(float(v[i]) for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in out))
    return [x / norm for x in out] if norm > 0 else out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["list", "approve", "reject"])
    ap.add_argument("index", nargs="?", type=int)
    ap.add_argument("--state-dir", default="/ai/server/state")
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir)
    pending_path = state_dir / "pending_enrollments.json"
    profiles_path = state_dir / "speaker_identity.json"
    pending = load(pending_path, "pending", [])

    if args.command == "list":
        if not pending:
            print("no pending enrollments")
        for i, p in enumerate(pending):
            age_min = (time.time() - float(p.get("created_at") or 0)) / 60
            print(f"[{i}] {p.get('display_name')} ({p.get('profile_id')})  {age_min:.0f} min ago  "
                  f"session {p.get('session_id')}")
        return 0

    if args.index is None or not 0 <= args.index < len(pending):
        print("give a valid index from 'list'", file=sys.stderr)
        return 2
    item = pending.pop(args.index)

    if args.command == "approve":
        emb = item.get("embedding")
        pid = str(item.get("profile_id") or "")
        if not pid or not isinstance(emb, list) or not emb:
            print("pending item is malformed; not applied", file=sys.stderr)
            return 1
        profiles = load(profiles_path, "profiles", {})
        entry = profiles.get(pid) if isinstance(profiles.get(pid), dict) else {}
        samples = entry.get("samples") if isinstance(entry.get("samples"), list) else []
        samples = (samples + [emb])[-MAX_SAMPLES:]
        profiles[pid] = {**entry, "samples": samples, "mean_embedding": average(samples)}
        save(profiles_path, "profiles", profiles)
        print(f"approved: {pid} now has {len(samples)} sample(s)")
    else:
        print(f"rejected: {item.get('profile_id')}")

    save(pending_path, "pending", pending)
    return 0


if __name__ == "__main__":
    sys.exit(main())
