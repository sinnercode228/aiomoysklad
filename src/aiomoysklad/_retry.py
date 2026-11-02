"""The rate limit headers, and when and how long the client waits before sending again."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["RateHeaders", "RetryPolicy", "has_sync_ids"]

# Methods that do the same thing when sent twice.
_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})


def _count(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    return int(text)


def _millis(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number / 1000.0


@dataclass(frozen=True)
class RateHeaders:
    """The rate limit headers of one answer, from ``_general.md § Обработка ошибок``.

    The docs give the times in milliseconds; they are converted to seconds here. Each field is
    ``None`` when the header is missing or unreadable.

    Attributes:
        limit: ``X-RateLimit-Limit``, "requests that can be made evenly during the interval
            before a 429".
        remaining: ``X-RateLimit-Remaining``, "requests that can be sent before a 429".
        interval: ``X-Lognex-Retry-TimeInterval``, the interval of ``limit``, in seconds.
        reset: ``X-Lognex-Reset``, seconds until the limit resets; 0 when no limit is set.
        retry_after: ``X-Lognex-Retry-After``, seconds until the limit resets.
    """

    limit: int | None = None
    remaining: int | None = None
    interval: float | None = None
    reset: float | None = None
    retry_after: float | None = None

    @classmethod
    def from_headers(cls, headers: httpx.Headers | Mapping[str, str]) -> RateHeaders:
        """Read the headers of an answer."""
        if not isinstance(headers, httpx.Headers):
            headers = httpx.Headers(headers)
        return cls(
            limit=_count(headers.get("X-RateLimit-Limit")),
            remaining=_count(headers.get("X-RateLimit-Remaining")),
            interval=_millis(headers.get("X-Lognex-Retry-TimeInterval")),
            reset=_millis(headers.get("X-Lognex-Reset")),
            retry_after=_millis(headers.get("X-Lognex-Retry-After")),
        )


def has_sync_ids(body: Any) -> bool:
    """Tell whether every object of a create body carries a ``syncId``.

    ``_general.md § Назначение поля syncId``: when a create is sent again with the same
    ``syncId``, the answer is the object created the first time instead of a duplicate.
    """
    items = body if isinstance(body, list) else [body]
    return bool(items) and all(
        isinstance(item, Mapping) and isinstance(item.get("syncId"), str) and item["syncId"]
        for item in items
    )


@dataclass(frozen=True)
class RetryPolicy:
    """Retry settings for one client.

    What is retried, up to ``max_retries`` times:

    * 429, and 502, 503 and 504 ("service temporarily unavailable", "temporarily disabled",
      "timeout, try again later" in ``_general.md § Обработка ошибок``). 500 is not retried:
      the same failing request sent again counts toward the automatic cut-off of
      ``_restrictions.md § Автоматическое отключение доступа``.
    * Connection failures where nothing was sent, for every method.
    * Other network errors, for the methods :meth:`may_resend` allows.

    Statuses are retried only for :meth:`may_resend` methods too: ``GET``, ``HEAD``, ``PUT``
    and ``DELETE``, and a ``POST`` whose every object carries a ``syncId``. The docs do not
    say that a 429 means the request was not processed, so any other write is resent only
    with ``retry_unsafe_writes=True``.

    The wait is ``X-Lognex-Retry-After``, else ``X-Lognex-Reset``, else exponential backoff
    with jitter: retry *n* waits a random time between half and all of
    ``min(backoff_max, backoff_base * 2 ** (n - 1))``. A wait longer than ``max_wait`` is not
    slept; the error is raised with ``retry_after`` set instead.

    Attributes:
        max_retries: Retries after the first attempt; ``0`` turns retries off.
        max_wait: Longest wait, in seconds, the client sleeps before a retry.
        backoff_base: Upper bound of the first backoff, in seconds.
        backoff_max: Cap on the backoff, in seconds.
        retry_unsafe_writes: Also resend ``POST`` without ``syncId``, and ``PATCH``.
        retry_statuses: HTTP statuses that are retried.
    """

    max_retries: int = 3
    max_wait: float = 60.0
    backoff_base: float = 0.5
    backoff_max: float = 10.0
    retry_unsafe_writes: bool = False
    retry_statuses: frozenset[int] = field(default=frozenset({429, 502, 503, 504}))

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            msg = "max_retries must not be negative"
            raise ValueError(msg)
        for name in ("max_wait", "backoff_base", "backoff_max"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                msg = f"{name} must be a non-negative number of seconds"
                raise ValueError(msg)

    def may_resend(self, method: str, body: Any) -> bool:
        """Tell whether a request with this method and JSON body may be sent twice."""
        method = method.upper()
        if method in _IDEMPOTENT or self.retry_unsafe_writes:
            return True
        return method == "POST" and has_sync_ids(body)

    def backoff(self, retry: int, rng: random.Random) -> float:
        """Return the jittered backoff before retry number ``retry`` (1-based)."""
        try:
            doubled = math.ldexp(self.backoff_base, max(0, retry - 1))
        except OverflowError:
            doubled = math.inf
        ceiling = min(self.backoff_max, doubled)
        return rng.uniform(ceiling / 2, ceiling)

    def delay(self, rate: RateHeaders, retry: int, rng: random.Random) -> float:
        """Return the wait before retry number ``retry``, from the headers or the backoff."""
        for seconds in (rate.retry_after, rate.reset):
            if seconds is not None and seconds > 0:
                return seconds
        return self.backoff(retry, rng)
