from __future__ import annotations

import asyncio

import pytest

from aiomoysklad import ConcurrencyLimit

from .clock import run_in_new_loop


async def run_many(limit: ConcurrencyLimit, count: int) -> int:
    peak = 0

    async def call() -> None:
        nonlocal peak
        async with limit:
            peak = max(peak, limit.in_flight)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    await asyncio.gather(*(call() for _ in range(count)))
    return peak


async def test_no_more_than_five_requests_are_in_flight_by_default() -> None:
    # _restrictions.md § Лимиты и ограничения в JSON API: no more than 5 parallel requests
    # from one user or solution.
    limit = ConcurrencyLimit()
    assert limit.limit == 5
    assert await run_many(limit, 30) == 5
    assert limit.in_flight == 0


async def test_the_cap_is_configurable() -> None:
    limit = ConcurrencyLimit(2)
    assert await run_many(limit, 10) == 2


async def test_a_shared_cap_holds_across_its_users() -> None:
    shared = ConcurrencyLimit(3)
    peaks = await asyncio.gather(run_many(shared, 10), run_many(shared, 10))
    assert max(peaks) == 3


async def test_waiters_get_slots_in_arrival_order() -> None:
    limit = ConcurrencyLimit(1)
    order: list[int] = []
    gate = asyncio.Event()

    async def first() -> None:
        async with limit:
            await gate.wait()
            order.append(0)

    async def call(n: int) -> None:
        async with limit:
            order.append(n)

    holder = asyncio.create_task(first())
    await asyncio.sleep(0)
    others = [asyncio.create_task(call(n)) for n in range(1, 6)]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(holder, *others)
    assert order == [0, 1, 2, 3, 4, 5]


async def test_a_slot_is_given_back_when_the_request_fails() -> None:
    limit = ConcurrencyLimit(1)
    with pytest.raises(RuntimeError):
        async with limit:
            raise RuntimeError
    assert limit.in_flight == 0
    async with limit:
        assert limit.in_flight == 1


async def test_a_cancelled_waiter_does_not_lose_a_slot() -> None:
    limit = ConcurrencyLimit(1)
    await limit.acquire()
    waiting = asyncio.create_task(limit.acquire())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    limit.release()
    assert limit.in_flight == 0
    await asyncio.wait_for(limit.acquire(), timeout=1.0)
    assert limit.in_flight == 1


async def test_a_waiter_cancelled_after_it_was_handed_a_slot_passes_it_on() -> None:
    limit = ConcurrencyLimit(1)
    await limit.acquire()
    first = asyncio.create_task(limit.acquire())
    second = asyncio.create_task(limit.acquire())
    await asyncio.sleep(0)
    limit.release()  # hands the slot to `first`, which has not run yet
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, timeout=1.0)
    assert limit.in_flight == 1


def test_one_cap_serves_one_event_loop_after_another() -> None:
    limit = ConcurrencyLimit(2)
    assert run_in_new_loop(run_many(limit, 6)) == 2
    assert run_in_new_loop(run_many(limit, 6)) == 2


@pytest.mark.parametrize("bad", [0, -1, 21])
def test_the_cap_must_be_between_1_and_the_account_cap(bad: int) -> None:
    # 20 parallel requests per account is the highest documented cap.
    with pytest.raises(ValueError, match="limit"):
        ConcurrencyLimit(bad)


def test_the_repr_shows_the_cap() -> None:
    assert repr(ConcurrencyLimit()) == "ConcurrencyLimit(5)"


def test_release_without_acquire_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="release"):
        ConcurrencyLimit().release()
