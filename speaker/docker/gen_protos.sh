#!/usr/bin/env bash
set -euo pipefail

SRC_DIR=/ai/speaker/modules/speaker/protos
OUT_DIR=/ai/speaker/modules/speaker/protos

mkdir -p "$OUT_DIR"
python -m grpc_tools.protoc \
  -I "$SRC_DIR" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$SRC_DIR/orchestrator.proto"

# Ensure generated gRPC stubs import the packaged module path
python - <<PY
from pathlib import Path
p = Path("${OUT_DIR}/orchestrator_pb2_grpc.py")
text = p.read_text()
patched = text.replace(
    "import orchestrator_pb2 as orchestrator__pb2",
    "from modules.speaker.protos import orchestrator_pb2 as orchestrator__pb2",
)
if patched != text:
    p.write_text(patched)
PY
touch "$OUT_DIR/__init__.py"

ls -l /ai/speaker/modules/speaker/protos/*pb2*.py
