#!/usr/bin/env python3
"""
Windows-friendly replacement for nuke_docker.sh.
Stops and removes all containers, volumes, networks (and optionally images).
"""

import argparse
import subprocess
import sys


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def confirm_prompt() -> bool:
    prompt = "ARE YOU SURE? This will remove ALL Docker containers, volumes, and networks! Type y/yes to confirm: "
    reply = input(prompt).strip().lower()
    return reply in {"y", "yes"}


def remove_all_containers() -> None:
    res = run(["docker", "ps", "-aq"])
    ids = res.stdout.strip().splitlines()
    if ids:
        run(["docker", "stop", *ids])
        run(["docker", "rm", "-f", *ids])


def remove_all_images() -> None:
    res = run(["docker", "images", "-q"])
    ids = res.stdout.strip().splitlines()
    if ids:
        run(["docker", "rmi", "-f", *ids])


def remove_all_volumes() -> None:
    res = run(["docker", "volume", "ls", "-q"])
    ids = res.stdout.strip().splitlines()
    if ids:
        run(["docker", "volume", "rm", *ids])


def remove_custom_networks() -> None:
    res = run(["docker", "network", "ls", "--format", "{{.Name}}"])
    names = [n for n in res.stdout.strip().splitlines() if n not in {"bridge", "host", "none"}]
    if names:
        run(["docker", "network", "rm", *names])


def main() -> int:
    parser = argparse.ArgumentParser(description="Nuke all Docker artifacts.")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt.")
    parser.add_argument("-i", "--images", action="store_true", help="Also remove all images.")
    args = parser.parse_args()

    if not args.yes and not confirm_prompt():
        print("Nuke operation cancelled.")
        return 0

    print("Nuking docker environment...")
    remove_all_containers()
    if args.images:
        remove_all_images()
    remove_all_volumes()
    remove_custom_networks()
    print("Docker environment nuked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
