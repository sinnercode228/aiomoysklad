"""Stop sending before MoySklad cuts off API access.

``_restrictions.md § Автоматическое отключение доступа``: MoySklad turns off a user's API
access after, within the last hour, more than 200 identical requests in a minute that ended in
an error, more than 200 requests in a minute answered 429, or more than 100 PUT requests to the
same entity in a minute. Getting access back takes a message to MoySklad support, so the guard
fails closed: it refuses to send instead of waiting.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ._errors import GuardTrippedError

__all__ = ["ErrorGuard", "GuardKey"]

# The documented cut-offs, per minute ("more than" these).
IDENTICAL_ERRORS_CUTOFF = 200
TOO_MANY_REQUESTS_CUTOFF = 200
PUTS_PER_ENTITY_CUTOFF = 100

_MINUTE = 60.0
_ERROR_FIRST = 400
_TOO_MANY_REQUESTS = 429


@dataclass(frozen=True)
class GuardKey:
    """What the guard remembers of one request between sending it and its answer."""

    method: str
    path: str
    identity: str


def _stop(name: str, value: int, cutoff: int) -> int:
    if not 1 <= value <= cutoff:
        msg = f"{name} must be between 1 and the documented {cutoff}, got {value}"
        raise ValueError(msg)
    return int(value)


class ErrorGuard:
    """Rolling one-minute counters that refuse a request before a documented cut-off.

    =========================  =======================  ============
    Counter                    Documented cut-off       Default stop
    =========================  =======================  ============
    identical failing requests more than 200 per minute 150
    answers 429                more than 200 per minute 150
    PUTs to one entity         more than 100 per minute 75
    =========================  =======================  ============

    A request is identical to another when its method, URL with the query string and body are
    the same, and it failed when it was answered with a 4xx or 5xx status. A PUT is counted
    when it is sent, whatever the answer. The client consults the guard before the concurrency
    cap and the rate limiter, so a refused request spends nothing. Share one guard between the
    clients of one user or solution, like the rate limiter.

    Args:
        identical_errors: Stop for identical failing requests per minute, at most 200.
        too_many_requests: Stop for 429 answers per minute, at most 200.
        puts_per_entity: Stop for PUTs to one entity per minute, at most 100.
        clock: Monotonic clock in seconds; replaceable in tests.
    """

    def __init__(
        self,
        *,
        identical_errors: int = 150,
        too_many_requests: int = 150,
        puts_per_entity: int = 75,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._identical_stop = _stop("identical_errors", identical_errors, IDENTICAL_ERRORS_CUTOFF)
        self._limited_stop = _stop("too_many_requests", too_many_requests, TOO_MANY_REQUESTS_CUTOFF)
        self._put_stop = _stop("puts_per_entity", puts_per_entity, PUTS_PER_ENTITY_CUTOFF)
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._puts: dict[str, deque[float]] = {}
        self._limited: deque[float] = deque()
        self._last_sweep = -math.inf

    @property
    def identical_errors(self) -> int:
        """Stop for identical failing requests per minute."""
        return self._identical_stop

    @property
    def too_many_requests(self) -> int:
        """Stop for 429 answers per minute."""
        return self._limited_stop

    @property
    def puts_per_entity(self) -> int:
        """Stop for PUTs to one entity per minute."""
        return self._put_stop

    def __repr__(self) -> str:
        return (
            f"ErrorGuard(identical_errors={self._identical_stop}, "
            f"too_many_requests={self._limited_stop}, puts_per_entity={self._put_stop})"
        )

    def tracked_keys(self) -> int:
        """Requests and entities with events in the last minute; for monitoring and tests."""
        return len(self._failures) + len(self._puts)

    def before_send(self, method: str, url: str, *, entity: str, body: bytes | None) -> GuardKey:
        """Check a request about to be sent, and count it if it is a PUT.

        Args:
            method: The HTTP method.
            url: The full URL with its query string.
            entity: The path of the entity, without the query string; PUTs are counted by it.
            body: The request body as sent.

        Returns:
            The key to pass to :meth:`record` with the answer.

        Raises:
            GuardTrippedError: Sending would come close to a cut-off; nothing was counted.
        """
        method = method.upper()
        now = self._clock()
        digest = hashlib.sha256(body or b"").hexdigest()
        key = GuardKey(method, httpx.URL(url).path, f"{method} {url} {digest}")

        self._trim(self._limited, now)
        self._check("too_many_requests", self._limited, self._limited_stop, now, key)
        failures = self._failures.get(key.identity)
        if failures is not None:
            self._trim(failures, now)
            self._check("identical_errors", failures, self._identical_stop, now, key)
        if method == "PUT":
            puts = self._puts.setdefault(entity, deque())
            self._trim(puts, now)
            self._check("puts_per_entity", puts, self._put_stop, now, key)
            puts.append(now)
        return key

    def record(self, key: GuardKey, status: int) -> None:
        """Count the answer to a request let through by :meth:`before_send`."""
        now = self._clock()
        if status >= _ERROR_FIRST:
            self._failures.setdefault(key.identity, deque()).append(now)
        if status == _TOO_MANY_REQUESTS:
            self._limited.append(now)
        if now - self._last_sweep >= _MINUTE:
            self._sweep(now)

    @staticmethod
    def _trim(events: deque[float], now: float) -> None:
        while events and events[0] + _MINUTE <= now:
            events.popleft()

    def _sweep(self, now: float) -> None:
        self._last_sweep = now
        for table in (self._failures, self._puts):
            for name in list(table):
                events = table[name]
                self._trim(events, now)
                if not events:
                    del table[name]

    @staticmethod
    def _check(counter: str, events: deque[float], stop: int, now: float, key: GuardKey) -> None:
        if len(events) < stop:
            return
        retry_after = events[0] + _MINUTE - now
        msg = (
            f"{counter}: {len(events)} in the last minute reached the stop of {stop}; "
            f"refusing to send {key.method} {key.path} for {retry_after:.1f} s "
            "(_restrictions.md § Автоматическое отключение доступа)"
        )
        raise GuardTrippedError(
            msg,
            counter=counter,
            limit=stop,
            retry_after=retry_after,
            method=key.method,
        )
