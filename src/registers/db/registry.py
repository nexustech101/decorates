"""
Public import path for the persistence manager.

Design: Manager pattern
-----------------------
All persistence operations live on the manager instance, not on the model class.
The decorator attaches it as ``Model.objects`` (or a custom ``manager_attr``).
Instance-level helpers (``save``, ``delete``, ``refresh``) are injected as thin
wrappers, keeping the model class itself clean.

Thread safety
-------------
* Engines are shared and pooled — see ``engine.py``.
* Every operation opens a fresh connection from the pool; writes run inside an
  ``engine.begin()`` context (auto-commit on success, rollback on failure).
* ``update_where`` updates and re-fetches on the same connection, avoiding a
  separate read-then-write TOCTOU window.

SQLite specifics
----------------
* Upsert uses dialect-aware conflict handling when supported.
* Unsupported dialects fall back to a transactional read-then-write path.

Date / datetime handling
------------------------
We use ``model_dump()`` without ``mode='json'`` so Python date/datetime objects are
preserved as native types. SQLAlchemy maps them to the underlying column type.
JSON-typed columns receive Python dicts/lists directly.

Module layout
-------------
The implementation lives in :mod:`registers.db.manager`, split by concern:

===========================  ==========================================
``manager.base``             construction, config, engine/table binding
``manager.crud``             write path and model<->row conversion
``manager.queries``          read path, filtering, criteria validation
``manager.schema_ops``       DDL surface
``manager.policies``         timestamps, soft delete, tenancy, audit, encryption
``manager.coordinator``      ``DatabaseRegistry`` and the assembled manager
``manager.context``          contextvars for tenancy/audit/transactions
===========================  ==========================================

This module re-exports the public names so ``from registers.db.registry import
DatabaseRegistry`` continues to work.
"""

from registers.db.manager import (
    MIGRATION_LEDGER_TABLE,
    AsyncModelManager,
    DatabaseRegistry,
    _ModelManager,
    audit_actor,
    tenant_scope,
    unscoped,
)

__all__ = [
    "DatabaseRegistry",
    "AsyncModelManager",
    "_ModelManager",
    "tenant_scope",
    "unscoped",
    "audit_actor",
    "MIGRATION_LEDGER_TABLE",
]
