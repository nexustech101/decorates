"""
Focused edge-case coverage for the database registry.

This module complements the broad CRUD test suite with more specification-
oriented checks around primary-key policy, schema mapping integrity, and
data-validation boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, computed_field
from sqlalchemy import inspect, text

from decorators.db import (
    HasManyThrough,
    ImmutableFieldError,
    InvalidQueryError,
    InvalidPrimaryKeyAssignmentError,
    UniqueConstraintError,
    database_registry,
    dispose_all,
    is_password_hash,
)


@pytest.fixture(autouse=True)
def _dispose_engines():
    """Ensure engine state does not leak between tests."""
    yield
    dispose_all()


def _url(tmp_path: Path, name: str = "edge") -> str:
    return f"sqlite:///{tmp_path / f'{name}.db'}"


class TestSchemaMappingIntegrity:
    def test_alias_private_and_computed_fields_do_not_become_columns(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="widgets", key_field="id")
        class Widget(BaseModel):
            id: int | None = None
            name: str = Field(alias="widget_name")
            is_active: bool = True
            _cache: str = PrivateAttr(default="cached")

            @computed_field
            @property
            def label(self) -> str:
                return self.name.upper()

        columns = inspect(Widget.objects._engine).get_columns("widgets")
        column_names = [column["name"] for column in columns]

        assert column_names == ["id", "name", "is_active"]
        assert "widget_name" not in column_names
        assert "label" not in column_names
        assert "_cache" not in column_names

    def test_optional_and_defaulted_fields_keep_expected_nullability(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="profiles", key_field="id")
        class Profile(BaseModel):
            id: int | None = None
            display_name: str
            nickname: str | None = None
            is_active: bool = True

        columns = inspect(Profile.objects._engine).get_columns("profiles")
        nullable = {column["name"]: column["nullable"] for column in columns}

        assert nullable["id"] is True
        assert nullable["display_name"] is False
        assert nullable["nickname"] is True
        assert nullable["is_active"] is False


class TestDataIntegrityBoundaries:
    def test_default_int_id_is_database_assigned_and_round_trips(self, tmp_path):
        @database_registry(
            _url(tmp_path),
            table_name="users",
            key_field="id",
            unique_fields=["email"],
        )
        class User(BaseModel):
            id: int | None = None
            name: str
            email: str

        alice = User.objects.create(name="Alice", email="alice@example.com")
        bob = User.objects.create(name="Bob", email="bob@example.com")

        assert alice.id == 1
        assert bob.id == 2
        assert User.objects.require(alice.id).email == "alice@example.com"

    def test_unique_constraint_is_normalized_for_auto_pk_models(self, tmp_path):
        @database_registry(
            _url(tmp_path),
            table_name="users",
            key_field="id",
            unique_fields=["email"],
        )
        class User(BaseModel):
            id: int | None = None
            email: str

        User.objects.create(email="alice@example.com")

        with pytest.raises(UniqueConstraintError):
            User.objects.create(email="alice@example.com")

    def test_post_read_validation_surfaces_invalid_database_rows(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="events", key_field="id")
        class Event(BaseModel):
            id: int | None = None
            score: int

        with Event.objects._engine.begin() as conn:
            conn.execute(text("INSERT INTO events (score) VALUES ('oops')"))

        with pytest.raises(ValidationError):
            Event.objects.require(1)

    def test_password_fields_are_hashed_automatically(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="accounts", key_field="id")
        class Account(BaseModel):
            id: int | None = None
            email: str
            password: str

        account = Account.objects.create(email="alice@example.com", password="secret123")

        assert account.password != "secret123"
        assert is_password_hash(account.password)
        assert account.verify_password("secret123") is True
        assert account.verify_password("wrong-password") is False

    def test_password_is_rehashed_only_when_changed(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="accounts", key_field="id")
        class Account(BaseModel):
            id: int | None = None
            email: str
            password: str

        account = Account.objects.create(email="alice@example.com", password="secret123")
        original_hash = account.password

        account.email = "alicia@example.com"
        account.save()
        assert account.password == original_hash

        account.password = "new-secret"
        account.save()
        assert account.password != original_hash
        assert is_password_hash(account.password)
        assert account.verify_password("new-secret") is True

    def test_update_where_hashes_password_updates(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="accounts", key_field="id")
        class Account(BaseModel):
            id: int | None = None
            email: str
            password: str

        account = Account.objects.create(email="alice@example.com", password="secret123")
        updated = Account.objects.update_where({"id": account.id}, password="rotated-secret")

        assert len(updated) == 1
        assert updated[0].password != "rotated-secret"
        assert is_password_hash(updated[0].password)
        assert updated[0].verify_password("rotated-secret") is True


class TestSpecifiedPrimaryKeyPolicy:
    def test_auto_pk_rejects_explicit_id_assignment(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        with pytest.raises(InvalidPrimaryKeyAssignmentError):
            User.objects.create(id=10, name="Alice")

    def test_primary_key_mutation_is_rejected(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        user = User.objects.create(name="Alice")
        user.id = 999

        with pytest.raises(ImmutableFieldError):
            user.save()

    def test_upsert_without_id_uses_unique_fields(self, tmp_path):
        @database_registry(
            _url(tmp_path),
            table_name="users",
            key_field="id",
            unique_fields=["email"],
        )
        class User(BaseModel):
            id: int | None = None
            name: str
            email: str

        User.objects.create(name="Alice", email="alice@example.com")
        updated = User.objects.upsert(name="Alicia", email="alice@example.com")

        assert User.objects.count() == 1
        assert updated.name == "Alicia"
        assert User.objects.require(1).name == "Alicia"

    def test_deleted_autoincrement_ids_are_not_reused(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        first = User.objects.create(name="Alice")
        second = User.objects.create(name="Bob")
        User.objects.delete(second.id)
        third = User.objects.create(name="Carol")

        assert first.id == 1
        assert second.id == 2
        assert third.id == 3


class TestQueryAndRelationshipGaps:
    def test_filter_rejects_values_with_incorrect_field_types(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="people", key_field="id")
        class Person(BaseModel):
            id: int | None = None
            age: int

        Person.objects.create(age=42)

        with pytest.raises(InvalidQueryError):
            Person.objects.filter(age="abc")

    def test_many_to_many_results_are_deduplicated(self, tmp_path):
        url = _url(tmp_path, "relations")

        @database_registry(url, table_name="tags", key_field="id")
        class Tag(BaseModel):
            id: int | None = None
            name: str

        @database_registry(url, table_name="post_tags", key_field="id")
        class PostTag(BaseModel):
            id: int | None = None
            post_id: int
            tag_id: int

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int | None = None
            title: str

        Post.tags = HasManyThrough(
            Tag,
            through=PostTag,
            source_key="post_id",
            target_key="tag_id",
        )

        post = Post.objects.create(title="SQLAlchemy Guide")
        tag = Tag.objects.create(name="python")
        PostTag.objects.create(post_id=post.id, tag_id=tag.id)
        PostTag.objects.create(post_id=post.id, tag_id=tag.id)

        related = post.tags

        assert len(related) == 1
        assert related[0].id == tag.id
