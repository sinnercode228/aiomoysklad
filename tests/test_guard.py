from __future__ import annotations

from typing import Any

import pytest

from aiomoysklad import ErrorGuard, GuardTrippedError

from .clock import FakeClock

API = "https://api.moysklad.ru/api/remap/1.2"
ORDER = "/entity/customerorder/7944ef04-f831-11e5-7a69-971500188b19"


def guard_with(clock: FakeClock, **kwargs: Any) -> ErrorGuard:
    return ErrorGuard(clock=clock, **kwargs)


def send(
    guard: ErrorGuard,
    status: int,
    *,
    method: str = "GET",
    path: str = "/entity/product",
    body: bytes | None = None,
) -> None:
    key = guard.before_send(method, f"{API}{path}", entity=path, body=body)
    guard.record(key, status)


def test_the_defaults_stop_below_the_documented_cut_offs() -> None:
    # _restrictions.md § Автоматическое отключение доступа: more than 200 identical failing
    # requests, more than 200 answers 429, more than 100 PUTs to one entity, per minute.
    guard = ErrorGuard()
    assert (guard.identical_errors, guard.too_many_requests, guard.puts_per_entity) == (
        150,
        150,
        75,
    )


def test_identical_failing_requests_are_stopped_at_the_limit() -> None:
    clock = FakeClock()
    guard = guard_with(clock, identical_errors=3)
    for _ in range(3):
        send(guard, 400)
    with pytest.raises(GuardTrippedError) as caught:
        send(guard, 400)
    assert caught.value.counter == "identical_errors"
    assert caught.value.limit == 3
    assert caught.value.retry_after == pytest.approx(60.0)
    assert "GET /api/remap/1.2/entity/product" in str(caught.value)


def test_a_different_request_is_not_identical() -> None:
    clock = FakeClock()
    guard = guard_with(clock, identical_errors=2)
    send(guard, 500)
    send(guard, 500)
    send(guard, 500, path="/entity/service")
    send(guard, 500, method="POST", body=b'{"name":"a"}')
    send(guard, 500, method="POST", body=b'{"name":"b"}')
    send(guard, 500, method="POST", body=b'{"name":"a"}')
    with pytest.raises(GuardTrippedError):
        send(guard, 500, method="POST", body=b'{"name":"a"}')


def test_the_query_string_is_part_of_the_identity() -> None:
    clock = FakeClock()
    guard = guard_with(clock, identical_errors=1)
    send(guard, 412, path="/entity/product?filter=a")
    send(guard, 412, path="/entity/product?filter=b")
    with pytest.raises(GuardTrippedError):
        send(guard, 412, path="/entity/product?filter=a")


@pytest.mark.parametrize("status", [200, 201, 204, 301])
def test_answers_that_are_not_errors_are_not_counted(status: int) -> None:
    clock = FakeClock()
    guard = guard_with(clock, identical_errors=1)
    for _ in range(5):
        send(guard, status)


def test_failures_older_than_a_minute_are_forgotten() -> None:
    clock = FakeClock()
    guard = guard_with(clock, identical_errors=2)
    send(guard, 404)
    clock.now = 30.0
    send(guard, 404)
    clock.now = 59.0
    with pytest.raises(GuardTrippedError) as caught:
        send(guard, 404)
    assert caught.value.retry_after == pytest.approx(1.0)
    clock.now = 60.0
    send(guard, 404)


def test_429_answers_stop_every_request() -> None:
    clock = FakeClock()
    guard = guard_with(clock, too_many_requests=3, identical_errors=200)
    send(guard, 429, path="/entity/a")
    send(guard, 429, path="/entity/b")
    send(guard, 429, path="/entity/c")
    with pytest.raises(GuardTrippedError) as caught:
        send(guard, 200, path="/entity/d")
    assert caught.value.counter == "too_many_requests"
    clock.now = 60.0
    send(guard, 200, path="/entity/d")


def test_a_429_also_counts_as_an_identical_failure() -> None:
    clock = FakeClock()
    guard = guard_with(clock, identical_errors=2, too_many_requests=200)
    send(guard, 429)
    send(guard, 429)
    with pytest.raises(GuardTrippedError) as caught:
        send(guard, 200)
    assert caught.value.counter == "identical_errors"


def test_puts_to_one_entity_are_stopped_at_the_limit_whatever_the_answer() -> None:
    clock = FakeClock()
    guard = guard_with(clock, puts_per_entity=3)
    send(guard, 200, method="PUT", path=ORDER, body=b"{}")
    send(guard, 200, method="PUT", path=ORDER, body=b'{"a":1}')
    send(guard, 409, method="PUT", path=ORDER, body=b'{"a":2}')
    with pytest.raises(GuardTrippedError) as caught:
        send(guard, 200, method="PUT", path=ORDER, body=b'{"a":3}')
    assert caught.value.counter == "puts_per_entity"
    # Reads of that entity and PUTs to another one go on.
    send(guard, 200, path=ORDER)
    send(guard, 200, method="PUT", path="/entity/customerorder/other", body=b"{}")
    clock.now = 60.0
    send(guard, 200, method="PUT", path=ORDER, body=b"{}")


def test_a_refused_put_is_not_counted() -> None:
    clock = FakeClock()
    guard = guard_with(clock, puts_per_entity=1)
    send(guard, 200, method="PUT", path=ORDER)
    for _ in range(3):
        with pytest.raises(GuardTrippedError):
            send(guard, 200, method="PUT", path=ORDER)
    clock.now = 60.0
    send(guard, 200, method="PUT", path=ORDER)


def test_a_put_counts_when_it_is_sent_not_when_it_is_answered() -> None:
    # Concurrent PUTs are all in flight before any answer comes back.
    clock = FakeClock()
    guard = guard_with(clock, puts_per_entity=2)
    url = f"{API}{ORDER}"
    guard.before_send("PUT", url, entity=ORDER, body=None)
    guard.before_send("PUT", url, entity=ORDER, body=None)
    with pytest.raises(GuardTrippedError):
        guard.before_send("PUT", url, entity=ORDER, body=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("identical_errors", 201),
        ("too_many_requests", 201),
        ("puts_per_entity", 101),
        ("identical_errors", 0),
        ("puts_per_entity", -1),
    ],
)
def test_the_stops_cannot_reach_the_documented_cut_offs(name: str, value: int) -> None:
    kwargs: dict[str, Any] = {name: value}
    with pytest.raises(ValueError, match=name):
        ErrorGuard(**kwargs)


def test_the_documented_values_themselves_are_allowed() -> None:
    # The cut-off is "more than" 200 and 100, so stopping at exactly that many is still safe.
    guard = ErrorGuard(identical_errors=200, too_many_requests=200, puts_per_entity=100)
    assert guard.puts_per_entity == 100


def test_old_keys_do_not_pile_up() -> None:
    clock = FakeClock()
    guard = guard_with(clock)
    for n in range(5000):
        send(guard, 404, path=f"/entity/product/{n}")
    clock.now = 61.0
    send(guard, 404, path="/entity/product/last")
    assert guard.tracked_keys() <= 1
