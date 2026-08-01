"""
Registry coordinator and the concrete manager assembled from its mixins.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields, is_dataclass

import asyncio
from contextlib import AbstractAsyncContextManager, ExitStack, contextmanager
import logging
from pathlib import Path
from typing import Any, Callable, Generator, Generic, Mapping, TypeVar

from sqlalchemy import (
    text,
)
from sqlalchemy.exc import SQLAlchemyError

from registers.db.engine import dispose_engine, get_db_context
from registers.db.exceptions import (
    MigrationError,
    ModelRegistrationError,
    SchemaError,
)
from registers.db.manager.context import (
    _ACTIVE_CONNECTIONS,
    _PASSWORD_FIELD,
    MIGRATION_LEDGER_TABLE,
)
from registers.db.security import (
    verify_and_upgrade_password as verify_and_upgrade_password_value,
    verify_password as verify_password_value,
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


from registers.db.manager.base import _ManagerBase
from registers.db.manager.crud import _WriteMixin
from registers.db.manager.policies import _PolicyMixin
from registers.db.manager.queries import _ReadMixin
from registers.db.manager.schema_ops import _SchemaOpsMixin
from registers.db.adapters import adapter_for


def _patch_values(patch: Any) -> dict[str, Any]:
    """
    Extract update values from a patch object.

    A Pydantic patch model uses ``exclude_unset=True`` so that fields the caller
    never touched are not written back as explicit ``None``. Plain mappings and
    dataclasses are taken as-is.
    """
    dump = getattr(patch, "model_dump", None)
    if callable(dump):
        return dump(exclude_unset=True)
    if is_dataclass(patch) and not isinstance(patch, type):
        return {f.name: getattr(patch, f.name) for f in dataclass_fields(patch)}
    return dict(patch)


class _ModelManager(
    _WriteMixin[ModelT],
    _ReadMixin[ModelT],
    _SchemaOpsMixin[ModelT],
    _PolicyMixin[ModelT],
    _ManagerBase[ModelT],
):
    """
    Persistence manager for a registered model, exposed as ``Model.objects``.

    Assembled from mixins so that the read path, write path, DDL surface, and
    cross-cutting policies can be read and changed independently. The public API
    is unchanged: every method resolves on this one class.

    Attach with the ``@database_registry`` decorator::

        @database_registry("app.db", table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            name: str

        user  = User.objects.create(name="Alice")
        users = User.objects.filter(name="Alice")
        user.save()      # instance method injected by the decorator
        user.delete()

    Internal; use ``DatabaseRegistry().database_registry(...)`` or the module-level
    ``@database_registry(...)`` instead.
    """


class _AsyncTransaction(AbstractAsyncContextManager[Any]):
    def __init__(self, manager: _ModelManager[Any]) -> None:
        self._manager = manager
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        self._cm = self._manager.transaction()
        return self._cm.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        return self._cm.__exit__(exc_type, exc, tb)


class AsyncModelManager(Generic[ModelT]):
    """Awaitable manager facade for ``async_mode=True`` registrations."""

    def __init__(self, sync_manager: _ModelManager[ModelT]) -> None:
        self._sync_manager = sync_manager

    def transaction(self) -> _AsyncTransaction:
        return _AsyncTransaction(self._sync_manager)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._sync_manager, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attr, *args, **kwargs)

        return call

    def __repr__(self) -> str:
        return f"AsyncModelManager({self._sync_manager!r})"


class DatabaseRegistry:
    """
    Coordinator for model registrations within one logical DB namespace.

    Create one instance and register models through ``@db.database_registry(...)``.
    Registered models still receive the same manager API on ``Model.objects``.
    """

    _OWNER_MARKER = "__registers_db_owner__"

    def __init__(self) -> None:
        self._managers: dict[type, _ModelManager[Any]] = {}

    def get_registry(self) -> DatabaseRegistry:
        return self

    def all(self) -> dict[type, _ModelManager[Any]]:
        return dict(self._managers)

    def clear(self) -> None:
        self._managers.clear()

    def reset_registry(self) -> None:
        self.clear()

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """
        Bind all manager CRUD for this registry to one transaction per database URL.

        When the registry spans multiple database URLs this coordinates a best-effort
        transaction per engine; it does not provide two-phase commit semantics.
        """
        urls = list(dict.fromkeys(manager.database_url for manager in self._managers.values()))
        if not urls:
            yield
            return

        active = _ACTIVE_CONNECTIONS.get()
        with ExitStack() as stack:
            updated = dict(active)
            for url in urls:
                if url in updated:
                    continue
                context = get_db_context(url)
                updated[url] = stack.enter_context(context.engine.begin())
            token = _ACTIVE_CONNECTIONS.set(updated)
            try:
                yield
            finally:
                _ACTIVE_CONNECTIONS.reset(token)

    def create_all(self) -> None:
        """Create schemas for every model registered on this registry."""
        contexts = {
            manager.database_url: manager._context
            for manager in self._managers.values()
        }
        for url, context in contexts.items():
            self._ensure_migration_ledger(context.engine)
            try:
                context.metadata.create_all(context.engine)
            except SQLAlchemyError as exc:
                raise SchemaError(
                    f"Failed to create schemas for database '{url}'.",
                    operation="create_all",
                    details={"database_url": url},
                ) from exc

    def check_all(self) -> bool:
        """Return True when every registered table exists and matches its model."""
        return all(diff.ok for diff in self.diff_all().values())

    def diff_all(self) -> dict[str, Any]:
        """Return schema drift reports keyed by table name."""
        return {
            manager.table_name: manager.diff_schema()
            for manager in self._managers.values()
        }

    def schema_diff(self) -> dict[str, Any]:
        """Alias for ``diff_all()``."""
        return self.diff_all()

    def migrate(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Run safe additive migrations for every registered manager."""
        return {
            manager.table_name: manager.migrate(dry_run=dry_run)
            for manager in self._managers.values()
        }

    def assert_schema_current(self) -> None:
        """Raise MigrationError when any registered table is missing or drifted."""
        drift = {
            table_name: diff.to_dict()
            for table_name, diff in self.diff_all().items()
            if not diff.ok
        }
        if drift:
            raise MigrationError(
                "Schema drift detected for registered models.",
                operation="schema_diff",
                details={"tables": drift},
            )

    def dispose_all(self) -> None:
        """Dispose every engine used by managers owned by this registry."""
        for url in list(dict.fromkeys(manager.database_url for manager in self._managers.values())):
            dispose_engine(url)

    @staticmethod
    def _ensure_migration_ledger(engine: Any) -> None:
        ledger_sql = f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_LEDGER_TABLE} (
            version VARCHAR(255) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        with engine.begin() as conn:
            conn.execute(text(ledger_sql))

    def database_registry(
        self,
        database_url: str | Path | None = None,
        **options: Any,
    ) -> Callable[[type[ModelT]], type[ModelT]]:
        """
        Bind a model to a table and attach its manager.

        Accepts every option documented on :meth:`RegistryConfig.build`:
        ``table_name``, ``key_field``, ``manager_attr``, ``auto_create``,
        ``autoincrement``, ``unique_fields``, ``async_mode``, ``timestamps``,
        ``soft_delete``, ``audit_log``, ``audit_log_table``, ``tenant_field``,
        ``encryption_key``, ``log_queries``, ``slow_query_ms``, ``engine_options``,
        and ``read_replica_url``.

        Options are forwarded rather than re-declared so the canonical list lives in
        exactly one place. Unknown keywords surface as a ``TypeError`` from
        ``RegistryConfig.build`` naming the offending argument.
        """
        options.setdefault("database_url", database_url)

        def decorator(model_cls: type[ModelT]) -> type[ModelT]:
            self._assert_valid_model(model_cls)
            self._assert_model_owner_available(model_cls)

            manager = _ModelManager(model_cls, **options)
            config = manager.config

            exposed_manager: Any = (
                AsyncModelManager(manager) if config.async_mode else manager
            )
            self._safe_setattr(model_cls, config.manager_attr, exposed_manager)
            self._inject_instance_methods(
                model_cls, manager, config.key_field, async_mode=config.async_mode
            )
            self._inject_schema_forwarders(model_cls, manager)

            self._managers[model_cls] = manager
            setattr(model_cls, self._OWNER_MARKER, id(self))
            return model_cls

        return decorator

    def _assert_model_owner_available(self, model_cls: type) -> None:
        owner_id = getattr(model_cls, self._OWNER_MARKER, None)
        if owner_id is None or owner_id == id(self):
            return
        raise ModelRegistrationError(
            f"Model '{model_cls.__name__}' is already registered by another "
            "DatabaseRegistry instance.",
            model=model_cls.__name__,
            details={"owner_conflict": True},
        )

    @staticmethod
    def _assert_valid_model(model_cls: type) -> None:
        """
        Reject anything the adapter layer cannot represent.

        ``adapter_for`` owns the accept/reject decision so that supported model
        flavours are enumerated in exactly one place.
        """
        try:
            adapter_for(model_cls)
        except ModelRegistrationError:
            logger.error("Invalid model registration target: %r", model_cls)
            raise

    @staticmethod
    def _safe_setattr(model_cls: type, name: str, value: Any) -> None:
        in_dict = name in model_cls.__dict__
        declared = getattr(model_cls, "model_fields", None)
        if declared is None:
            declared = getattr(model_cls, "__dataclass_fields__", {})
        in_declared_fields = name in declared

        if in_dict or in_declared_fields:
            source = "a model field" if in_declared_fields else "a class attribute"
            logger.error(
                "Attribute collision while attaching '%s' to model '%s' (%s).",
                name,
                model_cls.__name__,
                source,
            )
            raise ModelRegistrationError(
                f"Cannot attach '{name}' to '{model_cls.__name__}' - "
                f"it is already defined as {source} on the model. "
                "Choose a different manager_attr or rename the conflicting attribute."
            )
        setattr(model_cls, name, value)

    @classmethod
    def _inject_instance_methods(
        cls,
        model_cls: type[ModelT],
        manager: _ModelManager[ModelT],
        key_field: str,
        *,
        async_mode: bool = False,
    ) -> None:
        if async_mode:
            async def save(self: ModelT) -> ModelT:
                updated = await asyncio.to_thread(manager.save, self)
                for field in manager.adapter.fields():
                    object.__setattr__(self, field, getattr(updated, field))
                return self

            async def delete(self: ModelT) -> bool:
                return await asyncio.to_thread(manager.delete, getattr(self, key_field))

            async def refresh(self: ModelT) -> ModelT:
                return await asyncio.to_thread(manager.refresh, self)

            async def update_instance(self: ModelT, values: Mapping[str, Any]) -> ModelT:
                manager._assert_known_update_fields(values)
                for field_name, value in values.items():
                    object.__setattr__(self, field_name, value)
                return await save(self)

            async def apply_patch(self: ModelT, patch: Any) -> ModelT:
                return await update_instance(self, _patch_values(patch))

            for method_name, method in [
                ("save", save),
                ("delete", delete),
                ("refresh", refresh),
                ("update", update_instance),
                ("apply_patch", apply_patch),
            ]:
                cls._safe_setattr(model_cls, method_name, method)
            return

        def save(self: ModelT) -> ModelT:
            updated = manager.save(self)
            for field in manager.adapter.fields():
                object.__setattr__(self, field, getattr(updated, field))
            return self

        def delete(self: ModelT) -> bool:
            return manager.delete(getattr(self, key_field))

        def refresh(self: ModelT) -> ModelT:
            return manager.refresh(self)

        def update_instance(self: ModelT, values: Mapping[str, Any]) -> ModelT:
            manager._assert_known_update_fields(values)
            for field_name, value in values.items():
                object.__setattr__(self, field_name, value)
            saved = manager.save(self)
            for field in manager.adapter.fields():
                object.__setattr__(self, field, getattr(saved, field))
            return self

        def apply_patch(self: ModelT, patch: Any) -> ModelT:
            return update_instance(self, _patch_values(patch))

        injected_methods: list[tuple[str, Callable[..., Any]]] = [
            ("save", save),
            ("delete", delete),
            ("refresh", refresh),
            ("update", update_instance),
            ("apply_patch", apply_patch),
        ]

        if _PASSWORD_FIELD in manager._password_hash_fields:
            def verify_password(self: ModelT, candidate: str) -> bool:
                return verify_password_value(candidate, getattr(self, "password"))

            def verify_and_upgrade_password(self: ModelT, candidate: str) -> bool:
                verified, upgraded_hash = verify_and_upgrade_password_value(
                    candidate,
                    getattr(self, "password"),
                )
                if verified and upgraded_hash is not None:
                    object.__setattr__(self, "password", upgraded_hash)
                    manager.save(self)
                return verified

            injected_methods.append(("verify_password", verify_password))
            injected_methods.append(("verify_and_upgrade_password", verify_and_upgrade_password))

        for method_name, method in injected_methods:
            cls._safe_setattr(model_cls, method_name, method)

    @classmethod
    def _inject_schema_forwarders(
        cls,
        model_cls: type[ModelT],
        manager: _ModelManager[ModelT],
    ) -> None:
        @classmethod  # type: ignore[misc]
        def create_schema(_model_cls: type[ModelT]) -> None:
            manager.create_schema()

        @classmethod  # type: ignore[misc]
        def drop_schema(_model_cls: type[ModelT]) -> None:
            manager.drop_schema()

        @classmethod  # type: ignore[misc]
        def schema_exists(_model_cls: type[ModelT]) -> bool:
            return manager.schema_exists()

        @classmethod  # type: ignore[misc]
        def truncate(_model_cls: type[ModelT]) -> None:
            manager.truncate()

        for name, method in [
            ("create_schema", create_schema),
            ("drop_schema", drop_schema),
            ("schema_exists", schema_exists),
            ("truncate", truncate),
        ]:
            cls._safe_setattr(model_cls, name, method)


