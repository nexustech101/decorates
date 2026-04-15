"""
Decorator-driven persistence registry for Pydantic models.

Quick start
-----------

::

    from pydantic import BaseModel
    from decorates.db import database_registry

    @database_registry(
        "sqlite:///users.db",
        table_name="users",
        key_field="id",
        autoincrement=True,
        unique_fields=["email"],
    )
    class User(BaseModel):
        id: int | None = None
        name: str
        email: str

    # All persistence lives on the manager
    user  = User.objects.create(name="Alice", email="alice@example.com")
    users = User.objects.all()
    user.save()
    user.delete()

    # Schema helpers
    User.create_schema()
    User.schema_exists()
"""

from decorates.db.decorators import database_registry
from decorates.db.engine import dispose_all, dispose_engine, get_engine
from decorates.db.exceptions import (
    ConfigurationError,
    DuplicateKeyError,
    ImmutableFieldError,
    InvalidPrimaryKeyAssignmentError,
    InvalidQueryError,
    MigrationError,
    ModelRegistrationError,
    RecordNotFoundError,
    RegistryError,
    RelationshipError,
    SchemaError,
    UniqueConstraintError,
)
from decorates.db.registry import DatabaseRegistry
from decorates.db.relations import BelongsTo, HasMany, HasManyThrough
from decorates.db.schema import SchemaManager
from decorates.db.metadata import RegistryConfig
from decorates.db.fields import db_field
from decorates.db.security import hash_password, is_password_hash, verify_password

__all__ = [
    # Core
    "database_registry",
    "DatabaseRegistry",
    "db_field",
    "hash_password",
    "is_password_hash",
    "verify_password",
    # Relationships
    "HasMany",
    "BelongsTo",
    "HasManyThrough",
    # Schema evolution
    "SchemaManager",
    # Engine management
    "get_engine",
    "dispose_engine",
    "dispose_all",
    # Config
    "RegistryConfig",
    # Exceptions
    "RegistryError",
    "ConfigurationError",
    "ModelRegistrationError",
    "SchemaError",
    "MigrationError",
    "RelationshipError",
    "DuplicateKeyError",
    "InvalidPrimaryKeyAssignmentError",
    "ImmutableFieldError",
    "UniqueConstraintError",
    "RecordNotFoundError",
    "InvalidQueryError",
]
