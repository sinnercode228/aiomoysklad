from __future__ import annotations

import asyncio


class FakeClock:
    """A monotonic clock that only moves when somebody sleeps on it."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        wake_at = self.now + delay
        await asyncio.sleep(0)  # let other callers run, as a real sleep would
        self.now = max(self.now, wake_at)
