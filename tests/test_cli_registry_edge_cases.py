import pytest
import asyncio
import inspect
import sys
from typing import Optional, Literal

from decorators.cli.registry import (
    CommandRegistry,
    DuplicateCommandError,
    UnknownCommandError,
)
from decorators.cli.container import DIContainer, DependencyNotFoundError
from decorators.cli.parser import build_parser
from decorators.cli.dispatcher import Dispatcher
from decorators.cli.middleware import MiddlewareChain


# --------------------------------------------------
# 1. REGISTRATION & COLLISION TESTS
# --------------------------------------------------

def test_duplicate_command_names():
    reg = CommandRegistry()
    reg.register(name="deploy")(lambda: None)

    with pytest.raises(DuplicateCommandError):
        reg.register(name="deploy")(lambda: None)


def test_alias_collision_across_commands():
    reg = CommandRegistry()
    reg.register(name="sync", ops=["-s"])(lambda: None)

    with pytest.raises(DuplicateCommandError):
        reg.register(name="status", ops=["-s"])(lambda: None)


def test_alias_collision_with_command_name():
    reg = CommandRegistry()
    reg.register(name="sync")(lambda: None)

    with pytest.raises(DuplicateCommandError):
        reg.register(name="status", ops=["sync"])(lambda: None)


# --------------------------------------------------
# 2. ARGUMENT PARSING EDGE CASES
# --------------------------------------------------

def test_type_coercion_failure():
    reg = CommandRegistry()

    @reg.register(name="add")
    def add(x: int):
        return x + 1

    parser = build_parser(reg)

    with pytest.raises(SystemExit):
        parser.parse_args(["add", "abc"])


def test_bool_flag_behavior():
    reg = CommandRegistry()

    @reg.register(name="run")
    def run(verbose: bool = False):
        return verbose

    parser = build_parser(reg)

    ns = parser.parse_args(["run"])
    assert ns.verbose is False

    ns = parser.parse_args(["run", "--verbose"])
    assert ns.verbose is True

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--verbose", "false"])


def test_optional_argument_behavior():
    reg = CommandRegistry()

    @reg.register(name="run")
    def run(output: Optional[str] = None):
        return output

    parser = build_parser(reg)

    ns = parser.parse_args(["run"])
    assert ns.output is None


def test_missing_required_argument():
    reg = CommandRegistry()

    @reg.register(name="run")
    def run(name: str):
        return name

    parser = build_parser(reg)

    with pytest.raises(SystemExit):
        parser.parse_args(["run"])


def test_default_value_usage():
    reg = CommandRegistry()

    @reg.register(name="run")
    def run(limit: int = 10):
        return limit

    parser = build_parser(reg)

    ns = parser.parse_args(["run"])
    assert ns.limit == 10


def test_default_value_missing_flag_value():
    reg = CommandRegistry()

    @reg.register(name="run")
    def run(limit: int = 10):
        return limit

    parser = build_parser(reg)

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--limit"])


def test_enum_argument_valid():
    reg = CommandRegistry()

    @reg.register(name="deploy")
    def deploy(env: Literal["dev", "prod"]):
        return env

    parser = build_parser(reg)

    ns = parser.parse_args(["deploy", "dev"])
    assert ns.env == "dev"


def test_enum_argument_invalid():
    reg = CommandRegistry()

    @reg.register(name="deploy")
    def deploy(env: Literal["dev", "prod"]):
        return env

    parser = build_parser(reg)

    with pytest.raises(SystemExit):
        parser.parse_args(["deploy", "staging"])


# --------------------------------------------------
# 3. DISPATCH + EXECUTION PIPELINE
# --------------------------------------------------

def test_dispatch_success():
    reg = CommandRegistry()

    @reg.register(name="hello")
    def hello():
        return "world"

    dispatcher = Dispatcher(reg, DIContainer())

    result = dispatcher.dispatch("hello", {})
    assert result == "world"


def test_unknown_command_dispatch():
    reg = CommandRegistry()
    dispatcher = Dispatcher(reg, DIContainer())

    with pytest.raises(UnknownCommandError):
        dispatcher.dispatch("missing", {})


def test_dispatch_argument_passing():
    reg = CommandRegistry()

    @reg.register(name="add")
    def add(x: int, y: int):
        return x + y

    dispatcher = Dispatcher(reg, DIContainer())

    result = dispatcher.dispatch("add", {"x": 2, "y": 3})
    assert result == 5


# --------------------------------------------------
# 4. DEPENDENCY INJECTION
# --------------------------------------------------

def test_dependency_injection_success():
    reg = CommandRegistry()
    container = DIContainer()

    class DB:
        def ping(self):
            return "ok"

    container.register(DB, DB())

    @reg.register(name="run")
    def run(db: DB):
        return db.ping()

    dispatcher = Dispatcher(reg, container)

    result = dispatcher.dispatch("run", {})
    assert result == "ok"


def test_missing_dependency():
    reg = CommandRegistry()
    container = DIContainer()

    @reg.register(name="run")
    def run(db):
        return db

    dispatcher = Dispatcher(reg, container)

    with pytest.raises(DependencyNotFoundError):
        dispatcher.dispatch("run", {})


def test_di_not_exposed_to_cli():
    reg = CommandRegistry()

    class DB: ...

    @reg.register(name="run")
    def run(db: DB):
        return db

    parser = build_parser(reg)

    # DB should not appear as CLI argument
    ns = parser.parse_args(["run"])
    assert not hasattr(ns, "db")


# --------------------------------------------------
# 5. MIDDLEWARE
# --------------------------------------------------

def test_middleware_execution_order():
    reg = CommandRegistry()

    @reg.register(name="cmd")
    def cmd():
        return "ok"

    order = []

    def pre1(cmd, kwargs): order.append("pre1")
    def pre2(cmd, kwargs): order.append("pre2")
    def post1(cmd, result): order.append("post1")
    def post2(cmd, result): order.append("post2")

    chain = MiddlewareChain()
    chain.add_pre(pre1)
    chain.add_pre(pre2)
    chain.add_post(post1)
    chain.add_post(post2)

    dispatcher = Dispatcher(reg, DIContainer(), chain)
    dispatcher.dispatch("cmd", {})

    assert order == ["pre1", "pre2", "post1", "post2"]


def test_middleware_pre_failure_stops_execution():
    reg = CommandRegistry()

    called = {"executed": False}

    @reg.register(name="cmd")
    def cmd():
        called["executed"] = True

    def fail_pre(cmd, kwargs):
        raise RuntimeError("fail")

    chain = MiddlewareChain()
    chain.add_pre(fail_pre)

    dispatcher = Dispatcher(reg, DIContainer(), chain)

    with pytest.raises(RuntimeError):
        dispatcher.dispatch("cmd", {})

    assert called["executed"] is False


# --------------------------------------------------
# 6. ASYNC SUPPORT
# --------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.skipif(sys.version_info < (3, 8), reason="Requires Python 3.8+")
async def test_async_command():
    reg = CommandRegistry()

    @reg.register(name="async_cmd")
    async def async_cmd():
        return 42

    dispatcher = Dispatcher(reg, DIContainer())

    if inspect.iscoroutinefunction(async_cmd):
        result = await dispatcher.dispatch("async_cmd", {})
        assert result == 42


# --------------------------------------------------
# 7. HELP & UX
# --------------------------------------------------

def test_help_output(capsys):
    reg = CommandRegistry()

    @reg.register(name="greet", description="Say hello", ops=["-g"])
    def greet(name: str):
        pass

    parser = build_parser(reg)

    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])

    out = capsys.readouterr().out

    assert "greet" in out
    assert "Say hello" in out
    assert "-g" in out


def test_unknown_command_suggestion(capsys):
    reg = CommandRegistry()

    @reg.register(name="hello")
    def hello():
        pass

    parser = build_parser(reg)

    with pytest.raises(SystemExit):
        parser.parse_args(["helo"])

    out = capsys.readouterr().out

    assert "Unknown command" in out or "Did you mean" in out


# --------------------------------------------------
# 8. EDGE CASES
# --------------------------------------------------

def test_empty_registry_run():
    reg = CommandRegistry()
    parser = build_parser(reg)

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_command_returns_none():
    reg = CommandRegistry()

    @reg.register(name="noop")
    def noop():
        return None

    dispatcher = Dispatcher(reg, DIContainer())

    result = dispatcher.dispatch("noop", {})
    assert result is None


def test_large_number_of_commands():
    reg = CommandRegistry()

    for i in range(100):
        reg.register(name=f"cmd{i}")(lambda: i)

    assert len(reg._commands) == 100