"""The assortment endpoint without stock (dictionaries/_assortment.md § Ассортимент)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import Filter, MoySklad

from .conftest import API

ASSORTMENT = f"{API}/entity/assortment"
P1 = "35427052-36e7-11e7-8a7f-40d0000000d7"
P2 = "437f2d67-36e7-11e7-8a7f-40d0000000df"


def page(rows: list[dict[str, Any]], *, next_href: str | None = None) -> httpx.Response:
    meta: dict[str, Any] = {
        "href": ASSORTMENT,
        "type": "assortment",
        "mediaType": "application/json",
        "size": 2,
        "limit": 1000,
        "offset": 0,
    }
    if next_href:
        meta["nextHref"] = next_href
    return httpx.Response(200, json={"meta": meta, "rows": rows})


async def test_every_page_asks_for_the_endpoint_without_stock(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    answers = iter(
        [
            page([{"id": P1, "name": "Товар А"}], next_href=f"{ASSORTMENT}?offset=1&limit=1"),
            page([{"id": P2, "name": "Товар Б"}]),
        ]
    )
    route = mock.get("/entity/assortment").mock(side_effect=lambda request: next(answers))
    names = [row["name"] async for row in ms.assortment.iter(limit=1)]
    assert names == ["Товар А", "Товар Б"]
    for call in route.calls:
        headers = call.request.headers
        assert headers.get_list("X-Lognex-Remap-Beta-Feature") == ["assortmentWithoutStock"]


async def test_the_query_carries_the_filter_search_and_group(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get("/entity/assortment").mock(return_value=page([]))
    rows = ms.assortment.iter(
        filter=Filter().contains("name", "див").eq("archived", False),
        search="див",
        group_by="product",
        order="name,asc",
        expand=["product", "images"],
        limit=100,
    )
    assert [row async for row in rows] == []
    assert dict(route.calls.last.request.url.params) == {
        "limit": "100",
        "filter": "name~див;archived=false",
        "order": "name,asc",
        "search": "див",
        "expand": "product,images",
        "groupBy": "product",
    }


@pytest.mark.parametrize(
    "field",
    [
        "alcoholic.type",
        "article",
        "isSerialTrackable",
        "minimumBalance",
        "owner",
        "quantityMode",
        "salePrice",
        "search",
        "shared",
        "stockMode",
        "stockMoment",
        "stockStore",
        "updatedBy",
        "weighed",
    ],
)
def test_the_filters_the_new_endpoint_does_not_take_are_refused(ms: MoySklad, field: str) -> None:
    # § Недоступные фильтры.
    with pytest.raises(ValueError, match=field.replace(".", r"\.")):
        ms.assortment.iter(filter=f"{field}=1")


def test_barcode_has_no_not_equal(ms: MoySklad) -> None:
    # § Изменения в поведении фильтров.
    with pytest.raises(ValueError, match="barcode"):
        ms.assortment.iter(filter=Filter().ne("barcode", "2000000000015"))
    ms.assortment.iter(filter=Filter().eq("barcode", "2000000000015"))
    ms.assortment.iter(filter=Filter().startswith("barcode", "200"))


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("code~a;code~b", False),
        ("code~=a;code~=b", False),
        ("code~a;code=~b", False),
        ("code=~a;code~=b", True),
        ("code~=a;code=~b", True),
        ("code~=a;code=~b;code~c", False),
        ("code~a;name~b", True),
    ],
)
def test_like_operators_combine_on_one_field_only_as_prefix_and_suffix(
    ms: MoySklad, text: str, ok: bool
) -> None:
    if ok:
        ms.assortment.iter(filter=text)
    else:
        with pytest.raises(ValueError, match="code"):
            ms.assortment.iter(filter=text)


def test_consignments_by_type_need_the_consignment_grouping(ms: MoySklad) -> None:
    # § Атрибуты доступные для фильтрации, type.
    with pytest.raises(ValueError, match="groupBy=consignment"):
        ms.assortment.iter(filter=Filter().eq("type", "product", "consignment"))
    ms.assortment.iter(filter=Filter().eq("type", "consignment"), group_by="consignment")


def test_with_sub_folders_needs_a_product_folder(ms: MoySklad) -> None:
    folder = f"{API}/entity/productfolder/c56d0702-85c7-11e9-ac12-000d000000b1"
    with pytest.raises(ValueError, match="productFolder"):
        ms.assortment.iter(filter="withSubFolders=false")
    ms.assortment.iter(filter=f"productFolder={folder};withSubFolders=false")


@pytest.mark.parametrize("expand", ["agent", "product.supplier", ["images", "uom"]])
def test_expand_is_product_images_or_components_at_the_first_level(
    ms: MoySklad, expand: str | list[str]
) -> None:
    # § Ограничения expand.
    with pytest.raises(ValueError, match="expand"):
        ms.assortment.iter(expand=expand, limit=100)


@pytest.mark.parametrize("field", ["stock", "reserve", "inTransit", "quantity", "salePrice"])
def test_the_orders_the_new_endpoint_does_not_take_are_refused(ms: MoySklad, field: str) -> None:
    # § Недоступные сортировки.
    with pytest.raises(ValueError, match=field):
        ms.assortment.iter(order=f"name;{field},desc")


def test_an_unknown_grouping_is_refused(ms: MoySklad) -> None:
    with pytest.raises(ValueError, match="group_by"):
        ms.assortment.iter(group_by="bundle")  # type: ignore[arg-type]


async def test_rows_with_stock_fields_mean_the_header_was_ignored(
    ms: MoySklad, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    # _general.md § Заголовок X-Lognex-Remap-Beta-Feature: an unknown tag is ignored without
    # an error, and the request is served as without the header.
    mock.get("/entity/assortment").mock(
        return_value=page([{"id": P1, "stock": 1}, {"id": P2, "quantity": 2}])
    )
    caplog.set_level(logging.WARNING, logger="aiomoysklad")
    rows = [row async for row in ms.assortment.iter()]
    assert len(rows) == 2
    messages = [record.getMessage() for record in caplog.records]
    warnings = [message for message in messages if "assortmentWithoutStock" in message]
    assert len(warnings) == 1
