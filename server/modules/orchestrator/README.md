# Orchestrator (gRPC)

- Client-facing API: gRPC bidirectional stream (`Converse`)
- STT: Parakeet NIM gRPC streaming (placeholder client wiring)
- LLM: Nemotron NIM OpenAI-compatible HTTP streaming
- TTS: Custom FastAPI HTTP streaming PCM16

## Build stubs
```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. orchestrator.proto
```

## Run
```bash
cd /ai/server/modules/orchestrator
PYTHONPATH=/ai/server/modules python ../aiserver/server.py
```

## Logging
- stdout: INFO and below
- stderr: WARNING and above
- file: LOG_DIR/orchestrator.log rotated daily (keeps 14)
