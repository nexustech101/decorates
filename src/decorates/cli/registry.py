"""
The CommandRegistry stores command metadata and provides the @register
decorates. It is intentionally decoupled from argparse and dispatching —
it only knows about *what* commands exist, not *how* to invoke them.

Usage::

    registry = CommandRegistry()

    @registry.register("greet", help_text="Greet someone")
    def greet(name: str) -> str:
        return f"Hello, {name}!"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
import logging
from typing import TYPE_CHECKING, Any, Callable, Sequence

from decorates.cli.exceptions import (
    CommandExecutionError,
    DuplicateCommandError,
    FrameworkError,
    UnknownCommandError,
)

if TYPE_CHECKING:
    from decorates.cli.middleware import MiddlewareChain
    from decorates.cli.container import DIContainer

logger = logging.getLogger(__name__)


@dataclass
class CommandEntry:
    """All metadata the decorates needs for a single command."""
    name: str
    handler: Callable[..., Any]
    help_text: str = ""
    description: str = ""
    options: tuple[str, ...] = field(default_factory=tuple)


class CommandRegistry:
    """
    Maps command names to their handlers and metadata.

    Registries can be merged to support modular / plugin-based apps.
    """

    def __init__(self) -> None:
        self._commands: dict[str, CommandEntry] = {}
        self._aliases: dict[str, str] = {}  # NEW

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str | None = None,
        *,
        help_text: str = "",
        description: str = "",
        options: Sequence[str] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        Decorators that decorates a callable as a CLI command.

        Args:
            name:      The subcommand name used on the CLI.
            help_text: Short description shown in --help output.
            description: Longer description for docs / help output.
            options: Optional command aliases such as ``["-g", "--greet"]``.

        Raises:
            DuplicateCommandError: If *name* is already registered.
        """
        if name is None:
            raise TypeError("register() missing required argument: 'name'")

        summary = description or help_text
        normalized_options = tuple(options or ())

        def decorates(fn: Callable[..., Any]) -> Callable[..., Any]:
            # Check command name collision
            if name in self._commands or name in self._aliases:
                logger.warning("Attempted duplicate command registration name='%s'.", name)
                raise DuplicateCommandError(name)

            # Check alias collisions
            for option in normalized_options:
                normalized = option.lstrip("-")

                if normalized in self._commands or normalized in self._aliases:
                    logger.warning(
                        "Attempted duplicate command alias registration option='%s' normalized='%s'.",
                        option,
                        normalized,
                    )
                    raise DuplicateCommandError(option)

                self._aliases[normalized] = name

            self._commands[name] = CommandEntry(
                name=name,
                handler=fn,
                help_text=summary,
                description=description,
                options=normalized_options,
            )
            return fn

        return decorates

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> CommandEntry:
        """
        Return the entry for *name*.

        Raises:
            UnknownCommandError: If *name* has not been registered.
        """
        if name in self._commands:
            return self._commands[name]

        normalized = name.lstrip("-")
        if normalized in self._aliases:
            return self._commands[self._aliases[normalized]]

        raise UnknownCommandError(name)

    def all(self) -> dict[str, CommandEntry]:
        """Return a shallow copy of the command map."""
        return dict(self._commands)

    def has(self, name: str) -> bool:
        """Return True if *name* is registered."""
        if name in self._commands:
            return True

        normalized = name.lstrip("-")
        if normalized in self._aliases:
            return True

        return False

    def list_clis(self) -> None:
        """Print the registered commands and any configured aliases."""
        if not self._commands:
            print("No commands registered.")
            return

        print("Available commands:")
        for entry in self._commands.values():
            aliases = f" [{', '.join(entry.options)}]" if entry.options else ""
            summary = entry.help_text or entry.description or "(no description)"
            print(f"  {entry.name}{aliases}: {summary}")

    def list_commands(self) -> None:
        """Backward-compatible alias for :meth:`list_clis`."""
        self.list_clis()

    def run(
        self,
        argv: Sequence[str] | None = None,
        *,
        container: "DIContainer | None" = None,
        middleware: "MiddlewareChain | None" = None,
        print_result: bool = True,
    ) -> Any:
        """
        Parse CLI arguments and dispatch the matching command.

        This is a convenience wrapper around ``build_parser`` and
        ``Dispatcher`` for small scripts that don't need a custom
        bootstrap module.
        """
        import sys

        from decorates.cli.dispatcher import Dispatcher
        from decorates.cli.parser import build_parser
        from decorates.cli.container import DIContainer

        parser = build_parser(self, container)
        raw_argv = list(sys.argv[1:] if argv is None else argv)
        logger.debug("CommandRegistry.run invoked with argv=%s", raw_argv)
        normalized = self._normalize_argv(raw_argv)
        try:
            args = parser.parse_args(normalized)
        except SystemExit:
            if normalized:
                cmd = normalized[0]
                matches = get_close_matches(cmd, self._commands.keys())
                if matches:
                    print(f"Did you mean '{matches[0]}'?")
                    logger.info("Parser rejected command '%s'; suggested '%s'.", cmd, matches[0])
                else:
                    logger.info("Parser rejected argv=%s with no close command match.", normalized)
            raise

        if not args.command:
            logger.info("No command provided; printing parser help.")
            parser.print_help()
            raise SystemExit(1)

        cli_args = {k: v for k, v in vars(args).items() if k != "command"}
        dispatcher = Dispatcher(self, container or DIContainer(), middleware)
        try:
            result = dispatcher.dispatch(args.command, cli_args)
        except FrameworkError:
            logger.warning("Framework error while running command '%s'.", args.command, exc_info=True)
            raise
        except Exception as exc:
            logger.exception("Unhandled command failure in run() for '%s'.", args.command)
            raise CommandExecutionError(args.command, str(exc)) from exc

        if print_result and result is not None:
            print(result)

        return result

    def _normalize_argv(self, argv: Sequence[str]) -> list[str]:
        """Map command aliases like ``-g`` or ``--greet`` to the command name."""
        normalized = list(argv)
        if not normalized:
            return normalized

        first = normalized[0]
        alias_map: dict[str, str] = {}
        for entry in self._commands.values():
            for option in entry.options:
                alias_map[option] = entry.name
                stripped = option.lstrip("-")
                if stripped:
                    alias_map[stripped] = entry.name

        if first in alias_map:
            normalized[0] = alias_map[first]

        return normalized

    # ------------------------------------------------------------------
    # Merging (plugin / modular support)
    # ------------------------------------------------------------------

    def merge(self, other: "CommandRegistry", *, allow_override: bool = False) -> None:
        """
        Merge all commands from *other* into this registry.

        Args:
            other:           The registry to merge in.
            allow_override:  If False (default), raises DuplicateCommandError
                             on name collisions. If True, *other* wins.
        """
        for name, entry in other.all().items():
            if name in self._commands and not allow_override:
                raise DuplicateCommandError(name)
            self._commands[name] = entry

    def __len__(self) -> int:
        return len(self._commands)

    def __repr__(self) -> str:
        names = ", ".join(self._commands)
        return f"CommandRegistry([{names}])"
