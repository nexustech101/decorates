"""
cli_registry.utils.typing
~~~~~~~~~~~~~~~~~~~~~~
Helpers for resolving Python type annotations into argparse-compatible
concrete types. Kept isolated so the parser stays readable.
"""

from __future__ import annotations

import inspect
from typing import Any, Union, get_args, get_origin


# Types that map directly to argparse `type=` values.
_PRIMITIVE_TYPES: dict[Any, type] = {
    int: int,
    float: float,
    str: str,
    bool: bool,
}


def resolve_argparse_type(annotation: Any) -> type | None:
    """
    Return the argparse-compatible type for *annotation*, or None if
    the annotation is not a primitive the parser should coerce.

    - Handles ``Union[X, None]`` / ``Optional[X]`` by unwrapping to X.
    - Returns ``None`` for ``bool`` (handled separately as a flag).
    - Returns ``str`` as the fallback for unknown / complex types.
    """
    if annotation is inspect.Parameter.empty:
        return str  # No annotation → treat as string

    origin = get_origin(annotation)

    # Unwrap Optional[X] → X, Union[X, Y] → first non-None type
    if origin is Union:
        for arg in get_args(annotation):
            if arg is not type(None):
                return resolve_argparse_type(arg)

    # bool is handled as a flag, not a positional type
    if annotation is bool:
        return None

    return _PRIMITIVE_TYPES.get(annotation, str)


def is_optional(annotation: Any) -> bool:
    """Return True if the annotation is ``Optional[X]`` / ``Union[X, None]``."""
    if get_origin(annotation) is Union:
        return type(None) in get_args(annotation)
    return False


def is_bool_flag(annotation: Any) -> bool:
    """Return True if the annotation resolves to a boolean flag."""
    if annotation is bool:
        return True
    if get_origin(annotation) is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        return len(args) == 1 and args[0] is bool
    return False
