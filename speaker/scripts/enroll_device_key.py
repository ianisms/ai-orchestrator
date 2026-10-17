#!/usr/bin/env python3
"""Enroll this speaker's device key: generate it on the YubiKey, export the
public key, and register it with the orchestrator.

    # on the Pi, YubiKey plugged in, one-time
    python scripts/enroll_device_key.py generate --device-id speaker-1 --room kitchen --household home \
        --management-key-file /root/.yk_mgmt --pin-file /ai/speaker/state/device_pin

    # show the public key already in slot 9a without touching it
    python scripts/enroll_device_key.py show

generate writes an enrollment request (device_id, room, household, public key
PEM, requested_at) to --out, default /ai/speaker/state/device_enrollment.json,
and prints it. Registration is an admin step on the server host, the same way
voice enrollment is approved there:

    python server/scripts/enroll_device.py add --request device_enrollment.json

If --registry points at the orchestrator's registry file (this script running
on the server host with the YubiKey attached, or over a mount) it registers
directly and no second step is needed.

Key policy: ECCP256 in PIV slot 9a, touch policy never, PIN policy once by
default. The PIN is read from --pin-file at boot (mode 600). --pin-policy never
removes the PIN step entirely; then possession of the key is the whole secret.
The management key is needed only here, to generate the key; it is never read
at boot. The private key cannot be exported from the YubiKey.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.speaker.device_identity import DeviceIdentityError, Signer, YubiKeyPivSigner  # noqa: E402

DEFAULT_OUT = "/ai/speaker/state/device_enrollment.json"


def read_secret_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def enrollment_request(device_id: str, room: str, household: str, public_key_pem: str,
                       serial: int | None, now: float) -> dict:
    return {
        "device_id": device_id,
        "room": room,
        "household": household,
        "public_key_pem": public_key_pem,
        "yubikey_serial": serial,
        "requested_at": now,
    }


def register(request: dict, registry_path: str, server_root: Path | None = None) -> dict:
    """Write the entry into the orchestrator's registry. Imports the server-side
    module, so the server tree must be on disk next to this one or named."""
    core = (server_root or ROOT.parent / "server") / "modules" / "orchestrator" / "core"
    if str(core) not in sys.path:
        sys.path.insert(0, str(core))
    from device_identity import EnrollmentRegistry  # server-side module

    entry = EnrollmentRegistry(registry_path).enroll(
        request["device_id"], request["room"], request["household"], request["public_key_pem"]
    )
    return entry.to_json()


def main(argv=None, signer: Signer | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["generate", "show"])
    ap.add_argument("--device-id", default="", help="plain token, e.g. speaker-1 (matches session_id_base)")
    ap.add_argument("--room", default="")
    ap.add_argument("--household", default="")
    ap.add_argument("--management-key-file", default="", help="hex PIV management key; generate only")
    ap.add_argument("--pin-file", default="", help="PIN file the client reads at boot; checked for mode 600")
    ap.add_argument("--pin-policy", default="once", choices=["once", "never", "always"])
    ap.add_argument("--serial", type=int, default=None, help="pick this YubiKey when several are attached")
    ap.add_argument("--out", default=DEFAULT_OUT, help="where to write the enrollment request JSON")
    ap.add_argument("--registry", default="", help="orchestrator registry file; registers directly when given")
    ap.add_argument("--server-root", default="", help="path to the server/ tree when --registry is used")
    ap.add_argument("--yes", action="store_true", help="generate without asking; overwrites slot 9a")
    args = ap.parse_args(argv)

    now = time.time()
    yk = signer or YubiKeyPivSigner(pin_path=args.pin_file or None, serial=args.serial)

    try:
        if args.command == "show":
            print(yk.public_key_pem(), end="")
            return 0

        if not args.device_id:
            print("--device-id is required for generate", file=sys.stderr)
            return 2
        if not args.yes and sys.stdin.isatty():
            answer = input("This replaces any key in PIV slot 9a. Continue? [y/N] ").strip().lower()
            if answer != "y":
                print("aborted")
                return 1

        if hasattr(yk, "generate_key"):
            if not args.management_key_file:
                print("--management-key-file is required to generate a key on the YubiKey", file=sys.stderr)
                return 2
            mgmt = bytes.fromhex(read_secret_file(args.management_key_file))
            pem = yk.generate_key(mgmt, pin_policy=args.pin_policy)
        else:
            pem = yk.public_key_pem()
        serial = yk.serial() if hasattr(yk, "serial") else None

        request = enrollment_request(args.device_id, args.room, args.household, pem, serial, now)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(request, indent=2), encoding="utf-8")
        print(json.dumps(request, indent=2))
        print(f"enrollment request written to {out}", file=sys.stderr)

        if args.registry:
            entry = register(request, args.registry, Path(args.server_root) if args.server_root else None)
            print(f"registered {entry['device_id']} jkt={entry['thumbprint']} in {args.registry}", file=sys.stderr)
        else:
            print("next: on the server host, python server/scripts/enroll_device.py add --request "
                  f"{out.name}", file=sys.stderr)
        return 0
    except DeviceIdentityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
