#!/usr/bin/env python3
"""Admin-run voice enrollment sessions (security/VOICE_ENROLLMENT.md).

    python scripts/enroll_voice.py open <profile_id> --name "Alice" --device kitchen [--device-jkt <thumbprint>]
                                       [--consent "spoken, in the kitchen"] [--ttl 900] [--prompts file.txt] [--replace]
    python scripts/enroll_voice.py list
    python scripts/enroll_voice.py status <session_id>
    python scripts/enroll_voice.py abort <session_id>
    python scripts/enroll_voice.py revoke-profile <profile_id>
    python scripts/enroll_voice.py prompts

Opening a session is the admin step: it names who is being enrolled, on which
speaker, by whom (the --by flag, default the login user), and records consent
text. The person then talks to that speaker; the orchestrator runs the prompts
and writes the profile when the acceptance criteria hold. Nothing here touches
audio or embeddings. Run on the server host; paths default to the host side
of the /app/state mount, the same files the orchestrator and tools server read.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "modules", "orchestrator", "core"))

from voice_enrollment import DEFAULT_PROMPTS, EnrollmentError, EnrollmentPolicy, VoiceEnrollmentService  # noqa: E402


def build_service(args) -> VoiceEnrollmentService:
    floor = None if args.floor is None else float(args.floor)
    return VoiceEnrollmentService(
        Path(args.state_dir) / "voice_enrollment_sessions.json",
        Path(args.state_dir) / "speaker_identity.json",
        policy=EnrollmentPolicy(floor=floor),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default="/ai/server/state")
    ap.add_argument("--floor", type=float, default=None,
                    help="global VOICE_GATE_MIN_SCORE; the per-user threshold never goes below it")
    sub = ap.add_subparsers(dest="command", required=True)

    o = sub.add_parser("open", help="open a session for one person on one speaker")
    o.add_argument("profile_id")
    o.add_argument("--name", default="", help="display name spoken back during enrollment")
    o.add_argument("--device", required=True, help="device_id of the speaker (device_registry.json)")
    o.add_argument("--device-jkt", default="", help="the speaker's key thumbprint; refuses other keys")
    o.add_argument("--by", default=getpass.getuser(), help="the admin opening the session")
    o.add_argument("--consent", default="", help="how consent was given, recorded on the profile")
    o.add_argument("--ttl", type=int, default=None, help="seconds the session stays open")
    o.add_argument("--prompts", default="", help="file with one prompt sentence per line")
    o.add_argument("--replace", action="store_true", help="re-enroll an existing profile")

    sub.add_parser("list")
    st = sub.add_parser("status")
    st.add_argument("session_id")
    ab = sub.add_parser("abort")
    ab.add_argument("session_id")
    ab.add_argument("--by", default=getpass.getuser())
    rv = sub.add_parser("revoke-profile")
    rv.add_argument("profile_id")
    rv.add_argument("--by", default=getpass.getuser())
    sub.add_parser("prompts", help="print the default prompt sentences")

    args = ap.parse_args(argv)
    if args.command == "prompts":
        print("\n".join(DEFAULT_PROMPTS))
        return 0

    svc = build_service(args)
    try:
        if args.command == "open":
            prompts = None
            if args.prompts:
                prompts = [ln.strip() for ln in Path(args.prompts).read_text(encoding="utf-8").splitlines() if ln.strip()]
            s = svc.open(args.profile_id, args.name, args.by, args.device, args.device_jkt,
                         consent=args.consent, prompts=prompts, ttl_s=args.ttl, replace=args.replace)
            print(f"opened {s.session_id}: {s.profile_id} ({s.display_name}) on {s.device_id} by {s.opened_by}, "
                  f"{len(s.prompts)} prompts, expires in {int(s.expires_at - svc.clock())} s")
            print(f"first prompt: {s.current_prompt()}")
            return 0
        if args.command == "list":
            rows = svc.list()
            if not rows:
                print("no sessions")
            for s in rows:
                print(f"{s.session_id}  {s.status:10s} {s.profile_id:16s} device={s.device_id} by={s.opened_by} "
                      f"samples={len(s.samples)}")
            return 0
        if args.command == "status":
            s = svc.get(args.session_id)
            if s is None:
                print("unknown session", file=sys.stderr)
                return 2
            d = s.to_json()
            for smp in d["samples"]:
                smp.pop("embedding", None)
            print(json.dumps(d, indent=2, sort_keys=True))
            return 0
        if args.command == "abort":
            s = svc.abort(args.session_id, by=args.by)
            print(f"aborted {s.session_id}")
            return 0
        if args.command == "revoke-profile":
            ok = svc.revoke_profile(args.profile_id, by=args.by)
            print("revoked" if ok else "no such profile")
            return 0 if ok else 1
    except EnrollmentError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
