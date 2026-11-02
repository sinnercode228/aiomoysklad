"""A framework-agnostic helper for receiving webhooks.

From ``dictionaries/_webhook.md § Пример вебхука``, ``dictionaries/_webhookstock.md`` and
``workbook/_workbook_webhooks.md § Отправка вебхука в клиентское приложение``:

* MoySklad POSTs JSON to the webhook's url with ``?requestId=<id>`` added;
* it expects 200 or 204 within 1500 ms; otherwise it tries 3 more times, one after another,
  with the same ``requestId``, then drops the notification;
* the docs advise to separate receiving from handling.

So :meth:`WebhookReceiver.accept` only parses and dedupes, for the handler to answer at once,
and the fetching of entities is separate. The docs describe no signature: trust the payload
no further than its hrefs, which are fetched only from the API host.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from pydantic import ValidationError as PydanticValidationError

from ._errors import NotFoundError
from ._models import EntityNotification, StockNotification, WebhookEvent
from ._stock import ALL_PATH, BYSTORE_PATH, StockRow, StoreStockRow, parse_stock_rows
from ._time import moscow_today

if TYPE_CHECKING:
    from ._client import MoySklad

logger = logging.getLogger("aiomoysklad")

__all__ = [
    "Dedupe",
    "Delivery",
    "MemoryDedupe",
    "ReceivedEvent",
    "ReceiverStats",
    "WebhookPayloadError",
    "WebhookReceiver",
    "parse_webhook",
]

_KEY_PREFIX = "moysklad:webhook:"


class WebhookPayloadError(ValueError):
    """The body is not a MoySklad webhook, or it points outside the API."""


def parse_webhook(body: bytes | str) -> EntityNotification | StockNotification:
    """Parse the body of a webhook request.

    Returns:
        An :class:`~aiomoysklad.EntityNotification` for a body with ``events``, a
        :class:`~aiomoysklad.StockNotification` for one with ``reportUrl``.

    Raises:
        WebhookPayloadError: The body is not JSON, or neither kind of notification.
    """
    try:
        data = json.loads(body)
    except ValueError:
        msg = "the webhook body is not JSON"
        raise WebhookPayloadError(msg) from None
    if not isinstance(data, dict):
        msg = "the webhook body is not a JSON object"
        raise WebhookPayloadError(msg)
    try:
        if "events" in data:
            return EntityNotification.model_validate(data)
        if "reportUrl" in data:
            return StockNotification.model_validate(data)
    except PydanticValidationError as exc:
        msg = f"the webhook body does not match the documented payload: {exc.error_count()} errors"
        raise WebhookPayloadError(msg) from None
    msg = "the webhook body has neither events nor reportUrl"
    raise WebhookPayloadError(msg)


class Dedupe(Protocol):
    """A store of delivery keys; back it with Redis or a database to share it.

    ``first_seen`` must be atomic: it records ``key`` for ``ttl`` seconds and returns
    ``True`` only to the first caller, as ``SET key 1 NX EX ttl`` does in Redis.
    """

    async def first_seen(self, key: str, ttl: float) -> bool:
        """Record ``key`` and tell whether it was new."""
        ...


class MemoryDedupe:
    """An in-process :class:`Dedupe`, enough for one worker.

    Once ``max_keys`` keys are kept, the oldest one goes even if its ttl has not run out, so
    a repeat of that delivery is taken as new. The first time that happens a warning is
    logged; raise ``max_keys`` or use a shared store then. With the defaults (100 000 keys,
    600 s) it takes about 166 deliveries a second.

    Args:
        max_keys: The most keys kept; the oldest go first.
        clock: A monotonic clock in seconds.
    """

    def __init__(
        self, *, max_keys: int = 100_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if max_keys < 1:
            msg = f"max_keys must be at least 1, got {max_keys}"
            raise ValueError(msg)
        self.max_keys = max_keys
        self._clock = clock
        self._expiry: OrderedDict[str, float] = OrderedDict()
        self._warned_full = False

    def __len__(self) -> int:
        return len(self._expiry)

    def __repr__(self) -> str:
        return f"MemoryDedupe({len(self)} keys)"

    async def first_seen(self, key: str, ttl: float) -> bool:
        """Record ``key`` for ``ttl`` seconds; ``True`` when it was not recorded already."""
        now = self._clock()
        expires = self._expiry.get(key)
        if expires is not None and expires > now:
            return False
        self._expiry.pop(key, None)
        while self._expiry:
            oldest, at = next(iter(self._expiry.items()))
            if at > now and len(self._expiry) < self.max_keys:
                break
            if at > now and not self._warned_full:
                self._warned_full = True
                logger.warning(
                    "MemoryDedupe is full at max_keys=%d: dropping keys before their ttl, so "
                    "repeated deliveries can get through",
                    self.max_keys,
                )
            del self._expiry[oldest]
        self._expiry[key] = now + ttl
        return True


@dataclass(frozen=True)
class Delivery:
    """A webhook request that was not seen before.

    Attributes:
        request_id: The ``requestId`` query parameter, if there was one.
        key: The dedupe key.
        notification: The parsed body.
    """

    request_id: str | None
    key: str
    notification: EntityNotification | StockNotification

    @property
    def events(self) -> list[WebhookEvent]:
        """The events of an entity notification without exact repeats; empty for stock."""
        if not isinstance(self.notification, EntityNotification):
            return []
        unique: dict[tuple[str, str, tuple[str, ...] | None], WebhookEvent] = {}
        for event in self.notification.events:
            fields = None if event.updated_fields is None else tuple(event.updated_fields)
            unique.setdefault((event.meta.href, event.action, fields), event)
        return list(unique.values())


@dataclass(frozen=True)
class ReceivedEvent:
    """One event out of :meth:`WebhookReceiver.receive`.

    Attributes:
        event: The entity event, or the stock notification itself.
        request_id: The ``requestId`` of the delivery.
        entity: With ``fetch=True``, the entity as it is now; ``None`` for ``DELETE`` or when
            it is gone.
        stock: With ``fetch=True``, the rows of a stock notification's report.
    """

    event: WebhookEvent | StockNotification
    request_id: str | None
    entity: Any = None
    stock: list[StockRow] | list[StoreStockRow] | None = None


@dataclass
class ReceiverStats:
    """Counts of what a receiver saw, to alert well before MoySklad turns webhooks off.

    MoySklad turns webhooks off when, on more than seven of the last ten days, the receiver
    answered only with errors or was unreachable, and it handled not one webhook in those ten
    days (``_restrictions.md § Автоматическое отключение доступа``). These counts see only
    the requests that reached this process.

    Attributes:
        accepted: Deliveries accepted.
        duplicates: Repeated deliveries skipped.
        rejected: Bodies that were not webhooks.
        last_accepted: The Moscow date of the last delivery accepted or skipped as a repeat.
    """

    today: Callable[[], date] = field(default=moscow_today, repr=False)
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    last_accepted: date | None = None

    def days_without_success(self, today: date | None = None) -> int | None:
        """Days since the last delivery was answered; ``None`` if none ever was."""
        if self.last_accepted is None:
            return None
        return ((today or self.today()) - self.last_accepted).days


class WebhookReceiver:
    """Parse, dedupe and optionally fetch what MoySklad's webhooks point at.

    It works with any web framework: give it the raw body and the ``requestId`` query
    parameter. ::

        receiver = WebhookReceiver(ms)

        async def endpoint(body: bytes, request_id: str | None) -> int:
            delivery = await receiver.accept(body, request_id)  # parse and dedupe only
            if delivery is not None:
                queue.put_nowait(delivery)  # handle it after answering
            return 204

    Args:
        client: The client that fetches entities and reports; only :meth:`fetch`,
            :meth:`fetch_stock` and ``receive(fetch=True)`` need it.
        dedupe: Where delivery keys are kept; by default an in-process
            :class:`MemoryDedupe`, which lets repeats through once it is full.
        ttl: Seconds a key is kept. MoySklad's retries come right after each other, so a few
            minutes are enough.
        today: Returns the Moscow date, for :attr:`stats`.
    """

    def __init__(
        self,
        client: MoySklad | None = None,
        *,
        dedupe: Dedupe | None = None,
        ttl: float = 600.0,
        today: Callable[[], date] = moscow_today,
    ) -> None:
        if not ttl > 0:
            msg = f"ttl must be positive, got {ttl}"
            raise ValueError(msg)
        self._client = client
        self.dedupe: Dedupe = dedupe if dedupe is not None else MemoryDedupe()
        self.ttl = ttl
        self.stats = ReceiverStats(today=today)

    def __repr__(self) -> str:
        return f"WebhookReceiver({self.stats!r})"

    @staticmethod
    def key(body: bytes | str, request_id: str | None) -> str:
        """The dedupe key: the ``requestId``, or a SHA-256 of the body without one."""
        if request_id:
            return _KEY_PREFIX + request_id
        raw = body.encode() if isinstance(body, str) else body
        return _KEY_PREFIX + "sha256:" + hashlib.sha256(raw).hexdigest()

    async def accept(self, body: bytes | str, request_id: str | None = None) -> Delivery | None:
        """Parse a webhook request and drop a repeat.

        Call it from the endpoint and answer 200 or 204 right after: MoySklad waits 1500 ms.
        The delivery is marked seen here, so a handler that fails afterwards does not get it
        again from MoySklad.

        Args:
            body: The raw request body.
            request_id: The ``requestId`` query parameter.

        Returns:
            The delivery, or ``None`` when it was seen before and the dedupe store still
            holds its key (see :class:`MemoryDedupe` for when it does not).

        Raises:
            WebhookPayloadError: The body is not a webhook; it is not recorded as seen.
        """
        try:
            notification = parse_webhook(body)
        except WebhookPayloadError:
            self.stats.rejected += 1
            raise
        key = self.key(body, request_id)
        self.stats.last_accepted = self.stats.today()
        if not await self.dedupe.first_seen(key, self.ttl):
            self.stats.duplicates += 1
            return None
        self.stats.accepted += 1
        return Delivery(request_id=request_id, key=key, notification=notification)

    def _need_client(self) -> MoySklad:
        if self._client is None:
            msg = "this receiver has no client to fetch with; pass one to WebhookReceiver()"
            raise RuntimeError(msg)
        return self._client

    async def fetch(self, event: WebhookEvent) -> Any:
        """Read the entity an event points at, as it is now.

        Returns:
            The entity, or ``None`` for a ``DELETE`` event or an entity deleted since.

        Raises:
            WebhookPayloadError: The href is outside the API root of the client.
            RuntimeError: The receiver has no client.
        """
        client = self._need_client()
        if event.action == "DELETE":
            return None
        href = event.meta.href
        if not client.owns_url(href):
            msg = f"the event points outside {client.base_url}: {href}"
            raise WebhookPayloadError(msg)
        try:
            return await client.get(href)
        except NotFoundError:
            return None

    async def fetch_stock(
        self, notification: StockNotification
    ) -> list[StockRow] | list[StoreStockRow]:
        """Read the report a stock notification points at; it is charged 5 units.

        The 5 units are documented for the full stock reports; for the short ``/current`` ones
        they are a precaution (``docs/design.md``, open question 1).

        Raises:
            WebhookPayloadError: ``reportUrl`` is not a short stock report of the API root.
            RuntimeError: The receiver has no client.
        """
        client = self._need_client()
        url = notification.report_url
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            parsed = None
        path = client.api_path(parsed) if parsed is not None and client.owns_url(parsed) else None
        if path not in (ALL_PATH, BYSTORE_PATH):
            msg = f"reportUrl is not a short stock report of {client.base_url}: {url}"
            raise WebhookPayloadError(msg)
        body = await client.get(str(parsed))
        return parse_stock_rows(
            body, notification.stock_type, by_store=path == BYSTORE_PATH, path=path
        )

    async def receive(
        self, body: bytes | str, request_id: str | None = None, *, fetch: bool = False
    ) -> list[ReceivedEvent]:
        """Turn a webhook request into its events: raw body in, deduped events out.

        Args:
            body: The raw request body.
            request_id: The ``requestId`` query parameter.
            fetch: Also read each entity once (and a stock notification's report). That takes
                requests, so do it after answering MoySklad, or use :meth:`accept`.

        Returns:
            The events without repeats, or an empty list for a delivery seen before.
        """
        delivery = await self.accept(body, request_id)
        if delivery is None:
            return []
        notification = delivery.notification
        if isinstance(notification, StockNotification):
            rows = await self.fetch_stock(notification) if fetch else None
            return [ReceivedEvent(notification, request_id, stock=rows)]
        received: list[ReceivedEvent] = []
        fetched: dict[str, Any] = {}
        for event in delivery.events:
            entity = None
            if fetch and event.action != "DELETE":
                if event.meta.href not in fetched:
                    fetched[event.meta.href] = await self.fetch(event)
                entity = fetched[event.meta.href]
            received.append(ReceivedEvent(event, request_id, entity=entity))
        return received
