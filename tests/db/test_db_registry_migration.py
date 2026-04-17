"""
Focused migration and schema-evolution tests for functionals.db.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import inspect, text

from conftest import db_url
from functionals.db import database_registry


class TestSchemaMigrations:
    def test_create_schema_is_idempotent(self, tmp_path):
        @database_registry(
            db_url(tmp_path),
            table_name="users",
            key_field="id",
            auto_create=False,
        )
        class User(BaseModel):
            id: int
            name: str

        assert User.schema_exists() is False
        User.create_schema()
        User.create_schema()
        assert User.schema_exists() is True

    def test_add_non_nullable_column_backfills_existing_rows(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        User.objects.add_column("email", str, nullable=False)

        with User.objects._engine.begin() as conn:
            value = conn.execute(text("SELECT email FROM users WHERE id = 1")).scalar_one()
        assert value == ""

    def test_add_non_nullable_integer_column_uses_numeric_backfill_on_sqlite(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        User.objects.add_column("age", int, nullable=False)

        with User.objects._engine.begin() as conn:
            value, sqlite_type = conn.execute(
                text("SELECT age, typeof(age) FROM users WHERE id = 1")
            ).one()

        assert value == 0
        assert sqlite_type == "integer"

    def test_ensure_column_is_reentrant_for_startup_migrations(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="events", key_field="id")
        class Event(BaseModel):
            id: int
            title: str

        assert Event.objects.ensure_column("archived_at", str | None, nullable=True) is True
        assert Event.objects.ensure_column("archived_at", str | None, nullable=True) is False
        assert "archived_at" in Event.objects.column_names()

    def test_rename_table_allows_rebinding_registry_to_new_name(self, tmp_path):
        url = db_url(tmp_path)

        @database_registry(url, table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        User.objects.rename_table("users_archive")

        db_inspector = inspect(User.objects._engine)
        assert db_inspector.has_table("users") is False
        assert db_inspector.has_table("users_archive") is True

        @database_registry(url, table_name="users_archive", key_field="id", auto_create=False)
        class ArchivedUser(BaseModel):
            id: int
            name: str

        archived = ArchivedUser.objects.require(1)
        assert archived.name == "Alice"
