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
from collections.abc import Callable, Iterable, Mapping, Sequence
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
    "StockPoller",
    "StockReports",
    "StockRow",
    "StockType",
    "StockUpdate",
    "StoreStockRow",
    "join_stock",
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


@dataclass(frozen=True)
class StockUpdate:
    """What one :meth:`StockPoller.poll` read.

    Attributes:
        rows: The rows of the report.
        full: ``True`` for a full report, which lists every item with stock (zero rows
            included); ``False`` for the items that changed since :attr:`since`, with their
            current totals.
        since: The ``changedSince`` sent, or ``None`` for a full report.
        at: The moment the poll started; the next poll asks for changes since then, minus the
            overlap.
    """

    rows: list[StockRow] | list[StoreStockRow]
    full: bool
    since: datetime | None
    at: datetime


class StockPoller:
    """Keep stock in sync by polling the short report with ``changedSince``.

    It follows the advice of ``reports/_report_stock.md § Параметр changedSince``: overlap the
    intervals a little (the docs' example polls every 30 minutes for the past 35), run a full
    report without ``changedSince`` once a day or more often, and never ask for more than 24
    hours back, falling back to a full report instead. ::

        poller = StockPoller(ms.stock, stock_type="quantity")
        while True:
            update = await poller.poll()
            save(update.rows, replace_all=update.full)
            await asyncio.sleep(600)

    The poller does not sleep itself. Save :attr:`last_poll` and :attr:`last_full` and pass
    them back after a restart to continue with changes.

    Args:
        stock: ``ms.stock`` of a client.
        by_store: Poll ``/report/stock/bystore/current`` instead of ``/all/current``.
        stock_type: The value to poll; see :meth:`StockReports.all_current`.
        overlap: How far each poll reaches back before the previous one; 0 to under 24 hours.
        full_every: How often to read a full report; at most 24 hours.
        assortment_ids: Only these items.
        store_ids: Only these stores.
        last_poll: When the last poll started, from a saved state.
        last_full: When the last full report started, from a saved state.
    """

    def __init__(
        self,
        stock: StockReports,
        *,
        by_store: bool = False,
        stock_type: StockType = "stock",
        overlap: timedelta = timedelta(minutes=5),
        full_every: timedelta = MAX_CHANGED_SINCE,
        assortment_ids: Iterable[str | uuid.UUID] = (),
        store_ids: Iterable[str | uuid.UUID] = (),
        last_poll: datetime | None = None,
        last_full: datetime | None = None,
    ) -> None:
        if not timedelta(0) <= overlap < MAX_CHANGED_SINCE:
            msg = f"overlap must be at least 0 and under 24 hours, got {overlap}"
            raise ValueError(msg)
        if not timedelta(0) < full_every <= MAX_CHANGED_SINCE:
            msg = (
                "full_every must be more than 0 and at most 24 hours: the docs advise a full "
                f"report at least once a day, got {full_every}"
            )
            raise ValueError(msg)
        self._stock = stock
        self.by_store = by_store
        self.stock_type: StockType = stock_type
        self.overlap = overlap
        self.full_every = full_every
        self.assortment_ids = check_ids(assortment_ids, "assortment_ids")
        self.store_ids = check_ids(store_ids, "store_ids")
        self.last_poll = last_poll
        self.last_full = last_full

    def __repr__(self) -> str:
        report = "bystore" if self.by_store else "all"
        return f"StockPoller({report}, {self.stock_type}, last_poll={self.last_poll})"

    def _since(self, at: datetime) -> datetime | None:
        if self.last_poll is None or self.last_full is None:
            return None
        if at - self.last_full >= self.full_every:
            return None
        since = min(self.last_poll - self.overlap, at)
        if at - since > MAX_CHANGED_SINCE:
            return None
        return since

    async def poll(self) -> StockUpdate:
        """Read the changes since the last poll, or a full report when one is due.

        The state moves forward only when the report was read; after an error the next poll
        asks again from the same moment.
        """
        at = self._stock.now()
        since = self._since(at)
        full = since is None
        read = self._stock.bystore_current if self.by_store else self._stock.all_current
        rows = await read(
            self.stock_type,
            # A full report needs the zero rows to show what ran out; with changedSince the
            # zero rows are always included.
            include_zero=full,
            changed_since=since,
            assortment_ids=self.assortment_ids,
            store_ids=self.store_ids,
        )
        self.last_poll = at
        if full:
            self.last_full = at
        return StockUpdate(rows=rows, full=full, since=since, at=at)


def join_stock(
    rows: Iterable[Mapping[str, Any]],
    stock: Iterable[StockRow | StoreStockRow],
    *,
    default: float | None = None,
) -> list[tuple[Mapping[str, Any], float | None]]:
    """Pair assortment rows with their stock, matching ``id`` to ``assortmentId``.

    The new assortment endpoint returns no stock fields; the docs join it with the short
    report this way (``dictionaries/_assortment.md § Поля остатков``). Rows of a report by store
    are summed per item; the sum is ``None`` when any of them is ``None`` (being recalculated).

    Args:
        rows: Assortment rows, or any entities with ``id``.
        stock: Rows of one short report, all of one stock type.
        default: The value for an item missing from the report. A report without zero rows
            leaves out what is at zero, so pass ``0.0`` for such a full report; a report with
            ``changed_since`` leaves out what did not change, so keep ``None`` there.

    Raises:
        ValueError: ``stock`` mixes stock types.
    """
    totals: dict[str, float | None] = {}
    types: set[str] = set()
    for row in stock:
        types.add(row.stock_type)
        key = row.assortment_id.lower()
        if key not in totals:
            totals[key] = row.value
            continue
        before = totals[key]
        totals[key] = None if before is None or row.value is None else before + row.value
    if len(types) > 1:
        msg = f"join one stock type at a time, got {', '.join(sorted(types))}"
        raise ValueError(msg)
    joined: list[tuple[Mapping[str, Any], float | None]] = []
    for entity in rows:
        ident = entity.get("id")
        found = totals.get(ident.lower(), default) if isinstance(ident, str) else default
        joined.append((entity, found))
    return joined
