"""
Cron control-plane state backed by ``.fx/fx.db``.
"""

from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timezone
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from registers.db.registry import _ModelManager


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_root(root: str | Path | None = None) -> Path:
    target = Path.cwd() if root is None else Path(root)
    return target.resolve()


def fx_home(root: str | Path | None = None) -> Path:
    root_path = resolve_root(root)
    base = root_path / ".fx"
    legacy = root_path / ".registers"
    if not base.exists() and legacy.exists():
        try:
            legacy.rename(base)
        except OSError:
            pass
    base.mkdir(parents=True, exist_ok=True)
    return base


def control_db_path(root: str | Path | None = None) -> Path:
    return fx_home(root) / "fx.db"


@dataclass(kw_only=True)
class CronJobRecord:
    id: int | None = None
    job_key: str
    project_root: str
    name: str
    target: str
    trigger_kind: str
    trigger_config: str
    deployment_file: str = ""
    enabled: bool = True
    max_runtime: int = 0
    tags: str = "[]"
    overlap_policy: str = "skip"
    retry_policy: str = "none"
    retry_max_attempts: int = 0
    retry_backoff_seconds: float = 0.0
    retry_max_backoff_seconds: float = 0.0
    retry_jitter_seconds: float = 0.0
    handler_module: str
    handler_qualname: str
    created_at: str
    updated_at: str


@dataclass(kw_only=True)
class CronRunRecord:
    id: int | None = None
    project_root: str
    job_name: str
    event_id: int | None = None
    status: str
    message: str = ""
    started_at: str
    finished_at: str
    duration_ms: int = 0


@dataclass(kw_only=True)
class CronEventRecord:
    id: int | None = None
    project_root: str
    job_name: str
    source: str
    payload: str = "{}"
    status: str = "pending"
    created_at: str
    processed_at: str = ""
    error: str = ""


@dataclass(kw_only=True)
class CronRuntimeRecord:
    id: int | None = None
    project_root: str
    pid: int
    status: str
    workers: int = 4
    started_at: str
    last_heartbeat: str
    updated_at: str


@dataclass(kw_only=True)
class CronWorkflowRecord:
    id: int | None = None
    workflow_key: str
    project_root: str
    name: str
    file_path: str
    target: str = "local_async"
    job_name: str = ""
    command: str = ""
    enabled: bool = True
    metadata: str = "{}"
    created_at: str
    updated_at: str


def _db_file(root: str | Path | None = None) -> str:
    return str(control_db_path(root))


@lru_cache(maxsize=64)
def _job_registry(db_file: str) -> _ModelManager[CronJobRecord]:
    return _ModelManager(
        CronJobRecord,
        db_file,
        table_name="fx_cron_jobs",
        key_field="id",
        autoincrement=True,
        unique_fields=["job_key"],
    )


@lru_cache(maxsize=64)
def _run_registry(db_file: str) -> _ModelManager[CronRunRecord]:
    return _ModelManager(
        CronRunRecord,
        db_file,
        table_name="fx_cron_runs",
        key_field="id",
        autoincrement=True,
    )


@lru_cache(maxsize=64)
def _event_registry(db_file: str) -> _ModelManager[CronEventRecord]:
    return _ModelManager(
        CronEventRecord,
        db_file,
        table_name="fx_cron_events",
        key_field="id",
        autoincrement=True,
    )


@lru_cache(maxsize=64)
def _runtime_registry(db_file: str) -> _ModelManager[CronRuntimeRecord]:
    return _ModelManager(
        CronRuntimeRecord,
        db_file,
        table_name="fx_cron_runtime",
        key_field="id",
        autoincrement=True,
        unique_fields=["project_root"],
    )


@lru_cache(maxsize=64)
def _workflow_registry(db_file: str) -> _ModelManager[CronWorkflowRecord]:
    return _ModelManager(
        CronWorkflowRecord,
        db_file,
        table_name="fx_cron_workflows",
        key_field="id",
        autoincrement=True,
        unique_fields=["workflow_key"],
    )


def cron_job_registry(root: str | Path | None = None) -> _ModelManager[CronJobRecord]:
    return _job_registry(_db_file(root))


def cron_run_registry(root: str | Path | None = None) -> _ModelManager[CronRunRecord]:
    return _run_registry(_db_file(root))


def cron_event_registry(root: str | Path | None = None) -> _ModelManager[CronEventRecord]:
    return _event_registry(_db_file(root))


def cron_runtime_registry(root: str | Path | None = None) -> _ModelManager[CronRuntimeRecord]:
    return _runtime_registry(_db_file(root))


def cron_workflow_registry(root: str | Path | None = None) -> _ModelManager[CronWorkflowRecord]:
    return _workflow_registry(_db_file(root))


def upsert_runtime(
    *,
    root: str | Path | None,
    pid: int,
    status: str,
    workers: int,
) -> CronRuntimeRecord:
    root_path = str(resolve_root(root))
    reg = cron_runtime_registry(root_path)
    existing = reg.get(project_root=root_path)
    created_at = existing.started_at if existing is not None else utc_now()
    return reg.upsert(
        id=getattr(existing, "id", None),
        project_root=root_path,
        pid=pid,
        status=status,
        workers=workers,
        started_at=created_at,
        last_heartbeat=utc_now(),
        updated_at=utc_now(),
    )


def heartbeat_runtime(root: str | Path | None) -> CronRuntimeRecord | None:
    root_path = str(resolve_root(root))
    reg = cron_runtime_registry(root_path)
    existing = reg.get(project_root=root_path)
    if existing is None:
        return None
    return reg.upsert(
        id=existing.id,
        project_root=existing.project_root,
        pid=existing.pid,
        status=existing.status,
        workers=existing.workers,
        started_at=existing.started_at,
        last_heartbeat=utc_now(),
        updated_at=utc_now(),
    )


def mark_runtime_stopped(root: str | Path | None) -> CronRuntimeRecord | None:
    root_path = str(resolve_root(root))
    reg = cron_runtime_registry(root_path)
    existing = reg.get(project_root=root_path)
    if existing is None:
        return None
    return reg.upsert(
        id=existing.id,
        project_root=existing.project_root,
        pid=existing.pid,
        status="stopped",
        workers=existing.workers,
        started_at=existing.started_at,
        last_heartbeat=existing.last_heartbeat,
        updated_at=utc_now(),
    )


def create_event(
    *,
    root: str | Path | None,
    job_name: str,
    source: str,
    payload: dict[str, Any] | None = None,
    status: str = "pending",
) -> CronEventRecord:
    root_path = str(resolve_root(root))
    return cron_event_registry(root_path).create(
        project_root=root_path,
        job_name=job_name,
        source=source,
        payload=json.dumps(payload or {}, sort_keys=True),
        status=status,
        created_at=utc_now(),
        processed_at="",
        error="",
    )


def mark_event(
    event: CronEventRecord,
    *,
    status: str,
    error: str = "",
) -> CronEventRecord:
    root_path = event.project_root
    reg = cron_event_registry(root_path)
    return reg.upsert(
        id=event.id,
        project_root=event.project_root,
        job_name=event.job_name,
        source=event.source,
        payload=event.payload,
        status=status,
        created_at=event.created_at,
        processed_at=utc_now() if status in {"processed", "failed", "skipped"} else event.processed_at,
        error=error,
    )


def record_run(
    *,
    root: str | Path | None,
    job_name: str,
    event_id: int | None,
    status: str,
    message: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
) -> CronRunRecord:
    root_path = str(resolve_root(root))
    return cron_run_registry(root_path).create(
        project_root=root_path,
        job_name=job_name,
        event_id=event_id,
        status=status,
        message=message,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
    )


def sync_registry_to_state(root: str | Path | None, entries: list[Any]) -> list[str]:
    root_path = str(resolve_root(root))
    reg = cron_job_registry(root_path)
    synced: list[str] = []
    for entry in entries:
        job_key = f"{root_path}:{entry.name}"
        existing = reg.get(job_key=job_key)
        created_at = existing.created_at if existing is not None else utc_now()
        reg.upsert(
            id=getattr(existing, "id", None),
            job_key=job_key,
            project_root=root_path,
            name=entry.name,
            target=entry.target,
            trigger_kind=entry.trigger.kind,
            trigger_config=json.dumps(entry.trigger.config, sort_keys=True),
            deployment_file=entry.deployment_file,
            enabled=entry.enabled,
            max_runtime=entry.max_runtime,
            tags=json.dumps(list(entry.tags)),
            overlap_policy=entry.overlap_policy,
            retry_policy=entry.retry_policy,
            retry_max_attempts=getattr(entry, "retry_max_attempts", 0),
            retry_backoff_seconds=getattr(entry, "retry_backoff_seconds", 0.0),
            retry_max_backoff_seconds=getattr(entry, "retry_max_backoff_seconds", 0.0),
            retry_jitter_seconds=getattr(entry, "retry_jitter_seconds", 0.0),
            handler_module=entry.handler_module,
            handler_qualname=entry.handler_qualname,
            created_at=created_at,
            updated_at=utc_now(),
        )
        synced.append(entry.name)
    return synced


def parse_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return fallback


def clear_state_caches() -> None:
    _job_registry.cache_clear()
    _run_registry.cache_clear()
    _event_registry.cache_clear()
    _runtime_registry.cache_clear()
    _workflow_registry.cache_clear()
