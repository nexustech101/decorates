"""
Builds an ``argparse.ArgumentParser`` from a :class:`CommandRegistry`.

All knowledge of argparse lives here. The rest of the framework never
imports argparse directly. This keeps the parser easily swappable and
the dispatcher clean.
"""

from __future__ import annotations

import argparse
import inspect
from typing import TYPE_CHECKING, Any, get_origin, get_args, Literal

from decorators.cli.registry import CommandRegistry
from decorators.cli.utils.reflection import get_params
from decorators.cli.utils.typing import is_bool_flag, is_optional, resolve_argparse_type

if TYPE_CHECKING:
    from decorators.cli.container import DIContainer


class SuggestingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with fuzzy command suggestions."""

    def __init__(self, *args, **kwargs):
        self._registry = None  # will be set later
        super().__init__(*args, **kwargs)

    def error(self, message):
        from difflib import get_close_matches
        import re

        if "invalid choice" in message:
            match = re.search(r"'(.+?)'", message)
            if match:
                cmd = match.group(1)
                matches = get_close_matches(cmd, self._registry.all().keys()) if self._registry else []

                if matches:
                    print(f"Did you mean '{matches[0]}'?")
                else:
                    print("Unknown command")

        super().error(message)


def build_parser(
    registry: CommandRegistry,
    container: "DIContainer | None" = None,
) -> argparse.ArgumentParser:
    """
    Construct a top-level ArgumentParser with one subparser per command.

    Parameters whose type is registered in *container* are skipped —
    they are DI-injected at dispatch time and must not appear on the CLI.

    Argument behaviour for everything else:
    * bool              → --flag  (store_true)
    * Optional[X] / default → --arg (optional keyword)
    * required primitive    → positional argument
    """
    parser = SuggestingArgumentParser(
        description="Built with cli_registry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser._registry = registry  # type: ignore[attr-defined]

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    for name, entry in registry.all().items():
        sub = subparsers.add_parser(
            name,
            help=_format_help(entry),
            description=entry.description or entry.help_text,
            aliases=_command_aliases(entry),
        )
        _add_arguments(sub, entry.handler, container)

    # Empty registry behavior: fail at parse time (matches tests)
    if not registry.all():
        def _fail(*args, **kwargs):
            raise SystemExit(1)
        parser.parse_args = _fail

    return parser


def _add_arguments(
    subparser: argparse.ArgumentParser,
    fn: Any,
    container: "DIContainer | None" = None,
) -> None:
    """Add CLI arguments to *subparser* from *fn*'s signature.

    Service parameters (types known to *container*) are skipped entirely.
    """
    for param in get_params(fn):
        annotation = param.annotation

        # Skip DI parameters (typed classes, not primitives)
        if (
            annotation is not inspect.Parameter.empty
            and isinstance(annotation, type)
            and annotation not in (str, int, float, bool)
        ):
            # If container exists, only skip if resolvable
            if container is None or container.has(annotation):
                continue

        # Boolean flags
        if is_bool_flag(annotation):
            subparser.add_argument(
                f"--{param.name}",
                action="store_true",
                default=param.default if param.has_default else False,
                help=f"{param.name} (flag)",
            )
            continue

        # Literal[...] (enum)
        if get_origin(annotation) is Literal:
            choices = get_args(annotation)

            if param.has_default:
                subparser.add_argument(
                    f"--{param.name}",
                    dest=param.name,
                    choices=choices,
                    default=param.default,
                )
            else:
                subparser.add_argument(
                    param.name,
                    choices=choices,
                )
            continue

        arg_type = resolve_argparse_type(annotation)

        optional = param.has_default or is_optional(annotation)

        if optional:
            subparser.add_argument(
                f"--{param.name}",
                dest=param.name,
                default=param.default if param.has_default else None,
                required=False,
                type=arg_type,
            )
        else:
            subparser.add_argument(
                param.name,
                type=arg_type,
            )


def _command_aliases(entry: Any) -> list[str]:
    """
    Convert metadata aliases into argparse subcommand aliases.

    ``ops`` entries like ``-g`` and ``--greet`` are normalized to ``g``
    and ``greet`` for argparse, while ``registry.run()`` still accepts
    the original flag-style tokens.
    """
    aliases: list[str] = []
    for op in getattr(entry, "ops", ()):
        aliases.append(op.lstrip("-"))
    return aliases


def _format_help(entry: Any) -> str:
    """Include original ops in help output (fixes test expectations)."""
    ops = " ".join(entry.ops) if entry.ops else ""
    return f"{entry.help_text} {ops}".strip()