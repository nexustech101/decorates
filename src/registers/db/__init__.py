"""
Decorator-driven persistence registry for Pydantic models.

Quick start
-----------

::

    from pydantic import BaseModel
    from registers.db import database_registry, db_field

    @database_registry(
        "sqlite:///users.db",
        table_name="users",
        key_field="id",
        unique_fields=["email"],
    )
    class User(BaseModel):
        id: int | None = db_field(id_strategy="autoincrement", default=None)
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

from registers.db.decorators import database_registry
from registers.db.engine import dispose_all, dispose_engine, get_engine
from registers.db.exceptions import (
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
    StaleDataError,
    UniqueConstraintError,
)
from registers.db.registry import DatabaseRegistry, audit_actor, tenant_scope, unscoped
from registers.db.query import Agg, Page, Q
from registers.db.relations import BelongsTo, HasMany, HasManyThrough, ManyToMany, ManyToOne, OneToMany, prefetch
from registers.db.schema import SchemaDiff, SchemaManager
from registers.db.metadata import RegistryConfig
from registers.db.adapters import DataclassAdapter, ModelAdapter, PydanticAdapter, adapter_for
from registers.db.expressions import F
from registers.db.fields import db_field, dc_field
from registers.db.security import (
    PasswordHashPolicy,
    configure_password_policy,
    get_password_policy,
    hash_password,
    is_password_hash,
    verify_and_upgrade_password,
    verify_password,
)

__all__ = [
    "database_registry",
    "DatabaseRegistry",
    "db_field",
    "F",
    "dc_field",
    "ModelAdapter",
    "PydanticAdapter",
    "DataclassAdapter",
    "adapter_for",
    "hash_password",
    "is_password_hash",
    "verify_password",
    "verify_and_upgrade_password",
    "PasswordHashPolicy",
    "configure_password_policy",
    "get_password_policy",
    "Q",
    "Agg",
    "Page",
    "tenant_scope",
    "unscoped",
    "audit_actor",

    # Relationships
    "HasMany",
    "BelongsTo",
    "HasManyThrough",
    "OneToMany",
    "ManyToOne",
    "ManyToMany",
    "prefetch",

    # Schema evolution
    "SchemaManager",
    "SchemaDiff",
    
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
    "StaleDataError",
    "MigrationError",
    "RelationshipError",
    "DuplicateKeyError",
    "InvalidPrimaryKeyAssignmentError",
    "ImmutableFieldError",
    "UniqueConstraintError",
    "RecordNotFoundError",
    "InvalidQueryError",
]
