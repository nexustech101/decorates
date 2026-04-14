"""
db_registry.decorators
~~~~~~~~~~~~~~~~~~~~~~
``@database_registry`` — the single public decoration entrypoint.

Architecture change from v0.1
------------------------------
The previous version injected 20+ classmethods directly onto the model,
fighting against Pydantic's metaclass machinery and polluting ``__dict__``.

This version instead:

1. Creates a ``DatabaseRegistry`` and attaches it as ``Model.objects``
   (or the configured ``manager_attr``).  All CRUD lives there.

2. Injects only **three instance methods** via a lightweight mixin:
   ``save()``, ``delete()``, and ``refresh()``.  These are genuine instance
   operations that need access to ``self``, so living on the instance is
   correct.

3. Exposes schema helpers (``create_schema``, ``drop_schema``, etc.) as
   ``@classmethod`` forwarders on the model so the FastAPI pattern
   ``User.create_schema()`` continues to work.

Usage
-----

::

    @database_registry("app.db", table_name="users", key_field="id",
                       autoincrement=True, unique_fields=["email"])
    class User(BaseModel):
        id: int | None = None
        name: str
        email: str

    # Manager API — all CRUD
    user  = User.objects.create(name="Alice", email="a@example.com")
    users = User.objects.filter(name="Alice")
    page  = User.objects.filter(limit=10, offset=0)
    count = User.objects.count()

    # Instance API — save / delete / refresh
    user.name = "Alicia"
    user.save()
    fresh = user.refresh()
    user.delete()

    # Schema helpers (class-level forwarders over User.objects)
    User.create_schema()
    User.schema_exists()
    User.truncate()

Collision detection
-------------------
The decorator checks both ``model_cls.__dict__`` **and**
``model_cls.model_fields`` before injecting any attribute.  Pydantic fields
are not present in ``__dict__`` but are real attributes on the class, so
both checks are needed to catch all naming conflicts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from decorators.db.exceptions import ModelRegistrationError
from decorators.db.registry import DatabaseRegistry
from decorators.db.security import verify_password as verify_password_value
from decorators.db.typing_utils import annotation_is_integer, field_allows_none

ModelT = TypeVar("ModelT", bound=BaseModel)


# ---------------------------------------------------------------------------
# Public decorator
# ---------------------------------------------------------------------------

def database_registry(
    database_url: str | Path | None = None,
    *,
    table_name: str | None = None,
    key_field: str = "id",
    manager_attr: str = "objects",
    auto_create: bool = True,
    autoincrement: bool = False,
    unique_fields: list[str] | tuple[str, ...] = (),
) -> Callable[[type[ModelT]], type[ModelT]]:
    """
    Decorate a Pydantic ``BaseModel`` with a SQLite-backed persistence manager.

    The registry is attached as ``Model.objects`` (configurable via
    *manager_attr*).  Three instance methods are also injected: ``save()``,
    ``delete()``, and ``refresh()``.

    Parameters
    ----------
    database_url:
        SQLAlchemy-compatible URL or a bare file path.
        Defaults to ``<table_name>.db`` in the current directory.
    table_name:
        SQL table name.  Defaults to the snake_case plural of the model name.
    key_field:
        Primary key field name.  Must be a field on the model.
    manager_attr:
        Attribute name under which the registry is attached.  Defaults to
        ``"objects"`` (``User.objects``).
    auto_create:
        Create the table automatically on decoration if it does not exist.
    autoincrement:
        Use a database-generated integer primary key.  Requires the key field
        to be typed ``int | None = None``.
    unique_fields:
        Field names that should have UNIQUE constraints in the database.

    Raises
    ------
    ModelRegistrationError
        If the target class is not a Pydantic ``BaseModel``, is a stdlib
        ``@dataclass``, or if *manager_attr* / instance method names collide
        with existing model fields or class attributes.
    ConfigurationError
        If any configuration option references a non-existent field or is
        logically invalid.
    """

    def decorator(model_cls: type[ModelT]) -> type[ModelT]:
        _assert_valid_model(model_cls)

        # Auto-enable autoincrement when the key field is an integer type
        # and the caller hasn't explicitly opted out via a non-"id" key_field.
        resolved_autoincrement = autoincrement
        if not resolved_autoincrement and key_field == "id":
            field = model_cls.model_fields.get(key_field)
            if (
                field is not None
                and annotation_is_integer(field.annotation)
                and field_allows_none(field)
            ):
                resolved_autoincrement = True

        manager = DatabaseRegistry(
            model_cls,
            database_url=database_url,
            table_name=table_name,
            key_field=key_field,
            manager_attr=manager_attr,
            auto_create=auto_create,
            autoincrement=resolved_autoincrement,
            unique_fields=tuple(unique_fields),
        )

        # ---- Attach the manager ----------------------------------------
        _safe_setattr(model_cls, manager_attr, manager)

        # ---- Inject instance methods (save / delete / refresh) ---------
        _inject_instance_methods(model_cls, manager, key_field)

        # ---- Attach thin class-level schema forwarders -----------------
        _inject_schema_forwarders(model_cls, manager)

        return model_cls

    return decorator


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _assert_valid_model(model_cls: type) -> None:
    if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)):
        raise ModelRegistrationError(
            "@database_registry can only decorate Pydantic BaseModel subclasses."
        )
    if hasattr(model_cls, "__dataclass_fields__"):
        raise ModelRegistrationError(
            "Do not combine stdlib @dataclass with pydantic.BaseModel. "
            "Define the model as a plain `class User(BaseModel): ...`."
        )


def _safe_setattr(model_cls: type, name: str, value: Any) -> None:
    """
    Set *name* on *model_cls*, raising ``ModelRegistrationError`` if it
    would shadow a user-defined attribute or Pydantic field.

    We must check both ``model_cls.__dict__`` (for regular class attributes)
    **and** ``model_cls.model_fields`` (for Pydantic-declared fields), because
    Pydantic fields are not present in ``__dict__`` but are still real
    attributes accessible on the class.
    """
    in_dict = name in model_cls.__dict__
    in_pydantic_fields = name in model_cls.model_fields

    if in_dict or in_pydantic_fields:
        source = "a Pydantic field" if in_pydantic_fields else "a class attribute"
        raise ModelRegistrationError(
            f"Cannot attach '{name}' to '{model_cls.__name__}' — "
            f"it is already defined as {source} on the model.  "
            "Choose a different manager_attr or rename the conflicting attribute."
        )
    setattr(model_cls, name, value)


def _inject_instance_methods(
    model_cls: type[ModelT],
    manager: DatabaseRegistry,
    key_field: str,
) -> None:
    """
    Add ``save``, ``delete``, and ``refresh`` as instance methods.

    These are the only methods that genuinely belong on the instance because
    they operate on ``self``.  Everything else lives on ``Model.objects``.
    """

    def save(self: ModelT) -> ModelT:
        """
        Persist this instance using upsert semantics.

        An existing row (matched by primary key) is updated; a missing row is
        inserted.  Returns self after reflecting any DB-generated values.
        """
        updated = manager.save(self)
        # Reflect DB-generated values (e.g. autoincrement id) back onto self.
        # Use type(self).model_fields to avoid the deprecated instance-level access.
        for field in type(self).model_fields:
            object.__setattr__(self, field, getattr(updated, field))
        return self

    def delete(self: ModelT) -> bool:
        """Delete this instance's row from the database. Returns True if deleted."""
        return manager.delete(getattr(self, key_field))

    def refresh(self: ModelT) -> ModelT:
        """
        Return a fresh copy of this instance re-fetched from the database.

        Raises :class:`RecordNotFoundError` if the record no longer exists.
        """
        return manager.refresh(self)

    injected_methods = [("save", save), ("delete", delete), ("refresh", refresh)]

    if "password" in model_cls.model_fields:
        def verify_password(self: ModelT, candidate: str) -> bool:
            """Return True when *candidate* matches this model's stored password hash."""
            return verify_password_value(candidate, getattr(self, "password"))

        injected_methods.append(("verify_password", verify_password))

    for method_name, method in injected_methods:
        _safe_setattr(model_cls, method_name, method)


def _inject_schema_forwarders(
    model_cls: type[ModelT],
    manager: DatabaseRegistry,
) -> None:
    """
    Attach thin ``@classmethod`` forwarders for schema operations.

    These exist solely for the ``User.create_schema()`` convenience pattern
    used in FastAPI startup hooks and are explicitly documented as wrappers
    around ``User.objects``.
    """

    @classmethod  # type: ignore[misc]
    def create_schema(cls) -> None:
        """Create the backing table if it does not already exist."""
        manager.create_schema()

    @classmethod  # type: ignore[misc]
    def drop_schema(cls) -> None:
        """Drop the backing table. Irreversible."""
        manager.drop_schema()

    @classmethod  # type: ignore[misc]
    def schema_exists(cls) -> bool:
        """Return True when the backing table exists."""
        return manager.schema_exists()

    @classmethod  # type: ignore[misc]
    def truncate(cls) -> None:
        """Delete all rows without touching the schema."""
        manager.truncate()

    for name, method in [
        ("create_schema", create_schema),
        ("drop_schema", drop_schema),
        ("schema_exists", schema_exists),
        ("truncate", truncate),
    ]:
        _safe_setattr(model_cls, name, method)
