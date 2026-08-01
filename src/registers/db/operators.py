"""
Query operator parsing for ``Model.objects.filter()`` style lookups.

A single dispatch table drives both the predicate builder and the set of names the
validator accepts, so a new operator cannot be added to one without the other.
Previously ``VALID_OPERATORS`` was a hand-maintained set sitting next to a
fourteen-branch ``if`` chain — two places to update, and nothing enforcing agreement.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Callable


#: ``operator name -> (column, value) -> SQLAlchemy predicate``
OPERATOR_BUILDERS: dict[str, Callable[[Any, Any], Any]] = {
    "eq":         lambda column, value: column == value,
    "not":        lambda column, value: column != value,
    "gt":         lambda column, value: column > value,
    "gte":        lambda column, value: column >= value,
    "lt":         lambda column, value: column < value,
    "lte":        lambda column, value: column <= value,
    "like":       lambda column, value: column.like(value),
    "ilike":      lambda column, value: column.ilike(value),
    "in":         lambda column, value: column.in_(list(value)),
    "not_in":     lambda column, value: column.not_in(list(value)),
    "is_null":    lambda column, value: column.is_(None) if value else column.is_not(None),
    "between":    lambda column, value: column.between(value[0], value[1]),
    "contains":   lambda column, value: column.contains(value),
    "startswith": lambda column, value: column.startswith(value),
    "endswith":   lambda column, value: column.endswith(value),
}

#: Derived, never hand-maintained.
VALID_OPERATORS: frozenset[str] = frozenset(OPERATOR_BUILDERS)


def split_field_expr(field_expr: str) -> tuple[str, str]:
    """
    Split ``field__operator`` into ``(field, operator)``.

    Splits on the final ``__`` unconditionally rather than only on a recognized
    operator name. That means ``age__approx`` is reported as an unknown *operator*
    rather than an unknown *field*, which is the more actionable error for the
    overwhelmingly common cause — a typo'd or misremembered operator. The tradeoff
    is that model fields cannot themselves contain a double underscore.
    """
    if "__" not in field_expr:
        return field_expr, "eq"
    field_name, operator = field_expr.rsplit("__", 1)
    return field_name, operator


def is_iterable_value(value: Any) -> bool:
    """True for a genuine collection — strings and bytes are scalars here."""
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray))


def parse_criterion(table: Any, field_expr: str, value: Any) -> Any:
    """Return a SQLAlchemy predicate for one ``field__operator=value`` pair."""
    field_name, operator = split_field_expr(field_expr)
    builder = OPERATOR_BUILDERS.get(operator)
    if builder is None:  # pragma: no cover - callers validate the operator first
        raise ValueError(f"Unsupported operator: {operator}")
    return builder(table.c[field_name], value)
