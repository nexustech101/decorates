from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
from typing import Any, Generator
from types import SimpleNamespace

import pytest

from functionals.fx import run
from functionals.fx.commands import FX_VERSION, main as fx_main
from functionals.fx.state import clear_state_caches


@pytest.fixture(autouse=True)
def _clear_fx_state_caches() -> Generator[Any]:
    clear_state_caches()
    yield
    clear_state_caches()


def test_fx_init_creates_structure_and_project_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = run(["init", "DemoProject"], print_result=False)
    assert "Initialized cli project 'DemoProject'" in result

    project_root = tmp_path / "DemoProject"
    assert (project_root / "pyproject.toml").exists()
    assert (project_root / "README.md").exists()
    assert (project_root / "src" / "app" / "__main__.py").exists()
    assert (project_root / "src" / "app" / "todo.py").exists()
    assert (project_root / "src" / "app" / "plugins" / "__init__.py").exists()
    assert (project_root / "tests" / "test_todo_cli.py").exists()
    assert (project_root / ".fx" / "fx.db").exists()
    todo_content = (project_root / "src" / "app" / "todo.py").read_text(encoding="utf-8")
    assert "@cli.register(name=\"add\"" in todo_content
    assert "class TodoItem(BaseModel)" in todo_content
    assert "cli.load_plugins(\"app.plugins\"" in todo_content

    status = run(["status", str(project_root)], print_result=False)
    assert "Project record: present" in status
    assert "Project type: cli" in status
    assert "plugins package: present" in status


def test_fx_init_cli_dot_uses_current_directory_and_app_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = run(["init", "cli", "."], print_result=False)
    assert f"Initialized cli project '{tmp_path.name}'" in result

    assert (tmp_path / "src" / "app" / "__main__.py").exists()
    assert (tmp_path / "src" / "app" / "todo.py").exists()
    assert (tmp_path / "src" / "app" / "plugins" / "__init__.py").exists()


def test_fx_module_add_cli_structures_files_and_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    result = run(["module-add", "cli", "users"], print_result=False)
    assert "Structured cli module 'users'" in result

    assert (tmp_path / "src" / "app" / "plugins" / "users" / "__init__.py").exists()
    assert (tmp_path / "src" / "app" / "plugins" / "users" / "users.py").exists()

    module_list = run(["module-list"], print_result=False)
    assert "users  (cli)  app.plugins.users" in module_list

    plugin_list = run(["plugin-list"], print_result=False)
    assert "users  ->  app.plugins.users  (enabled)" in plugin_list


def test_fx_plugin_link_creates_alias_and_health_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    result = run(["plugin-link", "math", "math_ops"], print_result=False)
    assert "Linked plugin 'math_ops' -> math" in result

    link_file = tmp_path / "src" / "app" / "plugins" / "math_ops" / "__init__.py"
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


def test_fx_init_db_creates_db_structure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = run(["init", "db", "DataProject"], print_result=False)
    assert "Initialized db project 'DataProject'" in result

    project_root = tmp_path / "DataProject"
    assert (project_root / "pyproject.toml").exists()
    assert (project_root / "src" / "app" / "api.py").exists()
    assert (project_root / "src" / "app" / "models.py").exists()
    assert (project_root / "src" / "app" / "plugins" / "__init__.py").exists()
    assert (project_root / "tests" / "test_user_api.py").exists()
    api_content = (project_root / "src" / "app" / "api.py").read_text(encoding="utf-8")
    assert "FastAPI" in api_content
    assert "@app.post(\"/users\"" in api_content

    status = run(["status", str(project_root)], print_result=False)
    assert "Project type: db" in status
    assert "package models.py: present" in status

    health = run(["health", str(project_root)], print_result=False)
    assert "Health checks passed." in health


def test_fx_run_selects_cli_and_db_entrypoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli_root = tmp_path / "cli_proj"
    db_root = tmp_path / "db_proj"
    run(["init", "cli", "CliProj", str(cli_root)], print_result=False)
    run(["init", "db", "DbProj", str(db_root)], print_result=False)

    calls: list[tuple[list[str], Path | None]] = []

    def _fake_run_checked(argv: list[str], *, cwd: Path | None = None):
        calls.append((list(argv), cwd))
        return SimpleNamespace(returncode=0, argv=tuple(argv))

    monkeypatch.setattr("functionals.fx.commands.run_checked", _fake_run_checked)

    run(["run", str(cli_root)], print_result=False)
    run(["run", str(db_root), "--host", "0.0.0.0", "--port", "9000", "--reload"], print_result=False)

    assert calls
    assert calls[0][0][:3] == [sys.executable, "-m", "app"]
    assert calls[0][1] == cli_root
    assert calls[1][0][:4] == [sys.executable, "-m", "uvicorn", "app.api:app"]
    assert "--reload" in calls[1][0]
    assert calls[1][1] == db_root


def test_fx_install_builds_editable_install_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    calls: list[list[str]] = []
    progress_calls: list[tuple[int, str, Any]] = []

    class _Progress:
        def __init__(self) -> None:
            self.updated = 0
            self.messages: list[str] = []

        def update(self, amount: int = 1) -> None:
            self.updated += amount

        def set_postfix_str(self, message: str) -> None:
            self.messages.append(message)

    class _ProgressContext:
        def __init__(self, progress: _Progress) -> None:
            self._progress = progress

        def __enter__(self) -> _Progress:
            return self._progress

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def _fake_run_checked(argv: list[str], *, cwd: Path | None = None):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, argv=tuple(argv))

    def _fake_progress_steps(*, total: int, desc: str):
        progress = _Progress()
        progress_calls.append((total, desc, progress))
        return _ProgressContext(progress)

    monkeypatch.setattr("functionals.fx.commands.run_checked", _fake_run_checked)
    monkeypatch.setattr("functionals.fx.commands.progress_steps", _fake_progress_steps)

    run(["install", ".", "--extras", "dev,docs"], print_result=False)
    assert calls
    assert calls[0][:5] == [sys.executable, "-m", "pip", "install", "-e"]
    assert calls[0][5].endswith("[dev,docs]")
    assert progress_calls[0][0] == 3
    assert progress_calls[0][1] == "fx install"
    assert progress_calls[0][2].updated == 3

    def _fake_venv_python(_root: Path, _venv_path: str) -> Path:
        return Path("C:/tmp/fake-python")

    monkeypatch.setattr("functionals.fx.commands.ensure_venv_python", _fake_venv_python)
    run(["install", ".", "--venv-path", ".venv"], print_result=False)
    assert calls[1][:5] == [str(Path("C:/tmp/fake-python")), "-m", "pip", "install", "-e"]
    assert progress_calls[1][0] == 3
    assert progress_calls[1][1] == "fx install"
    assert progress_calls[1][2].updated == 3


def test_fx_update_builds_source_specific_commands_and_validates_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    calls: list[list[str]] = []
    progress_calls: list[tuple[int, str, Any]] = []

    class _Progress:
        def __init__(self) -> None:
            self.updated = 0
            self.messages: list[str] = []

        def update(self, amount: int = 1) -> None:
            self.updated += amount

        def set_postfix_str(self, message: str) -> None:
            self.messages.append(message)

    class _ProgressContext:
        def __init__(self, progress: _Progress) -> None:
            self._progress = progress

        def __enter__(self) -> _Progress:
            return self._progress

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def _fake_run_checked(argv: list[str], *, cwd: Path | None = None):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, argv=tuple(argv))

    def _fake_progress_steps(*, total: int, desc: str):
        progress = _Progress()
        progress_calls.append((total, desc, progress))
        return _ProgressContext(progress)

    monkeypatch.setattr("functionals.fx.commands.run_checked", _fake_run_checked)
    monkeypatch.setattr("functionals.fx.commands.progress_steps", _fake_progress_steps)

    run(["update", "."], print_result=False)
    assert calls[0] == [sys.executable, "-m", "pip", "install", "--upgrade", "decorates"]
    assert progress_calls[0][0] == 3
    assert progress_calls[0][1] == "fx update"
    assert progress_calls[0][2].updated == 3

    run(
        [
            "update",
            ".",
            "--source",
            "git",
            "--repo",
            "https://github.com/nexustech101/functionals.git",
            "--ref",
            "main",
        ],
        print_result=False,
    )
    assert calls[1][-1] == "git+https://github.com/nexustech101/functionals.git@main#egg=decorates"
    assert progress_calls[1][0] == 3
    assert progress_calls[1][1] == "fx update"
    assert progress_calls[1][2].updated == 3

    source_path = tmp_path / "source_pkg"
    source_path.mkdir()
    run(["update", ".", "--source", "path", "--path", str(source_path)], print_result=False)
    assert calls[2][-1] == str(source_path.resolve())
    assert progress_calls[2][0] == 3
    assert progress_calls[2][1] == "fx update"
    assert progress_calls[2][2].updated == 3

    with pytest.raises(Exception, match="requires --repo"):
        run(["update", ".", "--source", "git"], print_result=False)

    with pytest.raises(Exception, match="does not accept --repo"):
        run(["update", ".", "--source", "path", "--repo", "x", "--path", str(source_path)], print_result=False)


def test_fx_update_preserves_data_and_cache_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    fx_db = tmp_path / ".fx" / "fx.db"
    fx_db_bytes = fx_db.read_bytes()
    cache_dir = tmp_path / ".fx"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / "cache.json"
    cache_file.write_text('{"cached": true}', encoding="utf-8")
    app_db = tmp_path / "users.db"
    app_db.write_text("data", encoding="utf-8")

    def _fake_run_checked(argv: list[str], *, cwd: Path | None = None):
        return SimpleNamespace(returncode=0, argv=tuple(argv))

    monkeypatch.setattr("functionals.fx.commands.run_checked", _fake_run_checked)
    run(["update", "."], print_result=False)

    assert fx_db.read_bytes() == fx_db_bytes
    assert cache_file.read_text(encoding="utf-8") == '{"cached": true}'
    assert app_db.read_text(encoding="utf-8") == "data"


def test_fx_pull_syncs_plugins_and_updates_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)
    run(["module-add", "cli", "users"], print_result=False)

    checkout = tmp_path / "checkout"
    (checkout / "plugins" / "alpha").mkdir(parents=True)
    (checkout / "plugins" / "alpha" / "__init__.py").write_text("VALUE='alpha'\n", encoding="utf-8")
    (checkout / "plugins" / "users").mkdir(parents=True)
    (checkout / "plugins" / "users" / "__init__.py").write_text("VALUE='remote-users'\n", encoding="utf-8")

    @dataclass(frozen=True)
    class _Clone:
        repo_path: Path

    def _fake_clone_repo(*, repo_url: str, ref: str = "main"):
        clone_copy = tmp_path / "clone_copy"
        if clone_copy.exists():
            shutil.rmtree(clone_copy)
        shutil.copytree(checkout, clone_copy)
        return _Clone(repo_path=clone_copy)

    monkeypatch.setattr("functionals.fx.commands.clone_repo", _fake_clone_repo)

    result = run(["pull", str(checkout), "."], print_result=False)
    assert "created=1" in result
    assert "skipped=1" in result

    alpha_init = tmp_path / "src" / "app" / "plugins" / "alpha" / "__init__.py"
    assert alpha_init.exists()
    assert "VALUE='alpha'" in alpha_init.read_text(encoding="utf-8")

    plugin_list = run(["plugin-list"], print_result=False)
    assert "alpha  ->  app.plugins.alpha  (enabled)" in plugin_list


def test_fx_pull_force_overwrites_existing_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    existing = tmp_path / "src" / "app" / "plugins" / "users"
    existing.mkdir(parents=True, exist_ok=True)
    (existing / "__init__.py").write_text("VALUE='local'\n", encoding="utf-8")

    checkout = tmp_path / "checkout_force"
    (checkout / "plugins" / "users").mkdir(parents=True)
    (checkout / "plugins" / "users" / "__init__.py").write_text("VALUE='remote'\n", encoding="utf-8")

    @dataclass(frozen=True)
    class _Clone:
        repo_path: Path

    def _fake_clone_repo(*, repo_url: str, ref: str = "main"):
        clone_copy = tmp_path / "clone_copy_force"
        if clone_copy.exists():
            shutil.rmtree(clone_copy)
        shutil.copytree(checkout, clone_copy)
        return _Clone(repo_path=clone_copy)

    monkeypatch.setattr("functionals.fx.commands.clone_repo", _fake_clone_repo)
    run(["pull", str(checkout), ".", "--force"], print_result=False)

    init_content = (existing / "__init__.py").read_text(encoding="utf-8")
    assert "remote" in init_content


def test_fx_history_includes_new_runtime_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run(["init", "DemoProject", "."], print_result=False)

    def _fake_run_checked(argv: list[str], *, cwd: Path | None = None):
        return SimpleNamespace(returncode=0, argv=tuple(argv))

    @dataclass(frozen=True)
    class _Clone:
        repo_path: Path

    checkout = tmp_path / "checkout_history"
    (checkout / "plugins" / "alpha").mkdir(parents=True)
    (checkout / "plugins" / "alpha" / "__init__.py").write_text("VALUE='alpha'\n", encoding="utf-8")

    def _fake_clone_repo(*, repo_url: str, ref: str = "main"):
        clone_copy = tmp_path / "clone_copy_history"
        if clone_copy.exists():
            shutil.rmtree(clone_copy)
        shutil.copytree(checkout, clone_copy)
        return _Clone(repo_path=clone_copy)

    monkeypatch.setattr("functionals.fx.commands.run_checked", _fake_run_checked)
    monkeypatch.setattr("functionals.fx.commands.clone_repo", _fake_clone_repo)

    run(["run", "."], print_result=False)
    run(["install", "."], print_result=False)
    run(["update", "."], print_result=False)
    run(["pull", str(checkout), "."], print_result=False)

    history = run(["history", "20"], print_result=False)
    assert "run" in history
    assert "install" in history
    assert "update" in history
    assert "pull" in history


def test_fx_version_option_returns_current_version() -> None:
    result = run(["--version"], print_result=False)
    assert result == f"fx {FX_VERSION}"


def test_fx_help_includes_version_line(capsys: pytest.CaptureFixture[str]) -> None:
    run(["--help"], print_result=False)
    out = capsys.readouterr().out
    assert "Functionals FX" in out
    assert f"Version: {FX_VERSION}" in out


def test_fx_interactive_shell_prints_version_line(capsys: pytest.CaptureFixture[str]) -> None:
    lines = iter(["quit"])

    def _input(_prompt: str) -> str:
        return next(lines)

    run(
        ["--interactive"],
        print_result=False,
        shell_input_fn=_input,
        shell_banner=False,
        shell_colors=False,
    )
    out = capsys.readouterr().out
    assert "Functionals FX" in out
    assert f"Version: {FX_VERSION}" in out


def test_fx_main_version_prints_once(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = fx_main(["--version"])
    out = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 0
    assert out == [f"fx {FX_VERSION}"]


def test_fx_interactive_shell_colors_version_line(capsys: pytest.CaptureFixture[str]) -> None:
    lines = iter(["quit"])

    def _input(_prompt: str) -> str:
        return next(lines)

    run(
        ["--interactive"],
        print_result=False,
        shell_input_fn=_input,
        shell_banner=False,
        shell_colors=True,
    )
    out = capsys.readouterr().out
    assert f"\x1b[32mVersion: {FX_VERSION}\x1b[0m" in out
