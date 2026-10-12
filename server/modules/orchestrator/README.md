# Orchestrator (gRPC)

- Client-facing API: gRPC bidirectional stream (`Converse`)
- STT: Parakeet CTC 0.6B en-US NIM, streaming over the Riva ASR gRPC API (`server/modules/stt/parakeet_grpc.py`), interim results on
- LLM: Qwen3 on vLLM, OpenAI-compatible HTTP streaming
- TTS: Custom FastAPI service (NeMo FastPitch and HiFi-GAN), HTTP streaming PCM16

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
