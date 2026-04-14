"""
tests/unit/test_py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for the framework layer. Each module is tested in isolation
with no real filesystem or subprocess involvement.
"""

import pytest

from decorators.cli.registry import CommandRegistry
from decorators.cli.container import DIContainer
from decorators.cli.exceptions import (
    DuplicateCommandError,
    UnknownCommandError,
    DependencyNotFoundError,
)
from decorators.cli.dispatcher import Dispatcher
from decorators.cli.parser import build_parser
from decorators.cli.middleware import MiddlewareChain
from decorators.cli.utils.typing import resolve_argparse_type, is_bool_flag, is_optional
from decorators.cli.utils.reflection import get_params
from typing import Optional


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestCommandRegistry:
    def test_register_and_retrieve(self):
        reg = CommandRegistry()

        @reg.register("hello", help_text="say hi")
        def hello(name: str) -> str:
            return f"hi {name}"

        entry = reg.get("hello")
        assert entry.name == "hello"
        assert entry.help_text == "say hi"
        assert entry.handler is hello

    def test_duplicate_raises(self):
        reg = CommandRegistry()
        reg.register("dup", help_text="")(lambda: None)
        with pytest.raises(DuplicateCommandError):
            reg.register("dup", help_text="")(lambda: None)

    def test_unknown_raises(self):
        reg = CommandRegistry()
        with pytest.raises(UnknownCommandError):
            reg.get("nope")

    def test_has(self):
        reg = CommandRegistry()
        reg.register("x")(lambda: None)
        assert reg.has("x")
        assert not reg.has("y")

    def test_merge(self):
        a = CommandRegistry()
        b = CommandRegistry()
        a.register("foo")(lambda: None)
        b.register("bar")(lambda: None)
        a.merge(b)
        assert a.has("bar")

    def test_merge_collision_raises(self):
        a = CommandRegistry()
        b = CommandRegistry()
        a.register("clash")(lambda: None)
        b.register("clash")(lambda: None)
        with pytest.raises(DuplicateCommandError):
            a.merge(b)

    def test_merge_override(self):
        a = CommandRegistry()
        b = CommandRegistry()
        fn_a = lambda: "a"
        fn_b = lambda: "b"
        a.register("cmd")(fn_a)
        b.register("cmd")(fn_b)
        a.merge(b, allow_override=True)
        assert a.get("cmd").handler is fn_b

    def test_len(self):
        reg = CommandRegistry()
        assert len(reg) == 0
        reg.register("one")(lambda: None)
        assert len(reg) == 1

    def test_register_supports_description_and_ops(self):
        reg = CommandRegistry()

        @reg.register(name="greet", description="Say hello", ops=["-g", "--greet"])
        def greet(name: str) -> str:
            return f"hi {name}"

        entry = reg.get("greet")
        assert entry.handler is greet
        assert entry.help_text == "Say hello"
        assert entry.description == "Say hello"
        assert entry.ops == ("-g", "--greet")

    def test_run_maps_flag_style_alias_to_command_name(self):
        reg = CommandRegistry()

        @reg.register(name="greet", description="Say hello", ops=["-g", "--greet"])
        def greet(name: str) -> str:
            return f"hi {name}"

        assert reg.run(["--greet", "Alice"], print_result=False) == "hi Alice"

    def test_run_maps_stripped_alias_to_command_name(self):
        reg = CommandRegistry()

        @reg.register(name="greet", description="Say hello", ops=["-g", "--greet"])
        def greet(name: str) -> str:
            return f"hi {name}"

        assert reg.run(["g", "Alice"], print_result=False) == "hi Alice"

    def test_list_commands_prints_aliases(self, capsys):
        reg = CommandRegistry()
        reg.register(name="greet", description="Say hello", ops=["-g", "--greet"])(lambda: None)
        reg.list_commands()
        out = capsys.readouterr().out
        assert "greet" in out
        assert "--greet" in out


# ---------------------------------------------------------------------------
# DI Container
# ---------------------------------------------------------------------------

class TestDIContainer:
    def test_register_and_resolve(self):
        class MyService:
            pass

        container = DIContainer()
        svc = MyService()
        container.register(MyService, svc)
        assert container.resolve(MyService) is svc

    def test_missing_raises(self):
        class Ghost:
            pass

        container = DIContainer()
        with pytest.raises(DependencyNotFoundError):
            container.resolve(Ghost)

    def test_has(self):
        class Svc:
            pass

        container = DIContainer()
        assert not container.has(Svc)
        container.register(Svc, Svc())
        assert container.has(Svc)


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

class TestTypingHelpers:
    def test_primitives(self):
        assert resolve_argparse_type(int) is int
        assert resolve_argparse_type(float) is float
        assert resolve_argparse_type(str) is str

    def test_optional_unwrap(self):
        assert resolve_argparse_type(Optional[int]) is int

    def test_bool_returns_none(self):
        assert resolve_argparse_type(bool) is None

    def test_is_optional(self):
        assert is_optional(Optional[str])
        assert not is_optional(str)

    def test_is_bool_flag(self):
        assert is_bool_flag(bool)
        assert not is_bool_flag(str)


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------

class TestReflection:
    def test_get_params(self):
        def fn(a: int, b: str = "default") -> None:
            pass

        params = get_params(fn)
        assert len(params) == 2
        assert params[0].name == "a"
        assert params[0].annotation is int
        assert not params[0].has_default
        assert params[1].name == "b"
        assert params[1].has_default
        assert params[1].default == "default"

    def test_skips_self(self):
        class Cls:
            def method(self, x: int) -> None:
                pass

        params = get_params(Cls.method)
        assert all(p.name != "self" for p in params)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class TestDispatcher:
    def _make(self, fn, services=None):
        reg = CommandRegistry()
        reg.register("cmd")(fn)
        container = DIContainer()
        for t, inst in (services or {}).items():
            container.register(t, inst)
        return Dispatcher(reg, container)

    def test_basic_dispatch(self):
        called = {}

        def handler(name: str) -> str:
            called["name"] = name
            return f"hi {name}"

        d = self._make(handler)
        result = d.dispatch("cmd", {"name": "Alice"})
        assert result == "hi Alice"
        assert called["name"] == "Alice"

    def test_di_injection(self):
        class FakeSvc:
            value = 42

        def handler(svc: FakeSvc) -> int:
            return svc.value

        d = self._make(handler, {FakeSvc: FakeSvc()})
        assert d.dispatch("cmd", {}) == 42

    def test_unknown_command_raises(self):
        reg = CommandRegistry()
        container = DIContainer()
        d = Dispatcher(reg, container)
        with pytest.raises(UnknownCommandError):
            d.dispatch("ghost", {})

    def test_middleware_hooks_called(self):
        log = []

        def pre(cmd, kwargs):
            log.append(f"pre:{cmd}")

        def post(cmd, result):
            log.append(f"post:{cmd}")

        reg = CommandRegistry()
        reg.register("x")(lambda: None)
        container = DIContainer()
        chain = MiddlewareChain()
        chain.add_pre(pre)
        chain.add_post(post)
        d = Dispatcher(reg, container, chain)
        d.dispatch("x", {})
        assert log == ["pre:x", "post:x"]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestParser:
    def test_builds_subcommands(self):
        reg = CommandRegistry()
        reg.register("greet", help_text="Say hi")(lambda name: None)
        reg.register("add")(lambda a: None)
        parser = build_parser(reg)
        # Should parse known subcommands without error
        ns = parser.parse_args(["greet", "Alice"])
        assert ns.command == "greet"
        assert ns.name == "Alice"

    def test_optional_arg(self):
        reg = CommandRegistry()
        reg.register("cmd")(lambda msg="hello": None)
        parser = build_parser(reg)
        ns = parser.parse_args(["cmd"])
        assert ns.msg == "hello"
        ns2 = parser.parse_args(["cmd", "--msg", "world"])
        assert ns2.msg == "world"

    def test_bool_flag(self):
        reg = CommandRegistry()

        def cmd(verbose: bool) -> None:
            pass

        reg.register("cmd")(cmd)
        parser = build_parser(reg)
        # Bool flag should be optional and default False
        ns = parser.parse_args(["cmd"])
        assert ns.verbose is False
        ns2 = parser.parse_args(["cmd", "--verbose"])
        assert ns2.verbose is True

    def test_subcommand_aliases(self):
        reg = CommandRegistry()
        reg.register(name="greet", description="Say hi", ops=["-g", "--hello"])(lambda name: None)
        parser = build_parser(reg)
        ns = parser.parse_args(["hello", "Alice"])
        assert ns.command == "hello"
        assert ns.name == "Alice"
