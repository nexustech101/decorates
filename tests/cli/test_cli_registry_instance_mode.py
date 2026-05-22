from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import registers.cli as cli
from registers.cli.exceptions import DuplicateCommandError


@pytest.fixture(autouse=True)
def _reset_module_registry() -> None:
    cli.reset_registry()
    yield
    cli.reset_registry()


def _input_from_lines(lines: list[str]):
    iterator = iter(lines)

    def _read(_prompt: str) -> str:
        return next(iterator)

    return _read


def _purge_package(package_name: str) -> None:
    stale = [name for name in list(sys.modules) if name == package_name or name.startswith(f"{package_name}.")]
    for name in stale:
        sys.modules.pop(name, None)


def test_instance_decorators_register_and_run_happy_paths() -> None:
    registry = cli.CommandRegistry()

    @registry.register(description="Greet a user")
    @registry.argument("name", type=str, help="Name to greet")
    @registry.option("--greet")
    @registry.option("-g")
    def greet(name: str) -> str:
        return f"hello:{name}"

    assert registry.has("greet")
    assert registry.get("--greet").name == "greet"
    assert registry.run(["greet", "Ada"], print_result=False) == "hello:Ada"
    assert registry.run(["--greet", "Ada"], print_result=False) == "hello:Ada"
    assert registry.run(["-g", "--name", "Ada"], print_result=False) == "hello:Ada"


def test_instance_registries_are_isolated_and_allow_same_aliases() -> None:
    first = cli.CommandRegistry()
    second = cli.CommandRegistry()

    @first.register(description="First sync")
    @first.option("--sync")
    def sync_first() -> str:
        return "first"

    @second.register(description="Second sync")
    @second.option("--sync")
    def sync_second() -> str:
        return "second"

    @cli.register(description="Module sync")
    @cli.option("--sync")
    def sync_module() -> str:
        return "module"

    assert first.run(["sync"], print_result=False) == "first"
    assert second.run(["sync"], print_result=False) == "second"
    assert cli.run(["sync"], print_result=False) == "module"


def test_instance_collision_raises_duplicate_command_error() -> None:
    registry = cli.CommandRegistry()

    @registry.register(description="One")
    @registry.option("--add")
    def one() -> str:
        return "1"

    with pytest.raises(DuplicateCommandError):

        @registry.register(description="Two")
        @registry.option("--add")
        def two() -> str:
            return "2"


def test_instance_parse_error_paths_show_usage_and_exit(capsys) -> None:
    registry = cli.CommandRegistry()

    @registry.register(description="Multiply")
    @registry.argument("value", type=int)
    @registry.option("--multiply")
    def multiply(value: int) -> int:
        return value * 2

    with pytest.raises(SystemExit) as exc:
        registry.run(["multiply", "bad-int"], print_result=False)

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Invalid value for 'value'" in out
    assert "usage:" in out

    with pytest.raises(SystemExit) as exc:
        registry.run(["multiply", "--unknown", "1"], print_result=False)

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Unknown option '--unknown'" in out


def test_instance_shell_path_dispatches_registered_command(capsys) -> None:
    registry = cli.CommandRegistry()

    @registry.register(description="Echo text")
    @registry.argument("text", type=str)
    @registry.option("--echo")
    def echo(text: str) -> str:
        return text

    registry.run_shell(
        input_fn=_input_from_lines(["echo hello", "quit"]),
        print_result=True,
        banner=False,
        colors=False,
    )
    shell_out = capsys.readouterr().out
    assert "hello" in shell_out


def test_instance_discovery_load_plugins_targets_explicit_registry(tmp_path: Path) -> None:
    package_name = "instance_cli_plugins"
    package_dir = tmp_path / package_name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "alpha.py").write_text(
        "\n".join(
            [
                "import registers.cli as cli",
                "@cli.register(description='Plugin ping')",
                "@cli.option('--ping')",
                "def ping() -> str:",
                "    return 'pong'",
            ]
        ),
        encoding="utf-8",
    )

    registry = cli.CommandRegistry()
    sys.path.insert(0, str(tmp_path))
    _purge_package(package_name)
    try:
        loaded = registry.load_plugins(package_name)
    finally:
        _purge_package(package_name)
        sys.path.pop(0)

    assert loaded
    assert registry.has("ping")
    assert registry.run(["ping"], print_result=False) == "pong"
    assert not cli.get_registry().has("ping")


def test_instance_register_plugin_merges_commands_from_registry_instance() -> None:
    host = cli.CommandRegistry()
    users_plugin = cli.CommandRegistry()
    ops_plugin = cli.CommandRegistry()

    @users_plugin.register(name="create-user", description="Create user")
    @users_plugin.option("--create-user")
    @users_plugin.argument("email", type=str)
    def create_user(email: str) -> str:
        return f"user:{email}"

    @ops_plugin.register(name="health", description="Health check")
    @ops_plugin.option("--health")
    def health() -> str:
        return "ok"

    assert host.register_plugin(users_plugin) == 1
    assert host.register_plugin(ops_plugin) == 1

    assert host.run(["create-user", "ada@example.com"], print_result=False) == "user:ada@example.com"
    assert host.run(["health"], print_result=False) == "ok"


def test_instance_register_plugin_preserves_group_scoped_aliases() -> None:
    host = cli.CommandRegistry()
    users_plugin = cli.CommandRegistry()
    billing_plugin = cli.CommandRegistry()

    users = users_plugin.group("users", aliases=["u"], description="User commands")
    billing = billing_plugin.group("billing", aliases=["b"], description="Billing commands")

    @users.register("list", description="List users")
    @users.alias("--list")
    def list_users() -> str:
        return "users"

    @billing.register("list", description="List invoices")
    @billing.alias("--list")
    def list_billing() -> str:
        return "billing"

    assert host.register_plugin(users_plugin) == 1
    assert host.register_plugin(billing_plugin) == 1

    assert host.run(["users", "--list"], print_result=False) == "users"
    assert host.run(["u", "--list"], print_result=False) == "users"
    assert host.run(["billing", "--list"], print_result=False) == "billing"
    assert host.run(["b", "--list"], print_result=False) == "billing"

    with pytest.raises(SystemExit) as exc:
        host.run(["--list"], print_result=False)
    assert exc.value.code == 2


def test_instance_register_plugin_supports_module_with_cli_registry() -> None:
    host = cli.CommandRegistry()
    plugin_registry = cli.CommandRegistry()
    plugin_module = ModuleType("demo_plugin")
    plugin_module.cli = plugin_registry

    @plugin_registry.register(name="ping", description="Ping")
    @plugin_registry.option("--ping")
    def ping() -> str:
        return "pong"

    assert host.register_plugin(plugin_module) == 1
    assert host.run(["ping"], print_result=False) == "pong"


def test_instance_register_plugin_rejects_invalid_plugin_object() -> None:
    registry = cli.CommandRegistry()

    with pytest.raises(TypeError):
        registry.register_plugin(object())


def test_instance_register_plugin_raises_on_colliding_command_or_alias() -> None:
    host = cli.CommandRegistry()
    first_plugin = cli.CommandRegistry()
    second_plugin = cli.CommandRegistry()

    @first_plugin.register(name="sync", description="First")
    @first_plugin.option("--sync")
    def sync_first() -> str:
        return "first"

    @second_plugin.register(name="sync", description="Second")
    @second_plugin.option("--sync2")
    def sync_second() -> str:
        return "second"

    host.register_plugin(first_plugin)
    with pytest.raises(DuplicateCommandError):
        host.register_plugin(second_plugin)


def test_instance_and_module_facades_can_coexist_without_cross_registration() -> None:
    registry = cli.CommandRegistry()

    @registry.register(description="Instance command")
    @registry.option("--instance")
    def instance_cmd() -> str:
        return "instance"

    @cli.register(description="Module command")
    @cli.option("--module")
    def module_cmd() -> str:
        return "module"

    assert registry.has("instance")
    assert not registry.has("module")
    assert cli.get_registry().has("module")
    assert not cli.get_registry().has("instance")
