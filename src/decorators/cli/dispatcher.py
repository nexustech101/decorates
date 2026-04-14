"""
Resolves a parsed command name → handler, injects dependencies from the
DI container, runs middleware hooks, and executes the handler.

The dispatcher bridges the parser output (a flat ``argparse.Namespace``)
and the handler's actual signature — separating CLI arguments from
injected services.
"""

from __future__ import annotations

import inspect
from typing import Any

from decorators.cli.exceptions import DependencyNotFoundError
from decorators.cli.middleware import MiddlewareChain
from decorators.cli.container import DIContainer
from decorators.cli.registry import CommandRegistry
from decorators.cli.utils.reflection import get_params


class Dispatcher:
    """
    Executes commands by combining CLI arguments and DI-resolved
    dependencies.

    Args:
        registry:    The command registry to dispatch against.
        container:   DI container for service injection.
        middleware:  Optional middleware chain (pre/post hooks).
    """

    def __init__(
        self,
        registry: CommandRegistry,
        container: DIContainer,
        middleware: MiddlewareChain | None = None,
    ) -> None:
        self._registry = registry
        self._container = container
        self._middleware = middleware or MiddlewareChain()

    def dispatch(self, command: str, cli_args: dict[str, Any]) -> Any:
        """
        Execute the handler for *command*.

        Steps:
        1. Look up the handler in the registry.
        2. Inspect the handler's signature.
        3. Fill parameters from *cli_args* first, then DI container.
        4. Run pre-hooks → call handler → run post-hooks.

        Args:
            command:   The subcommand name (e.g. ``"create-user"``).
            cli_args:  Flat dict of values from the argparse namespace.

        Returns:
            Whatever the handler returns.

        Raises:
            UnknownCommandError:    If *command* is not registered.
            DependencyNotFoundError: If a required dependency is missing.
        """
        entry = self._registry.get(command)
        handler = entry.handler

        kwargs = self._resolve_kwargs(handler, cli_args)

        self._middleware.run_pre(command, kwargs)
        result = handler(**kwargs)
        self._middleware.run_post(command, result)

        return result

    def _resolve_kwargs(
        self, handler: Any, cli_args: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Build the kwargs dict for *handler* by combining CLI args and
        DI-resolved services.

        Priority: CLI args > DI container.
        Parameters whose type is registered in the container are treated
        as service dependencies and never expected on the CLI.
        """
        kwargs: dict[str, Any] = {}

        for param in get_params(handler):
            annotation = param.annotation

            is_service = (
                annotation is not inspect.Parameter.empty
                and isinstance(annotation, type)
                and self._container.has(annotation)
            )

            if param.name in cli_args:
                kwargs[param.name] = cli_args[param.name]
            elif is_service:
                kwargs[param.name] = self._container.resolve(annotation)
            elif param.has_default:
                kwargs[param.name] = param.default
            else:
                raise DependencyNotFoundError(annotation)

        return kwargs
