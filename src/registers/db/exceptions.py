from __future__ import annotations

from typing import Any

from registers.core.errors import RegistrationErrorBase


class RegistryError(RegistrationErrorBase):
    """Base class for all registers.db exceptions with structured context."""

    DEFAULT_OPERATION: str | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        operation: str | None = None,
        model: str | None = None,
        table: str | None = None,
        field: str | None = None,
        details: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {}
        if model is not None:
            payload["model"] = model
        if table is not None:
            payload["table"] = table
        if field is not None:
            payload["field"] = field
        if context:
            payload.update(context)

        resolved_operation = operation if operation is not None else self.DEFAULT_OPERATION
        super().__init__(
            message or self.__class__.__name__,
            operation=resolved_operation,
            module="db",
            details=details,
            context=payload,
            **extra,
        )

        self.model = model
        self.table = table
        self.field = field


class ConfigurationError(RegistryError):
    """Raised when decorator options or field references are invalid."""

    DEFAULT_OPERATION = "configuration"


class ModelRegistrationError(RegistryError):
    """Raised when a model class cannot be safely decorated."""

    DEFAULT_OPERATION = "model_registration"

    def __init__(self, message_or_model: str, reason: str | None = None, **context: Any) -> None:
        if reason is None:
            message = message_or_model
            model = context.pop("model", None)
        else:
            model = context.pop("model", message_or_model)
            message = f"Cannot register model '{model}': {reason}"
        super().__init__(message, model=model, reason=reason, **context)
        self.reason = reason


class SchemaError(RegistryError):
    """Raised when a DDL operation (CREATE/DROP/ALTER) fails."""

    DEFAULT_OPERATION = "schema"


class MigrationError(SchemaError):
    """Raised when a schema evolution step cannot be applied."""

    DEFAULT_OPERATION = "migration"

    def __init__(self, message: str, version: str | None = None, **context: Any) -> None:
        super().__init__(message, version=version, **context)
        self.version = version


class RelationshipError(RegistryError):
    """Raised when a relationship descriptor is misconfigured or misused."""

    DEFAULT_OPERATION = "relationship"


class DuplicateKeyError(RegistryError):
    """Raised when an INSERT collides with an existing primary-key value."""

    DEFAULT_OPERATION = "duplicate_key"


class InvalidPrimaryKeyAssignmentError(RegistryError):
    """Raised when callers assign a database-managed primary key explicitly."""

    DEFAULT_OPERATION = "invalid_primary_key_assignment"


class ImmutableFieldError(RegistryError):
    """Raised when an immutable persisted field is mutated."""

    DEFAULT_OPERATION = "immutable_field_mutation"


class UniqueConstraintError(RegistryError):
    """Raised when an INSERT or UPDATE violates a UNIQUE constraint."""

    DEFAULT_OPERATION = "unique_constraint"


class RecordNotFoundError(RegistryError):
    """Raised by require() and require_related() when no row matches."""

    DEFAULT_OPERATION = "record_not_found"


class InvalidQueryError(RegistryError):
    """Raised when filter criteria reference unknown fields or are malformed."""

    DEFAULT_OPERATION = "invalid_query"


class StaleDataError(RegistryError):
    """
    Raised when a write would overwrite a change made by someone else.

    Only models configured with ``version_field=`` can raise this. The record was
    read at one version, modified, and by the time the write landed the stored
    version had moved on — meaning another writer got there first and this write
    would silently discard their change.

    The caller decides what to do: re-read and reapply, surface a 409, or merge.
    Retrying blindly is only safe when the work in between has no side effects.

        try:
            order.status = "shipped"
            order.save()
        except StaleDataError:
            order = Order.objects.require(order.id)   # someone else moved it
            ...
    """

    DEFAULT_OPERATION = "stale_data"

    def __init__(
        self,
        message: str | None = None,
        *,
        expected_version: Any = None,
        **context: Any,
    ) -> None:
        super().__init__(message, expected_version=expected_version, **context)
        self.expected_version = expected_version
