"""
Ambient request/task scope for manager operations.

Tenancy, soft-delete bypass, audit actor, and the active-transaction map are all
carried in :mod:`contextvars` rather than threaded through every call signature.
That keeps ``Model.objects.create(...)`` free of plumbing arguments while still
working correctly under threads and asyncio tasks.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Generator

from sqlalchemy.engine import Connection

#: Attribute stamped on a model instance holding the primary key it was loaded
#: with, so a later save can detect an attempted key mutation.
_ORIGINAL_KEY_ATTR = "__registers_original_key__"

#: Attribute stamped on a model instance holding the row version it was loaded
#: at, so a later save can detect that someone else has written in between.
_ORIGINAL_VERSION_ATTR = "__registers_original_version__"

#: Instance helpers ``verify_password``/``verify_and_upgrade_password`` are only
#: injected for a hashed field with this exact name.
_PASSWORD_FIELD = "password"

MIGRATION_LEDGER_TABLE = "registers_schema_migrations"

#: Marker attribute holding the list of managers interested in query timing for a
#: shared engine. Keyed on the engine so listeners are installed exactly once.
_QUERY_LOG_SUBSCRIBERS = "_registers_query_log_subscribers"

#: Operators whose right-hand side is a match pattern, not a field-typed value.
_STRING_MATCH_OPERATORS = frozenset({"like", "ilike", "contains", "startswith", "endswith"})

_ACTIVE_CONNECTIONS: ContextVar[dict[str, Connection]] = ContextVar(
    "registers_db_active_connections",
    default={},
)
_TENANT_SCOPE: ContextVar[Any] = ContextVar("registers_db_tenant_scope", default=None)
_TENANT_UNSCOPED: ContextVar[bool] = ContextVar("registers_db_tenant_unscoped", default=False)
_AUDIT_ACTOR: ContextVar[str | None] = ContextVar("registers_db_audit_actor", default=None)


@contextmanager
def tenant_scope(tenant: Any) -> Generator[None, None, None]:
    """Apply a tenant value to tenant-scoped manager operations in this context."""
    token = _TENANT_SCOPE.set(tenant)
    try:
        yield
    finally:
        _TENANT_SCOPE.reset(token)


@contextmanager
def unscoped() -> Generator[None, None, None]:
    """Temporarily bypass tenant and soft-delete default filters."""
    token = _TENANT_UNSCOPED.set(True)
    try:
        yield
    finally:
        _TENANT_UNSCOPED.reset(token)


@contextmanager
def audit_actor(actor: str | None) -> Generator[None, None, None]:
    """Attach an actor value to audit rows written in this context."""
    token = _AUDIT_ACTOR.set(actor)
    try:
        yield
    finally:
        _AUDIT_ACTOR.reset(token)
