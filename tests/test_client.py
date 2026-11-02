from __future__ import annotations

import asyncio
import gzip
import json
import logging
from typing import Any

import httpx
import pytest
import respx

from aiomoysklad import (
    API_URL,
    AuthKind,
    BasicAuth,
    BearerToken,
    ConcurrencyLimit,
    ErrorGuard,
    GuardTrippedError,
    MoySklad,
    MoySkladError,
    NotFoundError,
    RateLimiter,
    RedirectError,
    __version__,
)
from aiomoysklad._client import encode_query

from .clock import FakeClock
from .conftest import API, TODAY, TOKEN

PRODUCT = "/entity/product/7944ef04-f831-11e5-7a69-971500188b19"


def recorder(sent: list[httpx.Request], response: httpx.Response | None = None) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response if response is not None else httpx.Response(200, json={"ok": True})

    return answer


def test_the_api_root_is_the_documented_server() -> None:
    assert API_URL == "https://api.moysklad.ru/api/remap/1.2"


async def test_every_request_sends_the_mandatory_headers(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    sent: list[httpx.Request] = []
    mock.get(PRODUCT).mock(side_effect=recorder(sent))
    assert await ms.get(PRODUCT) == {"ok": True}
    [request] = sent
    assert request.headers["Accept-Encoding"] == "gzip"
    assert request.headers["Accept"] == "application/json;charset=utf-8"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["User-Agent"] == f"aiomoysklad/{__version__}"
    assert "Content-Type" not in request.headers


async def test_a_client_passed_as_http_still_sends_only_gzip() -> None:
    # httpx asks for "gzip, deflate" (and more when brotli or zstd are installed); MoySklad
    # answers anything but plain gzip with 415 (_general.md § Сжатие содержимого ответов).
    sent: list[httpx.Request] = []
    transport = httpx.MockTransport(recorder(sent))
    async with httpx.AsyncClient(
        transport=transport,
        headers={"Accept-Encoding": "br", "Authorization": "Bearer other"},
        auth=("someone", "else"),
    ) as http:
        async with MoySklad(BasicAuth("admin@example", "secret"), http=http) as ms:
            await ms.get("/entity/product")
        assert not http.is_closed
    [request] = sent
    assert request.headers.get_list("Accept-Encoding") == ["gzip"]
    assert request.headers.get_list("Authorization") == [
        BasicAuth("admin@example", "secret").authorization
    ]


async def test_a_gzip_answer_is_decoded(ms: MoySklad, mock: respx.MockRouter) -> None:
    body = gzip.compress(json.dumps({"name": "Товар"}).encode())
    mock.get(PRODUCT).respond(
        200, content=body, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}
    )
    assert await ms.get(PRODUCT) == {"name": "Товар"}


async def test_a_body_is_sent_as_json(ms: MoySklad, mock: respx.MockRouter) -> None:
    sent: list[httpx.Request] = []
    mock.post("/entity/product").mock(side_effect=recorder(sent))
    await ms.request("POST", "entity/product", json={"name": "Товар"})
    [request] = sent
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {"name": "Товар"}


async def test_extra_headers_are_sent_but_cannot_replace_the_mandatory_ones(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    sent: list[httpx.Request] = []
    mock.get("/entity/assortment").mock(side_effect=recorder(sent))
    await ms.request(
        "GET",
        "/entity/assortment",
        headers={"X-Lognex-Remap-Beta-Feature": "assortmentWithoutStock"},
    )
    assert sent[0].headers["X-Lognex-Remap-Beta-Feature"] == "assortmentWithoutStock"
    for name in ("Accept-Encoding", "authorization"):
        with pytest.raises(ValueError, match="set by the client"):
            await ms.request("GET", "/entity/assortment", headers={name: "x"})
    assert len(sent) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/entity/product",
        "entity/product",
        "entity/product/",
        f"{API}/entity/product",
    ],
)
async def test_paths_and_hrefs_under_the_root_are_accepted(
    ms: MoySklad, mock: respx.MockRouter, path: str
) -> None:
    sent: list[httpx.Request] = []
    mock.get(url__regex=r"/entity/product/?$").mock(side_effect=recorder(sent))
    await ms.get(path)
    [request] = sent
    assert request.url.path.rstrip("/") == "/api/remap/1.2/entity/product"


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/api/remap/1.2/entity/product",
        "http://api.moysklad.ru/api/remap/1.2/entity/product",
        "https://api.moysklad.ru:8443/api/remap/1.2/entity/product",
        "https://api.moysklad.ru/api/remap/1.1/entity/product",
        "https://api.moysklad.ru/api/remap/1.2x/entity/product",
        "https://api.moysklad.ru.evil.example/api/remap/1.2/entity/product",
    ],
)
async def test_hrefs_elsewhere_are_refused_before_sending(
    ms: MoySklad, mock: respx.MockRouter, href: str
) -> None:
    with pytest.raises(ValueError, match="outside"):
        await ms.get(href)
    assert not mock.calls


def test_the_query_is_percent_encoded_with_20_for_spaces() -> None:
    query = encode_query(
        {
            "filter": "name=Иван Петров;updated>=2026-11-01 09:00:00",
            "order": "updated,asc;id,asc",
            "limit": 100,
            "search": "a+b&c",
            "flag": True,
            "skipped": None,
            "expand": ["agent", "state"],
        }
    )
    assert query == (
        "filter=name%3D%D0%98%D0%B2%D0%B0%D0%BD%20%D0%9F%D0%B5%D1%82%D1%80%D0%BE%D0%B2"
        "%3Bupdated%3E%3D2026-11-01%2009%3A00%3A00"
        "&order=updated%2Casc%3Bid%2Casc&limit=100&search=a%2Bb%26c&flag=true"
        "&expand=agent&expand=state"
    )


async def test_the_encoded_query_reaches_the_wire_unchanged(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    sent: list[httpx.Request] = []
    mock.get("/entity/customerorder").mock(side_effect=recorder(sent))
    await ms.get("/entity/customerorder", params={"filter": "name=a b;sum>1"})
    assert sent[0].url.raw_path == (
        b"/api/remap/1.2/entity/customerorder?filter=name%3Da%20b%3Bsum%3E1"
    )


async def test_params_are_appended_to_the_query_of_an_href(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    sent: list[httpx.Request] = []
    mock.get("/entity/customerorder").mock(side_effect=recorder(sent))
    await ms.get(f"{API}/entity/customerorder?offset=1000&limit=1000", params={"expand": "agent"})
    assert sent[0].url.raw_path == (
        b"/api/remap/1.2/entity/customerorder?offset=1000&limit=1000&expand=agent"
    )


async def test_headers_over_8_kb_are_refused_before_sending(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # _restrictions.md § Лимиты и ограничения в JSON API: at most 8 KB of request headers,
    # the URL included.
    ids = ";".join(f"id=7944ef04-f831-11e5-7a69-9715001{n:05d}" for n in range(200))
    with pytest.raises(ValueError, match="8192-byte limit"):
        await ms.get("/entity/product", params={"filter": ids})
    assert not mock.calls
    assert ms.limiter.available() == 45


async def test_headers_just_under_8_kb_are_sent(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get("/entity/product").respond(200, json={})
    await ms.get("/entity/product", params={"search": "x" * 7000})
    assert mock.calls.call_count == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_are_not_followed(
    ms: MoySklad, mock: respx.MockRouter, status: int
) -> None:
    mock.get(PRODUCT).respond(status, headers={"Location": "https://elsewhere.example/x"})
    with pytest.raises(MoySkladError) as caught:
        await ms.get(PRODUCT)
    assert caught.value.status == status
    if status != 307:
        assert isinstance(caught.value, RedirectError)
    assert mock.calls.call_count == 1


async def test_an_error_answer_is_raised_typed(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(PRODUCT).respond(
        404,
        json={
            "errors": [
                {
                    "error": "Объект с типом 'product' и идентификатором '7944' не найден",
                    "code": 1021,
                }
            ]
        },
    )
    with pytest.raises(NotFoundError) as caught:
        await ms.get(PRODUCT)
    assert caught.value.codes == [1021]


@pytest.mark.parametrize("response", [httpx.Response(204), httpx.Response(200, content=b"")])
async def test_an_empty_answer_is_none(
    ms: MoySklad, mock: respx.MockRouter, response: httpx.Response
) -> None:
    mock.delete(PRODUCT).mock(return_value=response)
    assert await ms.request("DELETE", PRODUCT) is None


async def test_a_success_that_is_not_json_is_an_error(ms: MoySklad, mock: respx.MockRouter) -> None:
    mock.get(PRODUCT).respond(200, text="<html>maintenance</html>")
    with pytest.raises(MoySkladError, match="not JSON"):
        await ms.get(PRODUCT)


async def test_webhook_prefixes_go_in_one_header_each(mock: respx.MockRouter) -> None:
    # dictionaries/_webhook.md § Заголовок временного отключения
    # X-Lognex-WebHook-DisableByPrefix через API: one header per prefix, no comma lists.
    sent: list[httpx.Request] = []
    mock.put(PRODUCT).mock(side_effect=recorder(sent))
    prefixes = ["https://hooks.example.com/ms", "https://other.example.org"]
    async with MoySklad(BearerToken(TOKEN), webhook_disable_prefixes=prefixes) as ms:
        await ms.request("PUT", PRODUCT, json={"name": "x"})
    assert sent[0].headers.get_list("X-Lognex-WebHook-DisableByPrefix") == prefixes
    assert "X-Lognex-WebHook-Disable" not in sent[0].headers


@pytest.mark.parametrize(
    "prefix", ["https://", "https://some_url", "ftp://hooks.example.com", "hooks.example.com"]
)
def test_a_webhook_prefix_needs_a_scheme_and_a_full_domain(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        MoySklad(BearerToken(TOKEN), webhook_disable_prefixes=[prefix])


async def test_a_deprecated_version_is_warned_about_once(
    ms: MoySklad, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock.get(PRODUCT).respond(
        200, json={}, headers={"X-Lognex-API-Version-Deprecated": "2027-01-01"}
    )
    with caplog.at_level(logging.WARNING, logger="aiomoysklad"):
        await ms.get(PRODUCT)
        await ms.get(PRODUCT)
    warnings = [r for r in caplog.records if "2027-01-01" in r.getMessage()]
    assert len(warnings) == 1


async def test_the_weight_of_each_request_is_charged(
    clock: FakeClock, mock: respx.MockRouter
) -> None:
    mock.get(PRODUCT).respond(200, json={})
    mock.get("/report/stock/all/current").respond(200, json=[])
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    async with MoySklad(BearerToken(TOKEN), limiter=limiter, today=lambda: TODAY) as ms:
        await ms.get(PRODUCT)
        assert limiter.available() == 42  # a user token costs 3 units on 2026-11-02
        await ms.get("/report/stock/all/current")
        assert limiter.available() == 37
        await ms.get(PRODUCT, params={"expand": "images"})
        assert limiter.available() == 34
        await ms.request("GET", PRODUCT, weight=10)
        assert limiter.available() == 24


async def test_the_weight_follows_the_date(clock: FakeClock, mock: respx.MockRouter) -> None:
    mock.get(PRODUCT).respond(200, json={})
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    days = iter([TODAY, TODAY.replace(month=12, day=1)])
    async with MoySklad(
        BasicAuth("admin@example", "secret"), limiter=limiter, today=lambda: next(days)
    ) as ms:
        await ms.get(PRODUCT)
        await ms.get(PRODUCT)
    assert limiter.available() == 45 - 3 - 4


async def test_weights_can_be_overridden_by_path(clock: FakeClock, mock: respx.MockRouter) -> None:
    mock.get("/report/stock/all/current").respond(200, json=[])
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    async with MoySklad(
        BearerToken(TOKEN, kind=AuthKind.SOLUTION),
        limiter=limiter,
        weights={"/report/stock/all/current": 1},
    ) as ms:
        await ms.get("/report/stock/all/current")
    assert limiter.available() == 44


async def test_requests_wait_for_the_basket(clock: FakeClock, mock: respx.MockRouter) -> None:
    sent_at: list[float] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent_at.append(clock.now)
        return httpx.Response(200, json={})

    mock.get(PRODUCT).mock(side_effect=answer)
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    async with MoySklad(BearerToken(TOKEN), limiter=limiter, today=lambda: TODAY) as ms:
        await asyncio.gather(*(ms.get(PRODUCT) for _ in range(20)))
    # 15 requests of 3 units fill the basket; the next 5 go 3 seconds later.
    assert sent_at[:15] == [1000.0] * 15
    assert sent_at[15:] == pytest.approx([1003.0] * 5)


async def test_no_more_than_five_requests_are_in_flight(mock: respx.MockRouter) -> None:
    in_flight = peak = 0

    async def answer(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1
        return httpx.Response(200, json={})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http,
        MoySklad(BearerToken(TOKEN, kind=AuthKind.SOLUTION), http=http) as ms,
    ):
        await asyncio.gather(*(ms.get(PRODUCT) for _ in range(30)))
    assert peak == 5


async def test_a_shared_cap_holds_across_clients(mock: respx.MockRouter) -> None:
    in_flight = peak = 0

    async def answer(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1
        return httpx.Response(200, json={})

    shared = ConcurrencyLimit()
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        first = MoySklad(BearerToken(TOKEN, kind=AuthKind.SOLUTION), http=http, concurrency=shared)
        second = MoySklad(BearerToken(TOKEN, kind=AuthKind.SOLUTION), http=http, concurrency=shared)
        await asyncio.gather(*(c.get(PRODUCT) for c in (first, second) for _ in range(15)))
    assert peak == 5


async def test_a_refused_request_spends_nothing_and_sends_nothing(
    clock: FakeClock, mock: respx.MockRouter
) -> None:
    mock.get(PRODUCT).respond(400, json={"errors": [{"error": "bad"}]})
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    guard = ErrorGuard(identical_errors=2, clock=clock)
    async with MoySklad(
        BearerToken(TOKEN, kind=AuthKind.SOLUTION), limiter=limiter, guard=guard
    ) as ms:
        for _ in range(2):
            with pytest.raises(MoySkladError):
                await ms.get(PRODUCT)
        with pytest.raises(GuardTrippedError):
            await ms.get(PRODUCT)
    assert mock.calls.call_count == 2
    assert limiter.available() == 43


async def test_puts_are_counted_per_entity(clock: FakeClock, mock: respx.MockRouter) -> None:
    mock.put(PRODUCT).respond(200, json={})
    guard = ErrorGuard(puts_per_entity=3, clock=clock)
    async with MoySklad(
        BearerToken(TOKEN, kind=AuthKind.SOLUTION),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        guard=guard,
    ) as ms:
        for n in range(3):
            await ms.request("PUT", PRODUCT, json={"name": str(n)})
        with pytest.raises(GuardTrippedError, match="puts_per_entity"):
            await ms.request("PUT", PRODUCT, json={"name": "again"})


async def test_the_rate_headers_tune_the_shared_limiter(
    clock: FakeClock, mock: respx.MockRouter
) -> None:
    mock.get(PRODUCT).respond(
        200, json={}, headers={"X-RateLimit-Limit": "15", "X-RateLimit-Remaining": "2"}
    )
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    async with MoySklad(BearerToken(TOKEN), limiter=limiter, today=lambda: TODAY) as ms:
        await ms.get(PRODUCT)
    assert limiter.available() == 6  # 2 of 15 left: 45 * 2 / 15


async def test_the_debug_log_shows_the_weight_and_the_remaining_count(
    ms: MoySklad, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock.get(PRODUCT).respond(
        200, json={}, headers={"X-RateLimit-Limit": "45", "X-RateLimit-Remaining": "44"}
    )
    with caplog.at_level(logging.DEBUG, logger="aiomoysklad"):
        await ms.get(PRODUCT)
    assert any("weight 1, remaining 44 of 45" in r.getMessage() for r in caplog.records)


async def test_the_query_stays_out_of_the_log(
    ms: MoySklad, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock.get("/entity/counterparty").respond(200, json={})
    with caplog.at_level(logging.DEBUG, logger="aiomoysklad"):
        await ms.get("/entity/counterparty", params={"search": "+79260000000"})
    assert "79260000000" not in caplog.text


async def test_the_client_closes_only_its_own_http_client() -> None:
    ms = MoySklad(BearerToken(TOKEN))
    await ms.aclose()
    assert ms.http.is_closed
    await ms.aclose()  # twice is fine


@pytest.mark.parametrize(
    "base_url",
    ["api.moysklad.ru/api/remap/1.2", "ftp://api.moysklad.ru", "https://x.example/api?a=1"],
)
def test_a_bad_base_url_is_refused(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        MoySklad(BearerToken(TOKEN), base_url=base_url)


async def test_a_test_server_can_be_the_base_url() -> None:
    sent: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(recorder(sent))) as http,
        MoySklad(
            BearerToken(TOKEN), base_url="http://localhost:8080/api/remap/1.2/", http=http
        ) as ms,
    ):
        assert ms.base_url == "http://localhost:8080/api/remap/1.2"
        await ms.get(f"{ms.base_url}/entity/product")
        await ms.get("/entity/product")
    assert [str(r.url) for r in sent] == ["http://localhost:8080/api/remap/1.2/entity/product"] * 2


def test_the_repr_hides_the_credentials() -> None:
    text = repr(MoySklad(BearerToken(TOKEN, kind=AuthKind.SOLUTION)))
    assert TOKEN not in text
    assert "SOLUTION" in text
