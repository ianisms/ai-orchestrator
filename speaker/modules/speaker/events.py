from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class WakewordEvent:
    score: float
    ts: float


@dataclass
class PlaybackCompleteEvent:
    turn_id: Optional[str]


@dataclass
class PlaybackStartEvent:
    turn_id: Optional[str]


@dataclass
class SpeechStreamSentEvent:
    turn_id: Optional[str]
    followup_count: int = 0


@dataclass
class BargeinEvent:
    pass
