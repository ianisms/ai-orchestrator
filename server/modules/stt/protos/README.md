# Riva ASR protos

`riva_asr.proto`, `riva_audio.proto`, and `riva_common.proto` are copied from
[nvidia-riva/common](https://github.com/nvidia-riva/common) (MIT license, NVIDIA
Corporation) with the `riva/proto/` import prefix flattened so
`run_orchestrator.sh` can generate `riva_asr_pb2.py` and `riva_asr_pb2_grpc.py`
into this directory at container start. The Parakeet NIM speaks this gRPC API.
