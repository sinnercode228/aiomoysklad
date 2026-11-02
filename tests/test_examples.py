"""The examples run against mocked MoySklad endpoints."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import AuthKind, BasicAuth, BearerToken, MoySklad, StockPoller
from aiomoysklad._time import MSK

from .conftest import API

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=MSK)
P1 = "35427052-36e7-11e7-8a7f-40d0000000d7"
P2 = "437f2d67-36e7-11e7-8a7f-40d0000000df"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def collection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"meta": {"href": API, "size": len(rows), "limit": 1000, "offset": 0}, "rows": rows}


@pytest.mark.parametrize("name", ["stock_sync", "sync_orders", "webhooks"])
def test_auth_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    module = load(name)
    monkeypatch.setenv("MOYSKLAD_TOKEN", "t")
    monkeypatch.setenv("MOYSKLAD_TOKEN_KIND", "solution")
    auth = module.auth_from_env()
    assert isinstance(auth, BearerToken)
    assert auth.kind is AuthKind.SOLUTION
    monkeypatch.delenv("MOYSKLAD_TOKEN")
    monkeypatch.setenv("MOYSKLAD_LOGIN", "admin@example")
    monkeypatch.setenv("MOYSKLAD_PASSWORD", "secret")
    assert isinstance(module.auth_from_env(), BasicAuth)


# stock_sync.py


async def test_stock_sync_prints_a_full_report_then_the_changes(
    ms: MoySklad, mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    now = [NOW]
    ms.stock.now = lambda: now[0]
    mock.get("/entity/assortment").respond(
        200,
        json=collection(
            [{"id": P1, "name": "Товар А", "code": "A"}, {"id": P2, "name": "Товар Б"}]
        ),
    )
    report = mock.get("/report/stock/all/current").mock(
        side_effect=[
            httpx.Response(200, json=[{"assortmentId": P1, "quantity": 15}]),
            httpx.Response(200, json=[{"assortmentId": P2, "quantity": 3}]),
        ]
    )
    example = load("stock_sync")
    assortment = await example.load_assortment(ms)
    poller = StockPoller(ms.stock, stock_type="quantity")

    await example.poll_once(poller, assortment)
    now[0] = NOW + timedelta(minutes=10)
    await example.poll_once(poller, assortment)

    assert report.calls[0].request.url.params["include"] == "zeroLines"
    assert report.calls[1].request.url.params["changedSince"] == "2026-11-02 11:55:00"
    assert capsys.readouterr().out.splitlines() == [
        "full report, 2 items",
        "        15  Товар А",
        "         0  Товар Б",
        "changes since 2026-11-02 11:55:00, 1 items",
        "         3  Товар Б",
    ]


async def test_stock_sync_main_runs_the_given_number_of_rounds(
    monkeypatch: pytest.MonkeyPatch, mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MOYSKLAD_TOKEN", "t")
    mock.get("/entity/assortment").respond(200, json=collection([]))
    mock.get("/report/stock/all/current").respond(200, json=[])
    await load("stock_sync").main(rounds=1, interval=0)
    assert capsys.readouterr().out == "full report, 0 items\n"


# sync_orders.py

NEW = "fb56c504-2e58-11e6-8a84-bae500000069"
PAID = "fb5f6bd0-2e58-11e6-8a84-bae50000006b"


def state_meta(ident: str) -> dict[str, Any]:
    return {
        "meta": {
            "href": f"{API}/entity/customerorder/metadata/states/{ident}",
            "type": "state",
            "mediaType": "application/json",
        }
    }


def order(ident: str, updated: str, *, paid: bool, state: str = NEW) -> dict[str, Any]:
    return {
        "id": ident,
        "name": f"0000{ident[-1]}",
        "updated": updated,
        "sum": 150000,
        "payedSum": 150000 if paid else 0,
        "state": state_meta(state),
    }


async def test_sync_orders_moves_paid_orders_and_saves_the_cursor(
    ms: MoySklad, mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orders = mock.get("/entity/customerorder").respond(
        200,
        json=collection(
            [
                order("o1", "2026-11-02 10:00:00.100", paid=True),
                order("o2", "2026-11-02 10:00:01.000", paid=False),
                order("o3", "2026-11-02 10:00:01.500", paid=True, state=PAID),
            ]
        ),
    )
    mock.get("/entity/customerorder/metadata").respond(
        200,
        json={
            "states": [
                {**state_meta(NEW), "id": NEW, "name": "Новый"},
                {**state_meta(PAID), "id": PAID, "name": "Оплачен"},
            ]
        },
    )
    put = mock.put("/entity/customerorder/o1").respond(200, json={"id": "o1"})
    path = tmp_path / "cursor.json"

    count = await load("sync_orders").sync(ms, path, paid_state="Оплачен", now=NOW)

    assert count == 3
    assert orders.calls[0].request.url.params["filter"] == (
        "updated>=2026-11-01 12:00:00;applicable=true"
    )
    assert json.loads(put.calls.last.request.content)["state"]["meta"]["href"].endswith(PAID)
    assert put.call_count == 1
    assert json.loads(path.read_text()) == {"updated": "2026-11-02 10:00:01", "seen": ["o2", "o3"]}
    assert capsys.readouterr().out.splitlines() == [
        "00001  2026-11-02 10:00:00.100  sum 150000, paid 150000 -> Оплачен",
        "00002  2026-11-02 10:00:01.000  sum 150000, paid 0",
        "00003  2026-11-02 10:00:01.500  sum 150000, paid 150000",
    ]

    orders.respond(200, json=collection([]))
    assert await load("sync_orders").sync(ms, path, paid_state=None, now=NOW) == 0
    assert orders.calls.last.request.url.params["filter"].startswith(
        "updated>=2026-11-02 10:00:01;"
    )


async def test_sync_orders_names_a_state_that_does_not_exist(
    ms: MoySklad, mock: respx.MockRouter, tmp_path: Path
) -> None:
    mock.get("/entity/customerorder/metadata").respond(
        200, json={"states": [{**state_meta(NEW), "id": NEW, "name": "Новый"}]}
    )
    orders = mock.get("/entity/customerorder").respond(200, json=collection([]))
    with pytest.raises(LookupError, match="'Оплачн'"):
        await load("sync_orders").sync(ms, tmp_path / "c.json", paid_state="Оплачн", now=NOW)
    assert not orders.called


async def test_sync_orders_main_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, mock: respx.MockRouter, tmp_path: Path
) -> None:
    monkeypatch.setenv("MOYSKLAD_TOKEN", "t")
    monkeypatch.setenv("ORDERS_CURSOR", str(tmp_path / "c.json"))
    route = mock.get("/entity/customerorder").respond(200, json=collection([]))
    await load("sync_orders").main()
    assert route.calls.last.request.headers["Authorization"] == "Bearer t"
    assert (tmp_path / "c.json").exists()


# webhooks.py

SECRET = "s3cret"
EVENT_BODY = {
    "auditContext": {
        "meta": {"type": "audit", "href": f"{API}/audit/75fe3b73-db16-11eb-c0a8-800d00000004"},
        "moment": "2021-07-21 15:51:16",
        "uid": "test@test",
    },
    "events": [
        {
            "meta": {"type": "customerorder", "href": f"{API}/entity/customerorder/o1"},
            "action": "CREATE",
            "accountId": "9171a53c-b719-11eb-c0a8-800d00000001",
        }
    ],
}


async def test_the_webhook_app_answers_at_once_and_handles_after(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    example = load("webhooks")
    fetched: list[Any] = []

    async def handle(receiver: Any, delivery: Any) -> None:
        fetched.extend([await receiver.fetch(event) for event in delivery.events])

    mock.get("/entity/customerorder/o1").respond(200, json={"id": "o1", "name": "00001"})
    app = example.create_app(ms, SECRET, handle=handle)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        url = f"/moysklad/{SECRET}?requestId=640569eb-522d-427a-b07e-fa757c5d4217"
        first = await client.post(url, content=json.dumps(EVENT_BODY))
        again = await client.post(url, content=json.dumps(EVENT_BODY))
        wrong = await client.post("/moysklad/guess", content=json.dumps(EVENT_BODY))
        bad = await client.post(f"/moysklad/{SECRET}", content=b"{}")
        get = await client.get(f"/moysklad/{SECRET}")
        await app.drain()

    assert [first.status_code, again.status_code] == [204, 204]
    assert [wrong.status_code, bad.status_code, get.status_code] == [404, 400, 405]
    assert fetched == [{"id": "o1", "name": "00001"}]


async def test_the_default_handler_logs_each_entity(
    ms: MoySklad, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    example = load("webhooks")
    mock.get("/entity/customerorder/o1").respond(200, json={"id": "o1", "name": "00001"})
    receiver = example.WebhookReceiver(ms)
    delivery = await receiver.accept(json.dumps(EVENT_BODY).encode(), "r1")
    caplog.set_level("INFO", logger="webhooks")
    await example.log_delivery(receiver, delivery)
    assert [r.getMessage() for r in caplog.records] == ["CREATE customerorder 00001"]


async def test_the_webhook_setup_ensures_the_webhooks_and_checks_them(
    ms: MoySklad, mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    base = "https://hooks.example.com/moysklad/s3cret"
    created: list[dict[str, Any]] = []

    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        created.append(body)
        return httpx.Response(
            200,
            json={
                "meta": {"href": f"{API}/entity/webhook/h{len(created)}"},
                "id": f"h{len(created)}",
                "enabled": True,
                "method": "POST",
                **body,
            },
        )

    mock.get("/entity/webhook").respond(200, json=collection([]))
    mock.get("/entity/webhookstock").respond(200, json=collection([]))
    mock.post("/entity/webhook").mock(side_effect=create)
    await load("webhooks").setup(ms, base)
    assert [(c["entityType"], c["action"]) for c in created] == [
        ("customerorder", "CREATE"),
        ("customerorder", "UPDATE"),
        ("customerorder", "DELETE"),
    ]
    assert {c["url"] for c in created} == {base}
    assert capsys.readouterr().out.splitlines()[-1] == "all webhooks under the prefix are on"


def test_the_webhook_app_needs_a_secret(ms: MoySklad) -> None:
    with pytest.raises(ValueError, match="secret"):
        load("webhooks").create_app(ms, "")


async def test_the_webhook_app_from_the_environment_closes_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://hooks.example.com/moysklad/s3cret"
    monkeypatch.setenv("MOYSKLAD_TOKEN", "t")
    monkeypatch.setenv("WEBHOOK_URL", url)
    app = load("webhooks").app_from_env()
    assert app.path == "/moysklad/s3cret"
    assert app.ms.webhook_disable_prefixes == (url,)

    incoming = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(incoming)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert app.ms.http.is_closed


async def test_the_webhook_commands(
    monkeypatch: pytest.MonkeyPatch, mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    example = load("webhooks")
    assert await example.main([]) == 2
    assert "WEBHOOK_URL" in capsys.readouterr().out

    url = "https://hooks.example.com/moysklad/s3cret"
    monkeypatch.setenv("MOYSKLAD_TOKEN", "t")
    monkeypatch.setenv("WEBHOOK_URL", url)
    disabled = {
        "meta": {"href": f"{API}/entity/webhook/h1"},
        "id": "h1",
        "entityType": "customerorder",
        "action": "CREATE",
        "url": url,
        "enabled": False,
    }
    mock.get("/entity/webhook").respond(200, json=collection([disabled]))
    mock.get("/entity/webhookstock").respond(200, json=collection([]))
    put = mock.put("/entity/webhook/h1").respond(200, json={**disabled, "enabled": True})
    assert await example.main(["health"]) == 1
    assert put.call_count == 0
    assert await example.main(["health", "--reenable"]) == 1
    assert put.call_count == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-2:] == [f"disabled: h1 {url}", "turned 1 back on"]
