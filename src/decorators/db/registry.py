"""
db_registry.registry
~~~~~~~~~~~~~~~~~~~~
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
* Upsert uses ``INSERT … ON CONFLICT DO UPDATE`` (SQLite dialect).
* When used with PostgreSQL, SQLAlchemy's dialect layer handles translation.

Date / datetime handling
------------------------
We use ``model_dump()`` without ``mode='json'`` so Python date/datetime
objects are preserved as native types.  SQLAlchemy maps them correctly to
the underlying column type.  JSON-typed columns receive Python dicts/lists
directly, which SQLAlchemy serialises appropriately.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Generic, Mapping, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import Column, MetaData, Table, delete, func, inspect, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from decorators.db.engine import dispose_engine, get_engine
from decorators.db.exceptions import (
    DuplicateKeyError,
    ImmutableFieldError,
    InvalidPrimaryKeyAssignmentError,
    InvalidQueryError,
    RecordNotFoundError,
    SchemaError,
    UniqueConstraintError,
)
from decorators.db.metadata import RegistryConfig
from decorators.db.schema import SchemaManager
from decorators.db.security import hash_password, is_password_hash
from decorators.db.typing_utils import (
    default_database_url,
    default_table_name,
    field_allows_none,
    normalize_database_url,
    sqlalchemy_type_for_annotation,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
_ORIGINAL_KEY_ATTR = "__decorators_original_key__"
_PASSWORD_FIELD = "password"


class DatabaseRegistry(Generic[ModelT]):
    """
    SQLite-backed (and SQLAlchemy-compatible) persistence manager for a
    Pydantic model class.

    Attach to a model with the ``@database_manager`` decorator::

        @database_manager("app.db", table_name="users", key_field="id")
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
        self._reject_explicit_autoincrement_key(instance)
        values = self._prepare_insert_values(instance)
        stmt = self._table.insert().values(**values)

        try:
            with self._engine.begin() as conn:
                result = conn.execute(stmt)
            return self._apply_generated_key(instance, result)
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc

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
        self._assert_immutable_key(target)
        values = self._model_to_row(target)
        key_value = values.get(self.key_field)

        if self.config.autoincrement and key_value is None:
            if self.config.unique_fields:
                return self._upsert_on_unique_fields(target, values)
            return self.create(**target.model_dump())

        stmt = sqlite_insert(self._table).values(**values)
        update_cols = {k: values[k] for k in values if k != self.key_field}
        stmt = stmt.on_conflict_do_update(
            index_elements=[self.key_field],
            set_=update_cols,
        )

        try:
            with self._engine.begin() as conn:
                conn.execute(stmt)
            return self._stamp_identity(target)
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc

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
            raise InvalidQueryError("update_where() requires at least one filter criterion.")
        if not updates:
            raise InvalidQueryError("update_where() requires at least one field to update.")

        self._assert_known_fields(criteria)
        self._assert_known_fields(updates)
        updates = self._normalize_write_mapping(updates)

        stmt = update(self._table).values(**updates)
        stmt = self._apply_where(stmt, criteria)

        # `update_where` re-fetch is logically broken when the criteria field 
        # is the one being updated, because the merged criteria+updates would 
        # look for rows matching the new value, not the old one.The re-fetch 
        # should use the key field of the affected rows, not the merged criteria. 
        # The fix is to collect PKs before updating, then re-fetch by PK after.
        affected_keys = [
            getattr(r, self.key_field) for r in self.filter(**criteria)
        ]

        try:
            with self._engine.begin() as conn:
                conn.execute(stmt)
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc

        # Re-fetch with merged criteria+updates to locate the modified rows
        # return self.filter(**{**dict(criteria), **updates})
        return [self.require(key) for key in affected_keys]

    def delete(self, key_value: Any) -> bool:
        """Delete the row with the given primary key. Returns True if deleted."""
        return self.delete_where(**{self.key_field: key_value}) > 0

    def delete_where(self, **criteria: Any) -> int:
        """Delete all rows matching *criteria*. Returns the deleted row count."""
        if not criteria:
            raise InvalidQueryError("delete_where() requires at least one filter criterion.")
        self._assert_known_fields(criteria)

        stmt = delete(self._table)
        stmt = self._apply_where(stmt, criteria)
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        return result.rowcount or 0

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
                f"No {self.model_cls.__name__} found matching {normalized!r}."
            )
        return record

    def filter(self, limit: int | None = None, offset: int | None = None, **criteria: Any) -> list[ModelT]:
        """
        Return all rows matching *criteria*.

        Supports optional *limit* and *offset* for pagination.
        """
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(self._table)
        stmt = self._apply_where(stmt, criteria)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)

        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_model(row) for row in rows]

    def all(self) -> list[ModelT]:
        """Return every row as validated Pydantic models."""
        return self.filter()

    def get_all(self) -> list[ModelT]:
        """Alias for ``all()``."""
        return self.all()

    def exists(self, **criteria: Any) -> bool:
        """Return True when at least one row matches *criteria*."""
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(func.count()).select_from(self._table)
        stmt = self._apply_where(stmt, criteria)
        with self._engine.begin() as conn:
            return (conn.execute(stmt).scalar_one() or 0) > 0

    def count(self, **criteria: Any) -> int:
        """Return the number of rows matching *criteria* (or all rows if empty)."""
        if criteria:
            self._assert_known_fields(criteria)

        stmt = select(func.count()).select_from(self._table)
        stmt = self._apply_where(stmt, criteria)
        with self._engine.begin() as conn:
            return conn.execute(stmt).scalar_one() or 0

    def first(self, **criteria: Any) -> ModelT | None:
        """Return the first row by insertion order, optionally filtered."""
        rows = self.filter(limit=1, **criteria)
        return rows[0] if rows else None

    def last(self, **criteria: Any) -> ModelT | None:
        """Return the last row ordered by primary key descending."""
        if criteria:
            self._assert_known_fields(criteria)

        stmt = (
            select(self._table)
            .order_by(self._table.c[self.key_field].desc())
            .limit(1)
        )
        stmt = self._apply_where(stmt, criteria)
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return self._row_to_model(rows[0]) if rows else None

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
        for field, value in criteria.items():
            if isinstance(value, (list, tuple, set, frozenset)):
                stmt = stmt.where(self._table.c[field].in_(list(value)))
            else:
                stmt = stmt.where(self._table.c[field] == value)
        return stmt

    def _normalize_lookup(self, args: tuple[Any, ...], criteria: Mapping[str, Any]) -> dict[str, Any]:
        if args and criteria:
            raise InvalidQueryError(
                "Pass either a positional primary-key or keyword criteria, not both."
            )
        if len(args) > 1:
            raise InvalidQueryError("Only one positional lookup argument is supported.")
        return {self.key_field: args[0]} if args else dict(criteria)

    def _assert_known_fields(self, fields: Any) -> None:
        model_fields = self.model_cls.model_fields
        unknown = [f for f in fields if f not in model_fields]
        if unknown:
            raise InvalidQueryError(
                f"Unknown field(s) {unknown!r} on model '{self.model_cls.__name__}'."
            )

        for field_name, value in fields.items():
            try:
                adapter = TypeAdapter(model_fields[field_name].annotation)
                if isinstance(value, (list, tuple, set, frozenset)):
                    for item in value:
                        adapter.validate_python(item)
                else:
                    adapter.validate_python(value)
            except ValidationError as exc:
                raise InvalidQueryError(
                    f"Invalid value for field '{field_name}' on model "
                    f"'{self.model_cls.__name__}': {value!r}"
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
                "when the primary key is database-managed."
            )

    def _assert_immutable_key(self, instance: ModelT) -> None:
        original = getattr(instance, _ORIGINAL_KEY_ATTR, None)
        current = getattr(instance, self.key_field, None)
        if original is not None and current != original:
            raise ImmutableFieldError(
                f"Field '{self.model_cls.__name__}.{self.key_field}' is immutable once "
                "the record has been persisted."
            )

    def _stamp_identity(self, instance: ModelT) -> ModelT:
        object.__setattr__(instance, _ORIGINAL_KEY_ATTR, getattr(instance, self.key_field, None))
        return instance

    def _upsert_on_unique_fields(self, target: ModelT, values: dict[str, Any]) -> ModelT:
        insert_values = dict(values)
        insert_values.pop(self.key_field, None)

        stmt = sqlite_insert(self._table).values(**insert_values)
        update_cols = {
            key: value
            for key, value in insert_values.items()
            if key != self.key_field
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=list(self.config.unique_fields),
            set_=update_cols,
        )

        try:
            with self._engine.begin() as conn:
                conn.execute(stmt)
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc

        lookup = {field: insert_values[field] for field in self.config.unique_fields}
        refreshed = self.require(**lookup)

        for field_name in type(target).model_fields:
            object.__setattr__(target, field_name, getattr(refreshed, field_name))
        return self._stamp_identity(target)

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

    def _classify_integrity_error(self, exc: IntegrityError) -> Exception:
        msg = str(exc.orig).lower()
        key_marker = f".{self.key_field}".lower()

        if "unique constraint failed" in msg or "duplicate" in msg:
            if key_marker in msg:
                return DuplicateKeyError(
                    f"Duplicate primary key for '{self.model_cls.__name__}.{self.key_field}'."
                )
            return UniqueConstraintError(
                f"Unique constraint violated on '{self.model_cls.__name__}'."
            )
        return SchemaError(
            f"Database integrity error on table '{self.table_name}': {exc.orig}"
        )

    def __repr__(self) -> str:
        return (
            f"DatabaseRegistry("
            f"model={self.model_cls.__name__!r}, "
            f"table={self.table_name!r}, "
            f"url={self.database_url!r})"
        )
