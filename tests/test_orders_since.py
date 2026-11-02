"""Customer orders read since an ``updated`` cursor (documents/_customerOrder.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import Filter, MoySklad, MoySkladError, OrderCursor
from aiomoysklad._filters import split_conditions
from aiomoysklad._time import MSK, format_datetime, parse_datetime

from .conftest import API

ORDERS = "/entity/customerorder"
T0 = datetime(2026, 11, 2, 10, 0, 0, tzinfo=MSK)


@dataclass
class Order:
    id: str
    updated: str  # with milliseconds, as MoySklad returns it

    def row(self) -> dict[str, Any]:
        return {
            "meta": {"href": f"{API}{ORDERS}/{self.id}"},
            "id": self.id,
            "updated": self.updated,
        }


class FakeOrders:
    """Answers list requests the way the server would, from a list of orders."""

    def __init__(self, orders: list[Order]) -> None:
        self.orders = orders
        self.requests: list[httpx.Request] = []
        self.on_request: Any = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(self)
        params = request.url.params
        assert params["order"] == "updated,asc;id,asc"
        rows = sorted(self.orders, key=lambda o: (parse_datetime(o.updated), o.id))
        for field, operator, value in split_conditions(params["filter"]):
            if field == "updated":
                bound = parse_datetime(value)
                if operator == ">=":
                    rows = [o for o in rows if parse_datetime(o.updated) >= bound]
                elif operator == "<":
                    rows = [o for o in rows if parse_datetime(o.updated) < bound]
                else:
                    raise AssertionError(operator)
        offset = int(params.get("offset", "0"))
        limit = int(params["limit"])
        page = rows[offset : offset + limit]
        meta = {"href": f"{API}{ORDERS}", "size": len(rows), "limit": limit, "offset": offset}
        return httpx.Response(200, json={"meta": meta, "rows": [o.row() for o in page]})

    def filters(self) -> list[str]:
        return [r.url.params["filter"] for r in self.requests]


def at(seconds: float) -> str:
    moment = T0 + timedelta(seconds=seconds)
    return format_datetime(moment) + f".{moment.microsecond // 1000:03d}"


async def collect(stream: Any) -> list[str]:
    return [row["id"] async for row in stream]


async def test_orders_come_in_update_order_and_the_cursor_follows(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    fake = FakeOrders([Order("a", at(1)), Order("b", at(2.5)), Order("c", at(2.7))])
    mock.get(ORDERS).mock(side_effect=fake)
    stream = ms.customer_orders.iter_updated_since(T0, page_size=10)
    assert await collect(stream) == ["a", "b", "c"]
    assert stream.cursor == OrderCursor(T0 + timedelta(seconds=2), frozenset({"b", "c"}))
    assert fake.filters() == ["updated>=2026-11-02 10:00:00"]


async def test_pages_move_on_from_the_second_of_the_last_row(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    fake = FakeOrders([Order(f"o{n}", at(n // 2)) for n in range(7)])
    mock.get(ORDERS).mock(side_effect=fake)
    ids = await collect(ms.customer_orders.iter_updated_since(T0, page_size=3))
    assert ids == [f"o{n}" for n in range(7)]
    # Each page starts at the second of the last row seen, rows of that second are not repeated.
    assert fake.filters() == [
        "updated>=2026-11-02 10:00:00",
        "updated>=2026-11-02 10:00:01",
        "updated>=2026-11-02 10:00:02",
        "updated>=2026-11-02 10:00:03",
    ]


async def test_a_page_within_one_second_is_read_on_with_an_offset(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    same = [Order(f"s{n}", at(1 + n / 1000)) for n in range(7)]
    fake = FakeOrders([Order("first", at(0)), *same, Order("last", at(5))])
    mock.get(ORDERS).mock(side_effect=fake)
    stream = ms.customer_orders.iter_updated_since(T0 + timedelta(seconds=1), page_size=3)
    ids = await collect(stream)
    assert ids == [o.id for o in same] + ["last"]
    second = "updated>=2026-11-02 10:00:01;updated<2026-11-02 10:00:02"
    assert [(r.url.params["filter"], r.url.params.get("offset")) for r in fake.requests] == [
        ("updated>=2026-11-02 10:00:01", None),
        (second, "3"),
        (second, "6"),
        # One more pass over the second finds nothing new, so no row was skipped.
        (second, None),
        (second, "3"),
        (second, "6"),
        ("updated>=2026-11-02 10:00:02", None),
    ]
    assert stream.cursor == OrderCursor(T0 + timedelta(seconds=5), frozenset({"last"}))


async def test_an_order_leaving_a_crowded_second_does_not_hide_another(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # Offset paging inside one second: when "a" moves to a later second after the first page,
    # every later row shifts back by one and the next offset page starts past "c".
    fake = FakeOrders([Order(name, at(1 + n / 1000)) for n, name in enumerate("abcde")])

    def move_a_once(orders: FakeOrders) -> None:
        if len(orders.requests) == 2:
            orders.orders[0] = Order("a", at(5))

    fake.on_request = move_a_once
    mock.get(ORDERS).mock(side_effect=fake)
    ids = await collect(ms.customer_orders.iter_updated_since(T0, page_size=2))
    assert sorted(set(ids)) == ["a", "b", "c", "d", "e"]
    assert ids.count("a") == 2  # once from the crowded second, once with its new updated


async def test_a_full_second_at_the_end_keeps_the_cursor_on_it(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    same = [Order(f"s{n}", at(1 + n / 1000)) for n in range(3)]
    fake = FakeOrders(same)
    mock.get(ORDERS).mock(side_effect=fake)
    stream = ms.customer_orders.iter_updated_since(T0, page_size=3)
    assert await collect(stream) == ["s0", "s1", "s2"]
    # More orders may still be written in that second: the cursor stays on it.
    assert stream.cursor == OrderCursor(T0 + timedelta(seconds=1), frozenset({"s0", "s1", "s2"}))

    fake.orders.append(Order("s3", at(1.9)))
    again = ms.customer_orders.iter_updated_since(stream.cursor, page_size=3)
    assert await collect(again) == ["s3"]


async def test_a_saved_cursor_skips_what_was_already_read(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    fake = FakeOrders([Order("a", at(1.1)), Order("b", at(1.2)), Order("c", at(3))])
    mock.get(ORDERS).mock(side_effect=fake)
    cursor = OrderCursor(T0 + timedelta(seconds=1), frozenset({"a"}))
    assert await collect(ms.customer_orders.iter_updated_since(cursor)) == ["b", "c"]


async def test_an_order_changed_while_reading_comes_again_later(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    fake = FakeOrders([Order("a", at(0)), Order("b", at(1)), Order("c", at(2))])

    def change_a_once(orders: FakeOrders) -> None:
        if len(orders.requests) == 2:
            orders.orders[0] = Order("a", at(9))

    fake.on_request = change_a_once
    mock.get(ORDERS).mock(side_effect=fake)
    ids = await collect(ms.customer_orders.iter_updated_since(T0, page_size=2))
    assert ids == ["a", "b", "c", "a"]


async def test_the_callers_filter_is_added(ms: MoySklad, mock: respx.MockRouter) -> None:
    fake = FakeOrders([])
    mock.get(ORDERS).mock(side_effect=fake)
    state = f"{API}/entity/customerorder/metadata/states/fb56c504-2e58-11e6-8a84-bae500000069"
    stream = ms.customer_orders.iter_updated_since(
        T0, filter=Filter().eq("state", {"meta": {"href": state}}), expand="agent", page_size=100
    )
    assert await collect(stream) == []
    [request] = fake.requests
    assert request.url.params["filter"] == f"updated>=2026-11-02 10:00:00;state={state}"
    assert request.url.params["expand"] == "agent"

    fake.requests.clear()
    await collect(ms.customer_orders.iter_updated_since(T0, filter="applicable=true"))
    assert fake.filters() == ["updated>=2026-11-02 10:00:00;applicable=true"]


@pytest.mark.parametrize(
    "bad", [Filter().lt("updated", T0), "updated<2026-11-02 10:00:00", "sum>1;updated=x"]
)
def test_the_callers_filter_cannot_bound_updated(ms: MoySklad, bad: Filter | str) -> None:
    with pytest.raises(ValueError, match="updated"):
        ms.customer_orders.iter_updated_since(T0, filter=bad)


def test_the_page_size_is_1_to_1000_and_100_with_expand(ms: MoySklad) -> None:
    with pytest.raises(ValueError, match="limit"):
        ms.customer_orders.iter_updated_since(T0, page_size=1001)
    with pytest.raises(ValueError, match="100"):
        ms.customer_orders.iter_updated_since(T0, expand="agent")


@pytest.mark.parametrize("row", [{"id": "a"}, {"updated": at(1)}, {"id": "a", "updated": "x"}])
async def test_a_row_without_id_or_updated_is_an_error(
    ms: MoySklad, mock: respx.MockRouter, row: dict[str, Any]
) -> None:
    mock.get(ORDERS).respond(200, json={"meta": {}, "rows": [row]})
    with pytest.raises(MoySkladError, match=r"updated|id"):
        await collect(ms.customer_orders.iter_updated_since(T0))


def test_a_cursor_keeps_whole_seconds_in_moscow_time() -> None:
    cursor = OrderCursor(datetime(2026, 11, 2, 7, 0, 1, 900_000, tzinfo=timezone.utc))
    assert cursor.updated == datetime(2026, 11, 2, 10, 0, 1, tzinfo=MSK)
    naive = OrderCursor(datetime(2026, 11, 2, 10, 0, 1))  # noqa: DTZ001
    assert naive.updated == cursor.updated


def test_a_cursor_round_trips_through_a_dict() -> None:
    cursor = OrderCursor(T0, frozenset({"b", "a"}))
    data = cursor.as_dict()
    assert data == {"updated": "2026-11-02 10:00:00", "seen": ["a", "b"]}
    assert OrderCursor.from_dict(data) == cursor
    with pytest.raises(ValueError, match="cursor"):
        OrderCursor.from_dict({"updated": 5})
    with pytest.raises(ValueError, match="cursor"):
        OrderCursor.from_dict({"updated": "2026-11-02 10:00:00", "seen": "a"})
