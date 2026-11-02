from __future__ import annotations

import random
from typing import Any

import httpx
import pytest

from aiomoysklad import RetryPolicy
from aiomoysklad._retry import RateHeaders, has_sync_ids

SYNC_ID = "c6d6bb8c-4fd2-4f40-a5f1-1d0a0b1c2d3e"


def headers(**values: str) -> httpx.Headers:
    return httpx.Headers({name.replace("_", "-"): value for name, value in values.items()})


def test_the_documented_rate_headers_are_read() -> None:
    # _general.md § Обработка ошибок: counts for Limit and Remaining, milliseconds for the
    # interval, the reset and the retry-after.
    rate = RateHeaders.from_headers(
        headers(
            X_RateLimit_Limit="45",
            X_RateLimit_Remaining="42",
            X_Lognex_Retry_TimeInterval="3000",
            X_Lognex_Reset="1500",
            X_Lognex_Retry_After="2500",
        )
    )
    assert rate == RateHeaders(limit=45, remaining=42, interval=3.0, reset=1.5, retry_after=2.5)


def test_missing_rate_headers_are_none() -> None:
    assert RateHeaders.from_headers(httpx.Headers()) == RateHeaders()


@pytest.mark.parametrize("value", ["", "abc", "1.5e", "-3", "nan", "inf", "  "])
def test_unreadable_rate_headers_are_ignored(value: str) -> None:
    rate = RateHeaders.from_headers(
        headers(X_RateLimit_Limit=value, X_Lognex_Reset=value, X_Lognex_Retry_After=value)
    )
    assert rate == RateHeaders()


def test_a_non_ascii_digit_is_not_a_count() -> None:
    raw = httpx.Headers([(b"X-RateLimit-Remaining", "²".encode("latin-1"))])
    assert RateHeaders.from_headers(raw).remaining is None


def test_header_names_are_case_insensitive() -> None:
    rate = RateHeaders.from_headers(httpx.Headers({"x-lognex-retry-after": "100"}))
    assert rate.retry_after == pytest.approx(0.1)


def test_a_zero_reset_means_no_limit_is_set() -> None:
    rate = RateHeaders.from_headers(headers(X_Lognex_Reset="0"))
    assert rate.reset == 0.0


def test_the_wait_is_retry_after_first() -> None:
    policy = RetryPolicy()
    rate = RateHeaders(reset=1.0, retry_after=2.0)
    assert policy.delay(rate, 1, random.Random(0)) == 2.0


def test_the_wait_falls_back_to_the_reset() -> None:
    policy = RetryPolicy()
    assert policy.delay(RateHeaders(reset=1.25), 1, random.Random(0)) == 1.25
    assert policy.delay(RateHeaders(reset=1.25, retry_after=0.0), 1, random.Random(0)) == 1.25


def test_without_headers_the_wait_is_a_jittered_backoff() -> None:
    # An answer without rate headers.
    policy = RetryPolicy()
    rng = random.Random(0)
    for retry, ceiling in [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0), (6, 10.0), (50, 10.0)]:
        for _ in range(20):
            wait = policy.delay(RateHeaders(), retry, rng)
            assert ceiling / 2 <= wait <= ceiling


def test_the_backoff_does_not_overflow() -> None:
    assert RetryPolicy().backoff(10_000, random.Random(0)) <= 10.0


def test_the_defaults() -> None:
    policy = RetryPolicy()
    assert policy.max_retries == 3
    assert policy.max_wait == 60.0
    assert not policy.retry_unsafe_writes
    assert policy.retry_statuses == frozenset({429, 502, 503, 504})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_retries": -1},
        {"max_wait": -1.0},
        {"backoff_base": -0.1},
        {"backoff_max": float("nan")},
    ],
)
def test_the_settings_are_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must"):
        RetryPolicy(**kwargs)


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "HEAD", "get"])
def test_idempotent_methods_may_be_resent(method: str) -> None:
    assert RetryPolicy().may_resend(method, {"name": "x"})


def test_a_post_is_resent_only_when_it_carries_a_sync_id() -> None:
    # _general.md § Назначение поля syncId: a create sent again with the same syncId answers
    # with the object created the first time.
    policy = RetryPolicy()
    assert policy.may_resend("POST", {"name": "x", "syncId": SYNC_ID})
    assert policy.may_resend("POST", [{"syncId": SYNC_ID}, {"syncId": SYNC_ID[::-1]}])
    assert not policy.may_resend("POST", {"name": "x"})
    assert not policy.may_resend("POST", [{"syncId": SYNC_ID}, {"name": "x"}])
    assert not policy.may_resend("POST", [])
    assert not policy.may_resend("POST", None)


def test_unsafe_writes_can_be_resent_on_request() -> None:
    policy = RetryPolicy(retry_unsafe_writes=True)
    assert policy.may_resend("POST", {"name": "x"})
    assert policy.may_resend("PATCH", None)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"syncId": SYNC_ID}, True),
        ({"syncId": ""}, False),
        ({"syncId": None}, False),
        ({"syncid": SYNC_ID}, False),
        ([{"syncId": SYNC_ID}], True),
        ("syncId", False),
        (None, False),
    ],
)
def test_sync_ids_are_recognized(body: Any, expected: bool) -> None:
    assert has_sync_ids(body) is expected


def test_rate_headers_can_be_read_from_a_plain_mapping() -> None:
    assert RateHeaders.from_headers({"X-Lognex-Reset": "250"}).reset == pytest.approx(0.25)
