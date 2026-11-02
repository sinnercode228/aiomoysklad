from __future__ import annotations

import httpx
import pytest
import respx

from aiomoysklad import AuthError, BasicAuth, MoySkladError, issue_user_token

from .conftest import API, TOKEN


async def test_a_new_token_is_issued_with_basic_auth(mock: respx.MockRouter) -> None:
    # _general.md § Получение нового токена, request and answer of the example.
    route = mock.post("/security/token").respond(200, json={"access_token": TOKEN})
    assert await issue_user_token("admin@example", "secret") == TOKEN
    [call] = route.calls
    assert (
        call.request.headers["Authorization"] == BasicAuth("admin@example", "secret").authorization
    )
    assert call.request.headers["Accept-Encoding"] == "gzip"
    assert call.request.content == b""


async def test_wrong_credentials_raise_auth_error(mock: respx.MockRouter) -> None:
    mock.post("/security/token").respond(
        401,
        # _errors.md, 1056, with the 401 text of _general.md § Обработка ошибок filled in.
        json={
            "errors": [
                {
                    "error": "Ошибка аутентификации: Имя и/или пароль пользователя указаны неверно",
                    "code": 1056,
                }
            ]
        },
    )
    with pytest.raises(AuthError):
        await issue_user_token("admin@example", "wrong")


@pytest.mark.parametrize("body", [{}, {"access_token": ""}, {"access_token": 1}, []])
async def test_an_answer_without_a_token_is_an_error(mock: respx.MockRouter, body: object) -> None:
    mock.post("/security/token").respond(200, json=body)
    with pytest.raises(MoySkladError, match="access_token"):
        await issue_user_token("admin@example", "secret")


async def test_a_429_is_not_resent(mock: respx.MockRouter) -> None:
    # Each new token revokes the earlier ones, so the request is not repeated blindly.
    route = mock.post("/security/token").respond(429, headers={"X-Lognex-Retry-After": "10"})
    with pytest.raises(MoySkladError):
        await issue_user_token("admin@example", "secret")
    assert route.call_count == 1


async def test_a_given_http_client_is_used_and_left_open() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"access_token": TOKEN})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        token = await issue_user_token(
            "admin@example", "secret", http=http, base_url="http://localhost:8080/api/remap/1.2"
        )
        assert not http.is_closed
    assert token == TOKEN
    assert str(sent[0].url) == "http://localhost:8080/api/remap/1.2/security/token"
    assert API.startswith("https://")
