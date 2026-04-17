import pytest

import functionals.cli as cli


@pytest.fixture(autouse=True)
def _reset_registry():
    cli.reset_registry()
    yield
    cli.reset_registry()


def _register_todo_commands() -> None:
    @cli.register(description="Add a new todo item")
    @cli.argument("title", type=str, help="Title of the todo item")
    @cli.argument("description", type=str, help="Description of the todo item", default="")
    @cli.option("--add")
    @cli.option("-a")
    def add_todo(title: str, description: str = "") -> str:
        return f"Added todo: {title} | {description}"

    @cli.register(description="Update todo")
    @cli.argument("todo_id", type=int, help="Todo id")
    @cli.argument("title", type=str, help="Title", default=None)
    @cli.argument("description", type=str, help="Description", default=None)
    @cli.option("--update")
    @cli.option("-u")
    def update_todo(todo_id: int, title: str | None = None, description: str | None = None) -> tuple[int, str | None, str | None]:
        return (todo_id, title, description)



def test_add_supports_positional_and_alias_command_tokens():
    _register_todo_commands()

    assert (
        cli.run(["add", "Buy groceries", "Milk, eggs, bread"], print_result=False)
        == "Added todo: Buy groceries | Milk, eggs, bread"
    )
    assert (
        cli.run(["--add", "Read a book", "Read start to finish"], print_result=False)
        == "Added todo: Read a book | Read start to finish"
    )


def test_add_supports_named_arguments():
    _register_todo_commands()

    result = cli.run(
        ["add", "--title", "Read a book", "--description", "Read start to finish"],
        print_result=False,
    )

    assert result == "Added todo: Read a book | Read start to finish"


def test_update_supports_positional_named_and_mixed_forms():
    _register_todo_commands()

    assert cli.run(["update", "1", "Read two books", "Finish both novels"], print_result=False) == (
        1,
        "Read two books",
        "Finish both novels",
    )

    assert cli.run(
        [
            "--update",
            "1",
            "--title",
            "Read two books",
            "--description",
            "Finish both novels",
        ],
        print_result=False,
    ) == (1, "Read two books", "Finish both novels")

    assert cli.run(
        ["update", "1", "Read two books", "--description", "Finish both novels"],
        print_result=False,
    ) == (1, "Read two books", "Finish both novels")


def test_duplicate_argument_with_different_values_is_parse_error(capsys):
    _register_todo_commands()

    with pytest.raises(SystemExit) as exc:
        cli.run(["add", "Task A", "--title", "Task B"], print_result=False)

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "provided multiple times with different values" in out


def test_duplicate_argument_with_same_values_is_allowed():
    _register_todo_commands()

    result = cli.run(["add", "Task A", "--title", "Task A"], print_result=False)
    assert result == "Added todo: Task A | "


def test_boolean_flag_parsing_uses_flag_style():
    @cli.register(description="Run command")
    @cli.option("--run")
    @cli.argument("verbose", type=bool, help="Enable verbose mode")
    def run_cmd(verbose: bool = False) -> bool:
        return verbose

    assert cli.run(["run"], print_result=False) is False
    assert cli.run(["run", "--verbose"], print_result=False) is True


def test_unknown_option_prints_parse_error(capsys):
    _register_todo_commands()

    with pytest.raises(SystemExit) as exc:
        cli.run(["add", "--unknown", "x"], print_result=False)

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Unknown option '--unknown'" in out


def test_unknown_command_shows_suggestion(capsys):
    _register_todo_commands()

    with pytest.raises(SystemExit) as exc:
        cli.run(["ad"], print_result=False)

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Did you mean 'add'" in out


def test_builtin_help_command_prints_professional_menu(capsys):
    _register_todo_commands()

    assert cli.run(["help"], print_result=False) is None
    out = capsys.readouterr().out

    assert "Decorates CLI Help" in out
    assert "Built-in Command" in out
    assert "Commands" in out
    assert "add" in out
    assert "update" in out
    assert "help <command>" in out


def test_builtin_help_aliases_print_menu(capsys):
    _register_todo_commands()

    assert cli.run(["--help"], print_result=False) is None
    out = capsys.readouterr().out
    assert "Decorates CLI Help" in out

    assert cli.run(["-h"], print_result=False) is None
    out = capsys.readouterr().out
    assert "Decorates CLI Help" in out


def test_builtin_help_supports_command_specific_view(capsys):
    _register_todo_commands()

    assert cli.run(["help", "add"], print_result=False) is None
    out = capsys.readouterr().out
    assert "Command Help: add" in out
    assert "Arguments" in out
    assert "title (str, required)" in out
    assert "description (str, optional" in out


def test_builtin_help_unknown_command_exits_with_code_2(capsys):
    _register_todo_commands()

    with pytest.raises(SystemExit) as exc:
        cli.run(["help", "ad"], print_result=False)

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Did you mean 'add'" in out


def test_inferred_arguments_work_when_argument_decorator_is_omitted():
    @cli.register(description="Multiply")
    @cli.option("--multiply")
    def multiply(num1: int, num2: int = 1) -> int:
        return num1 * num2

    assert cli.run(["multiply", "3", "4"], print_result=False) == 12
    assert cli.run(["multiply", "3", "--num2", "5"], print_result=False) == 15
    assert cli.run(["--multiply", "3"], print_result=False) == 3
