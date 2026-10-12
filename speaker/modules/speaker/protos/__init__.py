"""
Proto bootstrapper for the speaker client.
- Ensures orchestrator pb2 stubs are generated at runtime if missing/stale.
- Rewrites the grpc stub import to use the packaged module path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROTO_NAME = "orchestrator.proto"
THIS_DIR = Path(__file__).resolve().parent
PROTO_DIR = THIS_DIR  # /ai/speaker/modules/speaker/protos
SRC_PROTO = PROTO_DIR / PROTO_NAME
PB2 = THIS_DIR / "orchestrator_pb2.py"
PB2_GRPC = THIS_DIR / "orchestrator_pb2_grpc.py"


def _ensure_generated():
    # Always regenerate to ensure alignment with the current proto. Fast enough at startup.
    if not SRC_PROTO.exists():
        return

    os.makedirs(THIS_DIR, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        "-I",
        str(PROTO_DIR),
        f"--python_out={THIS_DIR}",
        f"--grpc_python_out={THIS_DIR}",
        str(SRC_PROTO),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[protos] failed to generate stubs: {exc}", file=sys.stderr)
        return

    # Patch grpc stub import to use package-qualified path.
    try:
        text = PB2_GRPC.read_text()
        patched = text.replace(
            "import orchestrator_pb2 as orchestrator__pb2",
            "from modules.speaker.protos import orchestrator_pb2 as orchestrator__pb2",
        )
        if patched != text:
            PB2_GRPC.write_text(patched)
    except Exception:
        pass


_ensure_generated()

from . import orchestrator_pb2  # noqa: E402,F401
from . import orchestrator_pb2_grpc  # noqa: E402,F401
