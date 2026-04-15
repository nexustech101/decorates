from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import inspect

from conftest import backend_table_name
from decorates.db import MigrationError, database_registry


@pytest.mark.parametrize("backend_url", ["postgres", "mysql"], indirect=True)
class TestRenameTableBackends:
    def test_rename_table_updates_binding_and_crud(self, backend_url):
        table = backend_table_name("users")
        archive = f"{table}_archive"

        @database_registry(backend_url, table_name=table, key_field="id")
        class User(BaseModel):
            id: int | None = None
            email: str

        first = User.objects.create(email="alice@example.com")
        User.objects.rename_table(archive)

        assert User.objects.table_name == archive
        assert User.objects.config.table_name == archive
        assert User.schema_exists() is True

        second = User.objects.create(email="bob@example.com")
        assert User.objects.require(first.id).email == "alice@example.com"
        assert User.objects.require(second.id).email == "bob@example.com"
        assert User.objects.count() == 2

    def test_rename_table_rejects_existing_target_name(self, backend_url):
        primary = backend_table_name("users")
        target = backend_table_name("users_archive")

        @database_registry(backend_url, table_name=primary, key_field="id")
        class User(BaseModel):
            id: int | None = None
            email: str

        @database_registry(backend_url, table_name=target, key_field="id")
        class Existing(BaseModel):
            id: int | None = None
            email: str

        User.objects.create(email="alice@example.com")
        Existing.objects.create(email="already@archive.example")

        with pytest.raises(MigrationError):
            User.objects.rename_table(target)

        assert User.objects.table_name == primary
        assert User.schema_exists() is True
        assert User.objects.count() == 1

    def test_add_column_after_rename_targets_new_table(self, backend_url):
        table = backend_table_name("profiles")
        archive = f"{table}_archive"

        @database_registry(backend_url, table_name=table, key_field="id")
        class Profile(BaseModel):
            id: int | None = None
            name: str

        Profile.objects.create(name="Alice")
        Profile.objects.rename_table(archive)
        Profile.objects.add_column("nickname", str | None, nullable=True)

        inspector = inspect(Profile.objects._engine)
        columns = {col["name"] for col in inspector.get_columns(archive)}

        assert "nickname" in columns
        assert Profile.objects.count() == 1
