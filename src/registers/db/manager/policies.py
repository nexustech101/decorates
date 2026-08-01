"""
Cross-cutting per-model policies.

Timestamps, soft delete, multi-tenancy, audit logging, field encryption, and
model lifecycle hooks. These all hook into the same two seams: criteria are
widened on read via ``_with_policy_criteria``, and values are stamped on write
via the ``_prepare_*`` helpers.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Generic, Mapping, TypeVar

from sqlalchemy.engine import Connection

from registers.db.exceptions import (
    InvalidQueryError,
)
from registers.db.manager.context import (
    _AUDIT_ACTOR,
    _TENANT_SCOPE,
    _TENANT_UNSCOPED,
)
from registers.db.specs import (
    FieldCoercionError,
)

logger = logging.getLogger(__name__)
# Unbound: a model may be a pydantic BaseModel or a stdlib dataclass.
ModelT = TypeVar("ModelT")


def _validate_bool_flag(value: Any) -> None:
    """``field__is_null=`` takes a truthy flag; reject obvious mistakes only."""
    if not isinstance(value, bool) and value not in (0, 1, None):
        raise FieldCoercionError(value, "a boolean")


class _PolicyMixin(Generic[ModelT]):
    """Cross-cutting per-model policies."""

    def _with_policy_criteria(
        self,
        criteria: Mapping[str, Any],
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        scoped = dict(criteria)
        if self.soft_delete and not include_deleted and not _TENANT_UNSCOPED.get():
            scoped.setdefault("deleted_at__is_null", True)
        if self.tenant_field is not None and not _TENANT_UNSCOPED.get():
            tenant = _TENANT_SCOPE.get()
            if tenant is None:
                raise InvalidQueryError(
                    f"tenant_scope(...) is required for tenant-scoped model '{self.model_cls.__name__}'.",
                    operation="tenant_scope",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.tenant_field,
                )
            scoped.setdefault(self.tenant_field, tenant)
        return scoped

    def _prepare_create_data(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.tenant_field is not None and not _TENANT_UNSCOPED.get():
            tenant = _TENANT_SCOPE.get()
            if tenant is None:
                raise InvalidQueryError(
                    f"tenant_scope(...) is required for tenant-scoped model '{self.model_cls.__name__}'.",
                    operation="tenant_scope",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.tenant_field,
                )
            explicit = data.get(self.tenant_field)
            if explicit is not None and explicit != tenant:
                raise InvalidQueryError(
                    f"Explicit tenant value for '{self.tenant_field}' does not match active tenant scope.",
                    operation="tenant_scope",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=self.tenant_field,
                )
            data[self.tenant_field] = tenant
        if self.timestamps:
            data.setdefault("created_at", self._timestamp_value("created_at"))
            data.setdefault("updated_at", data["created_at"])
        if self.soft_delete:
            data.setdefault("deleted_at", None)
        if self.version_field is not None:
            # New rows start at version 1 so that 0/None can never be mistaken for
            # "loaded from the database at some version".
            if data.get(self.version_field) is None:
                data[self.version_field] = 1
        return data

    def _prepare_update_values(self, values: dict[str, Any]) -> dict[str, Any]:
        if self.timestamps and "updated_at" not in values:
            values["updated_at"] = self._timestamp_value("updated_at")
        return values

    def _prepare_instance_for_save(self, instance: ModelT, *, is_create: bool) -> None:
        if self.timestamps:
            if is_create and getattr(instance, "created_at", None) is None:
                object.__setattr__(instance, "created_at", self._timestamp_value("created_at"))
            object.__setattr__(instance, "updated_at", self._timestamp_value("updated_at"))
        if self.soft_delete and getattr(instance, "deleted_at", None) is None:
            object.__setattr__(instance, "deleted_at", None)

    def _timestamp_value(self, field_name: str) -> Any:
        """
        Return "now" in whatever shape the declared field accepts.

        Models commonly declare ``created_at: str`` rather than ``datetime``; the
        field's own validator decides which it is, so this works for both without
        the manager needing to inspect annotations.
        """
        now = datetime.now(timezone.utc)
        spec = self._specs.get(field_name)
        if spec is None:
            return now
        try:
            return spec.validate(now)
        except FieldCoercionError:
            return now.isoformat()

    def _call_hook(self, name: str, *args: Any) -> None:
        hook = getattr(self.model_cls, name, None)
        if callable(hook):
            hook(*args)

    def _public_model_dump(self, model: ModelT) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.adapter.to_dict(model).items()
            if key not in self._db_excluded_fields
        }

    def _audit(
        self,
        conn: Connection,
        operation: str,
        model: ModelT | None,
        changed_fields: Mapping[str, Any],
    ) -> None:
        if self._audit_table is None:
            return
        record_id = None if model is None else getattr(model, self.key_field, None)
        conn.execute(
            self._audit_table.insert().values(
                table_name=self.table_name,
                record_id=None if record_id is None else str(record_id),
                operation=operation,
                changed_fields=json.loads(json.dumps(dict(changed_fields), default=str)),
                actor=_AUDIT_ACTOR.get(),
                timestamp=datetime.now(timezone.utc),
            )
        )

    def _audit_operation_for_updates(self, updates: Mapping[str, Any]) -> str:
        if self.soft_delete and set(updates) <= {"deleted_at", "updated_at"}:
            deleted_at = updates.get("deleted_at")
            return "restore" if deleted_at is None else "delete"
        return "update"

    def _encryption_fernet(self) -> Any:
        # Cached: the previous implementation derived a key and constructed a
        # Fernet on *every* encrypted value, which meant a SHA-256 per field per
        # row on both read and write.
        if self._fernet is not None:
            return self._fernet
        if self.encryption_key is None:
            raise InvalidQueryError(
                "Encrypted fields require encryption_key on database_registry(...).",
                operation="encryption",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        try:
            from cryptography.fernet import Fernet # type: ignore
        except Exception as exc:  # pragma: no cover
            raise InvalidQueryError(
                "Encrypted fields require the optional 'cryptography' package.",
                operation="encryption",
                model=self.model_cls.__name__,
                table=self.table_name,
            ) from exc
        key = self.encryption_key() if callable(self.encryption_key) else self.encryption_key
        raw_key = key.encode() if isinstance(key, str) else bytes(key)
        try:
            self._fernet = Fernet(raw_key)
        except Exception:
            self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest()))
        return self._fernet

    def _encrypt_value(self, value: Any) -> Any:
        if value is None:
            return None
        payload = value if isinstance(value, str) else json.dumps(value)
        return self._encryption_fernet().encrypt(payload.encode()).decode()

    def _decrypt_value(self, value: Any) -> Any:
        if value is None:
            return None
        return self._encryption_fernet().decrypt(str(value).encode()).decode()

