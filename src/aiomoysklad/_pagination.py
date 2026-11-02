"""Reading a whole collection page by page.

``workbook/_workbook_filter_paging_search_sort.md § Листание``: ``offset`` counts from zero,
``limit`` defaults to 1000 and cannot be more. When a list holds more than one page, its
``meta`` has ``nextHref`` and ``previousHref`` (``_general.md § Мета коллекций``), and
``nextHref`` already carries ``limit`` and ``offset`` (``workbook/_workbook_paging.md
§ Комбинации offset и limit``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from ._errors import MoySkladError

if TYPE_CHECKING:
    from ._client import MoySklad, Params
    from ._filters import Filter

__all__ = ["MAX_EXPAND_DEPTH", "MAX_EXPAND_LIMIT", "MAX_LIMIT", "Paginator", "list_params"]

MAX_LIMIT = 1000
# _general.md § На expand действуют следующие правила: expand only works with a limit of at
# most 100 (with a larger one it is silently ignored), and nests at most 3 levels.
MAX_EXPAND_LIMIT = 100
MAX_EXPAND_DEPTH = 3


def list_params(
    *,
    filter: Filter | str | None = None,
    order: str | None = None,
    search: str | None = None,
    expand: str | Sequence[str] | None = None,
    limit: int = MAX_LIMIT,
    params: Params | None = None,
) -> dict[str, Any]:
    """Check and assemble the query of a list request.

    Raises:
        ValueError: ``limit`` is not 1 to 1000, ``expand`` comes with a limit over 100, or an
            ``expand`` path nests deeper than 3 levels.
    """
    if not 1 <= limit <= MAX_LIMIT:
        msg = f"limit must be between 1 and {MAX_LIMIT}, got {limit}"
        raise ValueError(msg)
    query: dict[str, Any] = dict(params or {})
    query["limit"] = limit
    if filter is not None and str(filter):
        query["filter"] = str(filter)
    if order:
        query["order"] = order
    if search:
        query["search"] = search
    if expand:
        fields = [expand] if isinstance(expand, str) else list(expand)
        if limit > MAX_EXPAND_LIMIT:
            msg = (
                f"expand needs a limit of at most {MAX_EXPAND_LIMIT}; with {limit} MoySklad "
                "ignores it without an error"
            )
            raise ValueError(msg)
        for field in ",".join(fields).split(","):
            if field.count(".") >= MAX_EXPAND_DEPTH:
                msg = f"expand {field!r} is deeper than the maximum nesting of {MAX_EXPAND_DEPTH}"
                raise ValueError(msg)
        query["expand"] = ",".join(fields)
    return query


def _offset(href: str) -> int | None:
    value = httpx.URL(href).params.get("offset")
    if value is None or not (value.isascii() and value.isdigit()):
        return None
    return int(value)


class Paginator:
    """The rows of a collection, fetched page by page as they are iterated.

    ``async for row in paginator`` yields each row; :meth:`pages` yields each page body. The
    first request carries the query; after that the paginator follows ``meta.nextHref`` as
    the server gave it, only when it stays under the API root, and stops when it is missing.
    Extra headers, if any, go with every page. Iterating again starts over from the first page.

    Attributes:
        size: ``meta.size`` of the first page, the number of rows in the whole collection;
            ``None`` until the first page has been read.
    """

    def __init__(
        self,
        client: MoySklad,
        path: str,
        params: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._path = path
        self._params = dict(params)
        self._headers = dict(headers or {})
        self.size: int | None = None

    def __repr__(self) -> str:
        return f"Paginator({self._path!r})"

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._rows()

    async def _rows(self) -> AsyncIterator[dict[str, Any]]:
        async for body in self.pages():
            for row in body["rows"]:
                yield row

    async def pages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield the body of each page, ``meta`` and ``rows`` included."""
        href: str | None = None
        offset = 0
        first = True
        while True:
            if href is None:
                body = await self._client.request(
                    "GET", self._path, params=self._params, headers=self._headers
                )
            else:
                body = await self._client.request("GET", href, headers=self._headers)
            meta = body.get("meta") if isinstance(body, Mapping) else None
            if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
                msg = f"GET {self._path} did not answer with a collection of rows"
                raise MoySkladError(msg, body=body, method="GET")
            meta = meta if isinstance(meta, Mapping) else {}
            if first:
                size = meta.get("size")
                self.size = size if isinstance(size, int) else None
                first = False
            yield body
            next_href = meta.get("nextHref")
            if not isinstance(next_href, str) or not next_href:
                return
            next_offset = _offset(next_href)
            if next_offset is None:
                msg = f"GET {self._path}: nextHref has no offset, so paging could not end"
                raise MoySkladError(msg, method="GET")
            current = meta.get("offset")
            current = current if isinstance(current, int) else offset
            if next_offset <= current:
                msg = (
                    f"GET {self._path}: nextHref does not move forward "
                    f"(offset {current} to {next_offset})"
                )
                raise MoySkladError(msg, method="GET")
            href, offset = next_href, next_offset
