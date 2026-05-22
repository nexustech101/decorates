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

import asyncio
import base64
from contextlib import AbstractAsyncContextManager, ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
import uuid
from typing import Any, Callable, Generator, Generic, Iterator, Mapping, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import (
    JSON, 
    Column, 
    DateTime, 
    ForeignKey, 
    Index, 
    Integer, 
    LargeBinary, 
    MetaData, 
    String, 
    Table, 
    and_, 
    delete, 
    event, 
    func, 
    inspect, 
    not_, 
    or_, 
    select, 
    text, 
    update
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from registers.core.logging import log_exception
from registers.db.engine import DatabaseContext, dialect_insert, dispose_engine, get_db_context
from registers.db.exceptions import (
    DuplicateKeyError,
    ImmutableFieldError,
    InvalidPrimaryKeyAssignmentError,
    InvalidQueryError,
    MigrationError,
    ModelRegistrationError,
    RecordNotFoundError,
    SchemaError,
    UniqueConstraintError,
)
from registers.db.fields import get_db_field_metadata
from registers.db.metadata import RegistryConfig
from registers.db.operators import VALID_OPERATORS, is_iterable_value, parse_criterion, split_field_expr
from registers.db.query import Agg, Page, Q
from registers.db.schema import SchemaManager
from registers.db.security import (
    hash_password,
    is_password_hash,
    verify_and_upgrade_password as verify_and_upgrade_password_value,
    verify_password as verify_password_value,
)
from registers.db.typing_utils import (
    default_database_url,
    default_table_name,
    field_allows_none,
    normalize_database_url,
    sqlalchemy_type_for_annotation,
    sqlalchemy_type_for_field,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
_ORIGINAL_KEY_ATTR = "__registers_original_key__"
_PASSWORD_FIELD = "password"
logger = logging.getLogger(__name__)
_ACTIVE_CONNECTIONS: ContextVar[dict[str, Connection]] = ContextVar(
    "registers_db_active_connections",
    default={},
)
_TENANT_SCOPE: ContextVar[Any] = ContextVar("registers_db_tenant_scope", default=None)
_TENANT_UNSCOPED: ContextVar[bool] = ContextVar("registers_db_tenant_unscoped", default=False)
_AUDIT_ACTOR: ContextVar[str | None] = ContextVar("registers_db_audit_actor", default=None)
MIGRATION_LEDGER_TABLE = "registers_schema_migrations"


@contextmanager
def tenant_scope(tenant: Any) -> Generator[None, None, None]:
    """Apply a tenant value to tenant-scoped manager operations in this context."""
    token = _TENANT_SCOPE.set(tenant)
    try:
        yield
    finally:
        _TENANT_SCOPE.reset(token)


@contextmanager
def unscoped() -> Generator[None, None, None]:
    """Temporarily bypass tenant and soft-delete default filters."""
    token = _TENANT_UNSCOPED.set(True)
    try:
        yield
    finally:
        _TENANT_UNSCOPED.reset(token)


@contextmanager
def audit_actor(actor: str | None) -> Generator[None, None, None]:
    """Attach an actor value to audit rows written in this context."""
    token = _AUDIT_ACTOR.set(actor)
    try:
        yield
    finally:
        _AUDIT_ACTOR.reset(token)


class _ModelManager(Generic[ModelT]):
    """
    SQLite-backed (and SQLAlchemy-compatible) persistence manager for a
    Pydantic model class.

    Attach to a model with the ``@database_registry`` decorator::

        @database_registry("app.db", table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            name: str

        # All CRUD lives on the manager, not the model class
        user = User.objects.create(name="Alice")
        users = User.objects.filter(name="Alice")
        user.save()      # instance method injected by decorator
        user.delete()    # instance method injected by decorator

    This class is internal; use ``DatabaseRegistry().database_registry(...)``
    or module-level ``@database_registry(...)`` for public usage.
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
        async_mode: bool = False,
        timestamps: bool = False,
        soft_delete: bool = False,
        audit_log: bool = False,
        audit_log_table: str | None = None,
        tenant_field: str | None = None,
        encryption_key: str | bytes | Callable[[], str | bytes] | None = None,
        log_queries: bool = False,
        slow_query_ms: int | None = None,
        engine_options: Mapping[str, Any] | None = None,
        read_replica_url: str | Path | None = None,
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
        self._field_metadata = {
            field_name: get_db_field_metadata(field_info)
            for field_name, field_info in self.model_cls.model_fields.items()
        }
        self._password_hash_fields = {
            field_name
            for field_name, field_meta in self._field_metadata.items()
            if bool(field_meta.get("db_hash_password", False))
        }
        self._db_excluded_fields = {
            field_name
            for field_name, field_meta in self._field_metadata.items()
            if bool(field_meta.get("db_exclude_from_db", False))
        }
        self._encrypted_fields = {
            field_name
            for field_name, field_meta in self._field_metadata.items()
            if bool(field_meta.get("db_encrypted", False))
        }
        self.async_mode = async_mode
        self.timestamps = timestamps
        self.soft_delete = soft_delete
        self.audit_log = audit_log
        self.audit_log_table = audit_log_table or f"{self.table_name}_audit"
        self.tenant_field = tenant_field
        self.encryption_key = encryption_key
        self.log_queries = log_queries
        self.slow_query_ms = slow_query_ms
        self.engine_options = dict(engine_options or {})
        self.read_replica_url = (
            normalize_database_url(read_replica_url) if read_replica_url is not None else None
        )
        self._validate_policy_fields()

        self._context: DatabaseContext = get_db_context(
            self.database_url,
            engine_options=self.engine_options,
        )
        self._read_context: DatabaseContext | None = (
            get_db_context(self.read_replica_url, engine_options=self.engine_options)
            if self.read_replica_url is not None
            else None
        )
        self._metadata = self._context.metadata
        self._engine = self._context.engine
        self._read_engine = self._read_context.engine if self._read_context is not None else self._engine
        self._install_query_logging()
        self._table = self._build_table()
        self._schema = SchemaManager(self._engine, self._table, self.table_name)
        self._audit_table = self._build_audit_table() if self.audit_log else None

        if auto_create:
            self._schema.create_schema(strict=False, include_all_metadata=True)

    def get_registry(self) -> _ModelManager[ModelT]:
        """Return this manager instance for contract parity with other registries."""
        return self

    # ------------------------------------------------------------------
    # Public schema surface (delegate to SchemaManager)
    # ------------------------------------------------------------------

    def create_schema(self) -> None:
        """CREATE TABLE IF NOT EXISTS — idempotent."""
        self._schema.create_schema(strict=True, include_all_metadata=False)

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
                    Table(target_name, MetaData()),
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

    def diff_schema(self) -> Any:
        """Return a schema drift report for this manager's table."""
        return self._schema.diff()

    def schema_diff(self) -> Any:
        """Alias for ``diff_schema()`` using the FUTURE.md public name."""
        return self.diff_schema()

    def migrate(self, *, dry_run: bool = True) -> Any:
        """Apply safe additive schema changes, or return the diff in dry-run mode."""
        diff = self.diff_schema()
        if dry_run or diff.ok:
            return diff
        for column_name in diff.missing_columns or []:
            field = self.model_cls.model_fields[column_name]
            self.ensure_column(
                column_name,
                field.annotation,
                nullable=self._column_nullable(column_name, field),
            )
        return self.diff_schema()

    def assert_schema_current(self) -> None:
        """Raise MigrationError if the live table differs from the registered model."""
        diff = self.diff_schema()
        if not diff.ok:
            raise MigrationError(
                f"Schema drift detected for table '{self.table_name}'.",
                operation="schema_diff",
                model=self.model_cls.__name__,
                table=self.table_name,
                details=diff.to_dict(),
            )

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
        active = _ACTIVE_CONNECTIONS.get()
        existing = active.get(self.database_url)
        if existing is not None:
            yield existing
            return

        with self._engine.begin() as conn:
            updated = dict(active)
            updated[self.database_url] = conn
            token = _ACTIVE_CONNECTIONS.set(updated)
            try:
                yield conn
            finally:
                _ACTIVE_CONNECTIONS.reset(token)

    @contextmanager
    def _connection_scope(self) -> Generator[Connection, None, None]:
        if self._context.disposed:
            raise SchemaError(
                f"Database manager for '{self.model_cls.__name__}' has been disposed.",
                operation="database_lifecycle",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        active = _ACTIVE_CONNECTIONS.get().get(self.database_url)
        if active is not None:
            yield active
            return

        with self._engine.begin() as conn:
            yield conn

    @contextmanager
    def _read_connection_scope(self) -> Generator[Connection, None, None]:
        active = _ACTIVE_CONNECTIONS.get().get(self.database_url)
        if active is not None:
            yield active
            return
        engine = self._read_engine
        with engine.begin() as conn:
            yield conn

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create(self, **data: Any) -> ModelT:
        """
        Strict INSERT.  Raises on duplicate primary key or unique violation.

        Use this when you explicitly want an error if the record already exists.
        """
        data = self._prepare_create_data(dict(data))
        self._call_hook("before_create", data)
        instance = self.model_cls(**data)
        self._prepare_instance_for_save(instance, is_create=True)
        try:
            with self._connection_scope() as conn:
                created = self._create_with_conn(conn, instance)
                self._audit(conn, "create", created, self._public_model_dump(created))
            self._call_hook("after_create", created)
            self._call_hook("after_save", created)
            return created
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
        audit_operation = "update" if getattr(target, _ORIGINAL_KEY_ATTR, None) is not None else "upsert"
        self._prepare_instance_for_save(target, is_create=False)
        try:
            with self._connection_scope() as conn:
                saved = self._upsert_with_conn(conn, target)
                self._audit(conn, audit_operation, saved, self._public_model_dump(saved))
            self._call_hook("after_save", saved)
            return saved
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
        self._call_hook("before_save", instance)
        return self.upsert(instance)

    def update_where(self, criteria: Mapping[str, Any], **updates: Any) -> list[ModelT]:
        """
        Update all rows matching *criteria* and return the refreshed records.

        Both *criteria* and *updates* are validated against known model fields
        before any SQL is issued.
        """
        criteria = dict(criteria)
        include_deleted = bool(criteria.pop("include_deleted", False))
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
        self._assert_known_update_fields(updates)
        criteria = self._with_policy_criteria(criteria, include_deleted=include_deleted)
        updates = self._prepare_update_values(dict(updates))
        updates = self._normalize_write_mapping(updates)

        try:
            with self._connection_scope() as conn:
                stmt = update(self._table).values(**updates)
                stmt = self._apply_where(stmt, criteria)

                if getattr(conn.dialect, "update_returning", False):
                    rows = conn.execute(stmt.returning(self._table)).mappings().all()
                    models = [self._row_to_model(row) for row in rows]
                    audit_operation = self._audit_operation_for_updates(updates)
                    for model in models:
                        self._audit(conn, audit_operation, model, dict(updates))
                    return models

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
                models = [self._row_to_model(row) for row in rows]
                audit_operation = self._audit_operation_for_updates(updates)
                for model in models:
                    self._audit(conn, audit_operation, model, dict(updates))
                return models
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("update_where", exc)

    def delete(self, key_value: Any) -> bool:
        """Delete the row with the given primary key. Returns True if deleted."""
        if self.soft_delete:
            row = self.get(key_value, include_deleted=True)
            if row is None:
                return False
            self._call_hook("before_delete", row)
            updated = self.update_where({self.key_field: key_value}, deleted_at=self._timestamp_value("deleted_at"))
            if updated:
                self._call_hook("after_delete", updated[0])
            return bool(updated)
        row = self.get(key_value)
        if row is not None:
            self._call_hook("before_delete", row)
        deleted = self.delete_where(**{self.key_field: key_value}) > 0
        if deleted and row is not None:
            self._call_hook("after_delete", row)
        return deleted

    def delete_where(self, **criteria: Any) -> int:
        """Delete all rows matching *criteria*. Returns the deleted row count."""
        if criteria.pop("include_deleted", False):
            include_deleted = True
        else:
            include_deleted = False
        if not criteria:
            raise InvalidQueryError(
                "delete_where() requires at least one filter criterion.",
                operation="delete_where",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        self._assert_known_fields(criteria)
        criteria = self._with_policy_criteria(criteria, include_deleted=include_deleted)

        stmt = delete(self._table)
        stmt = self._apply_where(stmt, criteria)
        try:
            with self._connection_scope() as conn:
                if self.soft_delete:
                    rows = conn.execute(self._apply_where(select(self._table), criteria)).mappings().all()
                    values = self._normalize_write_mapping({"deleted_at": self._timestamp_value("deleted_at")})
                    result = conn.execute(update(self._table).values(**values).where(*stmt._where_criteria))
                    for row in rows:
                        self._audit(conn, "delete", self._row_to_model(row), values)
                    return result.rowcount or 0
                result = conn.execute(stmt)
            return result.rowcount or 0
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("delete_where", exc)

    def bulk_delete(
        self,
        ids: list[Any] | tuple[Any, ...] | set[Any] | None = None,
        *,
        dangerous_allow_full_table_delete: bool = False,
        **criteria: Any,
    ) -> int:
        """Delete by primary-key collection or criteria, returning affected rows."""
        if ids is not None:
            if criteria:
                raise InvalidQueryError(
                    "bulk_delete() accepts ids or criteria, not both.",
                    operation="bulk_delete",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                )
            return self.delete_where(**{f"{self.key_field}__in": list(ids)})
        if not criteria and not dangerous_allow_full_table_delete:
            raise InvalidQueryError(
                "bulk_delete() requires ids, criteria, or dangerous_allow_full_table_delete=True.",
                operation="bulk_delete",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        if dangerous_allow_full_table_delete and not criteria:
            stmt = delete(self._table)
            try:
                with self._connection_scope() as conn:
                    result = conn.execute(stmt)
                return result.rowcount or 0
            except SQLAlchemyError as exc:
                self._raise_sqlalchemy_error("bulk_delete", exc)
        return self.delete_where(**criteria)

    def bulk_create(self, records: list[Mapping[str, Any]]) -> list[ModelT]:
        """Create multiple records atomically and return stamped models."""
        if not records:
            return []

        mutable_records = [self._prepare_create_data(dict(record)) for record in records]
        self._call_hook("before_bulk_create", mutable_records)
        instances = [self.model_cls(**record) for record in mutable_records]

        for instance in instances:
            self._prepare_instance_for_save(instance, is_create=True)
        for instance in instances:
            self._reject_explicit_autoincrement_key(instance)

        values_list = [self._prepare_insert_values(instance) for instance in instances]

        try:
            with self._connection_scope() as conn:
                supports_insert_returning = bool(
                    getattr(conn.dialect, "insert_returning", False)
                    and getattr(conn.dialect, "insert_executemany_returning", False)
                )
                if supports_insert_returning:
                    stmt = self._table.insert().returning(self._table)
                    rows = conn.execute(stmt, values_list).mappings().all()
                    created = [self._row_to_model(row) for row in rows]
                else:
                    created = [self._create_with_conn(conn, instance) for instance in instances]
                for model in created:
                    self._audit(conn, "create", model, self._public_model_dump(model))
            self._call_hook("after_bulk_create", created)
            return created
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("bulk_create", exc)

    def bulk_upsert(self, records: list[Mapping[str, Any]]) -> list[ModelT]:
        """Upsert multiple records atomically and return stamped models."""
        if not records:
            return []

        targets = [self.model_cls(**record) for record in records]
        for target in targets:
            self._prepare_instance_for_save(target, is_create=False)
        persisted: list[ModelT] = []

        try:
            with self._connection_scope() as conn:
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

    def get_or_create(
        self,
        *,
        lookup: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> tuple[ModelT, bool]:
        """Return an existing record for a unique lookup or create it."""
        self._assert_atomic_lookup(lookup)
        existing = self.get(**dict(lookup))
        if existing is not None:
            return existing, False
        try:
            return self.create(**{**dict(defaults or {}), **dict(lookup)}), True
        except UniqueConstraintError:
            return self.require(**dict(lookup)), False

    def update_or_create(
        self,
        *,
        lookup: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> tuple[ModelT, bool]:
        """Update an existing record for a unique lookup or create it."""
        self._assert_atomic_lookup(lookup)
        existing = self.get(**dict(lookup))
        if existing is None:
            return self.create(**{**dict(defaults or {}), **dict(lookup)}), True
        for field, value in dict(defaults or {}).items():
            object.__setattr__(existing, field, value)
        existing.save()
        return existing, False

    def restore(self, key_value: Any) -> ModelT:
        """Restore a soft-deleted record."""
        if not self.soft_delete:
            raise InvalidQueryError("restore() requires soft_delete=True.", operation="restore")
        rows = self.update_where({self.key_field: key_value, "include_deleted": True}, deleted_at=None)
        if not rows:
            raise RecordNotFoundError(
                f"No {self.model_cls.__name__} found matching {self.key_field}={key_value!r}.",
                operation="restore",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        return rows[0]

    def hard_delete(self, key_value: Any) -> bool:
        """Physically delete a row, bypassing soft delete."""
        row = self.get(key_value, include_deleted=True)
        if row is None:
            return False
        stmt = delete(self._table).where(
            self._table.c[self.key_field]
            == self._normalize_mapping_for_db({self.key_field: key_value})[self.key_field]
        )
        try:
            with self._connection_scope() as conn:
                result = conn.execute(stmt)
                deleted = bool(result.rowcount)
                if deleted:
                    self._audit(conn, "hard_delete", row, {})
                return deleted
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("hard_delete", exc)

    def purge_deleted(self, *, before: datetime) -> int:
        """Physically delete soft-deleted rows before a timestamp."""
        if not self.soft_delete:
            raise InvalidQueryError("purge_deleted() requires soft_delete=True.", operation="purge_deleted")
        rows = self.filter(include_deleted=True, deleted_at__lt=before)
        count = 0
        for row in rows:
            if self.hard_delete(getattr(row, self.key_field)):
                count += 1
        return count

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
        with self._context.lock:
            existing = self._context.tables.get(self.table_name)
            if existing is not None:
                return existing

            table = self._construct_table(self._metadata, self.table_name)
            self._context.tables[self.table_name] = table
            return table

    def _build_audit_table(self) -> Table:
        with self._context.lock:
            existing = self._context.tables.get(self.audit_log_table)
            if existing is not None:
                return existing
            table = Table(
                self.audit_log_table,
                self._metadata,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("table_name", String(255), nullable=False),
                Column("record_id", String(255), nullable=True),
                Column("operation", String(64), nullable=False),
                Column("changed_fields", JSON(), nullable=False),
                Column("actor", String(255), nullable=True),
                Column("timestamp", DateTime(timezone=True), nullable=False),
            )
            self._context.tables[self.audit_log_table] = table
            return table

    def _validate_policy_fields(self) -> None:
        fields = self.model_cls.model_fields
        if self.timestamps:
            missing = [name for name in ("created_at", "updated_at") if name not in fields]
            if missing:
                raise ModelRegistrationError(
                    f"timestamps=True requires declared field(s): {', '.join(missing)}.",
                    model=self.model_cls.__name__,
                )
        if self.soft_delete and "deleted_at" not in fields:
            raise ModelRegistrationError(
                "soft_delete=True requires a declared nullable 'deleted_at' field.",
                model=self.model_cls.__name__,
            )
        if self.tenant_field is not None and self.tenant_field not in fields:
            raise ModelRegistrationError(
                f"tenant_field '{self.tenant_field}' is not a field on model '{self.model_cls.__name__}'.",
                model=self.model_cls.__name__,
                field=self.tenant_field,
            )

    def _install_query_logging(self) -> None:
        if not self.log_queries and self.slow_query_ms is None:
            return
        marker = f"_registers_query_logging_{id(self)}"
        if getattr(self._engine, marker, False):
            return
        setattr(self._engine, marker, True)

        @event.listens_for(self._engine, "before_cursor_execute")
        def _before_cursor_execute(conn, _cursor, _statement, _parameters, _context, _executemany):  # noqa: ANN001
            conn.info.setdefault("_registers_query_start", []).append(time.perf_counter())

        @event.listens_for(self._engine, "after_cursor_execute")
        def _after_cursor_execute(conn, _cursor, statement, _parameters, _context, _executemany):  # noqa: ANN001
            starts = conn.info.get("_registers_query_start") or []
            started = starts.pop() if starts else time.perf_counter()
            elapsed_ms = (time.perf_counter() - started) * 1000
            if self.log_queries or (
                self.slow_query_ms is not None and elapsed_ms >= self.slow_query_ms
            ):
                logger.info(
                    "registers.db query model=%s table=%s elapsed_ms=%.2f sql=%s",
                    self.model_cls.__name__,
                    self.table_name,
                    elapsed_ms,
                    statement,
                )

    def _construct_table(self, metadata: MetaData, table_name: str) -> Table:
        unique_set = set(self.config.unique_fields)
        columns: list[Column[Any]] = []
        index_fields: list[str] = []
        columns_by_name: dict[str, Column[Any]] = {}

        for field_name, field_info in self.model_cls.model_fields.items():
            field_meta = get_db_field_metadata(field_info)
            if bool(field_meta.get("db_exclude_from_db", False)):
                continue
            nullable = self._column_nullable(field_name, field_info)
            is_pk = field_name == self.key_field
            sa_type = (
                LargeBinary(16)
                if is_pk and self.config.id_strategy == "uuid4"
                else sqlalchemy_type_for_field(field_info.annotation, field_meta)
            )

            col_args: list[Any] = []
            fk_reference = field_meta.get("db_foreign_key")
            if fk_reference:
                col_args.append(ForeignKey(str(fk_reference)))

            is_unique = field_name in unique_set or bool(field_meta.get("db_unique", False))
            col_kwargs: dict[str, Any] = {
                "primary_key": is_pk,
                "nullable": nullable,
                "unique": is_unique,
            }
            if is_pk and self.config.autoincrement:
                col_kwargs["autoincrement"] = True
            elif is_pk:
                col_kwargs["autoincrement"] = False

            column = Column(field_name, sa_type, *col_args, **col_kwargs)
            columns.append(column)
            columns_by_name[field_name] = column

            should_index = bool(field_meta.get("db_index", False))
            if should_index and not is_pk and not is_unique:
                index_fields.append(field_name)

        table_kwargs: dict[str, Any] = {}
        if self.config.autoincrement and self.database_url.startswith("sqlite"):
            table_kwargs["sqlite_autoincrement"] = True

        index_specs = [
            Index(f"ix_{table_name}_{field_name}", columns_by_name[field_name])
            for field_name in index_fields
        ]

        return Table(table_name, metadata, *columns, *index_specs, **table_kwargs)

    def _detach_table_from_context(self, table_name: str, table: Table) -> None:
        mapped = self._context.tables.get(table_name)
        if mapped is table:
            self._context.tables.pop(table_name, None)
        if self._metadata.tables.get(table_name) is table:
            self._metadata.remove(table)

    def _capture_table_state(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "table_name": self.table_name,
        }

    def _restore_table_state(self, state: Mapping[str, Any]) -> None:
        self._detach_table_from_context(self.table_name, self._table)
        self.config = state["config"]
        self.table_name = state["table_name"]
        self._metadata = self._context.metadata
        self._table = self._build_table()
        self._schema = SchemaManager(self._engine, self._table, self.table_name)

    def _rebind_table_state(self, table_name: str) -> None:
        """
        Rebuild all state derived from the active table name.

        This is the single refresh path used after schema mutations that
        change table identity.
        """
        with self._context.lock:
            self._detach_table_from_context(self.table_name, self._table)
            self.config = replace(self.config, table_name=table_name)
            self.table_name = table_name
            self._metadata = self._context.metadata
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

    def _with_policy_criteria(
        self,
        criteria: Mapping[str, Any],
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        scoped = dict(criteria)
        if self.soft_delete and not include_deleted and not _TENANT_UNSCOPED.get():
            scoped.setdefault("deleted_at__is_null", True)
        if self.tenant_field is not None and not _TENANT_UNSCOPED.get():
            tenant = _TENANT_SCOPE.get()
            if tenant is None:
                raise InvalidQueryError(
                    f"tenant_scope(...) is required for tenant-scoped model '{self.model_cls.__name__}'.",
                    operation="tenant_scope",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.tenant_field,
                )
            scoped.setdefault(self.tenant_field, tenant)
        return scoped

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

    def _assert_atomic_lookup(self, lookup: Mapping[str, Any]) -> None:
        if not lookup:
            raise InvalidQueryError(
                "Atomic create helpers require a non-empty lookup.",
                operation="get_or_create",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        self._assert_known_fields(lookup)
        fields = tuple(lookup.keys())
        if fields == (self.key_field,) or set(fields) == set(self.config.unique_fields):
            return
        raise InvalidQueryError(
            "get_or_create()/update_or_create() require lookup fields matching the "
            "primary key or configured unique_fields.",
            operation="get_or_create",
            model=self.model_cls.__name__,
            table=self.table_name,
            details={"lookup_fields": list(fields), "unique_fields": list(self.config.unique_fields)},
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

    def _assert_known_update_fields(self, updates: Mapping[str, Any]) -> None:
        self._assert_known_fields(updates, allow_operators=False)

    def _assert_known_fields(
        self,
        fields: Mapping[str, Any],
        *,
        allow_operators: bool = True,
    ) -> None:
        model_fields = self.model_cls.model_fields
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
                    adapter.validate_python(value)
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

    def _prepare_create_data(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.tenant_field is not None and not _TENANT_UNSCOPED.get():
            tenant = _TENANT_SCOPE.get()
            if tenant is None:
                raise InvalidQueryError(
                    f"tenant_scope(...) is required for tenant-scoped model '{self.model_cls.__name__}'.",
                    operation="tenant_scope",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.tenant_field,
                )
            explicit = data.get(self.tenant_field)
            if explicit is not None and explicit != tenant:
                raise InvalidQueryError(
                    f"Explicit tenant value for '{self.tenant_field}' does not match active tenant scope.",
                    operation="tenant_scope",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.tenant_field,
                )
            data[self.tenant_field] = tenant
        if self.timestamps:
            data.setdefault("created_at", self._timestamp_value("created_at"))
            data.setdefault("updated_at", data["created_at"])
        if self.soft_delete:
            data.setdefault("deleted_at", None)
        return data

    def _prepare_update_values(self, values: dict[str, Any]) -> dict[str, Any]:
        if self.timestamps and "updated_at" not in values:
            values["updated_at"] = self._timestamp_value("updated_at")
        return values

    def _prepare_instance_for_save(self, instance: ModelT, *, is_create: bool) -> None:
        if self.timestamps:
            if is_create and getattr(instance, "created_at", None) is None:
                object.__setattr__(instance, "created_at", self._timestamp_value("created_at"))
            object.__setattr__(instance, "updated_at", self._timestamp_value("updated_at"))
        if self.soft_delete and getattr(instance, "deleted_at", None) is None:
            object.__setattr__(instance, "deleted_at", None)

    def _timestamp_value(self, field_name: str) -> Any:
        annotation = self.model_cls.model_fields[field_name].annotation
        now = datetime.now(timezone.utc)
        try:
            TypeAdapter(annotation).validate_python(now)
            return now
        except Exception:
            return now.isoformat()

    def _call_hook(self, name: str, *args: Any) -> None:
        hook = getattr(self.model_cls, name, None)
        if callable(hook):
            hook(*args)

    def _public_model_dump(self, model: ModelT) -> dict[str, Any]:
        return {
            key: value
            for key, value in model.model_dump().items()
            if key not in self._db_excluded_fields
        }

    def _audit(
        self,
        conn: Connection,
        operation: str,
        model: ModelT | None,
        changed_fields: Mapping[str, Any],
    ) -> None:
        if self._audit_table is None:
            return
        record_id = None if model is None else getattr(model, self.key_field, None)
        conn.execute(
            self._audit_table.insert().values(
                table_name=self.table_name,
                record_id=None if record_id is None else str(record_id),
                operation=operation,
                changed_fields=json.loads(json.dumps(dict(changed_fields), default=str)),
                actor=_AUDIT_ACTOR.get(),
                timestamp=datetime.now(timezone.utc),
            )
        )

    def _audit_operation_for_updates(self, updates: Mapping[str, Any]) -> str:
        if self.soft_delete and set(updates) <= {"deleted_at", "updated_at"}:
            deleted_at = updates.get("deleted_at")
            return "restore" if deleted_at is None else "delete"
        return "update"

    def _encryption_fernet(self) -> Any:
        if self.encryption_key is None:
            raise InvalidQueryError(
                "Encrypted fields require encryption_key on database_registry(...).",
                operation="encryption",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        try:
            from cryptography.fernet import Fernet # type: ignore
        except Exception as exc:  # pragma: no cover
            raise InvalidQueryError(
                "Encrypted fields require the optional 'cryptography' package.",
                operation="encryption",
                model=self.model_cls.__name__,
                table=self.table_name,
            ) from exc
        key = self.encryption_key() if callable(self.encryption_key) else self.encryption_key
        raw_key = key.encode() if isinstance(key, str) else bytes(key)
        try:
            return Fernet(raw_key)
        except Exception:
            return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest()))

    def _encrypt_value(self, value: Any) -> Any:
        if value is None:
            return None
        payload = value if isinstance(value, str) else json.dumps(value)
        return self._encryption_fernet().encrypt(payload.encode()).decode()

    def _decrypt_value(self, value: Any) -> Any:
        if value is None:
            return None
        return self._encryption_fernet().decrypt(str(value).encode()).decode()

    def _row_to_model(self, row: Mapping[str, Any]) -> ModelT:
        values = dict(row)
        if self.config.id_strategy == "uuid4" and self.key_field in values:
            values[self.key_field] = self._uuid_from_db(values[self.key_field])
        for field_name in self._encrypted_fields:
            if field_name in values:
                values[field_name] = self._decrypt_value(values[field_name])
        return self._stamp_identity(self.model_cls.model_validate(values))

    def _model_to_row(self, model: ModelT) -> dict[str, Any]:
        # Use plain model_dump() (no mode='json') so Python date/datetime/Decimal
        # objects are preserved as native types.  SQLAlchemy's column types handle
        # the DB-level serialisation correctly.
        self._ensure_generated_uuid_key(model)
        self._normalize_model_for_write(model)
        return self._normalize_mapping_for_db(self._public_model_dump(model))

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
        if not update_cols:
            return None
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
                    .where(
                        self._table.c[self.key_field]
                        == self._normalize_mapping_for_db(
                            {self.key_field: getattr(existing, self.key_field)}
                        )[self.key_field]
                    )
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
        for field_name in self._password_hash_fields:
            password = getattr(model, field_name, None)
            if isinstance(password, str) and password and not is_password_hash(password):
                object.__setattr__(model, field_name, hash_password(password))

    def _normalize_write_mapping(self, values: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(values)
        normalized = {
            key: value
            for key, value in normalized.items()
            if key not in self._db_excluded_fields
        }
        for field_name in self._password_hash_fields:
            password = normalized.get(field_name)
            if isinstance(password, str) and password and not is_password_hash(password):
                normalized[field_name] = hash_password(password)
        return self._normalize_mapping_for_db(normalized)

    def _ensure_generated_uuid_key(self, model: ModelT) -> None:
        if self.config.id_strategy != "uuid4":
            return
        if getattr(model, self.key_field, None) is None:
            object.__setattr__(model, self.key_field, uuid.uuid4())

    def _normalize_mapping_for_db(self, values: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {
            key: value
            for key, value in dict(values).items()
            if key not in self._db_excluded_fields
        }
        if self.config.id_strategy == "uuid4" and self.key_field in normalized:
            normalized[self.key_field] = self._uuid_to_db(normalized[self.key_field])
        for field_name in self._encrypted_fields:
            if field_name in normalized:
                normalized[field_name] = self._encrypt_value(normalized[field_name])
        return normalized

    def _normalize_criteria_for_db(self, criteria: Mapping[str, Any]) -> dict[str, Any]:
        if self.config.id_strategy != "uuid4":
            return dict(criteria)

        normalized: dict[str, Any] = {}
        for field_expr, value in criteria.items():
            field_name, operator = split_field_expr(field_expr)
            if field_name != self.key_field:
                normalized[field_expr] = value
                continue
            if operator in {"in", "not_in"} and is_iterable_value(value):
                normalized[field_expr] = [self._uuid_to_db(item) for item in value]
            elif operator == "between" and isinstance(value, (list, tuple)):
                normalized[field_expr] = [self._uuid_to_db(item) for item in value]
            else:
                normalized[field_expr] = self._uuid_to_db(value)
        return normalized

    @staticmethod
    def _uuid_to_db(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value.bytes
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return uuid.UUID(value).bytes
        return value

    @staticmethod
    def _uuid_from_db(value: Any) -> Any:
        if value is None or isinstance(value, uuid.UUID):
            return value
        if isinstance(value, (bytes, bytearray)):
            return uuid.UUID(bytes=bytes(value))
        if isinstance(value, str):
            return uuid.UUID(value)
        return value

    # ------------------------------------------------------------------
    # Private: error classification
    # ------------------------------------------------------------------

    def _raise_sqlalchemy_error(self, operation: str, exc: SQLAlchemyError) -> None:
        log_exception(
            logger,
            logging.ERROR,
            "Database operation failed.",
            error=exc,
            operation=operation,
            model=self.model_cls.__name__,
            table=self.table_name,
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
                log_exception(
                    logger,
                    logging.WARNING,
                    "Primary-key integrity violation.",
                    error=exc,
                    model=self.model_cls.__name__,
                    key_field=self.key_field,
                    table=self.table_name,
                )
                return DuplicateKeyError(
                    f"Duplicate primary key for '{self.model_cls.__name__}.{self.key_field}'.",
                    operation="write",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.key_field,
                )
            log_exception(
                logger,
                logging.WARNING,
                "Unique-constraint integrity violation.",
                error=exc,
                model=self.model_cls.__name__,
                table=self.table_name,
            )
            return UniqueConstraintError(
                f"Unique constraint violated on '{self.model_cls.__name__}'.",
                operation="write",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        log_exception(
            logger,
            logging.ERROR,
            "Unhandled integrity error.",
            error=exc,
            model=self.model_cls.__name__,
            table=self.table_name,
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
            f"_ModelManager("
            f"model={self.model_cls.__name__!r}, "
            f"table={self.table_name!r}, "
            f"url={self.database_url!r})"
        )


class _AsyncTransaction(AbstractAsyncContextManager[Any]):
    def __init__(self, manager: _ModelManager[Any]) -> None:
        self._manager = manager
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        self._cm = self._manager.transaction()
        return self._cm.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        return self._cm.__exit__(exc_type, exc, tb)


class AsyncModelManager(Generic[ModelT]):
    """Awaitable manager facade for ``async_mode=True`` registrations."""

    def __init__(self, sync_manager: _ModelManager[ModelT]) -> None:
        self._sync_manager = sync_manager

    def transaction(self) -> _AsyncTransaction:
        return _AsyncTransaction(self._sync_manager)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._sync_manager, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attr, *args, **kwargs)

        return call

    def __repr__(self) -> str:
        return f"AsyncModelManager({self._sync_manager!r})"


class DatabaseRegistry:
    """
    Coordinator for model registrations within one logical DB namespace.

    Create one instance and register models through ``@db.database_registry(...)``.
    Registered models still receive the same manager API on ``Model.objects``.
    """

    _OWNER_MARKER = "__registers_db_owner__"

    def __init__(self) -> None:
        self._managers: dict[type[BaseModel], _ModelManager[Any]] = {}

    def get_registry(self) -> DatabaseRegistry:
        return self

    def all(self) -> dict[type[BaseModel], _ModelManager[Any]]:
        return dict(self._managers)

    def clear(self) -> None:
        self._managers.clear()

    def reset_registry(self) -> None:
        self.clear()

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """
        Bind all manager CRUD for this registry to one transaction per database URL.

        When the registry spans multiple database URLs this coordinates a best-effort
        transaction per engine; it does not provide two-phase commit semantics.
        """
        urls = list(dict.fromkeys(manager.database_url for manager in self._managers.values()))
        if not urls:
            yield
            return

        active = _ACTIVE_CONNECTIONS.get()
        with ExitStack() as stack:
            updated = dict(active)
            for url in urls:
                if url in updated:
                    continue
                context = get_db_context(url)
                updated[url] = stack.enter_context(context.engine.begin())
            token = _ACTIVE_CONNECTIONS.set(updated)
            try:
                yield
            finally:
                _ACTIVE_CONNECTIONS.reset(token)

    def create_all(self) -> None:
        """Create schemas for every model registered on this registry."""
        contexts = {
            manager.database_url: manager._context
            for manager in self._managers.values()
        }
        for url, context in contexts.items():
            self._ensure_migration_ledger(context.engine)
            try:
                context.metadata.create_all(context.engine)
            except SQLAlchemyError as exc:
                raise SchemaError(
                    f"Failed to create schemas for database '{url}'.",
                    operation="create_all",
                    details={"database_url": url},
                ) from exc

    def check_all(self) -> bool:
        """Return True when every registered table exists and matches its model."""
        return all(diff.ok for diff in self.diff_all().values())

    def diff_all(self) -> dict[str, Any]:
        """Return schema drift reports keyed by table name."""
        return {
            manager.table_name: manager.diff_schema()
            for manager in self._managers.values()
        }

    def schema_diff(self) -> dict[str, Any]:
        """Alias for ``diff_all()``."""
        return self.diff_all()

    def migrate(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Run safe additive migrations for every registered manager."""
        return {
            manager.table_name: manager.migrate(dry_run=dry_run)
            for manager in self._managers.values()
        }

    def assert_schema_current(self) -> None:
        """Raise MigrationError when any registered table is missing or drifted."""
        drift = {
            table_name: diff.to_dict()
            for table_name, diff in self.diff_all().items()
            if not diff.ok
        }
        if drift:
            raise MigrationError(
                "Schema drift detected for registered models.",
                operation="schema_diff",
                details={"tables": drift},
            )

    def dispose_all(self) -> None:
        """Dispose every engine used by managers owned by this registry."""
        for url in list(dict.fromkeys(manager.database_url for manager in self._managers.values())):
            dispose_engine(url)

    @staticmethod
    def _ensure_migration_ledger(engine: Any) -> None:
        ledger_sql = f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_LEDGER_TABLE} (
            version VARCHAR(255) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        with engine.begin() as conn:
            conn.execute(text(ledger_sql))

    def database_registry(
        self,
        database_url: str | Path | None = None,
        *,
        table_name: str | None = None,
        key_field: str = "id",
        manager_attr: str = "objects",
        auto_create: bool = True,
        autoincrement: bool = False,
        unique_fields: list[str] | tuple[str, ...] = (),
        async_mode: bool = False,
        timestamps: bool = False,
        soft_delete: bool = False,
        audit_log: bool = False,
        audit_log_table: str | None = None,
        tenant_field: str | None = None,
        encryption_key: str | bytes | Callable[[], str | bytes] | None = None,
        log_queries: bool = False,
        slow_query_ms: int | None = None,
        engine_options: Mapping[str, Any] | None = None,
        read_replica_url: str | Path | None = None,
    ) -> Callable[[type[ModelT]], type[ModelT]]:
        def decorator(model_cls: type[ModelT]) -> type[ModelT]:
            self._assert_valid_model(model_cls)
            self._assert_model_owner_available(model_cls)

            manager = _ModelManager(
                model_cls,
                database_url=database_url,
                table_name=table_name,
                key_field=key_field,
                manager_attr=manager_attr,
                auto_create=auto_create,
                autoincrement=autoincrement,
                unique_fields=tuple(unique_fields),
                async_mode=async_mode,
                timestamps=timestamps,
                soft_delete=soft_delete,
                audit_log=audit_log,
                audit_log_table=audit_log_table,
                tenant_field=tenant_field,
                encryption_key=encryption_key,
                log_queries=log_queries,
                slow_query_ms=slow_query_ms,
                engine_options=engine_options,
                read_replica_url=read_replica_url,
            )

            exposed_manager: Any = AsyncModelManager(manager) if async_mode else manager
            self._safe_setattr(model_cls, manager_attr, exposed_manager)
            self._inject_instance_methods(model_cls, manager, key_field, async_mode=async_mode)
            self._inject_schema_forwarders(model_cls, manager)

            self._managers[model_cls] = manager
            setattr(model_cls, self._OWNER_MARKER, id(self))
            return model_cls

        return decorator

    def _assert_model_owner_available(self, model_cls: type[BaseModel]) -> None:
        owner_id = getattr(model_cls, self._OWNER_MARKER, None)
        if owner_id is None or owner_id == id(self):
            return
        raise ModelRegistrationError(
            f"Model '{model_cls.__name__}' is already registered by another "
            "DatabaseRegistry instance.",
            model=model_cls.__name__,
            details={"owner_conflict": True},
        )

    @staticmethod
    def _assert_valid_model(model_cls: type) -> None:
        if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)):
            logger.error("Invalid model registration target: %r", model_cls)
            raise ModelRegistrationError(
                "@database_registry can only decorate Pydantic BaseModel subclasses."
            )
        if hasattr(model_cls, "__dataclass_fields__"):
            logger.error(
                "Invalid model '%s': stdlib dataclass cannot be combined with BaseModel.",
                getattr(model_cls, "__name__", repr(model_cls)),
            )
            raise ModelRegistrationError(
                "Do not combine stdlib @dataclass with pydantic.BaseModel. "
                "Define the model as a plain `class User(BaseModel): ...`."
            )

    @staticmethod
    def _safe_setattr(model_cls: type, name: str, value: Any) -> None:
        in_dict = name in model_cls.__dict__
        in_pydantic_fields = name in model_cls.model_fields

        if in_dict or in_pydantic_fields:
            source = "a Pydantic field" if in_pydantic_fields else "a class attribute"
            logger.error(
                "Attribute collision while attaching '%s' to model '%s' (%s).",
                name,
                model_cls.__name__,
                source,
            )
            raise ModelRegistrationError(
                f"Cannot attach '{name}' to '{model_cls.__name__}' - "
                f"it is already defined as {source} on the model. "
                "Choose a different manager_attr or rename the conflicting attribute."
            )
        setattr(model_cls, name, value)

    @classmethod
    def _inject_instance_methods(
        cls,
        model_cls: type[ModelT],
        manager: _ModelManager[ModelT],
        key_field: str,
        *,
        async_mode: bool = False,
    ) -> None:
        if async_mode:
            async def save(self: ModelT) -> ModelT:
                updated = await asyncio.to_thread(manager.save, self)
                for field in type(self).model_fields:
                    object.__setattr__(self, field, getattr(updated, field))
                return self

            async def delete(self: ModelT) -> bool:
                return await asyncio.to_thread(manager.delete, getattr(self, key_field))

            async def refresh(self: ModelT) -> ModelT:
                return await asyncio.to_thread(manager.refresh, self)

            async def update_instance(self: ModelT, values: Mapping[str, Any]) -> ModelT:
                manager._assert_known_update_fields(values)
                for field_name, value in values.items():
                    object.__setattr__(self, field_name, value)
                return await save(self)

            async def apply_patch(self: ModelT, patch: Any) -> ModelT:
                values = patch.model_dump(exclude_unset=True) if isinstance(patch, BaseModel) else dict(patch)
                return await update_instance(self, values)

            for method_name, method in [
                ("save", save),
                ("delete", delete),
                ("refresh", refresh),
                ("update", update_instance),
                ("apply_patch", apply_patch),
            ]:
                cls._safe_setattr(model_cls, method_name, method)
            return

        def save(self: ModelT) -> ModelT:
            updated = manager.save(self)
            for field in type(self).model_fields:
                object.__setattr__(self, field, getattr(updated, field))
            return self

        def delete(self: ModelT) -> bool:
            return manager.delete(getattr(self, key_field))

        def refresh(self: ModelT) -> ModelT:
            return manager.refresh(self)

        def update_instance(self: ModelT, values: Mapping[str, Any]) -> ModelT:
            manager._assert_known_update_fields(values)
            for field_name, value in values.items():
                object.__setattr__(self, field_name, value)
            saved = manager.save(self)
            for field in type(self).model_fields:
                object.__setattr__(self, field, getattr(saved, field))
            return self

        def apply_patch(self: ModelT, patch: Any) -> ModelT:
            if isinstance(patch, BaseModel):
                values = patch.model_dump(exclude_unset=True)
            else:
                values = dict(patch)
            return update_instance(self, values)

        injected_methods: list[tuple[str, Callable[..., Any]]] = [
            ("save", save),
            ("delete", delete),
            ("refresh", refresh),
            ("update", update_instance),
            ("apply_patch", apply_patch),
        ]

        if _PASSWORD_FIELD in manager._password_hash_fields:
            def verify_password(self: ModelT, candidate: str) -> bool:
                return verify_password_value(candidate, getattr(self, "password"))

            def verify_and_upgrade_password(self: ModelT, candidate: str) -> bool:
                verified, upgraded_hash = verify_and_upgrade_password_value(
                    candidate,
                    getattr(self, "password"),
                )
                if verified and upgraded_hash is not None:
                    object.__setattr__(self, "password", upgraded_hash)
                    manager.save(self)
                return verified

            injected_methods.append(("verify_password", verify_password))
            injected_methods.append(("verify_and_upgrade_password", verify_and_upgrade_password))

        for method_name, method in injected_methods:
            cls._safe_setattr(model_cls, method_name, method)

    @classmethod
    def _inject_schema_forwarders(
        cls,
        model_cls: type[ModelT],
        manager: _ModelManager[ModelT],
    ) -> None:
        @classmethod  # type: ignore[misc]
        def create_schema(_model_cls: type[ModelT]) -> None:
            manager.create_schema()

        @classmethod  # type: ignore[misc]
        def drop_schema(_model_cls: type[ModelT]) -> None:
            manager.drop_schema()

        @classmethod  # type: ignore[misc]
        def schema_exists(_model_cls: type[ModelT]) -> bool:
            return manager.schema_exists()

        @classmethod  # type: ignore[misc]
        def truncate(_model_cls: type[ModelT]) -> None:
            manager.truncate()

        for name, method in [
            ("create_schema", create_schema),
            ("drop_schema", drop_schema),
            ("schema_exists", schema_exists),
            ("truncate", truncate),
        ]:
            cls._safe_setattr(model_cls, name, method)
