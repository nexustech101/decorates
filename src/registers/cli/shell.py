"""
Interactive REPL shell for ``registers.cli`` command registries.
"""

from __future__ import annotations

from collections.abc import Callable
import enum
import inspect
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, get_args, get_origin

from registers.cli.exceptions import CommandExecutionError, RegistrationError, UnknownCommandError
from registers.cli.parser import ParseError, parse_command_args, render_command_usage
from registers.cli.registry import HELP_ALIASES, MISSING

logger = logging.getLogger(__name__)

try:
    import readline as _readline  # noqa: F401
except Exception:  # pragma: no cover - platform-dependent
    _READLINE_AVAILABLE = False
else:
    _READLINE_AVAILABLE = True


# Mark non-printing spans for GNU readline prompt-length accounting.
_READLINE_NONPRINT_START = "\001"
_READLINE_NONPRINT_END = "\002"

# ANSI SGR escapes for coloring and generic CSI escapes (e.g. arrow keys).
_ANSI_ESCAPE = re.compile(r"\033\[[0-9;]*m")
_CSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ---------------------------------------------------------------------------
# Terminal color support
# ---------------------------------------------------------------------------

def _enable_windows_ansi() -> bool:
    """Best-effort enable ANSI escape sequences on Windows terminals."""
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


def _supports_color() -> bool:
    """Return True when the current stdout is likely to render ANSI colors."""
    if os.getenv("NO_COLOR"):
        return False
    stream = getattr(sys, "stdout", None)
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        tty = isatty()
    except Exception:
        return False
    return tty and os.getenv("TERM", "").lower() != "dumb" and _enable_windows_ansi()


def _is_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# ANSI palette - module-level so nothing leaks into the class namespace
# ---------------------------------------------------------------------------

class _C:
    """Minimal ANSI escape wrappers."""
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    CYAN   = "\033[36m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"

    # Composed shortcuts used repeatedly
    BOLD_CYAN  = "\033[1;36m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_RED   = "\033[1;31m"


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _render_banner(text: str) -> str:
    try:
        from pyfiglet import Figlet  # type: ignore[import-not-found]
        rendered = Figlet(font="slant").renderText(text).rstrip()
        if rendered:
            return rendered
    except Exception:
        pass

    width = max(len(text) + 8, 36)
    bar = "-" * width
    return f"+{bar}+\n|    {text.upper():<{width - 4}}|\n+{bar}+"


# ---------------------------------------------------------------------------
# Argument type rendering
# ---------------------------------------------------------------------------

def _render_arg_type(annotation: Any) -> str:
    describer = getattr(annotation, "describe", None)
    if callable(describer):
        return str(describer())
    if annotation in (inspect.Parameter.empty, Any):
        return "str"
    origin = get_origin(annotation)
    if origin is not None:
        args = ", ".join(_render_arg_type(a) for a in get_args(annotation))
        return f"{origin.__name__}[{args}]"
    return getattr(annotation, "__name__", None) or str(annotation)


def _wrap_ansi_for_readline(prompt: str) -> str:
    """Wrap ANSI escapes so GNU readline treats them as zero-width."""
    return _ANSI_ESCAPE.sub(
        lambda match: f"{_READLINE_NONPRINT_START}{match.group(0)}{_READLINE_NONPRINT_END}",
        prompt,
    )


def _strip_terminal_escapes(text: str) -> str:
    """Remove terminal CSI sequences from raw input fallback buffers."""
    return _CSI_ESCAPE.sub("", text)


# ---------------------------------------------------------------------------
# Builtin dispatch sentinel
# ---------------------------------------------------------------------------

class _BuiltinAction(enum.Enum):
    NOT_BUILTIN = enum.auto()   # caller should dispatch to registry
    CONTINUE    = enum.auto()   # handled; keep looping
    EXIT        = enum.auto()   # handled; terminate the loop


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

class InteractiveShell:
    """Run an interactive command loop against a :class:`CommandRegistry`."""

    def __init__(
        self,
        registry: Any,
        *,
        print_result: bool = True,
        prompt: str = "> ",
        program_name: str | None = None,
        input_fn: Callable[[str], str] | None = None,
        banner: bool = True,
        title: str = "Decorates CLI",
        banner_text: str | None = None,
        description: str = "Type 'help' for shell help and 'exit' to quit.",
        version_text: str | None = None,
        colors: bool | None = None,
        usage: bool = False,
        output: str | None = None,
        quiet: bool = False,
        no_color: bool = False,
        completion: bool = False,
        history: bool = False,
        multiline: bool = False,
        log_level: str | int | None = None,
        context: Any | None = None,
    ) -> None:
        self._registry     = registry
        self._print_result = print_result
        self._prompt       = prompt
        self._program_name = program_name or Path(sys.argv[0]).name or "app.py"
        self._input_fn     = input_fn or input
        self._using_builtin_input = input_fn is None
        self._readline_enabled = self._using_builtin_input and _READLINE_AVAILABLE
        self._banner       = banner
        self._title        = title
        self._banner_text  = banner_text
        self._description  = description
        self._version_text = version_text
        self._colors       = False if no_color else (_supports_color() if colors is None else colors)
        self._usage        = usage
        self._output       = output
        self._quiet        = quiet
        self._completion   = completion
        self._history      = history
        self._multiline    = multiline
        self._log_level    = log_level
        self._context      = context
        self._prompt_session = self._build_prompt_session() if self._using_builtin_input else None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        if self._banner:
            banner_value = self._banner_text if self._banner_text is not None else self._title
            print(self._c(_render_banner(banner_value), _C.BOLD_CYAN))

        print(self._c(self._title,       _C.BOLD_CYAN))
        print(self._c(self._description, _C.DIM))
        if self._version_text:
            print(self._c(self._version_text, _C.GREEN))
        print()
        if self._usage:
            print(self._render_full_help())
            print()

        while True:
            raw = self._read_line()
            if raw is None:
                break

            line = raw.strip()
            if not line:
                continue

            action = self._handle_shell_builtin_raw(line)
            if action is _BuiltinAction.EXIT:
                break
            if action is _BuiltinAction.CONTINUE:
                continue

            tokens = self._tokenize(line)
            if tokens is None:
                continue

            action = self._handle_shell_builtin(tokens)
            if action is _BuiltinAction.EXIT:
                break
            if action is _BuiltinAction.CONTINUE:
                continue

            self._dispatch(tokens)

        print()
        print(self._c("Goodbye.", _C.DIM))

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _read_line(self) -> str | None:
        prompt = self._c(self._prompt, _C.BOLD_GREEN)
        if self._readline_enabled and self._colors:
            prompt = _wrap_ansi_for_readline(prompt)

        try:
            if self._prompt_session is not None:
                return self._prompt_session.prompt(prompt)
            line = self._input_fn(prompt)
            if self._using_builtin_input and not self._readline_enabled and "\x1b[" in line:
                return _strip_terminal_escapes(line)
            return line
        except EOFError:
            print()
            return None
        except KeyboardInterrupt:
            print()
            return ""

    def _build_prompt_session(self) -> Any | None:
        if not (self._completion or self._history or self._multiline):
            return None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import InMemoryHistory
        except Exception:
            return None
        words = list(self._registry.all()) + ["help", "commands", "exec", "exit", "quit"]
        completer = WordCompleter(words, ignore_case=True) if self._completion else None
        history = InMemoryHistory() if self._history else None
        return PromptSession(completer=completer, history=history, multiline=self._multiline)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(line: str) -> list[str] | None:
        try:
            return shlex.split(line)
        except ValueError as exc:
            print(f"Error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Builtin commands
    # ------------------------------------------------------------------

    def _handle_shell_builtin_raw(self, line: str) -> _BuiltinAction:
        if line == "exec":
            self._error("'exec' requires a command to run.")
            return _BuiltinAction.CONTINUE

        if line.startswith("exec "):
            command = line[len("exec"):].lstrip()
            if not command:
                self._error("'exec' requires a command to run.")
                return _BuiltinAction.CONTINUE
            self._run_exec(command)
            return _BuiltinAction.CONTINUE

        return _BuiltinAction.NOT_BUILTIN

    def _handle_shell_builtin(self, tokens: list[str]) -> _BuiltinAction:
        token = tokens[0]

        if token in {"exit", "quit"}:
            if len(tokens) > 1:
                self._error(f"'{token}' takes no arguments.")
                return _BuiltinAction.CONTINUE
            return _BuiltinAction.EXIT

        if token == "commands":
            if len(tokens) > 1:
                self._error("'commands' takes no arguments.")
            else:
                print(self._render_commands_table())
            return _BuiltinAction.CONTINUE

        if token in HELP_ALIASES:
            if len(tokens) > 1:
                self._print_command_help(" ".join(tokens[1:]))
            else:
                print(self._render_full_help())
            return _BuiltinAction.CONTINUE

        return _BuiltinAction.NOT_BUILTIN

    def _run_exec(self, command: str) -> None:
        launchers: list[tuple[str, list[str]]]
        if _is_windows():
            launchers = [
                ("PowerShell", ["powershell", "-NoLogo", "-NoProfile", "-Command", command]),
                ("cmd", ["cmd", "/c", command]),
            ]
        else:
            launchers = [("bash", ["bash", "-lc", command])]

        last_missing: FileNotFoundError | None = None

        for shell_name, argv in launchers:
            try:
                result = subprocess.run(argv, capture_output=True, text=True)
            except FileNotFoundError as exc:
                last_missing = exc
                logger.debug("Shell '%s' is unavailable for exec builtin.", shell_name, exc_info=True)
                continue

            self._print_exec_output(shell_name=shell_name, command=command, result=result)
            if result.returncode != 0:
                self._error(f"'exec' command exited with status {result.returncode}.")
            return

        if _is_windows():
            self._error("Unable to run 'exec': neither PowerShell nor cmd is available.")
        else:
            self._error("Unable to run 'exec': bash is not available.")
        if last_missing is not None:
            logger.debug("Last exec launcher error: %s", last_missing)

    def _print_exec_output(self, *, shell_name: str, command: str, result: Any) -> None:
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        returncode = int(getattr(result, "returncode", 0))

        print(self._c("Exec Result", _C.BOLD))
        print(f"  {self._c('Shell:', _C.CYAN)} {self._c(shell_name, _C.GREEN)}")
        print(f"  {self._c('Command:', _C.CYAN)} {self._c(command, _C.DIM)}")

        exit_color = _C.BOLD_GREEN if returncode == 0 else _C.BOLD_RED
        print(f"  {self._c('Exit code:', _C.CYAN)} {self._c(str(returncode), exit_color)}")

        if stdout:
            print(f"  {self._c('Stdout:', _C.CYAN)}")
            for line in stdout.rstrip("\n").splitlines():
                print(f"    {self._c(line, _C.GREEN)}")
        if stderr:
            print(f"  {self._c('Stderr:', _C.CYAN)}")
            for line in stderr.rstrip("\n").splitlines():
                print(f"    {self._c(line, _C.RED)}")

    def _print_command_help(self, target: str) -> None:
        try:
            entry = self._registry.get(target)
            print(self._render_command_help(entry))
            return
        except UnknownCommandError:
            pass

        has_group = getattr(self._registry, "_has_group", None)
        if callable(has_group) and has_group(target):
            print(self._render_group_help(target))
            return

        # Fall back to registry for built-ins (help, --interactive, etc.)
        try:
            self._registry.print_help(
                target,
                program_name=self._program_name,
                colors=self._colors,
            )
        except UnknownCommandError:
            suggestion = self._registry.suggest(target)
            if suggestion:
                self._hint(f"Unknown command '{target}'. Did you mean '{suggestion}'?")
            else:
                self._error(f"Unknown command '{target}'.")

    # ------------------------------------------------------------------
    # Registry dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, tokens: list[str]) -> None:
        self._execute_tokens(tokens, print_result=True)

    def _execute_tokens(self, tokens: list[str], *, print_result: bool) -> Any:
        command_token = tokens[0]

        try:
            entry, _command_tokens, command_args = self._registry._resolve_command_tokens(tokens)
        except UnknownCommandError:
            has_group = getattr(self._registry, "_has_group", None)
            if callable(has_group) and has_group(command_token):
                print(self._render_group_help(command_token))
                return None
            suggestion = self._registry.suggest(command_token)
            if suggestion:
                self._hint(f"Unknown command '{command_token}'. Did you mean '{suggestion}'?")
            else:
                self._error(f"Unknown command '{command_token}'.")
            return None

        try:
            command_args, runtime_options = self._registry._strip_runtime_options(entry, command_args)
            output = runtime_options.get("output", self._output or entry.default_output)
            quiet = bool(runtime_options.get("quiet", self._quiet))
            kwargs = parse_command_args(entry, command_args, allow_missing_prompts=True)
            kwargs = self._registry._prompt_missing(entry, kwargs, input_fn=self._input_fn)
        except ParseError as exc:
            self._error(str(exc))
            print(self._c(render_command_usage(entry, program_name=self._program_name), _C.DIM))
            return None

        try:
            result = self._registry._execute_entry(
                entry,
                kwargs,
                context=self._context,
                force=bool(runtime_options.get("force", False)),
                input_fn=self._input_fn,
                log_level=self._log_level,
                event_loop=None,
            )
        except RegistrationError as exc:
            self._error(str(exc))
            return None
        except Exception as exc:
            logger.exception("Unhandled command failure in shell for '%s'.", entry.name)
            self._error(str(CommandExecutionError(entry.name, str(exc))))
            return None

        if print_result and self._print_result and result is not None and not quiet:
            self._print_command_result(entry.name, result, output=output)
        return result

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _render_full_help(self) -> str:
        shell_rows = [
            ("help",           "Show this menu"),
            ("help <command>", "Show detailed help for a specific command"),
            ("commands",       "List all registered commands"),
            ("exec <command>", "Run a system command in the host shell"),
            ("exit / quit",    "Leave interactive mode"),
        ]
        return "\n".join([
            self._section_header("Shell builtins"),
            self._render_table(shell_rows),
            "",
            self._render_commands_table(header="Registered commands"),
            "",
            self._c("Tip: run 'help <command>' for full argument details.", _C.DIM),
        ])

    def _render_commands_table(self, *, header: str = "Available commands") -> str:
        entries = list(self._registry.all().values())
        if not entries:
            return "\n".join([
                self._section_header(header),
                self._c("  No commands are currently registered.", _C.DIM),
            ])

        rows = [
            (entry.name, entry.help_text or entry.description or "-")
            for entry in entries
            if " " not in entry.name
        ]

        lines = [self._section_header(header)]
        if rows:
            lines.append(self._render_table(rows))

        groups = getattr(self._registry, "_groups", {})
        for group_name in groups:
            if " " in group_name:
                continue
            group_rows = self._group_command_rows(group_name, entries)
            if not group_rows:
                continue
            if len(lines) > 1:
                lines.append("")
            description = groups.get(group_name, ("", ()))[0] or f"{group_name} commands"
            lines.append(f"  {self._c(self._format_group_label(group_name), _C.CYAN)}  {description}")
            lines.append(self._render_table(group_rows, indent=4))

        return "\n".join(lines)

    def _render_command_help(self, entry: Any) -> str:
        summary = entry.help_text or entry.description or "-"
        aliases = ", ".join(self._entry_aliases(entry)) or "none"
        usage   = render_command_usage(entry, program_name=self._program_name)

        lines = [
            self._section_header(entry.name),
            self._c(f"  {summary}", _C.DIM),
            "",
            self._render_table([("Usage", usage), ("Aliases", aliases)]),
        ]

        if not entry.arguments:
            lines += ["", self._c("  This command takes no arguments.", _C.DIM)]
            lines += self._render_examples(entry)
            return "\n".join(lines)

        arg_rows = []
        for arg in entry.arguments:
            type_name = _render_arg_type(arg.type)
            qualifier = "required" if arg.required else "optional"
            default   = f", default={arg.default!r}" if arg.default is not MISSING else ""
            key       = f"{arg.name}  {self._c(f'({type_name}, {qualifier}{default})', _C.DIM)}"
            value     = arg.help_text or "-"
            arg_rows.append((key, value))

        lines += ["", self._section_header("Arguments"), self._render_table(arg_rows)]
        lines += self._render_examples(entry)
        return "\n".join(lines)

    def _render_examples(self, entry: Any) -> list[str]:
        examples = tuple(getattr(entry, "examples", ()) or ())
        if not examples:
            return []
        return [
            "",
            self._section_header("Examples"),
            "\n".join(f"  {example}" for example in examples),
        ]

    def _render_group_help(self, target: str) -> str:
        prefix = self._registry._normalize_alias(target)
        aliases = getattr(self._registry, "_aliases", {})
        if prefix in aliases:
            prefix = aliases[prefix]

        lines = [self._section_header(f"Command group: {self._format_group_label(prefix)}")]
        tree_text = self._render_group_plain(prefix)
        if tree_text:
            lines.append(tree_text)
        return "\n".join(lines)

    def _render_group_plain(self, prefix: str) -> str:
        rows = self._group_command_rows(prefix, list(self._registry.all().values()))
        return self._render_table(rows) if rows else ""

    def _group_command_rows(self, group_name: str, entries: list[Any]) -> list[tuple[str, str]]:
        rows = []
        command_prefix = f"{group_name} "
        for entry in entries:
            if not entry.name.startswith(command_prefix):
                continue
            relative = entry.name[len(command_prefix):]
            rows.append((self._command_signature(entry, primary=relative), entry.help_text or entry.description or "-"))
        return rows

    def _command_signature(self, entry: Any, *, primary: str | None = None) -> str:
        parts = [primary or entry.name]
        for arg in entry.arguments:
            flag = f"--{arg.name.replace('_', '-')}"
            if _render_arg_type(arg.type) == "bool":
                parts.append(f"[{flag}]")
            elif arg.required:
                parts.append(f"<{self._argument_label(arg)}>")
            else:
                parts.append(f"[{self._argument_label(arg)}]")
        return " ".join(parts)

    @staticmethod
    def _argument_label(arg: Any) -> str:
        choices = getattr(arg.type, "choices", None)
        if choices:
            return f"{arg.name}: {'|'.join(str(choice) for choice in choices)}"
        return arg.name

    def _entry_aliases(self, entry: Any) -> tuple[str, ...]:
        aliases: list[str] = list(getattr(entry, "options", ()))
        option_normalized = {self._registry._normalize_alias(option) for option in aliases}
        command_tokens = entry.name.split()
        candidates: list[str] = []
        for alias, target in sorted(getattr(self._registry, "_aliases", {}).items()):
            if target != entry.name or alias in option_normalized:
                continue
            if " " not in alias:
                aliases.append(alias)
                continue
            alias_tokens = alias.split()
            if len(alias_tokens) == len(command_tokens):
                candidates.append(alias)
        if candidates:
            max_changes = max(
                sum(1 for left, right in zip(candidate.split(), command_tokens) if left != right)
                for candidate in candidates
            )
            aliases.extend(
                candidate
                for candidate in candidates
                if sum(1 for left, right in zip(candidate.split(), command_tokens) if left != right) == max_changes
            )
        return tuple(dict.fromkeys(aliases))

    def _format_entry_label(self, entry: Any, *, primary: str | None = None) -> str:
        names = [primary or entry.name, *self._entry_aliases(entry)]
        return ", ".join(dict.fromkeys(names))

    def _group_aliases(self, group_name: str) -> tuple[str, ...]:
        prefix = f"{group_name} "
        aliases: list[str] = []
        for alias, target in sorted(getattr(self._registry, "_aliases", {}).items()):
            if target.startswith(prefix) and " " in alias:
                alias_group = alias.rsplit(" ", 1)[0]
                if alias_group == group_name:
                    continue
                if " " not in group_name:
                    alias_group = alias_group.split(" ", 1)[0]
                aliases.append(alias_group)
        candidates = tuple(dict.fromkeys(aliases))
        if not candidates:
            return ()
        group_tokens = group_name.split()
        matching = tuple(candidate for candidate in candidates if len(candidate.split()) == len(group_tokens))
        if not matching:
            return candidates
        max_changes = max(
            sum(1 for left, right in zip(candidate.split(), group_tokens) if left != right)
            for candidate in matching
        )
        return tuple(
            candidate
            for candidate in matching
            if sum(1 for left, right in zip(candidate.split(), group_tokens) if left != right) == max_changes
        )

    def _format_group_label(self, group_name: str, *, primary: str | None = None) -> str:
        names = [primary or group_name, *self._group_aliases(group_name)]
        return ", ".join(dict.fromkeys(names))

    def _render_table(self, rows: list[tuple[str, str]], *, indent: int = 2) -> str:
        if not rows:
            return ""
        pad = " " * indent
        # Strip ANSI codes for width measurement so alignment is based on visible chars
        ansi_len = lambda s: len(_ANSI_ESCAPE.sub("", s))  # noqa: E731
        col_width = max(ansi_len(k) for k, _ in rows)
        return "\n".join(
            f"{pad}{self._c(key, _C.CYAN)}{' ' * (col_width - ansi_len(key))}  {value}"
            for key, value in rows
        )

    def _section_header(self, title: str) -> str:
        return self._c(title, _C.BOLD)

    def _print_command_result(self, command_name: str, result: Any, *, output: str | None = None) -> None:
        if output:
            from registers.cli.ux import print_result as render_print_result
            render_print_result(result, output=output)
            return
        text = str(result)
        if command_name in {"run", "install", "update", "pull", "cron"} and text.startswith("FX "):
            self._print_structured_result(text)
            return
        print(self._c(text, _C.GREEN))

    def _print_structured_result(self, text: str) -> None:
        lines = text.splitlines()
        if not lines:
            return

        first = lines[0].strip()
        if first:
            print(self._c(first, _C.BOLD_GREEN))

        for raw in lines[1:]:
            line = raw.strip()
            if not line:
                continue
            if ":" not in line:
                print(self._c(f"  {line}", _C.GREEN))
                continue

            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            value_color = self._result_value_color(key, value)
            print(
                f"  {self._c(f'{key}:', _C.CYAN)} {self._c(value, value_color)}"
            )

    @staticmethod
    def _result_value_color(key: str, value: str) -> str:
        key_lower = key.lower()
        value_lower = value.lower()

        if key_lower == "status":
            if value_lower in {"success", "ok", "passed"}:
                return _C.BOLD_GREEN
            if value_lower in {"failure", "failed", "error"}:
                return _C.BOLD_RED
            return _C.YELLOW

        if key_lower == "exit code":
            return _C.BOLD_GREEN if value.strip() == "0" else _C.BOLD_RED

        if key_lower in {"command"}:
            return _C.DIM

        if key_lower in {"skipped"}:
            return _C.YELLOW

        if key_lower in {"stderr", "errors"}:
            return _C.RED

        return _C.GREEN

    # ------------------------------------------------------------------
    # Feedback primitives
    # ------------------------------------------------------------------

    def _error(self, message: str) -> None:
        prefix = self._c("error", _C.BOLD_RED)
        print(f"{prefix}  {message}")

    def _hint(self, message: str) -> None:
        print(self._c(f"  -> {message}", _C.YELLOW))

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def _c(self, text: str, code: str) -> str:
        """Apply an ANSI code when colors are enabled; no-op otherwise."""
        return f"{code}{text}{_C.RESET}" if self._colors else text
