"""
tests/test_db_registry.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Comprehensive test suite for db_registry v2.

Covers:
- Manager pattern (Model.objects)
- All CRUD operations
- Constraint enforcement
- Schema evolution
- Relationship descriptors (HasMany, BelongsTo, HasManyThrough)
- Concurrency safety
- Error semantics
- Edge cases (empty tables, missing fields, autoincrement, etc.)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from decorators.db import (
    BelongsTo,
    ConfigurationError,
    DatabaseRegistry,
    DuplicateKeyError,
    HasMany,
    HasManyThrough,
    InvalidQueryError,
    MigrationError,
    ModelRegistrationError,
    RecordNotFoundError,
    RelationshipError,
    UniqueConstraintError,
    database_registry,
    dispose_all,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(autouse=True)
def _dispose_engines():
    """Ensure all engine connections are cleaned up after each test."""
    yield
    dispose_all()


def _url(tmp_path: Path, name: str = "test") -> str:
    return f"sqlite:///{tmp_path / f'{name}.db'}"


# ===========================================================================
# Manager attachment
# ===========================================================================

class TestManagerAttachment:
    def test_objects_attr_is_database_registry(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        assert isinstance(User.objects, DatabaseRegistry)

    def test_custom_manager_attr(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="items", key_field="id",
                           manager_attr="db")
        class Item(BaseModel):
            id: int
            name: str

        assert isinstance(Item.db, DatabaseRegistry)
        assert not hasattr(Item, "objects")

    def test_manager_attr_collision_raises(self, tmp_path):
        with pytest.raises(ModelRegistrationError, match="already defined"):
            @database_registry(_url(tmp_path), table_name="x", key_field="id",
                               manager_attr="objects")
            class Clash(BaseModel):
                id: int
                objects: str = "oops"

    def test_instance_has_save_delete_refresh(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        user = User.objects.create(id=1, name="Alice")
        assert callable(user.save)  # Saving models in the database should be implicit, though. Maybe replace with user.update?
        assert callable(user.delete)
        assert callable(user.refresh)

    def test_schema_classmethods_exist(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        assert callable(User.create_schema)
        assert callable(User.drop_schema)
        assert callable(User.schema_exists)
        assert callable(User.truncate)

    def test_non_basemodel_raises(self, tmp_path):
        with pytest.raises(ModelRegistrationError):
            @database_registry(_url(tmp_path))
            class NotAModel:
                id: int = 1

    def test_dataclass_on_basemodel_raises(self, tmp_path):
        with pytest.raises(ModelRegistrationError):
            # Actually, using dataclass could be useful for some use cases (immutable models?)
            # We're using pydantic models for validation and type coercion
            @database_registry(_url(tmp_path))
            @dataclass
            class Bad(BaseModel):
                id: int


# ===========================================================================
# Configuration validation
# ===========================================================================

class TestConfiguration:
    def test_unknown_key_field_raises(self, tmp_path):
        with pytest.raises(ConfigurationError, match="key_field"):
            @database_registry(_url(tmp_path), key_field="nonexistent")
            class User(BaseModel):
                id: int

    def test_unknown_unique_field_raises(self, tmp_path):
        with pytest.raises(ConfigurationError, match="unique_fields"):
            @database_registry(_url(tmp_path), unique_fields=["ghost"])
            class User(BaseModel):
                id: int

    def test_duplicate_unique_fields_raises(self, tmp_path):
        with pytest.raises(ConfigurationError, match="duplicates"):
            @database_registry(_url(tmp_path), unique_fields=["name", "name"])
            class User(BaseModel):
                id: int
                name: str

    def test_autoincrement_requires_integer_key(self, tmp_path):
        with pytest.raises(ConfigurationError, match="integer"):
            @database_registry(_url(tmp_path), autoincrement=True)
            class Widget(BaseModel):
                id: str | None = None

    def test_autoincrement_requires_nullable_key(self, tmp_path):
        with pytest.raises(ConfigurationError, match="allow None"):
            @database_registry(_url(tmp_path), autoincrement=True)
            class Widget(BaseModel):
                id: int  # not nullable, no default


# ===========================================================================
# CRUD — happy paths
# ===========================================================================

class TestCreate:
    def test_create_returns_model_instance(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        user = User.objects.create(id=1, name="Alice")
        assert isinstance(user, User)
        assert user.id == 1
        assert user.name == "Alice"

    def test_autoincrement_assigns_generated_id(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id",
                           autoincrement=True)
        class User(BaseModel):
            id: int | None = None
            name: str

        u1 = User.objects.create(name="Alice")
        u2 = User.objects.create(name="Bob")
        assert u1.id == 1
        assert u2.id == 2

    def test_strict_create_is_alias(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        user = User.objects.strict_create(id=42, name="Carol")
        assert user.id == 42


class TestRead:
    def test_get_by_primary_key(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        user = User.objects.get(1)
        assert user is not None
        assert user.name == "Alice"

    def test_get_by_keyword(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        user = User.objects.get(name="Alice")
        assert user is not None
        assert user.id == 1

    def test_get_missing_returns_none(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        assert User.objects.get(999) is None

    def test_require_raises_when_missing(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        with pytest.raises(RecordNotFoundError):
            User.objects.require(999)

    def test_all_returns_all_rows(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        User.objects.create(id=2, name="Bob")
        assert len(User.objects.all()) == 2

    def test_get_all_alias(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        assert len(User.objects.get_all()) == 1

    def test_filter_with_criteria(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str
            role: str

        User.objects.create(id=1, name="Alice", role="admin")
        User.objects.create(id=2, name="Bob", role="user")
        User.objects.create(id=3, name="Carol", role="admin")

        admins = User.objects.filter(role="admin")
        assert len(admins) == 2
        assert all(u.role == "admin" for u in admins)

    def test_filter_with_limit_and_offset(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        for i in range(10):
            User.objects.create(id=i, name=f"User{i}")

        page = User.objects.filter(limit=3, offset=3)
        assert len(page) == 3

    def test_exists_true_and_false(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        assert User.objects.exists(id=1) is True
        assert User.objects.exists(id=99) is False

    def test_count_all_and_filtered(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            role: str

        User.objects.create(id=1, role="admin")
        User.objects.create(id=2, role="user")
        User.objects.create(id=3, role="admin")

        assert User.objects.count() == 3
        assert User.objects.count(role="admin") == 2

    def test_first_and_last(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="First")
        User.objects.create(id=2, name="Second")

        assert User.objects.first().name == "First"  # type: ignore[union-attr]
        assert User.objects.last().name == "Second"  # type: ignore[union-attr]

    def test_first_on_empty_returns_none(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        assert User.objects.first() is None


class TestUpdate:
    def test_save_updates_existing(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        user = User.objects.require(1)
        user.name = "Alicia"
        user.save()
        assert User.objects.require(1).name == "Alicia"

    def test_upsert_inserts_new(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        user = User.objects.upsert(id=1, name="Alice")
        assert User.objects.count() == 1
        assert user.name == "Alice"

    def test_upsert_updates_existing(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        User.objects.upsert(id=1, name="Alicia")
        assert User.objects.require(1).name == "Alicia"
        assert User.objects.count() == 1

    def test_update_where_returns_updated_records(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str
            role: str

        User.objects.create(id=1, name="Alice", role="user")
        User.objects.create(id=2, name="Bob", role="user")
        updated = User.objects.update_where({"role": "user"}, role="admin")
        assert len(updated) == 2
        assert all(u.role == "admin" for u in updated)

    def test_update_where_empty_criteria_raises(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        with pytest.raises(InvalidQueryError):
            User.objects.update_where({}, name="oops")

    def test_update_where_empty_updates_raises(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        with pytest.raises(InvalidQueryError):
            User.objects.update_where({"id": 1})

    def test_refresh_returns_fresh_data(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        user = User.objects.require(1)
        # Directly update in the DB behind the instance's back
        User.objects.update_where({"id": 1}, name="Alicia")
        fresh = user.refresh()
        assert fresh.name == "Alicia"


class TestDelete:
    def test_delete_by_instance(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        user = User.objects.require(1)
        result = user.delete()
        assert result is True
        assert User.objects.count() == 0

    def test_delete_missing_returns_false(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        assert User.objects.delete(999) is False

    def test_delete_where_returns_count(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            role: str

        User.objects.create(id=1, role="admin")
        User.objects.create(id=2, role="user")
        User.objects.create(id=3, role="admin")

        deleted = User.objects.delete_where(role="admin")
        assert deleted == 2
        assert User.objects.count() == 1

    def test_delete_where_no_criteria_raises(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int

        with pytest.raises(InvalidQueryError):
            User.objects.delete_where()


# ===========================================================================
# Constraint enforcement
# ===========================================================================

class TestConstraints:
    def test_create_duplicate_pk_raises_duplicate_key_error(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        with pytest.raises(DuplicateKeyError):
            User.objects.create(id=1, name="Bob")

    def test_unique_field_violation_raises_unique_constraint_error(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id",
                           unique_fields=["email"])
        class User(BaseModel):
            id: int
            email: str

        User.objects.create(id=1, email="alice@example.com")
        with pytest.raises(UniqueConstraintError):
            User.objects.create(id=2, email="alice@example.com")

    def test_invalid_field_in_filter_raises(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        with pytest.raises(InvalidQueryError, match="ghost"):
            User.objects.get(ghost="value")

    def test_positional_and_keyword_lookup_raises(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        with pytest.raises(InvalidQueryError):
            User.objects.get(1, name="Alice")

    def test_multiple_positional_args_raises(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        with pytest.raises(InvalidQueryError):
            User.objects.get(1, 2)


# ===========================================================================
# Schema operations
# ===========================================================================

class TestSchema:
    def test_schema_exists_after_creation(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        assert User.schema_exists() is True

    def test_schema_not_exists_after_drop(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.drop_schema()
        assert User.schema_exists() is False

    def test_truncate_removes_all_rows(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str

        User.objects.create(id=1, name="Alice")
        User.objects.create(id=2, name="Bob")
        User.truncate()
        assert User.objects.count() == 0

    def test_auto_create_false_skips_table_creation(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id",
                           auto_create=False)
        class User(BaseModel):
            id: int
            name: str

        assert User.schema_exists() is False
        User.create_schema()
        assert User.schema_exists() is True

    def test_add_column_adds_new_column(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="items", key_field="id")
        class Item(BaseModel):
            id: int
            name: str

        Item.objects.add_column("description", str, nullable=True)
        cols = Item.objects._schema.column_names()
        assert "description" in cols

    def test_add_column_duplicate_raises_migration_error(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="items", key_field="id")
        class Item(BaseModel):
            id: int
            name: str

        with pytest.raises(MigrationError):
            Item.objects.add_column("name", str)

    def test_ensure_column_is_idempotent(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="items", key_field="id")
        class Item(BaseModel):
            id: int
            name: str

        added_first = Item.objects.ensure_column("weight", float)
        added_second = Item.objects.ensure_column("weight", float)
        assert added_first is True
        assert added_second is False


# ===========================================================================
# Direct registry usage (without decorator)
# ===========================================================================

class TestDirectRegistry:
    def test_create_and_retrieve(self, tmp_path):
        class User(BaseModel):
            id: int | None = None
            email: str

        registry = DatabaseRegistry(
            User,
            _url(tmp_path),
            table_name="users",
            key_field="id",
            autoincrement=True,
            unique_fields=("email",),
        )

        alice = registry.create(email="a@example.com")
        assert alice.id == 1

        with pytest.raises(UniqueConstraintError):
            registry.create(email="a@example.com")

        registry.dispose()


# ===========================================================================
# Relationships
# ===========================================================================

class TestHasMany:
    def test_has_many_returns_related_records(self, tmp_path):
        url = _url(tmp_path)

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int
            author_id: int
            title: str

        @database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int
            name: str

        Author.posts = HasMany(Post, foreign_key="author_id")

        Author.objects.create(id=1, name="Alice")
        Post.objects.create(id=1, author_id=1, title="First Post")
        Post.objects.create(id=2, author_id=1, title="Second Post")
        Post.objects.create(id=3, author_id=2, title="Other Author's Post")

        author = Author.objects.require(1)
        posts = author.posts
        assert len(posts) == 2
        assert all(p.author_id == 1 for p in posts)

    def test_has_many_returns_empty_list_when_no_related(self, tmp_path):
        url = _url(tmp_path)

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int
            author_id: int
            title: str

        @database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int
            name: str

        Author.posts = HasMany(Post, foreign_key="author_id")

        Author.objects.create(id=1, name="Alice")
        author = Author.objects.require(1)
        assert author.posts == []

    def test_has_many_is_read_only(self, tmp_path):
        url = _url(tmp_path)

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int
            author_id: int
            title: str

        @database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int
            name: str

        Author.posts = HasMany(Post, foreign_key="author_id")

        Author.objects.create(id=1, name="Alice")
        author = Author.objects.require(1)
        # Pydantic's __setattr__ fires before our descriptor __set__, so the
        # assignment is blocked by either RelationshipError (class-level) or
        # ValueError (Pydantic instance-level). Both mean: read-only.
        with pytest.raises((RelationshipError, ValueError)):
            author.posts = []


class TestBelongsTo:
    def test_belongs_to_returns_parent(self, tmp_path):
        url = _url(tmp_path)

        @database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int
            name: str

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int
            author_id: int
            title: str

        Post.author = BelongsTo(Author, local_key="author_id")

        Author.objects.create(id=1, name="Alice")
        Post.objects.create(id=1, author_id=1, title="Hello World")

        post = Post.objects.require(1)
        parent = post.author
        assert parent is not None
        assert parent.name == "Alice"

    def test_belongs_to_returns_none_when_null_fk(self, tmp_path):
        url = _url(tmp_path)

        @database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int
            name: str

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int
            author_id: int | None = None
            title: str

        Post.author = BelongsTo(Author, local_key="author_id")

        Post.objects.create(id=1, author_id=None, title="Orphan Post")
        post = Post.objects.require(1)
        assert post.author is None


class TestHasManyThrough:
    def test_many_to_many_via_join_table(self, tmp_path):
        url = _url(tmp_path)

        @database_registry(url, table_name="tags", key_field="id")
        class Tag(BaseModel):
            id: int
            name: str

        @database_registry(url, table_name="post_tags", key_field="id")
        class PostTag(BaseModel):
            id: int
            post_id: int
            tag_id: int

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int
            title: str

        Post.tags = HasManyThrough(Tag, through=PostTag,
                                   source_key="post_id", target_key="tag_id")

        Tag.objects.create(id=1, name="python")
        Tag.objects.create(id=2, name="databases")
        Post.objects.create(id=1, title="SQLAlchemy Guide")

        PostTag.objects.create(id=1, post_id=1, tag_id=1)
        PostTag.objects.create(id=2, post_id=1, tag_id=2)

        post = Post.objects.require(1)
        tags = post.tags
        assert len(tags) == 2
        tag_names = {t.name for t in tags}
        assert tag_names == {"python", "databases"}

    def test_many_to_many_empty_when_no_joins(self, tmp_path):
        url = _url(tmp_path)

        @database_registry(url, table_name="tags", key_field="id")
        class Tag(BaseModel):
            id: int
            name: str

        @database_registry(url, table_name="post_tags", key_field="id")
        class PostTag(BaseModel):
            id: int
            post_id: int
            tag_id: int

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int
            title: str

        Post.tags = HasManyThrough(Tag, through=PostTag,
                                   source_key="post_id", target_key="tag_id")

        Post.objects.create(id=1, title="No Tags Yet")
        post = Post.objects.require(1)
        assert post.tags == []


# ===========================================================================
# Concurrency
# ===========================================================================

class TestConcurrency:
    def test_concurrent_inserts_all_succeed_with_unique_keys(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="counters", key_field="id")
        class Counter(BaseModel):
            id: int
            value: int

        errors: list[Exception] = []

        def insert(i: int) -> None:
            try:
                Counter.objects.create(id=i, value=i * 10)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=insert, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent inserts: {errors}"
        assert Counter.objects.count() == 20

    def test_concurrent_upserts_converge(self, tmp_path):
        """Concurrent upserts on the same key should not raise; last write wins."""
        @database_registry(_url(tmp_path), table_name="settings", key_field="id")
        class Setting(BaseModel):
            id: int
            value: str

        Setting.objects.create(id=1, value="initial")
        errors: list[Exception] = []

        def upsert_val(v: str) -> None:
            try:
                Setting.objects.upsert(id=1, value=v)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=upsert_val, args=(f"v{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # The row must still exist regardless of which value won
        assert Setting.objects.exists(id=1)


# ===========================================================================
# Type mapping
# ===========================================================================

class TestTypeMapping:
    def test_various_field_types_persist_correctly(self, tmp_path):
        from datetime import date, datetime

        @database_registry(_url(tmp_path), table_name="events", key_field="id")
        class Event(BaseModel):
            id: int
            name: str
            score: float
            active: bool
            created_on: date
            happened_at: datetime

        event = Event.objects.create(
            id=1,
            name="Launch",
            score=9.5,
            active=True,
            created_on=date(2024, 1, 15),
            happened_at=datetime(2024, 1, 15, 12, 0, 0),
        )

        fetched = Event.objects.require(1)
        assert fetched.name == "Launch"
        assert abs(fetched.score - 9.5) < 1e-6
        assert fetched.active is True

    def test_optional_fields_allow_none(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str
            nickname: str | None = None

        user = User.objects.create(id=1, name="Alice", nickname=None)
        fetched = User.objects.require(1)
        assert fetched.nickname is None

    def test_id_field_autoincrement(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str
            nickname: str | None = None

        # `id: int | None = None` opts into the default DB-assigned PK strategy.
        USERS = [
            {"name": "Alice", "nickname": None},  # Should generate id=1
            {"name": "Bobby", "nickname": None},  # Should generate id=2
            {"name": "Robbert", "nickname": None},  # Should generate id=3
        ]

        for user in USERS:
            User.objects.create(**user)

        users = User.objects.all()
        ids = [u.id for u in users]
        assert all(pk is not None for pk in ids)
        assert len(set(ids)) == 3

    def test_id_field_primary_key(self, tmp_path):
        @database_registry(_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int
            name: str
            nickname: str | None = None

        # @TODO: Test that id is actually the primary key and enforces uniqueness, even without autoincrement
        User.objects.create(id=1, name="Alice")
        with pytest.raises(DuplicateKeyError):
            User.objects.create(id=1, name="Bob")

        assert User.objects.count() == 1


# ===========================================================================
# In-memory database
# ===========================================================================

class TestInMemoryDatabase:
    def test_in_memory_url_works(self):
        @database_registry("sqlite:///:memory:", table_name="items", key_field="id")
        class Item(BaseModel):
            id: int
            name: str

        Item.objects.create(id=1, name="Widget")
        assert Item.objects.count() == 1

    def test_multiple_models_on_same_memory_db(self):
        url = "sqlite:///:memory:"

        @database_registry(url, table_name="cats", key_field="id")
        class Cat(BaseModel):
            id: int
            name: str

        @database_registry(url, table_name="dogs", key_field="id")
        class Dog(BaseModel):
            id: int
            name: str

        Cat.objects.create(id=1, name="Whiskers")
        Dog.objects.create(id=1, name="Rex")

        assert Cat.objects.count() == 1
        assert Dog.objects.count() == 1
