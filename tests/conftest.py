from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest
import respx

import aiomoysklad._client
from aiomoysklad import AuthKind, BearerToken, ErrorGuard, MoySklad, RateLimiter

from .clock import FakeClock

API = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = "0cbfc512618efa7d5fa306250bca064c1169b37c"  # the example of _general.md
TODAY = date(2026, 11, 2)  # user tokens cost 3 units, solutions 1


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A fake clock that the limiter, the guard and the retry waits all run on."""
    fake = FakeClock(now=1000.0)
    monkeypatch.setattr(aiomoysklad._client, "_sleep", fake.sleep)
    return fake


@pytest.fixture
async def ms(clock: FakeClock) -> AsyncIterator[MoySklad]:
    async with MoySklad(
        BearerToken(TOKEN, kind=AuthKind.SOLUTION),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        guard=ErrorGuard(clock=clock),
        today=lambda: TODAY,
    ) as client:
        yield client
