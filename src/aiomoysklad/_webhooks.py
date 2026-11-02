"""Webhook and webhookstock management, and a health check.

Paths and fields from ``dictionaries/_webhook.md`` and ``dictionaries/_webhookstock.md``.
Webhooks work only on paid plans and in the trial: on the free plan nothing is sent and
webhooks cannot be created or changed (``_restrictions.md § Тарифные ограничения``). Managing
them takes an account administrator or a solution with the right permissions.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, get_args

import httpx

from ._models import Webhook, WebhookStock
from ._orders import check_id

if TYPE_CHECKING:
    from ._client import MoySklad

__all__ = [
    "Action",
    "DiffType",
    "ReportType",
    "WebhookHealth",
    "WebhookStocks",
    "Webhooks",
]

logger = logging.getLogger("aiomoysklad")

Action = Literal["CREATE", "UPDATE", "DELETE", "PROCESSED"]
DiffType = Literal["NONE", "FIELDS"]
ReportType = Literal["all", "bystore"]
StockWebhookType = Literal["stock"]

# dictionaries/_webhook.md § Атрибуты сущности: url "up to 255 characters".
MAX_URL_LENGTH = 255
# dictionaries/_webhook.md § Доступные типы сущностей.
_NOT_WATCHABLE = frozenset({"webhook", "discount", "notes"})
# _general.md § Создание и обновление нескольких объектов: at most 1000 objects in one list.
_BATCH = 1000

ModelT = TypeVar("ModelT", Webhook, WebhookStock)


def _check_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        msg = f"a webhook url takes up to {MAX_URL_LENGTH} characters"
        raise ValueError(msg)
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        msg = f"a webhook url must be an absolute http(s) URL: {url!r}"
        raise ValueError(msg)
    return url


def _check_choice(name: str, value: Any, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        msg = f"{name} must be one of {', '.join(allowed)}, got {value!r}"
        raise ValueError(msg)
    return str(value)


def _check_entity_type(entity_type: str) -> str:
    if not isinstance(entity_type, str) or not entity_type.strip():
        msg = "entity_type must name an entity type, such as customerorder"
        raise ValueError(msg)
    if entity_type in _NOT_WATCHABLE:
        msg = f"MoySklad has no webhooks on {entity_type}"
        raise ValueError(msg)
    return entity_type


def _check_webhook_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(fields)
    if "url" in body:
        _check_url(body["url"])
    if "entityType" in body:
        _check_entity_type(body["entityType"])
    if "action" in body:
        _check_choice("action", body["action"], get_args(Action))
    if body.get("diffType") is not None:
        _check_choice("diff_type", body["diffType"], get_args(DiffType))
        # dictionaries/_webhook.md § Атрибуты сущности: diffType is set for UPDATE only.
        if "action" in body and body["action"] != "UPDATE":
            msg = "diff_type applies to UPDATE webhooks only"
            raise ValueError(msg)
    return body


def _check_stock_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(fields)
    if "url" in body:
        _check_url(body["url"])
    if "reportType" in body:
        _check_choice("report_type", body["reportType"], get_args(ReportType))
    if "stockType" in body:
        _check_choice("stock_type", body["stockType"], get_args(StockWebhookType))
    return body


class _Hooks(Generic[ModelT]):
    """What webhooks and webhookstocks share."""

    _path: str
    _type: str
    _model: type[ModelT]

    def __init__(self, client: MoySklad) -> None:
        self._client = client

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._client!r})"

    def _check(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _one(self, body: Any) -> ModelT:
        return self._model.model_validate(body)

    async def list(self) -> list[ModelT]:
        """Return every webhook of this kind on the account."""
        return [self._one(row) async for row in self._client.iter_rows(self._path)]

    async def get(self, webhook_id: str | uuid.UUID) -> ModelT:
        """Read one webhook by id."""
        return self._one(await self._client.get(f"{self._path}/{check_id(webhook_id)}"))

    async def update(self, webhook_id: str | uuid.UUID, changes: Mapping[str, Any]) -> ModelT:
        """Change fields of a webhook with ``PUT``; field names as in the API.

        Raises:
            ValueError: A value the docs do not allow.
        """
        path = f"{self._path}/{check_id(webhook_id)}"
        body = self._check(changes)
        return self._one(await self._client.request("PUT", path, json=body))

    async def enable(self, webhook_id: str | uuid.UUID) -> ModelT:
        """Turn a webhook on, also after MoySklad turned it off."""
        return await self.update(webhook_id, {"enabled": True})

    async def disable(self, webhook_id: str | uuid.UUID) -> ModelT:
        """Turn a webhook off with ``PUT {"enabled": false}``."""
        return await self.update(webhook_id, {"enabled": False})

    async def delete(self, webhook_id: str | uuid.UUID) -> None:
        """Delete a webhook."""
        await self._client.request("DELETE", f"{self._path}/{check_id(webhook_id)}")

    async def delete_many(self, webhooks: Iterable[ModelT | str | uuid.UUID]) -> None:
        """Delete webhooks with ``POST .../delete``, 1000 per request.

        Args:
            webhooks: Webhooks, or their ids.
        """
        metas: list[dict[str, Any]] = []
        for item in webhooks:
            ident = item.id if isinstance(item, (Webhook, WebhookStock)) else item
            href = f"{self._client.base_url}{self._path}/{check_id(ident)}"
            metas.append(
                {"meta": {"href": href, "type": self._type, "mediaType": "application/json"}}
            )
        for start in range(0, len(metas), _BATCH):
            batch = metas[start : start + _BATCH]
            await self._client.request("POST", f"{self._path}/delete", json=batch)

    async def _create(self, body: dict[str, Any]) -> ModelT:
        # A webhook has no syncId, so the client does not send this POST twice: a lost answer
        # may mean it was created.
        return self._one(await self._client.request("POST", self._path, json=self._check(body)))


@dataclass(frozen=True)
class WebhookHealth:
    """What :meth:`Webhooks.health` found.

    The API keeps no delivery log; what it shows is each webhook's ``enabled`` flag. MoySklad
    turns webhooks off when the receiver answered only with errors, or was unreachable, on
    more than seven of the last ten days and handled none
    (``_restrictions.md § Автоматическое отключение доступа``). That this shows as
    ``enabled: false`` is an assumption (``docs/design.md``, open question 5).

    Attributes:
        url_prefix: The prefix the webhooks were selected by.
        webhooks: Entity webhooks whose url starts with the prefix.
        stock_webhooks: Stock webhooks whose url starts with the prefix.
        reenabled: The disabled ones that were turned back on, as MoySklad returned them.
    """

    url_prefix: str
    webhooks: list[Webhook]
    stock_webhooks: list[WebhookStock]
    reenabled: list[Webhook | WebhookStock] = field(default_factory=list)

    @property
    def disabled(self) -> list[Webhook | WebhookStock]:
        """The webhooks of both kinds that were off when checked."""
        hooks: list[Webhook | WebhookStock] = [*self.webhooks, *self.stock_webhooks]
        return [hook for hook in hooks if not hook.enabled]

    @property
    def ok(self) -> bool:
        """``True`` when every selected webhook was on."""
        return not self.disabled


class Webhooks(_Hooks[Webhook]):
    """Webhooks on entity changes, ``ms.webhooks`` (``/entity/webhook``)."""

    _path = "/entity/webhook"
    _type = "webhook"
    _model = Webhook

    def _check(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        return _check_webhook_fields(fields)

    async def create(
        self,
        entity_type: str,
        action: Action,
        url: str,
        *,
        diff_type: DiffType | None = None,
    ) -> Webhook:
        """Create a webhook.

        ``entityType`` + ``action`` + ``url`` must be unique, and one ``entityType`` +
        ``action`` takes at most 5 urls for a user and 1 for a solution (``dictionaries/
        _webhook.md § Создать вебхук``). :meth:`ensure` avoids creating a duplicate.

        Args:
            entity_type: The entity type to watch, such as ``customerorder``; not
                ``webhook``, ``discount`` or ``notes``.
            action: ``CREATE``, ``UPDATE``, ``DELETE``, or ``PROCESSED`` for async tasks.
            url: The receiver, an absolute http(s) URL of up to 255 characters.
            diff_type: ``FIELDS`` to get the changed fields in each event; ``UPDATE`` only.

        Raises:
            ValueError: A value the docs do not allow.
        """
        body: dict[str, Any] = {"entityType": entity_type, "action": action, "url": url}
        if diff_type is not None:
            body["diffType"] = diff_type
        return await self._create(body)

    async def ensure(
        self,
        entity_type: str,
        action: Action,
        url: str,
        *,
        diff_type: DiffType | None = None,
    ) -> Webhook:
        """Make sure an enabled webhook exists, so a deploy script can run it every time.

        A webhook with the same ``entityType``, ``action`` and ``url`` is kept, turned back on
        if it is off, and given ``diff_type`` if that differs; otherwise one is created.
        """
        body = self._check({"entityType": entity_type, "action": action, "url": url})
        if diff_type is not None:
            self._check({"action": action, "diffType": diff_type})
        for hook in await self.list():
            if (hook.entity_type, hook.action, hook.url) != (entity_type, action, url):
                continue
            changes: dict[str, Any] = {}
            if not hook.enabled:
                changes["enabled"] = True
            if diff_type is not None and (hook.diff_type or "NONE") != diff_type:
                changes["diffType"] = diff_type
            return await self.update(hook.id, changes) if changes else hook
        if diff_type is not None:
            body["diffType"] = diff_type
        return await self._create(body)

    async def health(self, url_prefix: str = "", *, reenable: bool = False) -> WebhookHealth:
        """Check the webhooks of both kinds whose url starts with ``url_prefix``.

        Args:
            url_prefix: Selects your webhooks, such as ``https://hooks.example.com/ms/``;
                empty selects all.
            reenable: Turn the disabled ones back on. Fix the receiver first: MoySklad turned
                them off because it kept failing.
        """
        webhooks = [h for h in await self.list() if h.url.startswith(url_prefix)]
        stocks = [
            h for h in await self._client.webhook_stocks.list() if h.url.startswith(url_prefix)
        ]
        health = WebhookHealth(url_prefix, webhooks, stocks)
        for hook in health.disabled:
            logger.warning("webhook %s (%s) is disabled", hook.id, hook.url)
        if reenable:
            for hook in health.disabled:
                if isinstance(hook, Webhook):
                    health.reenabled.append(await self.enable(hook.id))
                else:
                    health.reenabled.append(await self._client.webhook_stocks.enable(hook.id))
        return health


class WebhookStocks(_Hooks[WebhookStock]):
    """Webhooks on stock changes, ``ms.webhook_stocks`` (``/entity/webhookstock``)."""

    _path = "/entity/webhookstock"
    _type = "webhookstock"
    _model = WebhookStock

    def _check(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        return _check_stock_fields(fields)

    async def create(
        self,
        url: str,
        *,
        report_type: ReportType = "all",
        stock_type: StockWebhookType = "stock",
    ) -> WebhookStock:
        """Create a stock webhook; MoySklad then posts every 1 to 5 minutes when stock changed.

        ``stockType`` + ``reportType`` + ``url`` must be unique, with at most 5 urls for a
        user and 1 for a solution per ``stockType`` + ``reportType``
        (``dictionaries/_webhookstock.md § Создать вебхук на изменение остатков``).

        Args:
            url: The receiver, an absolute http(s) URL of up to 255 characters.
            report_type: ``all`` or ``bystore``: the report its ``reportUrl`` points to.
            stock_type: ``stock``, the only value.

        Raises:
            ValueError: A value the docs do not allow.
        """
        return await self._create({"url": url, "reportType": report_type, "stockType": stock_type})

    async def ensure(
        self,
        url: str,
        *,
        report_type: ReportType = "all",
        stock_type: StockWebhookType = "stock",
    ) -> WebhookStock:
        """Make sure an enabled stock webhook exists, as :meth:`Webhooks.ensure` does."""
        body = self._check({"url": url, "reportType": report_type, "stockType": stock_type})
        for hook in await self.list():
            if (hook.url, hook.report_type, hook.stock_type) == (url, report_type, stock_type):
                return hook if hook.enabled else await self.enable(hook.id)
        return await self._create(body)
