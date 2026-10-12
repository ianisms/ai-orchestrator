#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
from typing import Iterable

TARGETS = [
    "/ai/server/scripts",
    "start-servers.py",
]

def pgrep(pattern: str) -> list[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
        return [int(pid) for pid in out.strip().splitlines() if pid.strip()]
    except subprocess.CalledProcessError:
        return []

def safe_kill(pids: Iterable[int], sig=signal.SIGKILL):
    me = os.getpid()
    parent = os.getppid()
    for pid in pids:
        if pid in (me, parent):
            continue
        # skip any process that contains kill-zombiez in its cmdline
        try:
            cmd = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True).strip()
            if "kill-zombiez" in cmd:
                continue
        except subprocess.CalledProcessError:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

def main():
    subprocess.call("clear")

    print("=" * 80)
    print(" 🧟   Killing zombiez...")
    print("=" * 80)

    for pat in TARGETS:
        safe_kill(pgrep(pat), signal.SIGKILL)

    print("=" * 80)
    print(" 🗡️🧟  All zombiez have been safely terminated... Safe to roam now 😁")
    print("=" * 80)

if __name__ == "__main__":
    main()
