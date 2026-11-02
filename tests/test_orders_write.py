"""Creating customer orders and changing their state (documents/_customerOrder.md)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import MoySklad, MoySkladError, OrderCursor, State

from .conftest import API

ORDERS = "/entity/customerorder"
ORDER_ID = "34efe2ee-015e-11e6-9464-e4de0000006b"
ORGANIZATION = {
    "meta": {
        "href": f"{API}/entity/organization/850c8195-f504-11e5-8a84-bae50000015e",
        "type": "organization",
        "mediaType": "application/json",
    }
}
AGENT = {
    "meta": {
        "href": f"{API}/entity/counterparty/9794d400-f689-11e5-8a84-bae500000078",
        "type": "counterparty",
        "mediaType": "application/json",
    }
}
NEW = "fb56c504-2e58-11e6-8a84-bae500000069"
SHIPPED = "fb5f6bd0-2e58-11e6-8a84-bae50000006b"


def state(ident: str, name: str, state_type: str = "Regular") -> dict[str, Any]:
    # The shape of dictionaries/_states.md § Получить метаданные.
    return {
        "meta": {
            "href": f"{API}/entity/customerorder/metadata/states/{ident}",
            "metadataHref": f"{API}/entity/customerorder/metadata",
            "type": "state",
            "mediaType": "application/json",
        },
        "id": ident,
        "accountId": "84e60e93-f504-11e5-8a84-bae500000008",
        "name": name,
        "color": 15106326,
        "stateType": state_type,
        "entityType": "customerorder",
    }


def metadata(*states: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": {"href": f"{API}/entity/customerorder/metadata", "mediaType": "application/json"},
        "attributes": [],
        "states": list(states),
        "createShared": False,
    }


def sent(route: respx.Route, index: int = -1) -> Any:
    return json.loads(route.calls[index].request.content)


async def test_create_adds_a_sync_id_so_a_retry_returns_the_same_order(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # _general.md § Назначение поля syncId: the same syncId returns the order created first.
    route = mock.post(ORDERS).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"id": ORDER_ID, "name": "00001"}),
        ]
    )
    order = await ms.customer_orders.create({"organization": ORGANIZATION, "agent": AGENT})
    assert order["id"] == ORDER_ID
    assert route.call_count == 2
    first, second = sent(route, 0), sent(route, 1)
    assert first == second
    assert uuid.UUID(first["syncId"]).version == 4
    assert first["organization"] == ORGANIZATION


async def test_create_keeps_a_given_sync_id(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.post(ORDERS).respond(200, json={"id": ORDER_ID})
    sync_id = uuid.UUID("734a9e26-45a2-4ead-849c-e144daeb854d")
    body = {"organization": ORGANIZATION, "agent": AGENT, "name": "web-1"}
    await ms.customer_orders.create(body, sync_id=sync_id)
    assert sent(route)["syncId"] == str(sync_id)
    assert "syncId" not in body  # the caller's dict is left alone

    await ms.customer_orders.create({**body, "syncId": str(sync_id)})
    assert sent(route)["syncId"] == str(sync_id)


def test_create_refuses_two_different_sync_ids(ms: MoySklad) -> None:
    body = {"organization": ORGANIZATION, "agent": AGENT, "syncId": str(uuid.uuid4())}
    with pytest.raises(ValueError, match="syncId"):
        ms.customer_orders._prepare(body, uuid.uuid4())
    with pytest.raises(ValueError, match="syncId"):
        ms.customer_orders._prepare({**body, "syncId": "not-a-uuid"}, None)


@pytest.mark.parametrize("missing", ["organization", "agent"])
async def test_create_needs_the_organization_and_the_agent(
    ms: MoySklad, mock: respx.MockRouter, missing: str
) -> None:
    # documents/_customerOrder.md § Создать Заказ покупателя.
    body = {"organization": ORGANIZATION, "agent": AGENT}
    del body[missing]
    with pytest.raises(ValueError, match=missing):
        await ms.customer_orders.create(body)
    assert not mock.calls


async def test_create_refuses_more_than_1000_positions(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # _restrictions.md § Ограничения на размер данных: at most 1000 items in one array, or 413.
    position = {"quantity": 1, "assortment": {"meta": {"href": f"{API}/entity/product/x"}}}
    body = {"organization": ORGANIZATION, "agent": AGENT, "positions": [position] * 1001}
    with pytest.raises(ValueError, match="1000"):
        await ms.customer_orders.create(body)
    assert not mock.calls
    route = mock.post(ORDERS).respond(200, json={"id": ORDER_ID})
    await ms.customer_orders.create({**body, "positions": [position] * 1000})
    assert route.called


async def test_get_reads_one_order(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.get(f"{ORDERS}/{ORDER_ID}").respond(200, json={"id": ORDER_ID})
    assert await ms.customer_orders.get(ORDER_ID, expand="agent") == {"id": ORDER_ID}
    assert route.calls.last.request.url.params["expand"] == "agent"


@pytest.mark.parametrize("bad", ["", "../webhook", "a/b", "a?b=c", "x" * 65])
async def test_an_order_id_cannot_change_the_path(ms: MoySklad, bad: str) -> None:
    with pytest.raises(ValueError, match="id"):
        await ms.customer_orders.get(bad)
    with pytest.raises(ValueError, match="id"):
        await ms.customer_orders.set_state(bad, "Новый")


async def test_states_come_from_the_metadata(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.get(f"{ORDERS}/metadata").respond(
        200, json=metadata(state(NEW, "Новый"), state(SHIPPED, "Отгружен", "Successful"))
    )
    states = await ms.customer_orders.states()
    assert [(s.name, s.state_type) for s in states] == [
        ("Новый", "Regular"),
        ("Отгружен", "Successful"),
    ]
    assert states[1].meta.href.endswith(f"/metadata/states/{SHIPPED}")
    await ms.customer_orders.states()
    assert route.call_count == 1  # cached
    await ms.customer_orders.states(reload=True)
    assert route.call_count == 2


async def test_set_state_by_name_puts_the_state_meta(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(f"{ORDERS}/metadata").respond(
        200, json=metadata(state(NEW, "Новый"), state(SHIPPED, "Отгружен", "Successful"))
    )
    put = mock.put(f"{ORDERS}/{ORDER_ID}").respond(200, json={"id": ORDER_ID})
    assert await ms.customer_orders.set_state(ORDER_ID, "Отгружен") == {"id": ORDER_ID}
    assert sent(put) == {
        "state": {
            "meta": {
                "href": f"{API}/entity/customerorder/metadata/states/{SHIPPED}",
                "type": "state",
                "mediaType": "application/json",
            }
        }
    }


async def test_set_state_by_id_or_state(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(f"{ORDERS}/metadata").respond(200, json=metadata(state(NEW, "Новый")))
    put = mock.put(f"{ORDERS}/{ORDER_ID}").respond(200, json={"id": ORDER_ID})
    await ms.customer_orders.set_state(ORDER_ID, uuid.UUID(NEW))
    assert sent(put)["state"]["meta"]["href"].endswith(NEW)
    await ms.customer_orders.set_state(ORDER_ID, NEW)
    assert sent(put)["state"]["meta"]["href"].endswith(NEW)
    [new] = await ms.customer_orders.states()
    await ms.customer_orders.set_state(uuid.UUID(ORDER_ID), new)
    assert put.call_count == 3


async def test_an_unknown_state_reloads_the_states_once(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    meta = mock.get(f"{ORDERS}/metadata").mock(
        side_effect=[
            httpx.Response(200, json=metadata(state(NEW, "Новый"))),
            httpx.Response(200, json=metadata(state(NEW, "Новый"), state(SHIPPED, "Отгружен"))),
            httpx.Response(200, json=metadata(state(NEW, "Новый"), state(SHIPPED, "Отгружен"))),
        ]
    )
    put = mock.put(f"{ORDERS}/{ORDER_ID}").respond(200, json={"id": ORDER_ID})
    await ms.customer_orders.states()
    await ms.customer_orders.set_state(ORDER_ID, "Отгружен")
    assert meta.call_count == 2
    with pytest.raises(LookupError, match="Отменен"):
        await ms.customer_orders.set_state(ORDER_ID, "Отменен")
    assert meta.call_count == 3
    assert put.call_count == 1


async def test_two_states_with_one_name_are_ambiguous(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(f"{ORDERS}/metadata").respond(
        200, json=metadata(state(NEW, "Новый"), state(SHIPPED, "Новый"))
    )
    with pytest.raises(LookupError, match="2 states"):
        await ms.customer_orders.set_state(ORDER_ID, "Новый")


@pytest.mark.parametrize("body", [[], {"states": "x"}, {"states": [{"name": "x"}]}])
async def test_metadata_without_states_is_an_error(
    ms: MoySklad, mock: respx.MockRouter, body: Any
) -> None:
    mock.get(f"{ORDERS}/metadata").respond(200, json=body)
    with pytest.raises(MoySkladError, match="states"):
        await ms.customer_orders.states()


async def test_metadata_with_no_states_gives_an_empty_list(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    mock.get(f"{ORDERS}/metadata").respond(200, json={"meta": {"href": f"{API}{ORDERS}"}})
    assert await ms.customer_orders.states() == []


def test_a_state_is_exported() -> None:
    parsed = State.model_validate(state(NEW, "Новый"))
    assert (parsed.id, parsed.entity_type, parsed.color) == (NEW, "customerorder", 15106326)


def test_a_saved_cursor_with_other_items_is_refused() -> None:
    with pytest.raises(ValueError, match="cursor"):
        OrderCursor.from_dict({"updated": "2026-11-02 10:00:00", "seen": [1]})
