"""
Immutable configuration for a single model registration.

One frozen object holds **every** registration option, and ``build()`` is the only
place any of them is validated. Previously the eight schema-shaping options lived
here while the ten behavioral ones (timestamps, soft delete, audit, tenancy,
encryption, replicas, logging) were assigned as loose mutable attributes on the
manager and validated afterwards by a separate ``_validate_policy_fields()``. That
split meant "is this option checked?" had no single answer.

Validation runs at decoration time so misconfiguration surfaces on import, not on
the first query.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from registers.db.exceptions import ConfigurationError, ModelRegistrationError
from registers.db.typing_utils import (
    annotation_is_integer,
    annotation_is_uuid,
    default_database_url,
    default_table_name,
    field_allows_none,
    normalize_database_url,
)

IdStrategy = Literal["manual", "autoincrement", "uuid4"]

#: Reserved on Pydantic models; a manager attached under these names would shadow
#: framework internals.
_RESERVED_MANAGER_ATTRS = frozenset({"model_fields", "model_config", "__class__"})


@dataclass(frozen=True)
class RegistryConfig:
    """Every validated option for a single model registration."""

    model_cls: type

    # --- identity & schema shape
    database_url: str
    table_name: str
    key_field: str
    manager_attr: str
    auto_create: bool
    autoincrement: bool
    id_strategy: IdStrategy | None
    unique_fields: tuple[str, ...]

    # --- behavioral policies
    async_mode: bool = False
    timestamps: bool = False
    soft_delete: bool = False
    audit_log: bool = False
    audit_log_table: str | None = None
    tenant_field: str | None = None
    version_field: str | None = None
    encryption_key: str | bytes | Callable[[], str | bytes] | None = None

    # --- operational
    log_queries: bool = False
    slow_query_ms: int | None = None
    engine_options: Mapping[str, Any] = dataclass_field(default_factory=dict)
    read_replica_url: str | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        model_cls: type,
        *,
        adapter: Any = None,
        database_url: str | Path | None = None,
        table_name: str | None = None,
        key_field: str = "id",
        manager_attr: str = "objects",
        auto_create: bool = True,
        autoincrement: bool = False,
        unique_fields: tuple[str, ...] | list[str] = (),
        async_mode: bool = False,
        timestamps: bool = False,
        soft_delete: bool = False,
        audit_log: bool = False,
        audit_log_table: str | None = None,
        tenant_field: str | None = None,
        version_field: str | None = None,
        encryption_key: str | bytes | Callable[[], str | bytes] | None = None,
        log_queries: bool = False,
        slow_query_ms: int | None = None,
        engine_options: Mapping[str, Any] | None = None,
        read_replica_url: str | Path | None = None,
    ) -> "RegistryConfig":
        if adapter is None:
            from registers.db.adapters import adapter_for

            adapter = adapter_for(model_cls)
        fields = adapter.fields()

        resolved_url = normalize_database_url(
            database_url if database_url is not None else default_database_url(model_cls.__name__)
        )
        resolved_table = table_name or default_table_name(model_cls.__name__)

        cls._validate_identity(model_cls, fields, key_field, manager_attr)
        explicit_unique = cls._validate_unique_fields(model_cls, fields, unique_fields)

        metadata_by_field = {
            field_name: adapter.field_metadata(field_name) for field_name in fields
        }

        cls._validate_field_metadata(model_cls, metadata_by_field, key_field)

        effective_autoincrement, effective_id_strategy = cls._resolve_id_strategy(
            model_cls,
            fields,
            metadata_by_field,
            key_field,
            autoincrement,
        )

        merged_unique = cls._merge_unique_fields(explicit_unique, metadata_by_field)

        cls._validate_policies(
            model_cls,
            fields,
            key_field=key_field,
            timestamps=timestamps,
            soft_delete=soft_delete,
            tenant_field=tenant_field,
            version_field=version_field,
            metadata_by_field=metadata_by_field,
            encryption_key=encryption_key,
        )
        cls._validate_operational(slow_query_ms=slow_query_ms)

        return cls(
            model_cls=model_cls,
            database_url=resolved_url,
            table_name=resolved_table,
            key_field=key_field,
            manager_attr=manager_attr,
            auto_create=auto_create,
            autoincrement=effective_autoincrement,
            id_strategy=effective_id_strategy,
            unique_fields=merged_unique,
            async_mode=async_mode,
            timestamps=timestamps,
            soft_delete=soft_delete,
            audit_log=audit_log,
            audit_log_table=audit_log_table or f"{resolved_table}_audit",
            tenant_field=tenant_field,
            version_field=version_field,
            encryption_key=encryption_key,
            log_queries=log_queries,
            slow_query_ms=slow_query_ms,
            engine_options=dict(engine_options or {}),
            read_replica_url=(
                normalize_database_url(read_replica_url) if read_replica_url is not None else None
            ),
        )

    # ------------------------------------------------------------------
    # Validation steps
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_identity(
        model_cls: type,
        fields: Mapping[str, Any],
        key_field: str,
        manager_attr: str,
    ) -> None:
        if key_field not in fields:
            raise ConfigurationError(
                f"key_field '{key_field}' is not a field on model '{model_cls.__name__}'."
            )
        if not manager_attr.strip():
            raise ConfigurationError("manager_attr must be a non-empty string.")
        if manager_attr in _RESERVED_MANAGER_ATTRS:
            raise ConfigurationError(
                f"manager_attr '{manager_attr}' conflicts with a Pydantic internal name."
            )

    @staticmethod
    def _validate_unique_fields(
        model_cls: type,
        fields: Mapping[str, Any],
        unique_fields: tuple[str, ...] | list[str],
    ) -> list[str]:
        explicit = list(unique_fields)
        unknown = [name for name in explicit if name not in fields]
        if unknown:
            raise ConfigurationError(
                f"unique_fields references unknown fields on '{model_cls.__name__}': "
                + ", ".join(unknown)
            )
        if len(set(explicit)) != len(explicit):
            raise ConfigurationError("unique_fields must not contain duplicates.")
        return explicit

    @staticmethod
    def _validate_field_metadata(
        model_cls: type,
        metadata_by_field: Mapping[str, Mapping[str, Any]],
        key_field: str,
    ) -> None:
        db_primary = [
            name for name, meta in metadata_by_field.items() if meta.get("db_primary_key", False)
        ]
        if len(db_primary) > 1:
            raise ConfigurationError(
                "Only one field may use db_field(primary_key=True). "
                f"Found: {', '.join(db_primary)}."
            )
        if db_primary and db_primary[0] != key_field:
            raise ConfigurationError(
                f"db_field(primary_key=True) is set on '{db_primary[0]}', "
                f"but key_field is '{key_field}'. Align these values."
            )

        non_key_autoincrement = [
            name
            for name, meta in metadata_by_field.items()
            if name != key_field and meta.get("db_autoincrement", False)
        ]
        if non_key_autoincrement:
            raise ConfigurationError(
                "db_field(autoincrement=True) is only supported on the key_field. "
                f"Invalid field(s): {', '.join(non_key_autoincrement)}."
            )

        non_key_id_strategy = [
            name
            for name, meta in metadata_by_field.items()
            if name != key_field and meta.get("db_id_strategy") is not None
        ]
        if non_key_id_strategy:
            raise ConfigurationError(
                "db_field(id_strategy=...) is only supported on the key_field. "
                f"Invalid field(s): {', '.join(non_key_id_strategy)}."
            )

    @staticmethod
    def _resolve_id_strategy(
        model_cls: type,
        fields: Mapping[str, Any],
        metadata_by_field: Mapping[str, Mapping[str, Any]],
        key_field: str,
        autoincrement: bool,
    ) -> tuple[bool, IdStrategy | None]:
        """
        Reconcile the legacy ``autoincrement=`` flag with ``db_field(id_strategy=)``.

        Returns ``(effective_autoincrement, effective_id_strategy)``.
        """
        key_meta = metadata_by_field[key_field]
        strategy: Any = key_meta.get("db_id_strategy")
        explicit_autoincrement = autoincrement or bool(key_meta.get("db_autoincrement", False))
        effective_autoincrement = explicit_autoincrement

        if strategy is None and explicit_autoincrement:
            strategy = "autoincrement"
        if strategy == "autoincrement":
            effective_autoincrement = True
        elif strategy in {"manual", "uuid4"}:
            effective_autoincrement = False

        if strategy == "manual" and explicit_autoincrement:
            raise ConfigurationError(
                "db_field(id_strategy='manual') conflicts with autoincrement settings. "
                "Remove autoincrement=True and db_field(autoincrement=True)."
            )
        if strategy == "uuid4" and explicit_autoincrement:
            raise ConfigurationError(
                "db_field(id_strategy='uuid4') conflicts with autoincrement settings. "
                "Use a UUID key field without autoincrement."
            )

        key_field_def = fields[key_field]
        key_annotation = key_field_def.annotation
        nullable_key = field_allows_none(key_field_def)

        if strategy is None and nullable_key:
            raise ConfigurationError(
                f"Nullable key field '{key_field}' on '{model_cls.__name__}' requires an "
                "explicit id strategy. Use db_field(id_strategy='autoincrement', default=None) "
                "for integer database IDs, db_field(id_strategy='uuid4', default=None) "
                "for generated UUIDs, or make the key field required for manual IDs."
            )

        if strategy == "manual" and nullable_key:
            raise ConfigurationError(
                f"Key field '{key_field}' on '{model_cls.__name__}' uses id_strategy='manual' "
                "but allows None. Manual primary keys must be supplied by the caller and "
                "should be declared as a required non-null field."
            )

        if effective_autoincrement:
            if not annotation_is_integer(key_annotation):
                raise ConfigurationError(
                    f"autoincrement requires an integer key field. "
                    f"'{key_field}' on '{model_cls.__name__}' is not an integer type."
                )
            if key_field_def.is_required() or not nullable_key:
                raise ConfigurationError(
                    f"Key field '{key_field}' on '{model_cls.__name__}' uses autoincrement "
                    "but must allow None so the database can generate it. "
                    'Change the field to: id: int | None = db_field(id_strategy="autoincrement", default=None)'
                )

        if strategy == "uuid4":
            if not annotation_is_uuid(key_annotation):
                raise ConfigurationError(
                    f"id_strategy='uuid4' requires a UUID key field. "
                    f"'{key_field}' on '{model_cls.__name__}' is not a UUID type."
                )
            if key_field_def.is_required() or not nullable_key:
                raise ConfigurationError(
                    f"Key field '{key_field}' on '{model_cls.__name__}' uses id_strategy='uuid4' "
                    "but must allow None so UUIDs can be generated automatically. "
                    'Change the field to: id: UUID | None = db_field(id_strategy="uuid4", default=None)'
                )

        return effective_autoincrement, strategy

    @staticmethod
    def _merge_unique_fields(
        explicit: list[str],
        metadata_by_field: Mapping[str, Mapping[str, Any]],
    ) -> tuple[str, ...]:
        """Explicit ``unique_fields`` first, then any ``db_field(unique=True)``."""
        merged: list[str] = []
        seen: set[str] = set()
        for name in explicit:
            if name not in seen:
                merged.append(name)
                seen.add(name)
        for name, meta in metadata_by_field.items():
            if meta.get("db_unique", False) and name not in seen:
                merged.append(name)
                seen.add(name)
        return tuple(merged)

    @staticmethod
    def _validate_policies(
        model_cls: type,
        fields: Mapping[str, Any],
        *,
        key_field: str,
        timestamps: bool,
        soft_delete: bool,
        tenant_field: str | None,
        version_field: str | None,
        metadata_by_field: Mapping[str, Mapping[str, Any]],
        encryption_key: Any,
    ) -> None:
        """Policies that require the model to declare supporting fields."""
        if timestamps:
            missing = [name for name in ("created_at", "updated_at") if name not in fields]
            if missing:
                raise ModelRegistrationError(
                    f"timestamps=True requires declared field(s): {', '.join(missing)}.",
                    model=model_cls.__name__,
                )

        if soft_delete and "deleted_at" not in fields:
            raise ModelRegistrationError(
                "soft_delete=True requires a declared nullable 'deleted_at' field.",
                model=model_cls.__name__,
            )

        if version_field is not None:
            if version_field not in fields:
                raise ModelRegistrationError(
                    f"version_field '{version_field}' is not a field on model "
                    f"'{model_cls.__name__}'.",
                    model=model_cls.__name__,
                    field=version_field,
                )
            if not annotation_is_integer(fields[version_field].annotation):
                raise ModelRegistrationError(
                    f"version_field '{version_field}' on '{model_cls.__name__}' must be "
                    "an integer field. Declare it as `version: int = 1`.",
                    model=model_cls.__name__,
                    field=version_field,
                )
            if version_field == key_field:
                raise ModelRegistrationError(
                    "version_field cannot be the primary key.",
                    model=model_cls.__name__,
                    field=version_field,
                )

        if tenant_field is not None and tenant_field not in fields:
            raise ModelRegistrationError(
                f"tenant_field '{tenant_field}' is not a field on model '{model_cls.__name__}'.",
                model=model_cls.__name__,
                field=tenant_field,
            )

        # Encrypted columns are unreadable without a key. Catching this at
        # decoration time beats a decrypt failure on the first read.
        encrypted = [
            name for name, meta in metadata_by_field.items() if meta.get("db_encrypted", False)
        ]
        if encrypted and encryption_key is None:
            raise ConfigurationError(
                f"Model '{model_cls.__name__}' declares db_field(encrypted=True) on "
                f"{', '.join(encrypted)} but no encryption_key was supplied to "
                "database_registry(...)."
            )

    @staticmethod
    def _validate_operational(*, slow_query_ms: int | None) -> None:
        if slow_query_ms is not None and (
            not isinstance(slow_query_ms, int)
            or isinstance(slow_query_ms, bool)
            or slow_query_ms < 0
        ):
            raise ConfigurationError(
                "slow_query_ms must be a non-negative integer when provided."
            )
