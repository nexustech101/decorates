"""
Query operator parsing for ``DatabaseRegistry.filter()`` style lookups.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


VALID_OPERATORS = {
    "eq",
    "not",
    "gt",
    "gte",
    "lt",
    "lte",
    "like",
    "ilike",
    "in",
    "not_in",
    "is_null",
    "between",
    "contains",
    "startswith",
    "endswith",
}


def split_field_expr(field_expr: str) -> tuple[str, str]:
    """Split ``field__operator`` into ``(field, operator)``."""
    if "__" not in field_expr:
        return field_expr, "eq"
    field_name, operator = field_expr.rsplit("__", 1)
    return field_name, operator


def is_iterable_value(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray))


def parse_criterion(table: Any, field_expr: str, value: Any) -> Any:
    """Return a SQLAlchemy predicate for one ``field__operator=value`` pair."""
    field_name, operator = split_field_expr(field_expr)
    column = table.c[field_name]

    if operator == "eq":
        if is_iterable_value(value):
            return column.in_(list(value))
        return column == value
    if operator == "not":
        return column != value
    if operator == "gt":
        return column > value
    if operator == "gte":
        return column >= value
    if operator == "lt":
        return column < value
    if operator == "lte":
        return column <= value
    if operator == "like":
        return column.like(value)
    if operator == "ilike":
        return column.ilike(value)
    if operator == "in":
        return column.in_(list(value))
    if operator == "not_in":
        return column.not_in(list(value))
    if operator == "is_null":
        return column.is_(None) if value else column.is_not(None)
    if operator == "between":
        lower, upper = value
        return column.between(lower, upper)
    if operator == "contains":
        return column.contains(value)
    if operator == "startswith":
        return column.startswith(value)
    if operator == "endswith":
        return column.endswith(value)
    raise ValueError(f"Unsupported operator: {operator}")
