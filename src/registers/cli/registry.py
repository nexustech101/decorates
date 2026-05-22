"""
Internal command registry for the module-level CLI decorators.

The public DX entrypoints live in ``registers.cli.decorators``. This module
stores command specs and executes commands from those specs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import logging
from pathlib import Path
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Sequence

from registers.cli.exceptions import CommandExecutionError, DuplicateCommandError, RegistrationError, UnknownCommandError
from registers.cli.ux import Context, capture_logs, format_error, print_result as render_print_result, run_awaitable
from registers.core.logging import log_exception
from registers.cli.utils.reflection import get_params
from registers.cli.utils.typing import is_bool_flag
from registers.cli.utils import registry_utils

logger = logging.getLogger(__name__)
HELP_COMMAND_NAME = registry_utils.HELP_COMMAND_NAME
HELP_ALIASES = registry_utils.HELP_ALIASES
HELP_RESERVED = frozenset({"help", "h"})
INTERACTIVE_ALIASES = ("--interactive", "-i")
INTERACTIVE_RESERVED = frozenset({"interactive", "i"})


class _MissingType:
    def __repr__(self) -> str:
        return "MISSING"


MISSING = _MissingType()


@dataclass(frozen=True)
class ArgumentEntry:
    """Typed metadata for one command argument."""

    name: str
    type: Any = str
    help_text: str = ""
    required: bool = True
    default: Any = MISSING
    prompt: bool = False
    prompt_text: str = ""
    secret: bool = False
    confirm: bool = False


@dataclass(frozen=True)
class CommandEntry:
    """All metadata needed to parse and execute a command."""

    name: str
    handler: Callable[..., Any]
    help_text: str = ""
    description: str = ""
    options: tuple[str, ...] = field(default_factory=tuple)
    # aliases: tuple[str, ...] = field(default_factory=tuple)
    arguments: tuple[ArgumentEntry, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    examples: tuple[str, ...] = field(default_factory=tuple)
    deprecated: bool = False
    render: bool = True
    default_output: str | None = None
    pager: bool = False
    error_hints: dict[str, str] = field(default_factory=dict)
    capture_logs: bool = False
    confirm_message: str | None = None
    confirm_danger: bool = False
    confirm_phrase: str | None = None


@dataclass(frozen=True)
class _StagedArgument:
    name: str
    arg_type: Any = str
    help_text: str = ""
    default: Any = MISSING
    prompt: bool = False
    prompt_text: str = ""
    secret: bool = False
    confirm: bool = False


@dataclass(frozen=True)
class _StagedOption:
    flag: str
    help_text: str = ""


@dataclass
class _CommandConfig:
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    deprecated: bool = False
    render: bool = True
    default_output: str | None = None
    pager: bool = False
    error_hints: dict[str, str] = field(default_factory=dict)
    capture_logs: bool = False
    confirm_message: str | None = None
    confirm_danger: bool = False
    confirm_phrase: str | None = None
    dry_run: bool = False


class CommandRegistry:
    """Internal state container for staged decorators and finalized commands."""

    if TYPE_CHECKING:
        # IDE/type-checker surface for instance-level decorator usage.
        # Runtime behavior is still provided dynamically via __getattr__.
        def argument(
            self,
            name: str,
            *,
            type: Any = str,
            help: str = "",
            default: Any = MISSING,
            prompt: bool = False,
            secret: bool = False,
            confirm: bool = False,
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

        def prompt(
            self,
            name: str,
            question: str,
            *,
            type: Any = str,
            help: str = "",
            default: Any = MISSING,
            secret: bool = False,
            confirm: bool = False,
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

        def option(
            self,
            flag: str,
            *,
            help: str = "",
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

        def alias(
            self,
            flag: str,
            *,
            help: str = "",
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

        def register(
            self,
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
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

        def register_plugin(self, plugin: Any) -> int: ...

    def __init__(self) -> None:
        self._commands: dict[str, CommandEntry] = {}
        self._aliases: dict[str, str] = {}
        self._groups: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._pending_args: dict[Callable[..., Any], list[_StagedArgument]] = {}
        self._pending_options: dict[Callable[..., Any], list[_StagedOption]] = {}
        self._pending_config: dict[Callable[..., Any], _CommandConfig] = {}
        self._context_factory: Callable[..., Any] | None = None

    def __getattr__(self, name: str) -> Any:
        """
        Backward-compatible instance facade for decorator-first registration.

        Notes:
        - We intentionally expose these on *instances* only so legacy checks
          like ``hasattr(CommandRegistry, "register")`` keep their behavior.
        - Usage:
              registry = CommandRegistry()
              @registry.register(...)
              @registry.argument(...)
              @registry.option(...)
              @registry.alias(...)
              def command(...): ...
        """
        if name == "argument":
            return self._decorator_argument
        if name == "prompt":
            return self._decorator_prompt
        if name == "option":
            return self._decorator_option
        if name == "alias":
            return self._decorator_alias
        if name == "register":
            return self._decorator_register
        if name == "group":
            return self.group
        if name == "confirm":
            return self.confirm
        if name == "dry_run":
            return self.dry_run
        if name == "context_factory":
            return self.context_factory
        raise AttributeError(f"{type(self).__name__!s} object has no attribute {name!r}")

    # ------------------------------------------------------------------
    # Decorator staging + finalization
    # ------------------------------------------------------------------

    def argument(
        self,
        name: str,
        *,
        type: Any = str,
        help: str = "",
        default: Any = MISSING,
        prompt: bool = False,
        secret: bool = False,
        confirm: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declare an argument spec for commands registered on this registry."""
        return self._decorator_argument(
            name,
            type=type,
            help=help,
            default=default,
            prompt=prompt,
            secret=secret,
            confirm=confirm,
        )

    def prompt(
        self,
        name: str,
        question: str,
        *,
        type: Any = str,
        help: str = "",
        default: Any = MISSING,
        secret: bool = False,
        confirm: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Prompt for a missing argument with a custom question."""
        return self._decorator_prompt(
            name,
            question,
            type=type,
            help=help,
            default=default,
            secret=secret,
            confirm=confirm,
        )

    def option(
        self,
        flag: str,
        *,
        help: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declare a command option token, for example ``--add`` or ``-a``."""
        return self._decorator_option(flag, help=help)

    def alias(
        self,
        flag: str,
        *,
        help: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declare an alias token for a command registered on this registry."""
        return self._decorator_alias(flag, help=help)

    def register(
        self,
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
        """Finalize a staged command and register it on this registry."""
        return self._decorator_register(
            name,
            description=description,
            help=help,
            tags=tags,
            examples=examples,
            deprecated=deprecated,
            render=render,
            default_output=default_output,
            pager=pager,
            error_hints=error_hints,
            capture_logs=capture_logs,
        )

    def _decorator_argument(
        self,
        name: str,
        *,
        type: Any = str,
        help: str = "",
        default: Any = MISSING,
        prompt: bool = False,
        secret: bool = False,
        confirm: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Instance-level decorator alias for ``stage_argument(...)``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.stage_argument(
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

    def _decorator_prompt(
        self,
        name: str,
        question: str,
        *,
        type: Any = str,
        help: str = "",
        default: Any = MISSING,
        secret: bool = False,
        confirm: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Instance-level decorator alias for prompted arguments."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.stage_argument(
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

    def _decorator_option(
        self,
        flag: str,
        *,
        help: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Instance-level decorator alias for ``stage_option(...)``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.stage_option(fn, flag, help_text=help)
            return fn

        return decorator
    
    def _decorator_alias(
        self,
        flag: str,
        *,
        help: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Instance-level decorator alias for ``stage_alias(...)``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.stage_alias(fn, flag, help_text=help)
            return fn

        return decorator

    def _decorator_register(
        self,
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
        """Instance-level decorator alias for ``finalize_command(...)``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            config = self._config_for(fn)
            config.tags = tuple(tags)
            config.examples = tuple(examples)
            config.deprecated = deprecated
            config.render = render
            config.default_output = default_output
            config.pager = pager
            config.error_hints = dict(error_hints or {})
            config.capture_logs = capture_logs
            self.finalize_command(
                fn,
                name=name,
                description=description,
                help_text=help,
            )
            return fn

        return decorator

    def stage_argument(
        self,
        fn: Callable[..., Any],
        name: str,
        *,
        arg_type: Any = str,
        help_text: str = "",
        default: Any = MISSING,
        prompt: bool = False,
        prompt_text: str = "",
        secret: bool = False,
        confirm: bool = False,
    ) -> None:
        if not name:
            raise ValueError("argument() requires a non-empty argument name.")

        staged = self._pending_args.setdefault(fn, [])
        if any(item.name == name for item in staged):
            raise ValueError(f"Argument '{name}' was declared more than once for '{fn.__name__}'.")

        # Decorators execute bottom-up; prepend to preserve top-down source order.
        staged.insert(
            0,
            _StagedArgument(
                name=name,
                arg_type=arg_type,
                help_text=help_text,
                default=default,
                prompt=prompt,
                prompt_text=prompt_text,
                secret=secret,
                confirm=confirm,
            ),
        )

    def stage_option(
        self,
        fn: Callable[..., Any],
        flag: str,
        *,
        help_text: str = "",
    ) -> None:
        if not flag or not flag.startswith("-"):
            raise ValueError("option() expects a CLI flag such as '-a' or '--add'.")

        staged = self._pending_options.setdefault(fn, [])
        if any(item.flag == flag for item in staged):
            raise ValueError(f"Option '{flag}' was declared more than once for '{fn.__name__}'.")

        # Decorators execute bottom-up; prepend to preserve top-down source order.
        staged.insert(0, _StagedOption(flag=flag, help_text=help_text))

    def stage_alias(
        self,
        fn: Callable[..., Any],
        flag: str,
        *,
        help_text: str = "",
    ) -> None:

        self.stage_option(fn, flag, help_text=help_text)

    def confirm(
        self,
        message: str,
        *,
        danger: bool = False,
        confirm_phrase: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Require confirmation before executing a command."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            config = self._config_for(fn)
            config.confirm_message = message
            config.confirm_danger = danger
            config.confirm_phrase = confirm_phrase
            return fn

        return decorator

    def dry_run(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Add a ``--dry-run`` boolean argument to the command."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._config_for(fn).dry_run = True
            return fn

        return decorator

    def context_factory(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a factory that builds a run/shell-scoped context object."""
        self._context_factory = fn
        return fn

    def group(
        self,
        name: str,
        *,
        description: str = "",
        aliases: Sequence[str] = (),
        tags: Sequence[str] = (),
    ) -> "CommandGroup":
        """Create a grouped command facade, for example ``users list``."""
        self._groups[name] = (description, tuple(tags))
        return CommandGroup(
            self,
            path=(name,),
            alias_paths=tuple((alias,) for alias in aliases),
            description=description,
            tags=tuple(tags),
        )

    def finalize_command(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str = "",
        help_text: str = "",
        register_option_aliases: bool = True,
    ) -> None:
        staged_args = self._pending_args.pop(fn, [])
        staged_options = self._pending_options.pop(fn, [])
        config = self._pending_config.pop(fn, _CommandConfig())

        options = tuple(item.flag for item in staged_options)
        command_name = (name or "").strip() or self._derive_command_name(options, fn.__name__)
        if not command_name:
            raise ValueError("register() could not determine a command name.")

        summary = description or help_text
        arguments_list = self._build_arguments(fn, staged_args)
        if config.dry_run:
            dry_run_arg = ArgumentEntry(
                name="dry_run",
                type=bool,
                help_text="Preview the command without applying changes.",
                required=False,
                default=False,
            )
            dry_run_index = next((idx for idx, arg in enumerate(arguments_list) if arg.name == "dry_run"), None)
            if dry_run_index is None:
                arguments_list.append(dry_run_arg)
            else:
                arguments_list[dry_run_index] = dry_run_arg
        arguments = tuple(arguments_list)

        self._assert_command_slot_available(command_name)
        if register_option_aliases:
            self._assert_options_available(command_name, options)

        entry = CommandEntry(
            name=command_name,
            handler=fn,
            help_text=summary,
            description=description,
            options=options,
            arguments=arguments,
            tags=config.tags,
            examples=config.examples,
            deprecated=config.deprecated,
            render=config.render,
            default_output=config.default_output,
            pager=config.pager,
            error_hints=config.error_hints,
            capture_logs=config.capture_logs,
            confirm_message=config.confirm_message,
            confirm_danger=config.confirm_danger,
            confirm_phrase=config.confirm_phrase,
        )

        self._commands[command_name] = entry
        if register_option_aliases:
            for flag in options:
                normalized = self._normalize_alias(flag)
                if normalized:
                    self._aliases[normalized] = command_name

    def _config_for(self, fn: Callable[..., Any]) -> _CommandConfig:
        return self._pending_config.setdefault(fn, _CommandConfig())

    # ------------------------------------------------------------------
    # Lookup + runtime
    # ------------------------------------------------------------------

    def get(self, name: str) -> CommandEntry:
        if name in self._commands:
            return self._commands[name]

        normalized = self._normalize_alias(name)
        if normalized in self._aliases:
            return self._commands[self._aliases[normalized]]

        raise UnknownCommandError(name)

    def all(self) -> dict[str, CommandEntry]:
        return dict(self._commands)

    def has(self, name: str) -> bool:
        try:
            self.get(name)
            return True
        except UnknownCommandError:
            return False

    def list_commands(self) -> None:
        if not self._commands:
            print("No commands registered.")
            return

        print("Available commands:")
        for entry in self._commands.values():
            aliases = f" [{', '.join(entry.options)}]" if entry.options else ""
            summary = entry.help_text or entry.description or "(no description)"
            print(f"  {entry.name}{aliases}: {summary}")

    def print_help(
        self,
        command_name: str | None = None,
        *,
        program_name: str | None = None,
        shell_title: str = "Registers CLI",
        shell_description: str = "Type 'help' for shell help and 'exit' to quit.",
        shell_version: str | None = None,
        colors: bool | None = None,
        tag: str | None = None,
    ) -> None:
        """Print comprehensive CLI help for all commands or one specific command."""
        use_color = self._supports_color(colors)
        if command_name is None:
            print(
                self._render_global_help(
                    program_name=program_name,
                    shell_title=shell_title,
                    shell_description=shell_description,
                    shell_version=shell_version,
                    use_color=use_color,
                    tag=tag,
                )
            )
            return

        normalized = self._normalize_alias(command_name)
        if normalized in HELP_RESERVED:
            print(self._render_builtin_help_detail(HELP_COMMAND_NAME, program_name=program_name, use_color=use_color))
            return
        if normalized in INTERACTIVE_RESERVED:
            print(self._render_builtin_help_detail("interactive", program_name=program_name, use_color=use_color))
            return

        try:
            entry = self.get(command_name)
        except UnknownCommandError:
            if self._has_group(command_name):
                print(self._render_group_help(command_name, program_name=program_name, use_color=use_color))
                return
            raise
        print(self._render_command_help(entry, program_name=program_name, use_color=use_color))

    def run(
        self,
        argv: Sequence[str] | None = None,
        *,
        print_result: bool = True,
        shell_prompt: str = "> ",
        shell_input_fn: Callable[[str], str] | None = None,
        shell_banner: bool = True,
        shell_banner_text: str | None = None,
        shell_title: str = "Registers CLI",
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
        from registers.cli.parser import ParseError, parse_command_args, render_command_usage

        program_name = Path(sys.argv[0]).name or "app.py"
        raw = list(sys.argv[1:] if argv is None else argv)
        raw, context_kwargs = self._strip_context_args(raw)
        if not raw:
            if self._stdin_is_interactive():
                context = self._build_context(context_kwargs)
                return self.run_shell(
                    print_result=print_result,
                    prompt=shell_prompt,
                    program_name=program_name,
                    input_fn=shell_input_fn,
                    banner=shell_banner,
                    banner_text=shell_banner_text,
                    shell_title=shell_title,
                    shell_description=shell_description,
                    shell_version=shell_version,
                    colors=False if no_color else shell_colors,
                    shell_usage=shell_usage,
                    output=output,
                    quiet=quiet,
                    no_color=no_color,
                    completion=completion,
                    history=history,
                    multiline=multiline,
                    log_level=log_level,
                    context=context,
                )
            self.print_help(
                program_name=program_name,
                shell_title=shell_title,
                shell_description=shell_description,
                shell_version=shell_version,
                colors=False if no_color else shell_colors,
            )
            return None

        token = raw[0]
        if token in INTERACTIVE_ALIASES:
            if len(raw) > 1:
                print(f"Error: {token} does not take additional arguments.")
                raise SystemExit(2)
            context = self._build_context(context_kwargs)
            return self.run_shell(
                print_result=print_result,
                prompt=shell_prompt,
                program_name=program_name,
                input_fn=shell_input_fn,
                banner=shell_banner,
                banner_text=shell_banner_text,
                shell_title=shell_title,
                shell_description=shell_description,
                shell_version=shell_version,
                colors=False if no_color else shell_colors,
                shell_usage=shell_usage,
                output=output,
                quiet=quiet,
                no_color=no_color,
                completion=completion,
                history=history,
                multiline=multiline,
                log_level=log_level,
                context=context,
            )

        if self._is_builtin_help_token(token):
            if len(raw) >= 2:
                target = " ".join(raw[1:])
                try:
                    self.print_help(
                        target,
                        program_name=program_name,
                        shell_title=shell_title,
                        shell_description=shell_description,
                        shell_version=shell_version,
                        colors=False if no_color else shell_colors,
                    )
                except UnknownCommandError:
                    suggestion = self.suggest(target)
                    if suggestion:
                        print(f"Did you mean '{suggestion}'?")
                    else:
                        print(f"Unknown command '{target}'.")
                    raise SystemExit(2)
            else:
                self.print_help(
                    program_name=program_name,
                    shell_title=shell_title,
                    shell_description=shell_description,
                    shell_version=shell_version,
                    colors=False if no_color else shell_colors,
                )
            return None

        try:
            entry, command_tokens, command_args = self._resolve_command_tokens(raw)
        except UnknownCommandError:
            suggestion = self.suggest(token)
            if suggestion:
                print(f"Did you mean '{suggestion}'?")
            else:
                print("Unknown command")
            raise SystemExit(2)

        if any(arg in {"--help", "-h"} for arg in command_args):
            self.print_help(
                entry.name,
                program_name=program_name,
                shell_title=shell_title,
                shell_description=shell_description,
                shell_version=shell_version,
                colors=False if no_color else shell_colors,
            )
            return None

        try:
            command_args, runtime_options = self._strip_runtime_options(entry, command_args)
        except ParseError as exc:
            print(format_error("Parse Error", str(exc)))
            print(render_command_usage(entry, program_name=program_name))
            raise SystemExit(2)
        final_output = runtime_options.get("output", output or entry.default_output)
        quiet = bool(runtime_options.get("quiet", quiet))
        no_color = bool(runtime_options.get("no_color", no_color))
        force = bool(runtime_options.get("force", False))

        try:
            kwargs = parse_command_args(entry, command_args, allow_missing_prompts=True)
        except ParseError as exc:
            print(format_error("Parse Error", str(exc)))
            print(render_command_usage(entry, program_name=program_name))
            raise SystemExit(2)

        try:
            kwargs = self._prompt_missing(entry, kwargs, input_fn=shell_input_fn)
        except ParseError as exc:
            print(format_error("Parse Error", str(exc)))
            print(render_command_usage(entry, program_name=program_name))
            raise SystemExit(2)

        try:
            context = self._build_context(context_kwargs)
            result = self._execute_entry(
                entry,
                kwargs,
                context=context,
                force=force,
                input_fn=shell_input_fn,
                log_level=log_level,
                event_loop=event_loop,
            )
        except ParseError as exc:
            print(format_error("Parse Error", str(exc)))
            print(render_command_usage(entry, program_name=program_name))
            raise SystemExit(2)
        except RegistrationError:
            raise
        except Exception as exc:
            log_exception(
                logger,
                logging.ERROR,
                "Unhandled command failure in run().",
                error=exc,
                command=entry.name,
            )
            raise CommandExecutionError(entry.name, str(exc)) from exc

        if print_result and result is not None and not quiet:
            render_print_result(result, output=final_output, render=entry.render)

        return result

    async def run_async(
        self,
        argv: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Async-friendly variant of :meth:`run`."""
        from registers.cli.parser import ParseError, parse_command_args, render_command_usage
        from registers.cli.ux import await_if_needed

        print_result = kwargs.pop("print_result", True)
        raw = list(sys.argv[1:] if argv is None else argv)
        raw, context_kwargs = self._strip_context_args(raw)
        if not raw:
            self.print_help()
            return None
        if self._is_builtin_help_token(raw[0]):
            self.print_help(raw[1] if len(raw) > 1 else None)
            return None
        try:
            entry, _command_tokens, command_args = self._resolve_command_tokens(raw)
            if any(arg in {"--help", "-h"} for arg in command_args):
                self.print_help(entry.name)
                return None
            command_args, runtime_options = self._strip_runtime_options(entry, command_args)
            parsed = parse_command_args(entry, command_args, allow_missing_prompts=True)
            parsed = self._prompt_missing(entry, parsed, input_fn=kwargs.get("shell_input_fn"))
            context = self._build_context(context_kwargs)
            call_kwargs = self._build_call_kwargs(entry, parsed, context=context)
            if entry.confirm_message and not runtime_options.get("force"):
                self._confirm_command(entry, parsed, input_fn=kwargs.get("shell_input_fn"))
            result = entry.handler(**call_kwargs)
            result = await await_if_needed(result)
        except ParseError as exc:
            print(f"Error: {exc}")
            if "entry" in locals():
                print(render_command_usage(entry))
            raise SystemExit(2)
        if print_result and result is not None and not runtime_options.get("quiet", False):
            render_print_result(
                result,
                output=runtime_options.get("output", kwargs.get("output") or entry.default_output),
                render=entry.render,
            )
        return result

    def run_shell(
        self,
        *,
        print_result: bool = True,
        prompt: str = "> ",
        program_name: str | None = None,
        input_fn: Callable[[str], str] | None = None,
        banner: bool = True,
        banner_text: str | None = None,
        shell_title: str = "Registers CLI",
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
        context: Any | None = None,
    ) -> None:
        """Run this registry in interactive REPL mode."""
        from registers.cli.shell import InteractiveShell

        shell = InteractiveShell(
            self,
            print_result=print_result,
            prompt=prompt,
            program_name=program_name,
            input_fn=input_fn,
            banner=banner,
            title=shell_title,
            banner_text=banner_text,
            description=shell_description,
            version_text=shell_version,
            colors=colors,
            usage=shell_usage,
            output=output,
            quiet=quiet,
            no_color=no_color,
            completion=completion,
            history=history,
            multiline=multiline,
            log_level=log_level,
            context=context,
        )
        shell.run()
        return None

    def clear(self) -> None:
        self._commands.clear()
        self._aliases.clear()
        self._groups.clear()
        self._pending_args.clear()
        self._pending_options.clear()
        self._pending_config.clear()
        self._context_factory = None

    def get_registry(self) -> CommandRegistry:
        """Return this registry instance (instance-mode compatibility helper)."""
        return self

    def reset_registry(self) -> None:
        """Clear this registry (instance-mode compatibility helper)."""
        self.clear()

    def load_plugins(self, package_path: str) -> list[Any]:
        """Load plugin modules into this registry instance."""
        from registers.cli.plugins import load_plugins

        return load_plugins(package_path, self)

    def register_plugin(self, plugin: Any) -> int:
        """
        Merge commands from another plugin registry into this registry.

        Supported plugin values:
        - ``CommandRegistry`` instance
        - any object exposing ``get_registry() -> CommandRegistry``
        - module object with a ``cli`` attribute that is a ``CommandRegistry``

        Returns:
            Number of commands merged into this registry.
        """
        plugin_registry = self._resolve_plugin_registry(plugin)
        if plugin_registry is self:
            return 0

        for group_name, metadata in plugin_registry._groups.items():
            self._groups.setdefault(group_name, metadata)

        added = 0
        for entry in plugin_registry.all().values():
            self._assert_command_slot_available(entry.name)
            if " " not in entry.name:
                self._assert_options_available(entry.name, entry.options)
            self._commands[entry.name] = entry
            if " " not in entry.name:
                for flag in entry.options:
                    normalized = self._normalize_alias(flag)
                    if normalized:
                        self._aliases[normalized] = entry.name
            added += 1
        for alias, target in plugin_registry._aliases.items():
            if target not in self._commands:
                continue
            if alias in self._commands and alias != target:
                raise DuplicateCommandError(alias)
            existing = self._aliases.get(alias)
            if existing is not None and existing != target:
                raise DuplicateCommandError(alias)
            self._aliases[alias] = target
        return added

    def _resolve_command_tokens(self, raw: Sequence[str]) -> tuple[CommandEntry, list[str], list[str]]:
        for size in range(len(raw), 0, -1):
            candidate = " ".join(raw[:size])
            try:
                return self.get(candidate), list(raw[:size]), list(raw[size:])
            except UnknownCommandError:
                continue
        raise UnknownCommandError(raw[0] if raw else "")

    def _strip_runtime_options(
        self,
        entry: CommandEntry,
        tokens: Sequence[str],
    ) -> tuple[list[str], dict[str, Any]]:
        from registers.cli.parser import named_argument_flags

        command_flags = named_argument_flags(entry.arguments)
        runtime: dict[str, Any] = {}
        remaining: list[str] = []
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token in {"--cli-output"} or (token == "--output" and token not in command_flags):
                idx += 1
                if idx >= len(tokens):
                    from registers.cli.parser import ParseError
                    raise ParseError(f"Missing value for option '{token}'.")
                runtime["output"] = tokens[idx]
                idx += 1
                continue
            if token in {"--cli-quiet"} or (token == "--quiet" and token not in command_flags):
                runtime["quiet"] = True
                idx += 1
                continue
            if token in {"--cli-no-color"} or (token == "--no-color" and token not in command_flags):
                runtime["no_color"] = True
                idx += 1
                continue
            if entry.confirm_message and token == "--force" and token not in command_flags:
                runtime["force"] = True
                idx += 1
                continue
            remaining.append(token)
            idx += 1
        return remaining, runtime

    def _strip_context_args(self, raw: list[str]) -> tuple[list[str], dict[str, Any]]:
        if self._context_factory is None:
            return raw, {}
        params = {param.name: param for param in get_params(self._context_factory)}
        context: dict[str, Any] = {}
        remaining: list[str] = []
        idx = 0
        while idx < len(raw):
            token = raw[idx]
            if not token.startswith("--"):
                remaining.extend(raw[idx:])
                break
            name = token[2:].replace("-", "_")
            param = params.get(name)
            if param is None:
                remaining.extend(raw[idx:])
                break
            annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
            if is_bool_flag(annotation):
                context[name] = True
                idx += 1
                continue
            idx += 1
            if idx >= len(raw):
                from registers.cli.parser import ParseError
                raise ParseError(f"Missing value for option '{token}'.")
            from registers.cli.parser import coerce_value
            context[name] = coerce_value(raw[idx], annotation, name)
            idx += 1
        return remaining, context

    def _build_context(self, context_kwargs: dict[str, Any]) -> Any | None:
        if self._context_factory is None:
            return None
        params = get_params(self._context_factory)
        kwargs: dict[str, Any] = {}
        for param in params:
            if param.name in context_kwargs:
                kwargs[param.name] = context_kwargs[param.name]
            elif param.has_default:
                kwargs[param.name] = param.default
        return self._context_factory(**kwargs)

    def _prompt_missing(
        self,
        entry: CommandEntry,
        kwargs: dict[str, Any],
        *,
        input_fn: Callable[[str], str] | None,
    ) -> dict[str, Any]:
        from registers.cli.parser import ParseError, coerce_value

        resolved = dict(kwargs)
        if input_fn is not None:
            reader = input_fn
        else:
            reader = input
        interactive = input_fn is not None or self._stdin_is_interactive()
        for arg in entry.arguments:
            if arg.name in resolved:
                continue
            if not arg.prompt:
                raise ParseError(f"Missing required argument '{arg.name}'.")
            if not interactive:
                raise ParseError(f"Missing required argument '{arg.name}'.")
            prompt = self._render_prompt(arg)
            if arg.secret and input_fn is None:
                import getpass
                raw = getpass.getpass(prompt)
            else:
                raw = reader(prompt)
            raw = self._resolve_prompt_choice(raw, arg)
            if arg.confirm:
                if arg.secret and input_fn is None:
                    import getpass
                    again = getpass.getpass(f"Confirm {arg.name}: ")
                else:
                    again = reader(f"Confirm {arg.name}: ")
                again = self._resolve_prompt_choice(again, arg)
                if raw != again:
                    raise ParseError(f"Confirmation for '{arg.name}' did not match.")
            resolved[arg.name] = coerce_value(raw, arg.type, arg.name)
        return resolved

    def _render_prompt(self, arg: ArgumentEntry) -> str:
        choices = self._prompt_choices(arg.type)
        question = arg.prompt_text or arg.name
        if not choices:
            return f"{question}: "

        lines = [question]
        for idx, choice in enumerate(choices, start=1):
            lines.append(f"  {idx}. {choice}")
        lines.append(f"{arg.name} [1-{len(choices)}]: ")
        return "\n".join(lines)

    @staticmethod
    def _resolve_prompt_choice(raw: str, arg: ArgumentEntry) -> str:
        choices = CommandRegistry._prompt_choices(arg.type)
        if not choices:
            return raw
        value = raw.strip()
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(choices):
                return choices[index - 1]
        return raw

    @staticmethod
    def _prompt_choices(annotation: Any) -> list[str]:
        choices = getattr(annotation, "choices", None)
        if choices:
            return [str(choice) for choice in choices]

        enum_type = getattr(annotation, "enum_type", None)
        if enum_type is not None:
            return [str(member.value) for member in enum_type]

        try:
            from enum import Enum

            if inspect.isclass(annotation) and issubclass(annotation, Enum):
                return [str(member.value) for member in annotation]
        except TypeError:
            return []
        return []

    def _execute_entry(
        self,
        entry: CommandEntry,
        kwargs: dict[str, Any],
        *,
        context: Any | None,
        force: bool,
        input_fn: Callable[[str], str] | None,
        log_level: str | int | None,
        event_loop: Any | None,
    ) -> Any:
        if entry.confirm_message and not force:
            self._confirm_command(entry, kwargs, input_fn=input_fn)

        call_kwargs = self._build_call_kwargs(entry, kwargs, context=context)
        with capture_logs(entry.capture_logs, level=log_level) as logs:
            result = entry.handler(**call_kwargs)
            if inspect.isawaitable(result):
                result = run_awaitable(result, event_loop=event_loop)
            if logs and hasattr(logs, "getvalue"):
                log_text = logs.getvalue().strip()
                if log_text:
                    print(log_text)
        return result

    def _build_call_kwargs(
        self,
        entry: CommandEntry,
        kwargs: dict[str, Any],
        *,
        context: Any | None,
    ) -> dict[str, Any]:
        call_kwargs: dict[str, Any] = {}
        for param in get_params(entry.handler):
            if param.name in kwargs:
                call_kwargs[param.name] = kwargs[param.name]
                continue
            if context is not None and self._param_accepts_context(param.name, param.annotation, context):
                call_kwargs[param.name] = context
                continue
            if param.has_default:
                call_kwargs[param.name] = param.default
        return call_kwargs

    @staticmethod
    def _param_accepts_context(name: str, annotation: Any, context: Any) -> bool:
        if name in {"ctx", "context"}:
            return True
        if annotation is inspect.Parameter.empty:
            return False
        try:
            return isinstance(context, annotation)
        except TypeError:
            return False

    def _confirm_command(
        self,
        entry: CommandEntry,
        kwargs: dict[str, Any],
        *,
        input_fn: Callable[[str], str] | None,
    ) -> None:
        from registers.cli.parser import ParseError

        reader = input_fn or input
        if input_fn is None and not self._stdin_is_interactive():
            raise ParseError("Confirmation required; pass --force or run interactively.")
        message = entry.confirm_message or "Continue?"
        try:
            rendered = message.format(**kwargs)
        except Exception:
            rendered = message
        if entry.confirm_phrase:
            try:
                phrase = entry.confirm_phrase.format(**kwargs)
            except Exception:
                phrase = entry.confirm_phrase
            answer = reader(f"{rendered}\nType {phrase} to confirm: ")
            if answer != phrase:
                raise ParseError("Confirmation phrase did not match.")
            return
        answer = reader(f"{rendered} [y/N]: ")
        if answer.lower() not in {"y", "yes"}:
            raise ParseError("Command was not confirmed.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_alias(token: str) -> str:
        return registry_utils.normalize_alias(token)

    @staticmethod
    def _resolve_plugin_registry(plugin: Any) -> CommandRegistry:
        return registry_utils.resolve_plugin_registry(plugin, CommandRegistry)

    def _assert_command_slot_available(self, command_name: str) -> None:
        if self._normalize_alias(command_name) in HELP_RESERVED:
            raise ValueError(
                "The command name 'help' is reserved for the built-in help command."
            )

        if command_name in self._commands:
            raise DuplicateCommandError(command_name)

        if command_name in self._aliases:
            raise DuplicateCommandError(command_name)

    def _assert_options_available(self, command_name: str, options: Sequence[str]) -> None:
        for flag in options:
            normalized = self._normalize_alias(flag)
            if not normalized:
                raise ValueError(f"Invalid option '{flag}'.")

            if normalized in HELP_RESERVED:
                raise ValueError(
                    f"Option '{flag}' is reserved for the built-in help command."
                )

            if normalized in INTERACTIVE_RESERVED:
                raise ValueError(
                    f"Option '{flag}' is reserved for interactive mode entry."
                )

            if normalized in self._commands and normalized != command_name:
                raise DuplicateCommandError(flag)

            existing = self._aliases.get(normalized)
            if existing is not None and existing != command_name:
                raise DuplicateCommandError(flag)

    def _register_alias_path(self, alias_name: str, target: str) -> None:
        normalized = self._normalize_alias(alias_name)
        if not normalized:
            raise ValueError(f"Invalid alias '{alias_name}'.")
        if normalized in HELP_RESERVED:
            raise ValueError(
                f"Alias '{alias_name}' is reserved for the built-in help command."
            )
        if normalized in INTERACTIVE_RESERVED:
            raise ValueError(
                f"Alias '{alias_name}' is reserved for interactive mode entry."
            )
        if alias_name in self._commands and alias_name != target:
            raise DuplicateCommandError(alias_name)
        existing = self._aliases.get(normalized)
        if existing is not None and existing != target:
            raise DuplicateCommandError(alias_name)
        self._aliases[normalized] = target

    @staticmethod
    def _derive_command_name(options: Sequence[str], fallback: str) -> str:
        return registry_utils.derive_command_name(options, fallback)

    def _build_arguments(
        self,
        fn: Callable[..., Any],
        staged_args: Sequence[_StagedArgument],
    ) -> list[ArgumentEntry]:
        params = get_params(fn)
        params_by_name = {param.name: param for param in params}

        for staged in staged_args:
            if staged.name not in params_by_name:
                raise ValueError(
                    f"@argument('{staged.name}') does not match any parameter on '{fn.__name__}'."
                )

        explicit_by_name = {item.name: item for item in staged_args}
        ordered: list[ArgumentEntry] = []

        # Explicit @argument entries are authoritative and preserve decorator order.
        for staged in staged_args:
            param = params_by_name[staged.name]
            annotation = self._resolve_annotation(staged.arg_type, param.annotation)
            required, default = self._resolve_requirement(
                annotation=annotation,
                param_has_default=param.has_default,
                param_default=param.default,
                explicit_default=staged.default,
            )
            ordered.append(
                ArgumentEntry(
                    name=staged.name,
                    type=annotation,
                    help_text=staged.help_text,
                    required=required,
                    default=default,
                    prompt=staged.prompt,
                    prompt_text=staged.prompt_text,
                    secret=staged.secret,
                    confirm=staged.confirm,
                )
            )

        # Fallback for undeclared params uses function signature inference.
        for param in params:
            if param.name in explicit_by_name:
                continue
            if self._is_injected_runtime_param(param.name, param.annotation):
                continue

            annotation = self._resolve_annotation(MISSING, param.annotation)
            required, default = self._resolve_requirement(
                annotation=annotation,
                param_has_default=param.has_default,
                param_default=param.default,
                explicit_default=MISSING,
            )
            ordered.append(
                ArgumentEntry(
                    name=param.name,
                    type=annotation,
                    help_text="",
                    required=required,
                    default=default,
                )
            )

        return ordered

    @staticmethod
    def _is_injected_runtime_param(name: str, annotation: Any) -> bool:
        return registry_utils.is_injected_runtime_param(name, annotation)

    @staticmethod
    def _resolve_annotation(explicit_type: Any, annotation: Any) -> Any:
        return registry_utils.resolve_annotation(
            explicit_type,
            annotation,
            missing=MISSING,
        )

    @staticmethod
    def _resolve_requirement(
        *,
        annotation: Any,
        param_has_default: bool,
        param_default: Any,
        explicit_default: Any,
    ) -> tuple[bool, Any]:
        return registry_utils.resolve_requirement(
            annotation=annotation,
            param_has_default=param_has_default,
            param_default=param_default,
            explicit_default=explicit_default,
            missing=MISSING,
        )

    def _suggest(self, token: str) -> str | None:
        return registry_utils.suggest_command(token, self._commands, self._aliases)

    def suggest(self, token: str) -> str | None:
        """Return the closest known command/alias for *token*, if any."""
        return self._suggest(token)

    @staticmethod
    def _is_builtin_help_token(token: str) -> bool:
        return registry_utils.is_builtin_help_token(token)

    @staticmethod
    def _stdin_is_interactive() -> bool:
        return registry_utils.stdin_is_interactive()

    @staticmethod
    def _render_argument_type(annotation: Any) -> str:
        return registry_utils.render_argument_type(annotation)

    def _render_global_help(
        self,
        *,
        program_name: str | None = None,
        shell_title: str = "Registers CLI",
        shell_description: str = "Type 'help' for shell help and 'exit' to quit.",
        shell_version: str | None = None,
        use_color: bool = False,
        tag: str | None = None,
    ) -> str:
        return registry_utils.render_global_help(
            self._commands,
            self._groups,
            self._aliases,
            program_name=program_name,
            shell_title=shell_title,
            shell_description=shell_description,
            shell_version=shell_version,
            use_color=use_color,
            tag=tag,
        )

    def _render_command_help(
        self,
        entry: CommandEntry,
        *,
        program_name: str | None = None,
        use_color: bool = False,
    ) -> str:
        return registry_utils.render_command_help(
            entry,
            self._aliases,
            missing=MISSING,
            program_name=program_name,
            use_color=use_color,
        )

    def _render_builtin_help_detail(
        self,
        target: str,
        *,
        program_name: str | None = None,
        use_color: bool = False,
    ) -> str:
        return registry_utils.render_builtin_help_detail(
            target,
            program_name=program_name,
            use_color=use_color,
        )

    def _render_global_commands_table(self, *, header: str, use_color: bool, tag: str | None = None) -> str:
        return registry_utils.render_global_commands_table(
            self._commands,
            self._groups,
            self._aliases,
            header=header,
            use_color=use_color,
            tag=tag,
        )

    def _has_group(self, group_name: str) -> bool:
        return registry_utils.has_group(group_name, self._commands, self._aliases)

    def _render_group_help(
        self,
        group_name: str,
        *,
        program_name: str | None = None,
        use_color: bool = False,
    ) -> str:
        return registry_utils.render_group_help(
            group_name,
            self._commands,
            self._aliases,
            program_name=program_name,
            use_color=use_color,
        )

    def _group_command_rows(self, group_name: str, entries: list[CommandEntry]) -> list[tuple[str, str]]:
        return registry_utils.group_command_rows(group_name, entries)

    def _render_help_table(self, rows: list[tuple[str, str]], *, use_color: bool, indent: int = 2) -> str:
        return registry_utils.render_help_table(rows, use_color=use_color, indent=indent)

    def _command_signature(self, entry: CommandEntry, *, primary: str | None = None) -> str:
        return registry_utils.command_signature(entry, primary=primary)

    @staticmethod
    def _argument_label(arg: ArgumentEntry) -> str:
        return registry_utils.argument_label(arg)

    def _entry_aliases(self, entry: CommandEntry) -> tuple[str, ...]:
        return registry_utils.entry_aliases(entry, self._aliases)

    def _format_entry_label(self, entry: CommandEntry, *, primary: str | None = None) -> str:
        return registry_utils.format_entry_label(
            entry,
            self._aliases,
            primary=primary,
        )

    def _group_aliases(self, group_name: str) -> tuple[str, ...]:
        return registry_utils.group_aliases(group_name, self._aliases)

    def _format_group_label(self, group_name: str, *, primary: str | None = None) -> str:
        return registry_utils.format_group_label(
            group_name,
            self._aliases,
            primary=primary,
        )

    @staticmethod
    def _section_header(title: str, use_color: bool) -> str:
        return registry_utils.section_header(title, use_color)

    @staticmethod
    def _c(text: str, code: str, enabled: bool) -> str:
        return registry_utils.color_text(text, code, enabled)

    @staticmethod
    def _enable_windows_ansi() -> bool:
        return registry_utils.enable_windows_ansi()

    @classmethod
    def _supports_color(cls, colors: bool | None) -> bool:
        return registry_utils.supports_color(colors)

    def __len__(self) -> int:
        return len(self._commands)

    def __repr__(self) -> str:
        names = ", ".join(self._commands)
        return f"CommandRegistry([{names}])"


class CommandGroup:
    """Decorator facade for grouped commands."""

    def __init__(
        self,
        registry: CommandRegistry,
        *,
        path: tuple[str, ...],
        alias_paths: tuple[tuple[str, ...], ...] = (),
        description: str = "",
        tags: tuple[str, ...] = (),
    ) -> None:
        self._registry = registry
        self._path = path
        self._alias_paths = alias_paths
        self.description = description
        self.tags = tags

    def argument(self, *args: Any, **kwargs: Any) -> Any:
        return self._registry.argument(*args, **kwargs)

    def prompt(self, *args: Any, **kwargs: Any) -> Any:
        return self._registry.prompt(*args, **kwargs)

    def option(self, *args: Any, **kwargs: Any) -> Any:
        return self._registry.option(*args, **kwargs)

    def alias(self, *args: Any, **kwargs: Any) -> Any:
        return self._registry.alias(*args, **kwargs)

    def confirm(self, *args: Any, **kwargs: Any) -> Any:
        return self._registry.confirm(*args, **kwargs)

    def dry_run(self) -> Any:
        return self._registry.dry_run()

    def group(
        self,
        name: str,
        *,
        description: str = "",
        aliases: Sequence[str] = (),
        tags: Sequence[str] = (),
    ) -> "CommandGroup":
        inherited_aliases = tuple(alias_path + (name,) for alias_path in self._alias_paths)
        local_aliases = tuple(self._path + (alias,) for alias in aliases)
        combined_aliases = tuple(
            alias_path + (alias,)
            for alias_path in self._alias_paths
            for alias in aliases
        )
        alias_paths = inherited_aliases + local_aliases + combined_aliases
        full_path = self._path + (name,)
        self._registry._groups[" ".join(full_path)] = (description, self.tags + tuple(tags))
        return CommandGroup(
            self._registry,
            path=full_path,
            alias_paths=alias_paths,
            description=description,
            tags=self.tags + tuple(tags),
        )

    def register(
        self,
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
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            options = self._registry._pending_options.get(fn, [])
            leaf_name = (name or "").strip() or self._registry._derive_command_name(
                tuple(item.flag for item in options),
                fn.__name__,
            )
            full_name = " ".join(self._path + (leaf_name,))
            config = self._registry._config_for(fn)
            config.tags = self.tags + tuple(tags)
            config.examples = tuple(examples)
            config.deprecated = deprecated
            config.render = render
            config.default_output = default_output
            config.pager = pager
            config.error_hints = dict(error_hints or {})
            config.capture_logs = capture_logs
            self._registry.finalize_command(
                fn,
                name=full_name,
                description=description,
                help_text=help,
                register_option_aliases=False,
            )
            for alias_path in self._alias_paths:
                alias_name = " ".join(alias_path + (leaf_name,))
                self._registry._register_alias_path(alias_name, full_name)
            for option in options:
                for alias_path in (self._path, *self._alias_paths):
                    alias_name = " ".join(alias_path + (option.flag,))
                    self._registry._register_alias_path(alias_name, full_name)
            return fn

        return decorator
