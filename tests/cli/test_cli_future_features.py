from __future__ import annotations

import enum
from pathlib import Path

import pytest

import registers.cli as cli
from registers.cli import Context
from registers.cli import types as t


@pytest.fixture(autouse=True)
def _reset_registry():
    cli.reset_registry()
    yield
    cli.reset_registry()


def _input_from_lines(lines: list[str]):
    iterator = iter(lines)

    def _read(_prompt: str) -> str:
        return next(iterator)

    return _read


def test_grouped_commands_support_longest_match_aliases():
    registry = cli.CommandRegistry()
    users = registry.group("users", description="User commands", aliases=["u"], tags=["users"])
    deploy = users.group("deploy", description="Deployment commands", aliases=["d"])

    @users.register("list", description="List users", examples=["users list"], tags=["read"])
    def list_users() -> str:
        return "users"

    @deploy.register("service", description="Deploy service")
    @deploy.argument("name", type=str)
    def deploy_service(name: str) -> str:
        return f"deploy:{name}"

    assert registry.run(["users", "list"], print_result=False) == "users"
    assert registry.run(["u", "list"], print_result=False) == "users"
    assert registry.run(["users", "deploy", "service", "api"], print_result=False) == "deploy:api"
    assert registry.run(["u", "d", "service", "api"], print_result=False) == "deploy:api"


def test_grouped_command_aliases_are_scoped_to_group_path():
    registry = cli.CommandRegistry()
    users = registry.group("users", description="User commands", aliases=["u"])
    projects = registry.group("projects", description="Project commands", aliases=["p"])

    @users.register("list", description="List users")
    @users.alias("--list")
    @users.alias("-l")
    def list_users() -> str:
        return "users"

    @projects.register("list", description="List projects")
    @projects.alias("--list")
    @projects.alias("-l")
    def list_projects() -> str:
        return "projects"

    assert registry.run(["users", "--list"], print_result=False) == "users"
    assert registry.run(["users", "-l"], print_result=False) == "users"
    assert registry.run(["u", "--list"], print_result=False) == "users"
    assert registry.run(["projects", "--list"], print_result=False) == "projects"
    assert registry.run(["p", "-l"], print_result=False) == "projects"

    with pytest.raises(SystemExit) as exc:
        registry.run(["--list"], print_result=False)
    assert exc.value.code == 2


def test_nested_group_command_aliases_are_scoped_to_group_alias_paths():
    registry = cli.CommandRegistry()
    users = registry.group("users", aliases=["u"])
    deploy = users.group("deploy", aliases=["d"])

    @deploy.register("service", description="Deploy service")
    @deploy.alias("--service")
    @deploy.argument("name", type=str)
    def deploy_service(name: str) -> str:
        return f"deploy:{name}"

    assert registry.run(["users", "deploy", "--service", "api"], print_result=False) == "deploy:api"
    assert registry.run(["u", "d", "--service", "api"], print_result=False) == "deploy:api"


def test_command_argument_named_output_wins_over_framework_output_flag():
    registry = cli.CommandRegistry()

    @registry.register(description="Echo output")
    @registry.argument("output", type=str)
    def echo(output: str) -> str:
        return output

    assert registry.run(["echo", "--output", "value"], print_result=False) == "value"


def test_extended_types_cover_choices_bounds_paths_dates_lists_json_and_enums(tmp_path: Path):
    registry = cli.CommandRegistry()
    input_file = tmp_path / "input.txt"
    input_file.write_text("ok", encoding="utf-8")

    class Role(enum.Enum):
        admin = "admin"
        member = "member"

    @registry.register(description="Typed")
    @registry.argument("env", type=t.Choice(["dev", "prod"]))
    @registry.argument("count", type=t.Int(min=1, max=5))
    @registry.argument("ratio", type=t.Float(min=0.0, max=1.0))
    @registry.argument("path", type=t.Path(exists=True))
    @registry.argument("day", type=t.Date())
    @registry.argument("tags", type=t.List(str))
    @registry.argument("payload", type=t.JSON)
    @registry.argument("role", type=t.Enum(Role))
    def typed(env, count, ratio, path, day, tags, payload, role):
        return env, count, ratio, path.name, day.isoformat(), tags, payload["ok"], role.value

    assert registry.run(
        [
            "typed",
            "prod",
            "3",
            "0.5",
            str(input_file),
            "2026-05-09",
            "a,b",
            '{"ok": true}',
            "admin",
        ],
        print_result=False,
    ) == ("prod", 3, 0.5, "input.txt", "2026-05-09", ["a", "b"], True, "admin")

    with pytest.raises(SystemExit) as exc:
        registry.run(["typed", "qa"], print_result=False)
    assert exc.value.code == 2


def test_prompt_confirmation_and_dry_run(capsys):
    registry = cli.CommandRegistry()

    @registry.register(description="Create")
    @registry.argument("name", type=str, prompt=True)
    def create(name: str) -> str:
        return f"created:{name}"

    assert registry.run(["create"], print_result=False, shell_input_fn=_input_from_lines(["Ada"])) == "created:Ada"

    @registry.register(description="Drop")
    @registry.argument("db_name", type=str)
    @registry.confirm("Drop {db_name}?", confirm_phrase="drop {db_name}")
    def drop(db_name: str) -> str:
        return f"dropped:{db_name}"

    assert registry.run(
        ["drop", "prod"],
        print_result=False,
        shell_input_fn=_input_from_lines(["drop prod"]),
    ) == "dropped:prod"
    assert registry.run(["drop", "prod", "--force"], print_result=False) == "dropped:prod"

    @registry.register(description="Migrate")
    @registry.dry_run()
    def migrate(dry_run: bool = False) -> bool:
        return dry_run

    assert registry.run(["migrate", "--dry-run"], print_result=False) is True


def test_prompt_decorator_supports_choices_and_enums():
    registry = cli.CommandRegistry()

    class Region(enum.Enum):
        east = "us-east-1"
        west = "us-west-2"

    @registry.register(description="Deploy")
    @registry.prompt("env", "Choose environment", type=t.Choice(["dev", "prod"]))
    @registry.prompt("region", "Choose region", type=Region)
    def deploy(env: str, region: Region) -> tuple[str, str]:
        return env, region.value

    seen_prompts: list[str] = []
    answers = iter(["2", "us-west-2"])

    def read(prompt: str) -> str:
        seen_prompts.append(prompt)
        return next(answers)

    result = registry.run(
        ["deploy"],
        print_result=False,
        shell_input_fn=read,
    )

    assert result == ("prod", "us-west-2")
    prompts = "\n".join(seen_prompts)
    assert "Choose environment" in prompts
    assert "1. dev" in prompts
    assert "2. prod" in prompts
    assert "Choose region" in prompts
    assert "us-east-1" in prompts
    assert "us-west-2" in prompts


def test_async_commands_and_context():
    registry = cli.CommandRegistry()

    class AppContext(Context):
        def __init__(self, env: str) -> None:
            self.env = env

    @registry.context_factory
    def build_context(env: str = "prod") -> AppContext:
        return AppContext(env)

    @registry.register(description="Health")
    async def health(ctx: AppContext) -> dict[str, str]:
        return {"env": ctx.env}

    assert registry.run(["--env", "staging", "health"], print_result=False) == {"env": "staging"}

    @registry.register(description="Echo")
    @registry.argument("value", type=str)
    async def echo(value: str) -> str:
        return value

    assert registry.run(["echo", "ok"], print_result=False) == "ok"

    async def _call() -> str:
        return await registry.run_async(["echo", "async"], print_result=False)

    import asyncio

    assert asyncio.run(_call()) == "async"


def test_public_exports_include_future_helpers():
    assert cli.Context
    assert cli.types.Choice
    assert cli.prompt
