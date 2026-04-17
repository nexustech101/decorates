"""
Spec-focused tests for the production-readiness feature slice.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from conftest import db_url
from functionals.db import InvalidQueryError, UniqueConstraintError, database_registry
from functionals.db.engine import dialect_insert


class TestDialectInsert:
    def test_sqlite_insert_factory_returns_dialect_insert(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="widgets", key_field="id")
        class Widget(BaseModel):
            id: int
            name: str

        stmt = dialect_insert(Widget.objects._engine, Widget.objects._table)
        assert stmt is not None
        assert hasattr(stmt, "on_conflict_do_update")

    def test_unknown_dialect_returns_none(self):
        fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="oracle"))
        assert dialect_insert(fake_engine, object()) is None


class TestQueryOperators:
    def test_filter_supports_comparison_and_membership_operators(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="people", key_field="id")
        class Person(BaseModel):
            id: int
            name: str
            age: int
            status: str
            nickname: str | None = None

        Person.objects.bulk_create(
            [
                {"id": 1, "name": "Alice", "age": 30, "status": "active", "nickname": None},
                {"id": 2, "name": "Bob", "age": 20, "status": "trial", "nickname": "B"},
                {"id": 3, "name": "Carol", "age": 40, "status": "active", "nickname": "C"},
            ]
        )

        assert [person.id for person in Person.objects.filter(age__gt=25, order_by="age")] == [1, 3]
        assert [person.id for person in Person.objects.filter(age__between=(20, 30), order_by="age")] == [2, 1]
        assert [person.id for person in Person.objects.filter(status__in=["active"])] == [1, 3]
        assert [person.id for person in Person.objects.filter(status__not_in=["active"], order_by="id")] == [2]
        assert [person.id for person in Person.objects.filter(nickname__is_null=True)] == [1]

    def test_filter_supports_string_operators(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="people", key_field="id")
        class Person(BaseModel):
            id: int
            name: str

        Person.objects.bulk_create(
            [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "ALICIA"},
                {"id": 3, "name": "Bob"},
            ]
        )

        assert [person.id for person in Person.objects.filter(name__like="%ce")] == [1]
        assert [person.id for person in Person.objects.filter(name__ilike="ali%", order_by="id")] == [1, 2]
        assert [person.id for person in Person.objects.filter(name__startswith="Alice")] == [1]
        assert [person.id for person in Person.objects.filter(name__endswith="ob")] == [3]
        assert [person.id for person in Person.objects.filter(name__contains="CIA", order_by="id")] == [2]

    def test_unknown_operator_raises(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="people", key_field="id")
        class Person(BaseModel):
            id: int
            age: int

        with pytest.raises(InvalidQueryError, match="Unknown query operator"):
            Person.objects.filter(age__approx=30)


class TestOrdering:
    def test_filter_all_first_and_last_support_ordering(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            role: str
            name: str

        User.objects.bulk_create(
            [
                {"id": 1, "role": "admin", "name": "Carol"},
                {"id": 2, "role": "user", "name": "Bob"},
                {"id": 3, "role": "admin", "name": "Alice"},
            ]
        )

        ordered = User.objects.all(order_by=["role", "name"])
        assert [(user.role, user.name) for user in ordered] == [
            ("admin", "Alice"),
            ("admin", "Carol"),
            ("user", "Bob"),
        ]
        assert User.objects.first(order_by="name").name == "Alice"  # type: ignore[union-attr]
        assert User.objects.last(order_by="name").name == "Carol"  # type: ignore[union-attr]

    def test_unknown_sort_field_raises(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        with pytest.raises(InvalidQueryError, match="Unknown sort field"):
            User.objects.all(order_by="missing")


class TestBulkOperations:
    def test_bulk_create_populates_autoincrement_keys(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        users = User.objects.bulk_create(
            [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Carol"},
            ]
        )

        assert [user.id for user in users] == [1, 2, 3]
        assert User.objects.count() == 3

    def test_bulk_create_rolls_back_on_duplicate(self, tmp_path):
        @database_registry(
            db_url(tmp_path),
            table_name="users",
            key_field="id",
            unique_fields=["email"],
        )
        class User(BaseModel):
            id: int | None = None
            email: str

        with pytest.raises(UniqueConstraintError):
            User.objects.bulk_create(
                [
                    {"email": "alice@example.com"},
                    {"email": "alice@example.com"},
                ]
            )

        assert User.objects.count() == 0

    def test_bulk_upsert_updates_existing_and_inserts_new(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        persisted = User.objects.bulk_upsert(
            [
                {"id": 1, "name": "Alicia"},
                {"id": 2, "name": "Bob"},
            ]
        )

        assert [user.id for user in persisted] == [1, 2]
        assert User.objects.require(1).name == "Alicia"
        assert User.objects.require(2).name == "Bob"
