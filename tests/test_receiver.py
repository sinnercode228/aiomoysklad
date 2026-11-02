"""The webhook receiver helper.

The payloads are the examples of dictionaries/_webhook.md § Пример вебхука and
dictionaries/_webhookstock.md § Описание вебхука на изменение остатков.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import (
    BearerToken,
    EntityNotification,
    MemoryDedupe,
    MoySklad,
    StockNotification,
    StockRow,
    StoreStockRow,
    WebhookPayloadError,
    WebhookReceiver,
    parse_webhook,
)

from .conftest import API

PRODUCT = f"{API}/entity/product/75c896d0-db16-11eb-c0a8-800d00000002"
REQUEST_ID = "640569eb-522d-427a-b07e-fa757c5d4217"


def entity_payload(*events: dict[str, Any]) -> dict[str, Any]:
    return {
        "auditContext": {
            "meta": {"type": "audit", "href": f"{API}/audit/75fe3b73-db16-11eb-c0a8-800d00000004"},
            "moment": "2021-07-21 15:51:16",
            "uid": "test@test",
        },
        "events": list(events)
        or [
            {
                "meta": {"type": "product", "href": PRODUCT},
                "updatedFields": ["name", "description"],
                "action": "UPDATE",
                "accountId": "9171a53c-b719-11eb-c0a8-800d00000001",
            }
        ],
    }


def event(href: str, action: str = "UPDATE", **extra: Any) -> dict[str, Any]:
    return {
        "meta": {"type": "product", "href": href},
        "action": action,
        "accountId": "9171a53c-b719-11eb-c0a8-800d00000001",
        **extra,
    }


STOCK_PAYLOAD = {
    "accountId": "f71cb8b6-f7b5-11ec-ac12-000f000000eb",
    "stockType": "stock",
    "reportType": "all",
    "reportUrl": f"{API}/report/stock/all/current?changedSince=2022-09-24 19:14:32",
}


def body(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_an_entity_webhook_is_parsed() -> None:
    parsed = parse_webhook(body(entity_payload()))
    assert isinstance(parsed, EntityNotification)
    assert parsed.audit_context is not None
    assert (parsed.audit_context.uid, parsed.audit_context.moment) == (
        "test@test",
        "2021-07-21 15:51:16",
    )
    [only] = parsed.events
    assert (only.action, only.meta.type, only.meta.href) == ("UPDATE", "product", PRODUCT)
    assert only.updated_fields == ["name", "description"]
    assert only.account_id == "9171a53c-b719-11eb-c0a8-800d00000001"


def test_a_stock_webhook_is_parsed() -> None:
    parsed = parse_webhook(body(STOCK_PAYLOAD).decode())
    assert isinstance(parsed, StockNotification)
    assert (parsed.stock_type, parsed.report_type) == ("stock", "all")
    assert parsed.report_url.endswith("changedSince=2022-09-24 19:14:32")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'{"events": "x"}',
        b'{"events": [{"action": "UPDATE"}]}',
        b'{"accountId": "a"}',
        b"\xff",
    ],
)
def test_a_payload_that_is_neither_kind_is_refused(raw: bytes) -> None:
    with pytest.raises(WebhookPayloadError):
        parse_webhook(raw)
    assert issubclass(WebhookPayloadError, ValueError)


async def test_a_resent_notification_is_accepted_once() -> None:
    # The same requestId comes back with each of the 3 retries (dictionaries/_webhook.md).
    receiver = WebhookReceiver()
    first = await receiver.accept(body(entity_payload()), REQUEST_ID)
    assert first is not None
    assert first.request_id == REQUEST_ID
    assert await receiver.accept(body(entity_payload()), REQUEST_ID) is None
    other = await receiver.accept(body(entity_payload()), "another-id")
    assert other is not None
    assert (receiver.stats.accepted, receiver.stats.duplicates) == (2, 1)


async def test_without_a_request_id_the_body_is_the_key() -> None:
    receiver = WebhookReceiver()
    assert await receiver.accept(body(STOCK_PAYLOAD)) is not None
    assert await receiver.accept(body(STOCK_PAYLOAD)) is None
    assert await receiver.accept(body({**STOCK_PAYLOAD, "reportType": "bystore"})) is not None


async def test_keys_are_forgotten_after_the_ttl() -> None:
    clock = Clock()
    receiver = WebhookReceiver(dedupe=MemoryDedupe(clock=clock), ttl=60.0)
    assert await receiver.accept(body(STOCK_PAYLOAD), REQUEST_ID) is not None
    clock.now = 59.0
    assert await receiver.accept(body(STOCK_PAYLOAD), REQUEST_ID) is None
    clock.now = 61.0
    assert await receiver.accept(body(STOCK_PAYLOAD), REQUEST_ID) is not None


async def test_the_memory_dedupe_holds_a_bounded_number_of_keys() -> None:
    dedupe = MemoryDedupe(max_keys=2)
    assert await dedupe.first_seen("a", 60.0)
    assert await dedupe.first_seen("b", 60.0)
    assert await dedupe.first_seen("c", 60.0)
    assert len(dedupe) == 2
    assert await dedupe.first_seen("a", 60.0)  # the oldest key was dropped
    assert not await dedupe.first_seen("c", 60.0)


async def test_a_custom_dedupe_store_is_used() -> None:
    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        async def first_seen(self, key: str, ttl: float) -> bool:
            self.calls.append((key, ttl))
            return False

    store = Store()
    receiver = WebhookReceiver(dedupe=store, ttl=30.0)
    assert await receiver.accept(body(STOCK_PAYLOAD), REQUEST_ID) is None
    assert store.calls == [(f"moysklad:webhook:{REQUEST_ID}", 30.0)]


async def test_a_bad_payload_is_counted_and_not_remembered() -> None:
    receiver = WebhookReceiver()
    with pytest.raises(WebhookPayloadError):
        await receiver.accept(b"{}", REQUEST_ID)
    assert receiver.stats.rejected == 1
    assert await receiver.accept(body(STOCK_PAYLOAD), REQUEST_ID) is not None


async def test_repeated_events_of_one_delivery_come_once() -> None:
    other = f"{API}/entity/product/00000000-0000-0000-0000-000000000001"
    payload = entity_payload(event(PRODUCT), event(PRODUCT), event(PRODUCT, "DELETE"), event(other))
    receiver = WebhookReceiver()
    events = await receiver.receive(body(payload), REQUEST_ID)
    assert [(e.event.meta.href, e.event.action) for e in events] == [  # type: ignore[union-attr]
        (PRODUCT, "UPDATE"),
        (PRODUCT, "DELETE"),
        (other, "UPDATE"),
    ]
    assert all(e.entity is None and e.request_id == REQUEST_ID for e in events)
    assert await receiver.receive(body(payload), REQUEST_ID) == []


async def test_receive_can_fetch_each_entity_once(ms: MoySklad, mock: respx.MockRouter) -> None:
    other = f"{API}/entity/product/00000000-0000-0000-0000-000000000001"
    product = mock.get(PRODUCT.removeprefix(API)).respond(200, json={"name": "Товар"})
    gone = mock.get(other.removeprefix(API)).respond(
        404,
        json={
            "errors": [
                {
                    # _errors.md, 1021.
                    "error": (
                        "Объект с типом 'product' и идентификатором "
                        "'00000000-0000-0000-0000-000000000001' не найден"
                    ),
                    "code": 1021,
                }
            ]
        },
    )
    payload = entity_payload(
        event(PRODUCT, updatedFields=["name"]),
        event(PRODUCT, updatedFields=["description"]),
        event(other),
        event(f"{API}/entity/product/00000000-0000-0000-0000-000000000002", "DELETE"),
    )
    receiver = WebhookReceiver(ms)
    events = await receiver.receive(body(payload), REQUEST_ID, fetch=True)
    assert [e.entity for e in events] == [{"name": "Товар"}, {"name": "Товар"}, None, None]
    assert product.call_count == 1
    assert gone.call_count == 1  # deleted since the event: no entity


async def test_a_forged_href_is_not_fetched(ms: MoySklad, mock: respx.MockRouter) -> None:
    payload = entity_payload(event("https://evil.example/api/remap/1.2/entity/product/1"))
    receiver = WebhookReceiver(ms)
    with pytest.raises(WebhookPayloadError, match="evil"):
        await receiver.receive(body(payload), REQUEST_ID, fetch=True)
    assert not mock.calls


async def test_fetch_needs_a_client() -> None:
    receiver = WebhookReceiver()
    with pytest.raises(RuntimeError, match="client"):
        await receiver.receive(body(entity_payload()), REQUEST_ID, fetch=True)


async def test_a_stock_notification_fetches_its_report(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get("/report/stock/all/current").respond(
        200, json=[{"assortmentId": "a", "stock": 3}]
    )
    receiver = WebhookReceiver(ms)
    [received] = await receiver.receive(body(STOCK_PAYLOAD), REQUEST_ID, fetch=True)
    assert isinstance(received.event, StockNotification)
    assert received.stock == [StockRow("a", "stock", 3.0)]
    request = route.calls.last.request
    assert request.url.params["changedSince"] == "2022-09-24 19:14:32"
    assert b" " not in request.url.raw_path


async def test_a_bystore_notification_reads_store_rows(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    mock.get("/report/stock/bystore/current").respond(
        200, json=[{"assortmentId": "a", "storeId": "s", "stock": 1}]
    )
    payload = {
        **STOCK_PAYLOAD,
        "reportType": "bystore",
        "reportUrl": f"{API}/report/stock/bystore/current?changedSince=2022-09-24 19:14:32",
    }
    receiver = WebhookReceiver(ms)
    delivery = await receiver.accept(body(payload), REQUEST_ID)
    assert delivery is not None
    assert isinstance(delivery.notification, StockNotification)
    rows = await receiver.fetch_stock(delivery.notification)
    assert rows == [StoreStockRow("a", "s", "stock", 1.0)]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/api/remap/1.2/report/stock/all/current",
        f"{API}/entity/product",
        f"{API}/report/stock/all",
    ],
)
async def test_a_report_url_outside_the_short_reports_is_refused(
    ms: MoySklad, mock: respx.MockRouter, url: str
) -> None:
    receiver = WebhookReceiver(ms)
    with pytest.raises(WebhookPayloadError, match="reportUrl"):
        await receiver.fetch_stock(
            StockNotification.model_validate({**STOCK_PAYLOAD, "reportUrl": url})
        )
    assert not mock.calls


async def test_the_stats_count_days_since_the_last_delivery() -> None:
    today = [date(2026, 11, 2)]
    receiver = WebhookReceiver(today=lambda: today[0])
    assert receiver.stats.days_without_success() is None
    await receiver.accept(body(STOCK_PAYLOAD), REQUEST_ID)
    assert receiver.stats.last_accepted == date(2026, 11, 2)
    today[0] = date(2026, 11, 5)
    assert receiver.stats.days_without_success() == 3
    assert receiver.stats.days_without_success(date(2026, 11, 2)) == 0
    await receiver.accept(body(STOCK_PAYLOAD), REQUEST_ID)  # a duplicate is still answered
    assert receiver.stats.days_without_success() == 0


def test_the_receiver_takes_a_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl"):
        WebhookReceiver(ttl=0)
    with pytest.raises(ValueError, match="max_keys"):
        MemoryDedupe(max_keys=0)


async def test_a_mock_transport_client_can_fetch_too() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"href": str(request.url)})

    transport = httpx.MockTransport(answer)
    async with (
        httpx.AsyncClient(transport=transport) as http,
        MoySklad(BearerToken("t"), http=http) as client,
    ):
        receiver = WebhookReceiver(client)
        delivery = await receiver.accept(body(entity_payload()), REQUEST_ID)
        assert delivery is not None
        [only] = delivery.events
        assert await receiver.fetch(only) == {"href": PRODUCT}
