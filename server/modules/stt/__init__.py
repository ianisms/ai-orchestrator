from pathlib import Path
import sys

proto_dir = Path(__file__).resolve().parent / "protos"
proto_path = str(proto_dir)
if proto_path not in sys.path:
    sys.path.insert(0, proto_path)

from .stream import STTStream
from .parakeet_grpc import ParakeetGrpcClient, ParakeetConfig
