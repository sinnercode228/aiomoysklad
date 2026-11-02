"""The HTTP core: addresses, headers, the limits, retries and error mapping."""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import random
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

from ._assortment import Assortment
from ._auth import Auth, BasicAuth
from ._errors import MoySkladError, NetworkError, error_from_response, parse_errors
from ._filters import Filter
from ._guard import ErrorGuard
from ._orders import CustomerOrders
from ._pagination import MAX_LIMIT, Paginator, list_params
from ._ratelimit import ConcurrencyLimit, RateLimiter
from ._retry import RateHeaders, RetryPolicy
from ._stock import StockReports
from ._time import moscow_today
from ._version import __version__
from ._webhooks import Webhooks, WebhookStocks
from ._weights import request_weight

__all__ = ["API_URL", "MAX_HEADER_BYTES", "MoySklad", "encode_query", "issue_user_token"]

logger = logging.getLogger("aiomoysklad")

# openapi: src/openapi.yaml, servers.
API_URL = "https://api.moysklad.ru/api/remap/1.2"

# _restrictions.md § Лимиты и ограничения в JSON API: no more than 8 KB in the request headers
# (URL, User-Agent, Authorization and so on).
MAX_HEADER_BYTES = 8 * 1024

# _general.md § Сжатие содержимого ответов: only gzip is supported; a request without this
# header, or with another encoding, is answered 415 with no body. httpx sends
# "gzip, deflate" by default, so the header is always set explicitly.
_ACCEPT_ENCODING = "gzip"
# _errors.md, 1062: the only supported Accept value.
_ACCEPT = "application/json;charset=utf-8"
# openapi: src/components/headers.yaml, ContentTypeJson; _errors.md, 1037.
_CONTENT_TYPE = "application/json"
# dictionaries/_webhook.md § Заголовок временного отключения X-Lognex-WebHook-DisableByPrefix
# через API. It replaces X-Lognex-WebHook-Disable, which stopped working on 2026-10-01
# (notifications/deprecate-header-x-lognex-webhook-disable.md).
_DISABLE_BY_PREFIX = "X-Lognex-WebHook-DisableByPrefix"
# _general.md § Обработка ошибок: the date the requested API version will be turned off.
_DEPRECATED = "X-Lognex-API-Version-Deprecated"

_NO_CONTENT = 204
_SUCCESS_FIRST = 200
_SUCCESS_END = 300
_TOO_MANY_REQUESTS = 429
_PARALLEL_LIMIT_CODE = 1073  # _errors.md, 1073

# Failures that happen before any byte of the request is sent: safe to retry for any method.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Indirection so tests can make the retry waits instant.
_sleep = asyncio.sleep

ParamValue = str | int | float | bool
Params = Mapping[str, ParamValue | Sequence[ParamValue] | None]


def _param_text(value: ParamValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode_query(params: Params) -> str:
    """Encode query parameters with every reserved character percent-encoded.

    The docs require ``filter`` and ``order`` to be urlencoded and show them unencoded only
    "for readability" (``_general.md § Фильтрация выборки с помощью параметра filter``); error
    1058 is a query that could not be decoded (``_errors.md``). Spaces become ``%20``, never
    ``+``. ``None`` values are left out and a sequence repeats the parameter.
    """
    parts: list[str] = []
    for name, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, Sequence) and not isinstance(value, str) else [value]
        parts.extend(
            f"{quote(name, safe='')}={quote(_param_text(item), safe='')}" for item in values
        )
    return "&".join(parts)


def _header_bytes(request: httpx.Request) -> int:
    line = len(request.method) + len(request.url.raw_path) + len("  HTTP/1.1\r\n")
    return line + sum(len(name) + len(value) + 4 for name, value in request.headers.raw)


def _check_prefix(prefix: str) -> str:
    # The prefix must hold the scheme and the whole domain name: "https://" and
    # "https://some_url" are refused by MoySklad, "https://some_url.com" and
    # "https://some_url.com/some_path" are valid (dictionaries/_webhook.md, same section).
    try:
        url = httpx.URL(prefix)
    except httpx.InvalidURL:
        url = None
    if url is None or url.scheme not in ("http", "https") or "." not in (url.host or ""):
        msg = f"a webhook prefix needs a scheme and a full domain name: {prefix!r}"
        raise ValueError(msg)
    return prefix


class MoySklad:
    """Async client for the MoySklad JSON API 1.2 of one user or solution.

    Use it as an async context manager, or call :meth:`aclose` when done::

        async with MoySklad(BearerToken(token, kind=AuthKind.SOLUTION)) as ms:
            product = await ms.get("/entity/product/7944ef04-f831-11e5-7a69-971500188b19")

    Each request goes through, in order: the :class:`~aiomoysklad.ErrorGuard`, the
    :class:`~aiomoysklad.ConcurrencyLimit`, the :class:`~aiomoysklad.RateLimiter`, which
    takes the request's weight, and the guard once more right before it is sent.

    Args:
        auth: :class:`~aiomoysklad.BasicAuth` or :class:`~aiomoysklad.BearerToken`; its kind
            sets the weight of each request.
        base_url: The API root, for a proxy or a test server.
        limiter: A rate limiter to share between the clients of one user or solution; by
            default each client has its own 45-unit basket.
        concurrency: A cap on requests in flight to share; by default each client has its own
            cap of 5.
        guard: A cut-off guard to share; by default each client has its own.
        retry: Retry settings; defaults to :class:`~aiomoysklad.RetryPolicy` ``()``.
        weights: Weights by path that override the documented schedule, such as
            ``{"/report/stock/all/current": 1}``.
        webhook_disable_prefixes: Webhook URL prefixes that this client's requests must not
            trigger, sent as one ``X-Lognex-WebHook-DisableByPrefix`` header each. An
            integration that writes back sets its own receiver here.
        timeout: Timeout in seconds for the client's own ``httpx.AsyncClient``.
        http: An ``httpx.AsyncClient`` to use; the client then leaves closing it to you. Its
            ``Accept-Encoding`` and auth are overridden on every request.
        today: Returns the date in Moscow that picks the weight from the schedule.

    Attributes:
        stock: The short stock reports, :class:`~aiomoysklad.StockReports`.
        assortment: The assortment without stock, :class:`~aiomoysklad.Assortment`.
        customer_orders: Customer orders, :class:`~aiomoysklad.CustomerOrders`.
        webhooks: Webhooks on entity changes and the health check,
            :class:`~aiomoysklad.Webhooks`.
        webhook_stocks: Webhooks on stock changes, :class:`~aiomoysklad.WebhookStocks`.
    """

    def __init__(
        self,
        auth: Auth,
        *,
        base_url: str = API_URL,
        limiter: RateLimiter | None = None,
        concurrency: ConcurrencyLimit | None = None,
        guard: ErrorGuard | None = None,
        retry: RetryPolicy | None = None,
        weights: Mapping[str, int] | None = None,
        webhook_disable_prefixes: Sequence[str] = (),
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
        today: Callable[[], date] = moscow_today,
    ) -> None:
        base = httpx.URL(base_url.strip().rstrip("/"))
        if base.scheme not in ("http", "https") or not base.host:
            msg = f"base_url must be an absolute http(s) URL: {base_url!r}"
            raise ValueError(msg)
        if base.query or base.fragment:
            msg = "base_url must not have a query string or a fragment"
            raise ValueError(msg)
        self.base_url = str(base)
        self._base = base
        self.auth = auth
        self.limiter = limiter if limiter is not None else RateLimiter()
        self.concurrency = concurrency if concurrency is not None else ConcurrencyLimit()
        self.guard = guard if guard is not None else ErrorGuard()
        self.retry = retry if retry is not None else RetryPolicy()
        self.weights = dict(weights or {})
        self.webhook_disable_prefixes = tuple(map(_check_prefix, webhook_disable_prefixes))
        self._owns_http = http is None
        self.http = http if http is not None else httpx.AsyncClient(timeout=timeout)
        self._today = today
        self._random = random.Random()  # noqa: S311 - jitter, not cryptography
        self._warned_deprecated = False
        self.stock = StockReports(self)
        self.assortment = Assortment(self)
        self.customer_orders = CustomerOrders(self)
        self.webhooks = Webhooks(self)
        self.webhook_stocks = WebhookStocks(self)

    def __repr__(self) -> str:
        return f"MoySklad(base_url={self.base_url!r}, kind={self.auth.kind.name})"

    async def __aenter__(self) -> MoySklad:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client if this client created it."""
        if self._owns_http and not self.http.is_closed:
            await self.http.aclose()

    def owns_url(self, url: str | httpx.URL) -> bool:
        """Tell whether ``url`` is an absolute URL under this client's API root.

        Hrefs from answers and webhook payloads (``meta.href``, ``nextHref``, ``reportUrl``)
        are sent only when they pass, so a forged one cannot make the client send its
        credentials to another host.
        """
        try:
            parsed = url if isinstance(url, httpx.URL) else httpx.URL(url)
        except httpx.InvalidURL:
            return False
        base = self._base
        return (
            parsed.scheme == base.scheme
            and parsed.host == base.host
            and parsed.port == base.port
            and (parsed.path == base.path or parsed.path.startswith(base.path + "/"))
        )

    def api_path(self, url: httpx.URL) -> str:
        """Return the path of ``url`` under the API root, such as ``/entity/product``."""
        return "/" + url.path[len(self._base.path) :].strip("/")

    def url(self, path_or_href: str, params: Params | None = None) -> httpx.URL:
        """Build the URL of a request.

        Args:
            path_or_href: A path under the API root (``/entity/product`` or
                ``entity/product``), or an absolute href under it, sent as given.
            params: Query parameters, appended to any query the href already has.

        Raises:
            ValueError: ``path_or_href`` is an absolute URL outside the API root.
        """
        if "://" in path_or_href:
            target = httpx.URL(path_or_href)
            if not self.owns_url(target):
                msg = f"refusing to send credentials outside {self.base_url}"
                raise ValueError(msg)
        else:
            target = httpx.URL(f"{self.base_url}/{path_or_href.lstrip('/')}")
        extra = encode_query(params) if params else ""
        if extra:
            query = target.query.decode("ascii")
            joined = f"{query}&{extra}" if query else extra
            target = target.copy_with(query=joined.encode("ascii"))
        return target

    def iter_rows(
        self,
        path: str,
        *,
        filter: Filter | str | None = None,
        order: str | None = None,
        search: str | None = None,
        expand: str | Sequence[str] | None = None,
        limit: int = MAX_LIMIT,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Paginator:
        """Iterate over every row of a collection, following ``meta.nextHref``.

        ::

            async for order in ms.iter_rows("/entity/customerorder", order="updated,asc"):
                ...

        Args:
            path: The collection, such as ``/entity/customerorder``.
            filter: A :class:`~aiomoysklad.Filter` or a ready ``filter`` string.
            order: The ``order`` parameter, such as ``"updated,asc;id,asc"``.
            search: The ``search`` parameter.
            expand: Fields to expand, as one comma-separated string or a list.
            limit: Rows per page, 1 to 1000; at most 100 with ``expand``.
            params: Other query parameters of the first request.
            headers: Extra headers sent with every page, such as a beta feature tag.

        Raises:
            ValueError: ``limit`` is out of range, ``expand`` comes with a limit over 100 (the
                server would silently drop it), or ``expand`` nests deeper than 3 levels.
        """
        query = list_params(
            filter=filter, order=order, search=search, expand=expand, limit=limit, params=params
        )
        return Paginator(self, path, query, headers=headers)

    async def get(self, href: str, *, params: Params | None = None) -> Any:
        """Send a ``GET`` to a path or href and return the decoded answer."""
        return await self.request("GET", href, params=params)

    async def request(
        self,
        method: str,
        path_or_href: str,
        *,
        params: Params | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        weight: int | None = None,
    ) -> Any:
        """Send one API request and decode the answer.

        Args:
            method: The HTTP method.
            path_or_href: A path under the API root, or an absolute href under it.
            params: Query parameters, percent-encoded by :func:`encode_query`.
            json: The JSON body.
            headers: Extra headers; ``Accept-Encoding`` and ``Authorization`` are refused.
            weight: Units of the 3-second basket this request costs; by default the
                documented weight for the client's auth kind and the path.

        Returns:
            The decoded JSON, or ``None`` for an empty answer.

        Raises:
            ValueError: The URL is outside the API root, the request line and headers exceed
                8 KB, or ``headers`` tries to set ``Accept-Encoding`` or ``Authorization``.
            GuardTrippedError: Sending could get API access cut off; nothing was sent.
            MoySkladError: A subclass matching the failure; see :mod:`aiomoysklad`.
        """
        method = method.upper()
        target = self.url(path_or_href, params)
        path = self.api_path(target)
        if weight is None:
            weight = request_weight(self.auth.kind, method, path, self._today(), self.weights)
        if not 1 <= weight <= self.limiter.capacity:
            msg = f"weight must be between 1 and {self.limiter.capacity}, got {weight}"
            raise ValueError(msg)
        content = None if json is None else jsonlib.dumps(json, ensure_ascii=False).encode()
        request = self._build(method, target, content, headers)
        response = await self._send(
            request, path, weight, may_resend=self.retry.may_resend(method, json)
        )
        return self._decode(response)

    def _build(
        self,
        method: str,
        target: httpx.URL,
        content: bytes | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Request:
        merged = list((headers or {}).items())
        names = {name.lower() for name, _ in merged}
        if names & {"accept-encoding", "authorization"}:
            msg = "Accept-Encoding and Authorization are set by the client"
            raise ValueError(msg)
        defaults = {
            "Accept": _ACCEPT,
            "User-Agent": f"aiomoysklad/{__version__}",
        }
        if content is not None:
            defaults["Content-Type"] = _CONTENT_TYPE
        merged.extend(
            (name, value) for name, value in defaults.items() if name.lower() not in names
        )
        merged.extend((_DISABLE_BY_PREFIX, prefix) for prefix in self.webhook_disable_prefixes)
        request = self.http.build_request(method, target, content=content, headers=merged)
        # A client passed as http= may carry its own defaults; these two must be exact.
        request.headers["Accept-Encoding"] = _ACCEPT_ENCODING
        request.headers["Authorization"] = self.auth.authorization
        size = _header_bytes(request)
        if size > MAX_HEADER_BYTES:
            msg = (
                f"{method} {target.path}: the request line and headers take {size} bytes, over "
                f"the {MAX_HEADER_BYTES}-byte limit; send fewer ids or a shorter filter"
            )
            raise ValueError(msg)
        return request

    async def _send(
        self, request: httpx.Request, path: str, weight: int, *, may_resend: bool
    ) -> httpx.Response:
        method = request.method
        policy = self.retry
        retries = 0
        url, entity, body = str(request.url), request.url.path, request.content or None
        while True:
            # A refusal here spends nothing.
            self.guard.check(method, url, entity=entity, body=body)
            try:
                async with self.concurrency:
                    await self.limiter.acquire(weight)
                    # Checked again, and counted, when the request leaves: requests queued on
                    # the cap or behind a limiter pause passed the first check before the ones
                    # ahead of them were answered.
                    key = self.guard.before_send(method, url, entity=entity, body=body)
                    try:
                        response = await self.http.send(request, auth=None, follow_redirects=False)
                    except BaseException:
                        self.guard.record(key, None)
                        raise
            except httpx.TransportError as exc:
                retryable = may_resend or isinstance(exc, _NOT_SENT)
                if retryable and retries < policy.max_retries:
                    retries += 1
                    delay = policy.backoff(retries, self._random)
                    logger.warning(
                        "%s %s failed with %s; retry %d of %d in %.2f s",
                        method,
                        path,
                        type(exc).__name__,
                        retries,
                        policy.max_retries,
                        delay,
                    )
                    await _sleep(delay)
                    continue
                msg = f"{method} {path} failed: {type(exc).__name__}: {exc}"
                where = str(request.url.copy_with(query=None))
                raise NetworkError(msg, method=method, url=where) from exc

            status = response.status_code
            self.guard.record(key, status)
            rate = RateHeaders.from_headers(response.headers)
            self.limiter.observe(rate)
            self._note(response, path, weight, rate)
            if _SUCCESS_FIRST <= status < _SUCCESS_END:
                return response

            wait: float | None = None
            if status in policy.retry_statuses:
                wait = self._retry_delay(response, rate, retries + 1)
            if (
                wait is not None
                and may_resend
                and retries < policy.max_retries
                and wait <= policy.max_wait
            ):
                retries += 1
                logger.warning(
                    "%s %s -> %d; retry %d of %d in %.2f s",
                    method,
                    path,
                    status,
                    retries,
                    policy.max_retries,
                    wait,
                )
                await _sleep(wait)
                continue
            raise error_from_response(response, retry_after=wait)

    def _retry_delay(self, response: httpx.Response, rate: RateHeaders, retry: int) -> float:
        if response.status_code != _TOO_MANY_REQUESTS:
            return self.retry.delay(rate, retry, self._random)
        try:
            details = parse_errors(response.json())
        except ValueError:
            details = []
        if any(detail.code == _PARALLEL_LIMIT_CODE for detail in details):
            # Error 1073 is about requests in flight, which the cap handles. The rate headers
            # do not reflect that limit (_general.md § Обработка ошибок), so back off instead
            # of trusting them, and leave the limiter alone.
            return self.retry.backoff(retry, self._random)
        # A 429 for the 3-second basket holds every request of this limiter, not only this one.
        delay = self.retry.delay(rate, retry, self._random)
        self.limiter.pause(delay)
        return delay

    def _note(self, response: httpx.Response, path: str, weight: int, rate: RateHeaders) -> None:
        # The drop in X-RateLimit-Remaining between two answers shows the real weight of an
        # endpoint on a live account (open questions 1 and 2 of docs/design.md).
        logger.debug(
            "%s %s -> %d (weight %d, remaining %s of %s)",
            response.request.method,
            path,
            response.status_code,
            weight,
            rate.remaining,
            rate.limit,
        )
        deprecated = response.headers.get(_DEPRECATED)
        if deprecated and not self._warned_deprecated:
            self._warned_deprecated = True
            logger.warning("MoySklad says this API version will be turned off on %s", deprecated)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.status_code == _NO_CONTENT or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            method = response.request.method
            msg = f"{method} {response.request.url.path} answered with a body that is not JSON"
            raise MoySkladError(
                msg,
                status=response.status_code,
                body=response.text,
                method=method,
                url=str(response.request.url.copy_with(query=None)),
            ) from None


async def issue_user_token(
    login: str,
    password: str,
    *,
    base_url: str = API_URL,
    http: httpx.AsyncClient | None = None,
    timeout: float = 30.0,
) -> str:
    """Get a new access token for a user with ``POST /security/token``.

    Issuing a token revokes the tokens the user had before (``_general.md § Получение нового
    токена``), so every other integration running with this user's old token stops working.
    The request is not resent after a 429 or a timeout for the same reason.

    Args:
        login: The user's login, such as ``admin@example``.
        password: The user's password.
        base_url: The API root.
        http: An ``httpx.AsyncClient`` to use; it is left open.
        timeout: Timeout in seconds when no ``http`` is given.

    Returns:
        The ``access_token``, for :class:`~aiomoysklad.BearerToken` with the default
        :attr:`~aiomoysklad.AuthKind.USER` kind.

    Raises:
        AuthError: The login or password was refused.
        MoySkladError: Any other failure, or an answer without ``access_token``.
    """
    async with MoySklad(
        BasicAuth(login, password), base_url=base_url, http=http, timeout=timeout
    ) as ms:
        body = await ms.request("POST", "/security/token")
    token = body.get("access_token") if isinstance(body, Mapping) else None
    if not isinstance(token, str) or not token:
        msg = "POST /security/token answered without an access_token"
        raise MoySkladError(msg, body=body, method="POST")
    return token
