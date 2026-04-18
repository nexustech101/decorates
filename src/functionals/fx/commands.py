"""
Command surface for ``functionals.fx`` project management tooling.

This module intentionally uses an isolated ``CommandRegistry`` so it does not
pollute the default ``functionals.cli`` command registry in application code.
"""

from __future__ import annotations

from collections.abc import Sequence
import importlib
from pathlib import Path
import sys
from typing import Any, Literal

from functionals.cli.registry import CommandRegistry, MISSING
from functionals.fx.scaffold import (
    ScaffoldResult,
    create_module_layout,
    create_plugin_link,
    discover_local_plugins,
    init_project_layout,
    normalize_identifier,
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


@register(name="init", description="Initialize a cli or db project scaffold and fx control database")
@option("--init")
@argument("project_type", type=str, default="cli", help="Project type: cli or db")
@argument("project_name", type=str, default="", help="Project display name; defaults to root folder name")
@argument("root", type=str, default="", help="Project root path; defaults to <project_name> when provided")
@argument("force", type=bool, default=False, help="Overwrite scaffold files if they already exist")
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
        root_input = name or "."
    root_path = resolve_root(root_input)
    root_path.mkdir(parents=True, exist_ok=True)
    if not name:
        name = root_path.name

    scaffold = init_project_layout(
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
    return _render_scaffold_result(
        title=f"Initialized {normalized_type} project '{name}' at {root_path}",
        root=root_path,
        result=scaffold,
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

    registered_aliases = [plugin.alias for plugin in plugins]
    missing_on_disk = sorted(set(registered_aliases) - set(local_plugins))
    untracked_on_disk = sorted(set(local_plugins) - set(registered_aliases))

    lines = [
        f"Root: {root_path}",
        f"Project record: {'present' if project else 'missing'}",
        f"Project type: {getattr(project, 'project_type', 'unknown') if project else 'unknown'}",
        f"app.py: {'present' if (root_path / 'app.py').exists() else 'missing'}",
        f"models.py: {'present' if (root_path / 'models.py').exists() else 'missing'}",
        f"plugins package: {'present' if (root_path / 'plugins' / '__init__.py').exists() else 'missing'}",
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


@register(name="module-add", description="Scaffold a new cli or db module under plugins/")
@option("--module-add")
@argument("module_type", type=Literal["cli", "db"], help="Module kind: cli or db")
@argument("module_name", type=str, help="Module identifier (Python identifier form)")
@argument("root", type=str, default=".", help="Project root path")
@argument("force", type=bool, default=False, help="Overwrite scaffold files if they already exist")
def module_add(
    module_type: Literal["cli", "db"],
    module_name: str,
    root: str = ".",
    force: bool = False,
) -> str:
    root_path = resolve_root(root)
    normalized = normalize_identifier(module_name)

    scaffold = create_module_layout(
        root=root_path,
        module_type=module_type,
        module_name=normalized,
        force=force,
    )

    modules = module_registry(root_path)
    package_path = f"plugins.{normalized}"
    existing = modules.get(package_path=package_path)
    created_at = existing.created_at if existing is not None else utc_now()
    modules.upsert(
        project_root=str(root_path),
        module_type=module_type,
        module_name=normalized,
        package_path=package_path,
        entry_file=str(scaffold.entry_file or ""),
        created_at=created_at,
        updated_at=utc_now(),
    )

    plugins = plugin_registry(root_path)
    existing_plugin = plugins.get(alias=normalized)
    plugin_created_at = existing_plugin.created_at if existing_plugin is not None else utc_now()
    plugins.upsert(
        project_root=str(root_path),
        alias=normalized,
        package_path=package_path,
        enabled=True,
        link_file=str(root_path / "plugins" / normalized / "__init__.py"),
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
        message=f"Scaffolded {module_type} module '{normalized}'.",
    )
    return _render_scaffold_result(
        title=f"Scaffolded {module_type} module '{normalized}'",
        root=root_path,
        result=scaffold,
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

    scaffold = create_plugin_link(
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
        link_file=str(scaffold.entry_file or ""),
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
    return _render_scaffold_result(
        title=f"Linked plugin '{resolved_alias}' -> {package_path}",
        root=root_path,
        result=scaffold,
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


@register(name="health", description="Validate scaffold health and plugin importability")
@option("--health")
@option("--doctor")
@argument("root", type=str, default=".", help="Project root path")
def health(root: str = ".") -> str:
    root_path = resolve_root(root)
    failures: list[str] = []
    project = project_registry(root_path).get(root_path=str(root_path))
    project_type = getattr(project, "project_type", "")
    if not project_type:
        project_type = "db" if (root_path / "models.py").exists() and not (root_path / "app.py").exists() else "cli"

    if project_type == "cli":
        if not (root_path / "app.py").exists():
            failures.append("Missing app.py")
    elif project_type == "db":
        if not (root_path / "models.py").exists():
            failures.append("Missing models.py")
    else:
        failures.append(f"Unsupported project type '{project_type}'.")

    if not (root_path / "plugins" / "__init__.py").exists():
        failures.append("Missing plugins/__init__.py")

    original_sys_path = list(sys.path)
    try:
        if str(root_path) not in sys.path:
            sys.path.insert(0, str(root_path))

        for alias in discover_local_plugins(root_path):
            dotted = f"plugins.{alias}"
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
    shell_description: str = "Manage Functionals projects, modules, and plugin scaffolds.",
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
        shell_colors=shell_colors,
        shell_usage=shell_usage,
    )


def get_registry() -> CommandRegistry:
    return _registry


def main(argv: Sequence[str] | None = None) -> Any:
    return run(argv)


def _render_scaffold_result(*, title: str, root: Path, result: ScaffoldResult) -> str:
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
