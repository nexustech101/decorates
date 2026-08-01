"""
Construction, configuration, engine/table binding, and error mapping.

This mixin owns everything that exists before a single row is read or written:
the validated :class:`RegistryConfig`, the resolved :class:`FieldSpec` map, the
SQLAlchemy ``Table``, connection scoping, and the translation of driver errors
into this package's exception hierarchy.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import logging
from pathlib import Path
import time
from typing import Any, Generator, Generic, Mapping, TypeVar

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    event,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from registers.core.logging import log_exception
from registers.db.adapters import ModelAdapter, adapter_for
from registers.db.engine import DatabaseContext, dispose_engine, get_db_context
from registers.db.exceptions import (
    DuplicateKeyError,
    ModelRegistrationError,
    SchemaError,
    UniqueConstraintError,
)
from registers.db.fields import get_db_field_metadata
from registers.db.manager.context import (
    _ACTIVE_CONNECTIONS,
    _QUERY_LOG_SUBSCRIBERS,
)
from registers.db.metadata import RegistryConfig
from registers.db.schema import SchemaManager
from registers.db.specs import (
    FieldCoercionError,
    FieldSpec,
    build_field_spec,
)
from registers.db.typing_utils import field_allows_none

logger = logging.getLogger(__name__)
# Unbound: a model may be a pydantic BaseModel or a stdlib dataclass.
ModelT = TypeVar("ModelT")


def _validate_bool_flag(value: Any) -> None:
    """``field__is_null=`` takes a truthy flag; reject obvious mistakes only."""
    if not isinstance(value, bool) and value not in (0, 1, None):
        raise FieldCoercionError(value, "a boolean")


class _ManagerBase(Generic[ModelT]):
    """Construction, configuration, engine/table binding, and error mapping."""

    def __init__(
        self,
        model_cls: type[ModelT],
        database_url: str | Path | None = None,
        **options: Any,
    ) -> None:
        """
        Build a manager for *model_cls*.

        All registration options are validated by :meth:`RegistryConfig.build`, which
        owns the full option list. Keeping the signature open here means adding an
        option touches ``RegistryConfig`` and the two public decorator surfaces only —
        not four places, as it did when this signature was spelled out longhand.

        ``database_url`` stays positional because ``registers.cron.state`` constructs
        managers directly, and it reads better at the call site than a keyword.
        """
        options.setdefault("database_url", database_url)
        self.adapter: ModelAdapter = adapter_for(model_cls)
        self.config = RegistryConfig.build(model_cls, adapter=self.adapter, **options)

        self.model_cls = model_cls
        self.key_field = self.config.key_field
        self.table_name = self.config.table_name
        self.database_url = self.config.database_url
        self._fernet: Any = None

        # One pass over the model's fields resolves column types, validators, and
        # DB codecs. Everything downstream is a dict lookup rather than repeated
        # annotation introspection.
        self._specs: dict[str, FieldSpec] = self._build_field_specs()
        self._password_hash_fields = {
            name for name, spec in self._specs.items() if spec.hash_password
        }
        self._db_excluded_fields = {
            name for name, spec in self._specs.items() if spec.exclude_from_db
        }
        self._encrypted_fields = {
            name for name, spec in self._specs.items() if spec.encrypted
        }
        # Dataclass models have no validation of their own; hand them the coercers
        # resolved above so construct() type-checks writes the way Pydantic would.
        if hasattr(self.adapter, "bind_validators"):
            self.adapter.bind_validators(
                {name: spec.validate for name, spec in self._specs.items()},
                {name: spec.python_type for name, spec in self._specs.items()},
            )

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

        if self.config.auto_create:
            self._schema.create_schema(strict=False, include_all_metadata=True)

    @property
    def async_mode(self) -> bool:
        return self.config.async_mode

    @property
    def timestamps(self) -> bool:
        return self.config.timestamps

    @property
    def soft_delete(self) -> bool:
        return self.config.soft_delete

    @property
    def audit_log(self) -> bool:
        return self.config.audit_log

    @property
    def audit_log_table(self) -> str:
        return self.config.audit_log_table or f"{self.table_name}_audit"

    @property
    def tenant_field(self) -> str | None:
        return self.config.tenant_field

    @property
    def version_field(self) -> str | None:
        return self.config.version_field

    @property
    def encryption_key(self) -> Any:
        return self.config.encryption_key

    @property
    def log_queries(self) -> bool:
        return self.config.log_queries

    @property
    def slow_query_ms(self) -> int | None:
        return self.config.slow_query_ms

    @property
    def engine_options(self) -> dict[str, Any]:
        return dict(self.config.engine_options)

    @property
    def read_replica_url(self) -> str | None:
        return self.config.read_replica_url

    def get_registry(self) -> _ModelManager[ModelT]:
        """Return this manager instance for contract parity with other registries."""
        return self

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
        # Reads never write, so use connect() rather than begin(). Opening a write
        # transaction for every SELECT is pure overhead and is actively wrong against
        # a read replica.
        with self._read_engine.connect() as conn:
            yield conn

    def dispose(self) -> None:
        """
        Dispose the connection pool for this registry's database URL.

        After calling this, the registry is no longer usable.  Wire into the
        FastAPI ``lifespan`` shutdown hook for clean application teardown.
        """
        dispose_engine(self.database_url)

    def _build_table(self) -> Table:
        """
        Build (or reuse) the SQLAlchemy ``Table`` for this model.

        Tables are shared per database URL so foreign keys across models resolve
        against one ``MetaData``. That sharing is keyed on *table name*, so two
        different models pointing at the same table would silently make the second
        one adopt the first one's columns — its own fields would never get a column
        and every write would fail or lose data. Caught here instead.
        """
        with self._context.lock:
            existing = self._context.tables.get(self.table_name)
            if existing is not None:
                owner = self._context.table_owners.get(self.table_name)
                if owner is not None and owner is not self.model_cls:
                    self._assert_compatible_with_existing_table(existing, owner)
                return existing

            table = self._construct_table(self._metadata, self.table_name)
            self._context.tables[self.table_name] = table
            self._context.table_owners[self.table_name] = self.model_cls
            return table

    def _assert_compatible_with_existing_table(self, existing: Table, owner: type) -> None:
        """
        Reject a second model claiming a table whose shape it does not match.

        Sharing is fine when the schemas agree — rebinding a new model to a renamed
        table is a legitimate pattern, and both models describe the same columns.
        It is only a problem when they disagree, because the newcomer's extra or
        differently-typed fields would never get columns and its writes would fail
        or silently drop data.
        """
        candidate = self._construct_table(MetaData(), self.table_name)
        existing_shape = {
            column.name: column.type.compile(dialect=self._engine.dialect)
            for column in existing.columns
        }
        candidate_shape = {
            column.name: column.type.compile(dialect=self._engine.dialect)
            for column in candidate.columns
        }
        if existing_shape == candidate_shape:
            return

        differences = sorted(
            set(existing_shape) ^ set(candidate_shape)
        ) or sorted(
            name for name in candidate_shape
            if existing_shape.get(name) != candidate_shape[name]
        )
        raise ModelRegistrationError(
            f"Table '{self.table_name}' on '{self.database_url}' is already registered "
            f"to model '{owner.__name__}' with a different schema, so "
            f"'{self.model_cls.__name__}' cannot share it. Mismatched column(s): "
            f"{', '.join(differences)}. Two models on one table use whichever was "
            "registered first, which would silently drop the other's fields. Use a "
            "distinct table_name or a separate database URL.",
            model=self.model_cls.__name__,
            details={
                "table": self.table_name,
                "existing_model": owner.__name__,
                "mismatched_columns": differences,
            },
        )

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

    def _install_query_logging(self) -> None:
        """
        Attach timing listeners to this manager's engine.

        Engines are shared per database URL, so the install guard is keyed on the
        *engine* (not on the manager) — otherwise every manager backed by the same
        URL stacks another pair of listeners that are never removed. The engine
        keeps a registry of interested managers; the listeners consult it at emit
        time so a later registration with different thresholds still works.
        """
        if not self.log_queries and self.slow_query_ms is None:
            return

        subscribers: list[_ModelManager[Any]] | None = getattr(
            self._engine, _QUERY_LOG_SUBSCRIBERS, None
        )
        if subscribers is not None:
            if self not in subscribers:
                subscribers.append(self)
            return

        subscribers = [self]
        setattr(self._engine, _QUERY_LOG_SUBSCRIBERS, subscribers)

        @event.listens_for(self._engine, "before_cursor_execute")
        def _before_cursor_execute(conn, _cursor, _statement, _parameters, _context, _executemany):  # noqa: ANN001
            conn.info.setdefault("_registers_query_start", []).append(time.perf_counter())

        @event.listens_for(self._engine, "after_cursor_execute")
        def _after_cursor_execute(conn, _cursor, statement, _parameters, _context, _executemany):  # noqa: ANN001
            starts = conn.info.get("_registers_query_start") or []
            started = starts.pop() if starts else time.perf_counter()
            elapsed_ms = (time.perf_counter() - started) * 1000
            for manager in subscribers:
                if manager.log_queries or (
                    manager.slow_query_ms is not None and elapsed_ms >= manager.slow_query_ms
                ):
                    logger.info(
                        "registers.db query model=%s table=%s elapsed_ms=%.2f sql=%s",
                        manager.model_cls.__name__,
                        manager.table_name,
                        elapsed_ms,
                        statement,
                    )
                    break

    def _build_field_specs(self) -> dict[str, FieldSpec]:
        """
        Resolve every model field into a :class:`FieldSpec`, once.

        Encryption is passed in as lazily-bound closures so ``specs.py`` never has
        to import ``cryptography`` and the Fernet instance is built on first use
        rather than per value.
        """
        specs: dict[str, FieldSpec] = {}
        for field_name, view in self.adapter.fields().items():
            specs[field_name] = build_field_spec(
                field_name,
                view.annotation,
                nullable=field_allows_none(view),
                required=view.is_required(),
                default=None if view.is_required() else view.default,
                metadata=self.adapter.field_metadata(field_name),
                is_key_field=field_name == self.config.key_field,
                key_id_strategy=self.config.id_strategy,
                encrypt=self._encrypt_value,
                decrypt=self._decrypt_value,
            )
        return specs

    def _construct_table(self, metadata: MetaData, table_name: str) -> Table:
        unique_set = set(self.config.unique_fields)
        columns: list[Column[Any]] = []
        index_fields: list[str] = []
        columns_by_name: dict[str, Column[Any]] = {}

        for field_name, spec in self._specs.items():
            if not spec.is_stored:
                continue
            is_pk = field_name == self.key_field
            # The autoincrement PK column must be nullable so INSERTs can omit it.
            nullable = True if (is_pk and self.config.autoincrement) else spec.nullable

            col_args: list[Any] = []
            if spec.foreign_key:
                col_args.append(ForeignKey(str(spec.foreign_key)))

            is_unique = field_name in unique_set or spec.unique
            col_kwargs: dict[str, Any] = {
                "primary_key": is_pk,
                "nullable": nullable,
                "unique": is_unique,
                "autoincrement": bool(is_pk and self.config.autoincrement),
            }
            if not is_pk:
                col_kwargs.pop("autoincrement")

            column = Column(field_name, spec.sa_type, *col_args, **col_kwargs)
            columns.append(column)
            columns_by_name[field_name] = column

            if spec.index and not is_pk and not is_unique:
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
            self._context.table_owners.pop(table_name, None)
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

