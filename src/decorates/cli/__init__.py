"""
A lightweight, decorates-based CLI decorates.

Public API surface::

    from decorates.cli import (
        CommandRegistry,
        DIContainer,
        MiddlewareChain,
        Dispatcher,
        CommandExecutionError,
        build_parser,
        load_plugins,
        logging_middleware_pre,
        logging_middleware_post,
    )
"""

from decorates.cli.dispatcher import Dispatcher
from decorates.cli.middleware import (
    MiddlewareChain,
    logging_middleware_post,
    logging_middleware_pre,
)
from decorates.cli.parser import build_parser
from decorates.cli.container import DIContainer
from decorates.cli.exceptions import (
    CommandExecutionError,
    DependencyNotFoundError,
    DuplicateCommandError,
    FrameworkError,
    PluginLoadError,
    UnknownCommandError,
)
from decorates.cli.registry import CommandRegistry
from decorates.cli.plugins import load_plugins

__all__ = [
    # Core decorates
    "CommandRegistry",
    "DIContainer",
    "Dispatcher",
    "MiddlewareChain",
    "build_parser",
    "load_plugins",
    "logging_middleware_pre",
    "logging_middleware_post",

    # Exceptions
    "CommandExecutionError",
    "DependencyNotFoundError",
    "DuplicateCommandError",
    "FrameworkError",
    "PluginLoadError",
    "UnknownCommandError",
]
