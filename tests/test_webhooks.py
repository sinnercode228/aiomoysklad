"""Webhook and webhookstock management (dictionaries/_webhook.md, dictionaries/_webhookstock.md).

The rows are the examples of those pages.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import MoySklad, ServerError, Webhook, WebhookStock

from .conftest import API

HOOKS = "/entity/webhook"
STOCK_HOOKS = "/entity/webhookstock"
ACCOUNT = "b8b74698-9128-11e6-8a84-bae500000001"
H1 = "aec51463-bbd2-11e6-8a84-bae500000003"
H2 = "d08f9217-bbd2-11e6-8a84-bae500000004"
RECEIVER = "https://hooks.example.com/ms/s3cret"


def hook(ident: str, entity_type: str, action: str, **extra: Any) -> dict[str, Any]:
    return {
        "meta": {
            "href": f"{API}{HOOKS}/{ident}",
            "metadataHref": f"{API}{HOOKS}/metadata",
            "type": "webhook",
            "mediaType": "application/json",
        },
        "id": ident,
        "accountId": ACCOUNT,
        "entityType": entity_type,
        "url": "http://www.example.com",
        "method": "POST",
        "enabled": True,
        "action": action,
        **extra,
    }


def stock_hook(ident: str, **extra: Any) -> dict[str, Any]:
    return {
        "meta": {
            "href": f"{API}{STOCK_HOOKS}/{ident}",
            "metadataHref": f"{API}{STOCK_HOOKS}/metadata",
            "type": "webhookstock",
            "mediaType": "application/json",
        },
        "id": ident,
        "accountId": ACCOUNT,
        "stockType": "stock",
        "reportType": "all",
        "url": "https://www.example.com",
        "enabled": True,
        **extra,
    }


def collection(path: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    meta = {"href": f"{API}{path}", "size": len(rows), "limit": 1000, "offset": 0}
    return {"meta": meta, "rows": rows}


def sent(route: respx.Route, index: int = -1) -> Any:
    return json.loads(route.calls[index].request.content)


async def test_list_reads_typed_webhooks(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(HOOKS).respond(
        200,
        json=collection(
            HOOKS,
            [
                hook(H1, "demand", "CREATE", authorApplication={"meta": {"href": "x"}}),
                hook(H2, "cashin", "UPDATE", diffType="NONE"),
            ],
        ),
    )
    hooks = await ms.webhooks.list()
    assert [(h.id, h.entity_type, h.action, h.diff_type) for h in hooks] == [
        (H1, "demand", "CREATE", None),
        (H2, "cashin", "UPDATE", "NONE"),
    ]
    assert hooks[0].enabled
    assert hooks[0].account_id == ACCOUNT
    assert hooks[0].meta.href == f"{API}{HOOKS}/{H1}"


async def test_create_sends_the_required_fields(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.post(HOOKS).respond(200, json=hook(H2, "supply", "UPDATE", diffType="FIELDS"))
    created = await ms.webhooks.create("supply", "UPDATE", RECEIVER, diff_type="FIELDS")
    assert isinstance(created, Webhook)
    assert created.diff_type == "FIELDS"
    assert sent(route) == {
        "entityType": "supply",
        "action": "UPDATE",
        "url": RECEIVER,
        "diffType": "FIELDS",
    }


async def test_a_create_is_not_resent(ms: MoySklad, mock: respx.MockRouter) -> None:
    # A webhook has no syncId, so a lost answer could mean it exists already.
    route = mock.post(HOOKS).respond(503)
    with pytest.raises(ServerError):
        await ms.webhooks.create("supply", "CREATE", RECEIVER)
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("args", "kwargs", "match"),
    [
        (("webhook", "CREATE", RECEIVER), {}, "webhook"),
        (("discount", "CREATE", RECEIVER), {}, "discount"),
        (("notes", "CREATE", RECEIVER), {}, "notes"),
        (("", "CREATE", RECEIVER), {}, "entity_type"),
        (("product", "MOVE", RECEIVER), {}, "action"),
        (("product", "CREATE", RECEIVER), {"diff_type": "FIELDS"}, "UPDATE"),
        (("product", "UPDATE", RECEIVER), {"diff_type": "ALL"}, "diff_type"),
        (("product", "CREATE", "https://h.example/" + "x" * 238), {}, "255"),
        (("product", "CREATE", "ftp://h.example/hook"), {}, "http"),
        (("product", "CREATE", "/hook"), {}, "http"),
    ],
)
async def test_create_checks_the_documented_values(
    ms: MoySklad, mock: respx.MockRouter, args: tuple[str, str, str], kwargs: Any, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        await ms.webhooks.create(*args, **kwargs)  # type: ignore[arg-type]
    assert not mock.calls


async def test_a_url_of_255_characters_is_allowed(ms: MoySklad, mock: respx.MockRouter) -> None:
    url = "https://h.example/" + "x" * 237
    assert len(url) == 255
    route = mock.post(HOOKS).respond(200, json=hook(H1, "product", "CREATE", url=url))
    await ms.webhooks.create("product", "CREATE", url)
    assert route.called


async def test_get_update_enable_disable_and_delete(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(f"{HOOKS}/{H1}").respond(200, json=hook(H1, "demand", "CREATE"))
    put = mock.put(f"{HOOKS}/{H1}").mock(
        side_effect=lambda request: httpx.Response(
            200, json=hook(H1, "demand", "CREATE", **json.loads(request.content))
        )
    )
    delete = mock.delete(f"{HOOKS}/{H1}").respond(200)

    assert (await ms.webhooks.get(H1)).id == H1
    assert (await ms.webhooks.update(H1, {"url": RECEIVER})).url == RECEIVER
    assert not (await ms.webhooks.disable(H1)).enabled
    assert sent(put) == {"enabled": False}
    assert (await ms.webhooks.enable(H1)).enabled
    assert sent(put) == {"enabled": True}
    await ms.webhooks.delete(H1)
    assert delete.called


async def test_update_checks_the_values_too(ms: MoySklad, mock: respx.MockRouter) -> None:
    with pytest.raises(ValueError, match="255"):
        await ms.webhooks.update(H1, {"url": "https://h.example/" + "x" * 300})
    with pytest.raises(ValueError, match="action"):
        await ms.webhooks.update(H1, {"action": "MOVE"})
    with pytest.raises(ValueError, match="id"):
        await ms.webhooks.update("../product", {"enabled": True})
    assert not mock.calls


async def test_delete_many_posts_metas_in_batches_of_1000(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # _general.md § Создание и обновление нескольких объектов: at most 1000 objects per list.
    route = mock.post(f"{HOOKS}/delete").respond(200, json=[])
    ids = [f"00000000-0000-0000-0000-{n:012d}" for n in range(1001)]
    first = Webhook.model_validate(hook(ids[0], "demand", "CREATE"))
    await ms.webhooks.delete_many([first, *ids[1:]])
    assert route.call_count == 2
    assert len(sent(route, 0)) == 1000
    assert sent(route, 1) == [
        {
            "meta": {
                "href": f"{API}{HOOKS}/{ids[-1]}",
                "type": "webhook",
                "mediaType": "application/json",
            }
        }
    ]
    await ms.webhooks.delete_many([])
    assert route.call_count == 2


async def test_ensure_creates_a_missing_webhook(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(HOOKS).respond(200, json=collection(HOOKS, [hook(H1, "demand", "CREATE")]))
    post = mock.post(HOOKS).respond(200, json=hook(H2, "customerorder", "CREATE", url=RECEIVER))
    made = await ms.webhooks.ensure("customerorder", "CREATE", RECEIVER)
    assert made.id == H2
    assert sent(post)["entityType"] == "customerorder"


async def test_ensure_keeps_a_matching_webhook(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(HOOKS).respond(
        200, json=collection(HOOKS, [hook(H1, "customerorder", "CREATE", url=RECEIVER)])
    )
    post = mock.post(HOOKS)
    put = mock.put(f"{HOOKS}/{H1}")
    assert (await ms.webhooks.ensure("customerorder", "CREATE", RECEIVER)).id == H1
    assert not post.called
    assert not put.called


async def test_ensure_enables_a_disabled_webhook_and_sets_the_diff_type(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    current = hook(H1, "customerorder", "UPDATE", url=RECEIVER, enabled=False, diffType="NONE")
    mock.get(HOOKS).respond(200, json=collection(HOOKS, [current]))
    put = mock.put(f"{HOOKS}/{H1}").respond(
        200, json={**current, "enabled": True, "diffType": "FIELDS"}
    )
    made = await ms.webhooks.ensure("customerorder", "UPDATE", RECEIVER, diff_type="FIELDS")
    assert made.enabled
    assert sent(put) == {"enabled": True, "diffType": "FIELDS"}


async def test_stock_webhooks_have_their_own_fields(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(STOCK_HOOKS).respond(200, json=collection(STOCK_HOOKS, [stock_hook(H1)]))
    post = mock.post(STOCK_HOOKS).respond(
        200, json=stock_hook(H2, reportType="bystore", url=RECEIVER)
    )
    [listed] = await ms.webhook_stocks.list()
    assert isinstance(listed, WebhookStock)
    assert (listed.report_type, listed.stock_type) == ("all", "stock")
    made = await ms.webhook_stocks.ensure(RECEIVER, report_type="bystore")
    assert made.report_type == "bystore"
    assert sent(post) == {"url": RECEIVER, "reportType": "bystore", "stockType": "stock"}


async def test_stock_webhooks_check_the_documented_values(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    with pytest.raises(ValueError, match="report_type"):
        await ms.webhook_stocks.create(RECEIVER, report_type="byslot")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="stock_type"):
        await ms.webhook_stocks.create(RECEIVER, stock_type="reserve")  # type: ignore[arg-type]
    assert not mock.calls


async def test_health_reports_the_disabled_webhooks_under_a_prefix(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    mock.get(HOOKS).respond(
        200,
        json=collection(
            HOOKS,
            [
                hook(H1, "customerorder", "CREATE", url=RECEIVER, enabled=False),
                hook(H2, "demand", "CREATE", url="https://other.example/x", enabled=False),
            ],
        ),
    )
    mock.get(STOCK_HOOKS).respond(
        200, json=collection(STOCK_HOOKS, [stock_hook(H1, url=f"{RECEIVER}/stock")])
    )
    put = mock.put(f"{HOOKS}/{H1}")
    health = await ms.webhooks.health("https://hooks.example.com/ms/")
    assert [h.id for h in health.webhooks] == [H1]
    assert [h.id for h in health.stock_webhooks] == [H1]
    assert [h.url for h in health.disabled] == [RECEIVER]
    assert not health.ok
    assert health.reenabled == []
    assert not put.called


async def test_health_can_enable_them_again(ms: MoySklad, mock: respx.MockRouter) -> None:
    # _restrictions.md § Автоматическое отключение доступа: the owner turns webhooks back on,
    # support is not needed.
    disabled = stock_hook(H1, url=RECEIVER, enabled=False)
    mock.get(HOOKS).respond(200, json=collection(HOOKS, []))
    mock.get(STOCK_HOOKS).respond(200, json=collection(STOCK_HOOKS, [disabled]))
    put = mock.put(f"{STOCK_HOOKS}/{H1}").respond(200, json={**disabled, "enabled": True})
    health = await ms.webhooks.health(RECEIVER, reenable=True)
    assert [h.enabled for h in health.reenabled] == [True]
    assert sent(put) == {"enabled": True}
    assert not health.ok  # it was disabled when checked


async def test_health_of_working_webhooks_is_ok(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(HOOKS).respond(200, json=collection(HOOKS, [hook(H1, "demand", "CREATE")]))
    mock.get(STOCK_HOOKS).respond(200, json=collection(STOCK_HOOKS, []))
    health = await ms.webhooks.health()
    assert health.ok
    assert health.disabled == []
