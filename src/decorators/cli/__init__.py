"""
A lightweight, decorators-based CLI framework.

Public API surface::

    from decorators.cli import (
        CommandRegistry,
        DIContainer,
        MiddlewareChain,
        Dispatcher,
        build_parser,
        load_plugins,
        logging_middleware_pre,
        logging_middleware_post,
    )
"""

from decorators.cli.dispatcher import Dispatcher
from decorators.cli.middleware import (
    MiddlewareChain,
    logging_middleware_post,
    logging_middleware_pre,
)
from decorators.cli.parser import build_parser
from decorators.cli.container import DIContainer
from decorators.cli.exceptions import (
    DependencyNotFoundError,
    DuplicateCommandError,
    FrameworkError,
    PluginLoadError,
    UnknownCommandError,
)
from decorators.cli.registry import CommandRegistry
from decorators.cli.plugins import load_plugins

__all__ = [
    # Core framework
    "CommandRegistry",
    "DIContainer",
    "Dispatcher",
    "MiddlewareChain",
    "build_parser",
    "load_plugins",
    "logging_middleware_pre",
    "logging_middleware_post",

    # Exceptions
    "DependencyNotFoundError",
    "DuplicateCommandError",
    "FrameworkError",
    "PluginLoadError",
    "UnknownCommandError",
]
