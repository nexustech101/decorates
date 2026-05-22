"""
Public module-level decorators for ``registers.cli``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from registers.cli.registry import CommandRegistry, MISSING

_default_registry = CommandRegistry()
_active_registry: ContextVar[CommandRegistry | None] = ContextVar(
    "registers.cli.active_registry",
    default=None,
)


def _resolve_registry() -> CommandRegistry:
    registry = _active_registry.get()
    return _default_registry if registry is None else registry


@contextmanager
def use_registry(registry: CommandRegistry):
    """
    Temporarily route module-level decorators to ``registry``.

    This enables plugin/discovery imports to register commands into an explicit
    instance while preserving default module-level behavior outside the context.
    """
    token = _active_registry.set(registry)
    try:
        yield registry
    finally:
        _active_registry.reset(token)


def argument(
    name: str,
    *,
    type: Any = str,
    help: str = "",
    default: Any = MISSING,
    prompt: bool = False,
    secret: bool = False,
    confirm: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare an argument spec for a command function."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _resolve_registry().stage_argument(
            fn,
            name,
            arg_type=type,
            help_text=help,
            default=default,
            prompt=prompt,
            secret=secret,
            confirm=confirm,
        )
        return fn

    return decorator


def prompt(
    name: str,
    question: str,
    *,
    type: Any = str,
    help: str = "",
    default: Any = MISSING,
    secret: bool = False,
    confirm: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a prompted argument with a custom question."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _resolve_registry().stage_argument(
            fn,
            name,
            arg_type=type,
            help_text=help,
            default=default,
            prompt=True,
            prompt_text=question,
            secret=secret,
            confirm=confirm,
        )
        return fn

    return decorator


def option(
    flag: str,
    *,
    help: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a command option token, for example ``--add`` or ``-a``."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _resolve_registry().stage_option(fn, flag, help_text=help)
        return fn

    return decorator


def alias(
    flag: str,
    *,
    help: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Alias decorator for `@option(...)` -> `@alias(...)`."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _resolve_registry().stage_alias(fn, flag, help_text=help)
        return fn

    return decorator


def register(
    name: str | None = None,
    *,
    description: str = "",
    help: str = "",
    tags: Sequence[str] = (),
    examples: Sequence[str] = (),
    deprecated: bool = False,
    render: bool = True,
    default_output: str | None = None,
    pager: bool = False,
    error_hints: dict[str, str] | None = None,
    capture_logs: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Finalize a staged command and register it in the default registry."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        registry = _resolve_registry()
        config = registry._config_for(fn)
        config.tags = tuple(tags)
        config.examples = tuple(examples)
        config.deprecated = deprecated
        config.render = render
        config.default_output = default_output
        config.pager = pager
        config.error_hints = dict(error_hints or {})
        config.capture_logs = capture_logs
        registry.finalize_command(
            fn,
            name=name,
            description=description,
            help_text=help,
        )
        return fn

    return decorator


def run(
    argv: Sequence[str] | None = None,
    *,
    print_result: bool = True,
    shell_prompt: str = "> ",
    shell_input_fn: Callable[[str], str] | None = None,
    shell_banner: bool = True,
    shell_banner_text: str | None = None,
    shell_title: str = "Decorates CLI",
    shell_description: str = "Type 'help' for shell help and 'exit' to quit.",
    shell_version: str | None = None,
    shell_colors: bool | None = None,
    shell_usage: bool = False,
    output: str | None = None,
    quiet: bool = False,
    no_color: bool = False,
    completion: bool = False,
    history: bool = False,
    multiline: bool = False,
    log_level: str | int | None = None,
    event_loop: Any | None = None,
) -> Any:
    """Run the module-level default registry."""

    return _default_registry.run(
        argv,
        print_result=print_result,
        shell_prompt=shell_prompt,
        shell_input_fn=shell_input_fn,
        shell_banner=shell_banner,
        shell_banner_text=shell_banner_text,
        shell_title=shell_title,
        shell_description=shell_description,
        shell_version=shell_version,
        shell_colors=shell_colors,
        shell_usage=shell_usage,
        output=output,
        quiet=quiet,
        no_color=no_color,
        completion=completion,
        history=history,
        multiline=multiline,
        log_level=log_level,
        event_loop=event_loop,
    )


async def run_async(
    argv: Sequence[str] | None = None,
    *,
    print_result: bool = True,
    **kwargs: Any,
) -> Any:
    """Run the module-level default registry from an async runtime."""
    return await _default_registry.run_async(argv, print_result=print_result, **kwargs)


def run_shell(
    *,
    print_result: bool = True,
    prompt: str = "> ",
    program_name: str | None = None,
    input_fn: Callable[[str], str] | None = None,
    banner: bool = True,
    banner_text: str | None = None,
    shell_title: str = "Decorates CLI",
    shell_description: str = "Type 'help' for shell help and 'exit' to quit.",
    shell_version: str | None = None,
    colors: bool | None = None,
    shell_usage: bool = False,
    output: str | None = None,
    quiet: bool = False,
    no_color: bool = False,
    completion: bool = False,
    history: bool = False,
    multiline: bool = False,
    log_level: str | int | None = None,
) -> None:
    """Run the module-level default registry in interactive mode."""

    return _default_registry.run_shell(
        print_result=print_result,
        prompt=prompt,
        program_name=program_name,
        input_fn=input_fn,
        banner=banner,
        banner_text=banner_text,
        shell_title=shell_title,
        shell_description=shell_description,
        shell_version=shell_version,
        colors=colors,
        shell_usage=shell_usage,
        output=output,
        quiet=quiet,
        no_color=no_color,
        completion=completion,
        history=history,
        multiline=multiline,
        log_level=log_level,
    )


def list_commands() -> None:
    """Print commands registered on the module-level default registry."""

    _default_registry.list_commands()


def get_registry() -> CommandRegistry:
    """Return the active registry in context, else the module default."""

    return _resolve_registry()


def reset_registry() -> None:
    """Clear the module-level default registry (useful for tests)."""

    _default_registry.clear()


def group(
    name: str,
    *,
    description: str = "",
    aliases: Sequence[str] = (),
    tags: Sequence[str] = (),
):
    """Create a grouped command facade on the active registry."""
    return _resolve_registry().group(name, description=description, aliases=aliases, tags=tags)


def confirm(message: str, *, danger: bool = False, confirm_phrase: str | None = None):
    """Require confirmation before a command runs."""
    return _resolve_registry().confirm(message, danger=danger, confirm_phrase=confirm_phrase)


def dry_run():
    """Add a ``--dry-run`` flag to a command."""
    return _resolve_registry().dry_run()


def context_factory(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register a context factory on the active registry."""
    return _resolve_registry().context_factory(fn)
