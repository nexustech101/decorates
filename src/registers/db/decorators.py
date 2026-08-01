"""
Module-level decorator surface for registers.db.

Backward-compatible: delegates to a default registry coordinator so that
``@database_registry(...)`` and ``@DatabaseRegistry().database_registry(...)``
behave identically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from registers.db.registry import DatabaseRegistry

ModelT = TypeVar("ModelT")

_DEFAULT_DB_REGISTRY = DatabaseRegistry()


def database_registry(
    database_url: str | Path | None = None,
    **options: Any,
) -> Callable[[type[ModelT]], type[ModelT]]:
    """
    Decorate a Pydantic model and attach a persistence manager as ``Model.objects``.

    Accepts every option documented on ``RegistryConfig.build``:

    ``table_name``, ``key_field``, ``manager_attr``, ``auto_create``,
    ``autoincrement``, ``unique_fields``, ``async_mode``, ``timestamps``,
    ``soft_delete``, ``audit_log``, ``audit_log_table``, ``tenant_field``,
    ``encryption_key``, ``log_queries``, ``slow_query_ms``, ``engine_options``,
    ``read_replica_url``.

    Options are forwarded rather than restated so the canonical option list lives
    in ``RegistryConfig`` alone. Backed by a module-level default
    ``DatabaseRegistry`` coordinator.

    Example::

        @database_registry("sqlite:///app.db", table_name="users", unique_fields=["email"])
        class User(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            email: str
    """
    return _DEFAULT_DB_REGISTRY.database_registry(database_url, **options)
