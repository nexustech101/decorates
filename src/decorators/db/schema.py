"""
db_registry.schema
~~~~~~~~~~~~~~~~~~
Safe, additive schema evolution without a full migration framework.

Philosophy
----------
Full migrations (Alembic) are out of scope for v1, but completely ad-hoc
``DROP + CREATE`` workflows are too destructive for production.  This module
provides a middle ground:

* ``create_schema()``   — CREATE TABLE IF NOT EXISTS  (always safe)
* ``drop_schema()``     — DROP TABLE  (explicit, intentional)
* ``truncate()``        — DELETE all rows  (no DDL)
* ``schema_exists()``   — inspection only
* ``add_column()``      — ADD COLUMN  (non-destructive, SQLite-safe)
* ``rename_table()``    — RENAME TABLE  (SQLite-safe)

Limitations
-----------
* SQLite does not support DROP COLUMN or RENAME COLUMN before 3.35.  The
  helpers below raise ``MigrationError`` when the operation is unsupported
  rather than silently doing nothing.
* This is **not** a replacement for Alembic in projects that need full
  downgrade support.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import Column, MetaData, Table, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from decorators.db.exceptions import MigrationError, SchemaError
from decorators.db.typing_utils import sqlalchemy_type_for_annotation


class SchemaManager:
    """
    Handles DDL operations for a single table.

    Parameters
    ----------
    engine:     The SQLAlchemy engine to use.
    table:      The SQLAlchemy Table object defining the target schema.
    table_name: String name (redundant with table.name, kept for clarity).
    """

    def __init__(self, engine: Engine, table: Table, table_name: str) -> None:
        self._engine = engine
        self._table = table
        self._table_name = table_name

    # ------------------------------------------------------------------
    # Core DDL
    # ------------------------------------------------------------------

    def create_schema(self) -> None:
        """CREATE TABLE IF NOT EXISTS — always idempotent."""
        try:
            self._table.metadata.create_all(self._engine, tables=[self._table])
        except SQLAlchemyError as exc:
            raise SchemaError(
                f"Failed to create schema for '{self._table_name}'."
            ) from exc

    def drop_schema(self) -> None:
        """DROP TABLE — irreversible. Caller is responsible for data safety."""
        try:
            self._table.metadata.drop_all(self._engine, tables=[self._table])
        except SQLAlchemyError as exc:
            raise SchemaError(
                f"Failed to drop schema for '{self._table_name}'."
            ) from exc

    def schema_exists(self) -> bool:
        """Return True when the backing table already exists in the database."""
        return inspect(self._engine).has_table(self._table_name)

    def truncate(self) -> None:
        """
        Delete every row without touching the schema.

        Uses DELETE (not TRUNCATE) for broad SQLite compatibility.
        """
        try:
            with self._engine.begin() as conn:
                conn.execute(self._table.delete())
        except SQLAlchemyError as exc:
            raise SchemaError(f"Failed to truncate '{self._table_name}'.") from exc

    # ------------------------------------------------------------------
    # Additive evolution (non-destructive)
    # ------------------------------------------------------------------

    def add_column(self, column_name: str, annotation: Any, *, nullable: bool = True) -> None:
        """
        Add *column_name* to the live table if it does not already exist.

        This is an **additive** operation only — it never drops or modifies
        existing columns.  New columns are always nullable unless the
        database can supply a DEFAULT value (which you should pass via
        ``annotation``).

        Raises
        ------
        MigrationError
            If the column already exists (use ``schema_exists`` checks to
            guard idempotency in scripts).
        SchemaError
            If the underlying DDL fails.
        """
        inspector = inspect(self._engine)
        existing = {col["name"] for col in inspector.get_columns(self._table_name)}

        if column_name in existing:
            raise MigrationError(
                f"Column '{column_name}' already exists on '{self._table_name}'. "
                "add_column() is not idempotent by design — guard with schema_exists() "
                "or use ensure_column() for safe re-entrant scripts."
            )

        sa_type = sqlalchemy_type_for_annotation(annotation)
        col_ddl = f'ALTER TABLE "{self._table_name}" ADD COLUMN "{column_name}" {sa_type}'
        if not nullable:
            # SQLite requires a DEFAULT when adding NOT NULL columns
            col_ddl += " NOT NULL DEFAULT ''"

        try:
            with self._engine.begin() as conn:
                conn.execute(text(col_ddl))
        except (SQLAlchemyError, OperationalError) as exc:
            raise SchemaError(
                f"Failed to add column '{column_name}' to '{self._table_name}'."
            ) from exc

    def ensure_column(self, column_name: str, annotation: Any, *, nullable: bool = True) -> bool:
        """
        Add *column_name* only if it does not already exist.

        Returns True when the column was added, False when it already existed.
        This is the safe, idempotent variant suitable for startup scripts and
        migration runners.
        """
        inspector = inspect(self._engine)
        existing = {col["name"] for col in inspector.get_columns(self._table_name)}
        if column_name in existing:
            return False
        self.add_column(column_name, annotation, nullable=nullable)
        return True

    def rename_table(self, new_name: str) -> None:
        """
        Rename the table.  Supported on SQLite 3.26+ and all major backends.

        Note: This does *not* update the in-process ``Table`` object or the
        registry's ``table_name``.  You must recreate the registry after
        renaming if you intend to keep using it.
        """
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{self._table_name}" RENAME TO "{new_name}"')
                )
        except (SQLAlchemyError, OperationalError) as exc:
            raise SchemaError(
                f"Failed to rename '{self._table_name}' to '{new_name}'."
            ) from exc

    def column_names(self) -> list[str]:
        """Return the current column names from live database inspection."""
        inspector = inspect(self._engine)
        return [col["name"] for col in inspector.get_columns(self._table_name)]

    def sqlite_version_supports_drop_column(self) -> bool:
        """
        Return True when the runtime SQLite version supports DROP COLUMN
        (requires SQLite ≥ 3.35.0, released 2021-03-12).
        """
        try:
            with self._engine.connect() as conn:
                row = conn.execute(text("SELECT sqlite_version()")).scalar()
                if row:
                    parts = [int(x) for x in str(row).split(".")]
                    return (parts[0], parts[1], parts[2] if len(parts) > 2 else 0) >= (3, 35, 0)
        except Exception:
            pass
        return False