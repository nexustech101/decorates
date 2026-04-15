from __future__ import annotations

from decorates.cli import CommandRegistry, DIContainer, Dispatcher
from decorates.cli.parser import build_parser


class GreeterService:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"


def test_cli_di_future_annotations_resolve_correctly():
    cli = CommandRegistry()
    container = DIContainer()
    container.register(GreeterService, GreeterService())

    @cli.register(name="hello")
    def hello(name: str, service: GreeterService) -> str:
        return service.greet(name)

    parser = build_parser(cli, container)
    parsed = parser.parse_args(["hello", "Alice"])

    assert parsed.command == "hello"
    assert parsed.name == "Alice"
    assert not hasattr(parsed, "service")

    dispatcher = Dispatcher(cli, container)
    result = dispatcher.dispatch(parsed.command, {"name": parsed.name})
    assert result == "Hello, Alice!"
