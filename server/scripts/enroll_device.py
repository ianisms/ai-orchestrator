#!/usr/bin/env python3
"""List, add, revoke or remove enrolled speaker device keys.

    python scripts/enroll_device.py list
    python scripts/enroll_device.py add --request device_enrollment.json [--replace]
    python scripts/enroll_device.py revoke <device_id>
    python scripts/enroll_device.py remove <device_id>

The request file is written by speaker/scripts/enroll_device_key.py on the Pi
with the YubiKey plugged in. Registering is deliberately a host-side admin
step, like approving a voice enrollment (approve_enrollment.py): a device
cannot enroll itself over the network.

revoke keeps the entry but refuses every future challenge and every credential
check for it, which is what you want when a YubiKey goes missing. remove drops
the entry; use it when re-enrolling a rebuilt device with a new key. Paths
default to the host side of the /app/state mount.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "modules", "orchestrator", "core"))

from device_identity import DeviceIdentityError, EnrollmentRegistry  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["list", "add", "revoke", "remove"])
    ap.add_argument("device_id", nargs="?")
    ap.add_argument("--request", help="enrollment request JSON from the speaker (add)")
    ap.add_argument("--replace", action="store_true", help="rotate the key of an already enrolled device")
    ap.add_argument("--state-dir", default="/ai/server/state")
    ap.add_argument("--registry", default="", help="registry file; default <state-dir>/device_registry.json")
    args = ap.parse_args(argv)

    path = Path(args.registry) if args.registry else Path(args.state_dir) / "device_registry.json"
    registry = EnrollmentRegistry(path)

    try:
        if args.command == "list":
            entries = registry.list()
            if not entries:
                print("no enrolled devices")
            for e in entries:
                age_d = (time.time() - e.enrolled_at) / 86400
                state = "REVOKED" if e.revoked else "active"
                print(f"{e.device_id:<20} {e.room:<12} {e.household:<10} {state:<8} jkt={e.thumbprint} "
                      f"enrolled {age_d:.0f} d ago")
            return 0

        if args.command == "add":
            if not args.request:
                print("--request is required", file=sys.stderr)
                return 2
            req = json.loads(Path(args.request).read_text(encoding="utf-8"))
            if not isinstance(req, dict) or not req.get("device_id") or not req.get("public_key_pem"):
                print("request is malformed; needs device_id and public_key_pem", file=sys.stderr)
                return 1
            entry = registry.enroll(str(req["device_id"]), str(req.get("room") or ""),
                                    str(req.get("household") or ""), str(req["public_key_pem"]),
                                    replace=args.replace)
            print(f"enrolled {entry.device_id} room={entry.room} household={entry.household} jkt={entry.thumbprint}")
            return 0

        if not args.device_id:
            print("device_id is required", file=sys.stderr)
            return 2

        if args.command == "revoke":
            if registry.revoke(args.device_id):
                print(f"revoked {args.device_id}; its credentials stop verifying now")
                return 0
            print(f"{args.device_id} is not enrolled", file=sys.stderr)
            return 1

        # remove
        devices = registry._load()
        if args.device_id not in devices:
            print(f"{args.device_id} is not enrolled", file=sys.stderr)
            return 1
        del devices[args.device_id]
        registry._save(devices)
        print(f"removed {args.device_id}")
        return 0
    except DeviceIdentityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
