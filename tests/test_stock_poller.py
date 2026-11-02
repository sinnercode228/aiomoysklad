"""Polling the short report the way reports/_report_stock.md § Параметр changedSince advises."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import respx

from aiomoysklad import ForbiddenError, MoySklad, StockPoller, StockRow, StoreStockRow, join_stock
from aiomoysklad._time import MSK

ALL = "/report/stock/all/current"
BYSTORE = "/report/stock/bystore/current"
START = datetime(2026, 11, 2, 9, 0, tzinfo=MSK)
A1 = "35427052-36e7-11e7-8a7f-40d0000000d7"
A2 = "437f2d67-36e7-11e7-8a7f-40d0000000df"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def now(ms: MoySklad) -> Clock:
    fake = Clock()
    ms.stock.now = fake
    return fake


async def test_the_first_poll_is_a_full_report_with_zero_rows(
    ms: MoySklad, mock: respx.MockRouter, now: Clock
) -> None:
    route = mock.get(ALL).respond(200, json=[{"assortmentId": A1, "stock": 0}])
    poller = StockPoller(ms.stock)
    update = await poller.poll()
    assert update.full
    assert update.since is None
    assert update.at == START
    assert update.rows == [StockRow(A1, "stock", 0.0)]
    assert dict(route.calls.last.request.url.params) == {
        "stockType": "stock",
        "include": "zeroLines",
    }
    assert (poller.last_poll, poller.last_full) == (START, START)


async def test_later_polls_ask_for_changes_since_the_last_one_with_an_overlap(
    ms: MoySklad, mock: respx.MockRouter, now: Clock
) -> None:
    # The docs' example: every 30 minutes, for the past 35.
    route = mock.get(ALL).respond(200, json=[])
    poller = StockPoller(ms.stock, overlap=timedelta(minutes=5))
    await poller.poll()
    now.now = START + timedelta(minutes=30)
    update = await poller.poll()
    assert not update.full
    assert update.since == START - timedelta(minutes=5)
    assert dict(route.calls.last.request.url.params) == {
        "stockType": "stock",
        "changedSince": "2026-11-02 08:55:00",
    }
    assert poller.last_poll == now.now
    assert poller.last_full == START


async def test_a_full_report_runs_again_after_full_every(
    ms: MoySklad, mock: respx.MockRouter, now: Clock
) -> None:
    # "A full synchronization without changedSince once a day or more often."
    mock.get(ALL).respond(200, json=[])
    poller = StockPoller(ms.stock, full_every=timedelta(hours=6))
    await poller.poll()
    now.now = START + timedelta(hours=5, minutes=59)
    assert not (await poller.poll()).full
    now.now = START + timedelta(hours=6)
    assert (await poller.poll()).full


async def test_a_cursor_older_than_24_hours_falls_back_to_a_full_report(
    ms: MoySklad, mock: respx.MockRouter, now: Clock
) -> None:
    mock.get(ALL).respond(200, json=[])
    poller = StockPoller(
        ms.stock,
        last_poll=START - timedelta(hours=23, minutes=58),
        last_full=START - timedelta(hours=1),
        overlap=timedelta(minutes=5),
    )
    update = await poller.poll()
    assert update.full
    assert update.since is None


async def test_a_saved_state_continues_with_changes(
    ms: MoySklad, mock: respx.MockRouter, now: Clock
) -> None:
    route = mock.get(BYSTORE).respond(
        200, json=[{"assortmentId": A1, "storeId": None, "reserve": 2}]
    )
    poller = StockPoller(
        ms.stock,
        by_store=True,
        stock_type="reserve",
        store_ids=["12345678-b123-aaee-0a80-012b0001bb10"],
        last_poll=START - timedelta(minutes=10),
        last_full=START - timedelta(hours=2),
        overlap=timedelta(0),
    )
    update = await poller.poll()
    assert update.rows == [StoreStockRow(A1, None, "reserve", 2.0)]
    assert dict(route.calls.last.request.url.params) == {
        "stockType": "reserve",
        "changedSince": "2026-11-02 08:50:00",
        "filter": "storeId=12345678-b123-aaee-0a80-012b0001bb10",
    }


async def test_a_clock_that_went_back_does_not_ask_for_the_future(
    ms: MoySklad, mock: respx.MockRouter, now: Clock
) -> None:
    route = mock.get(ALL).respond(200, json=[])
    poller = StockPoller(
        ms.stock,
        last_poll=START + timedelta(minutes=10),
        last_full=START,
        overlap=timedelta(minutes=1),
    )
    update = await poller.poll()
    assert update.since == START
    assert route.calls.last.request.url.params["changedSince"] == "2026-11-02 09:00:00"


async def test_a_failed_poll_keeps_the_state(
    ms: MoySklad, mock: respx.MockRouter, now: Clock
) -> None:
    mock.get(ALL).respond(403, json={"errors": [{"error": "Нет прав", "code": 1016}]})
    poller = StockPoller(ms.stock)
    with pytest.raises(ForbiddenError):
        await poller.poll()
    assert (poller.last_poll, poller.last_full) == (None, None)


@pytest.mark.parametrize(
    ("overlap", "full_every"),
    [
        (timedelta(seconds=-1), timedelta(hours=1)),
        (timedelta(hours=24), timedelta(hours=1)),
        (timedelta(0), timedelta(0)),
        (timedelta(0), timedelta(hours=24, seconds=1)),
    ],
)
def test_the_settings_stay_within_the_24_hour_window(
    ms: MoySklad, overlap: timedelta, full_every: timedelta
) -> None:
    with pytest.raises(ValueError, match=r"overlap|full_every"):
        StockPoller(ms.stock, overlap=overlap, full_every=full_every)


def test_join_stock_pairs_rows_by_id_and_assortment_id() -> None:
    # dictionaries/_assortment.md § Поля остатков: assortment.rows[i].id ===
    # stockReport[j].assortmentId.
    rows = [{"id": A1, "name": "Товар А"}, {"id": A2, "name": "Товар Б"}]
    stock = [StockRow(A1, "stock", 15.0)]
    assert join_stock(rows, stock) == [(rows[0], 15.0), (rows[1], None)]
    assert join_stock(rows, stock, default=0.0) == [(rows[0], 15.0), (rows[1], 0.0)]


def test_join_stock_sums_the_stores_of_a_report_by_store() -> None:
    rows = [{"id": A1}]
    stock = [
        StoreStockRow(A1, "s1", "stock", 4.0),
        StoreStockRow(A1, None, "stock", 1.5),
        StoreStockRow(A2, "s1", "stock", 9.0),
    ]
    assert join_stock(rows, stock) == [(rows[0], 5.5)]


def test_join_stock_keeps_an_unknown_value_unknown() -> None:
    rows = [{"id": A1}]
    stock = [StoreStockRow(A1, "s1", "stock", 4.0), StoreStockRow(A1, "s2", "stock", None)]
    assert join_stock(rows, stock, default=0.0) == [(rows[0], None)]


def test_join_stock_refuses_mixed_stock_types() -> None:
    with pytest.raises(ValueError, match="stock type"):
        join_stock([], [StockRow(A1, "stock", 1.0), StockRow(A2, "reserve", 1.0)])
