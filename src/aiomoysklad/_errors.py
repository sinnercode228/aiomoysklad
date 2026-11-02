"""Exceptions, and the mapping from MoySklad error answers to them.

An error answer is ``{"errors": [error, ...]}`` (``_general.md § Обработка ошибок``;
``openapi: src/components/schemas/common/errors.yaml``). A bulk request can be answered 400
with an array of such objects (``openapi: src/components/responses.yaml``, ``BatchBadRequest``),
and an error can also come as ``text/html`` (``CommonError``), so every field is read
defensively and the raw body is kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pydantic

from ._models import ApiErrorDetail

__all__ = [
    "ApiVersionGoneError",
    "AuthError",
    "ConflictError",
    "ForbiddenError",
    "GuardTrippedError",
    "MoySkladError",
    "NetworkError",
    "NotFoundError",
    "PayloadTooLargeError",
    "RateLimitError",
    "RedirectError",
    "ServerError",
    "UnsupportedMediaTypeError",
    "ValidationError",
    "error_from_response",
    "parse_errors",
]

# _errors.md, 1073: "Превышено ограничение на одновременное количество запросов".
PARALLEL_LIMIT_CODE = 1073

_MAX_TEXT = 200


class MoySkladError(Exception):
    """Base class of every error raised by aiomoysklad.

    Attributes:
        message: The exception text.
        status: HTTP status of the answer, or ``None`` when there was no answer.
        errors: The parsed ``errors`` objects, in order; empty when the body had none.
        body: The decoded JSON body, the text body, or ``None``.
        method: HTTP method of the failed request.
        url: URL of the failed request, without its query string, which may hold search terms.
        auth_code: The ``X-Lognex-Auth`` header, "extended authentication error code".
        auth_message: The ``X-Lognex-Auth-Message`` header.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        errors: list[ApiErrorDetail] | None = None,
        body: Any = None,
        method: str | None = None,
        url: str | None = None,
        auth_code: str | None = None,
        auth_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.errors: list[ApiErrorDetail] = list(errors or [])
        self.body = body
        self.method = method
        self.url = url
        self.auth_code = auth_code
        self.auth_message = auth_message

    @property
    def codes(self) -> list[int]:
        """The MoySklad error codes of :attr:`errors`, in order, without the empty ones."""
        return [e.code for e in self.errors if e.code is not None]


class RedirectError(MoySkladError):
    """HTTP 301, 302, 303 or 308. The client never follows redirects.

    Attributes:
        location: The ``Location`` header, or ``None``.
    """

    def __init__(self, message: str, *, location: str | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.location = location


class ValidationError(MoySkladError):
    """HTTP 400 (bad request JSON) or 412 (a required parameter or field is missing)."""


class AuthError(MoySkladError):
    """HTTP 401: wrong credentials, or the user or the account is blocked."""


class ForbiddenError(MoySkladError):
    """HTTP 403: no permission for this object."""


class NotFoundError(MoySkladError):
    """HTTP 404: the resource does not exist."""


class ConflictError(MoySkladError):
    """HTTP 409: the object is in use and cannot be deleted."""


class ApiVersionGoneError(MoySkladError):
    """HTTP 410: this API version is no longer supported."""


class PayloadTooLargeError(MoySkladError):
    """HTTP 413: the request or one of its arrays is too large (for example over 1000 rows)."""


class UnsupportedMediaTypeError(MoySkladError):
    """HTTP 415: unsupported headers or body; also the answer to a missing gzip encoding."""


class RateLimitError(MoySkladError):
    """HTTP 429 that was not retried, or still there after the retries ran out.

    Attributes:
        retry_after: Seconds the client was told, or chose, to wait; ``None`` if unknown.
        parallel: ``True`` when the error is 1073, too many requests at the same time, rather
            than too many requests in the 3-second period.
    """

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
        self.parallel = PARALLEL_LIMIT_CODE in self.codes


class ServerError(MoySkladError):
    """HTTP 5xx that was not retried, or still there after the retries ran out."""


class NetworkError(MoySkladError):
    """The request got no HTTP answer: a connection failure, a timeout or a broken stream."""


class GuardTrippedError(MoySkladError):
    """The client refused to send a request that could get API access cut off.

    Raised by :class:`~aiomoysklad.ErrorGuard` before a request goes out; nothing was sent.

    Attributes:
        counter: ``"identical_errors"``, ``"too_many_requests"`` or ``"puts_per_entity"``.
        limit: The stop that was reached, per rolling minute.
        retry_after: Seconds until the oldest counted event leaves the minute.
    """

    def __init__(
        self, message: str, *, counter: str, limit: int, retry_after: float, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.counter = counter
        self.limit = limit
        self.retry_after = retry_after


_BY_STATUS: dict[int, type[MoySkladError]] = {
    301: RedirectError,
    302: RedirectError,
    303: RedirectError,
    308: RedirectError,
    400: ValidationError,
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    410: ApiVersionGoneError,
    412: ValidationError,
    413: PayloadTooLargeError,
    415: UnsupportedMediaTypeError,
    429: RateLimitError,
}

_SERVER_ERRORS = 500


def _detail(raw: Mapping[str, Any]) -> ApiErrorDetail:
    try:
        return ApiErrorDetail.model_validate(raw)
    except pydantic.ValidationError as exc:
        bad = {err["loc"][0] for err in exc.errors() if err["loc"]}
    cleaned = {key: value for key, value in raw.items() if key not in bad}
    try:
        return ApiErrorDetail.model_validate(cleaned)
    except pydantic.ValidationError:
        text = raw.get("error")
        return ApiErrorDetail(error=text if isinstance(text, str) else "")


def parse_errors(body: Any) -> list[ApiErrorDetail]:
    """Read the ``errors`` objects of an error body.

    Args:
        body: The decoded body: ``{"errors": [...]}``, a list of such objects (bulk requests),
            or anything else.

    Returns:
        The details in order; an empty list when the body holds none.
    """
    holders: list[Any] = body if isinstance(body, list) else [body]
    details: list[ApiErrorDetail] = []
    for holder in holders:
        if not isinstance(holder, Mapping):
            continue
        errors = holder.get("errors")
        if not isinstance(errors, list):
            continue
        details.extend(_detail(raw) for raw in errors if isinstance(raw, Mapping))
    return details


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "..."


def _without_query(url: httpx.URL) -> httpx.URL:
    return url.copy_with(query=None, fragment=None)


def _describe(details: list[ApiErrorDetail], body: Any) -> str:
    if details:
        first = details[0]
        text = f": {first.error}" if first.error else ""
        extras = []
        if first.code is not None:
            extras.append(f"code {first.code}")
        if first.parameter:
            extras.append(f"parameter {first.parameter}")
        if extras:
            text += f" ({', '.join(extras)})"
        if len(details) > 1:
            text += f" (and {len(details) - 1} more)"
        return text
    if isinstance(body, str) and body.strip():
        return f": {_short(body)}"
    return ""


def error_from_response(
    response: httpx.Response, *, retry_after: float | None = None
) -> MoySkladError:
    """Build the exception for an answer that is not a success.

    Args:
        response: An answer with a 3xx, 4xx or 5xx status; its request must be set.
        retry_after: For a 429, the seconds the client was told, or chose, to wait.

    Returns:
        The exception to raise, its class chosen by status from the table in
        ``_general.md § Обработка ошибок``.
    """
    request = response.request
    status = response.status_code
    body = _decode(response)
    details = parse_errors(body)
    auth_code = response.headers.get("X-Lognex-Auth")
    auth_message = response.headers.get("X-Lognex-Auth-Message")
    url = _without_query(request.url)

    message = f"HTTP {status}{_describe(details, body)}"
    if auth_message:
        message += f" [{auth_message}]"
    message += f" ({request.method} {url.path})"

    kind = _BY_STATUS.get(status)
    if kind is None:
        kind = ServerError if status >= _SERVER_ERRORS else MoySkladError
    common: dict[str, Any] = {
        "status": status,
        "errors": details,
        "body": body,
        "method": request.method,
        "url": str(url),
        "auth_code": auth_code,
        "auth_message": auth_message,
    }
    if kind is RedirectError:
        location = response.headers.get("Location")
        if location:
            try:
                target = _without_query(request.url.join(location))
            except (httpx.InvalidURL, ValueError):
                message += ": redirected"
            else:
                message += f": redirected to {target}"
        return RedirectError(message, location=location, **common)
    if kind is UnsupportedMediaTypeError and body is None:
        # _general.md § Сжатие содержимого ответов: a request without Accept-Encoding: gzip,
        # or with another encoding, is answered 415 with no body.
        message += ": check that Accept-Encoding is exactly gzip"
    if kind is RateLimitError:
        return RateLimitError(message, retry_after=retry_after, **common)
    return kind(message, **common)
