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

import logging
import threading
from typing import Any

from sqlalchemy import Table, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool


_lock: threading.Lock = threading.Lock()
_engines: dict[str, Engine] = {}
logger = logging.getLogger(__name__)


def get_engine(database_url: str) -> Engine:
    """
    Return a cached :class:`~sqlalchemy.engine.Engine` for *database_url*,
    creating one on first call.

    Thread-safe: a single lock serialises engine construction so parallel
    decorator calls for the same database don't race.
    """
    with _lock:
        if database_url not in _engines:
            logger.debug("Creating new SQLAlchemy engine for url='%s'.", database_url)
            _engines[database_url] = _create_engine(database_url)
        else:
            logger.debug("Reusing cached SQLAlchemy engine for url='%s'.", database_url)
        return _engines[database_url]


def dispose_engine(database_url: str) -> None:
    """Dispose the engine for *database_url* and remove it from the cache."""
    with _lock:
        engine = _engines.pop(database_url, None)
    if engine is not None:
        logger.debug("Disposing SQLAlchemy engine for url='%s'.", database_url)
        engine.dispose()


def dispose_all() -> None:
    """Dispose every cached engine. Call on application shutdown."""
    with _lock:
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


def _create_engine(database_url: str) -> Engine:
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
