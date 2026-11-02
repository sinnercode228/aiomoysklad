"""The guard against concurrent callers: requests that queue are checked again when sent."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from aiomoysklad import (
    AuthKind,
    BearerToken,
    ErrorGuard,
    GuardTrippedError,
    MoySklad,
    NetworkError,
    RateLimiter,
    RetryPolicy,
)

from .clock import FakeClock
from .conftest import TODAY, TOKEN

PRODUCT = "/entity/product/7944ef04-f831-11e5-7a69-971500188b19"


def client(clock: FakeClock, **kwargs: object) -> MoySklad:
    return MoySklad(
        BearerToken(TOKEN, kind=AuthKind.SOLUTION),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        guard=ErrorGuard(clock=clock),
        today=lambda: TODAY,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_gathered_identical_failures_stop_at_the_guard(
    clock: FakeClock, mock: respx.MockRouter
) -> None:
    # _restrictions.md § Автоматическое отключение доступа: more than 200 identical failing
    # requests in a minute. A check made only before they queue on the cap of 5 would let all
    # 400 through.
    error = (
        "Объект с типом 'product' и идентификатором "
        "'7944ef04-f831-11e5-7a69-971500188b19' не найден"
    )
    route = mock.get(PRODUCT).respond(404, json={"errors": [{"error": error, "code": 1021}]})
    async with client(clock) as ms:
        results = await asyncio.gather(
            *(ms.get(PRODUCT) for _ in range(400)), return_exceptions=True
        )
        assert route.call_count <= ms.guard.identical_errors
        assert sum(isinstance(r, GuardTrippedError) for r in results) == 400 - route.call_count
    assert clock.now - 1000.0 < 60.0


async def test_gathered_429s_stop_at_the_guard(clock: FakeClock, mock: respx.MockRouter) -> None:
    # Error 1073: another process holds the parallel cap, so every request is answered 429.
    route = mock.get(path__startswith="/api/remap/1.2/entity/product/").respond(
        429,
        json={
            "errors": [
                {
                    "error": "Превышено ограничение на одновременное количество запросов",
                    "code": 1073,
                }
            ]
        },
    )
    async with client(clock, retry=RetryPolicy(max_retries=0)) as ms:
        await asyncio.gather(
            *(ms.get(f"/entity/product/{n:08x}-f831-11e5-7a69-971500188b19") for n in range(400)),
            return_exceptions=True,
        )
        assert route.call_count <= ms.guard.too_many_requests
    assert clock.now - 1000.0 < 60.0


async def test_puts_are_counted_when_they_leave_not_when_they_queue(
    clock: FakeClock, mock: respx.MockRouter
) -> None:
    sent_at: list[float] = []

    def answer(request: object) -> respx.MockResponse:
        sent_at.append(clock.now)
        return respx.MockResponse(200, json={})

    mock.put(PRODUCT).mock(side_effect=answer)
    async with client(clock) as ms:
        ms.limiter.pause(59.0)  # as after X-Lognex-Reset on a 429
        first = await asyncio.gather(
            *(ms.request("PUT", PRODUCT, json={"n": n}) for n in range(75)),
            return_exceptions=True,
        )
        assert not [r for r in first if isinstance(r, BaseException)]
        assert sent_at[0] >= 1059.0
        clock.now = 1060.5  # the first batch was queued more than a minute ago
        second = await asyncio.gather(
            *(ms.request("PUT", PRODUCT, json={"n": n}) for n in range(75)),
            return_exceptions=True,
        )
        assert all(isinstance(r, GuardTrippedError) for r in second)
    in_a_minute = [t for t in sent_at if t < sent_at[0] + 60.0]
    assert len(in_a_minute) == 75


async def test_a_request_that_gets_no_answer_frees_its_reservation(
    clock: FakeClock, mock: respx.MockRouter
) -> None:
    mock.get(PRODUCT).mock(side_effect=httpx.ReadTimeout("slow"))
    guard = ErrorGuard(too_many_requests=1, clock=clock)
    async with MoySklad(
        BearerToken(TOKEN, kind=AuthKind.SOLUTION),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        guard=guard,
        today=lambda: TODAY,
    ) as ms:
        for _ in range(3):
            with pytest.raises(NetworkError, match="ReadTimeout"):
                await ms.get(PRODUCT)
    assert guard.in_flight() == 0


async def test_a_cancelled_request_frees_its_reservation(clock: FakeClock) -> None:
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover

    guard = ErrorGuard(clock=clock)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(hang)) as http,
        MoySklad(BearerToken(TOKEN, kind=AuthKind.SOLUTION), guard=guard, http=http) as ms,
    ):
        task = asyncio.create_task(ms.get(PRODUCT))
        await started.wait()
        assert guard.in_flight() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert guard.in_flight() == 0
