from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aiomoysklad._time import MSK, format_datetime, moscow_today, parse_datetime


def test_moscow_time_is_utc_plus_three() -> None:
    assert MSK.utcoffset(None) == timedelta(hours=3)


def test_a_naive_datetime_is_taken_as_moscow_time() -> None:
    assert format_datetime(datetime(2026, 11, 1, 9, 5, 7)) == "2026-11-01 09:05:07"  # noqa: DTZ001


def test_an_aware_datetime_is_converted_to_moscow_time() -> None:
    utc = datetime(2026, 11, 1, 22, 30, 0, tzinfo=timezone.utc)
    assert format_datetime(utc) == "2026-11-02 01:30:00"


def test_microseconds_are_dropped_not_rounded() -> None:
    assert (
        format_datetime(datetime(2026, 1, 2, 3, 4, 5, 999_999, tzinfo=MSK)) == "2026-01-02 03:04:05"
    )


def test_years_below_1000_are_zero_padded() -> None:
    assert format_datetime(datetime(970, 1, 1, tzinfo=MSK)) == "0970-01-01 00:00:00"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2016-08-23 15:21:09", datetime(2016, 8, 23, 15, 21, 9, tzinfo=MSK)),
        ("2016-08-23 15:21:09.123", datetime(2016, 8, 23, 15, 21, 9, 123_000, tzinfo=MSK)),
        ("2016-08-23 15:21", datetime(2016, 8, 23, 15, 21, tzinfo=MSK)),
    ],
)
def test_the_documented_formats_parse_as_moscow_time(text: str, expected: datetime) -> None:
    parsed = parse_datetime(text)
    assert parsed == expected
    assert parsed.utcoffset() == timedelta(hours=3)


@pytest.mark.parametrize("text", ["", "2016-08-23", "2016-08-23T15:21:09", "23.08.2016 15:21"])
def test_other_formats_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="date"):
        parse_datetime(text)


def test_today_is_the_moscow_date() -> None:
    # 22:30 UTC on 1 November is already 2 November in Moscow.
    now = datetime(2026, 11, 1, 22, 30, tzinfo=timezone.utc)
    assert moscow_today(now) == date(2026, 11, 2)


def test_today_needs_an_aware_moment() -> None:
    with pytest.raises(ValueError, match="aware"):
        moscow_today(datetime(2026, 11, 1, 22, 30))  # noqa: DTZ001


def test_today_defaults_to_the_current_moment() -> None:
    expected = datetime.now(MSK).date()
    assert moscow_today() in (expected, expected + timedelta(days=1))
