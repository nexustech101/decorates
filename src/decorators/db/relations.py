"""
db_registry.relations
~~~~~~~~~~~~~~~~~~~~~
Explicit, lazy-loaded relationship descriptors.

Usage pattern
-------------
Relationships must be **assigned after** the class body is executed.
This is necessary because Pydantic v2's metaclass validates the class body
at class-creation time and rejects unannotated non-field attributes.

By assigning after the class (and after ``@database_manager`` decoration),
Pydantic's metaclass has already run and we can safely attach descriptors::

    @database_manager("app.db", table_name="authors", key_field="id")
    class Author(BaseModel):
        id: int | None = None
        name: str

    @database_manager("app.db", table_name="posts", key_field="id")
    class Post(BaseModel):
        id: int | None = None
        author_id: int
        title: str

    # Declare relationships AFTER both classes are decorated
    Author.posts = HasMany(Post, foreign_key="author_id")
    Post.author  = BelongsTo(Author, local_key="author_id")

This pattern also naturally solves forward-reference ordering — related
models must exist before you can reference them in a relationship, which is
the same constraint you'd face with inline declarations anyway.

Relationship types
------------------
* :class:`HasMany`         — one-to-many  (Author → Posts)
* :class:`BelongsTo`       — many-to-one  (Post → Author)
* :class:`HasManyThrough`  — many-to-many via join table (Post ↔ Tag)

All relationships are:
* **Lazy-loaded** — no query runs until the descriptor is accessed.
* **Read-only** — assignment raises :class:`RelationshipError`.
* **Self-validating** — misconfiguration is detected at access time with
  a clear error message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decorators.db.exceptions import RelationshipError

if TYPE_CHECKING:
    from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Base descriptor
# ---------------------------------------------------------------------------

class _BaseRelationship:
    """
    Common validation and attribute-name bookkeeping for all relationship
    descriptors.

    ``__set_name__`` is called automatically by Python when the descriptor is
    assigned as a class attribute, giving us the attribute name for use in
    error messages.  Because we also support post-class assignment
    (``Author.posts = HasMany(...)``), we fall back to ``'<unbound>'`` until
    ``__set_name__`` is called.
    """

    _attr_name: str = "<unbound>"

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr_name = name

    def __set__(self, obj: Any, value: Any) -> None:
        raise RelationshipError(
            f"Relationship '{self._attr_name}' is read-only. "
            "To associate records, use the related model's manager directly "
            f"(e.g. Post.objects.create(author_id=...))."
        )

    def _get_manager(self, model_cls: Any, manager_attr: str = "objects") -> Any:
        """Retrieve the DatabaseRegistry attached to *model_cls*."""
        manager = getattr(model_cls, manager_attr, None)
        if manager is None:
            raise RelationshipError(
                f"Model '{model_cls.__name__}' has no '{manager_attr}' manager. "
                "Make sure it is decorated with @database_manager before "
                f"the relationship '{self._attr_name}' is accessed."
            )
        return manager


# ---------------------------------------------------------------------------
# HasMany  (one-to-many)
# ---------------------------------------------------------------------------

class HasMany(_BaseRelationship):
    """
    Declares a one-to-many relationship.

    The *foreign_key* is the column on the **related** model that stores the
    primary key of **this** model.

    Example::

        @database_manager("app.db", table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int | None = None
            name: str

        @database_manager("app.db", table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int | None = None
            author_id: int
            title: str

        Author.posts = HasMany(Post, foreign_key="author_id")

        author = Author.objects.require(1)
        author.posts  # → list[Post]

    Parameters
    ----------
    related_model:
        The model class on the **many** side of the relationship.
    foreign_key:
        The field name on *related_model* that stores this model's primary key.
    """

    def __init__(self, related_model: type, *, foreign_key: str) -> None:
        self._related_model = related_model
        self._foreign_key = foreign_key

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        # Accessed on the class itself → return the descriptor for introspection
        if obj is None:
            return self

        manager = self._get_manager(self._related_model)

        if self._foreign_key not in self._related_model.model_fields:
            raise RelationshipError(
                f"HasMany foreign_key '{self._foreign_key}' is not a field on "
                f"'{self._related_model.__name__}'. "
                f"Available fields: {list(self._related_model.model_fields.keys())}"
            )

        # Determine the local primary key value
        local_manager = getattr(type(obj), "objects", None)
        local_key_field = local_manager.config.key_field if local_manager else "id"
        local_key_value = getattr(obj, local_key_field)

        return manager.filter(**{self._foreign_key: local_key_value})


# ---------------------------------------------------------------------------
# BelongsTo  (many-to-one / inverse of HasMany)
# ---------------------------------------------------------------------------

class BelongsTo(_BaseRelationship):
    """
    Declares a many-to-one relationship.

    The *local_key* is the column on **this** model that stores the foreign key
    pointing at the related model's primary key.

    Example::

        Post.author = BelongsTo(Author, local_key="author_id")

        post = Post.objects.require(1)
        post.author  # → Author | None

    Parameters
    ----------
    related_model:
        The model class on the **one** side of the relationship.
    local_key:
        The field name on **this** model that stores the foreign key.
    """

    def __init__(self, related_model: type, *, local_key: str) -> None:
        self._related_model = related_model
        self._local_key = local_key

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self

        manager = self._get_manager(self._related_model)

        local_fields = type(obj).model_fields
        if self._local_key not in local_fields:
            raise RelationshipError(
                f"BelongsTo local_key '{self._local_key}' is not a field on "
                f"'{type(obj).__name__}'. "
                f"Available fields: {list(local_fields.keys())}"
            )

        fk_value = getattr(obj, self._local_key)
        if fk_value is None:
            return None

        return manager.get(fk_value)


# ---------------------------------------------------------------------------
# HasManyThrough  (many-to-many via join table)
# ---------------------------------------------------------------------------

class HasManyThrough(_BaseRelationship):
    """
    Declares a many-to-many relationship via an explicit join table.

    Example::

        @database_manager("app.db", table_name="post_tags", key_field="id")
        class PostTag(BaseModel):
            id: int | None = None
            post_id: int
            tag_id: int

        Post.tags = HasManyThrough(Tag, through=PostTag,
                                   source_key="post_id", target_key="tag_id")

        post = Post.objects.require(1)
        post.tags  # → list[Tag]

    Parameters
    ----------
    related_model:
        The model class on the far side of the relationship.
    through:
        The join-table model.  Must have fields named *source_key* and
        *target_key*.
    source_key:
        Column on *through* that stores **this** model's primary key.
    target_key:
        Column on *through* that stores *related_model*'s primary key.
    """

    def __init__(
        self,
        related_model: type,
        *,
        through: type,
        source_key: str,
        target_key: str,
    ) -> None:
        self._related_model = related_model
        self._through = through
        self._source_key = source_key
        self._target_key = target_key

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self

        through_manager = self._get_manager(self._through)
        related_manager = self._get_manager(self._related_model)

        # Validate join-table fields
        through_fields = self._through.model_fields
        for key_attr in (self._source_key, self._target_key):
            if key_attr not in through_fields:
                raise RelationshipError(
                    f"HasManyThrough key '{key_attr}' is not a field on "
                    f"through-model '{self._through.__name__}'. "
                    f"Available fields: {list(through_fields.keys())}"
                )

        # Determine local primary key
        local_manager = getattr(type(obj), "objects", None)
        local_key_field = local_manager.config.key_field if local_manager else "id"
        local_key_value = getattr(obj, local_key_field)

        # Step 1: find all join-table rows matching this instance
        join_rows = through_manager.filter(**{self._source_key: local_key_value})
        if not join_rows:
            return []

        # Step 2: collect target PKs
        related_ids = [getattr(row, self._target_key) for row in join_rows]

        # Step 3: fetch related records; skip any that were deleted
        records: list[Any] = []
        seen: set[Any] = set()
        for pk in related_ids:
            if pk in seen:
                continue
            seen.add(pk)
            record = related_manager.get(pk)
            if record is not None:
                records.append(record)
        return records
