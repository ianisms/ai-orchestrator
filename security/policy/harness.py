"""Shared setup for demo and tests: path to prototype A, an OPA server, and the wiring."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REGO = HERE / "rego"
sys.path.insert(0, str(HERE.parent / "delegation"))


def find_opa() -> str | None:
    return os.environ.get("OPA_BIN") or shutil.which("opa") or shutil.which("opa.exe")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OPAServer:
    def __init__(self, opa_bin: str):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [opa_bin, "run", "--server", "--addr", f"127.0.0.1:{self.port}",
             str(REGO / "agent_tools.rego"), str(REGO / "data.json")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.url}/health", timeout=0.5).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("OPA did not start")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)


def catalog() -> dict:
    return json.loads((REGO / "data.json").read_text())["tools"]
