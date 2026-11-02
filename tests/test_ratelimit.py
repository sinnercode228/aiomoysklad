from __future__ import annotations

import asyncio
import random

import pytest

from aiomoysklad import RateLimiter

from .clock import FakeClock


def limiter_with(clock: FakeClock, capacity: int = 45, window: float = 3.0) -> RateLimiter:
    return RateLimiter(capacity, window, clock=clock, sleep=clock.sleep)


def assert_no_window_over(sends: list[tuple[float, int]], capacity: int, window: float) -> None:
    for start, _ in sends:
        spent = sum(w for t, w in sends if start <= t < start + window - 1e-9)
        assert spent <= capacity, (start, spent)


async def test_the_full_basket_is_available_at_once() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock)
    for _ in range(15):  # 15 user-token requests of 3 units
        await limiter.acquire(3)
    assert clock.now == 0.0
    assert limiter.available() == 0


async def test_a_spent_unit_comes_back_one_window_later() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock)
    for _ in range(15):
        await limiter.acquire(3)
    await limiter.acquire(3)
    assert clock.now == pytest.approx(3.0)
    assert clock.sleeps == pytest.approx([3.0])


async def test_units_come_back_in_the_order_they_were_spent() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock, capacity=10, window=3.0)
    await limiter.acquire(4)  # t=0
    clock.now = 1.0
    await limiter.acquire(6)  # t=1, basket empty
    clock.now = 1.5
    await limiter.acquire(4)  # needs the 4 units of t=0 back: t=3
    assert clock.now == pytest.approx(3.0)
    await limiter.acquire(5)  # needs the 6 units of t=1 back: t=4
    assert clock.now == pytest.approx(4.0)


async def test_a_heavy_request_waits_for_enough_free_units() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock)
    for i in range(42):
        clock.now = i * 0.01
        await limiter.acquire(1)
    # 3 units free; a stock report needs 5, so the first two units spent must come back.
    await limiter.acquire(5)
    assert clock.now == pytest.approx(3.01)


async def test_no_3_second_window_ever_holds_more_than_45_units() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock)
    rng = random.Random(1)
    sends: list[tuple[float, int]] = []
    idle = 0.0
    for _ in range(400):
        gap = rng.choice([0.0, 0.0, 0.01, 0.05, 0.3])
        idle += gap
        clock.now += gap
        weight = rng.choice([1, 1, 3, 4, 5])
        await limiter.acquire(weight)
        sends.append((clock.now, weight))
    assert_no_window_over(sends, 45, 3.0)
    # And it does not hold callers longer than needed: the time spent is about one window
    # per basket of units, plus the caller's own pauses.
    total = sum(w for _, w in sends)
    assert clock.now <= (total / 45 + 1) * 3.0 + idle


async def test_concurrent_callers_never_overspend() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock)
    sends: list[tuple[float, int]] = []

    async def call(weight: int) -> None:
        await limiter.acquire(weight)
        sends.append((clock.now, weight))

    weights = [3] * 40 + [5] * 10
    await asyncio.gather(*(call(w) for w in weights))
    assert len(sends) == 50
    assert_no_window_over(sends, 45, 3.0)


async def test_waiters_are_served_in_arrival_order() -> None:
    # A cheap request behind a stock report must not jump the queue, or a stream of cheap
    # requests could starve the report.
    clock = FakeClock()
    limiter = limiter_with(clock, capacity=10)
    await limiter.acquire(8)
    order: list[str] = []

    async def call(name: str, weight: int) -> None:
        await limiter.acquire(weight)
        order.append(name)

    await asyncio.gather(call("report", 5), call("cheap", 1), call("cheap2", 1))
    assert order == ["report", "cheap", "cheap2"]
    assert clock.now == pytest.approx(3.0)


async def test_a_cancelled_waiter_takes_nothing() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock, capacity=5)
    await limiter.acquire(5)
    waiting = asyncio.create_task(limiter.acquire(5))
    await asyncio.sleep(0)  # now asleep until the units come back
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    clock.now = 3.0
    assert limiter.available() == 5
    await asyncio.wait_for(limiter.acquire(5), timeout=1.0)
    assert clock.now == 3.0


async def test_a_pause_holds_every_caller() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock)
    limiter.pause(1.5)
    await limiter.acquire(1)
    assert clock.now == pytest.approx(1.5)


async def test_a_shorter_pause_does_not_shorten_a_longer_one() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock)
    limiter.pause(2.0)
    limiter.pause(0.5)
    await limiter.acquire(1)
    assert clock.now == pytest.approx(2.0)


async def test_one_limiter_can_be_shared_by_several_callers() -> None:
    clock = FakeClock()
    shared = limiter_with(clock)
    sends: list[tuple[float, int]] = []

    async def client(weight: int, count: int) -> None:
        for _ in range(count):
            await shared.acquire(weight)
            sends.append((clock.now, weight))

    await asyncio.gather(client(3, 20), client(1, 30))
    assert_no_window_over(sends, 45, 3.0)


def test_one_limiter_serves_one_event_loop_after_another() -> None:
    clock = FakeClock()
    limiter = limiter_with(clock, capacity=5)

    async def burst() -> None:
        await asyncio.gather(*(limiter.acquire(1) for _ in range(5)))

    asyncio.run(burst())
    asyncio.run(burst())
    assert clock.now == pytest.approx(3.0)


@pytest.mark.parametrize("weight", [0, -1, 46])
async def test_a_weight_must_fit_in_the_basket(weight: int) -> None:
    with pytest.raises(ValueError, match="weight"):
        await RateLimiter().acquire(weight)


def test_the_defaults_are_the_documented_basket() -> None:
    limiter = RateLimiter()
    assert (limiter.capacity, limiter.window) == (45, 3.0)
    assert limiter.available() == 45
    assert repr(limiter) == "RateLimiter(capacity=45, window=3.0)"


@pytest.mark.parametrize(
    ("capacity", "window"),
    [(0, 3.0), (-1, 3.0), (45, 0.0), (45, -1.0), (45, float("inf")), (45, float("nan"))],
)
def test_the_basket_is_validated(capacity: int, window: float) -> None:
    with pytest.raises(ValueError, match=r"capacity|window"):
        RateLimiter(capacity, window)


def test_a_negative_pause_is_refused() -> None:
    with pytest.raises(ValueError, match="pause"):
        RateLimiter().pause(-1.0)
