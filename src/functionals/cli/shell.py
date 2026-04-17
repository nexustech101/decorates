"""
Interactive REPL shell for ``functionals.cli`` command registries.
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

from functionals.cli.exceptions import CommandExecutionError, FrameworkError, UnknownCommandError
from functionals.cli.parser import ParseError, parse_command_args, render_command_usage
from functionals.cli.registry import HELP_ALIASES, MISSING

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
# ANSI palette — module-level so nothing leaks into the class namespace
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
    bar = "─" * width
    return f"┌{bar}┐\n│    {text.upper():<{width - 4}}│\n└{bar}┘"


# ---------------------------------------------------------------------------
# Argument type rendering
# ---------------------------------------------------------------------------

def _render_arg_type(annotation: Any) -> str:
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
        prompt: str = "› ",
        program_name: str | None = None,
        input_fn: Callable[[str], str] | None = None,
        banner: bool = True,
        title: str = "Decorates CLI",
        description: str = "Type 'help' for shell help and 'exit' to quit.",
        colors: bool | None = None,
        usage: bool = False,
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
        self._description  = description
        self._colors       = _supports_color() if colors is None else colors
        self._usage        = usage

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        if self._banner:
            print(self._c(_render_banner(self._title), _C.BOLD_CYAN))

        print(self._c(self._title,       _C.BOLD_CYAN))
        print(self._c(self._description, _C.DIM))
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
            if len(tokens) > 2:
                self._error("'help' accepts at most one command name.")
            elif len(tokens) == 2:
                self._print_command_help(tokens[1])
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

            self._print_process_output(result)
            if result.returncode != 0:
                self._error(f"'exec' command exited with status {result.returncode}.")
            return

        if _is_windows():
            self._error("Unable to run 'exec': neither PowerShell nor cmd is available.")
        else:
            self._error("Unable to run 'exec': bash is not available.")
        if last_missing is not None:
            logger.debug("Last exec launcher error: %s", last_missing)

    @staticmethod
    def _print_process_output(result: Any) -> None:
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""

        if stdout:
            end = "" if stdout.endswith("\n") else "\n"
            print(stdout, end=end)
        if stderr:
            end = "" if stderr.endswith("\n") else "\n"
            print(stderr, end=end, file=sys.stderr)

    def _print_command_help(self, target: str) -> None:
        try:
            entry = self._registry.get(target)
            print(self._render_command_help(entry))
            return
        except UnknownCommandError:
            pass

        # Fall back to registry for built-ins (help, --interactive, etc.)
        try:
            self._registry.print_help(target, program_name=self._program_name)
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
        command_token = tokens[0]

        try:
            entry = self._registry.get(command_token)
        except UnknownCommandError:
            suggestion = self._registry.suggest(command_token)
            if suggestion:
                self._hint(f"Unknown command '{command_token}'. Did you mean '{suggestion}'?")
            else:
                self._error(f"Unknown command '{command_token}'.")
            return

        try:
            kwargs = parse_command_args(entry, tokens[1:])
        except ParseError as exc:
            self._error(str(exc))
            print(self._c(render_command_usage(entry, program_name=self._program_name), _C.DIM))
            return

        try:
            result = entry.handler(**kwargs)
        except FrameworkError as exc:
            self._error(str(exc))
            return
        except Exception as exc:
            logger.exception("Unhandled command failure in shell for '%s'.", entry.name)
            self._error(str(CommandExecutionError(entry.name, str(exc))))
            return

        if self._print_result and result is not None:
            print(self._c(str(result), _C.GREEN))

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
            (entry.name, entry.help_text or entry.description or "—")
            for entry in entries
        ]
        return "\n".join([
            self._section_header(header),
            self._render_table(rows),
        ])

    def _render_command_help(self, entry: Any) -> str:
        summary = entry.help_text or entry.description or "—"
        aliases = ", ".join(entry.options) if entry.options else "none"
        usage   = render_command_usage(entry, program_name=self._program_name)

        lines = [
            self._section_header(entry.name),
            self._c(f"  {summary}", _C.DIM),
            "",
            self._render_table([("Usage", usage), ("Aliases", aliases)]),
        ]

        if not entry.arguments:
            lines += ["", self._c("  This command takes no arguments.", _C.DIM)]
            return "\n".join(lines)

        arg_rows = []
        for arg in entry.arguments:
            type_name = _render_arg_type(arg.type)
            qualifier = "required" if arg.required else "optional"
            default   = f", default={arg.default!r}" if arg.default is not MISSING else ""
            key       = f"{arg.name}  {self._c(f'({type_name}, {qualifier}{default})', _C.DIM)}"
            value     = arg.help_text or "—"
            arg_rows.append((key, value))

        lines += ["", self._section_header("Arguments"), self._render_table(arg_rows)]
        return "\n".join(lines)

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

    # ------------------------------------------------------------------
    # Feedback primitives
    # ------------------------------------------------------------------

    def _error(self, message: str) -> None:
        prefix = self._c("error", _C.BOLD_RED)
        print(f"{prefix}  {message}")

    def _hint(self, message: str) -> None:
        print(self._c(f"  → {message}", _C.YELLOW))

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def _c(self, text: str, code: str) -> str:
        """Apply an ANSI code when colors are enabled; no-op otherwise."""
        return f"{code}{text}{_C.RESET}" if self._colors else text
