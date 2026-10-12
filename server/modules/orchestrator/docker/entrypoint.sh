#!/bin/sh
set -eux

cd /ai/server/modules/orchestrator

python -m grpc_tools.protoc -I . \
  --python_out=. \
  --grpc_python_out=. \
  orchestrator.proto

python -m grpc_tools.protoc -I ../stt/protos \
  --python_out=../stt/protos \
  --grpc_python_out=../stt/protos \
  ../stt/protos/riva_asr.proto

export PYTHONPATH="/ai/server/modules:/ai/server/modules/orchestrator:${PYTHONPATH:-}"
exec python ../aiserver/server.py
