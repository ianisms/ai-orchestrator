#!/usr/bin/env python
import json
import sys
import time

HEALTH_PATH = "/tmp/speaker_health.json"


def main() -> int:
    try:
        with open(HEALTH_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as exc:  # pragma: no cover - best effort health
        print(f"health: no file ({exc})")
        return 1

    now = time.time()
    if now - d.get("wake_loop_alive_ts", 0) > 5:
        print("health: wake loop stale")
        return 1

    print("health: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
