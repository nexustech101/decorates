"""Contract tests for ``registers.db.specs``.

These tests were written from the Phase 1 interface specification alone, without
reading the implementation, so that a deviation from the documented contract
shows up as a failing test instead of being mirrored into the suite.
"""

from __future__ import annotations

import dataclasses
import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional

import pytest
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
from sqlalchemy.types import TypeEngine

from registers.db.exceptions import ConfigurationError, RegistryError
from registers.db.specs import (
    MISSING,
    FieldCoercionError,
    FieldSpec,
    build_field_spec,
    build_validator,
    resolve_codec,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def coercer(annotation: Any):
    """The coercer half of ``resolve_codec``."""
    return resolve_codec(annotation)[0]


def sa_type(annotation: Any) -> TypeEngine:
    """The SQLAlchemy type half of ``resolve_codec``."""
    return resolve_codec(annotation)[1]


def make_spec(
    name: str = "value",
    annotation: Any = int,
    *,
    nullable: bool = False,
    required: bool = True,
    default: Any = MISSING,
    metadata: dict[str, Any] | None = None,
    is_key_field: bool = False,
    key_id_strategy: str | None = None,
    encrypt: Any = None,
    decrypt: Any = None,
) -> FieldSpec:
    return build_field_spec(
        name,
        annotation,
        nullable=nullable,
        required=required,
        default=default,
        metadata={} if metadata is None else metadata,
        is_key_field=is_key_field,
        key_id_strategy=key_id_strategy,
        encrypt=encrypt,
        decrypt=decrypt,
    )


def bare_spec(**overrides: Any) -> FieldSpec:
    kwargs: dict[str, Any] = dict(
        name="value",
        annotation=int,
        python_type=int,
        nullable=False,
        required=True,
    )
    kwargs.update(overrides)
    return FieldSpec(**kwargs)


class Widget:
    """An arbitrary, unregistered class -> JSON fallback."""


class Level(enum.Enum):
    LOW = 1
    HIGH = 7


class Colour(enum.Enum):
    RED = "crimson"
    BLUE = "azure"


class Priority(enum.IntEnum):
    """An Enum subclass whose member values are all int, via the int mixin."""

    MINOR = 3
    MAJOR = 9


class Method(str, enum.Enum):
    """An Enum subclass with non-int member values, via the str mixin."""

    GET = "fetch"
    POST = "submit"


SENTINEL = object()


# --------------------------------------------------------------------------- #
# FieldCoercionError
# --------------------------------------------------------------------------- #


def test_field_coercion_error_is_a_value_error():
    assert issubclass(FieldCoercionError, ValueError)


def test_field_coercion_error_is_not_a_registry_error():
    assert not issubclass(FieldCoercionError, RegistryError)
    assert not issubclass(FieldCoercionError, ConfigurationError)


def test_field_coercion_error_exposes_value_and_target():
    payload = ["not", "an", "integer"]
    exc = FieldCoercionError(payload, "an integer")
    assert exc.value is payload
    assert exc.target == "an integer"


def test_field_coercion_error_str_contains_repr_of_value():
    exc = FieldCoercionError("weird\nvalue", "an integer")
    assert repr("weird\nvalue") in str(exc)


def test_field_coercion_error_is_catchable_as_value_error():
    with pytest.raises(ValueError):
        raise FieldCoercionError(object(), "an integer")


# --------------------------------------------------------------------------- #
# MISSING
# --------------------------------------------------------------------------- #


def test_missing_sentinel_is_distinct_from_none():
    assert MISSING is not None
    assert MISSING != None  # noqa: E711 - deliberate identity/equality check


def test_missing_is_the_default_default_and_none_is_storable():
    assert bare_spec().default is MISSING
    assert bare_spec(default=None).default is None


# --------------------------------------------------------------------------- #
# resolve_codec - type mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (bool, Boolean),
        (int, Integer),
        (float, Float),
        (Decimal, Numeric),
        (datetime, DateTime),
        (date, Date),
        (uuid.UUID, String),
        (str, String),
        (bytes, LargeBinary),
        (list[int], JSON),
        (dict[str, Any], JSON),
        (Widget, JSON),
    ],
)
def test_resolve_codec_type_mapping(annotation, expected):
    assert isinstance(sa_type(annotation), expected)


def test_str_default_length_is_255():
    assert sa_type(str).length == 255


def test_uuid_default_is_string_of_length_36():
    resolved = sa_type(uuid.UUID)
    assert isinstance(resolved, String)
    assert resolved.length == 36
    assert resolved.length == len(str(uuid.uuid4()))


def test_resolve_codec_returns_type_instances_not_classes():
    for annotation in (bool, int, float, Decimal, datetime, date, str, bytes, uuid.UUID):
        resolved = sa_type(annotation)
        assert isinstance(resolved, TypeEngine), annotation
        assert not isinstance(resolved, type), annotation


# --- traps: subclass ordering ---------------------------------------------- #


def test_bool_resolves_to_boolean_not_integer():
    """bool is a subclass of int; a naive isinstance chain maps it to Integer."""
    resolved = sa_type(bool)
    assert isinstance(resolved, Boolean)
    assert not isinstance(resolved, Integer)


def test_datetime_resolves_to_datetime_not_date():
    """datetime is a subclass of date; ordering must put datetime first."""
    resolved = sa_type(datetime)
    assert isinstance(resolved, DateTime)
    assert not isinstance(resolved, Date)


def test_date_resolves_to_date_not_datetime():
    resolved = sa_type(date)
    assert isinstance(resolved, Date)
    assert not isinstance(resolved, DateTime)


# --- Optional unwrapping ---------------------------------------------------- #


@pytest.mark.parametrize(
    "annotation",
    [Optional[int], int | None, Optional[str], str | None, Optional[datetime]],
)
def test_optional_resolves_like_the_inner_type(annotation):
    inner = [a for a in getattr(annotation, "__args__", ()) if a is not type(None)][0]
    optional_coerce, optional_type = resolve_codec(annotation)
    plain_coerce, plain_type = resolve_codec(inner)
    assert type(optional_type) is type(plain_type)
    assert optional_type.__class__ is plain_type.__class__
    sample = {int: "12", str: 12, datetime: "2020-01-02T03:04:05"}[inner]
    assert optional_coerce(sample) == plain_coerce(sample)


def test_optional_string_keeps_the_same_length():
    assert sa_type(Optional[str]).length == sa_type(str).length


def test_coercers_do_not_handle_none():
    """None handling belongs to build_validator, not the raw coercer."""
    for annotation in (int, float, Decimal, str, bool, bytes, datetime, date, uuid.UUID):
        with pytest.raises(FieldCoercionError):
            coercer(annotation)(None)


# --------------------------------------------------------------------------- #
# resolve_codec - enums
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("enum_cls", [Level, Priority])
def test_all_int_valued_enum_maps_to_integer(enum_cls):
    assert isinstance(sa_type(enum_cls), Integer)


@pytest.mark.parametrize("enum_cls", [Colour, Method])
def test_non_int_valued_enum_maps_to_string(enum_cls):
    assert isinstance(sa_type(enum_cls), String)


@pytest.mark.parametrize("enum_cls", [Level, Colour, Priority, Method])
def test_enum_coercer_accepts_member_value_and_name(enum_cls):
    coerce = coercer(enum_cls)
    for member in enum_cls:
        assert coerce(member) is member
        assert coerce(member.value) is member
        assert coerce(member.name) is member


@pytest.mark.parametrize(
    ("enum_cls", "bad"),
    [
        (Level, 4),
        (Level, "MEDIUM"),
        (Level, None),
        (Colour, "green"),
        (Colour, 1),
        (Colour, ["crimson"]),
        (Priority, 0),
        (Priority, 999),
        (Method, "delete"),
        (Method, 999),
    ],
)
def test_enum_coercer_rejects_unknown_values(enum_cls, bad):
    with pytest.raises(FieldCoercionError):
        coercer(enum_cls)(bad)


# --------------------------------------------------------------------------- #
# bool coercer
# --------------------------------------------------------------------------- #


def test_bool_coercer_passes_through_booleans():
    coerce = coercer(bool)
    assert coerce(True) is True
    assert coerce(False) is False


@pytest.mark.parametrize(("value", "expected"), [(1, True), (0, False)])
def test_bool_coercer_accepts_zero_and_one(value, expected):
    assert coercer(bool)(value) is expected


@pytest.mark.parametrize("text", ["true", "1", "yes", "on"])
def test_bool_coercer_truthy_strings(text):
    coerce = coercer(bool)
    assert coerce(text) is True
    assert coerce(text.upper()) is True
    assert coerce(f"  {text}  ") is True


@pytest.mark.parametrize("text", ["false", "0", "no", "off"])
def test_bool_coercer_falsy_strings(text):
    coerce = coercer(bool)
    assert coerce(text) is False
    assert coerce(text.upper()) is False
    assert coerce(f"\t{text}\n") is False


@pytest.mark.parametrize("bad", [2, -1, "maybe", [], {}, object()])
def test_bool_coercer_rejects_non_boolean_values(bad):
    with pytest.raises(FieldCoercionError):
        coercer(bool)(bad)


# --------------------------------------------------------------------------- #
# int coercer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value", [0, 5, -17, 3.0, -8.0, "42", "-42", Decimal("7"), Decimal("-7")]
)
def test_int_coercer_accepts_integral_values(value):
    result = coercer(int)(value)
    assert isinstance(result, int)
    assert not isinstance(result, bool)
    assert result == int(value)


def test_int_coercer_rejects_non_integral_float():
    with pytest.raises(FieldCoercionError):
        coercer(int)(3.5)


@pytest.mark.parametrize("bad", [True, False])
def test_int_coercer_rejects_bool(bad):
    """TRAP: bool is an int subclass; True must not silently become 1."""
    with pytest.raises(FieldCoercionError):
        coercer(int)(bad)


@pytest.mark.parametrize("bad", ["abc", "", None, object(), [], {}, [1]])
def test_int_coercer_rejects_garbage(bad):
    with pytest.raises(FieldCoercionError):
        coercer(int)(bad)


# --------------------------------------------------------------------------- #
# float coercer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [3, -3, 2.5, Decimal("1.25"), "2.5", "-0.5"])
def test_float_coercer_accepts_numeric_values(value):
    result = coercer(float)(value)
    assert isinstance(result, float)
    assert result == float(value)


@pytest.mark.parametrize("bad", [True, False, "abc", object(), []])
def test_float_coercer_rejects_bool_and_garbage(bad):
    with pytest.raises(FieldCoercionError):
        coercer(float)(bad)


# --------------------------------------------------------------------------- #
# Decimal coercer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [Decimal("1.25"), 7, -7, "1.25", "-0.5"])
def test_decimal_coercer_accepts_exact_values(value):
    result = coercer(Decimal)(value)
    assert isinstance(result, Decimal)
    assert result == Decimal(str(value))


def test_decimal_coercer_of_float_avoids_binary_noise():
    """TRAP: Decimal(0.1) carries float noise; the coercer must go via str()."""
    coerce = coercer(Decimal)
    result = coerce(0.1)
    assert isinstance(result, Decimal)
    assert result == Decimal("0.1")
    assert result != Decimal(0.1)
    assert str(result) == "0.1"


def test_decimal_coercer_float_round_trip_for_several_values():
    coerce = coercer(Decimal)
    for value in (0.1, 0.2, 2.675, -1.1):
        assert coerce(value) == Decimal(str(value))
        assert coerce(value) != Decimal(value)


@pytest.mark.parametrize("bad", [True, False, "abc", object(), []])
def test_decimal_coercer_rejects_bool_and_garbage(bad):
    with pytest.raises(FieldCoercionError):
        coercer(Decimal)(bad)


# --------------------------------------------------------------------------- #
# str coercer
# --------------------------------------------------------------------------- #


def test_str_coercer_passes_strings_through_unchanged():
    coerce = coercer(str)
    text = "already a string"
    assert coerce(text) is text


@pytest.mark.parametrize(
    "value",
    [
        5,
        -5,
        2.5,
        Decimal("1.25"),
        True,
        False,
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        datetime(2020, 1, 2, 3, 4, 5),
        date(2020, 1, 2),
    ],
)
def test_str_coercer_stringifies_scalars(value):
    result = coercer(str)(value)
    assert isinstance(result, str)
    assert result == str(value)


@pytest.mark.parametrize(
    "bad", [None, [], {}, (), set(), [1, 2], {"a": 1}, (1,), {1, 2}, b"bytes"]
)
def test_str_coercer_rejects_none_and_containers(bad):
    """TRAP: silently stringifying a container is a caller bug, not formatting."""
    with pytest.raises(FieldCoercionError):
        coercer(str)(bad)


# --------------------------------------------------------------------------- #
# bytes coercer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value", [b"payload", bytearray(b"payload"), memoryview(b"payload")]
)
def test_bytes_coercer_accepts_binary_buffers(value):
    result = coercer(bytes)(value)
    assert isinstance(result, bytes)
    assert result == b"payload"


def test_bytes_coercer_encodes_str_as_utf8():
    text = "héllo"
    result = coercer(bytes)(text)
    assert isinstance(result, bytes)
    assert result == text.encode("utf-8")
    assert result.decode("utf-8") == text


@pytest.mark.parametrize("bad", [5, 0, 3.5])
def test_bytes_coercer_rejects_int(bad):
    with pytest.raises(FieldCoercionError):
        coercer(bytes)(bad)


# --------------------------------------------------------------------------- #
# datetime coercer
# --------------------------------------------------------------------------- #


def test_datetime_coercer_passes_datetime_through():
    coerce = coercer(datetime)
    moment = datetime(2020, 1, 2, 3, 4, 5)
    assert coerce(moment) == moment


def test_datetime_coercer_widens_date_to_midnight():
    result = coercer(datetime)(date(2020, 1, 2))
    assert isinstance(result, datetime)
    assert result.date() == date(2020, 1, 2)
    assert result.time() == time(0, 0, 0)


def test_datetime_coercer_parses_iso_strings():
    coerce = coercer(datetime)
    moment = datetime(2020, 1, 2, 3, 4, 5)
    assert coerce(moment.isoformat()) == moment


@pytest.mark.parametrize("bad", ["not-a-datetime", "02/01/2020", "", "2020-13-45"])
def test_datetime_coercer_rejects_non_iso_strings(bad):
    with pytest.raises(FieldCoercionError):
        coercer(datetime)(bad)


# --------------------------------------------------------------------------- #
# date coercer
# --------------------------------------------------------------------------- #


def test_date_coercer_passes_date_through():
    coerce = coercer(date)
    day = date(2020, 1, 2)
    assert coerce(day) == day


def test_date_coercer_narrows_datetime_via_date():
    """TRAP: datetime is a date subclass; it must be narrowed, not passed through."""
    result = coercer(date)(datetime(2020, 1, 2, 3, 4, 5))
    assert not isinstance(result, datetime)
    assert isinstance(result, date)
    assert result == date(2020, 1, 2)


def test_date_coercer_parses_iso_strings():
    coerce = coercer(date)
    day = date(2020, 1, 2)
    assert coerce(day.isoformat()) == day


@pytest.mark.parametrize("bad", ["not-a-date", "02/01/2020", "", "2020-13-45"])
def test_date_coercer_rejects_non_iso_strings(bad):
    with pytest.raises(FieldCoercionError):
        coercer(date)(bad)


# --------------------------------------------------------------------------- #
# uuid coercer
# --------------------------------------------------------------------------- #


def test_uuid_coercer_passes_uuid_through():
    coerce = coercer(uuid.UUID)
    value = uuid.uuid4()
    assert coerce(value) == value


def test_uuid_coercer_accepts_hyphenated_string():
    value = uuid.uuid4()
    text = str(value)
    assert len(text) == 36
    assert coercer(uuid.UUID)(text) == value


@pytest.mark.parametrize("wrap", [bytes, bytearray])
def test_uuid_coercer_accepts_exactly_16_bytes(wrap):
    value = uuid.uuid4()
    raw = wrap(value.bytes)
    assert len(raw) == 16
    assert coercer(uuid.UUID)(raw) == value


@pytest.mark.parametrize("length", [0, 1, 15, 17, 32, 36])
def test_uuid_coercer_rejects_wrong_length_bytes(length):
    """TRAP: only a 16-byte buffer is a UUID; other lengths must not sneak through."""
    with pytest.raises(FieldCoercionError):
        coercer(uuid.UUID)(b"\x01" * length)


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "1234", 5, []])
def test_uuid_coercer_rejects_malformed_input(bad):
    with pytest.raises(FieldCoercionError):
        coercer(uuid.UUID)(bad)


# --------------------------------------------------------------------------- #
# JSON fallback coercers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("annotation", [list[int], dict[str, Any], Widget])
@pytest.mark.parametrize(
    "value", [[1, 2], {"a": 1}, "anything", 5, None, ["mixed", 1, None]]
)
def test_json_fallback_coercer_is_identity(annotation, value):
    """TRAP: JSON fallback must not validate or copy - it returns the input."""
    assert coercer(annotation)(value) is value


# --------------------------------------------------------------------------- #
# build_validator
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("annotation", [int, str, bool, datetime, uuid.UUID])
def test_validator_allows_none_when_nullable(annotation):
    assert build_validator(annotation, nullable=True)(None) is None


@pytest.mark.parametrize("annotation", [int, str, bool, datetime, uuid.UUID])
def test_validator_rejects_none_when_not_nullable(annotation):
    with pytest.raises(FieldCoercionError):
        build_validator(annotation, nullable=False)(None)


@pytest.mark.parametrize("nullable", [True, False])
def test_validator_delegates_non_none_values_to_the_coercer(nullable):
    validate = build_validator(int, nullable=nullable)
    assert validate("42") == coercer(int)("42") == 42
    with pytest.raises(FieldCoercionError):
        validate("abc")


@pytest.mark.parametrize("nullable", [True, False])
def test_validator_still_rejects_bool_for_int_fields(nullable):
    with pytest.raises(FieldCoercionError):
        build_validator(int, nullable=nullable)(True)


@pytest.mark.parametrize("nullable", [True, False])
@pytest.mark.parametrize("annotation", [Widget, list[int], dict[str, Any]])
def test_json_fallback_validator_is_identity_and_accepts_none(nullable, annotation):
    validate = build_validator(annotation, nullable=nullable)
    assert validate(None) is None
    assert validate(SENTINEL) is SENTINEL
    assert validate([1, "two"]) == [1, "two"]


@pytest.mark.parametrize("nullable", [True, False])
def test_optional_annotation_validator_matches_inner_annotation(nullable):
    optional = build_validator(Optional[int], nullable=nullable)
    plain = build_validator(int, nullable=nullable)
    assert optional("13") == plain("13") == 13


# --------------------------------------------------------------------------- #
# FieldSpec
# --------------------------------------------------------------------------- #


def test_field_spec_requires_only_the_five_core_arguments():
    spec = FieldSpec("value", int, int, False, True)
    assert (spec.name, spec.annotation, spec.python_type) == ("value", int, int)
    assert spec.nullable is False
    assert spec.required is True


def test_field_spec_defaults():
    spec = bare_spec()
    assert spec.default is MISSING
    assert spec.primary_key is False
    assert spec.autoincrement is False
    assert spec.unique is False
    assert spec.index is False
    assert spec.foreign_key is None
    assert spec.hash_password is False
    assert spec.encrypted is False
    assert spec.exclude_from_db is False
    assert spec.id_strategy is None
    assert spec.length is None
    assert spec.precision is None
    assert spec.scale is None
    assert spec.timezone is None
    assert spec.column_type is None
    assert isinstance(spec.sa_type, JSON)


def test_field_spec_default_codecs_are_identity():
    spec = bare_spec()
    for func in (spec.validate, spec.to_db, spec.from_db):
        assert func(SENTINEL) is SENTINEL
        assert func(None) is None


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("name", "renamed"),
        ("nullable", True),
        ("primary_key", True),
        ("sa_type", String(10)),
        ("default", 5),
    ],
)
def test_field_spec_is_frozen(attribute, value):
    spec = bare_spec()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(spec, attribute, value)


@pytest.mark.parametrize("excluded", [True, False])
def test_is_stored_is_the_negation_of_exclude_from_db(excluded):
    spec = bare_spec(exclude_from_db=excluded)
    assert spec.is_stored is (not excluded)


# --------------------------------------------------------------------------- #
# build_field_spec - basics and metadata unpacking
# --------------------------------------------------------------------------- #


def test_build_field_spec_threads_core_arguments():
    spec = make_spec("age", int, nullable=True, required=False, default=7)
    assert spec.name == "age"
    assert spec.annotation is int
    assert spec.nullable is True
    assert spec.required is False
    assert spec.default == 7


def test_empty_metadata_yields_all_default_flags():
    spec = make_spec("age", int)
    reference = bare_spec()
    for attribute in (
        "primary_key",
        "autoincrement",
        "unique",
        "index",
        "foreign_key",
        "hash_password",
        "encrypted",
        "exclude_from_db",
        "id_strategy",
        "length",
        "precision",
        "scale",
        "timezone",
        "column_type",
    ):
        assert getattr(spec, attribute) == getattr(reference, attribute), attribute


def test_metadata_keys_are_unpacked_onto_matching_attributes():
    metadata = {
        "db_primary_key": True,
        "db_autoincrement": True,
        "db_unique": True,
        "db_index": True,
        "db_foreign_key": "other.id",
        "db_hash_password": True,
        "db_exclude_from_db": True,
        "db_id_strategy": "manual",
    }
    spec = make_spec("age", int, metadata=metadata)
    for key, value in metadata.items():
        assert getattr(spec, key[len("db_") :]) == value, key


# --------------------------------------------------------------------------- #
# build_field_spec - type parameters
# --------------------------------------------------------------------------- #


def test_db_length_applies_to_string_fields():
    spec = make_spec("name", str, metadata={"db_length": 512})
    assert isinstance(spec.sa_type, String)
    assert spec.sa_type.length == 512
    assert spec.length == 512


def test_string_without_db_length_keeps_the_default_length():
    spec = make_spec("name", str)
    assert isinstance(spec.sa_type, String)
    assert spec.sa_type.length == sa_type(str).length == 255


def test_db_precision_and_scale_apply_to_decimal_fields():
    spec = make_spec("amount", Decimal, metadata={"db_precision": 12, "db_scale": 4})
    assert isinstance(spec.sa_type, Numeric)
    assert spec.sa_type.precision == 12
    assert spec.sa_type.scale == 4


def test_db_timezone_applies_to_datetime_fields():
    spec = make_spec("created", datetime, metadata={"db_timezone": True})
    assert isinstance(spec.sa_type, DateTime)
    assert spec.sa_type.timezone is True


def test_db_length_applies_to_bytes_fields():
    spec = make_spec("blob", bytes, metadata={"db_length": 4096})
    assert isinstance(spec.sa_type, LargeBinary)
    assert spec.sa_type.length == 4096


# --------------------------------------------------------------------------- #
# build_field_spec - db_column_type override
# --------------------------------------------------------------------------- #


def test_column_type_instance_is_used_as_is():
    custom = Numeric(18, 6)
    spec = make_spec("amount", str, metadata={"db_column_type": custom, "db_length": 32})
    assert spec.sa_type is custom


def test_column_type_subclass_is_instantiated():
    spec = make_spec("amount", str, metadata={"db_column_type": LargeBinary})
    assert isinstance(spec.sa_type, LargeBinary)
    assert not isinstance(spec.sa_type, type)


def test_column_type_zero_arg_callable_is_called():
    spec = make_spec("amount", str, metadata={"db_column_type": lambda: Numeric(9, 3)})
    assert isinstance(spec.sa_type, Numeric)
    assert (spec.sa_type.precision, spec.sa_type.scale) == (9, 3)


@pytest.mark.parametrize("bad", ["VARCHAR(10)", 42, object(), ["Numeric"]])
def test_invalid_column_type_raises_configuration_error(bad):
    with pytest.raises(ConfigurationError):
        make_spec("amount", str, metadata={"db_column_type": bad})


# --------------------------------------------------------------------------- #
# build_field_spec - uuid primary key
# --------------------------------------------------------------------------- #


def uuid_key_spec() -> FieldSpec:
    return make_spec("id", uuid.UUID, is_key_field=True, key_id_strategy="uuid4")


def test_uuid4_key_field_uses_16_byte_large_binary():
    """TRAP: a uuid4 key is stored as 16 raw bytes, not as String(36)."""
    spec = uuid_key_spec()
    assert isinstance(spec.sa_type, LargeBinary)
    assert not isinstance(spec.sa_type, String)
    assert spec.sa_type.length == 16
    assert spec.sa_type.length == len(uuid.uuid4().bytes)


def test_uuid4_key_field_to_db_produces_raw_bytes():
    spec = uuid_key_spec()
    value = uuid.uuid4()
    stored = spec.to_db(value)
    assert isinstance(stored, bytes)
    assert len(stored) == 16
    assert stored == value.bytes


def test_uuid4_key_field_from_db_restores_the_uuid():
    spec = uuid_key_spec()
    value = uuid.uuid4()
    assert spec.from_db(value.bytes) == value


def test_uuid4_key_field_round_trips():
    spec = uuid_key_spec()
    for _ in range(5):
        value = uuid.uuid4()
        assert spec.from_db(spec.to_db(value)) == value


def test_uuid4_key_field_passes_none_through_both_directions():
    spec = uuid_key_spec()
    assert spec.to_db(None) is None
    assert spec.from_db(None) is None


def test_uuid4_key_field_to_db_accepts_strings_and_bytes():
    spec = uuid_key_spec()
    value = uuid.uuid4()
    assert spec.to_db(str(value)) == value.bytes
    assert spec.to_db(value.bytes) == value.bytes
    assert spec.from_db(spec.to_db(str(value))) == value


def test_non_key_uuid_field_keeps_string36_and_identity_codecs():
    spec = make_spec("ref", uuid.UUID, is_key_field=False, key_id_strategy="uuid4")
    value = uuid.uuid4()
    assert isinstance(spec.sa_type, String)
    assert spec.sa_type.length == 36
    assert spec.to_db(value) is value
    assert spec.from_db(value) is value


@pytest.mark.parametrize("strategy", ["manual", "autoincrement", None])
def test_uuid_key_field_with_other_strategy_keeps_string36(strategy):
    spec = make_spec("id", uuid.UUID, is_key_field=True, key_id_strategy=strategy)
    value = uuid.uuid4()
    assert isinstance(spec.sa_type, String)
    assert spec.sa_type.length == 36
    assert spec.to_db(value) is value
    assert spec.from_db(value) is value


# --------------------------------------------------------------------------- #
# build_field_spec - encryption
# --------------------------------------------------------------------------- #


def encryption_spec(annotation: Any = int, **overrides: Any) -> FieldSpec:
    kwargs: dict[str, Any] = dict(
        metadata={"db_encrypted": True},
        encrypt=lambda v: f"ENC:{v}",
        decrypt=lambda v: v[len("ENC:") :],
    )
    kwargs.update(overrides)
    return make_spec("secret", annotation, **kwargs)


def test_encrypted_field_uses_supplied_encrypt_and_decrypt():
    spec = encryption_spec()
    assert spec.to_db(41) == "ENC:41"
    assert spec.from_db("ENC:41") == "41"
    assert spec.from_db(spec.to_db("plaintext")) == "plaintext"


def test_encrypted_field_stores_as_string():
    spec = encryption_spec()
    assert isinstance(spec.sa_type, String)


def test_encrypted_field_validate_is_identity():
    """Ciphertext must not be coerced back to the declared type."""
    spec = encryption_spec(int)
    assert spec.validate("ENC:41") == "ENC:41"
    assert spec.validate(SENTINEL) is SENTINEL


def test_encrypted_field_marks_the_encrypted_flag():
    assert encryption_spec().encrypted is True


@pytest.mark.parametrize(
    ("encrypt", "decrypt"),
    [(None, None), (lambda v: v, None), (None, lambda v: v)],
)
def test_encrypted_field_without_codecs_raises_configuration_error(encrypt, decrypt):
    with pytest.raises(ConfigurationError):
        make_spec(
            "secret",
            int,
            metadata={"db_encrypted": True},
            encrypt=encrypt,
            decrypt=decrypt,
        )


def test_unencrypted_field_ignores_supplied_codecs():
    spec = make_spec("plain", int, encrypt=lambda v: f"ENC:{v}", decrypt=lambda v: v)
    assert spec.to_db(41) == 41
    assert spec.from_db(41) == 41


# --------------------------------------------------------------------------- #
# build_field_spec - validator threading and python_type
# --------------------------------------------------------------------------- #


def test_nullable_false_is_threaded_into_validate():
    spec = make_spec("age", int, nullable=False)
    with pytest.raises(FieldCoercionError):
        spec.validate(None)


def test_nullable_true_is_threaded_into_validate():
    spec = make_spec("age", Optional[int], nullable=True)
    assert spec.validate(None) is None
    assert spec.validate("9") == 9


def test_spec_validate_delegates_to_the_declared_coercer():
    spec = make_spec("age", int, nullable=True)
    assert spec.validate("9") == 9
    with pytest.raises(FieldCoercionError):
        spec.validate("nine")
    with pytest.raises(FieldCoercionError):
        spec.validate(True)


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (int, int),
        (Optional[int], int),
        (int | None, int),
        (str | None, str),
        (Optional[datetime], datetime),
        (Optional[uuid.UUID], uuid.UUID),
    ],
)
def test_python_type_is_the_optional_unwrapped_type(annotation, expected):
    assert make_spec("value", annotation, nullable=True).python_type is expected
