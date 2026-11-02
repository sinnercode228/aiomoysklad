from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


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


def run_in_new_loop(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` in a loop of its own, as a script calling asyncio.run() would.

    asyncio.run() itself is not used: it unsets the current loop, and pytest-asyncio 0.24 then
    creates one at teardown that it never closes.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
