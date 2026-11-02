"""The weighted 3-second basket.

``_restrictions.md § Лимит запросов за 3-секундный период``: every user and every solution has
a basket of 45 units "handed out every 3 seconds", and each request takes its weight out of it
(see :mod:`aiomoysklad._weights`). Going over is answered with 429. The docs do not say when the
3-second period starts, so the limiter assumes the stricter sliding window.
"""

from __future__ import annotations

import asyncio
import math
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable

from ._weights import CAPACITY, WINDOW

__all__ = ["RateLimiter"]


class RateLimiter:
    """A basket of ``capacity`` units in which each spent unit comes back ``window`` s later.

    That keeps the units spent in any ``window``-second span at or under ``capacity``,
    however the server aligns its period, and still allows ``capacity`` units per ``window``
    on average.

    Callers are served in arrival order: a 1-unit request waits behind a 5-unit stock report
    instead of starving it. A cancelled caller leaves the queue without spending anything.

    One limiter can be passed to several clients that use the same credentials, within one
    event loop at a time. Since 16.02.2026 the limit is per user and per solution, not per
    account, so clients with different credentials should each have their own.

    Args:
        capacity: Units in the basket.
        window: Seconds after which a spent unit comes back.
        clock: Monotonic clock in seconds; replaceable in tests.
        sleep: Coroutine function that sleeps for the given seconds; replaceable in tests.
    """

    def __init__(
        self,
        capacity: int = CAPACITY,
        window: float = WINDOW,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if capacity < 1:
            msg = "capacity must be at least 1 unit"
            raise ValueError(msg)
        if not math.isfinite(window) or window <= 0:
            msg = "window must be a positive number of seconds"
            raise ValueError(msg)
        self._capacity = int(capacity)
        self._window = float(window)
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()  # (when, units), oldest first
        self._used = 0
        self._paused_until = -math.inf
        self._queue: asyncio.Lock | None = None
        self._queue_loop: weakref.ref[asyncio.AbstractEventLoop] | None = None

    @property
    def capacity(self) -> int:
        """Units in the basket."""
        return self._capacity

    @property
    def window(self) -> float:
        """Seconds after which a spent unit comes back."""
        return self._window

    def __repr__(self) -> str:
        return f"RateLimiter(capacity={self._capacity}, window={self._window})"

    def available(self) -> int:
        """Units that could be spent right now, ignoring a pause."""
        self._expire(self._clock())
        return self._capacity - self._used

    def pause(self, seconds: float) -> None:
        """Hold every caller for ``seconds`` from now; a shorter pause never cuts a longer one."""
        if not seconds >= 0:
            msg = "pause must be a non-negative number of seconds"
            raise ValueError(msg)
        self._paused_until = max(self._paused_until, self._clock() + seconds)

    async def acquire(self, weight: int = 1) -> None:
        """Wait until ``weight`` units are free, then spend them.

        Raises:
            ValueError: ``weight`` is not between 1 and :attr:`capacity`.
        """
        if not 1 <= weight <= self._capacity:
            msg = f"weight must be between 1 and {self._capacity}, got {weight}"
            raise ValueError(msg)
        # The lock is a fair FIFO queue, so waiters are served in arrival order.
        async with self._queue_of_running_loop():
            while True:
                now = self._clock()
                delay = self._delay(now, weight)
                if delay <= 0:
                    self._spend(now, weight)
                    return
                await self._sleep(delay)

    def _expire(self, now: float) -> None:
        spent = self._spent
        while spent and spent[0][0] + self._window <= now:
            self._used -= spent.popleft()[1]

    def _spend(self, now: float, units: int) -> None:
        self._spent.append((now, units))
        self._used += units

    def _delay(self, now: float, weight: int) -> float:
        if self._paused_until > now:
            return self._paused_until - now
        self._expire(now)
        missing = self._used + weight - self._capacity
        if missing <= 0:
            return 0.0
        returned = 0
        for when, units in self._spent:
            returned += units
            if returned >= missing:
                return when + self._window - now
        return self._window  # unreachable while weight <= capacity

    def _queue_of_running_loop(self) -> asyncio.Lock:
        # An asyncio.Lock belongs to the loop it first waited in; a limiter that outlives its
        # loop (one client, several asyncio.run() calls) gets a fresh queue in the next one.
        loop = asyncio.get_running_loop()
        if self._queue is None or self._queue_loop is None or self._queue_loop() is not loop:
            self._queue = asyncio.Lock()
            self._queue_loop = weakref.ref(loop)
        return self._queue
