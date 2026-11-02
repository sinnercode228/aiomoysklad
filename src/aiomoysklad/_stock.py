"""The short stock reports.

Everything here comes from ``reports/_report_stock.md § Краткий отчет об остатках`` and its
subsections, and ``openapi: src/paths/reports/stocks/stock-all-current.yaml`` and
``stock-bystore-current.yaml``:

* ``GET /report/stock/all/current`` groups by assortment, ``GET /report/stock/bystore/current``
  by assortment and store. The answer is one JSON array, with no paging and no order.
* Each row holds ``assortmentId`` (``storeId`` too, by store) and one value, named by
  ``stockType``: ``stock`` (the default), ``freeStock``, ``quantity``, ``reserve`` or
  ``inTransit``. One type per request.
* ``include=zeroLines`` adds the zero rows; ``changedSince`` gives the current totals of the
  items that changed since then, zero rows included. It may not be in the future nor more than
  24 hours back.
* ``withRecalculate=true`` (by store only) adds rows being recalculated, with a ``null`` value.
  The row without a store holds reserves that are not tied to a store.
* ``filter`` takes ``assortmentId`` and ``storeId`` with ``=`` only, as ids (not hrefs), several
  separated by commas.
* Only an administrator or an employee with the stock permission may read it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args
from urllib.parse import quote

from ._errors import MoySkladError
from ._time import MSK, format_datetime

if TYPE_CHECKING:
    from ._client import MoySklad

__all__ = [
    "MAX_CHANGED_SINCE",
    "STOCK_TYPES",
    "StockReports",
    "StockRow",
    "StockType",
    "StoreStockRow",
    "parse_stock_rows",
]

StockType = Literal["stock", "freeStock", "quantity", "reserve", "inTransit"]
STOCK_TYPES: tuple[str, ...] = get_args(StockType)

ALL_PATH = "/report/stock/all/current"
BYSTORE_PATH = "/report/stock/bystore/current"

# reports/_report_stock.md § Параметр changedSince: at most 24 hours back, never in the future.
MAX_CHANGED_SINCE = timedelta(hours=24)

# The encoded filter gets this much of the 8 KB that the request line and headers may take
# (_restrictions.md § Лимиты и ограничения в JSON API); the rest is left for the path, the other
# parameters and the headers. The client checks the whole request against the 8 KB as well.
_FILTER_BUDGET = 8 * 1024 - 2048

_ID = re.compile(r"[0-9A-Fa-f-]{1,36}")


@dataclass(frozen=True, slots=True)
class StockRow:
    """One row of ``/report/stock/all/current``.

    Attributes:
        assortment_id: The id of the product, variant or consignment; the ``id`` of its
            assortment row.
        stock_type: Which value this is: ``stock``, ``freeStock``, ``quantity``, ``reserve``
            or ``inTransit``.
        value: The amount; ``None`` only for a row still being recalculated.
    """

    assortment_id: str
    stock_type: str
    value: float | None


@dataclass(frozen=True, slots=True)
class StoreStockRow:
    """One row of ``/report/stock/bystore/current``.

    Attributes:
        assortment_id: The id of the product, variant or consignment.
        store_id: The id of the store; ``None`` for reserves that are not tied to a store.
        stock_type: Which value this is, as in :class:`StockRow`.
        value: The amount; ``None`` for a row still being recalculated
            (``with_recalculate=True``).
    """

    assortment_id: str
    store_id: str | None
    stock_type: str
    value: float | None


def _now() -> datetime:
    return datetime.now(MSK)


def _check_stock_type(stock_type: str) -> str:
    if stock_type not in STOCK_TYPES:
        msg = f"stock_type must be one of {', '.join(STOCK_TYPES)}, got {stock_type!r}"
        raise ValueError(msg)
    return stock_type


def check_ids(ids: Iterable[str | uuid.UUID], what: str) -> list[str]:
    """Return the ids as text, without repeats, refusing anything that is not an id."""
    seen: dict[str, None] = {}
    for value in ids:
        text = str(value) if isinstance(value, uuid.UUID) else value
        if not isinstance(text, str) or _ID.fullmatch(text) is None:
            msg = f"not a MoySklad id in {what}: {value!r}"
            raise ValueError(msg)
        seen.setdefault(text.lower(), None)
    return list(seen)


def _encoded(text: str) -> int:
    return len(quote(text, safe=""))


def _chunks(ids: Sequence[str], name: str, budget: int) -> list[list[str]]:
    """Split ``ids`` so that ``name=id,id,...`` stays within ``budget`` encoded bytes."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = _encoded(f"{name}=")
    start = size
    for value in ids:
        cost = _encoded(value) + (_encoded(",") if current else 0)
        if current and size + cost > budget:
            chunks.append(current)
            current, size = [], start
            cost = _encoded(value)
        current.append(value)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def _filters(assortment_ids: list[str], store_ids: list[str]) -> list[str | None]:
    if not assortment_ids and not store_ids:
        return [None]
    budget = _FILTER_BUDGET
    store_len = _encoded("storeId=" + ",".join(store_ids)) if store_ids else 0
    if store_len <= budget // 2:
        store_chunks = [store_ids] if store_ids else []
        assortment_budget = budget - store_len - _encoded(";")
    else:
        store_chunks = _chunks(store_ids, "storeId", budget // 2)
        assortment_budget = budget // 2 - _encoded(";")
    assortment_chunks = _chunks(assortment_ids, "assortmentId", assortment_budget)
    filters: list[str | None] = []
    for stores in store_chunks or [[]]:
        for assortment in assortment_chunks or [[]]:
            parts = []
            if assortment:
                parts.append("assortmentId=" + ",".join(assortment))
            if stores:
                parts.append("storeId=" + ",".join(stores))
            filters.append(";".join(parts))
    return filters


def _report_error(path: str, detail: str, body: Any) -> MoySkladError:
    return MoySkladError(f"GET {path}: not a stock report, {detail}", body=body, method="GET")


def _value(row: dict[str, Any], stock_type: str, path: str, body: Any) -> tuple[str, float | None]:
    # The row names its value by stock type. The changedSince example of the docs asks for the
    # default type and answers with "quantity", so a row naming another type is read as that.
    key = stock_type if stock_type in row else next((t for t in STOCK_TYPES if t in row), None)
    if key is None:
        raise _report_error(path, f"a row has no {stock_type} value", body)
    value = row[key]
    if value is None:
        return key, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _report_error(path, f"{key} is not a number: {value!r}", body)
    return key, float(value)


def parse_stock_rows(
    body: Any, stock_type: str, *, by_store: bool, path: str = ALL_PATH
) -> list[StockRow] | list[StoreStockRow]:
    """Read the JSON array of a short stock report into typed rows.

    Raises:
        MoySkladError: ``body`` is not an array of report rows.
    """
    if not isinstance(body, list):
        raise _report_error(path, "the answer is not an array", body)
    plain: list[StockRow] = []
    stores: list[StoreStockRow] = []
    for row in body:
        if not isinstance(row, dict) or not isinstance(row.get("assortmentId"), str):
            raise _report_error(path, "a row has no assortmentId", body)
        key, value = _value(row, stock_type, path, body)
        if by_store:
            store = row.get("storeId")
            if store is not None and not isinstance(store, str):
                raise _report_error(path, f"storeId is not text: {store!r}", body)
            stores.append(StoreStockRow(row["assortmentId"], store, key, value))
        else:
            plain.append(StockRow(row["assortmentId"], key, value))
    return stores if by_store else plain


class StockReports:
    """The short stock reports, ``ms.stock``.

    Each request costs 5 units of the 3-second basket, whatever the credentials
    (``_restrictions.md § Лимит запросов за 3-секундный период``; see ``docs/design.md``, open
    question 1, for the ``/current`` paths).

    Attributes:
        now: Returns the current moment, used to check ``changed_since``.
    """

    def __init__(self, client: MoySklad, *, now: Callable[[], datetime] = _now) -> None:
        self._client = client
        self.now = now

    def __repr__(self) -> str:
        return f"StockReports({self._client!r})"

    async def all_current(
        self,
        stock_type: StockType = "stock",
        *,
        include_zero: bool = False,
        changed_since: datetime | None = None,
        assortment_ids: Iterable[str | uuid.UUID] = (),
        store_ids: Iterable[str | uuid.UUID] = (),
    ) -> list[StockRow]:
        """Read ``GET /report/stock/all/current``: one value per product, variant or consignment.

        Args:
            stock_type: The value to read: ``stock`` (physical stock), ``freeStock`` (stock
                minus reserve), ``quantity`` (available), ``reserve`` or ``inTransit``.
            include_zero: Also return zero rows (``include=zeroLines``). Items never used in
                any operation are left out even then.
            changed_since: Only the items whose stock changed since then, with their current
                totals (not deltas), zero rows included. At most 24 hours back; a naive
                datetime is taken as Moscow time.
            assortment_ids: Only these products, variants or consignments, by id.
            store_ids: Only these stores, by id.

        Long id lists are split over several requests so each stays under the 8 KB header
        limit; every request costs 5 units, so for thousands of ids a full report filtered
        locally is cheaper.

        Raises:
            ValueError: An unknown ``stock_type``, an id that is not an id, or a
                ``changed_since`` in the future or more than 24 hours back.
        """
        rows = await self._read(
            ALL_PATH,
            stock_type,
            include_zero=include_zero,
            changed_since=changed_since,
            assortment_ids=assortment_ids,
            store_ids=store_ids,
            with_recalculate=False,
        )
        return [row for row in rows if isinstance(row, StockRow)]

    async def bystore_current(
        self,
        stock_type: StockType = "stock",
        *,
        include_zero: bool = False,
        changed_since: datetime | None = None,
        assortment_ids: Iterable[str | uuid.UUID] = (),
        store_ids: Iterable[str | uuid.UUID] = (),
        with_recalculate: bool = False,
    ) -> list[StoreStockRow]:
        """Read ``GET /report/stock/bystore/current``: one value per item and store.

        The arguments are those of :meth:`all_current`, plus:

        Args:
            stock_type: As in :meth:`all_current`.
            include_zero: As in :meth:`all_current`.
            changed_since: As in :meth:`all_current`.
            assortment_ids: As in :meth:`all_current`.
            store_ids: As in :meth:`all_current`.
            with_recalculate: Also return rows whose stock is being recalculated, with
                ``value`` ``None`` (``withRecalculate=true``).

        Raises:
            ValueError: As in :meth:`all_current`.
        """
        rows = await self._read(
            BYSTORE_PATH,
            stock_type,
            include_zero=include_zero,
            changed_since=changed_since,
            assortment_ids=assortment_ids,
            store_ids=store_ids,
            with_recalculate=with_recalculate,
        )
        return [row for row in rows if isinstance(row, StoreStockRow)]

    def _changed_since(self, value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=MSK)
        now = self.now()
        if value > now:
            msg = f"changed_since cannot be in the future: {value.isoformat()}"
            raise ValueError(msg)
        if now - value > MAX_CHANGED_SINCE:
            msg = (
                f"changed_since cannot be more than 24 hours back: {value.isoformat()}; "
                "read a full report instead"
            )
            raise ValueError(msg)
        return format_datetime(value)

    async def _read(
        self,
        path: str,
        stock_type: str,
        *,
        include_zero: bool,
        changed_since: datetime | None,
        assortment_ids: Iterable[str | uuid.UUID],
        store_ids: Iterable[str | uuid.UUID],
        with_recalculate: bool,
    ) -> list[StockRow] | list[StoreStockRow]:
        _check_stock_type(stock_type)
        params: dict[str, str] = {"stockType": stock_type}
        if include_zero:
            params["include"] = "zeroLines"
        if changed_since is not None:
            params["changedSince"] = self._changed_since(changed_since)
        if with_recalculate:
            params["withRecalculate"] = "true"
        filters = _filters(
            check_ids(assortment_ids, "assortment_ids"), check_ids(store_ids, "store_ids")
        )
        by_store = path == BYSTORE_PATH
        plain: list[StockRow] = []
        stores: list[StoreStockRow] = []
        for text in filters:
            query = dict(params) if text is None else {**params, "filter": text}
            body = await self._client.get(path, params=query)
            rows = parse_stock_rows(body, stock_type, by_store=by_store, path=path)
            for row in rows:
                if isinstance(row, StoreStockRow):
                    stores.append(row)
                else:
                    plain.append(row)
        return stores if by_store else plain
