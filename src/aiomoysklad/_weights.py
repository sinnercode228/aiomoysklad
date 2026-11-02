"""How many units of the 3-second basket one request costs.

Everything here comes from ``_restrictions.md § Лимит запросов за 3-секундный период``
(``moysklad/api-remap-1.2-doc`` at ``9a811ce``), so a change in the docs is a change to this
module only:

* Since 16.02.2026 the limit is counted per user and per solution, not per account.
* The basket holds 45 units, handed out every 3 seconds.
* ``GET /api/remap/1.2/report/stock/all`` and ``.../report/stock/bystore`` cost 5 units,
  whatever the authentication.
* Any other request costs 1 unit with a public or private solution's token; 4 units from
  13.04.2026 with the token of a solution in status ``Черновик`` or ``На модерации``; and with
  login and password or a user token 2 units from 12.05.2026, 3 from 01.09.2026 and 4 from
  01.12.2026.

The docs give bare dates; they are compared with the date in Moscow.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from ._auth import AuthKind

__all__ = ["CAPACITY", "HEAVY_PATHS", "HEAVY_WEIGHT", "WINDOW", "base_weight", "request_weight"]

CAPACITY = 45
WINDOW = 3.0

HEAVY_PATHS = ("/report/stock/all", "/report/stock/bystore")
# The docs name only these two paths. Their /current sub-paths are charged the same until a
# live account shows otherwise (open question 1 of docs/design.md).
HEAVY_WEIGHT = 5

# (first day, weight) in date order; before the first day the weight is 1.
_SCHEDULE: dict[AuthKind, tuple[tuple[date, int], ...]] = {
    AuthKind.SOLUTION: (),
    AuthKind.DRAFT_SOLUTION: ((date(2026, 4, 13), 4),),
    AuthKind.USER: ((date(2026, 5, 12), 2), (date(2026, 9, 1), 3), (date(2026, 12, 1), 4)),
}


def base_weight(kind: AuthKind, today: date) -> int:
    """Return the weight of an ordinary request made with ``kind`` credentials on ``today``."""
    weight = 1
    for first_day, value in _SCHEDULE[kind]:
        if today >= first_day:
            weight = value
    return weight


def _normalize(path: str) -> str:
    return "/" + path.strip("/")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def request_weight(
    kind: AuthKind,
    method: str,
    path: str,
    today: date,
    overrides: Mapping[str, int] | None = None,
) -> int:
    """Return the units one request costs.

    Args:
        kind: Whose credentials the request is sent with.
        method: The HTTP method.
        path: The path under ``/api/remap/1.2``, such as ``/entity/product``.
        today: The date in Moscow.
        overrides: Weights by path, applied to the path and everything under it; the longest
            matching path wins over the schedule.

    Raises:
        ValueError: An override is not between 1 and :data:`CAPACITY`.
    """
    path = _normalize(path)
    if overrides:
        matches = [p for p in map(_normalize, overrides) if _under(path, p)]
        if matches:
            best = max(matches, key=len)
            weight = next(w for p, w in overrides.items() if _normalize(p) == best)
            if not 1 <= weight <= CAPACITY:
                msg = f"weight for {best} must be between 1 and {CAPACITY}, got {weight}"
                raise ValueError(msg)
            return weight
    if method.upper() == "GET" and any(_under(path, heavy) for heavy in HEAVY_PATHS):
        return HEAVY_WEIGHT
    return base_weight(kind, today)
