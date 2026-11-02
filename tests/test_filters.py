from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from aiomoysklad import Filter, Meta
from aiomoysklad._time import MSK

API = "https://api.moysklad.ru/api/remap/1.2"
AGENT = f"{API}/entity/counterparty/7944ef04-f831-11e5-7a69-971500188b19"


def test_the_example_of_the_design() -> None:
    f = (
        Filter()
        .eq("state.name", "Новый", "Принят")
        .ge("updated", datetime(2026, 11, 1, 9, 0, tzinfo=MSK))
        .contains("name", "a;b")
        .is_empty("description")
    )
    assert str(f) == (
        r"state.name=Новый;state.name=Принят;updated>=2026-11-01 09:00:00;name~a\;b;description="
    )


@pytest.mark.parametrize(
    ("method", "operator"),
    [
        ("eq", "="),
        ("ne", "!="),
        ("gt", ">"),
        ("lt", "<"),
        ("ge", ">="),
        ("le", "<="),
        ("contains", "~"),
        ("startswith", "~="),
        ("endswith", "=~"),
    ],
)
def test_each_documented_operator(method: str, operator: str) -> None:
    # _general.md § Фильтрация выборки с помощью параметра filter and § Оператор фильтрации
    # "подобие".
    f = getattr(Filter(), method)("code", "ms")
    assert str(f) == f"code{operator}ms"


def test_several_equalities_are_an_or_and_several_inequalities_an_and() -> None:
    assert str(Filter().eq("sum", 100, 150)) == "sum=100;sum=150"
    assert str(Filter().ne("name", "0001", "0002")) == "name!=0001;name!=0002"


def test_empty_and_non_empty_checks() -> None:
    # _general.md § Проверка на пустое значение.
    assert str(Filter().is_empty("agent")) == "agent="
    assert str(Filter().not_empty("agent")) == "agent!="


def test_a_range_on_one_field_is_allowed() -> None:
    f = (
        Filter()
        .gt("moment", datetime(2016, 10, 11, 12, tzinfo=MSK))
        .lt("moment", datetime(2016, 10, 11, 13, tzinfo=MSK))
    )
    assert str(f) == "moment>2016-10-11 12:00:00;moment<2016-10-11 13:00:00"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (True, "true"),
        (False, "false"),
        (100, "100"),
        (200.8, "200.8"),
        (Decimal("10.50"), "10.50"),
        (uuid.UUID("94975104-3cad-11e8-1e44-bd4d00000084"), "94975104-3cad-11e8-1e44-bd4d00000084"),
        (datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), "2026-11-01 09:00:00"),
        (datetime(2026, 11, 1, 9, 0), "2026-11-01 09:00:00"),  # noqa: DTZ001 - taken as MSK
        ({"meta": {"href": AGENT, "type": "counterparty"}}, AGENT),
        ({"href": AGENT, "type": "counterparty"}, AGENT),
        (Meta(href=AGENT, type="counterparty"), AGENT),
    ],
)
def test_values_are_formatted_by_type(value: Any, text: str) -> None:
    assert str(Filter().eq("x", value)) == f"x={text}"


@pytest.mark.parametrize(
    "value", [None, [1, 2], date(2026, 11, 1), object(), {"name": "x"}, {"meta": {}}]
)
def test_values_of_other_types_are_refused(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        Filter().eq("x", value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN")])
def test_numbers_that_are_not_finite_are_refused(value: Any) -> None:
    with pytest.raises(ValueError, match="finite"):
        Filter().gt("sum", value)


def test_a_semicolon_in_a_value_is_escaped() -> None:
    # "Для использования самого символа ; в текстовых фильтрах необходимо указывать два символа
    # \;" (_general.md § Фильтрация выборки с помощью параметра filter).
    assert str(Filter().eq("name", "a;b;c")) == r"name=a\;b\;c"


@pytest.mark.parametrize("value", ["ends with \\", r"a\;b", "\\"])
def test_a_backslash_before_a_separator_is_refused(value: str) -> None:
    # The docs do not say how such a backslash is read (open question 7 of docs/design.md).
    with pytest.raises(ValueError, match="backslash"):
        Filter().eq("name", value)


def test_other_backslashes_pass() -> None:
    assert str(Filter().eq("name", "C:\\dir\\file")) == "name=C:\\dir\\file"


def test_an_equality_and_a_comparison_on_one_field_are_refused() -> None:
    # "filter=sum=100;sum>99 вызовет ошибку".
    with pytest.raises(ValueError, match="sum"):
        Filter().eq("sum", 100).gt("sum", 99)
    with pytest.raises(ValueError, match="sum"):
        Filter().le("sum", 99).eq("sum", 100)
    with pytest.raises(ValueError, match="sum"):
        Filter().is_empty("sum").ge("sum", 1)
    with pytest.raises(ValueError, match="sum"):
        Filter().eq("sum", 100) & Filter().lt("sum", 99)


@pytest.mark.parametrize("method", ["gt", "lt", "ge", "le"])
def test_a_repeated_comparison_on_one_field_is_refused(method: str) -> None:
    # Only the first one is used, and it "will be an error in future versions".
    f = getattr(Filter(), method)("sum", 99)
    with pytest.raises(ValueError, match="repeated"):
        getattr(f, method)("sum", 100)


def test_one_comparison_per_call() -> None:
    with pytest.raises(TypeError):
        Filter().gt("sum", 1, 2)  # type: ignore[call-arg]


@pytest.mark.parametrize("method", ["contains", "startswith", "endswith"])
@pytest.mark.parametrize("value", [1, True, uuid.UUID(int=1), datetime(2026, 1, 1, tzinfo=MSK)])
def test_like_operators_take_text_only(method: str, value: Any) -> None:
    # They work "exclusively with string fields" and not with uuid or date fields.
    with pytest.raises(ValueError, match="text"):
        getattr(Filter(), method)("code", value)


@pytest.mark.parametrize("method", ["contains", "startswith", "endswith", "eq", "ne"])
def test_an_empty_text_is_refused(method: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        getattr(Filter(), method)("code", "")


def test_an_equality_needs_a_value() -> None:
    with pytest.raises(ValueError, match="value"):
        Filter().eq("code")


@pytest.mark.parametrize("field", ["", " name", "a;b", "a=b", "a<b", "a>b", "a!b", "a~b"])
def test_bad_field_names_are_refused(field: str) -> None:
    with pytest.raises(ValueError, match="field"):
        Filter().eq(field, "x")


def test_an_attribute_href_is_a_field_name() -> None:
    # _general.md § Фильтрация по дополнительным полям.
    attribute = f"{API}/entity/product/metadata/attributes/0d2cdbb4-3c8c-11e8-1e44-bd4d00000007"
    assert str(Filter().eq(attribute, "x")) == f"{attribute}=x"


def test_a_filter_is_immutable() -> None:
    empty = Filter()
    named = empty.eq("name", "x")
    assert str(empty) == ""
    assert not empty
    assert named
    assert str(named.eq("code", "y")) == "name=x;code=y"
    assert str(named) == "name=x"


def test_filters_combine_with_and() -> None:
    f = Filter().eq("archived", False) & Filter().startswith("code", "ms")
    assert str(f) == "archived=false;code~=ms"
    with pytest.raises(TypeError):
        _ = Filter() & "code=x"


def test_raw_conditions_pass_through() -> None:
    f = Filter().raw("isDeleted=true;isDeleted=false").eq("name", "x")
    assert str(f) == "isDeleted=true;isDeleted=false;name=x"
    with pytest.raises(ValueError, match="empty"):
        Filter().raw("")


def test_the_repr_shows_the_text() -> None:
    assert repr(Filter().eq("name", "x")) == "Filter('name=x')"


def test_equal_filters_compare_equal() -> None:
    assert Filter().eq("a", 1) == Filter().eq("a", 1)
    assert Filter().eq("a", 1) != Filter().eq("a", 2)
    assert Filter() != "a=1"


def test_dates_are_moscow_time_down_to_the_second() -> None:
    moment = datetime(2026, 11, 1, 23, 59, 59, 999_999, tzinfo=timezone(timedelta(hours=-5)))
    assert str(Filter().ge("updated", moment)) == "updated>=2026-11-02 07:59:59"
