from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import Filter, MoySklad, MoySkladError, Paginator

from .conftest import API

ORDERS = f"{API}/entity/customerorder"


def page(
    rows: list[dict[str, Any]],
    *,
    offset: int,
    limit: int,
    size: int,
    next_href: str | None = None,
) -> httpx.Response:
    # The collection meta of _general.md § Мета коллекций.
    meta: dict[str, Any] = {
        "href": f"{ORDERS}?offset={offset}&limit={limit}",
        "type": "customerorder",
        "mediaType": "application/json",
        "size": size,
        "limit": limit,
        "offset": offset,
    }
    if next_href is not None:
        meta["nextHref"] = next_href
    return httpx.Response(200, json={"meta": meta, "rows": rows})


def orders(start: int, count: int) -> list[dict[str, Any]]:
    return [{"id": str(n), "name": f"{n:05d}"} for n in range(start, start + count)]


async def test_rows_are_read_across_pages_by_following_next_href(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    sent: list[httpx.Request] = []
    answers = [
        page(orders(0, 3), offset=0, limit=3, size=7, next_href=f"{ORDERS}?offset=3&limit=3"),
        page(orders(3, 3), offset=3, limit=3, size=7, next_href=f"{ORDERS}?limit=3&offset=6"),
        page(orders(6, 1), offset=6, limit=3, size=7),
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answers[len(sent) - 1]

    mock.get("/entity/customerorder").mock(side_effect=answer)
    paginator = ms.iter_rows("/entity/customerorder", limit=3)
    before = paginator.size
    names = [row["name"] async for row in paginator]
    assert names == [f"{n:05d}" for n in range(7)]
    assert (before, paginator.size) == (None, 7)
    assert [r.url.raw_path for r in sent] == [
        b"/api/remap/1.2/entity/customerorder?limit=3",
        b"/api/remap/1.2/entity/customerorder?offset=3&limit=3",
        b"/api/remap/1.2/entity/customerorder?limit=3&offset=6",
    ]


async def test_the_query_goes_into_the_first_request_only(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    sent: list[httpx.Request] = []
    # The server repeats the query in nextHref, already encoded, as in
    # workbook/_workbook_expand.md.
    next_href = f"{ORDERS}?expand=positions&fields=stock&filter=name%3D1&limit=100&offset=100"
    answers = [
        page(orders(0, 100), offset=0, limit=100, size=101, next_href=next_href),
        page(orders(100, 1), offset=100, limit=100, size=101),
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answers[len(sent) - 1]

    mock.get("/entity/customerorder").mock(side_effect=answer)
    rows = [
        row
        async for row in ms.iter_rows(
            "/entity/customerorder",
            filter=Filter().eq("name", "1"),
            order="updated,asc;id,asc",
            search="Иван",
            expand=["positions"],
            limit=100,
            # The example of _general.md § Что такое fields.
            params={"fields": "stock"},
        )
    ]
    assert len(rows) == 101
    assert sent[0].url.raw_path == (
        b"/api/remap/1.2/entity/customerorder?fields=stock&limit=100&filter=name%3D1"
        b"&order=updated%2Casc%3Bid%2Casc&search=%D0%98%D0%B2%D0%B0%D0%BD&expand=positions"
    )
    assert sent[1].url.raw_path == next_href.removeprefix("https://api.moysklad.ru").encode()


async def test_the_default_page_is_the_maximum_of_1000(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get("/entity/customerorder").mock(
        return_value=page([], offset=0, limit=1000, size=0)
    )
    assert [row async for row in ms.iter_rows("/entity/customerorder")] == []
    assert route.calls[0].request.url.params["limit"] == "1000"


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_the_limit_is_1_to_1000(ms: MoySklad, limit: int) -> None:
    # workbook/_workbook_filter_paging_search_sort.md § Листание: default 1000, maximum 1000.
    with pytest.raises(ValueError, match="limit"):
        ms.iter_rows("/entity/customerorder", limit=limit)


def test_expand_with_more_than_100_rows_is_refused(ms: MoySklad) -> None:
    # _general.md § На expand действуют следующие правила: with a larger limit the server
    # silently ignores expand.
    with pytest.raises(ValueError, match="100"):
        ms.iter_rows("/entity/customerorder", expand="agent", limit=101)
    ms.iter_rows("/entity/customerorder", expand="agent", limit=100)


def test_expand_deeper_than_three_levels_is_refused(ms: MoySklad) -> None:
    # Same section: the maximum nesting of expand is 3.
    ms.iter_rows("/entity/demand", expand="positions.assortment.product", limit=100)
    with pytest.raises(ValueError, match="nesting"):
        ms.iter_rows("/entity/demand", expand="a.b.c.d", limit=100)


async def test_a_next_href_that_does_not_move_forward_is_an_error(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    mock.get("/entity/customerorder").mock(
        return_value=page(
            orders(0, 2), offset=0, limit=2, size=10, next_href=f"{ORDERS}?offset=0&limit=2"
        )
    )
    paginator = ms.iter_rows("/entity/customerorder", limit=2)
    with pytest.raises(MoySkladError, match="forward"):
        _ = [row async for row in paginator]
    assert mock.calls.call_count == 1


async def test_a_next_href_without_an_offset_is_an_error(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    mock.get("/entity/customerorder").mock(
        return_value=page(orders(0, 2), offset=0, limit=2, size=10, next_href=f"{ORDERS}?limit=2")
    )
    with pytest.raises(MoySkladError, match="offset"):
        _ = [row async for row in ms.iter_rows("/entity/customerorder", limit=2)]


async def test_a_next_href_on_another_host_is_not_followed(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    mock.get("/entity/customerorder").mock(
        return_value=page(
            orders(0, 2),
            offset=0,
            limit=2,
            size=4,
            next_href="https://evil.example/api/remap/1.2/entity/customerorder?offset=2&limit=2",
        )
    )
    with pytest.raises(ValueError, match="outside"):
        _ = [row async for row in ms.iter_rows("/entity/customerorder", limit=2)]
    assert mock.calls.call_count == 1


@pytest.mark.parametrize("body", [[], {"meta": {}}, {"rows": "x"}, None])
async def test_an_answer_that_is_not_a_collection_is_an_error(
    ms: MoySklad, mock: respx.MockRouter, body: Any
) -> None:
    mock.get("/entity/customerorder").respond(200, json=body)
    with pytest.raises(MoySkladError, match="rows"):
        _ = [row async for row in ms.iter_rows("/entity/customerorder")]


async def test_pages_can_be_read_whole(ms: MoySklad, mock: respx.MockRouter) -> None:
    answers = iter(
        [
            page(orders(0, 2), offset=0, limit=2, size=3, next_href=f"{ORDERS}?offset=2&limit=2"),
            page(orders(2, 1), offset=2, limit=2, size=3),
        ]
    )
    mock.get("/entity/customerorder").mock(side_effect=lambda request: next(answers))
    paginator = ms.iter_rows("/entity/customerorder", limit=2)
    sizes = [len(body["rows"]) async for body in paginator.pages()]
    assert sizes == [2, 1]


async def test_a_paginator_starts_over_when_iterated_again(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get("/entity/customerorder").mock(
        return_value=page(orders(0, 1), offset=0, limit=1000, size=1)
    )
    paginator = ms.iter_rows("/entity/customerorder")
    assert len([r async for r in paginator]) == 1
    assert len([r async for r in paginator]) == 1
    assert route.call_count == 2


def test_the_paginator_is_exported() -> None:
    assert Paginator.__name__ == "Paginator"
