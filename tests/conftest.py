from __future__ import annotations

import subprocess
import sys
import time
import uuid
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from registers.db import dispose_all

DOCKER_COMPOSE_FILE = ROOT / "docker-compose.test-db.yml"
BACKEND_URLS = {
    "postgres": "postgresql+psycopg://registers:registers@127.0.0.1:54329/registers_test",
    "mysql": "mysql+pymysql://registers:registers@127.0.0.1:33069/registers_test",
}


@pytest.fixture(autouse=True)
def _dispose_engines():
    """Ensure engine connections are cleaned up between every test."""
    yield
    dispose_all()


def db_url(tmp_path: Path, name: str = "test") -> str:
    return f"sqlite:///{tmp_path / f'{name}.db'}"


def _compose_output(*args: str) -> str:
    cmd = ["docker", "compose", "-f", str(DOCKER_COMPOSE_FILE), *args]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return "docker compose unavailable"
    out = result.stdout.strip()
    err = result.stderr.strip()
    return "\n".join(part for part in (out, err) if part)


def _wait_for_database(url: str, *, timeout_seconds: int = 240, service_name: str | None = None) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None

    while time.time() < deadline:
        engine = create_engine(url, future=True, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # pragma: no cover - exercised on failures
            last_error = exc
            time.sleep(1.0)
        finally:
            engine.dispose()

    ps_output = _compose_output("ps")
    target = [service_name] if service_name else ["postgres", "mysql"]
    logs_output = _compose_output("logs", "--tail", "200", *target)
    raise RuntimeError(
        f"Database '{url}' was not ready in {timeout_seconds}s: {last_error}\n\n"
        f"docker compose ps:\n{ps_output}\n\n"
        f"docker compose logs:\n{logs_output}"
    )


@pytest.fixture(scope="session")
def docker_backends() -> dict[str, str]:
    up_cmd = [
        "docker",
        "compose",
        "-f",
        str(DOCKER_COMPOSE_FILE),
        "up",
        "-d",
        "postgres",
        "mysql",
    ]
    down_cmd = [
        "docker",
        "compose",
        "-f",
        str(DOCKER_COMPOSE_FILE),
        "down",
        "-v",
    ]

    try:
        subprocess.run(up_cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Docker is required to run the PostgreSQL/MySQL integration tests."
        ) from exc
    except subprocess.CalledProcessError as exc:
        ps_output = _compose_output("ps")
        logs_output = _compose_output("logs", "--tail", "200", "postgres", "mysql")
        raise RuntimeError(
            "Failed to start docker compose services for backend tests.\n"
            f"stdout:\n{exc.stdout}\n\nstderr:\n{exc.stderr}\n\n"
            f"docker compose ps:\n{ps_output}\n\n"
            f"docker compose logs:\n{logs_output}"
        ) from exc

    try:
        yield BACKEND_URLS
    finally:
        subprocess.run(down_cmd, check=False, capture_output=True, text=True)


@pytest.fixture()
def backend_url(request: pytest.FixtureRequest, docker_backends: dict[str, str]) -> str:
    backend_name = request.param
    if backend_name not in docker_backends:
        raise RuntimeError(
            f"Unknown backend '{backend_name}'. Expected one of: {sorted(docker_backends)}"
        )
    service_name = "postgres" if backend_name == "postgres" else "mysql"
    timeout_seconds = int(os.getenv("REGISTER_BACKEND_TIMEOUT_SECONDS", "240"))
    _wait_for_database(
        docker_backends[backend_name],
        timeout_seconds=timeout_seconds,
        service_name=service_name,
    )
    return docker_backends[backend_name]


def backend_table_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"
