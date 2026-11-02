r"""A builder for the ``filter`` query parameter.

Everything here comes from ``_general.md § Фильтрация выборки с помощью параметра filter``,
``§ Проверка на пустое значение``, ``§ Фильтрация ссылочных полей``, ``§ Фильтрация по
дополнительным полям`` and ``§ Оператор фильтрации "подобие"``:

* conditions are joined with ``;``; the operators are ``=``, ``>``, ``<``, ``>=``, ``<=``,
  ``!=``, ``~``, ``~=`` and ``=~``;
* several ``=`` on one field mean OR, several ``!=`` mean AND, different fields are ANDed;
* ``=`` and then an inequality on the same field "will cause an error"; a repeated ``>``,
  ``<``, ``>=`` or ``<=`` on one field keeps only the first one and "will be an error in future
  versions";
* ``field=`` matches an empty value, ``field!=`` a present one;
* ``~`` (substring), ``~=`` (prefix) and ``=~`` (suffix) work only on string fields, case
  insensitive, and not on uuid or date fields;
* a ``;`` inside a text value is written ``\;``. That is the only escape the docs define;
* reference fields are filtered by href, custom attributes by the attribute's href as the
  field name.

The client percent-encodes the whole value when it sends it.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from ._time import format_datetime

__all__ = ["Filter", "split_conditions"]

_EQ = "="
_NE = "!="
_COMPARISONS = frozenset({">", "<", ">=", "<="})
_LIKE = frozenset({"~", "~=", "=~"})
_FIELD_FORBIDDEN = frozenset(";=<>!~")

# A condition is a field without operator characters, then the longest operator that fits.
_CONDITION = re.compile(r"([^=<>!~]*)(!=|~=|=~|>=|<=|=|>|<|~)(.*)", re.S)
_SEPARATOR = re.compile(r"(?<!\\);")


@dataclass(frozen=True)
class _Condition:
    field: str | None  # None for a raw expression
    operator: str
    text: str

    def __str__(self) -> str:
        if self.field is None:
            return self.text
        return f"{self.field}{self.operator}{self.text}"


def _check_field(field: str) -> str:
    if not isinstance(field, str) or not field or field != field.strip():
        msg = f"invalid filter field: {field!r}"
        raise ValueError(msg)
    if _FIELD_FORBIDDEN & set(field):
        msg = f"a filter field cannot hold ; = < > ! or ~: {field!r}"
        raise ValueError(msg)
    return field


def _escape(text: str) -> str:
    if text.endswith("\\") or "\\;" in text:
        # The docs define \\; as an escaped semicolon and nothing else, so a backslash right
        # before a separator could be read either way (open question 7 of docs/design.md).
        msg = f"a backslash before ; or at the end of a filter value is ambiguous: {text!r}"
        raise ValueError(msg)
    return text.replace(";", "\\;")


def _href(value: Any) -> str | None:
    if isinstance(value, Mapping):
        meta = value.get("meta", value)
        href = meta.get("href") if isinstance(meta, Mapping) else None
    else:
        href = getattr(value, "href", None)
    return href if isinstance(href, str) and href else None


def _text(value: str) -> str:
    if not value:
        msg = "an empty text value is ambiguous; use is_empty() or not_empty()"
        raise ValueError(msg)
    return _escape(value)


def _number(value: float | Decimal) -> str:
    if not math.isfinite(value):
        msg = f"a filter number must be finite: {value!r}"
        raise ValueError(msg)
    return str(value)


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (int, uuid.UUID)):
        return str(value)
    if isinstance(value, (float, Decimal)):
        return _number(value)
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, date):
        msg = "pass a datetime, not a date: filters take YYYY-MM-DD HH:MM:SS"
        raise TypeError(msg)
    if value is None:
        msg = "None is not a filter value; use is_empty() or not_empty()"
        raise ValueError(msg)
    href = _href(value)
    if href is None:
        msg = f"cannot use a {type(value).__name__} as a filter value"
        raise TypeError(msg)
    return _check_href(href)


def _check_href(href: str) -> str:
    # An href goes into the filter as it is, so a ; in it would start another condition, and
    # a backslash could escape the separator after it.
    if ";" in href or "\\" in href:
        msg = f"an href in a filter cannot hold ; or a backslash: {href!r}"
        raise ValueError(msg)
    parts = urlsplit(href)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        msg = f"an href in a filter must be an absolute http(s) URL: {href!r}"
        raise ValueError(msg)
    return href


class Filter:
    """An immutable ``filter`` expression; each method returns a new filter.

    ::

        f = Filter().eq("state.name", "Новый", "Принят").ge("updated", since)
        await ms.get("/entity/customerorder", params={"filter": str(f)})

    Values: ``str`` with ``;`` escaped, ``bool`` as ``true``/``false``, numbers as they are, a
    ``UUID`` as text, a ``datetime`` as ``YYYY-MM-DD HH:MM:SS`` in Moscow time (a naive one is
    taken as Moscow time), and a meta dict, an entity with ``meta``, or anything with ``href``
    as that href, which must be an absolute http(s) URL without ``;`` or a backslash.

    Raises ``ValueError`` early for what the docs call an error: an equality and a comparison
    on one field, the same comparison twice on one field, and a like operator on a value that
    is not text.
    """

    __slots__ = ("_conditions",)

    def __init__(self) -> None:
        self._conditions: tuple[_Condition, ...] = ()

    @classmethod
    def _of(cls, conditions: tuple[_Condition, ...]) -> Filter:
        _check_conflicts(conditions)
        made = cls()
        made._conditions = conditions
        return made

    def _add(self, *conditions: _Condition) -> Filter:
        return Filter._of(self._conditions + conditions)

    def _many(self, field: str, operator: str, values: tuple[Any, ...]) -> Filter:
        _check_field(field)
        if not values:
            msg = f"{operator} on {field} needs at least one value"
            raise ValueError(msg)
        return self._add(*(_Condition(field, operator, _format(v)) for v in values))

    def _one(self, field: str, operator: str, value: Any) -> Filter:
        _check_field(field)
        return self._add(_Condition(field, operator, _format(value)))

    def _like(self, field: str, operator: str, value: Any) -> Filter:
        _check_field(field)
        if not isinstance(value, str):
            msg = f"{operator} works on text only, got {type(value).__name__} for {field}"
            raise ValueError(msg)
        return self._add(_Condition(field, operator, _format(value)))

    def eq(self, field: str, *values: Any) -> Filter:
        """``field=value``; several values mean any of them."""
        return self._many(field, _EQ, values)

    def ne(self, field: str, *values: Any) -> Filter:
        """``field!=value``; several values mean none of them."""
        return self._many(field, _NE, values)

    def gt(self, field: str, value: Any) -> Filter:
        """``field>value``."""
        return self._one(field, ">", value)

    def lt(self, field: str, value: Any) -> Filter:
        """``field<value``."""
        return self._one(field, "<", value)

    def ge(self, field: str, value: Any) -> Filter:
        """``field>=value``."""
        return self._one(field, ">=", value)

    def le(self, field: str, value: Any) -> Filter:
        """``field<=value``."""
        return self._one(field, "<=", value)

    def contains(self, field: str, text: str) -> Filter:
        """``field~text``: the text appears anywhere, case insensitive."""
        return self._like(field, "~", text)

    def startswith(self, field: str, text: str) -> Filter:
        """``field~=text``: the value starts with the text, case insensitive."""
        return self._like(field, "~=", text)

    def endswith(self, field: str, text: str) -> Filter:
        """``field=~text``: the value ends with the text, case insensitive."""
        return self._like(field, "=~", text)

    def is_empty(self, field: str) -> Filter:
        """``field=``: the field has no value (or an empty string)."""
        return self._add(_Condition(_check_field(field), _EQ, ""))

    def not_empty(self, field: str) -> Filter:
        """``field!=``: the field has a value."""
        return self._add(_Condition(_check_field(field), _NE, ""))

    def raw(self, expression: str) -> Filter:
        """Append an expression as it is, unchecked and unescaped."""
        if not expression:
            msg = "a raw filter expression must not be empty"
            raise ValueError(msg)
        return self._add(_Condition(None, "", expression))

    def __and__(self, other: object) -> Filter:
        if not isinstance(other, Filter):
            return NotImplemented
        return Filter._of(self._conditions + other._conditions)

    def __str__(self) -> str:
        return ";".join(map(str, self._conditions))

    def __repr__(self) -> str:
        return f"Filter({str(self)!r})"

    def __bool__(self) -> bool:
        return bool(self._conditions)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Filter):
            return NotImplemented
        return self._conditions == other._conditions

    def __hash__(self) -> int:
        return hash(self._conditions)


def _check_conflicts(conditions: tuple[_Condition, ...]) -> None:
    equalities: set[str] = set()
    comparisons: set[tuple[str, str]] = set()
    for condition in conditions:
        field, operator = condition.field, condition.operator
        if field is None:
            continue
        if operator in _COMPARISONS:
            if (field, operator) in comparisons:
                msg = f"repeated {operator} on {field}: MoySklad uses only the first one"
                raise ValueError(msg)
            comparisons.add((field, operator))
        elif operator == _EQ:
            equalities.add(field)
    clash = equalities & {field for field, _ in comparisons}
    if clash:
        msg = f"= and a comparison on the same field are an error in MoySklad: {sorted(clash)}"
        raise ValueError(msg)


def split_conditions(text: str) -> list[tuple[str, str, str]]:
    r"""Split a ``filter`` string into ``(field, operator, value)`` triples.

    Conditions are separated by ``;`` that is not escaped as ``\;``; values keep their
    escapes. A part without an operator comes back as ``(part, "", "")``. Used to check a
    filter against the rules of one endpoint, for filters built here and raw strings alike.
    """
    conditions: list[tuple[str, str, str]] = []
    for part in _SEPARATOR.split(text) if text else []:
        match = _CONDITION.fullmatch(part)
        if match is None:
            conditions.append((part, "", ""))
        else:
            field, operator, value = match.groups()
            conditions.append((field, operator, value))
    return conditions
