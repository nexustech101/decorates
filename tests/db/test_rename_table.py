from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import inspect

from conftest import db_url
from decorates.db import MigrationError, database_registry


class TestRenameTableStateSync:
    def test_rename_table_updates_registry_state(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        User.objects.create(name="Alice")
        User.objects.rename_table("users_archive")

        assert User.objects.table_name == "users_archive"
        assert User.objects.config.table_name == "users_archive"
        assert User.objects._table.name == "users_archive"
        assert User.objects._schema._table_name == "users_archive"
        assert User.schema_exists() is True

        inspector = inspect(User.objects._engine)
        assert inspector.has_table("users") is False
        assert inspector.has_table("users_archive") is True

    def test_rename_table_then_schema_exists_uses_new_name(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="events", key_field="id")
        class Event(BaseModel):
            id: int | None = None
            title: str

        assert Event.schema_exists() is True
        Event.objects.rename_table("events_archive")

        assert Event.schema_exists() is True

        inspector = inspect(Event.objects._engine)
        assert inspector.has_table("events") is False
        assert inspector.has_table("events_archive") is True

    def test_rename_table_preserves_existing_data(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="people", key_field="id")
        class Person(BaseModel):
            id: int | None = None
            name: str

        alice = Person.objects.create(name="Alice")
        bob = Person.objects.create(name="Bob")

        Person.objects.rename_table("people_archive")

        assert Person.objects.get(alice.id).name == "Alice"  # type: ignore[union-attr]
        assert Person.objects.require(bob.id).name == "Bob"
        assert [row.name for row in Person.objects.all(order_by="id")] == ["Alice", "Bob"]
        assert Person.objects.count() == 2

    def test_rename_table_allows_new_crud_operations(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="accounts", key_field="id")
        class Account(BaseModel):
            id: int | None = None
            email: str

        original = Account.objects.create(email="alice@example.com")
        Account.objects.rename_table("accounts_archive")

        created = Account.objects.create(email="bob@example.com")
        created.email = "robert@example.com"
        created.save()

        assert Account.objects.require(original.id).email == "alice@example.com"
        assert Account.objects.require(created.id).email == "robert@example.com"

        assert Account.objects.delete(original.id) is True
        assert Account.objects.count() == 1

    def test_rename_table_invalidates_old_table_references(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="items", key_field="id")
        class Item(BaseModel):
            id: int | None = None
            name: str

        Item.objects.create(name="A")
        Item.objects.rename_table("items_archive")

        assert Item.objects._table.name == "items_archive"
        assert set(Item.objects.column_names()) == {"id", "name"}
        assert Item.objects.exists(name="A") is True

    def test_rename_table_rejects_existing_target_name(self, tmp_path):
        url = db_url(tmp_path)

        @database_registry(url, table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        @database_registry(url, table_name="users_archive", key_field="id")
        class Archive(BaseModel):
            id: int | None = None
            name: str

        User.objects.create(name="Alice")
        Archive.objects.create(name="Existing")

        with pytest.raises(MigrationError):
            User.objects.rename_table("users_archive")

        assert User.objects.table_name == "users"
        assert User.schema_exists() is True
        assert User.objects.require(1).name == "Alice"

    def test_rename_table_failure_does_not_corrupt_registry_state(self, tmp_path, monkeypatch):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        User.objects.create(name="Alice")

        def fail_rebind(_table_name: str) -> None:
            raise RuntimeError("rebind failed")

        monkeypatch.setattr(User.objects, "_rebind_table_state", fail_rebind, raising=False)

        with pytest.raises(MigrationError):
            User.objects.rename_table("users_archive")

        assert User.objects.table_name == "users"
        assert User.schema_exists() is True
        assert User.objects.require(1).name == "Alice"
        assert User.objects.create(name="Bob").id is not None

    def test_bulk_upsert_after_rename(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            email: str

        first = User.objects.create(email="a@example.com")
        User.objects.rename_table("users_archive")

        rows = User.objects.bulk_upsert(
            [
                {"id": first.id, "email": "a+updated@example.com"},
                {"email": "b@example.com"},
            ]
        )

        assert len(rows) == 2
        assert User.objects.count() == 2
        assert User.objects.require(first.id).email == "a+updated@example.com"

    def test_transaction_after_rename_uses_new_table(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="logs", key_field="id")
        class Log(BaseModel):
            id: int
            message: str

        Log.objects.create(id=1, message="before")
        Log.objects.rename_table("logs_archive")

        with Log.objects.transaction() as conn:
            Log.objects._create_with_conn(conn, Log(id=2, message="during"))

        assert Log.objects.count() == 2
        assert Log.objects.require(2).message == "during"
