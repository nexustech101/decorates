import os
import pytest

import functionals.cli as cli
from functionals.cli.shell import _strip_terminal_escapes, _wrap_ansi_for_readline


@pytest.fixture(autouse=True)
def _reset_registry():
    cli.reset_registry()
    yield
    cli.reset_registry()


class _TTYStdin:
    def isatty(self) -> bool:
        return True


class _PipeLikeStdin:
    def isatty(self) -> bool:
        return False


def _input_from_lines(lines: list[str]):
    iterator = iter(lines)
    def _read(_prompt: str) -> str:
        return next(iterator)
    return _read


def _register_interactive_commands() -> None:
    @cli.register(description="Add item")
    @cli.option("--add")
    @cli.argument("title", type=str)
    def add(title: str) -> str:
        return f"added:{title}"

    @cli.register(description="Run")
    @cli.option("--run")
    @cli.argument("verbose", type=bool)
    def run_cmd(verbose: bool = False) -> str:
        return f"verbose={verbose}"


def test_empty_argv_enters_shell_when_tty(monkeypatch):
    monkeypatch.setattr("functionals.cli.registry.sys.stdin", _TTYStdin())

    called: dict[str, object] = {}

    def _fake_run_shell(**kwargs):
        called.update(kwargs)
        return "shell-entered"

    monkeypatch.setattr(cli.get_registry(), "run_shell", _fake_run_shell)

    result = cli.run([], print_result=False)

    assert result == "shell-entered"
    assert called["print_result"] is False


def test_empty_argv_shows_help_when_not_tty(monkeypatch, capsys):
    monkeypatch.setattr("functionals.cli.registry.sys.stdin", _PipeLikeStdin())

    @cli.register(description="Noop")
    @cli.option("--noop")
    def noop() -> None:
        return None

    assert cli.run([], print_result=False) is None
    out = capsys.readouterr().out
    assert "Decorates CLI Help" in out
    assert "noop" in out


def test_interactive_flag_enters_shell(monkeypatch):
    called = {"count": 0}

    def _fake_run_shell(**_kwargs):
        called["count"] += 1
        return None

    monkeypatch.setattr(cli.get_registry(), "run_shell", _fake_run_shell)

    assert cli.run(["--interactive"], print_result=False) is None
    assert cli.run(["-i"], print_result=False) is None
    assert called["count"] == 2


def test_interactive_mode_dispatches_registered_commands(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(
            [
                "add Alpha",
                "--add Beta",
                "add --title Gamma",
                "run",
                "run --verbose",
                "exit",
            ]
        )
    )

    out = capsys.readouterr().out
    assert "added:Alpha" in out
    assert "added:Beta" in out
    assert "added:Gamma" in out
    assert "verbose=False" in out
    assert "verbose=True" in out


def test_interactive_mode_keeps_running_after_parse_and_unknown_errors(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(
            [
                "add",
                "ad",
                "add Working",
                "exit",
            ]
        )
    )

    out = capsys.readouterr().out
    assert "Missing required argument 'title'" in out
    assert "Did you mean 'add'" in out
    assert "added:Working" in out


def test_interactive_mode_supports_shell_local_commands(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(
            [
                "commands",
                "help",
                "help add",
                "quit",
            ]
        ),
        print_result=False,
    )

    out = capsys.readouterr().out
    assert "Shell builtins" in out
    assert "Registered commands" in out
    assert "Decorates CLI Help" not in out
    assert "add" in out
    assert "Usage    usage:" in out
    assert "Aliases  --add" in out
    assert "Arguments" in out
    assert "title  (str, required)" in out


def test_interactive_mode_can_print_help_menu_on_startup(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(["quit"]),
        print_result=False,
        banner=False,
        colors=False,
        shell_usage=True,
    )

    out = capsys.readouterr().out
    assert "Shell builtins" in out
    assert "Registered commands" in out
    assert "help <command>" in out


def test_interactive_mode_renders_banner_by_default(monkeypatch, capsys):
    _register_interactive_commands()

    monkeypatch.setattr("functionals.cli.shell._render_banner", lambda text: f"FIGLET::{text}")

    cli.run_shell(
        input_fn=_input_from_lines(["exit"]),
        print_result=False,
    )

    out = capsys.readouterr().out
    assert "FIGLET::Decorates CLI" in out
    assert "Decorates CLI" in out


def test_interactive_mode_can_disable_banner(monkeypatch, capsys):
    _register_interactive_commands()
    monkeypatch.setattr("functionals.cli.shell._render_banner", lambda text: f"FIGLET::{text}")

    cli.run_shell(
        input_fn=_input_from_lines(["quit"]),
        print_result=False,
        banner=False,
    )

    out = capsys.readouterr().out
    assert "FIGLET::Decorates CLI" not in out
    assert "Decorates CLI" in out
    assert "Type 'help' for shell help and 'exit' to quit." in out


def test_interactive_mode_uses_simple_prompt_by_default():
    _register_interactive_commands()

    prompts: list[str] = []

    def _read(prompt: str) -> str:
        prompts.append(prompt)
        return "exit"

    cli.run_shell(
        input_fn=_read,
        print_result=False,
        banner=False,
        colors=False,
    )

    assert prompts
    assert prompts[0] == "> "


def test_interactive_mode_can_emit_color_when_enabled(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(["quit"]),
        print_result=False,
        banner=False,
        colors=True,
    )

    out = capsys.readouterr().out
    assert "\x1b[" in out


def test_interactive_mode_title_and_description_are_configurable(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(["quit"]),
        print_result=False,
        banner=False,
        colors=False,
        shell_title="Todo Console",
        shell_description="Manage tasks and users.",
    )

    out = capsys.readouterr().out
    assert "Todo Console" in out
    assert "Manage tasks and users." in out


def test_interactive_mode_supports_legacy_banner_text_alias(monkeypatch, capsys):
    _register_interactive_commands()
    monkeypatch.setattr("functionals.cli.shell._render_banner", lambda text: f"FIGLET::{text}")

    cli.run_shell(
        input_fn=_input_from_lines(["exit"]),
        print_result=False,
        banner_text="Legacy Title",
    )

    out = capsys.readouterr().out
    assert "FIGLET::Legacy Title" in out
    assert "Legacy Title" in out


def test_run_supports_cli_args_even_when_shell_options_are_provided():
    _register_interactive_commands()

    def _should_not_be_called(_prompt: str) -> str:
        raise AssertionError("shell_input_fn should not be used when argv has a command")

    result = cli.run(
        ["add", "ViaArgs"],
        print_result=False,
        shell_input_fn=_should_not_be_called,
        shell_title="Custom Shell Title",
        shell_description="Custom description",
    )

    assert result == "added:ViaArgs"


def test_run_supports_interactive_shell_with_custom_branding(monkeypatch, capsys):
    _register_interactive_commands()
    monkeypatch.setattr("functionals.cli.registry.sys.stdin", _TTYStdin())

    cli.run(
        [],
        print_result=False,
        shell_input_fn=_input_from_lines(["quit"]),
        shell_banner=False,
        shell_colors=False,
        shell_title="Task Console",
        shell_description="Operate tasks from this shell.",
    )

    out = capsys.readouterr().out
    assert "Task Console" in out
    assert "Operate tasks from this shell." in out


def test_run_interactive_flag_uses_shell_customization(capsys):
    _register_interactive_commands()

    cli.run(
        ["--interactive"],
        print_result=False,
        shell_input_fn=_input_from_lines(["quit"]),
        shell_banner=False,
        shell_colors=False,
        shell_title="Flag Shell",
        shell_description="Entered via flag.",
    )

    out = capsys.readouterr().out
    assert "Flag Shell" in out
    assert "Entered via flag." in out


def test_run_interactive_flag_can_print_help_menu_on_startup(capsys):
    _register_interactive_commands()

    cli.run(
        ["--interactive"],
        print_result=False,
        shell_input_fn=_input_from_lines(["quit"]),
        shell_banner=False,
        shell_colors=False,
        shell_usage=True,
    )

    out = capsys.readouterr().out
    assert "Shell builtins" in out
    assert "Registered commands" in out


def test_interactive_help_for_help_builtin_does_not_suggest_help(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(
            [
                "help --help",
                "quit",
            ]
        ),
        print_result=False,
        banner=False,
        colors=False,
    )

    out = capsys.readouterr().out
    assert "Built-in Command: help" in out
    assert "Did you mean 'help'" not in out


def test_shell_strips_terminal_escape_sequences_from_raw_input():
    raw = 'add "Task"\x1b[D\x1b[D'
    assert _strip_terminal_escapes(raw) == 'add "Task"'


def test_shell_wraps_ansi_prompt_for_readline():
    prompt = "\x1b[1;32m> \x1b[0m"
    wrapped = _wrap_ansi_for_readline(prompt)
    assert wrapped.startswith("\x01\x1b[1;32m\x02")
    assert wrapped.endswith("\x01\x1b[0m\x02")


def test_interactive_mode_supports_exec_builtin(capsys, monkeypatch):
    _register_interactive_commands()

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "exec-ok\n"
        stderr = ""

    def _fake_run(argv, capture_output, text):
        assert capture_output is True
        assert text is True
        calls.append(argv)
        return _Result()

    monkeypatch.setattr("functionals.cli.shell.subprocess.run", _fake_run)

    cli.run_shell(
        input_fn=_input_from_lines(["exec echo hello world", "quit"]),
        print_result=False,
        banner=False,
        colors=False,
    )

    out = capsys.readouterr().out
    assert "exec-ok" in out
    assert calls
    if os.name == "nt":
        assert calls[0][:4] == ["powershell", "-NoLogo", "-NoProfile", "-Command"]
        assert calls[0][4] == "echo hello world"
    else:
        assert calls[0][:2] == ["bash", "-lc"]
        assert calls[0][2] == "echo hello world"


def test_exec_falls_back_to_cmd_when_powershell_missing(capsys, monkeypatch):
    _register_interactive_commands()

    calls: list[str] = []

    class _Result:
        returncode = 0
        stdout = "fallback-ok\n"
        stderr = ""

    def _fake_run(argv, capture_output, text):
        calls.append(argv[0])
        if argv[0] == "powershell":
            raise FileNotFoundError("powershell missing")
        return _Result()

    monkeypatch.setattr("functionals.cli.shell._is_windows", lambda: True)
    monkeypatch.setattr("functionals.cli.shell.subprocess.run", _fake_run)

    cli.run_shell(
        input_fn=_input_from_lines(["exec echo from-cmd", "quit"]),
        print_result=False,
        banner=False,
        colors=False,
    )

    out = capsys.readouterr().out
    assert "fallback-ok" in out
    assert calls == ["powershell", "cmd"]


def test_exec_requires_command_text(capsys):
    _register_interactive_commands()

    cli.run_shell(
        input_fn=_input_from_lines(["exec", "quit"]),
        print_result=False,
        banner=False,
        colors=False,
    )

    out = capsys.readouterr().out
    assert "'exec' requires a command to run." in out
