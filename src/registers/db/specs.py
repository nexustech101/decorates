"""
Per-field resolution, computed once at registration.

Motivation
----------
Before this module, four separate concerns each re-derived what they knew about a
field, on every call:

* ``metadata.py`` read ``db_field`` metadata to validate the key field;
* ``_ModelManager.__init__`` read it again to build three ``set`` s of field names;
* ``_construct_table`` read it a third time to build columns;
* ``_assert_known_fields`` built a **fresh** ``pydantic.TypeAdapter`` per field, per
  query, to validate criteria values — roughly 60x the cost of a cached validator.

:class:`FieldSpec` resolves all of it exactly once, at decoration time, and hands the
manager three closures per field: ``validate`` (coerce-or-raise), ``to_db``, and
``from_db``. Everything downstream is a dict lookup.

Codec model
-----------
The set of types this library can store is bounded by the set of SQL column types it
emits. That makes a finite codec table possible, and it is what allows the model layer
to be swapped (Pydantic today, dataclasses next) without the manager caring:

    Python value  --to_db-->  DB-ready value  --SQLAlchemy-->  column
    Python value  <-from_db-- DB row value    <-SQLAlchemy---  column

``to_db``/``from_db`` are identity for most types, because SQLAlchemy's own column
types already round-trip ``datetime``, ``Decimal``, and JSON correctly. They earn their
keep for the cases SQLAlchemy does not cover here: binary UUID primary keys, encrypted
fields, and hashed passwords.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Literal, Mapping
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    String,
)
from sqlalchemy.sql.type_api import TypeEngine

from registers.db.exceptions import ConfigurationError
from registers.db.typing_utils import DEFAULT_VARCHAR_LENGTH, unwrap_annotation

IdStrategy = Literal["manual", "autoincrement", "uuid4"]

#: Sentinel meaning "this field has no default"; ``None`` is a legitimate default.
MISSING: Any = object()


class FieldCoercionError(ValueError):
    """
    Raised by a :class:`FieldSpec` validator when a value cannot be coerced.

    Deliberately a plain ``ValueError`` and *not* a ``RegistryError``: this module
    has no opinion about which public exception the failure should surface as.
    Callers translate it — the query path raises ``InvalidQueryError``, the model
    construction path raises a validation error.
    """

    def __init__(self, value: Any, target: str) -> None:
        super().__init__(f"Cannot interpret {value!r} as {target}.")
        self.value = value
        self.target = target


def _coercion_failed(value: Any, target: str) -> FieldCoercionError:
    return FieldCoercionError(value, target)


# ---------------------------------------------------------------------------
# Scalar coercion
# ---------------------------------------------------------------------------
#
# Each coercer accepts what a caller or a database driver might plausibly hand
# over and returns the canonical Python type, or raises. They are deliberately
# stricter than ``bool(value)``-style coercion: ``int("abc")`` must fail loudly
# rather than reach the database as garbage.


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    raise _coercion_failed(value, "a boolean")


def _coerce_int(value: Any) -> int:
    # bool is a subclass of int; accepting it silently turns True into 1 in an
    # integer column, which is almost never what the caller meant.
    if isinstance(value, bool):
        raise _coercion_failed(value, "an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise _coercion_failed(value, "an integer")
        return int(value)
    if isinstance(value, (str, Decimal)):
        try:
            return int(value)
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise _coercion_failed(value, "an integer") from exc
    raise _coercion_failed(value, "an integer")


def _coerce_float(value: Any) -> float:
    if isinstance(value, bool):
        raise _coercion_failed(value, "a float")
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise _coercion_failed(value, "a float") from exc
    raise _coercion_failed(value, "a float")


def _coerce_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise _coercion_failed(value, "a decimal")
    if isinstance(value, (int, str)):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise _coercion_failed(value, "a decimal") from exc
    if isinstance(value, float):
        # str() first: Decimal(0.1) captures binary float noise.
        return Decimal(str(value))
    raise _coercion_failed(value, "a decimal")


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    # Refuse silent stringification of containers and None — those indicate a
    # caller mistake, not a formatting preference.
    if value is None or isinstance(value, (list, dict, tuple, set, bytes)):
        raise _coercion_failed(value, "a string")
    if isinstance(value, (int, float, Decimal, bool, uuid.UUID, datetime, date)):
        return str(value)
    raise _coercion_failed(value, "a string")


def _coerce_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode()
    raise _coercion_failed(value, "bytes")


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise _coercion_failed(value, "a datetime") from exc
    raise _coercion_failed(value, "a datetime")


def _coerce_date(value: Any) -> date:
    # datetime is a subclass of date, so this ordering matters.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise _coercion_failed(value, "a date") from exc
    raise _coercion_failed(value, "a date")


def _coerce_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise _coercion_failed(value, "a UUID") from exc
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) != 16:
            raise _coercion_failed(value, "a UUID")
        return uuid.UUID(bytes=raw)
    raise _coercion_failed(value, "a UUID")


def _identity(value: Any) -> Any:
    return value


#: ``python_type -> (coercer, sqlalchemy type factory)``.
#: Ordered most-specific first so ``issubclass`` probing resolves correctly:
#: ``bool`` before ``int``, ``datetime`` before ``date``.
_SCALAR_CODECS: list[tuple[type, Callable[[Any], Any], Callable[[], TypeEngine[Any]]]] = [
    (bool,     _coerce_bool,     Boolean),
    (int,      _coerce_int,      Integer),
    (float,    _coerce_float,    Float),
    (Decimal,  _coerce_decimal,  Numeric),
    (datetime, _coerce_datetime, DateTime),
    (date,     _coerce_date,     Date),
    (uuid.UUID, _coerce_uuid,    lambda: String(36)),
    (str,      _coerce_str,      lambda: String(DEFAULT_VARCHAR_LENGTH)),
    (bytes,    _coerce_bytes,    LargeBinary),
]


def _lookup_scalar(
    python_type: Any,
) -> tuple[Callable[[Any], Any], Callable[[], TypeEngine[Any]]] | None:
    """Return ``(coercer, sa_type_factory)`` for a scalar type, or None."""
    if not isinstance(python_type, type):
        return None
    for candidate, coercer, sa_factory in _SCALAR_CODECS:
        if python_type is candidate or issubclass(python_type, candidate):
            return coercer, sa_factory
    return None


def _enum_coercer(enum_cls: type[Enum]) -> Callable[[Any], Any]:
    def coerce(value: Any) -> Any:
        if isinstance(value, enum_cls):
            return value
        try:
            return enum_cls(value)
        except (ValueError, KeyError, TypeError):
            pass
        # Fall back to member-name lookup so ``Status.ACTIVE`` round-trips even
        # when the stored form is the name rather than the value.
        if isinstance(value, str) and value in enum_cls.__members__:
            return enum_cls[value]
        raise _coercion_failed(value, f"a {enum_cls.__name__}")

    return coerce


def resolve_codec(annotation: Any) -> tuple[Callable[[Any], Any], TypeEngine[Any]]:
    """
    Return ``(validate, sa_type)`` for a field annotation.

    ``validate`` coerces a value to the annotation's Python type or raises
    :class:`ConfigurationError`. It does **not** handle ``None`` — nullability is
    applied by :func:`build_validator`, which wraps this.

    Unrecognized types (nested models, ``list``, ``dict``, ``Literal`` unions, …)
    fall back to a permissive JSON codec: they are stored as JSON and passed
    through unvalidated, matching the previous behavior.
    """
    resolved = unwrap_annotation(annotation)

    # Enum MUST be probed before the scalar table. Mixin enums (``IntEnum``,
    # ``StrEnum``, ``class Method(str, Enum)``) are genuine subclasses of int/str,
    # so ``_lookup_scalar`` would match them first and hand back the raw scalar
    # coercer — silently dropping membership validation and letting an
    # out-of-range value like 999 reach an IntEnum column.
    if isinstance(resolved, type) and issubclass(resolved, Enum):
        # Members are stored by value; column width follows the member type.
        member_types = {type(member.value) for member in resolved}
        if member_types <= {int, bool} and member_types:
            return _enum_coercer(resolved), Integer()
        return _enum_coercer(resolved), String(DEFAULT_VARCHAR_LENGTH)

    scalar = _lookup_scalar(resolved)
    if scalar is not None:
        coercer, sa_factory = scalar
        return coercer, sa_factory()

    return _identity, JSON()


def build_validator(
    annotation: Any,
    *,
    nullable: bool,
) -> Callable[[Any], Any]:
    """
    Return a cached value validator for *annotation*.

    Built once per field at registration. This is the replacement for the
    per-call ``TypeAdapter(...)`` construction in the query path.
    """
    coercer, _ = resolve_codec(annotation)

    if coercer is _identity:
        return _identity

    def validate(value: Any) -> Any:
        if value is None:
            if nullable:
                return None
            raise _coercion_failed(None, "a non-null value")
        return coercer(value)

    return validate


# ---------------------------------------------------------------------------
# FieldSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """
    Everything the manager needs to know about one model field.

    Immutable and computed once at registration. ``validate``/``to_db``/``from_db``
    are closures resolved at build time, so no per-call type introspection remains
    in the query or write paths.
    """

    name: str
    annotation: Any
    python_type: Any
    nullable: bool
    required: bool
    default: Any = MISSING

    # --- db_field metadata, as typed attributes rather than a dict of "db_*" keys
    primary_key: bool = False
    autoincrement: bool = False
    unique: bool = False
    index: bool = False
    foreign_key: str | None = None
    hash_password: bool = False
    encrypted: bool = False
    exclude_from_db: bool = False
    id_strategy: IdStrategy | None = None
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    timezone: bool | None = None
    column_type: Any = None

    # --- resolved once
    sa_type: TypeEngine[Any] = dataclass_field(default_factory=JSON)
    validate: Callable[[Any], Any] = _identity
    to_db: Callable[[Any], Any] = _identity
    from_db: Callable[[Any], Any] = _identity

    @property
    def is_stored(self) -> bool:
        """False for fields kept on the model but never given a column."""
        return not self.exclude_from_db


def _sa_type_for(
    annotation: Any,
    meta: Mapping[str, Any],
    fallback: TypeEngine[Any],
) -> TypeEngine[Any]:
    """Apply ``db_field`` width/precision overrides on top of the codec's type."""
    explicit = meta.get("db_column_type")
    if explicit is not None:
        if isinstance(explicit, TypeEngine):
            return explicit
        if isinstance(explicit, type) and issubclass(explicit, TypeEngine):
            return explicit()
        if callable(explicit):
            resolved = explicit()
            if isinstance(resolved, TypeEngine):
                return resolved
        raise ConfigurationError(
            "db_field(column_type=...) must be a SQLAlchemy TypeEngine, "
            "TypeEngine class, or factory."
        )

    resolved_type = unwrap_annotation(annotation)
    length = meta.get("db_length")
    precision = meta.get("db_precision")
    scale = meta.get("db_scale")
    tz = meta.get("db_timezone")

    if isinstance(resolved_type, type):
        if issubclass(resolved_type, bool):
            return fallback
        if issubclass(resolved_type, str):
            return String(length or DEFAULT_VARCHAR_LENGTH)
        if issubclass(resolved_type, bytes):
            return LargeBinary(length)
        if issubclass(resolved_type, Decimal):
            return Numeric(precision=precision, scale=scale)
        if issubclass(resolved_type, datetime):
            return DateTime(timezone=bool(tz))

    return fallback


def build_field_spec(
    name: str,
    annotation: Any,
    *,
    nullable: bool,
    required: bool,
    default: Any,
    metadata: Mapping[str, Any],
    is_key_field: bool,
    key_id_strategy: IdStrategy | None,
    encrypt: Callable[[Any], Any] | None = None,
    decrypt: Callable[[Any], Any] | None = None,
) -> FieldSpec:
    """
    Resolve one field into a :class:`FieldSpec`.

    Parameters
    ----------
    metadata:
        Normalized ``db_field`` metadata (the ``"db_*"`` mapping produced by
        ``fields.get_db_field_metadata``).
    is_key_field / key_id_strategy:
        Together these select the UUID-primary-key codec, which stores UUIDs as
        16 raw bytes rather than a 36-character string.
    encrypt / decrypt:
        Supplied by the manager when the model declares an ``encryption_key``.
        Kept as injected callables so this module never imports ``cryptography``.
    """
    coercer, codec_sa_type = resolve_codec(annotation)
    sa_type = _sa_type_for(annotation, metadata, codec_sa_type)
    validate = build_validator(annotation, nullable=nullable)

    to_db: Callable[[Any], Any] = _identity
    from_db: Callable[[Any], Any] = _identity

    uuid_pk = is_key_field and key_id_strategy == "uuid4"
    if uuid_pk:
        sa_type = LargeBinary(16)
        to_db = _uuid_to_db
        from_db = _uuid_from_db

    encrypted = bool(metadata.get("db_encrypted", False))
    if encrypted:
        if encrypt is None or decrypt is None:
            raise ConfigurationError(
                f"Field '{name}' is marked db_field(encrypted=True) but no "
                "encryption_key was configured on database_registry(...)."
            )
        sa_type = String(DEFAULT_VARCHAR_LENGTH)
        to_db = encrypt
        from_db = decrypt
        # Ciphertext is opaque; do not try to coerce it back to the declared type.
        validate = _identity

    return FieldSpec(
        name=name,
        annotation=annotation,
        python_type=unwrap_annotation(annotation),
        nullable=nullable,
        required=required,
        default=default,
        primary_key=bool(metadata.get("db_primary_key", False)),
        autoincrement=bool(metadata.get("db_autoincrement", False)),
        unique=bool(metadata.get("db_unique", False)),
        index=bool(metadata.get("db_index", False)),
        foreign_key=metadata.get("db_foreign_key"),
        hash_password=bool(metadata.get("db_hash_password", False)),
        encrypted=encrypted,
        exclude_from_db=bool(metadata.get("db_exclude_from_db", False)),
        id_strategy=metadata.get("db_id_strategy"),
        length=metadata.get("db_length"),
        precision=metadata.get("db_precision"),
        scale=metadata.get("db_scale"),
        timezone=metadata.get("db_timezone"),
        column_type=metadata.get("db_column_type"),
        sa_type=sa_type,
        validate=validate,
        to_db=to_db,
        from_db=from_db,
    )


# ---------------------------------------------------------------------------
# UUID primary-key codec
# ---------------------------------------------------------------------------


def _uuid_to_db(value: Any) -> Any:
    """UUID -> 16 raw bytes for a ``LargeBinary(16)`` primary key column."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value.bytes
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return uuid.UUID(value).bytes
    return value


def _uuid_from_db(value: Any) -> Any:
    """16 raw bytes -> UUID when hydrating a model."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    if isinstance(value, (bytes, bytearray)):
        return uuid.UUID(bytes=bytes(value))
    if isinstance(value, str):
        return uuid.UUID(value)
    return value
