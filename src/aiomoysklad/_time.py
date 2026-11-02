"""Moscow time, the zone of every MoySklad date.

``_general.md § Формат даты и времени``: dates are strings ``YYYY-MM-DD HH:MM:SS``, returned
with milliseconds (``YYYY-MM-DD HH:MM:SS.mmm``), and filters also accept ``YYYY-MM-DD HH:MM``.
The moments of creation and update are in ``MSK``. Moscow has kept a fixed UTC+3 since 2014,
so no time zone database is needed.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

__all__ = ["MSK", "format_datetime", "moscow_today", "parse_datetime"]

MSK = timezone(timedelta(hours=3), "MSK")

_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{3}))?)?",
)


def format_datetime(value: datetime) -> str:
    """Format a moment as ``YYYY-MM-DD HH:MM:SS`` in Moscow time.

    An aware datetime is converted to Moscow time; a naive one is taken as Moscow time
    already. Microseconds are dropped.
    """
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(MSK)
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d} "
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    )


def parse_datetime(text: str) -> datetime:
    """Read a MoySklad date into an aware datetime in Moscow time.

    Raises:
        ValueError: ``text`` is not in one of the documented formats.
    """
    match = _DATETIME.fullmatch(text.strip())
    if match is None:
        msg = f"not a MoySklad date: {text!r}"
        raise ValueError(msg)
    year, month, day, hour, minute, second, millis = match.groups()
    return datetime(
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second or 0),
        int(millis or 0) * 1000,
        tzinfo=MSK,
    )


def moscow_today(now: datetime | None = None) -> date:
    """Return the current date in Moscow, or the Moscow date of the aware moment ``now``."""
    if now is None:
        return datetime.now(MSK).date()
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "now must be an aware datetime"
        raise ValueError(msg)
    return now.astimezone(MSK).date()
