"""
The persistence manager, split by concern.

``registers.db.registry`` remains the public import path and re-exports everything
here, so existing imports keep working.
"""

from registers.db.manager.base import _ManagerBase
from registers.db.manager.context import (
    MIGRATION_LEDGER_TABLE,
    audit_actor,
    tenant_scope,
    unscoped,
)
from registers.db.manager.coordinator import (
    AsyncModelManager,
    DatabaseRegistry,
    _AsyncTransaction,
    _ModelManager,
)
from registers.db.manager.crud import _WriteMixin
from registers.db.manager.policies import _PolicyMixin
from registers.db.manager.queries import _ReadMixin
from registers.db.manager.schema_ops import _SchemaOpsMixin

__all__ = [
    "DatabaseRegistry",
    "AsyncModelManager",
    "_ModelManager",
    "tenant_scope",
    "unscoped",
    "audit_actor",
    "MIGRATION_LEDGER_TABLE",
]
