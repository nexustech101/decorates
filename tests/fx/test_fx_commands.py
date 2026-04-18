from __future__ import annotations

from pathlib import Path
from typing import Any, Generator

import pytest

from functionals.fx import run
from functionals.fx.state import clear_state_caches


@pytest.fixture(autouse=True)
def _clear_fx_state_caches() -> Generator[Any]:
    clear_state_caches()
    yield
    clear_state_caches()


def test_fx_init_creates_scaffold_and_project_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = run(["init", "DemoProject"], print_result=False)
    assert "Initialized cli project 'DemoProject'" in result

    project_root = tmp_path / "DemoProject"
    assert (project_root / "app.py").exists()
    assert (project_root / "plugins" / "__init__.py").exists()
    assert (project_root / ".functionals" / "fx.db").exists()

    status = run(["status", str(project_root)], print_result=False)
    assert "Project record: present" in status
    assert "Project type: cli" in status
    assert "plugins package: present" in status


def test_fx_module_add_cli_scaffolds_files_and_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    result = run(["module-add", "cli", "users"], print_result=False)
    assert "Scaffolded cli module 'users'" in result

    assert (tmp_path / "plugins" / "users" / "__init__.py").exists()
    assert (tmp_path / "plugins" / "users" / "users.py").exists()

    module_list = run(["module-list"], print_result=False)
    assert "users  (cli)  plugins.users" in module_list

    plugin_list = run(["plugin-list"], print_result=False)
    assert "users  ->  plugins.users  (enabled)" in plugin_list


def test_fx_plugin_link_creates_alias_and_health_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    result = run(["plugin-link", "math", "math_ops"], print_result=False)
    assert "Linked plugin 'math_ops' -> math" in result

    link_file = tmp_path / "plugins" / "math_ops" / "__init__.py"
    assert link_file.exists()
    assert "from math import *" in link_file.read_text(encoding="utf-8")

    health = run(["health"], print_result=False)
    assert "Health checks passed." in health


def test_fx_history_tracks_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "HistoryProject", "."], print_result=False)
    run(["module-add", "db", "audit"], print_result=False)
    run(["health"], print_result=False)

    history = run(["history", "10"], print_result=False)
    assert "Recent operations:" in history
    assert "init" in history
    assert "module-add" in history
    assert "health" in history


def test_fx_init_db_creates_db_scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = run(["init", "db", "DataProject"], print_result=False)
    assert "Initialized db project 'DataProject'" in result

    project_root = tmp_path / "DataProject"
    assert (project_root / "models.py").exists()
    assert (project_root / "plugins" / "__init__.py").exists()

    status = run(["status", str(project_root)], print_result=False)
    assert "Project type: db" in status
    assert "models.py: present" in status

    health = run(["health", str(project_root)], print_result=False)
    assert "Health checks passed." in health
