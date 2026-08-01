"""
Centralised engine / connection-pool management.

Design decisions
----------------
* One Engine per unique database URL — engines are expensive to create and
  contain the connection pool, so sharing them is critical.
* SQLite file databases use ``check_same_thread=False`` with a
  ``StaticPool`` for in-memory URLs and ``NullPool`` for :memory: to match
  the SQLite threading model safely.
* A ``threading.Lock`` guards the engine registry itself so that two threads
  decorating models backed by the same database simultaneously don't race to
  create duplicate engines.
* ``dispose_all()`` is provided for application shutdown and test teardown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool


_lock: threading.Lock = threading.Lock()
_engines: dict[str, Engine] = {}
logger = logging.getLogger(__name__)


@dataclass
class DatabaseContext:
    """Shared per-URL context for table metadata and table object reuse."""

    database_url: str
    engine: Engine
    metadata: MetaData = field(default_factory=MetaData)
    tables: dict[str, Table] = field(default_factory=dict)
    #: table name -> the model class that defined it, so a second model claiming
    #: the same table is caught instead of silently inheriting the first's columns.
    table_owners: dict[str, type] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    disposed: bool = False


_contexts: dict[str, DatabaseContext] = {}


def _get_or_create_engine_unlocked(
    database_url: str,
    engine_options: dict[str, Any] | None = None,
) -> Engine:
    if database_url not in _engines:
        logger.debug("Creating new SQLAlchemy engine for url='%s'.", database_url)
        _engines[database_url] = _create_engine(database_url, engine_options=engine_options)
    else:
        logger.debug("Reusing cached SQLAlchemy engine for url='%s'.", database_url)
    return _engines[database_url]


def get_engine(database_url: str, *, engine_options: dict[str, Any] | None = None) -> Engine:
    """
    Return a cached :class:`~sqlalchemy.engine.Engine` for *database_url*,
    creating one on first call.

    Thread-safe: a single lock serialises engine construction so parallel
    decorator calls for the same database don't race.
    """
    with _lock:
        return _get_or_create_engine_unlocked(database_url, engine_options)


def get_db_context(
    database_url: str,
    *,
    engine_options: dict[str, Any] | None = None,
) -> DatabaseContext:
    """
    Return a cached :class:`DatabaseContext` for *database_url*.

    Contexts share SQLAlchemy ``MetaData`` across all registered tables for the
    same URL so foreign-key graphs can be resolved without isolated metadata.
    """
    with _lock:
        context = _contexts.get(database_url)
        if context is None:
            context = DatabaseContext(
                database_url=database_url,
                engine=_get_or_create_engine_unlocked(database_url, engine_options),
            )
            _contexts[database_url] = context
        return context


def dispose_engine(database_url: str) -> None:
    """Dispose the engine for *database_url* and remove it from the cache."""
    with _lock:
        context = _contexts.pop(database_url, None)
        if context is not None:
            context.disposed = True
        engine = _engines.pop(database_url, None)
    if engine is not None:
        logger.debug("Disposing SQLAlchemy engine for url='%s'.", database_url)
        engine.dispose()


def dispose_all() -> None:
    """Dispose every cached engine. Call on application shutdown."""
    with _lock:
        # Mark before clearing: managers hold a direct reference to their context,
        # so a context dropped from the cache without ``disposed=True`` would let
        # post-teardown operations proceed silently instead of raising.
        for context in _contexts.values():
            context.disposed = True
        _contexts.clear()
        urls = list(_engines.keys())
    for url in urls:
        dispose_engine(url)


def dialect_insert(engine: Engine, table: Table) -> Any:
    """
    Return a dialect-specific INSERT construct for *table*.

    SQLite and PostgreSQL share ``on_conflict_do_update`` support, while
    MySQL/MariaDB use ``on_duplicate_key_update``. Unsupported dialects
    return ``None`` so callers can fall back to a read-then-write upsert.
    """
    dialect_name = engine.dialect.name

    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        return insert(table)

    if dialect_name in {"postgresql", "pg8000"}:
        from sqlalchemy.dialects.postgresql import insert

        return insert(table)

    if dialect_name in {"mysql", "mariadb"}:
        from sqlalchemy.dialects.mysql import insert

        return insert(table)

    return None


def _create_engine(
    database_url: str,
    *,
    engine_options: dict[str, Any] | None = None,
) -> Engine:
    kwargs: dict[str, Any] = {"future": True}

    if database_url.startswith("sqlite"):
        if ":memory:" in database_url:
            # In-memory SQLite: a single shared connection is required so that
            # the same database is visible to all operations in the process.
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool
        else:
            # File-based SQLite: disable the same-thread check so the engine
            # can be used from multiple threads (it serialises at the OS level
            # via its own locking).
            kwargs["connect_args"] = {"check_same_thread": False}

    if engine_options:
        kwargs.update(engine_options)

    engine = create_engine(database_url, **kwargs)

    # Enable WAL mode for SQLite so readers don't block writers.
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        @event.listens_for(engine, "connect")
        def _set_wal(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # For in-memory SQLite, still enable FK enforcement.
    if ":memory:" in database_url:
        @event.listens_for(engine, "connect")
        def _set_memory_pragmas(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine
