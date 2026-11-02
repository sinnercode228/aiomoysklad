"""The few structures the toolkit itself reads.

Entities stay plain dicts. These models keep unknown fields (``extra="allow"``) because "a new
field may be added without breaking the response structure" (``_general.md § Замечания по
разработке клиентских приложений``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ApiErrorDetail",
    "AuditContext",
    "EntityNotification",
    "Meta",
    "MoySkladModel",
    "State",
    "StockNotification",
    "Webhook",
    "WebhookEvent",
    "WebhookStock",
]


class MoySkladModel(BaseModel):
    """Base of the models: keeps unknown fields and accepts both field names and API names."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, frozen=True)


class Meta(MoySkladModel):
    """A ``meta`` object (``_general.md § Метаданные``, ``§ Мета коллекций``).

    The collection fields ``size``, ``limit``, ``offset``, ``next_href`` and ``previous_href``
    are only set on the ``meta`` of a list.
    """

    href: str
    metadata_href: str | None = Field(default=None, alias="metadataHref")
    type: str | None = None
    media_type: str | None = Field(default=None, alias="mediaType")
    uuid_href: str | None = Field(default=None, alias="uuidHref")
    download_href: str | None = Field(default=None, alias="downloadHref")
    size: int | None = None
    limit: int | None = None
    offset: int | None = None
    next_href: str | None = Field(default=None, alias="nextHref")
    previous_href: str | None = Field(default=None, alias="previousHref")


class ApiErrorDetail(MoySkladModel):
    """One object of an ``errors`` array (``_general.md § СТРУКТУРА ОБЪЕКТА error``).

    Attributes:
        error: The error title; the only field the docs mark as always present.
        parameter: The parameter the error is about.
        code: The MoySklad error code (see ``_errors.md``); when empty, the HTTP status applies.
        error_message: A message attached to the error.
        more_info: Link to the documentation of the error (``moreInfo``).
        line: The line of the request JSON where the error was found.
        column: The position in that line.
        dependencies: Metas of the objects that prevent a deletion.
        meta: Meta of the entity the error is about.
    """

    error: str = ""
    parameter: str | None = None
    code: int | None = None
    error_message: str | None = None
    more_info: str | None = Field(default=None, alias="moreInfo")
    line: int | None = None
    column: int | None = None
    dependencies: list[Meta] | None = None
    meta: Meta | None = None


class State(MoySkladModel):
    """A document state (``dictionaries/_states.md § Статусы``).

    The states of a document type are listed in its metadata, such as
    ``GET /entity/customerorder/metadata``, field ``states``.

    Attributes:
        meta: Meta of the state; its href is under ``.../metadata/states/{id}``.
        id: The state's id.
        name: The state's name.
        state_type: ``Regular``, ``Successful`` (final, positive) or ``Unsuccessful`` (final,
            negative).
        color: The color, as an ARGB integer.
        entity_type: The document type the state belongs to.
    """

    meta: Meta
    id: str
    name: str
    state_type: str | None = Field(default=None, alias="stateType")
    color: int | None = None
    entity_type: str | None = Field(default=None, alias="entityType")


class Webhook(MoySkladModel):
    """A webhook on entity changes (``dictionaries/_webhook.md § Атрибуты сущности``).

    Attributes:
        meta: Meta of the webhook.
        id: The webhook's id.
        account_id: The account's id.
        entity_type: The entity type it watches, such as ``customerorder``.
        action: ``CREATE``, ``UPDATE``, ``DELETE`` or ``PROCESSED`` (async tasks only).
        url: Where MoySklad posts the notifications; up to 255 characters.
        enabled: Whether it is on. MoySklad turns it off after its receiver failed for more
            than seven of the last ten days.
        method: The HTTP method of the notifications, ``POST``.
        diff_type: ``NONE`` or ``FIELDS``, for ``UPDATE`` only; with ``FIELDS`` each event
            lists the changed fields.
        author_application: Meta of the solution that created it, if one did.
    """

    meta: Meta
    id: str
    account_id: str | None = Field(default=None, alias="accountId")
    entity_type: str = Field(alias="entityType")
    action: str
    url: str
    enabled: bool
    method: str | None = None
    diff_type: str | None = Field(default=None, alias="diffType")
    author_application: dict[str, Any] | None = Field(default=None, alias="authorApplication")


class WebhookStock(MoySkladModel):
    """A webhook on stock changes (``dictionaries/_webhookstock.md § Атрибуты сущности``).

    Attributes:
        meta: Meta of the webhook.
        id: The webhook's id.
        account_id: The account's id.
        stock_type: The stock it watches; only ``stock`` exists.
        report_type: The report it points to: ``all`` or ``bystore``.
        url: Where MoySklad posts the notifications; up to 255 characters.
        enabled: Whether it is on.
        author_application: Meta of the solution that created it, if one did.
    """

    meta: Meta
    id: str
    account_id: str | None = Field(default=None, alias="accountId")
    stock_type: str = Field(alias="stockType")
    report_type: str = Field(alias="reportType")
    url: str
    enabled: bool
    author_application: dict[str, Any] | None = Field(default=None, alias="authorApplication")


class WebhookEvent(MoySkladModel):
    """One event of an entity webhook (``dictionaries/_webhook.md § Атрибуты сущности событие``).

    Attributes:
        meta: Meta of the changed entity; fetch its ``href`` for the current state.
        action: ``CREATE``, ``UPDATE``, ``DELETE`` or ``PROCESSED``.
        account_id: The account's id.
        updated_fields: The fields the user changed; only for a webhook with
            ``diffType=FIELDS`` and ``action=UPDATE``.
    """

    meta: Meta
    action: str
    account_id: str | None = Field(default=None, alias="accountId")
    updated_fields: list[str] | None = Field(default=None, alias="updatedFields")


class AuditContext(MoySkladModel):
    """The audit context of an entity webhook (``§ Атрибуты сущности контекст аудита``).

    Attributes:
        meta: Meta of the audit record.
        uid: Login of the employee who made the change.
        moment: When the change was made, ``YYYY-MM-DD HH:MM:SS`` in Moscow time.
    """

    meta: Meta
    uid: str | None = None
    moment: str | None = None


class EntityNotification(MoySkladModel):
    """The body MoySklad posts for an entity webhook.

    From ``dictionaries/_webhook.md § Пример вебхука``.

    Attributes:
        events: The events; one notification may hold several.
        audit_context: The audit context of the events.
    """

    events: list[WebhookEvent]
    audit_context: AuditContext | None = Field(default=None, alias="auditContext")


class StockNotification(MoySkladModel):
    """The body MoySklad posts for a stock webhook.

    From ``dictionaries/_webhookstock.md § Атрибуты отправляемого сообщения``.

    Attributes:
        account_id: The account's id.
        stock_type: ``stock``.
        report_type: ``all`` or ``bystore``.
        report_url: The short report of the items that changed, with ``changedSince``.
    """

    account_id: str | None = Field(default=None, alias="accountId")
    stock_type: str = Field(alias="stockType")
    report_type: str = Field(alias="reportType")
    report_url: str = Field(alias="reportUrl")
