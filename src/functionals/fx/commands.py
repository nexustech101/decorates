"""
Command surface for ``functionals.fx`` project management tooling.

This module intentionally uses an isolated ``CommandRegistry`` so it does not
pollute the default ``functionals.cli`` command registry in application code.
"""

from __future__ import annotations

from collections.abc import Sequence
import importlib
from importlib.metadata import PackageNotFoundError, version as resolve_distribution_version
from pathlib import Path
import shutil
import sys
from typing import Any, Literal

from functionals.cli.registry import CommandRegistry, MISSING
from functionals.fx.plugin_sync import sync_plugins_from_checkout
from functionals.fx.runtime_ops import (
    clone_repo,
    editable_install_target,
    ensure_venv_python,
    progress_steps,
    run_checked,
)
from functionals.fx.structure import (
    StructureResult,
    create_module_layout,
    create_plugin_link,
    discover_project_package,
    discover_local_plugins,
    init_project_layout,
    normalize_identifier,
    resolve_plugin_import_base,
    resolve_plugin_layout,
)
from functionals.fx.state import (
    module_registry,
    operation_registry,
    plugin_registry,
    project_registry,
    record_operation,
    resolve_root,
    utc_now,
)

_registry = CommandRegistry()
_FX_DISTRIBUTION_NAME = "decorates"


def _resolve_fx_version() -> str:
    try:
        return resolve_distribution_version(_FX_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "dev"


FX_VERSION = _resolve_fx_version()


def argument(
    name: str,
    *,
    type: Any = str,
    help: str = "",
    default: Any = MISSING,
):
    def decorator(fn):
        _registry.stage_argument(fn, name, arg_type=type, help_text=help, default=default)
        return fn

    return decorator


def option(flag: str, *, help: str = ""):
    def decorator(fn):
        _registry.stage_option(fn, flag, help_text=help)
        return fn

    return decorator


def register(name: str | None = None, *, description: str = "", help: str = ""):
    def decorator(fn):
        _registry.finalize_command(fn, name=name, description=description, help_text=help)
        return fn

    return decorator


@register(name="init", description="Initialize a cli or db project structure and fx control database")
@option("--init")
@argument("project_type", type=str, default="cli", help="Project type: cli or db")
@argument("project_name", type=str, default="", help="Project display name; defaults to root folder name")
@argument("root", type=str, default="", help="Project root path; defaults to <project_name> when provided")
@argument("force", type=bool, default=False, help="Overwrite structure files if they already exist")
def init(
    project_type: str = "cli",
    project_name: str = "",
    root: str = "",
    force: bool = False,
) -> str:
    normalized_type = project_type.strip().lower() or "cli"
    if normalized_type not in {"cli", "db"}:
        if root.strip():
            raise ValueError("project_type must be either 'cli' or 'db'.")
        # Backward-compatible shapes:
        #   fx init <project_name>
        #   fx init <project_name> <root>
        legacy_project_name = project_type
        legacy_root = project_name
        project_name = legacy_project_name
        root = legacy_root
        normalized_type = "cli"

    name = project_name.strip()
    root_input = root.strip()
    if not root_input:
        if name in {".", "./"}:
            root_input = "."
            name = ""
        else:
            root_input = name or "."
    root_path = resolve_root(root_input)
    root_path.mkdir(parents=True, exist_ok=True)
    if not name or name in {".", "./"}:
        name = root_path.name

    structure = init_project_layout(
        root=root_path,
        project_name=name,
        project_type=normalized_type,
        force=force,
    )

    projects = project_registry(root_path)
    existing = projects.get(root_path=str(root_path))
    created_at = existing.created_at if existing is not None else utc_now()
    projects.upsert(
        name=name,
        root_path=str(root_path),
        project_type=normalized_type,
        created_at=created_at,
        updated_at=utc_now(),
    )
    record_operation(
        root=root_path,
        command="init",
        arguments={
            "project_type": normalized_type,
            "project_name": name,
            "root": str(root_path),
            "force": force,
        },
        status="success",
        message=f"Initialized {normalized_type} project '{name}'.",
    )
    return _render_structure_result(
        title=f"Initialized {normalized_type} project '{name}' at {root_path}",
        root=root_path,
        result=structure,
    )


@register(name="status", description="Show current project structure and registry status")
@option("--status")
@argument("root", type=str, default=".", help="Project root path")
def status(root: str = ".") -> str:
    root_path = resolve_root(root)
    project = project_registry(root_path).get(root_path=str(root_path))
    modules = module_registry(root_path).filter(project_root=str(root_path), order_by="module_name")
    plugins = plugin_registry(root_path).filter(project_root=str(root_path), order_by="alias")
    local_plugins = discover_local_plugins(root_path)
    package_name = discover_project_package(root_path)
    plugin_layout = resolve_plugin_layout(root_path)
    src_root = root_path / "src"
    todo_file = src_root / package_name / "todo.py" if package_name else None
    api_file = src_root / package_name / "api.py" if package_name else None
    models_file = src_root / package_name / "models.py" if package_name else None

    registered_aliases = [plugin.alias for plugin in plugins]
    missing_on_disk = sorted(set(registered_aliases) - set(local_plugins))
    untracked_on_disk = sorted(set(local_plugins) - set(registered_aliases))

    lines = [
        f"Root: {root_path}",
        f"Project record: {'present' if project else 'missing'}",
        f"Project type: {getattr(project, 'project_type', 'unknown') if project else 'unknown'}",
        f"pyproject.toml: {'present' if (root_path / 'pyproject.toml').exists() else 'missing'}",
        f"src package: {package_name or 'missing'}",
        f"legacy app.py: {'present' if (root_path / 'app.py').exists() else 'missing'}",
        f"todo.py: {'present' if (todo_file and todo_file.exists()) else 'missing'}",
        f"api.py: {'present' if (api_file and api_file.exists()) else 'missing'}",
        f"legacy models.py: {'present' if (root_path / 'models.py').exists() else 'missing'}",
        f"package models.py: {'present' if (models_file and models_file.exists()) else 'missing'}",
        f"plugins package: {'present' if (plugin_layout.directory / '__init__.py').exists() else 'missing'}",
        f"plugins import base: {plugin_layout.import_base}",
        f"Registered modules: {len(modules)}",
        f"Registered plugin links: {len(plugins)}",
        f"Local plugin packages: {len(local_plugins)}",
    ]

    if missing_on_disk:
        lines.append(f"Missing on disk: {', '.join(missing_on_disk)}")
    if untracked_on_disk:
        lines.append(f"Untracked on disk: {', '.join(untracked_on_disk)}")
    if not missing_on_disk and not untracked_on_disk:
        lines.append("Registry and filesystem plugin lists are aligned.")

    return "\n".join(lines)


@register(name="module-add", description="Structure a new cli or db module under plugins/")
@option("--module-add")
@argument("module_type", type=Literal["cli", "db"], help="Module kind: cli or db")
@argument("module_name", type=str, help="Module identifier (Python identifier form)")
@argument("root", type=str, default=".", help="Project root path")
@argument("force", type=bool, default=False, help="Overwrite structure files if they already exist")
def module_add(
    module_type: Literal["cli", "db"],
    module_name: str,
    root: str = ".",
    force: bool = False,
) -> str:
    root_path = resolve_root(root)
    normalized = normalize_identifier(module_name)
    import_base = resolve_plugin_import_base(root_path)

    structure = create_module_layout(
        root=root_path,
        module_type=module_type,
        module_name=normalized,
        force=force,
    )

    modules = module_registry(root_path)
    package_path = f"{import_base}.{normalized}"
    existing = modules.get(package_path=package_path)
    created_at = existing.created_at if existing is not None else utc_now()
    modules.upsert(
        project_root=str(root_path),
        module_type=module_type,
        module_name=normalized,
        package_path=package_path,
        entry_file=str(structure.entry_file or ""),
        created_at=created_at,
        updated_at=utc_now(),
    )

    plugins = plugin_registry(root_path)
    existing_plugin = plugins.get(alias=normalized)
    plugin_created_at = existing_plugin.created_at if existing_plugin is not None else utc_now()
    link_file = str(structure.entry_file.parent / "__init__.py") if structure.entry_file is not None else ""
    plugins.upsert(
        project_root=str(root_path),
        alias=normalized,
        package_path=package_path,
        enabled=True,
        link_file=link_file,
        created_at=plugin_created_at,
        updated_at=utc_now(),
    )

    record_operation(
        root=root_path,
        command="module-add",
        arguments={
            "module_type": module_type,
            "module_name": normalized,
            "root": str(root_path),
            "force": force,
        },
        status="success",
        message=f"Structured {module_type} module '{normalized}'.",
    )
    return _render_structure_result(
        title=f"Structured {module_type} module '{normalized}'",
        root=root_path,
        result=structure,
    )


@register(name="module-list", description="List modules recorded in the fx module registry")
@option("--module-list")
@argument("root", type=str, default=".", help="Project root path")
def module_list(root: str = ".") -> str:
    root_path = resolve_root(root)
    modules = module_registry(root_path).filter(project_root=str(root_path), order_by="module_name")
    if not modules:
        return "No modules registered for this project."

    lines = ["Registered modules:"]
    for entry in modules:
        lines.append(f"  {entry.module_name}  ({entry.module_type})  {entry.package_path}")
    return "\n".join(lines)


@register(name="plugin-link", description="Create a local plugins/<alias> shim to an importable package")
@option("--plugin-link")
@argument("package_path", type=str, help="Importable dotted package path, for example 'my_app.plugins.billing'")
@argument("alias", type=str, default="", help="Local alias under plugins/; defaults to the last package segment")
@argument("root", type=str, default=".", help="Project root path")
@argument("force", type=bool, default=False, help="Overwrite plugins/<alias>/__init__.py if it already exists")
def plugin_link(
    package_path: str,
    alias: str = "",
    root: str = ".",
    force: bool = False,
) -> str:
    root_path = resolve_root(root)
    resolved_alias = normalize_identifier(alias or package_path.split(".")[-1])

    structure = create_plugin_link(
        root=root_path,
        package_path=package_path,
        alias=resolved_alias,
        force=force,
    )

    plugins = plugin_registry(root_path)
    existing = plugins.get(alias=resolved_alias)
    created_at = existing.created_at if existing is not None else utc_now()
    plugins.upsert(
        project_root=str(root_path),
        alias=resolved_alias,
        package_path=package_path,
        enabled=True,
        link_file=str(structure.entry_file or ""),
        created_at=created_at,
        updated_at=utc_now(),
    )

    record_operation(
        root=root_path,
        command="plugin-link",
        arguments={
            "package_path": package_path,
            "alias": resolved_alias,
            "root": str(root_path),
            "force": force,
        },
        status="success",
        message=f"Linked plugin '{resolved_alias}' to {package_path}.",
    )
    return _render_structure_result(
        title=f"Linked plugin '{resolved_alias}' -> {package_path}",
        root=root_path,
        result=structure,
    )


@register(name="plugin-list", description="List linked plugin aliases for the current project")
@option("--plugin-list")
@argument("root", type=str, default=".", help="Project root path")
def plugin_list(root: str = ".") -> str:
    root_path = resolve_root(root)
    plugins = plugin_registry(root_path).filter(project_root=str(root_path), order_by="alias")
    if not plugins:
        return "No plugins linked for this project."

    lines = ["Linked plugins:"]
    for entry in plugins:
        marker = "enabled" if entry.enabled else "disabled"
        lines.append(f"  {entry.alias}  ->  {entry.package_path}  ({marker})")
    return "\n".join(lines)


@register(name="version", description="Show fx version")
@option("--version")
@option("-V")
def show_version() -> str:
    return f"fx {FX_VERSION}"


@register(name="run", description="Run the structured project application")
@option("--run")
@argument("root", type=str, default=".", help="Project root path")
@argument("host", type=str, default="127.0.0.1", help="Host binding for DB/FastAPI projects")
@argument("port", type=int, default=8000, help="Port for DB/FastAPI projects")
@argument("reload", type=bool, default=False, help="Enable auto-reload for DB/FastAPI projects")
def run_project(
    root: str = ".",
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
) -> str:
    root_path = resolve_root(root)
    project_type = "cli"
    argv: list[str] = []
    cwd = root_path

    try:
        package_name = discover_project_package(root_path)
        project = project_registry(root_path).get(root_path=str(root_path))
        project_type = getattr(project, "project_type", "")

        has_cli_layout = bool(package_name and (root_path / "src" / package_name / "todo.py").exists()) or (root_path / "app.py").exists()
        has_db_layout = bool(package_name and (root_path / "src" / package_name / "api.py").exists()) or (root_path / "models.py").exists()
        if not project_type:
            project_type = "db" if has_db_layout and not has_cli_layout else "cli"

        if project_type == "db":
            if package_name:
                argv = [
                    str(Path(sys.executable)),
                    "-m",
                    "uvicorn",
                    f"{package_name}.api:app",
                    "--host",
                    host,
                    "--port",
                    str(port),
                ]
                if reload:
                    argv.append("--reload")
            else:
                raise ValueError("Could not determine package name for DB project run.")
        else:
            if package_name:
                argv = [str(Path(sys.executable)), "-m", package_name]
            elif (root_path / "app.py").exists():
                argv = [str(Path(sys.executable)), str(root_path / "app.py")]
            else:
                raise ValueError("Could not determine CLI entrypoint for project run.")

        run_checked(argv, cwd=cwd)
    except Exception as exc:
        record_operation(
            root=root_path,
            command="run",
            arguments={
                "root": str(root_path),
                "project_type": project_type,
                "host": host,
                "port": port,
                "reload": reload,
            },
            status="failure",
            message=str(exc),
        )
        raise

    record_operation(
        root=root_path,
        command="run",
        arguments={
            "root": str(root_path),
            "project_type": project_type,
            "host": host,
            "port": port,
            "reload": reload,
        },
        status="success",
        message="Application command executed successfully.",
    )
    return _render_runtime_summary(
        "FX Run Result",
        fields=[
            ("Status", "success"),
            ("Project", str(root_path)),
            ("Project type", project_type),
            ("Command", " ".join(argv)),
        ],
    )


@register(name="install", description="Install the project package in editable mode")
@option("--install")
@argument("root", type=str, default=".", help="Project root path")
@argument("venv_path", type=str, default="", help="Optional virtualenv path to create/use, for example '.venv'")
@argument("extras", type=str, default="", help="Optional extras list, for example 'dev' or 'dev,docs'")
def install_project(
    root: str = ".",
    venv_path: str = "",
    extras: str = "",
) -> str:
    root_path = resolve_root(root)
    editable_target = ""
    argv: list[str] = []

    try:
        with progress_steps(total=3, desc="fx install") as progress:
            progress.set_postfix_str("resolving python environment")
            python_exe = ensure_venv_python(root_path, venv_path)
            progress.update(1)

            progress.set_postfix_str("building editable target")
            editable_target = editable_install_target(root_path, extras)
            argv = [str(python_exe), "-m", "pip", "install", "-e", editable_target]
            progress.update(1)

            progress.set_postfix_str("running pip install -e")
            run_checked(argv, cwd=root_path)
            progress.update(1)
    except Exception as exc:
        record_operation(
            root=root_path,
            command="install",
            arguments={
                "root": str(root_path),
                "venv_path": venv_path,
                "extras": extras,
            },
            status="failure",
            message=str(exc),
        )
        raise

    record_operation(
        root=root_path,
        command="install",
        arguments={
            "root": str(root_path),
            "venv_path": venv_path,
            "extras": extras,
        },
        status="success",
        message="Editable install completed successfully.",
    )
    return _render_runtime_summary(
        "FX Install Result",
        fields=[
            ("Status", "success"),
            ("Project", str(root_path)),
            ("Target", editable_target),
            ("Command", " ".join(argv)),
        ],
    )


@register(name="update", description="Update the decorates package from selected source")
@option("--update")
@argument("root", type=str, default=".", help="Project root path")
@argument("source", type=Literal["pypi", "git", "path"], default="pypi", help="Update source: pypi, git, or path")
@argument("repo", type=str, default="", help="Git repository URL when source=git")
@argument("ref", type=str, default="main", help="Git ref/branch/tag when source=git")
@argument("path", type=str, default="", help="Local source path when source=path")
@argument("venv_path", type=str, default="", help="Optional virtualenv path to create/use")
@argument("package", type=str, default="decorates", help="Package name/egg name to upgrade")
def update_project(
    root: str = ".",
    source: Literal["pypi", "git", "path"] = "pypi",
    repo: str = "",
    ref: str = "main",
    path: str = "",
    venv_path: str = "",
    package: str = "decorates",
) -> str:
    root_path = resolve_root(root)
    pkg = package.strip() or "decorates"
    argv: list[str] = []

    try:
        with progress_steps(total=3, desc="fx update") as progress:
            progress.set_postfix_str("resolving python environment")
            python_exe = ensure_venv_python(root_path, venv_path)
            progress.update(1)

            progress.set_postfix_str("resolving update source")
            if source == "pypi":
                if repo.strip() or path.strip():
                    raise ValueError("source='pypi' does not accept --repo or --path.")
                argv = [str(python_exe), "-m", "pip", "install", "--upgrade", pkg]
            elif source == "git":
                if not repo.strip():
                    raise ValueError("source='git' requires --repo.")
                if path.strip():
                    raise ValueError("source='git' does not accept --path.")
                git_spec = f"git+{repo}@{ref}#egg={pkg}"
                argv = [str(python_exe), "-m", "pip", "install", "--upgrade", git_spec]
            else:
                if not path.strip():
                    raise ValueError("source='path' requires --path.")
                if repo.strip():
                    raise ValueError("source='path' does not accept --repo.")
                source_path = Path(path)
                if not source_path.is_absolute():
                    source_path = (root_path / source_path).resolve()
                if not source_path.exists():
                    raise FileNotFoundError(f"Update source path does not exist: {source_path}")
                argv = [str(python_exe), "-m", "pip", "install", "--upgrade", str(source_path)]
            progress.update(1)

            progress.set_postfix_str("running pip install --upgrade")
            run_checked(argv, cwd=root_path)
            progress.update(1)
    except Exception as exc:
        record_operation(
            root=root_path,
            command="update",
            arguments={
                "root": str(root_path),
                "source": source,
                "repo": repo,
                "ref": ref,
                "path": path,
                "venv_path": venv_path,
                "package": pkg,
            },
            status="failure",
            message=str(exc),
        )
        raise

    record_operation(
        root=root_path,
        command="update",
        arguments={
            "root": str(root_path),
            "source": source,
            "repo": repo,
            "ref": ref,
            "path": path,
            "venv_path": venv_path,
            "package": pkg,
        },
        status="success",
        message=f"Updated package '{pkg}' from source '{source}'.",
    )
    return _render_runtime_summary(
        "FX Update Result",
        fields=[
            ("Status", "success"),
            ("Project", str(root_path)),
            ("Source", source),
            ("Package", pkg),
            ("Command", " ".join(argv)),
        ],
    )


@register(name="pull", description="Pull plugins from a git repository")
@option("--pull")
@argument("repo_url", type=str, help="Git repository URL or local git path")
@argument("root", type=str, default=".", help="Project root path")
@argument("ref", type=str, default="main", help="Git ref/branch/tag")
@argument("subdir", type=str, default="plugins", help="Plugin directory inside the repository")
@argument("force", type=bool, default=False, help="Overwrite existing plugin directories")
def pull_plugins(
    repo_url: str,
    root: str = ".",
    ref: str = "main",
    subdir: str = "plugins",
    force: bool = False,
) -> str:
    root_path = resolve_root(root)
    arguments = {
        "repo_url": repo_url,
        "root": str(root_path),
        "ref": ref,
        "subdir": subdir,
        "force": force,
    }

    try:
        plugin_layout = resolve_plugin_layout(root_path)
        plugin_layout.directory.mkdir(parents=True, exist_ok=True)
        init_path = plugin_layout.directory / "__init__.py"
        if not init_path.exists():
            init_path.write_text("", encoding="utf-8")

        clone_result = clone_repo(repo_url=repo_url, ref=ref)
        try:
            report = sync_plugins_from_checkout(
                checkout_root=clone_result.repo_path,
                subdir=subdir,
                target_plugins_dir=plugin_layout.directory,
                force=force,
            )
        finally:
            shutil.rmtree(clone_result.repo_path, ignore_errors=True)

        import_base = resolve_plugin_import_base(root_path)
        import_failures: list[str] = []
        original_sys_path = list(sys.path)
        try:
            if str(root_path) not in sys.path:
                sys.path.insert(0, str(root_path))
            src_root = root_path / "src"
            if src_root.exists() and str(src_root) not in sys.path:
                sys.path.insert(0, str(src_root))

            root_pkg = import_base.split(".")[0]
            stale_modules = [
                key
                for key in list(sys.modules)
                if key == root_pkg or key.startswith(f"{root_pkg}.")
            ]
            for key in stale_modules:
                sys.modules.pop(key, None)

            for alias in report.synced_aliases:
                dotted = f"{import_base}.{alias}"
                try:
                    importlib.invalidate_caches()
                    importlib.import_module(dotted)
                except Exception as exc:
                    import_failures.append(f"{dotted}: {exc}")
        finally:
            sys.path[:] = original_sys_path

        if import_failures:
            message = "Import validation failed for pulled plugins: " + "; ".join(import_failures)
            raise RuntimeError(message)

        plugins = plugin_registry(root_path)
        for alias in report.synced_aliases:
            package_path = f"{import_base}.{alias}"
            existing = plugins.get(alias=alias)
            created_at = existing.created_at if existing is not None else utc_now()
            plugins.upsert(
                project_root=str(root_path),
                alias=alias,
                package_path=package_path,
                enabled=True,
                link_file=str(plugin_layout.directory / alias / "__init__.py"),
                created_at=created_at,
                updated_at=utc_now(),
            )

        summary_parts = [
            f"created={len(report.created)}",
            f"updated={len(report.updated)}",
            f"skipped={len(report.skipped)}",
        ]
        summary = ", ".join(summary_parts)
        record_operation(
            root=root_path,
            command="pull",
            arguments=arguments,
            status="success",
            message=f"Pulled plugins successfully ({summary}).",
        )

        return _render_runtime_summary(
            "FX Pull Result",
            fields=[
                ("Status", "success"),
                ("Project", str(root_path)),
                ("Repository", repo_url),
                ("Summary", summary),
            ],
            sections=[
                ("Created", report.created),
                ("Updated", report.updated),
                ("Skipped", report.skipped),
            ],
        )
    except Exception as exc:
        record_operation(
            root=root_path,
            command="pull",
            arguments=arguments,
            status="failure",
            message=str(exc),
        )
        raise


@register(name="health", description="Validate structure health and plugin importability")
@option("--health")
@option("--doctor")
@argument("root", type=str, default=".", help="Project root path")
def health(root: str = ".") -> str:
    root_path = resolve_root(root)
    failures: list[str] = []
    project = project_registry(root_path).get(root_path=str(root_path))
    project_type = getattr(project, "project_type", "")
    package_name = discover_project_package(root_path)
    import_base = resolve_plugin_import_base(root_path)
    plugin_layout = resolve_plugin_layout(root_path)
    has_legacy_cli = (root_path / "app.py").exists()
    has_legacy_db = (root_path / "models.py").exists()
    has_package_cli = bool(package_name and (root_path / "src" / package_name / "todo.py").exists())
    has_package_db = bool(
        package_name
        and (root_path / "src" / package_name / "models.py").exists()
        and (root_path / "src" / package_name / "api.py").exists()
    )
    if not project_type:
        project_type = "db" if (has_legacy_db or has_package_db) and not (has_legacy_cli or has_package_cli) else "cli"

    if project_type == "cli":
        if not (has_legacy_cli or has_package_cli):
            failures.append("Missing CLI starter (app.py or src/<package>/todo.py).")
    elif project_type == "db":
        if not (has_legacy_db or has_package_db):
            failures.append("Missing DB starter (models.py or src/<package>/models.py + api.py).")
    else:
        failures.append(f"Unsupported project type '{project_type}'.")

    if not (plugin_layout.directory / "__init__.py").exists():
        failures.append(f"Missing plugins package at {plugin_layout.directory}.")

    if not (root_path / "pyproject.toml").exists() and not (has_legacy_cli or has_legacy_db):
        failures.append("Missing pyproject.toml.")

    original_sys_path = list(sys.path)
    try:
        if str(root_path) not in sys.path:
            sys.path.insert(0, str(root_path))
        src_root = root_path / "src"
        if src_root.exists() and str(src_root) not in sys.path:
            sys.path.insert(0, str(src_root))

        for alias in discover_local_plugins(root_path):
            dotted = f"{import_base}.{alias}"
            try:
                importlib.import_module(dotted)
            except Exception as exc:
                failures.append(f"Import failed for {dotted}: {exc}")
    finally:
        sys.path[:] = original_sys_path

    status_value = "success" if not failures else "failure"
    message = "Project checks passed." if not failures else "; ".join(failures)
    record_operation(
        root=root_path,
        command="health",
        arguments={"root": str(root_path), "project_type": project_type},
        status=status_value,
        message=message,
    )

    if not failures:
        return "Health checks passed."
    return "Health checks failed:\n" + "\n".join(f"  - {failure}" for failure in failures)


@register(name="history", description="Show recent fx operation history")
@option("--history")
@argument("limit", type=int, default=20, help="Maximum number of operations to show")
@argument("root", type=str, default=".", help="Project root path")
def history(limit: int = 20, root: str = ".") -> str:
    root_path = resolve_root(root)
    rows = operation_registry(root_path).filter(project_root=str(root_path), order_by="-id", limit=limit)
    if not rows:
        return "No operation history found."

    lines = ["Recent operations:"]
    for row in rows:
        lines.append(f"  [{row.id}] {row.created_at}  {row.command}  {row.status}")
        if row.message:
            lines.append(f"      {row.message}")
    return "\n".join(lines)


def run(
    argv: Sequence[str] | None = None,
    *,
    print_result: bool = True,
    shell_prompt: str = "fx > ",
    shell_input_fn=None,
    shell_banner: bool = True,
    shell_banner_text: str | None = None,
    shell_title: str = "Functionals FX",
    shell_description: str = "Manage Functionals projects, modules, and plugin structures.",
    shell_colors: bool | None = None,
    shell_usage: bool = True,
) -> Any:
    return _registry.run(
        argv,
        print_result=print_result,
        shell_prompt=shell_prompt,
        shell_input_fn=shell_input_fn,
        shell_banner=shell_banner,
        shell_banner_text=shell_banner_text,
        shell_title=shell_title,
        shell_description=shell_description,
        shell_version=f"Version: {FX_VERSION}",
        shell_colors=shell_colors,
        shell_usage=shell_usage,
    )


def get_registry() -> CommandRegistry:
    return _registry


def main(argv: Sequence[str] | None = None) -> int:
    run(argv)
    return 0


def _render_runtime_summary(
    title: str,
    *,
    fields: Sequence[tuple[str, Any]],
    sections: Sequence[tuple[str, Sequence[str]]] = (),
) -> str:
    lines = [title]
    for key, value in fields:
        lines.append(f"{key}: {value}")
    for key, items in sections:
        if not items:
            continue
        lines.append(f"{key}: {', '.join(items)}")
    return "\n".join(lines)


def _render_structure_result(*, title: str, root: Path, result: StructureResult) -> str:
    lines = [title]
    if result.created:
        lines.append("Created:")
        lines.extend(f"  - {path.relative_to(root)}" for path in result.created)
    if result.updated:
        lines.append("Updated:")
        lines.extend(f"  - {path.relative_to(root)}" for path in result.updated)
    if result.skipped:
        lines.append("Skipped:")
        lines.extend(f"  - {path.relative_to(root)}" for path in result.skipped)
    return "\n".join(lines)
