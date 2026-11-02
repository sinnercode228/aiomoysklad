from __future__ import annotations

from datetime import date

import pytest

from aiomoysklad import AuthKind, request_weight
from aiomoysklad._weights import CAPACITY, WINDOW, base_weight

USER = AuthKind.USER
SOLUTION = AuthKind.SOLUTION
DRAFT = AuthKind.DRAFT_SOLUTION


def test_the_basket_is_45_units_per_3_seconds() -> None:
    # _restrictions.md § Лимит запросов за 3-секундный период.
    assert CAPACITY == 45
    assert WINDOW == 3.0


@pytest.mark.parametrize(
    ("kind", "today", "weight"),
    [
        # A public or private solution: the standard weight, 45 requests per 3 s.
        (SOLUTION, date(2026, 1, 1), 1),
        (SOLUTION, date(2027, 6, 1), 1),
        # A solution in status Черновик or На модерации: 4 units from 13.04.2026.
        (DRAFT, date(2026, 4, 12), 1),
        (DRAFT, date(2026, 4, 13), 4),
        (DRAFT, date(2027, 6, 1), 4),
        # Login and password or a user token: 2 from 12 May, 3 from 1 September, 4 from
        # 1 December 2026.
        (USER, date(2026, 5, 11), 1),
        (USER, date(2026, 5, 12), 2),
        (USER, date(2026, 8, 31), 2),
        (USER, date(2026, 9, 1), 3),
        (USER, date(2026, 11, 30), 3),
        (USER, date(2026, 12, 1), 4),
        (USER, date(2028, 1, 1), 4),
    ],
)
def test_the_weight_follows_the_documented_schedule(
    kind: AuthKind, today: date, weight: int
) -> None:
    assert base_weight(kind, today) == weight
    assert request_weight(kind, "GET", "/entity/product", today) == weight


@pytest.mark.parametrize(
    "path",
    [
        "/report/stock/all",
        "/report/stock/bystore",
        "report/stock/all",
        "/report/stock/all/",
        # Not named by the docs; charged 5 until a live account says otherwise.
        "/report/stock/all/current",
        "/report/stock/bystore/current",
    ],
)
@pytest.mark.parametrize("kind", list(AuthKind))
def test_the_stock_reports_cost_5_whatever_the_auth(kind: AuthKind, path: str) -> None:
    assert request_weight(kind, "GET", path, date(2026, 11, 2)) == 5
    assert request_weight(kind, "get", path, date(2026, 12, 2)) == 5


@pytest.mark.parametrize(
    "path", ["/report/stock/allx", "/report/stock/byoperation", "/report/stock", "/entity/all"]
)
def test_other_paths_have_the_standard_weight(path: str) -> None:
    assert request_weight(SOLUTION, "GET", path, date(2026, 11, 2)) == 1


def test_only_get_on_a_stock_report_is_heavy() -> None:
    assert request_weight(SOLUTION, "POST", "/report/stock/all", date(2026, 11, 2)) == 1


def test_a_path_override_wins_over_the_schedule() -> None:
    overrides = {"/report/stock/all/current": 1, "/entity/product": 2}
    today = date(2026, 11, 2)
    assert request_weight(USER, "GET", "/report/stock/all/current", today, overrides) == 1
    assert request_weight(USER, "GET", "/report/stock/all", today, overrides) == 5
    assert request_weight(SOLUTION, "PUT", "/entity/product/1", today, overrides) == 2
    assert request_weight(SOLUTION, "GET", "/entity/productfolder", today, overrides) == 1


def test_the_longest_override_prefix_wins() -> None:
    overrides = {"/report": 2, "/report/stock/bystore": 3}
    today = date(2026, 11, 2)
    assert request_weight(SOLUTION, "GET", "/report/stock/bystore/current", today, overrides) == 3
    assert request_weight(SOLUTION, "GET", "/report/profit", today, overrides) == 2


@pytest.mark.parametrize("bad", [0, -1, 46])
def test_an_override_must_fit_in_the_basket(bad: int) -> None:
    with pytest.raises(ValueError, match="weight"):
        request_weight(SOLUTION, "GET", "/x", date(2026, 11, 2), {"/x": bad})
