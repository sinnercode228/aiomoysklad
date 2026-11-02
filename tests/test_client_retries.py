from __future__ import annotations

import httpx
import pytest
import respx

from aiomoysklad import (
    AuthKind,
    BearerToken,
    ErrorGuard,
    MoySklad,
    NetworkError,
    RateLimiter,
    RateLimitError,
    RetryPolicy,
    ServerError,
)

from .clock import FakeClock
from .conftest import TOKEN

PRODUCT = "/entity/product/7944ef04-f831-11e5-7a69-971500188b19"
SYNC_ID = "c6d6bb8c-4fd2-4f40-a5f1-1d0a0b1c2d3e"

# _errors.md, 1049 and 1073.
RATE = {
    "errors": [
        {"error": "Превышено ограничение на количество запросов в единицу времени", "code": 1049}
    ]
}
PARALLEL = {
    "errors": [
        {"error": "Превышено ограничение на одновременное количество запросов", "code": 1073}
    ]
}
OK = httpx.Response(200, json={"ok": True})


def limited(retry_after: str | None = None, reset: str | None = None) -> httpx.Response:
    headers = {"X-RateLimit-Limit": "45", "X-RateLimit-Remaining": "0"}
    if retry_after is not None:
        headers["X-Lognex-Retry-After"] = retry_after
    if reset is not None:
        headers["X-Lognex-Reset"] = reset
    return httpx.Response(429, json=RATE, headers=headers)


def client(clock: FakeClock, retry: RetryPolicy) -> MoySklad:
    return MoySklad(
        BearerToken(TOKEN, kind=AuthKind.SOLUTION),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        guard=ErrorGuard(clock=clock),
        retry=retry,
    )


async def test_a_429_waits_for_x_lognex_retry_after(
    ms: MoySklad, mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = mock.get(PRODUCT)
    route.side_effect = [limited(retry_after="2500", reset="1000"), OK]
    assert await ms.get(PRODUCT) == {"ok": True}
    assert route.call_count == 2
    assert clock.sleeps[0] == pytest.approx(2.5)


async def test_a_429_without_retry_after_waits_for_the_reset(
    ms: MoySklad, mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = mock.get(PRODUCT)
    route.side_effect = [limited(reset="1200"), OK]
    await ms.get(PRODUCT)
    assert clock.sleeps[0] == pytest.approx(1.2)


async def test_a_429_holds_the_other_requests_of_the_limiter(
    mock: respx.MockRouter, clock: FakeClock
) -> None:
    only_retry_after = httpx.Response(429, json=RATE, headers={"X-Lognex-Retry-After": "2000"})
    mock.get(PRODUCT).mock(return_value=only_retry_after)
    mock.get("/entity/service").mock(return_value=OK)
    start = clock.now
    async with client(clock, RetryPolicy(max_retries=0)) as ms:
        with pytest.raises(RateLimitError) as caught:
            await ms.get(PRODUCT)
        assert caught.value.retry_after == pytest.approx(2.0)
        assert clock.now == start  # not retried, so the client did not sleep itself
        await ms.get("/entity/service")
    assert clock.now - start == pytest.approx(2.0)


async def test_a_429_with_nothing_remaining_empties_the_basket(
    mock: respx.MockRouter, clock: FakeClock
) -> None:
    mock.get(PRODUCT).mock(return_value=limited(retry_after="2000"))
    mock.get("/entity/service").mock(return_value=OK)
    start = clock.now
    async with client(clock, RetryPolicy(max_retries=0)) as ms:
        with pytest.raises(RateLimitError):
            await ms.get(PRODUCT)
        await ms.get("/entity/service")
    # Held for the 2 s of the header, then until the emptied basket refills a window later.
    assert clock.now - start == pytest.approx(3.0)


async def test_too_many_parallel_requests_back_off_without_holding_the_limiter(
    ms: MoySklad, mock: respx.MockRouter, clock: FakeClock
) -> None:
    # The rate headers do not reflect the parallel limit (_general.md § Обработка ошибок).
    route = mock.get(PRODUCT)
    route.side_effect = [httpx.Response(429, json=PARALLEL), OK]
    await ms.get(PRODUCT)
    assert route.call_count == 2
    [wait] = clock.sleeps
    assert 0.25 <= wait <= 0.5


async def test_a_parallel_429_backs_off_whatever_its_rate_headers_say(
    ms: MoySklad, mock: respx.MockRouter, clock: FakeClock
) -> None:
    # The headers describe the 3-second basket, not the requests in flight that error 1073 is
    # about (_general.md § Обработка ошибок), so they are not trusted for it.
    route = mock.get(PRODUCT)
    route.side_effect = [
        httpx.Response(
            429, json=PARALLEL, headers={"X-Lognex-Retry-After": "20000", "X-Lognex-Reset": "20000"}
        ),
        OK,
    ]
    await ms.get(PRODUCT)
    [wait] = clock.sleeps
    assert 0.25 <= wait <= 0.5
    assert ms.limiter.available() == 45 - 2


async def test_a_parallel_429_that_is_not_retried_is_marked(
    mock: respx.MockRouter, clock: FakeClock
) -> None:
    mock.get(PRODUCT).mock(return_value=httpx.Response(429, json=PARALLEL))
    async with client(clock, RetryPolicy(max_retries=0)) as ms:
        with pytest.raises(RateLimitError) as caught:
            await ms.get(PRODUCT)
    assert caught.value.parallel
    assert ms.limiter.available() == 44


async def test_retries_stop_after_max_retries(
    ms: MoySklad, mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = mock.get(PRODUCT).mock(return_value=limited(retry_after="100"))
    with pytest.raises(RateLimitError) as caught:
        await ms.get(PRODUCT)
    assert route.call_count == 4  # the first attempt and 3 retries
    assert caught.value.retry_after == pytest.approx(0.1)


async def test_a_wait_over_max_wait_is_raised_instead_of_slept(
    mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = mock.get(PRODUCT).mock(return_value=limited(retry_after="90000"))
    async with client(clock, RetryPolicy(max_wait=60.0)) as ms:
        with pytest.raises(RateLimitError) as caught:
            await ms.get(PRODUCT)
    assert route.call_count == 1
    assert caught.value.retry_after == pytest.approx(90.0)
    assert clock.sleeps == []


async def test_a_post_without_sync_id_is_not_resent(
    ms: MoySklad, mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = mock.post("/entity/customerorder").mock(return_value=limited(retry_after="100"))
    with pytest.raises(RateLimitError) as caught:
        await ms.request("POST", "/entity/customerorder", json={"name": "0001"})
    assert route.call_count == 1
    assert caught.value.retry_after == pytest.approx(0.1)


async def test_a_post_with_sync_id_is_resent_with_the_same_body(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # _general.md § Назначение поля syncId.
    route = mock.post("/entity/customerorder")
    route.side_effect = [limited(retry_after="100"), OK]
    body = {"name": "0001", "syncId": SYNC_ID}
    await ms.request("POST", "/entity/customerorder", json=body)
    assert route.call_count == 2
    first, second = (call.request for call in route.calls)
    assert first.content == second.content


async def test_unsafe_writes_are_resent_on_request(
    mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = mock.post("/entity/customerorder")
    route.side_effect = [limited(retry_after="100"), OK]
    async with client(clock, RetryPolicy(retry_unsafe_writes=True)) as ms:
        await ms.request("POST", "/entity/customerorder", json={"name": "0001"})
    assert route.call_count == 2


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_temporary_server_errors_are_retried_with_backoff(
    ms: MoySklad, mock: respx.MockRouter, clock: FakeClock, status: int
) -> None:
    route = mock.get(PRODUCT)
    route.side_effect = [httpx.Response(status), httpx.Response(status), OK]
    assert await ms.get(PRODUCT) == {"ok": True}
    assert len(clock.sleeps) == 2
    assert 0.25 <= clock.sleeps[0] <= 0.5
    assert 0.5 <= clock.sleeps[1] <= 1.0


async def test_a_500_is_not_retried(ms: MoySklad, mock: respx.MockRouter) -> None:
    # The same failing request sent again counts toward the cut-off of
    # _restrictions.md § Автоматическое отключение доступа.
    route = mock.get(PRODUCT).mock(return_value=httpx.Response(500, json={"errors": []}))
    with pytest.raises(ServerError):
        await ms.get(PRODUCT)
    assert route.call_count == 1


async def test_put_and_delete_are_retried(ms: MoySklad, mock: respx.MockRouter) -> None:
    put = mock.put(PRODUCT)
    put.side_effect = [httpx.Response(503), OK]
    delete = mock.delete(PRODUCT)
    delete.side_effect = [httpx.Response(503), httpx.Response(200)]
    await ms.request("PUT", PRODUCT, json={"name": "x"})
    await ms.request("DELETE", PRODUCT)
    assert (put.call_count, delete.call_count) == (2, 2)


async def test_a_failed_connection_is_retried_for_any_method(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    # Nothing was sent, so nothing can be created twice.
    route = mock.post("/entity/customerorder")
    route.side_effect = [httpx.ConnectError("refused"), OK]
    await ms.request("POST", "/entity/customerorder", json={"name": "0001"})
    assert route.call_count == 2


async def test_a_timeout_after_sending_is_retried_only_when_safe(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    get = mock.get(PRODUCT)
    get.side_effect = [httpx.ReadTimeout("slow"), OK]
    await ms.get(PRODUCT)
    assert get.call_count == 2

    post = mock.post("/entity/customerorder").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(NetworkError) as caught:
        await ms.request("POST", "/entity/customerorder", json={"name": "0001"})
    assert post.call_count == 1
    assert caught.value.status is None
    assert caught.value.url == "https://api.moysklad.ru/api/remap/1.2/entity/customerorder"


async def test_network_errors_give_up_after_max_retries(
    ms: MoySklad, mock: respx.MockRouter
) -> None:
    route = mock.get(PRODUCT).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NetworkError, match="ConnectError"):
        await ms.get(PRODUCT)
    assert route.call_count == 4


async def test_each_retry_spends_its_weight_again(ms: MoySklad, mock: respx.MockRouter) -> None:
    route = mock.get("/report/stock/all")
    route.side_effect = [httpx.Response(503), OK]
    await ms.get("/report/stock/all")
    assert ms.limiter.available() == 45 - 2 * 5


async def test_retries_turned_off(mock: respx.MockRouter, clock: FakeClock) -> None:
    route = mock.get(PRODUCT).mock(return_value=httpx.Response(503))
    async with client(clock, RetryPolicy(max_retries=0)) as ms:
        with pytest.raises(ServerError):
            await ms.get(PRODUCT)
    assert route.call_count == 1
