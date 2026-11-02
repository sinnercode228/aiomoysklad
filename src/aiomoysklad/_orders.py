"""Customer orders: read since a cursor, create, change state.

Paths from ``openapi: src/openapi.yaml`` and ``documents/_customerOrder.md``:
``GET``/``POST /entity/customerorder``, ``GET``/``PUT /entity/customerorder/{id}`` and
``GET /entity/customerorder/metadata``.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from ._errors import MoySkladError
from ._filters import Filter, split_conditions
from ._models import State
from ._pagination import MAX_LIMIT, list_params
from ._time import MSK, format_datetime, parse_datetime

if TYPE_CHECKING:
    from ._client import MoySklad

__all__ = [
    "MAX_POSITIONS",
    "PATH",
    "CustomerOrders",
    "OrderCursor",
    "OrderStream",
    "check_id",
]

logger = logging.getLogger("aiomoysklad")

PATH = "/entity/customerorder"
# workbook/_workbook_sorting.md § Доступные поля для сортировки: updated and id are sortable.
_ORDER = "updated,asc;id,asc"
_SECOND = timedelta(seconds=1)
_ID = re.compile(r"[0-9A-Za-z-]{1,64}")
# _restrictions.md § Ограничения на размер данных: at most 1000 items in one array of a
# request, or the answer is 413.
MAX_POSITIONS = 1000


def check_id(value: str | uuid.UUID, what: str = "id") -> str:
    """Return an entity id as text, refusing anything that could change the path."""
    text = str(value) if isinstance(value, uuid.UUID) else value
    if not isinstance(text, str) or _ID.fullmatch(text) is None:
        msg = f"not a MoySklad {what}: {value!r}"
        raise ValueError(msg)
    return text


def _whole_second(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK).replace(microsecond=0)


@dataclass(frozen=True)
class OrderCursor:
    """Where reading orders by ``updated`` stopped.

    Attributes:
        updated: The second of the last order read, in Moscow time; a naive datetime is taken
            as Moscow time, and fractions of a second are dropped.
        seen: Ids of the orders read whose ``updated`` falls in that second, so they are not
            read again.
    """

    updated: datetime
    seen: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "updated", _whole_second(self.updated))
        object.__setattr__(self, "seen", frozenset(self.seen))

    def as_dict(self) -> dict[str, Any]:
        """Return the cursor as JSON-ready data, to save between runs."""
        return {"updated": format_datetime(self.updated), "seen": sorted(self.seen)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OrderCursor:
        """Read a cursor saved with :meth:`as_dict`.

        Raises:
            ValueError: ``data`` is not a saved cursor.
        """
        updated = data.get("updated")
        seen = data.get("seen", [])
        if not isinstance(updated, str) or not isinstance(seen, list):
            msg = f"not a saved order cursor: {data!r}"
            raise ValueError(msg)
        if not all(isinstance(item, str) for item in seen):
            msg = f"not a saved order cursor: {data!r}"
            raise ValueError(msg)
        return cls(parse_datetime(updated), frozenset(seen))


def _check_caller_filter(filter: Filter | str | None) -> Filter:
    if filter is None:
        return Filter()
    text = str(filter)
    if any(name == "updated" for name, _, _ in split_conditions(text)):
        msg = "the filter cannot hold updated: the cursor sets it"
        raise ValueError(msg)
    if isinstance(filter, Filter):
        return filter
    return Filter().raw(text) if text else Filter()


class OrderStream:
    """Customer orders in ``updated`` order, from a cursor on.

    Made by :meth:`CustomerOrders.iter_updated_since`.

    Attributes:
        cursor: Where reading stopped; it moves with every order yielded. Save it with
            :meth:`OrderCursor.as_dict` once the orders are handled.
    """

    def __init__(
        self,
        client: MoySklad,
        cursor: OrderCursor,
        *,
        filter: Filter,
        page_size: int,
        expand: str | Sequence[str] | None,
    ) -> None:
        self._client = client
        self._filter = filter
        self._page_size = page_size
        self._expand = expand
        self._second = cursor.updated
        self._seen = set(cursor.seen)
        # Checks the page size and expand now, before any request.
        list_params(limit=page_size, expand=expand)

    def __repr__(self) -> str:
        return f"OrderStream(cursor={self.cursor!r})"

    @property
    def cursor(self) -> OrderCursor:
        return OrderCursor(self._second, frozenset(self._seen))

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._rows()

    async def _page(
        self, lower: datetime, upper: datetime | None, offset: int
    ) -> list[dict[str, Any]]:
        bound = Filter().ge("updated", lower)
        if upper is not None:
            bound = bound.lt("updated", upper)
        params = list_params(
            filter=bound & self._filter,
            order=_ORDER,
            expand=self._expand,
            limit=self._page_size,
            params={"offset": offset} if offset else None,
        )
        body = await self._client.get(PATH, params=params)
        rows = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            msg = f"GET {PATH} did not answer with a collection of rows"
            raise MoySkladError(msg, body=body, method="GET")
        return rows

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple[str, datetime]:
        ident, updated = row.get("id"), row.get("updated")
        if not isinstance(ident, str) or not isinstance(updated, str):
            msg = f"GET {PATH}: an order without id or updated"
            raise MoySkladError(msg, body=row, method="GET")
        try:
            moment = parse_datetime(updated)
        except ValueError:
            msg = f"GET {PATH}: order {ident} has an unreadable updated {updated!r}"
            raise MoySkladError(msg, body=row, method="GET") from None
        return ident, moment.replace(microsecond=0)

    def _take(self, row: dict[str, Any]) -> bool:
        ident, second = self._key(row)
        if second > self._second:
            self._second, self._seen = second, {ident}
            return True
        if second == self._second and ident not in self._seen:
            self._seen.add(ident)
            return True
        return False

    async def _rows(self) -> AsyncIterator[dict[str, Any]]:
        size = self._page_size
        lower = self._second
        while True:
            rows = await self._page(lower, None, 0)
            for row in rows:
                if self._take(row):
                    yield row
            if len(rows) < size:
                return
            first, last = self._key(rows[0])[1], self._key(rows[-1])[1]
            if first != last:
                lower = last
                continue
            # A whole page within one second: asking again from that second would return the
            # same page, so read the rest of the second by offset, then go on after it. The
            # cursor stays on that second until a later order is read, since more orders may
            # still be written in it.
            upper = first + _SECOND
            async for row in self._crowded_second(first, upper):
                yield row
            lower = upper

    async def _crowded_second(
        self, first: datetime, upper: datetime
    ) -> AsyncIterator[dict[str, Any]]:
        # The docs give id only = and != in a filter (documents/_customerOrder.md § Атрибуты
        # сущности), so the second cannot be paged by key. An order that leaves it between two
        # offset pages shifts the later rows back and hides one, so the second is read again
        # from offset 0 until a whole pass brings nothing new.
        size = self._page_size
        offset = size  # the first page of this second was already read
        while True:
            took = False
            while True:
                more = await self._page(first, upper, offset)
                for row in more:
                    if self._take(row):
                        took = True
                        yield row
                if len(more) < size:
                    break
                offset += size
            if not took:
                return
            offset = 0


class CustomerOrders:
    """Customer orders, ``ms.customer_orders``."""

    def __init__(self, client: MoySklad) -> None:
        self._client = client
        self._states: list[State] | None = None

    def __repr__(self) -> str:
        return f"CustomerOrders({self._client!r})"

    async def get(self, order_id: str | uuid.UUID, *, expand: str | None = None) -> Any:
        """Read one order with ``GET /entity/customerorder/{id}``."""
        params = {"expand": expand} if expand else None
        return await self._client.get(f"{PATH}/{check_id(order_id, 'order id')}", params=params)

    @staticmethod
    def _prepare(body: Mapping[str, Any], sync_id: str | uuid.UUID | None) -> dict[str, Any]:
        order = dict(body)
        # documents/_customerOrder.md § Создать Заказ покупателя: both are required.
        for required in ("organization", "agent"):
            if not isinstance(order.get(required), Mapping):
                msg = f"a customer order needs {required} as a meta object"
                raise ValueError(msg)
        positions = order.get("positions")
        if isinstance(positions, Sequence) and len(positions) > MAX_POSITIONS:
            msg = (
                f"a request takes at most {MAX_POSITIONS} positions, got {len(positions)}; "
                "add the rest through the positions resource"
            )
            raise ValueError(msg)
        given = order.get("syncId")
        if given is not None:
            try:
                given = str(uuid.UUID(str(given)))
            except ValueError:
                msg = f"syncId must be a UUID, got {given!r}"
                raise ValueError(msg) from None
        if sync_id is not None:
            wanted = str(uuid.UUID(str(sync_id)))
            if given is not None and given != wanted:
                msg = "the body already has another syncId"
                raise ValueError(msg)
            given = wanted
        order["syncId"] = given if given is not None else str(uuid.uuid4())
        return order

    async def create(
        self, body: Mapping[str, Any], *, sync_id: str | uuid.UUID | None = None
    ) -> Any:
        """Create an order with ``POST /entity/customerorder``.

        The body gets a ``syncId`` (a new UUID unless one is given), so a request that has to
        be sent again after a timeout or a 5xx returns the order created the first time
        instead of a second one (``_general.md § Назначение поля syncId``). The client resends
        it for that reason; keep the ``syncId`` to resend it yourself later.

        Args:
            body: The order; ``organization`` and ``agent`` metas are required. It is not
                changed.
            sync_id: The ``syncId`` to use, such as the id of the order in your shop.

        Returns:
            The created order.

        Raises:
            ValueError: ``organization`` or ``agent`` is missing, more than 1000 positions,
                or a ``syncId`` that is not a UUID or contradicts ``sync_id``.
        """
        order = self._prepare(body, sync_id)
        return await self._client.request("POST", PATH, json=order)

    async def states(self, *, reload: bool = False) -> list[State]:
        """Return the order states from ``GET /entity/customerorder/metadata``.

        They are read once per client and cached; ``reload=True`` reads them again.

        Raises:
            MoySkladError: The metadata has no readable ``states``.
        """
        if self._states is not None and not reload:
            return list(self._states)
        body = await self._client.get(f"{PATH}/metadata")
        raw = body.get("states", []) if isinstance(body, dict) else None
        if not isinstance(raw, list):
            msg = f"GET {PATH}/metadata: no list of states"
            raise MoySkladError(msg, body=body, method="GET")
        try:
            states = [State.model_validate(item) for item in raw]
        except PydanticValidationError:
            msg = f"GET {PATH}/metadata: unreadable states"
            raise MoySkladError(msg, body=body, method="GET") from None
        self._states = states
        return list(states)

    @staticmethod
    def _find(states: list[State], wanted: str) -> State | None:
        by_id = [s for s in states if s.id.lower() == wanted.lower()]
        if by_id:
            return by_id[0]
        by_name = [s for s in states if s.name == wanted]
        if len(by_name) > 1:
            msg = f"{len(by_name)} states are named {wanted!r}; pass the state or its id"
            raise LookupError(msg)
        return by_name[0] if by_name else None

    async def find_state(self, state: str | uuid.UUID) -> State:
        """Return the order state with this name or id.

        The states are cached as in :meth:`states`; a name not found there reloads them once.

        Raises:
            LookupError: No state, or more than one, has that name.
        """
        return await self._resolve(state)

    async def _resolve(self, state: str | uuid.UUID | State) -> State:
        if isinstance(state, State):
            return state
        wanted = str(state)
        found = self._find(await self.states(), wanted)
        if found is None:
            # States change rarely; one reload covers a state added since they were cached.
            found = self._find(await self.states(reload=True), wanted)
        if found is None:
            msg = f"no customer order state named or with id {wanted!r}"
            raise LookupError(msg)
        return found

    async def set_state(self, order_id: str | uuid.UUID, state: str | uuid.UUID | State) -> Any:
        """Move an order to another state with ``PUT /entity/customerorder/{id}``.

        The order's ``state`` field takes the state's meta (``documents/_customerOrder.md
        § Атрибуты сущности``). Each call is a PUT to that order, which counts towards the
        cut-off of 100 PUTs to one entity per minute (``_restrictions.md § Автоматическое
        отключение доступа``).

        Args:
            order_id: The order's id.
            state: The state's name, such as ``"Отгружен"``, its id, or a
                :class:`~aiomoysklad.State` from :meth:`states`.

        Returns:
            The changed order.

        Raises:
            ValueError: ``order_id`` is not an id.
            LookupError: No state, or more than one, has that name.
        """
        path = f"{PATH}/{check_id(order_id, 'order id')}"
        found = await self._resolve(state)
        meta = {
            "href": found.meta.href,
            "type": found.meta.type or "state",
            "mediaType": found.meta.media_type or "application/json",
        }
        return await self._client.request("PUT", path, json={"state": {"meta": meta}})

    def iter_updated_since(
        self,
        since: OrderCursor | datetime,
        *,
        filter: Filter | str | None = None,
        page_size: int = MAX_LIMIT,
        expand: str | Sequence[str] | None = None,
    ) -> OrderStream:
        """Iterate over the orders updated since a moment or a saved cursor, oldest first.

        ::

            orders = ms.customer_orders.iter_updated_since(cursor)
            async for order in orders:
                handle(order)
            save(orders.cursor.as_dict())

        It pages by key, not by offset: each page asks for ``updated>=`` the second of the
        last order read, ordered by ``updated`` and ``id``, and skips the orders of that second
        already read. An order changed while the walk runs moves to the end and comes again
        with its new ``updated``; nothing that stays put is skipped. ``updated`` is filtered to
        the second (``_general.md § Формат даты и времени``). Only when a whole page falls
        within one second is that second read on by ``offset``, and then read again from the
        start until a pass finds nothing new, since an order leaving the second mid-pass
        shifts the offsets; that costs one more pass over the second.

        Orders in the trash presumably do not come. ``_general.md § Фильтрация документов``
        says ``isDeleted=true`` returns the documents in the trash, ``isDeleted=false`` the
        ones not deleted, and both together all of them, but not what comes without the
        filter (``docs/design.md``, open question 8). Pass ``Filter().eq("isDeleted", False)``
        to be sure, and use DELETE webhooks to learn about deleted orders.

        Args:
            since: A moment (naive means Moscow time), or a cursor from an earlier run.
            filter: More conditions, such as ``Filter().eq("applicable", True)``; not on
                ``updated``.
            page_size: Orders per request, 1 to 1000; at most 100 with ``expand``.
            expand: Fields to expand in each order.

        Raises:
            ValueError: The filter holds ``updated``, or the page size or expand is out of
                range.
        """
        cursor = since if isinstance(since, OrderCursor) else OrderCursor(since)
        return OrderStream(
            self._client,
            cursor,
            filter=_check_caller_filter(filter),
            page_size=page_size,
            expand=expand,
        )
