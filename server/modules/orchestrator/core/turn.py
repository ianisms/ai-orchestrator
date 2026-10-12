import asyncio
import uuid
from dataclasses import dataclass

def new_turn_id() -> str:
    return uuid.uuid4().hex[:10]

@dataclass
class CancelToken:
    event: asyncio.Event
    def cancel(self) -> None:
        self.event.set()
    def cancelled(self) -> bool:
        return self.event.is_set()
