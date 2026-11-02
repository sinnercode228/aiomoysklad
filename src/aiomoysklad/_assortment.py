"""The assortment endpoint without stock.

``GET /entity/assortment`` with the header ``X-Lognex-Remap-Beta-Feature:
assortmentWithoutStock`` (``dictionaries/_assortment.md § Ассортимент``). Without the header the
request goes to the legacy endpoint, which is to be removed gradually in 2027
(``notifications/new-assortment-endpoint.md``). It is a beta: its contract may still change, and
an unknown tag is ignored without an error (``_general.md § Заголовок
X-Lognex-Remap-Beta-Feature``).

The checks below come from ``dictionaries/_assortment.md § Отличия от старого эндпоинта`` and
``§ Атрибуты доступные для фильтрации``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal, get_args

from ._filters import Filter, split_conditions
from ._pagination import MAX_LIMIT, Paginator, list_params

if TYPE_CHECKING:
    from ._client import MoySklad, Params

__all__ = ["BETA_FEATURE_HEADER", "WITHOUT_STOCK", "Assortment", "GroupBy"]

logger = logging.getLogger("aiomoysklad")

BETA_FEATURE_HEADER = "X-Lognex-Remap-Beta-Feature"
WITHOUT_STOCK = "assortmentWithoutStock"
PATH = "/entity/assortment"

GroupBy = Literal["product", "variant", "consignment"]

# § Недоступные фильтры.
_UNAVAILABLE_FILTERS = frozenset(
    {
        "alcoholic.type",
        "article",
        "isSerialTrackable",
        "minimumBalance",
        "owner",
        "quantityMode",
        "salePrice",
        "search",
        "shared",
        "stockMode",
        "stockMoment",
        "stockStore",
        "updatedBy",
        "weighed",
    }
)
# § Недоступные сортировки.
_UNAVAILABLE_ORDERS = frozenset(
    {"inTransit", "minimumBalance", "quantity", "reserve", "salePrice", "stock"}
)
# § Ограничения expand: only these, at the first level.
_EXPANDABLE = frozenset({"product", "images", "components"})
# § Поля остатков: the fields the new endpoint no longer returns.
_STOCK_FIELDS = frozenset({"stock", "reserve", "inTransit", "quantity"})
# § Изменения в поведении фильтров: on one field, like operators combine only as ~= with =~.
_LIKE = frozenset({"~", "~=", "=~"})
_PREFIX_AND_SUFFIX = ["=~", "~="]


def check_filter(text: str, group_by: str | None) -> None:
    """Refuse a filter the new assortment endpoint does not take.

    Raises:
        ValueError: An unavailable field, ``barcode!=``, an unsupported like combination, a
            ``type=consignment`` without ``groupBy=consignment``, or ``withSubFolders`` without
            ``productFolder``.
    """
    likes: dict[str, list[str]] = {}
    fields: set[str] = set()
    for field, operator, value in split_conditions(text):
        fields.add(field)
        if field in _UNAVAILABLE_FILTERS:
            msg = f"the assortment endpoint without stock does not filter by {field}"
            raise ValueError(msg)
        if field == "barcode" and operator == "!=":
            msg = "the assortment endpoint without stock does not take barcode!="
            raise ValueError(msg)
        if operator in _LIKE:
            likes.setdefault(field, []).append(operator)
        # § Атрибуты доступные для фильтрации, type.
        if field == "type" and value == "consignment" and group_by != "consignment":
            msg = "filtering by type=consignment needs groupBy=consignment"
            raise ValueError(msg)
    for field, operators in likes.items():
        if len(operators) > 1 and sorted(operators) != _PREFIX_AND_SUFFIX:
            msg = (
                f"on {field}, like operators combine only as one ~= with one =~, got "
                f"{' '.join(operators)}"
            )
            raise ValueError(msg)
    # § Атрибуты доступные для фильтрации, withSubFolders.
    if "withSubFolders" in fields and "productFolder" not in fields:
        msg = "withSubFolders works only together with a productFolder filter"
        raise ValueError(msg)


def _check_order(order: str) -> None:
    for part in order.split(";"):
        field = part.split(",", 1)[0].strip()
        if field in _UNAVAILABLE_ORDERS:
            msg = f"the assortment endpoint without stock cannot be ordered by {field}"
            raise ValueError(msg)


def _check_expand(expand: str | Sequence[str]) -> None:
    fields = [expand] if isinstance(expand, str) else list(expand)
    for field in ",".join(fields).split(","):
        if field.strip() not in _EXPANDABLE:
            msg = (
                "the assortment endpoint without stock can expand only product, images and "
                f"components, at the first level; got expand {field!r}"
            )
            raise ValueError(msg)


class _AssortmentRows(Paginator):
    """A paginator that warns once when rows come back with stock fields."""

    async def pages(self) -> AsyncIterator[dict[str, Any]]:
        warned = False
        async for body in super().pages():
            if not warned and any(
                isinstance(row, dict) and _STOCK_FIELDS & row.keys() for row in body["rows"]
            ):
                warned = True
                logger.warning(
                    "GET %s returned stock fields: MoySklad ignored %s: %s and served the "
                    "legacy endpoint",
                    PATH,
                    BETA_FEATURE_HEADER,
                    WITHOUT_STOCK,
                )
            yield body


class Assortment:
    """The assortment without stock, ``ms.assortment``."""

    def __init__(self, client: MoySklad) -> None:
        self._client = client

    def __repr__(self) -> str:
        return f"Assortment({self._client!r})"

    def iter(
        self,
        *,
        filter: Filter | str | None = None,
        search: str | None = None,
        group_by: GroupBy | None = None,
        order: str | None = None,
        expand: str | Sequence[str] | None = None,
        limit: int = MAX_LIMIT,
        params: Params | None = None,
    ) -> Paginator:
        """Iterate over products, services, bundles, variants and consignments, without stock.

        The rows carry no ``stock``, ``reserve``, ``inTransit`` or ``quantity``; read those from
        the short stock report and pair them with :func:`~aiomoysklad.join_stock`
        (``id`` = ``assortmentId``). If rows do come back with stock fields, MoySklad ignored
        the beta header, and a warning is logged.

        Args:
            filter: A :class:`~aiomoysklad.Filter` or a ``filter`` string.
            search: Context search, as a separate parameter; not a filter field here.
            group_by: ``product`` (products only), ``variant`` (products and variants, the same
                as no grouping) or ``consignment`` (everything, consignments included).
            order: The ``order`` parameter.
            expand: ``product``, ``images`` and ``components`` only, with ``limit`` at most 100.
            limit: Rows per page, 1 to 1000.
            params: Other query parameters.

        Raises:
            ValueError: A filter, order, expand or grouping the new endpoint does not take.
        """
        if group_by is not None and group_by not in get_args(GroupBy):
            msg = f"group_by must be product, variant or consignment, got {group_by!r}"
            raise ValueError(msg)
        if filter is not None:
            check_filter(str(filter), group_by)
        if order:
            _check_order(order)
        if expand:
            _check_expand(expand)
        query = dict(params or {})
        if group_by is not None:
            query["groupBy"] = group_by
        built = list_params(
            filter=filter, order=order, search=search, expand=expand, limit=limit, params=query
        )
        return _AssortmentRows(
            self._client, PATH, built, headers={BETA_FEATURE_HEADER: WITHOUT_STOCK}
        )
