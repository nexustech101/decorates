"""Utility helpers used by :mod:`registers.cli.registry`.

The registry owns command state and public behavior. This module keeps the
pure formatting, resolution, and environment helpers out of the registry class
so that the command runtime stays easier to scan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import get_close_matches
import inspect
import os
import sys
from types import ModuleType
from typing import Any, get_args, get_origin

from registers.cli.ux import Context
from registers.cli.utils.typing import is_bool_flag, is_optional


HELP_COMMAND_NAME = "help"
HELP_ALIASES = ("help", "--help", "-h")


class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    BOLD_CYAN = "\033[1;36m"


def normalize_alias(token: str) -> str:
    return token.lstrip("-").strip()


def derive_command_name(options: Sequence[str], fallback: str) -> str:
    for flag in options:
        if flag.startswith("--") and len(flag) > 2:
            return flag[2:]
    return fallback


def is_injected_runtime_param(name: str, annotation: Any) -> bool:
    if name in {"ctx", "context"}:
        return True
    try:
        if inspect.isclass(annotation) and issubclass(annotation, Context):
            return True
    except TypeError:
        return False
    return False


def resolve_annotation(explicit_type: Any, annotation: Any, *, missing: Any) -> Any:
    if explicit_type is not missing:
        return explicit_type
    if annotation is inspect.Parameter.empty:
        return str
    return annotation


def resolve_requirement(
    *,
    annotation: Any,
    param_has_default: bool,
    param_default: Any,
    explicit_default: Any,
    missing: Any,
) -> tuple[bool, Any]:
    if explicit_default is not missing:
        return False, explicit_default

    if param_has_default:
        return False, param_default

    if is_bool_flag(annotation):
        return False, False

    if is_optional(annotation):
        return False, None

    return True, missing


def param_accepts_context(name: str, annotation: Any, context: Any) -> bool:
    if name in {"ctx", "context"}:
        return True
    if annotation is inspect.Parameter.empty:
        return False
    try:
        return isinstance(context, annotation)
    except TypeError:
        return False


def resolve_plugin_registry(plugin: Any, registry_type: type[Any]) -> Any:
    if isinstance(plugin, registry_type):
        return plugin

    getter = getattr(plugin, "get_registry", None)
    if callable(getter):
        resolved = getter()
        if isinstance(resolved, registry_type):
            return resolved

    if isinstance(plugin, ModuleType):
        module_registry = getattr(plugin, "cli", None)
        if isinstance(module_registry, registry_type):
            return module_registry

    raise TypeError(
        "register_plugin(...) expects a CommandRegistry, an object with "
        "get_registry() returning CommandRegistry, or a module exposing "
        "a CommandRegistry as 'cli'."
    )


def suggest_command(
    token: str,
    commands: Mapping[str, Any],
    aliases: Mapping[str, str],
) -> str | None:
    candidates = set(commands)
    candidates.update(aliases)
    candidates.update({HELP_COMMAND_NAME})
    matches = get_close_matches(normalize_alias(token), sorted(candidates), n=1)
    if not matches:
        return None

    guess = matches[0]
    if guess in aliases:
        return aliases[guess]
    return guess


def is_builtin_help_token(token: str) -> bool:
    return token in HELP_ALIASES


def stdin_is_interactive() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    if callable(isatty):
        try:
            return bool(isatty())
        except Exception:
            return False
    return False


def render_argument_type(annotation: Any) -> str:
    describer = getattr(annotation, "describe", None)
    if callable(describer):
        return str(describer())
    if annotation in (inspect.Parameter.empty, Any):
        return "str"
    origin = get_origin(annotation)
    if origin is not None:
        args = ", ".join(render_argument_type(a) for a in get_args(annotation))
        return f"{origin.__name__}[{args}]"
    return getattr(annotation, "__name__", None) or str(annotation)


def argument_label(arg: Any) -> str:
    choices = getattr(arg.type, "choices", None)
    if choices:
        return f"{arg.name}: {'|'.join(str(choice) for choice in choices)}"
    return arg.name


def entry_aliases(entry: Any, aliases: Mapping[str, str]) -> tuple[str, ...]:
    result: list[str] = list(entry.options)
    option_normalized = {normalize_alias(option) for option in entry.options}
    for alias, target in sorted(aliases.items()):
        if target != entry.name or alias in option_normalized:
            continue
        result.append(alias)
    return tuple(dict.fromkeys(result))


def group_aliases(group_name: str, aliases: Mapping[str, str]) -> tuple[str, ...]:
    prefix = f"{group_name} "
    result: list[str] = []
    for alias, target in sorted(aliases.items()):
        if target.startswith(prefix) and " " in alias:
            alias_group = alias.rsplit(" ", 1)[0]
            if alias_group == group_name:
                continue
            if " " not in group_name:
                alias_group = alias_group.split(" ", 1)[0]
            result.append(alias_group)
    return tuple(dict.fromkeys(result))


def format_entry_label(
    entry: Any,
    aliases: Mapping[str, str],
    *,
    primary: str | None = None,
) -> str:
    names = [primary or entry.name]
    names.extend(entry_aliases(entry, aliases))
    return ", ".join(dict.fromkeys(names))


def format_group_label(
    group_name: str,
    aliases: Mapping[str, str],
    *,
    primary: str | None = None,
) -> str:
    names = [primary or group_name, *group_aliases(group_name, aliases)]
    return ", ".join(dict.fromkeys(names))


def command_signature(entry: Any, *, primary: str | None = None) -> str:
    parts = [primary or entry.name]
    for arg in entry.arguments:
        flag = f"--{arg.name.replace('_', '-')}"
        if render_argument_type(arg.type) == "bool":
            parts.append(f"[{flag}]")
        elif arg.required:
            parts.append(f"<{argument_label(arg)}>")
        else:
            parts.append(f"[{argument_label(arg)}]")
    return " ".join(parts)


def color_text(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{Color.RESET}" if enabled else text


def section_header(title: str, use_color: bool) -> str:
    return color_text(title, Color.BOLD, use_color)


def render_help_table(
    rows: list[tuple[str, str]],
    *,
    use_color: bool,
    indent: int = 2,
) -> str:
    if not rows:
        return ""
    pad = " " * indent
    col_width = max(len(key) for key, _ in rows)
    return "\n".join(
        f"{pad}{color_text(key, Color.CYAN, use_color)}{' ' * (col_width - len(key))}  {value}"
        for key, value in rows
    )


def group_command_rows(group_name: str, entries: list[Any]) -> list[tuple[str, str]]:
    rows = []
    command_prefix = f"{group_name} "
    for entry in entries:
        if not entry.name.startswith(command_prefix):
            continue
        relative = entry.name[len(command_prefix):]
        rows.append(
            (
                command_signature(entry, primary=relative),
                entry.help_text or entry.description or "No description provided.",
            )
        )
    return rows


def has_group(
    group_name: str,
    commands: Mapping[str, Any],
    aliases: Mapping[str, str],
) -> bool:
    prefix = normalize_alias(group_name)
    for name in commands:
        if name == prefix or name.startswith(f"{prefix} "):
            return True
    alias_target = aliases.get(prefix)
    if alias_target:
        return has_group(alias_target, commands, aliases)
    return False


def render_global_commands_table(
    commands: Mapping[str, Any],
    groups: Mapping[str, tuple[str, tuple[str, ...]]],
    aliases: Mapping[str, str],
    *,
    header: str,
    use_color: bool,
    tag: str | None = None,
) -> str:
    entries = list(commands.values())
    if tag:
        entries = [entry for entry in entries if tag in entry.tags]
    if not entries:
        return "\n".join(
            [
                section_header(header, use_color),
                color_text("  No commands are currently registered.", Color.DIM, use_color),
            ]
        )

    rows = [
        (
            entry.name,
            entry.help_text or entry.description or "No description provided.",
        )
        for entry in entries
        if " " not in entry.name
    ]

    lines = [section_header(header, use_color)]
    if rows:
        lines.append(render_help_table(rows, use_color=use_color))

    for group_name in groups:
        if " " in group_name:
            continue
        group_rows = group_command_rows(group_name, entries)
        if not group_rows:
            continue
        if len(lines) > 1:
            lines.append("")
        description = groups.get(group_name, ("", ()))[0] or f"{group_name} commands"
        label = color_text(format_group_label(group_name, aliases), Color.CYAN, use_color)
        lines.append(f"  {label}  {description}")
        lines.append(render_help_table(group_rows, use_color=use_color, indent=4))

    return "\n".join(lines)


def render_global_help(
    commands: Mapping[str, Any],
    groups: Mapping[str, tuple[str, tuple[str, ...]]],
    aliases: Mapping[str, str],
    *,
    program_name: str | None = None,
    shell_title: str = "Registers CLI",
    shell_description: str = "Type 'help' for shell help and 'exit' to quit.",
    shell_version: str | None = None,
    use_color: bool = False,
    tag: str | None = None,
) -> str:
    _ = program_name or "app.py"
    lines: list[str] = []
    lines += [
        color_text(shell_title, Color.BOLD_CYAN, use_color),
        color_text(shell_description, Color.DIM, use_color),
    ]
    if shell_version:
        lines.append(color_text(shell_version, Color.GREEN, use_color))
    lines += [
        "",
        section_header("Shell builtins", use_color),
        render_help_table(
            [
                ("help", "Show this menu"),
                ("help <command>", "Show detailed help for a specific command"),
                ("commands", "List all registered commands"),
                ("exec <command>", "Run a system command in the host shell"),
                ("exit / quit", "Leave interactive mode"),
            ],
            use_color=use_color,
        ),
        "",
        render_global_commands_table(
            commands,
            groups,
            aliases,
            header="Registered commands",
            use_color=use_color,
            tag=tag,
        ),
        "",
        color_text("Tip: run 'help <command>' for full argument details.", Color.DIM, use_color),
    ]
    return "\n".join(lines)


def render_command_help(
    entry: Any,
    aliases: Mapping[str, str],
    *,
    missing: Any,
    program_name: str | None = None,
    use_color: bool = False,
) -> str:
    from registers.cli.parser import render_command_usage

    prog = program_name or "app.py"
    summary = entry.help_text or entry.description or "No description provided."
    alias_text = ", ".join(entry_aliases(entry, aliases)) or "none"
    usage = render_command_usage(entry, program_name=prog)
    tags = ", ".join(entry.tags) if entry.tags else "none"

    lines: list[str] = [
        section_header(entry.name, use_color),
        color_text(f"  {summary}", Color.DIM, use_color),
        "",
        render_help_table(
            [("Usage", usage), ("Aliases", alias_text), ("Tags", tags)],
            use_color=use_color,
        ),
    ]
    if entry.deprecated:
        lines.append(color_text("  Deprecated command.", Color.DIM, use_color))

    if not entry.arguments:
        lines += [
            "",
            color_text("  This command takes no arguments.", Color.DIM, use_color),
        ]
        if entry.examples:
            lines += [
                "",
                section_header("Examples", use_color),
                "\n".join(f"  {example}" for example in entry.examples),
            ]
        return "\n".join(lines)

    argument_rows: list[tuple[str, str]] = []
    for arg in entry.arguments:
        type_name = render_argument_type(arg.type)
        qualifier = "required" if arg.required else "optional"
        default_suffix = f", default={arg.default!r}" if arg.default is not missing else ""
        signature = f"{arg.name}  ({type_name}, {qualifier}{default_suffix})"
        details = arg.help_text or "-"
        argument_rows.append((signature, details))

    lines += [
        "",
        section_header("Arguments", use_color),
        render_help_table(argument_rows, use_color=use_color),
    ]
    if entry.examples:
        lines += [
            "",
            section_header("Examples", use_color),
            "\n".join(f"  {example}" for example in entry.examples),
        ]
    return "\n".join(lines)


def render_builtin_help_detail(
    target: str,
    *,
    program_name: str | None = None,
    use_color: bool = False,
) -> str:
    prog = program_name or "app.py"

    if target == HELP_COMMAND_NAME:
        name = "help"
        description = "Show the global help menu or detailed help for one command."
        usage_lines = [f"{prog} help", f"{prog} help <command>", f"{prog} --help", f"{prog} -h"]
    else:
        name = "interactive"
        description = "Start interactive REPL mode."
        usage_lines = [f"{prog} --interactive", f"{prog} -i"]

    title = f"Built-in Command: {name}"
    lines = [
        section_header(title, use_color),
        color_text("=" * len(title), Color.DIM, use_color),
        "",
        description,
        "",
        section_header("Usage", use_color),
    ]
    lines += [f"  {line}" for line in usage_lines]
    return "\n".join(lines)


def render_group_help(
    group_name: str,
    commands: Mapping[str, Any],
    aliases: Mapping[str, str],
    *,
    program_name: str | None = None,
    use_color: bool = False,
) -> str:
    _ = program_name or "app.py"
    prefix = normalize_alias(group_name)
    if prefix in aliases:
        prefix = aliases[prefix]
    rows = group_command_rows(prefix, list(commands.values()))
    lines = [section_header(f"Command group: {format_group_label(prefix, aliases)}", use_color)]
    if rows:
        lines.append(render_help_table(rows, use_color=use_color))
    lines.extend(["", color_text(f"Tip: run 'help {prefix} <command>' for details.", Color.DIM, use_color)])
    return "\n".join(lines)


def enable_windows_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        if not handle:
            return False
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return kernel32.SetConsoleMode(handle, mode.value | 0x0004) != 0
    except Exception:
        return False


def supports_color(colors: bool | None) -> bool:
    if colors is not None:
        return colors
    if os.getenv("NO_COLOR"):
        return False
    stream = getattr(sys, "stdout", None)
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        tty = bool(isatty())
    except Exception:
        return False
    if not tty:
        return False
    term = os.getenv("TERM", "").lower()
    return term != "dumb" and enable_windows_ansi()
