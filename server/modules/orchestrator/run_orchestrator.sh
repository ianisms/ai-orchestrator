#!/usr/bin/env sh
set -eu

cd /ai/server/modules/orchestrator

gen_orch_protos() {
  # Generate protos into a writable temp dir (avoids bind-mount permission weirdness)
  GEN=/tmp/gen
  rm -rf "$GEN"
  mkdir -p "$GEN"

  python -m grpc_tools.protoc -I . \
    --python_out="$GEN" \
    --grpc_python_out="$GEN" \
    orchestrator.proto

  echo "$GEN"
}

gen_stt_protos_once() {
  PROTO=../stt/protos/riva_asr.proto
  OUT_PY=../stt/protos/riva_asr_pb2.py
  OUT_GRPC=../stt/protos/riva_asr_pb2_grpc.py

  if [ ! -f "$OUT_PY" ] || [ ! -f "$OUT_GRPC" ] || [ "$PROTO" -nt "$OUT_PY" ] || [ "$PROTO" -nt "$OUT_GRPC" ]; then
    python -m grpc_tools.protoc -I ../stt/protos \
      --python_out=../stt/protos \
      --grpc_python_out=../stt/protos \
      "$PROTO"
  fi
}

run_server() {
  GEN="$(gen_orch_protos)"
  PYTHONPATH="$GEN:/ai/server/modules:/ai/server/modules/orchestrator:${PYTHONPATH:-}" \
    ORCH_GEN_PATH="$GEN" \
    exec python ../aiserver/server.py
}

if [ "${1:-}" = "--once" ]; then
  run_server
fi

gen_stt_protos_once

hot_reload="$(
  PYTHONPATH="/ai/server/modules:/ai/server/modules/orchestrator:${PYTHONPATH:-}" \
    python - <<'PY'
from config.settings import settings
print("1" if bool(settings.ORCH_HOT_RELOAD) else "0")
PY
)"

if [ "$hot_reload" != "1" ]; then
  run_server
fi

# Hot reload on any change under /ai/server/modules (ignore log + cache churn)
IGNORE_PATHS="/ai/server/modules/orchestrator/logs,/ai/server/state,__pycache__"
exec watchfiles --ignore-paths "$IGNORE_PATHS" \
  "sh -lc '/ai/server/modules/orchestrator/run_orchestrator.sh --once'" /ai/server/modules
