"""
``DatabaseRegistry`` — the persistence manager attached to a model as
``Model.objects``.

Design: Manager pattern
-----------------------
All persistence operations live on the ``DatabaseRegistry`` instance, not
on the model class.  The decorator attaches the registry as
``Model.objects`` (or a custom ``manager_attr``).  Instance-level helpers
(``save``, ``delete``, ``refresh``) are injected as instance methods via a
thin mixin, keeping the model class itself clean.

Thread safety
-------------
* Engines are shared and pooled — see ``engine.py``.
* Every operation opens a fresh connection from the pool and runs inside an
  ``engine.begin()`` context (auto-commit on success, rollback on failure).
* ``update_where`` updates and re-fetches in the same connection, avoiding
  a separate read-then-write TOCTOU window.

SQLite specifics
----------------
* Upsert uses dialect-aware conflict handling when supported.
* Unsupported dialects fall back to a transactional read-then-write path.

Date / datetime handling
------------------------
We use ``model_dump()`` without ``mode='json'`` so Python date/datetime
objects are preserved as native types.  SQLAlchemy maps them correctly to
the underlying column type.  JSON-typed columns receive Python dicts/lists
directly, which SQLAlchemy serialises appropriately.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import logging
from pathlib import Path
from typing import Any, Generator, Generic, Mapping, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import Column, MetaData, Table, delete, func, inspect, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from decorates.db.engine import dialect_insert, dispose_engine, get_engine
from decorates.db.exceptions import (
    DuplicateKeyError,
    ImmutableFieldError,
    InvalidPrimaryKeyAssignmentError,
    InvalidQueryError,
    MigrationError,
    RecordNotFoundError,
    SchemaError,
    UniqueConstraintError,
)
from decorates.db.metadata import RegistryConfig
from decorates.db.operators import VALID_OPERATORS, is_iterable_value, parse_criterion, split_field_expr
from decorates.db.schema import SchemaManager
from decorates.db.security import hash_password, is_password_hash
from decorates.db.typing_utils import (
    default_database_url,
    default_table_name,
    field_allows_none,
    normalize_database_url,
    sqlalchemy_type_for_annotation,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
_ORIGINAL_KEY_ATTR = "__decorates_original_key__"
_PASSWORD_FIELD = "password"
logger = logging.getLogger(__name__)


class DatabaseRegistry(Generic[ModelT]):
    """
    SQLite-backed (and SQLAlchemy-compatible) persistence manager for a
    Pydantic model class.

    Attach to a model with the ``@database_registry`` decorator::

        @database_registry("app.db", table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        # All CRUD lives on the manager, not the model class
        user = User.objects.create(name="Alice")
        users = User.objects.filter(name="Alice")
        user.save()      # instance method injected by decorator
        user.delete()    # instance method injected by decorator

    You can also use the registry directly without the decorator::

        registry = DatabaseRegistry(User, "app.db", table_name="users")
        user = registry.create(name="Bob")
    """

    def __init__(
        self,
        model_cls: type[ModelT],
        database_url: str | Path | None = None,
        *,
        table_name: str | None = None,
        key_field: str = "id",
        auto_create: bool = True,
        autoincrement: bool = False,
        unique_fields: tuple[str, ...] | list[str] = (),
        manager_attr: str = "objects",
    ) -> None:
        normalized_url = normalize_database_url(
            database_url or default_database_url(model_cls.__name__)
        )
        resolved_table = table_name or default_table_name(model_cls.__name__)

        self.config = RegistryConfig.build(
            model_cls,
            database_url=normalized_url,
            table_name=resolved_table,
            key_field=key_field,
            manager_attr=manager_attr,
            auto_create=auto_create,
            autoincrement=autoincrement,
            unique_fields=tuple(unique_fields),
        )

        self.model_cls = model_cls
        self.key_field = self.config.key_field
        self.table_name = self.config.table_name
        self.database_url = self.config.database_url

        self._metadata = MetaData()
        self._engine = get_engine(self.database_url)
        self._table = self._build_table()
        self._schema = SchemaManager(self._engine, self._table, self.table_name)

        if auto_create:
            self._schema.create_schema()

    # ------------------------------------------------------------------
    # Public schema surface (delegate to SchemaManager)
    # ------------------------------------------------------------------

    def create_schema(self) -> None:
        """CREATE TABLE IF NOT EXISTS — idempotent."""
        self._schema.create_schema()

    def drop_schema(self) -> None:
        """DROP TABLE — irreversible."""
        self._schema.drop_schema()

    def schema_exists(self) -> bool:
        """Return True when the backing table exists in the database."""
        return self._schema.schema_exists()

    def truncate(self) -> None:
        """Delete all rows without touching the schema."""
        self._schema.truncate()

    def add_column(self, column_name: str, annotation: Any, *, nullable: bool = True) -> None:
        """Add a column to the live table (non-destructive)."""
        self._schema.add_column(column_name, annotation, nullable=nullable)

    def ensure_column(self, column_name: str, annotation: Any, *, nullable: bool = True) -> bool:
        """Add a column only if it doesn't already exist. Returns True if added."""
        return self._schema.ensure_column(column_name, annotation, nullable=nullable)

    def rename_table(self, new_name: str) -> None:
        """
        Rename the backing table and atomically refresh table-bound state.

        Either the rename fully succeeds (DDL + in-memory rebinding), or the
        registry remains bound to the original table.
        """
        target_name = new_name.strip()
        if not target_name:
            raise MigrationError(
                "rename_table() requires a non-empty target table name.",
                operation="rename_table",
                model=self.model_cls.__name__,
                table=self.table_name,
            )

        previous_name = self.table_name
        if target_name == previous_name:
            return

        inspector = inspect(self._engine)
        if inspector.has_table(target_name):
            logger.warning(
                "rename_table rejected for model='%s' table='%s' target='%s' because target exists.",
                self.model_cls.__name__,
                previous_name,
                target_name,
            )
            raise MigrationError(
                f"Cannot rename '{previous_name}' to '{target_name}': target table already exists.",
                operation="rename_table",
                model=self.model_cls.__name__,
                table=previous_name,
                details={"target_table": target_name},
            )

        previous_state = self._capture_table_state()

        try:
            self._schema.rename_table(target_name)
        except SchemaError as exc:
            logger.exception(
                "DDL rename failed for model='%s' table='%s' target='%s'.",
                self.model_cls.__name__,
                previous_name,
                target_name,
            )
            raise MigrationError(
                f"Failed to rename '{previous_name}' to '{target_name}'.",
                operation="rename_table",
                model=self.model_cls.__name__,
                table=previous_name,
                details={"target_table": target_name},
            ) from exc

        try:
            self._rebind_table_state(target_name)
            if not self.schema_exists():
                raise MigrationError(
                    f"State refresh failed after renaming '{previous_name}' to '{target_name}'.",
                    operation="rename_table",
                    model=self.model_cls.__name__,
                    table=target_name,
                    details={"previous_table": previous_name},
                )
            logger.info(
                "rename_table completed for model='%s' old='%s' new='%s'.",
                self.model_cls.__name__,
                previous_name,
                target_name,
            )
        except Exception as exc:
            rollback_error: Exception | None = None

            try:
                rollback_schema = SchemaManager(
                    self._engine,
                    previous_state["table"],
                    target_name,
                )
                rollback_schema.rename_table(previous_name)
            except Exception as rollback_exc:  # pragma: no cover - hard to force deterministically
                rollback_error = rollback_exc
            finally:
                self._restore_table_state(previous_state)

            if rollback_error is not None:
                logger.exception(
                    "rename_table rollback failed for model='%s' old='%s' new='%s'.",
                    self.model_cls.__name__,
                    previous_name,
                    target_name,
                )
                raise MigrationError(
                    f"Rename '{previous_name}' -> '{target_name}' failed and rollback did not complete.",
                    operation="rename_table",
                    model=self.model_cls.__name__,
                    table=target_name,
                    details={"rollback_target": previous_name},
                ) from rollback_error

            logger.exception(
                "rename_table state transition failed and was rolled back for model='%s' old='%s' new='%s'.",
                self.model_cls.__name__,
                previous_name,
                target_name,
            )
            raise MigrationError(
                f"Rename '{previous_name}' -> '{target_name}' did not complete state transition.",
                operation="rename_table",
                model=self.model_cls.__name__,
                table=target_name,
                details={"previous_table": previous_name},
            ) from exc

    def column_names(self) -> list[str]:
        """Return current column names from live DB inspection."""
        return self._schema.column_names()

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Generator[Connection, None, None]:
        """
        Explicit transaction context manager for batching operations atomically::

            with User.objects.transaction() as conn:
                User.objects.create(name="Alice")
                Post.objects.create(author_id=1, title="Hello")
        """
        with self._engine.begin() as conn:
            yield conn

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create(self, **data: Any) -> ModelT:
        """
        Strict INSERT.  Raises on duplicate primary key or unique violation.

        Use this when you explicitly want an error if the record already exists.
        """
        instance = self.model_cls(**data)
        try:
            with self._engine.begin() as conn:
                return self._create_with_conn(conn, instance)
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("create", exc)

    def strict_create(self, **data: Any) -> ModelT:
        """Alias for ``create()`` for callers that prefer explicit wording."""
        return self.create(**data)

    def upsert(self, instance: ModelT | None = None, /, **data: Any) -> ModelT:
        """
        INSERT-or-UPDATE by primary key.

        When *autoincrement* is enabled and no primary key is supplied, this
        falls back to a plain ``create()`` so the database generates the ID.

        Atomic: uses ``INSERT … ON CONFLICT DO UPDATE`` — no separate SELECT
        pre-check, eliminating read-then-write race conditions.
        """
        target = instance if instance is not None else self.model_cls(**data)
        try:
            with self._engine.begin() as conn:
                return self._upsert_with_conn(conn, target)
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("upsert", exc)

    def save(self, instance: ModelT) -> ModelT:
        """
        Persist *instance* using upsert semantics.

        Policy: an existing row (matched by primary key) is updated; a new
        row is inserted.  The primary key determines which path is taken.
        """
        return self.upsert(instance)

    def update_where(self, criteria: Mapping[str, Any], **updates: Any) -> list[ModelT]:
        """
        Update all rows matching *criteria* and return the refreshed records.

        Both *criteria* and *updates* are validated against known model fields
        before any SQL is issued.
        """
        if not criteria:
            raise InvalidQueryError(
                "update_where() requires at least one filter criterion.",
                operation="update_where",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        if not updates:
            raise InvalidQueryError(
                "update_where() requires at least one field to update.",
                operation="update_where",
                model=self.model_cls.__name__,
                table=self.table_name,
            )

        self._assert_known_fields(criteria)
        self._assert_known_fields(updates)
        updates = self._normalize_write_mapping(updates)

        try:
            with self._engine.begin() as conn:
                stmt = update(self._table).values(**updates)
                stmt = self._apply_where(stmt, criteria)

                if getattr(conn.dialect, "update_returning", False):
                    rows = conn.execute(stmt.returning(self._table)).mappings().all()
                    return [self._row_to_model(row) for row in rows]

                # Fallback for dialects without UPDATE ... RETURNING support.
                key_column = self._table.c[self.key_field]
                key_stmt = select(key_column)
                key_stmt = self._apply_where(key_stmt, criteria)
                affected_keys = conn.execute(key_stmt).scalars().all()
                if not affected_keys:
                    return []

                conn.execute(stmt)
                refresh_stmt = select(self._table).where(key_column.in_(affected_keys))
                rows = conn.execute(refresh_stmt).mappings().all()
                return [self._row_to_model(row) for row in rows]
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("update_where", exc)

    def delete(self, key_value: Any) -> bool:
        """Delete the row with the given primary key. Returns True if deleted."""
        return self.delete_where(**{self.key_field: key_value}) > 0

    def delete_where(self, **criteria: Any) -> int:
        """Delete all rows matching *criteria*. Returns the deleted row count."""
        if not criteria:
            raise InvalidQueryError(
                "delete_where() requires at least one filter criterion.",
                operation="delete_where",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        self._assert_known_fields(criteria)

        stmt = delete(self._table)
        stmt = self._apply_where(stmt, criteria)
        try:
            with self._engine.begin() as conn:
                result = conn.execute(stmt)
            return result.rowcount or 0
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("delete_where", exc)

    def bulk_create(self, records: list[Mapping[str, Any]]) -> list[ModelT]:
        """Create multiple records atomically and return stamped models."""
        if not records:
            return []

        instances = [self.model_cls(**record) for record in records]
        values_list = [self._prepare_insert_values(instance) for instance in instances]

        try:
            with self._engine.begin() as conn:
                supports_insert_returning = bool(
                    getattr(conn.dialect, "insert_returning", False)
                    and getattr(conn.dialect, "insert_executemany_returning", False)
                )
                if supports_insert_returning:
                    stmt = self._table.insert().returning(self._table)
                    rows = conn.execute(stmt, values_list).mappings().all()
                    return [self._row_to_model(row) for row in rows]
                return [self._create_with_conn(conn, instance) for instance in instances]
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("bulk_create", exc)

    def bulk_upsert(self, records: list[Mapping[str, Any]]) -> list[ModelT]:
        """Upsert multiple records atomically and return stamped models."""
        if not records:
            return []

        targets = [self.model_cls(**record) for record in records]
        persisted: list[ModelT] = []

        try:
            with self._engine.begin() as conn:
                for target in targets:
                    persisted.append(self._upsert_with_conn(conn, target))
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("bulk_upsert", exc)

        return persisted

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, *args: Any, **criteria: Any) -> ModelT | None:
        """
        Return the first matching row, or None.

        Accepts a single positional primary-key value::

            User.objects.get(1)

        Or keyword criteria::

            User.objects.get(email="alice@example.com")
        """
        normalized = self._normalize_lookup(args, criteria)
        rows = self.filter(limit=1, **normalized)
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
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | list[str] | tuple[str, ...] | None = None,
        **criteria: Any,
    ) -> list[ModelT]:
        """
        Return all rows matching *criteria*.

        Supports optional *limit* and *offset* for pagination plus
        ``order_by`` using ``field`` / ``-field`` syntax.
        """
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(self._table)
        stmt = self._apply_where(stmt, criteria)
        if order_by is not None:
            stmt = self._apply_order_by(stmt, order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)

        try:
            with self._engine.begin() as conn:
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
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(func.count()).select_from(self._table)
        stmt = self._apply_where(stmt, criteria)
        try:
            with self._engine.begin() as conn:
                return (conn.execute(stmt).scalar_one() or 0) > 0
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("exists", exc)

    def count(self, **criteria: Any) -> int:
        """Return the number of rows matching *criteria* (or all rows if empty)."""
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(func.count()).select_from(self._table)
        stmt = self._apply_where(stmt, criteria)
        try:
            with self._engine.begin() as conn:
                return conn.execute(stmt).scalar_one() or 0
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("count", exc)

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def dispose(self) -> None:
        """
        Dispose the connection pool for this registry's database URL.

        After calling this, the registry is no longer usable.  Wire into the
        FastAPI ``lifespan`` shutdown hook for clean application teardown.
        """
        dispose_engine(self.database_url)

    # ------------------------------------------------------------------
    # Private: table construction
    # ------------------------------------------------------------------

    def _build_table(self) -> Table:
        unique_set = set(self.config.unique_fields)
        columns: list[Column[Any]] = []

        for field_name, field_info in self.model_cls.model_fields.items():
            sa_type = sqlalchemy_type_for_annotation(field_info.annotation)
            nullable = self._column_nullable(field_name, field_info)
            is_pk = field_name == self.key_field

            col_kwargs: dict[str, Any] = {
                "primary_key": is_pk,
                "nullable": nullable,
                "unique": field_name in unique_set,
            }
            if is_pk and self.config.autoincrement:
                col_kwargs["autoincrement"] = True

            columns.append(Column(field_name, sa_type, **col_kwargs))

        table_kwargs: dict[str, Any] = {}
        if self.config.autoincrement and self.database_url.startswith("sqlite"):
            table_kwargs["sqlite_autoincrement"] = True

        return Table(self.table_name, self._metadata, *columns, **table_kwargs)

    def _capture_table_state(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "table_name": self.table_name,
            "metadata": self._metadata,
            "table": self._table,
            "schema": self._schema,
        }

    def _restore_table_state(self, state: Mapping[str, Any]) -> None:
        self.config = state["config"]
        self.table_name = state["table_name"]
        self._metadata = state["metadata"]
        self._table = state["table"]
        self._schema = state["schema"]

    def _rebind_table_state(self, table_name: str) -> None:
        """
        Rebuild all state derived from the active table name.

        This is the single refresh path used after schema mutations that
        change table identity.
        """
        self.config = replace(self.config, table_name=table_name)
        self.table_name = table_name
        self._metadata = MetaData()
        self._table = self._build_table()
        self._schema = SchemaManager(self._engine, self._table, self.table_name)

    def _column_nullable(self, field_name: str, field_info: Any) -> bool:
        # The PK column for autoincrement must be nullable so INSERTs can
        # omit it and let the database generate it.
        if field_name == self.key_field and self.config.autoincrement:
            return True
        return field_allows_none(field_info)

    # ------------------------------------------------------------------
    # Private: query helpers
    # ------------------------------------------------------------------

    def _apply_where(self, stmt: Any, criteria: Mapping[str, Any]) -> Any:
        for field_expr, value in criteria.items():
            stmt = stmt.where(parse_criterion(self._table, field_expr, value))
        return stmt

    def _apply_order_by(
        self,
        stmt: Any,
        order_by: str | list[str] | tuple[str, ...],
    ) -> Any:
        fields = [order_by] if isinstance(order_by, str) else list(order_by)
        for field in fields:
            descending = field.startswith("-")
            field_name = field[1:] if descending else field
            if field_name not in self.model_cls.model_fields:
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

    def _assert_known_fields(self, fields: Any) -> None:
        model_fields = self.model_cls.model_fields
        unknown: list[str] = []
        normalized_fields: list[tuple[str, str, Any]] = []

        for field_expr, value in fields.items():
            field_name, operator = split_field_expr(field_expr)
            if field_name not in model_fields:
                unknown.append(field_name)
                continue
            if operator not in VALID_OPERATORS:
                raise InvalidQueryError(
                    f"Unknown query operator '{operator}' for field '{field_name}'.",
                    operation="query_validation",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_name,
                    details={"operator": operator},
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
            try:
                adapter = TypeAdapter(model_fields[field_name].annotation)
                if operator == "is_null":
                    TypeAdapter(bool).validate_python(value)
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
                    adapter.validate_python(value[0])
                    adapter.validate_python(value[1])
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
                        adapter.validate_python(item)
                else:
                    adapter.validate_python(value)
            except ValidationError as exc:
                raise InvalidQueryError(
                    f"Invalid value for field '{field_name}' on model "
                    f"'{self.model_cls.__name__}': {value!r}",
                    operation="query_validation",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=field_name,
                    details={"operator": operator, "value": value},
                ) from exc

    # ------------------------------------------------------------------
    # Private: row ↔ model conversion
    # ------------------------------------------------------------------

    def _row_to_model(self, row: Mapping[str, Any]) -> ModelT:
        return self._stamp_identity(self.model_cls.model_validate(dict(row)))

    def _model_to_row(self, model: ModelT) -> dict[str, Any]:
        # Use plain model_dump() (no mode='json') so Python date/datetime/Decimal
        # objects are preserved as native types.  SQLAlchemy's column types handle
        # the DB-level serialisation correctly.
        self._normalize_model_for_write(model)
        return model.model_dump()

    def _prepare_insert_values(self, model: ModelT) -> dict[str, Any]:
        values = self._model_to_row(model)
        # Strip None autoincrement key so the DB generates it
        if self.config.autoincrement and values.get(self.key_field) is None:
            values.pop(self.key_field, None)
        return values

    def _create_with_conn(self, conn: Connection, instance: ModelT) -> ModelT:
        self._reject_explicit_autoincrement_key(instance)
        values = self._prepare_insert_values(instance)
        stmt = self._table.insert().values(**values)
        result = conn.execute(stmt)
        return self._apply_generated_key(instance, result)

    def _apply_generated_key(self, instance: ModelT, result: Any) -> ModelT:
        if self.config.autoincrement and getattr(instance, self.key_field, None) is None:
            pks = list(result.inserted_primary_key or ())
            if pks:
                instance = instance.model_copy(update={self.key_field: pks[0]})
        return self._stamp_identity(instance)

    def _reject_explicit_autoincrement_key(self, instance: ModelT) -> None:
        if not self.config.autoincrement:
            return

        key_value = getattr(instance, self.key_field, None)
        if key_value is not None:
            raise InvalidPrimaryKeyAssignmentError(
                f"Cannot explicitly assign '{self.model_cls.__name__}.{self.key_field}' "
                "when the primary key is database-managed.",
                operation="create",
                model=self.model_cls.__name__,
                table=self.table_name,
                field=self.key_field,
            )

    def _assert_immutable_key(self, instance: ModelT) -> None:
        original = getattr(instance, _ORIGINAL_KEY_ATTR, None)
        current = getattr(instance, self.key_field, None)
        if original is not None and current != original:
            raise ImmutableFieldError(
                f"Field '{self.model_cls.__name__}.{self.key_field}' is immutable once "
                "the record has been persisted.",
                operation="save",
                model=self.model_cls.__name__,
                table=self.table_name,
                field=self.key_field,
                details={"original": original, "current": current},
            )

    def _stamp_identity(self, instance: ModelT) -> ModelT:
        object.__setattr__(instance, _ORIGINAL_KEY_ATTR, getattr(instance, self.key_field, None))
        return instance

    def _upsert_with_conn(self, conn: Connection, target: ModelT) -> ModelT:
        self._assert_immutable_key(target)
        values = self._model_to_row(target)
        key_value = values.get(self.key_field)

        if self.config.autoincrement and key_value is None:
            if self.config.unique_fields:
                return self._upsert_on_unique_fields(conn, target, values)
            return self._create_with_conn(conn, target)

        key_value = self._execute_upsert(conn, values, [self.key_field])
        if key_value is not None and getattr(target, self.key_field, None) is None:
            object.__setattr__(target, self.key_field, key_value)
        return self._stamp_identity(target)

    def _upsert_on_unique_fields(
        self,
        conn: Connection,
        target: ModelT,
        values: dict[str, Any],
    ) -> ModelT:
        insert_values = dict(values)
        insert_values.pop(self.key_field, None)

        key_value = self._execute_upsert(conn, insert_values, self.config.unique_fields)
        lookup = {field: insert_values[field] for field in self.config.unique_fields}
        refreshed = self._row_from_connection(conn, **lookup)
        if refreshed is None:
            refreshed = self.require(**lookup)

        for field_name in type(target).model_fields:
            object.__setattr__(target, field_name, getattr(refreshed, field_name))
        if key_value is not None and getattr(target, self.key_field, None) is None:
            object.__setattr__(target, self.key_field, key_value)
        return self._stamp_identity(target)

    def _execute_upsert(
        self,
        conn: Connection,
        values: dict[str, Any],
        conflict_fields: tuple[str, ...] | list[str],
    ) -> Any:
        stmt = self._build_upsert_statement(values, conflict_fields)
        if stmt is not None:
            result = conn.execute(stmt)
            pks = list(result.inserted_primary_key or ())
            if pks:
                return pks[0]
            return values.get(self.key_field)
        return self._upsert_fallback_with_conn(conn, values, conflict_fields)

    def _build_upsert_statement(
        self,
        values: dict[str, Any],
        conflict_fields: tuple[str, ...] | list[str],
    ) -> Any:
        insert_stmt = dialect_insert(self._engine, self._table)
        if insert_stmt is None:
            return None

        update_cols = {key: value for key, value in values.items() if key != self.key_field}
        stmt = insert_stmt.values(**values)
        dialect_name = self._engine.dialect.name

        if dialect_name in {"mysql", "mariadb"}:
            return stmt.on_duplicate_key_update(**update_cols)
        return stmt.on_conflict_do_update(
            index_elements=list(conflict_fields),
            set_=update_cols,
        )

    def _upsert_fallback_with_conn(
        self,
        conn: Connection,
        values: dict[str, Any],
        conflict_fields: tuple[str, ...] | list[str],
    ) -> Any:
        lookup = {
            field: values[field]
            for field in conflict_fields
            if field in values and values[field] is not None
        }
        existing = self._row_from_connection(conn, lock_for_update=True, **lookup) if lookup else None
        if existing is not None:
            updates = {key: value for key, value in values.items() if key != self.key_field}
            if updates:
                stmt = (
                    update(self._table)
                    .where(self._table.c[self.key_field] == getattr(existing, self.key_field))
                    .values(**updates)
                )
                conn.execute(stmt)
            return getattr(existing, self.key_field)

        insert_values = dict(values)
        if self.config.autoincrement and insert_values.get(self.key_field) is None:
            insert_values.pop(self.key_field, None)
        result = conn.execute(self._table.insert().values(**insert_values))
        pks = list(result.inserted_primary_key or ())
        if pks:
            return pks[0]
        return insert_values.get(self.key_field)

    def _row_from_connection(
        self,
        conn: Connection,
        *,
        lock_for_update: bool = False,
        **criteria: Any,
    ) -> ModelT | None:
        stmt = select(self._table).limit(1)
        stmt = self._apply_where(stmt, criteria)
        if lock_for_update:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).mappings().first()
        return self._row_to_model(row) if row is not None else None

    def _normalize_model_for_write(self, model: ModelT) -> None:
        if _PASSWORD_FIELD not in type(model).model_fields:
            return

        password = getattr(model, _PASSWORD_FIELD, None)
        if isinstance(password, str) and password and not is_password_hash(password):
            object.__setattr__(model, _PASSWORD_FIELD, hash_password(password))

    def _normalize_write_mapping(self, values: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(values)
        password = normalized.get(_PASSWORD_FIELD)
        if isinstance(password, str) and password and not is_password_hash(password):
            normalized[_PASSWORD_FIELD] = hash_password(password)
        return normalized

    # ------------------------------------------------------------------
    # Private: error classification
    # ------------------------------------------------------------------

    def _raise_sqlalchemy_error(self, operation: str, exc: SQLAlchemyError) -> None:
        logger.exception(
            "Database operation '%s' failed for model='%s' table='%s'.",
            operation,
            self.model_cls.__name__,
            self.table_name,
        )
        raise SchemaError(
            f"Database operation '{operation}' failed for '{self.model_cls.__name__}' "
            f"on table '{self.table_name}'.",
            operation=operation,
            model=self.model_cls.__name__,
            table=self.table_name,
            details={"driver_error": str(exc)},
        ) from exc

    def _classify_integrity_error(self, exc: IntegrityError) -> Exception:
        msg = str(exc.orig).lower()
        key_marker = f".{self.key_field}".lower()

        if "unique constraint failed" in msg or "duplicate" in msg:
            if key_marker in msg:
                logger.warning(
                    "Primary-key integrity violation model='%s' key_field='%s' table='%s'.",
                    self.model_cls.__name__,
                    self.key_field,
                    self.table_name,
                    exc_info=True,
                )
                return DuplicateKeyError(
                    f"Duplicate primary key for '{self.model_cls.__name__}.{self.key_field}'.",
                    operation="write",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.key_field,
                )
            logger.warning(
                "Unique-constraint integrity violation model='%s' table='%s'.",
                self.model_cls.__name__,
                self.table_name,
                exc_info=True,
            )
            return UniqueConstraintError(
                f"Unique constraint violated on '{self.model_cls.__name__}'.",
                operation="write",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        logger.exception(
            "Unhandled integrity error model='%s' table='%s'.",
            self.model_cls.__name__,
            self.table_name,
        )
        return SchemaError(
            f"Database integrity error on table '{self.table_name}': {exc.orig}",
            operation="write",
            model=self.model_cls.__name__,
            table=self.table_name,
            details={"driver_error": str(exc.orig)},
        )

    def __repr__(self) -> str:
        return (
            f"DatabaseRegistry("
            f"model={self.model_cls.__name__!r}, "
            f"table={self.table_name!r}, "
            f"url={self.database_url!r})"
        )
