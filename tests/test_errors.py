from __future__ import annotations

from typing import Any

import httpx
import pytest

from aiomoysklad import (
    ApiErrorDetail,
    ApiVersionGoneError,
    AuthError,
    ConflictError,
    ForbiddenError,
    Meta,
    MoySkladError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitError,
    RedirectError,
    ServerError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from aiomoysklad._errors import error_from_response, parse_errors

API = "https://api.moysklad.ru/api/remap/1.2"

# Error 1021 of _errors.md, with its message template filled in.
NOT_FOUND = {
    "errors": [
        {
            "error": "Объект с типом 'product' и идентификатором '8e9ab1bb' не найден",
            "code": 1021,
            "moreInfo": "https://api.moysklad.ru/api/remap/1.2/doc#обработка-ошибок-1021",
        }
    ]
}

# The answer to a product created without a name, from workbook/_workbook_first_steps.md.
NAME_MISSING = {
    "errors": [
        {
            "error": (
                "Ошибка сохранения объекта: поле 'name' не может быть пустым или отсутствовать"
            ),
            "code": 3000,
            "parameter": "name",
            "moreInfo": "https://api.moysklad.ru/api/remap/1.2/doc#обработка-ошибок-3000",
            "line": 1,
            "column": 2,
        }
    ]
}


def answer(
    status: int,
    body: Any = None,
    *,
    method: str = "GET",
    url: str = f"{API}/entity/product/8e9ab1bb?filter=name=secret",
    headers: dict[str, str] | None = None,
    text: str | None = None,
) -> httpx.Response:
    request = httpx.Request(method, url)
    if text is not None:
        return httpx.Response(status, text=text, headers=headers, request=request)
    if body is None:
        return httpx.Response(status, headers=headers, request=request)
    return httpx.Response(status, json=body, headers=headers, request=request)


def test_an_error_body_is_parsed_into_details() -> None:
    [detail] = parse_errors(NOT_FOUND)
    assert detail.error.startswith("Объект с типом 'product'")
    assert detail.code == 1021
    assert detail.more_info == "https://api.moysklad.ru/api/remap/1.2/doc#обработка-ошибок-1021"
    assert detail.parameter is None


def test_the_documented_validation_example_is_parsed() -> None:
    [detail] = parse_errors(NAME_MISSING)
    assert detail.code == 3000
    assert detail.parameter == "name"
    assert (detail.line, detail.column) == (1, 2)


def test_every_documented_field_is_read() -> None:
    meta = {
        "href": f"{API}/entity/demand/7944ef04-f831-11e5-7a69-971500188b19",
        "type": "demand",
        "mediaType": "application/json",
    }
    body = {
        "errors": [
            {
                "error": "Невозможно удалить объект: на него есть ссылки",
                "parameter": "positions",
                "code": 1023,
                "error_message": "сообщение",
                "moreInfo": "https://api.moysklad.ru/api/remap/1.2/doc#обработка-ошибок-1023",
                "line": 3,
                "column": 14,
                "dependencies": [meta],
                "meta": meta,
                "someNewField": True,
            }
        ]
    }
    [detail] = parse_errors(body)
    assert detail.parameter == "positions"
    assert detail.error_message == "сообщение"
    assert (detail.line, detail.column) == (3, 14)
    assert detail.dependencies is not None
    assert detail.dependencies[0].href == meta["href"]
    assert detail.meta == Meta.model_validate(meta)
    assert detail.meta.media_type == "application/json"
    # New fields may appear without notice (_general.md § Замечания по разработке клиентских
    # приложений); they are kept, not refused.
    assert detail.model_extra == {"someNewField": True}


def test_a_batch_answer_holds_one_errors_object_per_item() -> None:
    # openapi: src/components/responses.yaml, BatchBadRequest.
    body = [
        {"errors": [{"error": "first", "code": 3000}]},
        {"meta": {"href": f"{API}/entity/product/1", "type": "product"}},
        {"errors": [{"error": "second", "code": 3001}, {"error": "third"}]},
    ]
    assert [d.error for d in parse_errors(body)] == ["first", "second", "third"]


@pytest.mark.parametrize(
    "body",
    [None, "", "<html>Bad gateway</html>", 42, {}, {"errors": "x"}, {"errors": [1, None]}, [3]],
)
def test_bodies_that_are_not_error_objects_give_no_details(body: Any) -> None:
    assert parse_errors(body) == []


def test_a_malformed_detail_keeps_its_text() -> None:
    [detail] = parse_errors({"errors": [{"error": "broken", "code": "not a number"}]})
    assert detail.error == "broken"
    assert detail.code is None


def test_a_detail_without_an_error_title_is_still_kept() -> None:
    [detail] = parse_errors({"errors": [{"code": 1021}]})
    assert detail.code == 1021
    assert detail.error == ""


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, ValidationError),
        (401, AuthError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
        (410, ApiVersionGoneError),
        (412, ValidationError),
        (413, PayloadTooLargeError),
        (415, UnsupportedMediaTypeError),
        (429, RateLimitError),
        (500, ServerError),
        (502, ServerError),
        (503, ServerError),
        (504, ServerError),
        (507, ServerError),
        (405, MoySkladError),
        (414, MoySkladError),
    ],
)
def test_each_documented_status_has_its_exception(status: int, kind: type[MoySkladError]) -> None:
    error = error_from_response(answer(status, NOT_FOUND))
    assert type(error) is kind
    assert error.status == status


def test_the_message_names_the_status_text_code_and_path_but_not_the_query() -> None:
    error = error_from_response(answer(404, NOT_FOUND))
    text = str(error)
    assert text.startswith("HTTP 404: Объект с типом")
    assert "code 1021" in text
    assert "GET /api/remap/1.2/entity/product/8e9ab1bb" in text
    assert "secret" not in text
    assert error.url == f"{API}/entity/product/8e9ab1bb"
    assert error.method == "GET"
    assert error.codes == [1021]
    assert error.errors == parse_errors(NOT_FOUND)


def test_several_errors_are_counted_in_the_message() -> None:
    body = {"errors": [{"error": "a", "code": 1}, {"error": "b"}, {"error": "c"}]}
    assert "(and 2 more)" in str(error_from_response(answer(400, body)))


def test_the_parameter_is_named_in_the_message() -> None:
    error = error_from_response(answer(400, NAME_MISSING, method="POST"))
    assert isinstance(error, ValidationError)
    assert "parameter name" in str(error)
    assert "POST /api/remap/1.2/entity/product" in str(error)


def test_the_extended_auth_headers_are_kept() -> None:
    error = error_from_response(
        answer(
            401,
            {
                "errors": [
                    {
                        "error": "Ошибка аутентификации: Неправильный пароль или имя пользователя",
                        "code": 1056,
                    }
                ]
            },
            # The docs name these headers but give no example values.
            headers={"X-Lognex-Auth": "some-code", "X-Lognex-Auth-Message": "some message"},
        )
    )
    assert isinstance(error, AuthError)
    assert error.auth_code == "some-code"
    assert error.auth_message == "some message"
    assert "some message" in str(error)


def test_an_html_error_page_is_quoted_shortly() -> None:
    error = error_from_response(answer(502, text="<html>" + "x" * 1000 + "</html>"))
    assert isinstance(error, ServerError)
    assert error.errors == []
    assert error.body.startswith("<html>")
    assert len(str(error)) < 400


def test_a_415_without_a_body_points_at_the_encoding() -> None:
    # _general.md § Сжатие содержимого ответов: 415 with no body when Accept-Encoding is not
    # gzip.
    error = error_from_response(answer(415))
    assert isinstance(error, UnsupportedMediaTypeError)
    assert error.body is None
    assert "Accept-Encoding" in str(error)


def test_a_429_for_too_many_parallel_requests_is_marked() -> None:
    # _general.md § Обработка ошибок; _errors.md, 1073.
    body = {
        "errors": [
            {"error": "Превышено ограничение на одновременное количество запросов", "code": 1073}
        ]
    }
    error = error_from_response(answer(429, body), retry_after=1.5)
    assert isinstance(error, RateLimitError)
    assert error.parallel
    assert error.retry_after == 1.5


def test_a_plain_429_is_not_marked_parallel() -> None:
    error = error_from_response(
        answer(
            429,
            {
                "errors": [
                    {
                        "error": "Превышено ограничение на количество запросов в единицу времени",
                        "code": 1049,
                    }
                ]
            },
        )
    )
    assert isinstance(error, RateLimitError)
    assert not error.parallel
    assert error.retry_after is None


@pytest.mark.parametrize("status", [301, 302, 303, 308])
def test_a_redirect_keeps_its_location(status: int) -> None:
    location = f"{API}/entity/product/other?token=x"
    error = error_from_response(answer(status, headers={"Location": location}))
    assert isinstance(error, RedirectError)
    assert error.location == location
    assert "redirected to https://api.moysklad.ru/api/remap/1.2/entity/product/other" in str(error)
    assert "token=x" not in str(error)


def test_a_redirect_without_a_location_is_still_a_redirect_error() -> None:
    error = error_from_response(answer(302))
    assert isinstance(error, RedirectError)
    assert error.location is None


def test_the_detail_model_reads_the_documented_names() -> None:
    detail = ApiErrorDetail.model_validate({"error": "e", "moreInfo": "https://x"})
    assert detail.more_info == "https://x"


def test_an_unparseable_location_is_not_quoted() -> None:
    error = error_from_response(answer(301, headers={"Location": "https://[::1"}))
    assert isinstance(error, RedirectError)
    assert error.location == "https://[::1"
    assert str(error).endswith(": redirected")
