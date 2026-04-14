"""
db_registry.engine
~~~~~~~~~~~~~~~~~~
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

import threading
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool, StaticPool


_lock: threading.Lock = threading.Lock()
_engines: dict[str, Engine] = {}


def get_engine(database_url: str) -> Engine:
    """
    Return a cached :class:`~sqlalchemy.engine.Engine` for *database_url*,
    creating one on first call.

    Thread-safe: a single lock serialises engine construction so parallel
    decorator calls for the same database don't race.
    """
    with _lock:
        if database_url not in _engines:
            _engines[database_url] = _create_engine(database_url)
        return _engines[database_url]


def dispose_engine(database_url: str) -> None:
    """Dispose the engine for *database_url* and remove it from the cache."""
    with _lock:
        engine = _engines.pop(database_url, None)
    if engine is not None:
        engine.dispose()


def dispose_all() -> None:
    """Dispose every cached engine. Call on application shutdown."""
    with _lock:
        urls = list(_engines.keys())
    for url in urls:
        dispose_engine(url)


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