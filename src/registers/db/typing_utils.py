"""
Maps Python / Pydantic type annotations to SQLAlchemy column types.
Handles Optional, Union, constrained types, and JSON fallback.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args, get_origin
from uuid import UUID

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, LargeBinary, Numeric, String
from sqlalchemy.sql.type_api import TypeEngine

DEFAULT_VARCHAR_LENGTH = 255


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def default_table_name(model_name: str) -> str:
    """``UserProfile`` → ``user_profiles``."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", model_name).lower()
    return f"{snake}s"


def default_database_url(model_name: str) -> str:
    db_path = Path(f"{default_table_name(model_name)}.db").resolve()
    return f"sqlite:///{db_path.as_posix()}"


def normalize_database_url(database_url: str | Path) -> str:
    """Ensure the URL has a proper scheme; coerce bare paths to sqlite:///."""
    if isinstance(database_url, Path):
        return f"sqlite:///{database_url.resolve().as_posix()}"
    if "://" in str(database_url):
        return str(database_url)
    return f"sqlite:///{Path(str(database_url)).resolve().as_posix()}"


# ---------------------------------------------------------------------------
# Annotation introspection
# ---------------------------------------------------------------------------

def unwrap_annotation(annotation: Any) -> Any:
    """
    Strip Optional / Union wrappers to reach the concrete inner type.

    ``Optional[int]`` → ``int``, ``Union[str, None]`` → ``str``.
    Collection types (list, dict, …) are returned as-is so they map to JSON.
    """
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    if origin in (list, dict, tuple, set, frozenset):
        return annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == 1:
        return unwrap_annotation(args[0])
    return annotation


def annotation_is_integer(annotation: Any) -> bool:
    resolved = unwrap_annotation(annotation)
    return resolved is int or (isinstance(resolved, type) and issubclass(resolved, int))


def annotation_is_uuid(annotation: Any) -> bool:
    resolved = unwrap_annotation(annotation)
    return resolved is UUID or (isinstance(resolved, type) and issubclass(resolved, UUID))


def field_allows_none(field: Any) -> bool:
    """
    Return True when the field accepts ``None``.

    Accepts anything exposing ``.annotation`` and ``.default`` — a Pydantic
    ``FieldInfo`` or a ``registers.db.adapters.FieldView`` — so nullability is
    determined the same way regardless of the model flavour.

    A field is nullable if its annotation includes ``NoneType`` *or* it defaults to
    ``None``. The second clause matters for bare ``x: int = None`` declarations.
    """
    annotation = field.annotation
    origin = get_origin(annotation)
    if origin is not None and type(None) in get_args(annotation):
        return True
    return getattr(field, "default", None) is None


# ---------------------------------------------------------------------------
# SQLAlchemy type mapping
# ---------------------------------------------------------------------------

# Ordered most-specific first so issubclass probes work correctly.
_DIRECT_MAP: list[tuple[type, type[TypeEngine] | Any]] = [
    (bool,     Boolean),
    (int,      Integer),
    (float,    Float),
    (Decimal,  Numeric),
    (datetime, DateTime),
    (date,     Date),
    (UUID,     lambda: String(36)),
    (str,      lambda: String(DEFAULT_VARCHAR_LENGTH)),
    (bytes,    LargeBinary),
]


def sqlalchemy_type_for_annotation(annotation: Any) -> TypeEngine[Any]:
    """Return the best SQLAlchemy column type for a Python type annotation."""
    resolved = unwrap_annotation(annotation)

    for python_type, type_factory in _DIRECT_MAP:
        if resolved is python_type:
            return type_factory()
        if isinstance(resolved, type) and issubclass(resolved, python_type):
            return type_factory()

    # Fall back to Pydantic JSON schema for Enum, Literal, custom types etc.
    schema = _json_schema_for(resolved)
    fmt    = schema.get("format", "")
    kind   = schema.get("type", "")

    if fmt in ("date-time", "datetime"):
        return DateTime()
    if fmt == "date":
        return Date()
    if fmt == "uuid":
        return String(36)
    if kind == "string":
        return String(DEFAULT_VARCHAR_LENGTH)
    if kind == "integer":
        return Integer()
    if kind == "number":
        return Float()
    if kind == "boolean":
        return Boolean()

    # Unknown / complex types stored as JSON text
    return JSON()


def _json_schema_for(annotation: Any) -> dict[str, Any]:
    """
    Best-effort JSON-schema probe for exotic annotations.

    Only reached for types the direct map does not cover. Pydantic is imported
    lazily and its absence is not an error — without it these types simply fall
    through to a JSON column, which is the same outcome an unrecognized schema
    would produce.
    """
    try:
        from pydantic import TypeAdapter
    except ImportError:
        return {}
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception:
        return {}
