import pytest

import functionals.cli as cli
from functionals.cli.exceptions import DuplicateCommandError


@pytest.fixture(autouse=True)
def _reset_registry():
    cli.reset_registry()
    yield
    cli.reset_registry()


def test_registry_class_no_longer_exposes_register_decorator_method():
    assert not hasattr(cli.CommandRegistry, "register")


def test_module_level_decorators_finalize_command_with_argument_order():
    @cli.register(description="Add a todo")
    @cli.argument("title", type=str, help="Title")
    @cli.argument("description", type=str, help="Description", default="")
    @cli.option("--add", help="Add alias")
    @cli.option("-a", help="Short alias")
    def add_todo(title: str, description: str = "") -> str:
        return f"{title}:{description}"

    entry = cli.get_registry().get("add")

    assert entry.name == "add"
    assert entry.description == "Add a todo"
    assert entry.options == ("--add", "-a")
    assert [arg.name for arg in entry.arguments] == ["title", "description"]
    assert entry.arguments[0].required is True
    assert entry.arguments[1].required is False
    assert entry.arguments[1].default == ""


def test_register_name_defaults_from_first_long_option():
    @cli.register(description="List todos")
    @cli.option("--list")
    def list_todos() -> str:
        return "ok"

    assert cli.get_registry().has("list")
    assert cli.get_registry().get("--list").name == "list"


def test_register_name_falls_back_to_function_name_without_long_option():
    @cli.register(description="Ping")
    @cli.option("-p")
    def ping_server() -> str:
        return "pong"

    assert cli.get_registry().has("ping_server")
    assert cli.get_registry().get("-p").name == "ping_server"


def test_alias_collision_raises_duplicate_command_error():
    @cli.register(description="First")
    @cli.option("--add")
    def one() -> str:
        return "1"

    with pytest.raises(DuplicateCommandError):

        @cli.register(description="Second")
        @cli.option("--add")
        def two() -> str:
            return "2"


def test_argument_name_must_exist_on_function_signature():
    with pytest.raises(ValueError, match="does not match any parameter"):

        @cli.register(description="Broken")
        @cli.argument("missing")
        def broken(name: str) -> str:
            return name


def test_duplicate_argument_declaration_rejected():
    with pytest.raises(ValueError, match="declared more than once"):

        @cli.register(description="Broken")
        @cli.argument("name")
        @cli.argument("name")
        def broken(name: str) -> str:
            return name


def test_list_commands_prints_registered_aliases(capsys):
    @cli.register(description="Greet")
    @cli.option("--hello")
    @cli.option("-x")
    @cli.argument("name")
    def hello(name: str) -> str:
        return f"hi {name}"

    cli.list_commands()
    out = capsys.readouterr().out

    assert "hello" in out
    assert "--hello" in out
    assert "-x" in out


def test_help_name_and_aliases_are_reserved():
    with pytest.raises(ValueError, match="reserved"):
        @cli.register(description="Reserved name")
        def help() -> None:
            return None

    with pytest.raises(ValueError, match="reserved"):
        @cli.register(description="Reserved alias")
        @cli.option("--help")
        def custom_help_alias() -> None:
            return None

    with pytest.raises(ValueError, match="reserved"):
        @cli.register(description="Reserved alias")
        @cli.option("-h")
        def custom_help_short_alias() -> None:
            return None
