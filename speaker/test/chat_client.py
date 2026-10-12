#!/usr/bin/env python3
"""
CLI chat client:
- Sends text to orchestrator (gRPC)
- Prints streamed reply text
- Streams TTS audio and writes WAV output
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
import time
import wave

import grpc
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency
    yaml = None

DEFAULT_CONFIG_PATH = "/ai/server/config/config.yaml"
DEFAULT_TTS_OUT_DIR = "/ai/server/test/out/tts"
DEFAULT_ORCH_TARGET = "ai-server.local:7000"
ORCH_DIR = Path("/ai/speaker/modules/speaker/protos")
PROTO = ORCH_DIR / "orchestrator.proto"
GEN_DIR = Path("/ai/speaker/test/.gen")
PB2 = GEN_DIR / "orchestrator_pb2.py"
PB2_GRPC = GEN_DIR / "orchestrator_pb2_grpc.py"
HISTORY_PATH = Path("/ai/speaker/test/.chat_history")


def _read_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required to read config.yaml. Install pyyaml.")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _load_config(path: Path) -> dict:
    cfg = _read_yaml(path)
    normalized = {}
    for k, v in cfg.items():
        if isinstance(k, str):
            normalized[k.upper()] = v
    return normalized


def ensure_stubs() -> None:
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    # Always regenerate in this CLI to avoid stale proto field mismatches.
    if not PROTO.exists():
        raise FileNotFoundError(f"Missing proto: {PROTO}")

    print("gRPC stubs missing — generating from orchestrator.proto ...")
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{ORCH_DIR}",
            f"--python_out={GEN_DIR}",
            f"--grpc_python_out={GEN_DIR}",
            str(PROTO),
        ],
        check=True,
    )
    if not (PB2.exists() and PB2_GRPC.exists()):
        raise RuntimeError("Stub generation ran but orchestrator_pb2*.py still not found.")


def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def _resolve_orch_target(cfg: dict, override: str | None) -> str:
    if override:
        return override
    bind = str(cfg.get("ORCH_GRPC_BIND", DEFAULT_ORCH_TARGET)).strip()
    if bind.startswith("[::]"):
        return f"127.0.0.1:{bind.split(':')[-1]}"
    if bind.startswith("0.0.0.0"):
        return f"127.0.0.1:{bind.split(':')[-1]}"
    if bind.startswith("::"):
        return f"127.0.0.1:{bind.split(':')[-1]}"
    return bind


def main() -> int:
    parser = argparse.ArgumentParser(description="Local CLI chat client with TTS output")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--no-tts", action="store_true")
    parser.add_argument("--out-dir", default=DEFAULT_TTS_OUT_DIR)
    parser.add_argument("--orch", help="orchestrator gRPC target (host:port)")
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    args = parser.parse_args()

    ensure_stubs()
    import sys
    sys.path.insert(0, str(GEN_DIR))
    import orchestrator_pb2 as pb  # noqa: E402
    import orchestrator_pb2_grpc as pbg  # noqa: E402

    history_path = HISTORY_PATH
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_items: list[str] = []
    try:
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                history_items.append(line)
    except FileNotFoundError:
        pass
    history_items = history_items[-10:]
    history = InMemoryHistory()
    for item in history_items:
        history.append_string(item)
    session = PromptSession(history=history)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    last_wav_path: Path | None = None

    print("Type a message and press enter. Type 'quit', 'exit', or 'q' to exit.")

    while True:
        try:
            user_text = session.prompt("You> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            break
        if not user_text:
            continue
        if user_text.lower() in {"quit", "exit", "q"}:
            break
        if last_wav_path and last_wav_path.exists():
            try:
                last_wav_path.unlink()
            except Exception:
                pass
        history_items.append(user_text)
        history_items = history_items[-10:]
        history_path.write_text("\n".join(history_items) + "\n", encoding="utf-8")

        cfg = _load_config(Path(args.config))
        orch_target = _resolve_orch_target(cfg, args.orch)
        reply_buf: list[str] = []
        pcm_chunks: list[bytes] = []
        sample_rate = 22050
        printed_thinking = False

        start_field_names = {f.name for f in pb.Start.DESCRIPTOR.fields}
        has_no_tts = "no_tts" in start_field_names
        if args.no_tts and not has_no_tts:
            print("Warning: generated stubs do not include Start.no_tts; continuing without no_tts field.")

        start_kwargs = dict(
            session_id="cli",
            turn_id=f"cli-{int(time.time() * 1000)}",
            sample_rate_hz=16000,
            locale="en-US",
            request_partials=True,
        )
        if has_no_tts:
            start_kwargs["no_tts"] = bool(args.no_tts)
        req_messages = [
            pb.ClientMessage(start=pb.Start(**start_kwargs)),
            pb.ClientMessage(text=pb.TextInput(text=user_text)),
        ]
        try:
            for m in req_messages:
                _ = m.SerializeToString()
        except Exception as exc:
            print()
            print(f"Client request serialization error: {exc!r}")
            continue

        try:
            channel = grpc.insecure_channel(orch_target)
            grpc.channel_ready_future(channel).result(timeout=args.connect_timeout)
        except grpc.FutureTimeoutError:
            print()
            print(f"Orchestrator not reachable at {orch_target} (timeout {args.connect_timeout}s)")
            print("Check that the orchestrator container is running and port 7000 is published.")
            continue
        except grpc.RpcError as exc:
            print()
            print(f"Orchestrator connection error: {exc}")
            continue

        with channel:
            stub = pbg.OrchestratorStub(channel)
            responses = stub.Converse(iter(req_messages), timeout=60)

            if not printed_thinking:
                print("...thinking...", end=" ", flush=True)
                printed_thinking = True

            try:
                for msg in responses:
                    if msg.HasField("reply"):
                        if msg.reply.text:
                            print(msg.reply.text, end="", flush=True)
                            reply_buf.append(msg.reply.text)
                        if msg.reply.is_final:
                            print()
                    elif msg.HasField("audio") and not args.no_tts:
                        if msg.audio.sample_rate_hz:
                            sample_rate = msg.audio.sample_rate_hz
                        if msg.audio.pcm_s16le:
                            pcm_chunks.append(msg.audio.pcm_s16le)
                        if msg.audio.is_last:
                            break
                    elif msg.HasField("error"):
                        print()
                        print(f"Orchestrator error: {msg.error.message}")
                        break
            except grpc.RpcError as exc:
                print()
                print(f"Orchestrator RPC error: {exc}")
                continue

        reply = "".join(reply_buf).strip()
        if not reply:
            print()
            print("No reply text received.")
            continue

        if not args.no_tts and pcm_chunks:
            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = out_dir / f"chat_{ts}.wav"
            _write_wav(out_path, b"".join(pcm_chunks), sample_rate)
            print(f"Wrote TTS WAV: {out_path}")
            last_wav_path = out_path

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
