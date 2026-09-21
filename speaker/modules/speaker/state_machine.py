from __future__ import annotations

import asyncio
from enum import Enum


class SpeakerState(str, Enum):
    IDLE = "IDLE"
    WAKE_CHIME = "WAKE_CHIME"
    CAPTURING = "CAPTURING"
    WAITING_TTS = "WAITING_TTS"
    PLAYING = "PLAYING"
    ERROR = "ERROR"


class SpeakerStateMachine:
    def __init__(self) -> None:
        self._state = SpeakerState.IDLE
        self._lock = asyncio.Lock()

    @property
    def state(self) -> SpeakerState:
        return self._state

    async def transition(self, expected: set[SpeakerState], target: SpeakerState) -> bool:
        async with self._lock:
            if self._state not in expected:
                return False
            self._state = target
            return True

    async def force(self, target: SpeakerState) -> None:
        async with self._lock:
            self._state = target
