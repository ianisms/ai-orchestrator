#!/usr/bin/env python3
"""
Orchestrator smoke test (gRPC):
- Streams a WAV as PCM16 mono 16k frames into Orchestrator gRPC
- Prints transcripts + state transitions
- Writes returned TTS PCM stream to a WAV via ffmpeg
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time
import grpc

ORCH_DIR = Path("/ai/server/modules/orchestrator")
PROTO = ORCH_DIR / "orchestrator.proto"
PB2 = ORCH_DIR / "orchestrator_pb2.py"
PB2_GRPC = ORCH_DIR / "orchestrator_pb2_grpc.py"


def ensure_stubs():
    if PB2.exists() and PB2_GRPC.exists():
        try:
            proto_mtime = PROTO.stat().st_mtime
            if proto_mtime <= PB2.stat().st_mtime and proto_mtime <= PB2_GRPC.stat().st_mtime:
                return
        except FileNotFoundError:
            return
    if not PROTO.exists():
        raise FileNotFoundError(f"Missing proto: {PROTO}")

    print("🛠️  gRPC stubs missing — generating from orchestrator.proto ...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{ORCH_DIR}",
            f"--python_out={ORCH_DIR}",
            f"--grpc_python_out={ORCH_DIR}",
            str(PROTO),
        ],
        check=True,
    )
    if not (PB2.exists() and PB2_GRPC.exists()):
        raise RuntimeError("Stub generation ran but orchestrator_pb2*.py still not found.")


sys.path.insert(0, str(ORCH_DIR))
ensure_stubs()

import orchestrator_pb2 as pb  # noqa: E402
import orchestrator_pb2_grpc as pbg  # noqa: E402


def _iter_client_messages(
    wav_path: Path,
    frame_ms: int = 20,
    *,
    tail_silence_ms: int = 200,
    limit_seconds: float | None = None,   # set e.g. 4.0 to only send first 4 seconds
):
    """Streams WAV audio as PCM16 mono 16k frames."""
    yield pb.ClientMessage(
        start=pb.Start(
            session_id="smoke",
            sample_rate_hz=16000,
            locale="en-US",
            request_partials=True,
        )
    )

    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-v", "error",
            "-i", str(wav_path),
            "-f", "s16le",
            "-ac", "1",
            "-ar", "16000",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    bytes_per_frame = int(16000 * (frame_ms / 1000.0) * 2)

    t0 = time.time()
    try:
        while True:
            if limit_seconds is not None and (time.time() - t0) >= limit_seconds:
                break

            assert proc.stdout is not None
            chunk = proc.stdout.read(bytes_per_frame)
            if not chunk:
                break

            yield pb.ClientMessage(
                audio=pb.AudioFrame(
                    pcm_s16le=chunk,
                    timestamp_ms=int(time.time() * 1000),
                )
            )
            time.sleep(frame_ms / 1000.0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Tiny tail of silence helps STT finalize
    if tail_silence_ms > 0:
        tail_bytes = int(16000 * (tail_silence_ms / 1000.0) * 2)
        yield pb.ClientMessage(
            audio=pb.AudioFrame(
                pcm_s16le=b"\x00" * tail_bytes,
                timestamp_ms=int(time.time() * 1000),
            )
        )

    # End-of-utterance — let server finish STT → LLM → TTS
    yield pb.ClientMessage(eou=pb.EndOfUtterance())
    yield pb.ClientMessage(cancel=pb.Cancel(reason="smoke test done"))



def main():
    test_wavs = []

    for i in range(2, 4):
        wav_path = Path(f"/ai/server/stt/wavs/Recording16k_chunks/Recording16k_chunk_0{i}.wav")
        if not wav_path.exists():
            raise FileNotFoundError(f"Missing input wav: {wav_path}")
        test_wavs.append(wav_path)

    for wav_in in test_wavs:
        print(f"\n🎤 Processing test WAV: {wav_in}\n{'-'*60}")

        out_wav = Path(f"/ai/server/stt/wavs/Recording16k_chunks/{wav_in.name.replace('.wav', '_orch_out.wav')}")
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        if out_wav.exists():
            out_wav.unlink()

        ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-f", "s16le",
                "-ar", "22050",   # will restart once SR is known
                "-ac", "1",
                "-i", "pipe:0",
                "-c:a", "pcm_s16le",
                str(out_wav),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        got_sr = False
        total_audio_bytes = 0

        with grpc.insecure_channel("127.0.0.1:7000") as channel:
            # Make sure we don’t hang forever if server-side is stuck
            grpc.channel_ready_future(channel).result(timeout=5)

            stub = pbg.OrchestratorStub(channel)

            # Deadline on the stream (seconds). Tune up if needed.
            responses = stub.Converse(
                _iter_client_messages(
                    wav_in,
                    frame_ms=20,
                    tail_silence_ms=200,
                    limit_seconds=None,  # set to e.g. 3.5 if your WAV has many utterances
                ),
                timeout=60,
            )

            try:
                for msg in responses:
                    if msg.HasField("state"):
                        print(f"🧭 STATE  phase={pb.State.Phase.Name(msg.state.phase)} turn_id={msg.state.turn_id}")

                    elif msg.HasField("transcript"):
                        tag = "FINAL" if msg.transcript.is_final else "PART"
                        print(f"📝 {tag:<5} turn_id={msg.transcript.turn_id}  {msg.transcript.text}")

                    elif msg.HasField("audio"):
                        if not got_sr and msg.audio.sample_rate_hz:
                            sr = msg.audio.sample_rate_hz
                            got_sr = True
                            if sr != 22050:
                                if ffmpeg_proc.stdin:
                                    ffmpeg_proc.stdin.close()
                                ffmpeg_proc.wait()

                                ffmpeg_proc = subprocess.Popen(
                                    [
                                        "ffmpeg",
                                        "-y",
                                        "-f", "s16le",
                                        "-ar", str(sr),
                                        "-ac", "1",
                                        "-i", "pipe:0",
                                        "-c:a", "pcm_s16le",
                                        str(out_wav),
                                    ],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                                print(f"🔊 Using TTS sample rate: {sr}")

                        if msg.audio.pcm_s16le:
                            total_audio_bytes += len(msg.audio.pcm_s16le)
                            assert ffmpeg_proc.stdin is not None
                            ffmpeg_proc.stdin.write(msg.audio.pcm_s16le)

                        if msg.audio.is_last:
                            print("✅ AUDIO done (is_last=true)")
                            break

                    elif msg.HasField("error"):
                        raise RuntimeError(f"Orchestrator error: {msg.error.message}")

            finally:
                if ffmpeg_proc.stdin:
                    ffmpeg_proc.stdin.close()
                rc = ffmpeg_proc.wait()
                if rc != 0:
                    raise subprocess.CalledProcessError(rc, ffmpeg_proc.args)

        print(f"\n📊 Total audio bytes written: {total_audio_bytes}")
        print(f"✅ Wrote orchestrator output to: {out_wav}")


if __name__ == "__main__":
    main()
