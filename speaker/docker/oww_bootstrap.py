#!/usr/bin/env python3
import os
import sys
from pathlib import Path

def main():
    try:
        from openwakeword.utils import download_models
    except Exception as e:
        print(f"[bootstrap] openwakeword import failed: {e}", file=sys.stderr)
        return 1

    cache_dir = os.environ.get("OPENWAKEWORD_HOME") or os.environ.get("OWW_MODEL_CACHE") or "/ai/speaker/.cache/openwakeword"
    os.environ["OPENWAKEWORD_HOME"] = cache_dir

    print(f"[bootstrap] OPENWAKEWORD_HOME={cache_dir}", flush=True)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    download_models()

    # Sanity check (location varies by version; we check common spots)
    expected = [
        Path(cache_dir) / "models" / "melspectrogram.onnx",
        Path(cache_dir) / "melspectrogram.onnx",
    ]
    if not any(p.exists() for p in expected):
        print("[bootstrap] WARNING: melspectrogram.onnx not found in cache after download_models()", file=sys.stderr)
        print("[bootstrap] Contents:", list(Path(cache_dir).rglob("*.onnx"))[:20], file=sys.stderr)

    print("[bootstrap] Done.", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
