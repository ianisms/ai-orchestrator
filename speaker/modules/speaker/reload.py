from __future__ import annotations

import asyncio
import os
from typing import Iterable, List


def expand_watch_paths(paths: Iterable[str]) -> List[str]:
    expanded = set()
    for p in paths:
        ap = os.path.abspath(p)
        if os.path.isdir(ap):
            for root, _dirs, files in os.walk(ap):
                if any(
                    x in root
                    for x in (
                        os.sep + ".git",
                        os.sep + "__pycache__",
                        os.sep + ".cache",
                        os.sep + "logs",
                        os.sep + ".venv",
                        os.sep + "venv",
                        os.sep + "env",
                        os.sep + "site-packages",
                    )
                ):
                    continue
                for fname in files:
                    if fname.endswith((".py", ".proto", ".yaml", ".yml")):
                        expanded.add(os.path.join(root, fname))
        else:
            expanded.add(ap)
    return sorted(expanded)


async def watch_for_reload(paths, stop_evt: asyncio.Event, reload_evt: asyncio.Event, poll_s: float = 1.0):
    """Poll watched files (recurses into dirs); request restart when they change."""
    mtimes = {}
    base = list(paths)
    while not stop_evt.is_set():
        files = expand_watch_paths(base)
        for p in files:
            try:
                m = os.path.getmtime(p)
            except FileNotFoundError:
                continue
            prev = mtimes.get(p)
            mtimes[p] = m
            if prev is not None and m > prev:
                print(f"[reload] change detected in {p}; requesting restart", flush=True)
                reload_evt.set()
                stop_evt.set()
                return
        await asyncio.sleep(poll_s)
