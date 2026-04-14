"""
db_registry.metadata
~~~~~~~~~~~~~~~~~~~~
Immutable configuration for a single model registration.
Validated at decoration time so problems surface immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from decorators.db.exceptions import ConfigurationError
from decorators.db.typing_utils import annotation_is_integer, field_allows_none


@dataclass(frozen=True)
class RegistryConfig:
    """All validated options for a single :class:`DatabaseRegistry`."""

    model_cls: type[BaseModel]
    database_url: str
    table_name: str
    key_field: str
    manager_attr: str
    auto_create: bool
    autoincrement: bool
    unique_fields: tuple[str, ...]

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        model_cls: type[BaseModel],
        *,
        database_url: str | Path,
        table_name: str,
        key_field: str,
        manager_attr: str,
        auto_create: bool,
        autoincrement: bool,
        unique_fields: tuple[str, ...],
    ) -> "RegistryConfig":
        fields = model_cls.model_fields

        if key_field not in fields:
            raise ConfigurationError(
                f"key_field '{key_field}' is not a field on model '{model_cls.__name__}'."
            )

        if not manager_attr.strip():
            raise ConfigurationError("manager_attr must be a non-empty string.")

        if manager_attr in ("model_fields", "model_config", "__class__"):
            raise ConfigurationError(
                f"manager_attr '{manager_attr}' conflicts with a Pydantic internal name."
            )

        unknown = [f for f in unique_fields if f not in fields]
        if unknown:
            raise ConfigurationError(
                f"unique_fields references unknown fields on '{model_cls.__name__}': "
                + ", ".join(unknown)
            )

        if len(set(unique_fields)) != len(unique_fields):
            raise ConfigurationError("unique_fields must not contain duplicates.")

        key_field_def = fields[key_field]
        key_annotation = key_field_def.annotation

        if autoincrement:
            if not annotation_is_integer(key_annotation):
                raise ConfigurationError(
                    f"autoincrement requires an integer key field. "
                    f"'{key_field}' on '{model_cls.__name__}' is not an integer type."
                )
            if key_field_def.is_required() and not field_allows_none(key_field_def):
                raise ConfigurationError(
                    f"Key field '{key_field}' on '{model_cls.__name__}' uses autoincrement "
                    "but must allow None so the database can generate it. "
                    "Change the field to: id: int | None = None"
                )

        return cls(
            model_cls=model_cls,
            database_url=str(database_url),
            table_name=table_name,
            key_field=key_field,
            manager_attr=manager_attr,
            auto_create=auto_create,
            autoincrement=autoincrement,
            unique_fields=unique_fields,
        )
