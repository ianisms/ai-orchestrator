from __future__ import annotations

from typing import Awaitable, Callable

SendEvent = Callable[[str, dict], Awaitable[None]]
