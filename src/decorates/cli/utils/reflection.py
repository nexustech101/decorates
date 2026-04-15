"""
Thin wrappers around ``inspect`` for extracting parameter metadata from
command handler signatures.

Uses ``typing.get_type_hints()`` to resolve annotations so that handlers
defined in modules with ``from __future__ import annotations`` (PEP 563
stringified annotations) are handled correctly.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ParamInfo:
    """Distilled metadata for a single function parameter."""
    name: str
    annotation: Any       # Fully resolved type (never a string)
    has_default: bool
    default: Any


def get_params(fn: Callable[..., Any]) -> list[ParamInfo]:
    """
    Return a list of :class:`ParamInfo` for every parameter in *fn*,
    excluding ``self`` and ``cls``.

    Annotations are resolved via ``typing.get_type_hints()`` so that
    PEP 563 stringified annotations (``from __future__ import annotations``)
    are evaluated to their actual types. Falls back to the raw annotation
    from the signature if ``get_type_hints()`` fails (e.g. forward
    references that can't be resolved in the current scope).
    """
    sig = inspect.signature(fn)

    # Resolve all string annotations to real types where possible
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    result: list[ParamInfo] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue

        # Prefer the resolved hint; fall back to the raw annotation
        annotation = hints.get(name, param.annotation)
        has_default = param.default is not inspect.Parameter.empty

        result.append(
            ParamInfo(
                name=name,
                annotation=annotation,
                has_default=has_default,
                default=param.default if has_default else None,
            )
        )

    return result
