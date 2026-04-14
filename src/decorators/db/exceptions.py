"""
db_registry.errors
~~~~~~~~~~~~~~~~~~
All package-defined exceptions in one place.

Hierarchy
---------
RegistryError                     ← base; safe to catch everything
├── ConfigurationError            ← bad decorator args, bad field refs
├── ModelRegistrationError        ← model class cannot be decorated
├── SchemaError                   ← DDL failures
│   └── MigrationError            ← schema evolution failures
├── RelationshipError             ← misconfigured relationship descriptor
├── DuplicateKeyError             ← INSERT collides on primary key
├── UniqueConstraintError         ← INSERT/UPDATE violates UNIQUE column
├── RecordNotFoundError           ← require() / require_related() found nothing
└── InvalidQueryError             ← malformed criteria or unknown fields
"""

from __future__ import annotations


class RegistryError(Exception):
    """Base class for all db_registry exceptions."""


class ConfigurationError(RegistryError):
    """Raised when decorator options or field references are invalid."""


class ModelRegistrationError(RegistryError):
    """Raised when a model class cannot be safely decorated."""


class SchemaError(RegistryError):
    """Raised when a DDL operation (CREATE/DROP/ALTER) fails."""


class MigrationError(SchemaError):
    """Raised when a schema evolution step cannot be applied."""


class RelationshipError(RegistryError):
    """Raised when a relationship descriptor is misconfigured or misused."""


class DuplicateKeyError(RegistryError):
    """Raised when an INSERT collides with an existing primary-key value."""


class InvalidPrimaryKeyAssignmentError(RegistryError):
    """Raised when callers assign a database-managed primary key explicitly."""


class ImmutableFieldError(RegistryError):
    """Raised when an immutable persisted field is mutated."""


class UniqueConstraintError(RegistryError):
    """Raised when an INSERT or UPDATE violates a UNIQUE constraint."""


class RecordNotFoundError(RegistryError):
    """Raised by require() and require_related() when no row matches."""


class InvalidQueryError(RegistryError):
    """Raised when filter criteria reference unknown fields or are malformed."""
