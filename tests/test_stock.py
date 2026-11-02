"""The short stock reports of reports/_report_stock.md § Краткий отчет об остатках.

The answers are the examples of that section.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import MoySklad, MoySkladError, StockRow, StoreStockRow
from aiomoysklad._client import MAX_HEADER_BYTES
from aiomoysklad._time import MSK

ALL = "/report/stock/all/current"
BYSTORE = "/report/stock/bystore/current"
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=MSK)

A1 = "12345678-5838-aaeb-0a80-003a003ef439"
A2 = "12345678-279c-aaeb-0a80-00d6001f847c"
S1 = "12345678-b123-aaee-0a80-012b0001bb10"
S2 = "12345678-b123-aaee-0a80-012b0001bb12"


@pytest.fixture(autouse=True)
def frozen_now(ms: MoySklad) -> None:
    ms.stock.now = lambda: NOW


def query(route: respx.Route, index: int = -1) -> dict[str, str]:
    return dict(route.calls[index].request.url.params)


async def test_all_current_reads_rows_by_assortment(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.get(ALL).respond(
        200, json=[{"assortmentId": A1, "stock": 11}, {"assortmentId": A2, "stock": 46.5}]
    )
    rows = await ms.stock.all_current()
    assert rows == [StockRow(A1, "stock", 11.0), StockRow(A2, "stock", 46.5)]
    assert query(route) == {"stockType": "stock"}


async def test_all_current_with_a_stock_type_and_zero_rows(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get(ALL).respond(
        200,
        json=[{"assortmentId": A1, "quantity": 11}, {"assortmentId": A2, "quantity": 0}],
    )
    rows = await ms.stock.all_current("quantity", include_zero=True)
    assert [(row.assortment_id, row.value) for row in rows] == [(A1, 11.0), (A2, 0.0)]
    assert {row.stock_type for row in rows} == {"quantity"}
    assert query(route) == {"stockType": "quantity", "include": "zeroLines"}


async def test_bystore_current_keeps_the_row_without_a_store(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # "storeId":null is the part of the reserve of a customer order without a store.
    mock.get(BYSTORE).respond(
        200,
        json=[
            {"assortmentId": A1, "storeId": S1, "freeStock": 4},
            {"assortmentId": A2, "storeId": None, "freeStock": -5},
        ],
    )
    rows = await ms.stock.bystore_current("freeStock")
    assert rows == [
        StoreStockRow(A1, S1, "freeStock", 4.0),
        StoreStockRow(A2, None, "freeStock", -5.0),
    ]


async def test_bystore_current_with_recalculate_gives_null_values(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get(BYSTORE).respond(
        200, json=[{"assortmentId": A1, "storeId": S1, "stock": None}]
    )
    [row] = await ms.stock.bystore_current(with_recalculate=True)
    assert row.value is None
    assert query(route)["withRecalculate"] == "true"


async def test_ids_go_into_one_filter_separated_by_commas(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get(BYSTORE).respond(200, json=[])
    await ms.stock.bystore_current(assortment_ids=[A1, A2, A1], store_ids=[S1, S2])
    assert query(route)["filter"] == f"assortmentId={A1},{A2};storeId={S1},{S2}"


async def test_changed_since_is_sent_in_moscow_time(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.get(ALL).respond(200, json=[])
    since = datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc)
    await ms.stock.all_current(changed_since=since)
    assert query(route)["changedSince"] == "2026-11-02 11:30:00"
    assert b"changedSince=2026-11-02%2011%3A30%3A00" in route.calls.last.request.url.raw_path


@pytest.mark.parametrize(
    "since",
    [NOW + timedelta(seconds=1), NOW - timedelta(hours=24, seconds=1)],
    ids=["future", "older-than-24h"],
)
async def test_changed_since_must_be_within_the_last_24_hours(
    ms: MoySklad, mock: respx.MockRouter, since: datetime
) -> None:
    # The parameter cannot be in the future nor more than 24 hours back.
    with pytest.raises(ValueError, match="changed_since"):
        await ms.stock.all_current(changed_since=since)
    assert not mock.calls


async def test_a_naive_changed_since_is_moscow_time(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.get(ALL).respond(200, json=[])
    await ms.stock.all_current(changed_since=datetime(2026, 11, 1, 12, 0))  # noqa: DTZ001
    assert query(route)["changedSince"] == "2026-11-01 12:00:00"


async def test_an_unknown_stock_type_is_refused(ms: MoySklad) -> None:
    with pytest.raises(ValueError, match="stock_type"):
        await ms.stock.all_current("free")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "a;b", "a,b", "x" * 37])
async def test_an_id_that_is_not_a_uuid_is_refused(ms: MoySklad, bad: str) -> None:
    with pytest.raises(ValueError, match="id"):
        await ms.stock.all_current(assortment_ids=[bad])


async def test_long_id_lists_are_split_to_stay_under_the_header_limit(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    ids = [f"00000000-0000-0000-0000-{n:012d}" for n in range(400)]

    def answer(request: httpx.Request) -> httpx.Response:
        chunk = request.url.params["filter"].removeprefix("assortmentId=").split(",")
        return httpx.Response(200, json=[{"assortmentId": i, "stock": 1} for i in chunk])

    route = mock.get(ALL).mock(side_effect=answer)
    rows = await ms.stock.all_current(assortment_ids=ids)
    assert [row.assortment_id for row in rows] == ids
    assert route.call_count > 1
    for call in route.calls:
        request = call.request
        size = len(request.url.raw_path) + sum(len(k) + len(v) for k, v in request.headers.raw)
        assert size < MAX_HEADER_BYTES


async def test_long_store_lists_are_split_too(ms: MoySklad, mock: respx.MockRouter) -> None:
    stores = [f"00000000-0000-0000-0001-{n:012d}" for n in range(200)]
    route = mock.get(BYSTORE).respond(200, json=[])
    await ms.stock.bystore_current(assortment_ids=[A1], store_ids=stores)
    seen = []
    for call in route.calls:
        assortment, store = call.request.url.params["filter"].split(";")
        assert assortment == f"assortmentId={A1}"
        seen.extend(store.removeprefix("storeId=").split(","))
    assert seen == stores
    assert route.call_count > 1


async def test_each_report_request_costs_five_units(ms: MoySklad, mock: respx.MockRouter) -> None:
    # _restrictions.md § Лимит запросов за 3-секундный период.
    mock.get(ALL).respond(200, json=[])
    before = ms.limiter.available()
    await ms.stock.all_current()
    assert before - ms.limiter.available() == 5


async def test_a_row_named_by_another_stock_type_is_read_as_that_type(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # The changedSince example of the docs asks for the default stockType and shows rows with
    # "quantity"; the row says what it holds.
    mock.get(ALL).respond(200, json=[{"assortmentId": A1, "quantity": 1}])
    [row] = await ms.stock.all_current(changed_since=NOW - timedelta(minutes=35))
    assert (row.stock_type, row.value) == ("quantity", 1.0)


@pytest.mark.parametrize(
    "body",
    [
        {"rows": []},
        [{"stock": 1}],
        [{"assortmentId": A1}],
        [{"assortmentId": A1, "stock": "1"}],
        [{"assortmentId": A1, "stock": True}],
        ["x"],
    ],
)
async def test_an_answer_that_is_not_a_report_is_an_error(
    ms: MoySklad, mock: respx.MockRouter, body: Any
) -> None:
    mock.get(ALL).respond(200, json=body)
    with pytest.raises(MoySkladError, match="report"):
        await ms.stock.all_current()


async def test_a_store_id_that_is_not_text_is_an_error(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    mock.get(BYSTORE).respond(200, json=[{"assortmentId": A1, "storeId": 5, "stock": 1}])
    with pytest.raises(MoySkladError, match="storeId"):
        await ms.stock.bystore_current()
