"""
Local control-plane storage for ``functionals.fx``.

This module uses ``functionals.db`` registries against a project-local sqlite
database (``.fx/fx.db``) to track project metadata, modules, linked
plugins, and operation history.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from functionals.db import DatabaseRegistry


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_root(root: str | Path | None = None) -> Path:
    target = Path.cwd() if root is None else Path(root)
    return target.resolve()


def fx_home(root: str | Path | None = None) -> Path:
    root_path = resolve_root(root)
    base = root_path / ".fx"
    legacy = root_path / ".functionals"
    if not base.exists() and legacy.exists():
        try:
            legacy.rename(base)
        except OSError:
            # Fall back to creating the new path when rename is not possible.
            pass
    base.mkdir(parents=True, exist_ok=True)
    return base


def control_db_path(root: str | Path | None = None) -> Path:
    return fx_home(root) / "fx.db"


class ProjectRecord(BaseModel):
    id: int | None = None
    name: str
    root_path: str
    project_type: str = "cli"
    created_at: str
    updated_at: str


class ModuleRecord(BaseModel):
    id: int | None = None
    project_root: str
    module_type: str
    module_name: str
    package_path: str
    entry_file: str
    created_at: str
    updated_at: str


class PluginRecord(BaseModel):
    id: int | None = None
    project_root: str
    alias: str
    package_path: str
    enabled: bool = True
    link_file: str
    created_at: str
    updated_at: str


class OperationRecord(BaseModel):
    id: int | None = None
    project_root: str
    command: str
    arguments: str
    status: str
    message: str = ""
    created_at: str


@lru_cache(maxsize=64)
def _project_registry(db_file: str) -> DatabaseRegistry[ProjectRecord]:
    return DatabaseRegistry(
        ProjectRecord,
        db_file,
        table_name="fx_projects",
        key_field="id",
        autoincrement=True,
        unique_fields=["root_path"],
    )


@lru_cache(maxsize=64)
def _module_registry(db_file: str) -> DatabaseRegistry[ModuleRecord]:
    return DatabaseRegistry(
        ModuleRecord,
        db_file,
        table_name="fx_modules",
        key_field="id",
        autoincrement=True,
        unique_fields=["package_path"],
    )


@lru_cache(maxsize=64)
def _plugin_registry(db_file: str) -> DatabaseRegistry[PluginRecord]:
    return DatabaseRegistry(
        PluginRecord,
        db_file,
        table_name="fx_plugins",
        key_field="id",
        autoincrement=True,
        unique_fields=["alias"],
    )


@lru_cache(maxsize=64)
def _operation_registry(db_file: str) -> DatabaseRegistry[OperationRecord]:
    return DatabaseRegistry(
        OperationRecord,
        db_file,
        table_name="fx_operations",
        key_field="id",
        autoincrement=True,
    )


def project_registry(root: str | Path | None = None) -> DatabaseRegistry[ProjectRecord]:
    return _project_registry(str(control_db_path(root)))


def module_registry(root: str | Path | None = None) -> DatabaseRegistry[ModuleRecord]:
    return _module_registry(str(control_db_path(root)))


def plugin_registry(root: str | Path | None = None) -> DatabaseRegistry[PluginRecord]:
    return _plugin_registry(str(control_db_path(root)))


def operation_registry(root: str | Path | None = None) -> DatabaseRegistry[OperationRecord]:
    return _operation_registry(str(control_db_path(root)))


def record_operation(
    *,
    root: str | Path | None,
    command: str,
    arguments: dict[str, Any],
    status: str,
    message: str = "",
) -> None:
    operation_registry(root).create(
        project_root=str(resolve_root(root)),
        command=command,
        arguments=json.dumps(arguments, sort_keys=True),
        status=status,
        message=message,
        created_at=utc_now(),
    )


def clear_state_caches() -> None:
    """Testing helper: clear cached registries."""
    _project_registry.cache_clear()
    _module_registry.cache_clear()
    _plugin_registry.cache_clear()
    _operation_registry.cache_clear()
