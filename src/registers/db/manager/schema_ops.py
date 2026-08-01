"""
DDL surface: create, drop, truncate, add/ensure column, rename, diff, migrate.

Thin forwarders onto :class:`~registers.db.schema.SchemaManager`, plus the
table-rebinding dance that ``rename_table`` needs in order to be atomic with
respect to in-memory state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import (
    MetaData,
    Table,
    inspect,
    text,
)
from sqlalchemy.exc import SQLAlchemyError

from registers.db.manager.context import MIGRATION_LEDGER_TABLE

from registers.db.exceptions import (
    MigrationError,
    SchemaError,
)
from registers.db.schema import SchemaManager
from registers.db.specs import (
    FieldCoercionError,
)

logger = logging.getLogger(__name__)
# Unbound: a model may be a pydantic BaseModel or a stdlib dataclass.
ModelT = TypeVar("ModelT")


def _validate_bool_flag(value: Any) -> None:
    """``field__is_null=`` takes a truthy flag; reject obvious mistakes only."""
    if not isinstance(value, bool) and value not in (0, 1, None):
        raise FieldCoercionError(value, "a boolean")


class _SchemaOpsMixin(Generic[ModelT]):
    """DDL surface: create, drop, truncate, add/ensure column, rename, diff, migrate."""

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
        """
        Apply safe **additive** schema changes, or return the diff in dry-run mode.

        Adds missing columns only. Renames, drops, type changes, and data backfills
        are out of scope by design — those need review, ordering, and a rollback
        plan, which is what Alembic is for. ``diff_schema()`` will keep reporting
        them so drift stays visible rather than silently applied.

        Every applied column is recorded in the ``registers_schema_migrations``
        ledger, so what ran against a database is auditable after the fact.
        """
        diff = self.diff_schema()
        if dry_run or diff.ok:
            return diff

        applied: list[str] = []
        for column_name in diff.missing_columns or []:
            field = self.adapter.fields()[column_name]
            if self.ensure_column(
                column_name,
                field.annotation,
                nullable=self._column_nullable(column_name, field),
            ):
                applied.append(column_name)

        if applied:
            self._record_migration(
                name=f"add_columns:{self.table_name}:{','.join(sorted(applied))}"
            )
        return self.diff_schema()

    def _record_migration(self, *, name: str) -> None:
        """
        Append an entry to the migration ledger.

        Best-effort and non-fatal: a schema change that succeeded must not be
        reported as failed because the bookkeeping row could not be written.
        The failure is logged so it is not invisible.
        """
        from registers.db.manager.coordinator import DatabaseRegistry

        version = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
        try:
            DatabaseRegistry._ensure_migration_ledger(self._engine)
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        f"INSERT INTO {MIGRATION_LEDGER_TABLE} (version, name) "
                        "VALUES (:version, :name)"
                    ),
                    {"version": version, "name": name},
                )
        except SQLAlchemyError:
            logger.warning(
                "Applied schema change '%s' but could not record it in the "
                "'%s' ledger.",
                name,
                MIGRATION_LEDGER_TABLE,
                exc_info=True,
            )

    def applied_migrations(self) -> list[dict[str, Any]]:
        """
        Return ledger entries for this database, oldest first.

        Empty when nothing has been applied through ``migrate()`` — the ledger
        records what this library did, not migrations run by other tooling.
        """
        try:
            with self._engine.begin() as conn:
                rows = conn.execute(
                    text(
                        f"SELECT version, name, applied_at FROM {MIGRATION_LEDGER_TABLE} "
                        "ORDER BY version"
                    )
                ).mappings().all()
            return [dict(row) for row in rows]
        except SQLAlchemyError:
            # Ledger absent means nothing has been applied yet.
            return []

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

