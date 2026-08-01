"""
Read path: lookups, filtering, projection, aggregation, pagination, raw SQL.

All criteria validation lives here, driven by the pre-built per-field validators
on :class:`~registers.db.specs.FieldSpec`.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Generic, Mapping, TypeVar

from sqlalchemy import (
    and_,
    func,
    not_,
    or_,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError

from registers.db.exceptions import (
    InvalidQueryError,
    RecordNotFoundError,
)
from registers.db.manager.context import (
    _STRING_MATCH_OPERATORS,
)
from registers.db.operators import VALID_OPERATORS, is_iterable_value, parse_criterion, split_field_expr
from registers.db.query import Agg, Page, Q
from registers.db.specs import (
    FieldCoercionError,
    _identity as _spec_identity,
)

logger = logging.getLogger(__name__)
# Unbound: a model may be a pydantic BaseModel or a stdlib dataclass.
ModelT = TypeVar("ModelT")


def _validate_bool_flag(value: Any) -> None:
    """``field__is_null=`` takes a truthy flag; reject obvious mistakes only."""
    if not isinstance(value, bool) and value not in (0, 1, None):
        raise FieldCoercionError(value, "a boolean")


class _ReadMixin(Generic[ModelT]):
    """Read path: lookups, filtering, projection, aggregation, pagination, raw SQL."""

    def get(self, *args: Any, **criteria: Any) -> ModelT | None:
        """
        Return the first matching row, or None.

        Accepts a single positional primary-key value::

            User.objects.get(1)

        Or keyword criteria::

            User.objects.get(email="alice@example.com")
        """
        include_deleted = bool(criteria.pop("include_deleted", False))
        normalized = self._normalize_lookup(args, criteria)
        rows = self.filter(limit=1, include_deleted=include_deleted, **normalized)
        return rows[0] if rows else None

    def require(self, *args: Any, **criteria: Any) -> ModelT:
        """Return the first matching row or raise :class:`RecordNotFoundError`."""
        record = self.get(*args, **criteria)
        if record is None:
            normalized = self._normalize_lookup(args, criteria)
            raise RecordNotFoundError(
                f"No {self.model_cls.__name__} found matching {normalized!r}.",
                operation="require",
                model=self.model_cls.__name__,
                table=self.table_name,
                details={"criteria": normalized},
            )
        return record

    def filter(
        self,
        *conditions: Q,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | list[str] | tuple[str, ...] | None = None,
        include_deleted: bool = False,
        **criteria: Any,
    ) -> list[ModelT]:
        """
        Return all rows matching *criteria*.

        Supports optional *limit* and *offset* for pagination plus
        ``order_by`` using ``field`` / ``-field`` syntax.
        """
        if criteria:
            self._assert_known_fields(criteria)
        for condition in conditions:
            self._validate_q(condition)
        self._validate_pagination(limit=limit, offset=offset)

        stmt = select(self._table)
        criteria = self._with_policy_criteria(criteria, include_deleted=include_deleted)
        stmt = self._apply_where(stmt, criteria)
        stmt = self._apply_q_conditions(stmt, conditions)

        if order_by is not None:
            stmt = self._apply_order_by(stmt, order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)

        try:
            with self._read_connection_scope() as conn:
                rows = conn.execute(stmt).mappings().all()
            return [self._row_to_model(row) for row in rows]
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("filter", exc)

    def all(self, order_by: str | list[str] | tuple[str, ...] | None = None) -> list[ModelT]:
        """Return every row as validated Pydantic models."""
        return self.filter(order_by=order_by)

    def get_all(self) -> list[ModelT]:
        """Alias for ``all()``."""
        return self.all()

    def exists(self, **criteria: Any) -> bool:
        """Return True when at least one row matches *criteria*."""
        include_deleted = bool(criteria.pop("include_deleted", False))
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(func.count()).select_from(self._table)
        criteria = self._with_policy_criteria(criteria, include_deleted=include_deleted)
        stmt = self._apply_where(stmt, criteria)
        try:
            with self._read_connection_scope() as conn:
                return (conn.execute(stmt).scalar_one() or 0) > 0
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("exists", exc)

    def count(self, **criteria: Any) -> int:
        """Return the number of rows matching *criteria* (or all rows if empty)."""
        include_deleted = bool(criteria.pop("include_deleted", False))
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(func.count()).select_from(self._table)
        criteria = self._with_policy_criteria(criteria, include_deleted=include_deleted)
        stmt = self._apply_where(stmt, criteria)
        try:
            with self._read_connection_scope() as conn:
                return conn.execute(stmt).scalar_one() or 0
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("count", exc)

    def exclude(self, *conditions: Q, **criteria: Any) -> list[ModelT]:
        """Return records that do not match the supplied predicates."""
        inverted = [~condition for condition in conditions]
        if criteria:
            inverted.append(~Q(**criteria))
        return self.filter(*inverted)

    def select(self, *fields: str, q: Q | None = None, **criteria: Any) -> list[dict[str, Any]]:
        """Return projected dictionaries for selected fields."""
        if not fields:
            raise InvalidQueryError(
                "select() requires at least one field.",
                operation="select",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        self._assert_known_projection_fields(fields)
        if criteria:
            self._assert_known_fields(criteria)
        conditions = (q,) if q is not None else ()
        stmt = select(*[self._table.c[field] for field in fields])
        stmt = self._apply_where(stmt, self._with_policy_criteria(criteria))
        stmt = self._apply_q_conditions(stmt, conditions)
        try:
            with self._read_connection_scope() as conn:
                return [dict(row) for row in conn.execute(stmt).mappings().all()]
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("select", exc)

    def values_list(self, field: str, *, q: Q | None = None, **criteria: Any) -> list[Any]:
        """Return one selected field as a flat list."""
        return [row[field] for row in self.select(field, q=q, **criteria)]

    def count_by(self, field: str, **criteria: Any) -> dict[Any, int]:
        """Return grouped counts keyed by ``field``."""
        self._assert_known_projection_fields((field,))
        if criteria:
            self._assert_known_fields(criteria)
        column = self._table.c[field]
        stmt = select(column, func.count()).select_from(self._table).group_by(column)
        stmt = self._apply_where(stmt, self._with_policy_criteria(criteria))
        try:
            with self._read_connection_scope() as conn:
                return {row[0]: row[1] for row in conn.execute(stmt).all()}
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("count_by", exc)

    def aggregate(self, *aggregates: Agg, **kwargs: Any) -> Any:
        """Run aggregate expressions over rows matching optional criteria."""
        named_aggs = {key: value for key, value in kwargs.items() if isinstance(value, Agg)}
        criteria = {key: value for key, value in kwargs.items() if not isinstance(value, Agg)}
        if criteria:
            self._assert_known_fields(criteria)

        expressions: list[Any] = []
        labels: list[str] = []
        for index, aggregate in enumerate(aggregates):
            label = f"{aggregate.function}_{aggregate.field.replace('*', 'all')}_{index}"
            expressions.append(self._aggregate_expression(aggregate).label(label))
            labels.append(label)
        for label, aggregate in named_aggs.items():
            expressions.append(self._aggregate_expression(aggregate).label(label))
            labels.append(label)
        if not expressions:
            raise InvalidQueryError(
                "aggregate() requires at least one Agg expression.",
                operation="aggregate",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        stmt = select(*expressions).select_from(self._table)
        stmt = self._apply_where(stmt, self._with_policy_criteria(criteria))
        try:
            with self._read_connection_scope() as conn:
                row = conn.execute(stmt).mappings().one()
            if len(aggregates) == 1 and not named_aggs:
                return row[labels[0]]
            return {label: row[label] for label in labels}
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("aggregate", exc)

    def paginate(
        self,
        *,
        order_by: str,
        limit: int = 20,
        cursor: str | None = None,
        **criteria: Any,
    ) -> Page:
        """Return a cursor-based page ordered by one stable model field."""
        self._validate_pagination(limit=limit, offset=None)
        if limit == 0:
            return Page(items=[], next_cursor=None, has_next=False)
        
        descending = order_by.startswith("-")
        field = order_by[1:] if descending else order_by
        self._assert_known_projection_fields((field,))

        if cursor is not None:
            cursor_value = self._decode_cursor(cursor)
            criteria[f"{field}__lt" if descending else f"{field}__gt"] = cursor_value

        rows = self.filter(limit=limit + 1, order_by=order_by, **criteria)
        has_next = len(rows) > limit
        items = rows[:limit]
        next_cursor = self._encode_cursor(getattr(items[-1], field)) if has_next and items else None
        return Page(items=items, next_cursor=next_cursor, has_next=has_next)

    def raw(self, sql: str, params: Mapping[str, Any] | None = None) -> list[ModelT]:
        """Execute parameterized SQL and hydrate model instances."""
        rows = self.raw_dicts(sql, params)
        return [self._row_to_model(row) for row in rows]

    def raw_dicts(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute parameterized SQL and return dictionaries."""
        self._assert_safe_raw_sql(sql, params)
        try:
            with self._read_connection_scope() as conn:
                return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("raw", exc)

    def execute_raw(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        """Execute parameterized SQL and return SQLAlchemy's result object."""
        self._assert_safe_raw_sql(sql, params)
        try:
            with self._connection_scope() as conn:
                return conn.execute(text(sql), params or {})
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("execute_raw", exc)

    def first(
        self,
        order_by: str | list[str] | tuple[str, ...] | None = None,
        **criteria: Any,
    ) -> ModelT | None:
        """Return the first row for the given filter and sort order."""
        rows = self.filter(limit=1, order_by=order_by, **criteria)
        return rows[0] if rows else None

    def last(
        self,
        order_by: str | list[str] | tuple[str, ...] | None = None,
        **criteria: Any,
    ) -> ModelT | None:
        """Return the last row for the given filter and sort order."""
        reverse_order = self._reverse_order_by(order_by or self.key_field)
        rows = self.filter(limit=1, order_by=reverse_order, **criteria)
        return rows[0] if rows else None

    def refresh(self, instance: ModelT) -> ModelT:
        """
        Return a fresh copy of *instance* re-fetched from the database.

        Raises :class:`RecordNotFoundError` if the record no longer exists.
        """
        key_value = getattr(instance, self.key_field)
        return self.require(key_value)

    def _validate_pagination(self, *, limit: int | None, offset: int | None) -> None:
        if limit is not None and limit < 0:
            raise InvalidQueryError(
                "limit must be greater than or equal to 0.",
                operation="query_validation",
                model=self.model_cls.__name__,
                table=self.table_name,
                field="limit",
                details={"limit": limit},
            )
        if offset is not None and offset < 0:
            raise InvalidQueryError(
                "offset must be greater than or equal to 0.",
                operation="query_validation",
                model=self.model_cls.__name__,
                table=self.table_name,
                field="offset",
                details={"offset": offset},
            )

    def _apply_where(self, stmt: Any, criteria: Mapping[str, Any]) -> Any:
        for field_expr, value in self._normalize_criteria_for_db(criteria).items():
            stmt = stmt.where(parse_criterion(self._table, field_expr, value))
        return stmt

    def _criteria_expression(self, criteria: Mapping[str, Any]) -> Any:
        expressions = [
            parse_criterion(self._table, field_expr, value)
            for field_expr, value in self._normalize_criteria_for_db(criteria).items()
        ]
        return and_(*expressions) if expressions else None

    def _q_expression(self, condition: Q) -> Any:
        if condition.children:
            child_expressions = [self._q_expression(child) for child in condition.children]
            expression = (
                or_(*child_expressions)
                if condition.connector == "or"
                else and_(*child_expressions)
            )
        else:
            expression = self._criteria_expression(condition.criteria)
        if condition.negated and expression is not None:
            return not_(expression)
        return expression

    def _apply_q_conditions(self, stmt: Any, conditions: tuple[Q, ...] | list[Q]) -> Any:
        for condition in conditions:
            expression = self._q_expression(condition)
            if expression is not None:
                stmt = stmt.where(expression)
        return stmt

    def _validate_q(self, condition: Q) -> None:
        if not isinstance(condition, Q):
            raise InvalidQueryError(
                "filter() positional arguments must be Q objects.",
                operation="query_validation",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        if condition.criteria:
            self._assert_known_fields(condition.criteria)
        for child in condition.children:
            self._validate_q(child)

    def _assert_known_projection_fields(self, fields: tuple[str, ...] | list[str]) -> None:
        unknown = [field for field in fields if field not in self._table.c]
        if unknown:
            raise InvalidQueryError(
                f"Unknown field(s) {unknown!r} on model '{self.model_cls.__name__}'.",
                operation="query_validation",
                model=self.model_cls.__name__,
                table=self.table_name,
                details={"unknown_fields": unknown},
            )
        encrypted = [field for field in fields if field in self._encrypted_fields]
        if encrypted:
            raise InvalidQueryError(
                f"Encrypted field(s) {encrypted!r} cannot be projected directly.",
                operation="query_validation",
                model=self.model_cls.__name__,
                table=self.table_name,
                details={"encrypted_fields": encrypted},
            )

    def _aggregate_expression(self, aggregate: Agg) -> Any:
        if aggregate.field != "*":
            self._assert_known_projection_fields((aggregate.field,))
            column = self._table.c[aggregate.field]
        else:
            column = self._table.c[self.key_field]

        if aggregate.function == "count":
            expression = func.count(column)
        elif aggregate.function == "sum":
            expression = func.sum(column)
        elif aggregate.function == "avg":
            expression = func.avg(column)
        elif aggregate.function == "min":
            expression = func.min(column)
        elif aggregate.function == "max":
            expression = func.max(column)
        elif aggregate.function == "count_distinct":
            expression = func.count(func.distinct(column))
        else:  # pragma: no cover - Agg factory constrains this
            raise InvalidQueryError(f"Unknown aggregate function '{aggregate.function}'.")

        if aggregate.criteria:
            self._assert_known_fields(aggregate.criteria)
            criteria = self._with_policy_criteria(aggregate.criteria)
            predicate = self._criteria_expression(criteria)
            if predicate is not None:
                expression = expression.filter(predicate)
        return expression

    @staticmethod
    def _encode_cursor(value: Any) -> str:
        payload = json.dumps({"value": value}, default=str).encode()
        return base64.urlsafe_b64encode(payload).decode()

    @staticmethod
    def _decode_cursor(cursor: str) -> Any:
        try:
            return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())["value"]
        except Exception as exc:
            raise InvalidQueryError("Invalid pagination cursor.", operation="paginate") from exc

    def _assert_safe_raw_sql(self, sql: str, params: Mapping[str, Any] | None) -> None:
        if params is None and any(marker in sql for marker in ("%s", "{}", "{0}")):
            raise InvalidQueryError(
                "raw SQL must use bound parameters instead of string interpolation.",
                operation="raw",
                model=self.model_cls.__name__,
                table=self.table_name,
            )

    def _apply_order_by(
        self,
        stmt: Any,
        order_by: str | list[str] | tuple[str, ...],
    ) -> Any:
        fields = [order_by] if isinstance(order_by, str) else list(order_by)
        for field in fields:
            descending = field.startswith("-")
            field_name = field[1:] if descending else field
            if field_name not in self.adapter.fields():
                raise InvalidQueryError(
                    f"Unknown sort field '{field_name}'.",
                    operation="order_by",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_name,
                )
            column = self._table.c[field_name]
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        return stmt

    def _reverse_order_by(
        self,
        order_by: str | list[str] | tuple[str, ...],
    ) -> str | list[str]:
        fields = [order_by] if isinstance(order_by, str) else list(order_by)
        reversed_fields = [
            field[1:] if field.startswith("-") else f"-{field}"
            for field in fields
        ]
        return reversed_fields[0] if isinstance(order_by, str) else reversed_fields

    def _normalize_lookup(self, args: tuple[Any, ...], criteria: Mapping[str, Any]) -> dict[str, Any]:
        if args and criteria:
            raise InvalidQueryError(
                "Pass either a positional primary-key or keyword criteria, not both.",
                operation="lookup",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        if len(args) > 1:
            raise InvalidQueryError(
                "Only one positional lookup argument is supported.",
                operation="lookup",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        return {self.key_field: args[0]} if args else dict(criteria)

    def _assert_known_update_fields(self, updates: Mapping[str, Any]) -> None:
        self._assert_known_fields(updates, allow_operators=False)

    def _assert_known_fields(
        self,
        fields: Mapping[str, Any],
        *,
        allow_operators: bool = True,
    ) -> None:
        model_fields = self.adapter.fields()
        unknown: list[str] = []
        normalized_fields: list[tuple[str, str, Any]] = []

        for field_expr, value in fields.items():
            if not allow_operators and "__" in field_expr:
                raise InvalidQueryError(
                    f"Update field '{field_expr}' is invalid. "
                    "Use plain field names without query operators.",
                    operation="query_validation",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_expr,
                    details={"operator_style_updates_not_allowed": True},
                )
            field_name, operator = split_field_expr(field_expr)
            if field_name in self._db_excluded_fields:
                unknown.append(field_name)
                continue
            if field_name not in model_fields or field_name not in self._table.c:
                unknown.append(field_name)
                continue
            if allow_operators and field_name in self._encrypted_fields:
                raise InvalidQueryError(
                    f"Encrypted field '{field_name}' cannot be used in query criteria.",
                    operation="query_validation",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_name,
                    details={"encrypted": True},
                )
            if operator not in VALID_OPERATORS:
                raise InvalidQueryError(
                    f"Unknown query operator '{operator}' for field '{field_name}'.",
                    operation="query_validation",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_name,
                    details={"operator": operator},
                )
            if not allow_operators and operator != "eq":
                raise InvalidQueryError(
                    f"Update field '{field_expr}' is invalid. "
                    "Use plain field names without query operators.",
                    operation="query_validation",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_expr,
                    details={"operator_style_updates_not_allowed": True},
                )
            normalized_fields.append((field_name, operator, value))

        if unknown:
            raise InvalidQueryError(
                f"Unknown field(s) {unknown!r} on model '{self.model_cls.__name__}'.",
                operation="query_validation",
                model=self.model_cls.__name__,
                table=self.table_name,
                details={"unknown_fields": unknown},
            )

        for field_name, operator, value in normalized_fields:
            # Validators are pre-built per field at registration. The previous
            # implementation constructed a fresh TypeAdapter here on every call,
            # which measured ~60x slower than a cached validator.
            validate = self._specs[field_name].validate
            try:
                if operator == "is_null":
                    _validate_bool_flag(value)
                elif operator == "between":
                    if not isinstance(value, (list, tuple)) or len(value) != 2:
                        raise InvalidQueryError(
                            f"Field '{field_name}__between' requires a two-item tuple or list.",
                            operation="query_validation",
                            model=self.model_cls.__name__,
                            table=self.table_name,
                            field=field_name,
                            details={"operator": operator},
                        )
                    validate(value[0])
                    validate(value[1])
                elif operator in {"in", "not_in"}:
                    if not is_iterable_value(value):
                        raise InvalidQueryError(
                            f"Field '{field_name}__{operator}' requires an iterable of values.",
                            operation="query_validation",
                            model=self.model_cls.__name__,
                            table=self.table_name,
                            field=field_name,
                            details={"operator": operator},
                        )
                    for item in value:
                        validate(item)
                elif operator == "eq":
                    if is_iterable_value(value):
                        raise InvalidQueryError(
                            f"Field '{field_name}' does not accept iterable equality values. "
                            f"Use '{field_name}__in' for membership filters.",
                            operation="query_validation",
                            model=self.model_cls.__name__,
                            table=self.table_name,
                            field=field_name,
                            details={"operator": operator},
                        )
                    validate(value)
                elif operator in _STRING_MATCH_OPERATORS:
                    # like/ilike/contains/startswith/endswith take patterns, not
                    # field-typed values: 'ali%' is not a valid email, but it is a
                    # valid filter for one.
                    if not isinstance(value, (str, bytes)):
                        raise InvalidQueryError(
                            f"Field '{field_name}__{operator}' requires a string pattern.",
                            operation="query_validation",
                            model=self.model_cls.__name__,
                            table=self.table_name,
                            field=field_name,
                            details={"operator": operator},
                        )
                else:
                    validate(value)
            except FieldCoercionError as exc:
                raise InvalidQueryError(
                    f"Invalid value for field '{field_name}' on model "
                    f"'{self.model_cls.__name__}': {value!r}",
                    operation="query_validation",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_name,
                    details={"operator": operator, "value": value},
                ) from exc

    def _normalize_criteria_for_db(self, criteria: Mapping[str, Any]) -> dict[str, Any]:
        """
        Apply the primary key's DB codec to key-field criteria.

        Scoped deliberately to the key field rather than applied to every column.
        The only non-identity codecs are the binary-UUID key and encryption, and
        encrypted fields are already rejected as query criteria — applying
        ``to_db`` broadly here would double-encrypt values that reach this method
        already normalized (see ``_upsert_on_unique_fields``).
        """
        key_spec = self._specs.get(self.key_field)
        if key_spec is None or key_spec.to_db is _spec_identity:
            return dict(criteria)

        to_db = key_spec.to_db
        normalized: dict[str, Any] = {}
        for field_expr, value in criteria.items():
            field_name, operator = split_field_expr(field_expr)
            if field_name != self.key_field:
                normalized[field_expr] = value
            elif operator in {"in", "not_in"} and is_iterable_value(value):
                normalized[field_expr] = [to_db(item) for item in value]
            elif operator == "between" and isinstance(value, (list, tuple)):
                normalized[field_expr] = [to_db(item) for item in value]
            else:
                normalized[field_expr] = to_db(value)
        return normalized

