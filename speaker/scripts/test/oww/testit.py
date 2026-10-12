#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from openwakeword.model import Model


def ensure_beep_wav(path: str, sr: int = 16000):
    if os.path.exists(path):
        return
    subprocess.run(
        ["sox", "-n", "-r", str(sr), "-c", "1", "-b", "16", "-e", "signed-integer",
         path, "synth", "0.12", "sine", "880", "vol", "0.2"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def play_wav(path: str):
    subprocess.run(["paplay", path], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="hey_jarvis")
    ap.add_argument("--threshold", type=float, default=0.50)
    ap.add_argument("--cooldown_s", type=float, default=1.0)
    ap.add_argument("--chunk_ms", type=int, default=80)
    ap.add_argument("--source", default=os.getenv("PULSE_SOURCE", "RDPSource"))
    ap.add_argument("--no_beep", action="store_true")
    args = ap.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        raise SystemExit(f"Model not found: {model_path}")

    sr = 16000
    chunk = int(sr * (args.chunk_ms / 1000.0))     # samples per inference
    need_bytes = chunk * 2                          # int16 mono

    oww = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
    model_key = None

    beep_path = "/tmp/oww_beep.wav"
    if not args.no_beep:
        try:
            ensure_beep_wav(beep_path, sr)
        except Exception as e:
            print(f"[warn] beep disabled (sox failed): {e}", file=sys.stderr)
            args.no_beep = True

    print(f"Model: {model_path}")
    print(f"Pulse source: {args.source}")
    print(f"Audio: parec s16le mono @ {sr}Hz")
    print(f"Chunk: {chunk} samples ({args.chunk_ms}ms)  Threshold: {args.threshold}")
    print("Ctrl+C to stop.\n")

    cmd = ["parec", "-d", args.source, "--format=s16le", "--channels=1", f"--rate={sr}", "--latency-msec=30"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    buf = bytearray()
    last_fire = 0.0

    try:
        while True:
            data = proc.stdout.read(4096)
            if not data:
                raise RuntimeError("parec produced no data (stopped).")
            buf.extend(data)

            while len(buf) >= need_bytes:
                frame = bytes(buf[:need_bytes])
                del buf[:need_bytes]

                x = np.frombuffer(frame, dtype=np.int16)
                pred = oww.predict(x)

                if model_key is None:
                    model_key = next(iter(pred.keys()))
                    print(f"[info] openWakeWord key: '{model_key}'")

                score = float(pred.get(model_key, 0.0))
                print(f"\rscore={score:0.3f} thr={args.threshold:0.2f}        ", end="", flush=True)

                now = time.time()
                if score >= args.threshold and (now - last_fire) >= args.cooldown_s:
                    last_fire = now
                    print(f"\n>>> WAKEWORD DETECTED (score={score:0.3f}) <<<")
                    if not args.no_beep:
                        play_wav(beep_path)

    except KeyboardInterrupt:
        print("\nbye 👋")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
