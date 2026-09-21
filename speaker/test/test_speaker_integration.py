from __future__ import annotations

import asyncio
import math
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.speaker.events_bus import EventBus
from modules.speaker.state_machine import SpeakerState, SpeakerStateMachine
from modules.speaker.vad import EnergyVAD, VADConfig


def _pcm_sine(sample_rate: int, seconds: float, freq: float = 220.0, amp: float = 0.2) -> bytes:
    n = int(sample_rate * seconds)
    out = bytearray()
    for i in range(n):
        v = int(max(-1.0, min(1.0, amp * math.sin(2.0 * math.pi * freq * i / sample_rate))) * 32767)
        out.extend(int(v).to_bytes(2, "little", signed=True))
    return bytes(out)


def _pcm_silence(sample_rate: int, seconds: float) -> bytes:
    return b"\x00\x00" * int(sample_rate * seconds)


def test_vad_adaptive_and_calibration_persistence():
    with tempfile.TemporaryDirectory() as td:
        cal = Path(td) / "vad.json"
        cfg = VADConfig(
            frame_ms=20,
            sample_rate_hz=16000,
            rms_threshold=120.0,
            adaptive_enable=True,
            calibration_path=str(cal),
        )
        vad = EnergyVAD(cfg)
        silence = _pcm_silence(16000, 0.02)
        for _ in range(50):
            _ = vad.is_speech(silence)
        vad.save_calibration()
        assert cal.exists()

        vad2 = EnergyVAD(cfg)
        assert vad2._noise_floor >= 0.0  # smoke check load path


def test_vad_detects_speech_fixture():
    cfg = VADConfig(frame_ms=20, sample_rate_hz=16000, rms_threshold=100.0, adaptive_enable=False)
    vad = EnergyVAD(cfg)
    speech = _pcm_sine(16000, 0.02, amp=0.35)
    silence = _pcm_silence(16000, 0.02)
    assert vad.is_speech(speech) is True
    assert vad.is_speech(silence) is False


def test_state_machine_transitions():
    sm = SpeakerStateMachine()

    async def _run():
        ok = await sm.transition({SpeakerState.IDLE}, SpeakerState.WAKE_CHIME)
        assert ok
        ok = await sm.transition({SpeakerState.CAPTURING}, SpeakerState.PLAYING)
        assert not ok
        await sm.force(SpeakerState.CAPTURING)
        ok = await sm.transition({SpeakerState.CAPTURING}, SpeakerState.WAITING_TTS)
        assert ok
        assert sm.state == SpeakerState.WAITING_TTS

    asyncio.run(_run())


def test_event_bus_topics_isolated():
    bus = EventBus(maxsize=8)
    got_control = []
    got_playback = []
    got_wild = []

    async def _run():
        async def _consume(topic, sink, limit=1):
            i = 0
            async for evt in bus.subscribe(topic=topic):
                sink.append(evt)
                i += 1
                if i >= limit:
                    break

        t1 = asyncio.create_task(_consume("control", got_control))
        t2 = asyncio.create_task(_consume("playback", got_playback))
        t3 = asyncio.create_task(_consume("*", got_wild, limit=2))
        await asyncio.sleep(0)
        await bus.publish({"k": "c"}, topic="control")
        await bus.publish({"k": "p"}, topic="playback")
        await asyncio.gather(t1, t2, t3)

    asyncio.run(_run())
    assert got_control == [{"k": "c"}]
    assert got_playback == [{"k": "p"}]
    assert got_wild == [{"k": "c"}, {"k": "p"}]
