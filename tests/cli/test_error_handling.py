from __future__ import annotations

import logging

import pytest

from decorates.cli import CommandRegistry, CommandExecutionError, DependencyNotFoundError, Dispatcher, DIContainer


class Service:
    pass


class TestCliErrorHandling:
    def test_run_wraps_unhandled_handler_exception(self):
        reg = CommandRegistry()

        @reg.register(name="boom")
        def boom() -> None:
            raise RuntimeError("boom")

        with pytest.raises(CommandExecutionError, match="Command 'boom' failed: boom"):
            reg.run(["boom"], print_result=False)

    def test_run_preserves_decorates_errors(self):
        reg = CommandRegistry()

        @reg.register(name="needs_dep")
        def needs_dep(service: Service):
            return service

        with pytest.raises(DependencyNotFoundError):
            reg.run(["needs_dep"], print_result=False)

    def test_dispatch_logs_unhandled_exception(self, caplog):
        caplog.set_level(logging.ERROR)

        reg = CommandRegistry()

        @reg.register(name="explode")
        def explode() -> None:
            raise ValueError("kaboom")

        dispatcher = Dispatcher(reg, DIContainer())

        with pytest.raises(ValueError):
            dispatcher.dispatch("explode", {})

        assert "Unhandled exception during command 'explode' execution." in caplog.text
