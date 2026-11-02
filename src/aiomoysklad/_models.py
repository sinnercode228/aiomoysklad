"""The few structures the toolkit itself reads.

Entities stay plain dicts. These models keep unknown fields (``extra="allow"``) because "a new
field may be added without breaking the response structure" (``_general.md § Замечания по
разработке клиентских приложений``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ApiErrorDetail", "Meta", "MoySkladModel", "State"]


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
