"""
The model-layer boundary.

Everything the manager needs from a model class goes through a
:class:`ModelAdapter`: enumerate fields, construct an instance, read it back as a
flat dict, mutate an attribute, and copy with updates. That is the complete list —
five operations.

Why this exists
---------------
Pydantic is excellent at validation and expensive at scale: a five-field
``BaseModel`` instance measures ~1,184 bytes against ~304 for the equivalent
``@dataclass``. For a service that hydrates tens of thousands of rows, that
difference is the difference between fitting in memory and not.

Putting an adapter here means the manager never imports ``pydantic`` and never
calls ``model_dump``/``model_validate``/``model_fields`` directly, so a second
implementation backed by stdlib dataclasses can be dropped in without the
persistence logic noticing. Existing Pydantic models keep working unchanged.

Deliberately *not* a validation framework
-----------------------------------------
Adapters do not reimplement Pydantic. Per-field coercion already lives in
:mod:`registers.db.specs`, which is shared by both implementations. An adapter's
only job is to know how a particular flavour of model class is introspected,
constructed, and read.
"""

from __future__ import annotations

from dataclasses import MISSING as DATACLASS_MISSING, fields as dataclass_fields, is_dataclass
from typing import Any, Iterator, Mapping, Protocol, get_type_hints, runtime_checkable

from registers.db.exceptions import ModelRegistrationError
from registers.db.specs import MISSING


class FieldView:
    """
    Uniform view of one declared field, whatever the underlying model flavour.

    Mirrors the subset of ``pydantic.fields.FieldInfo`` the registry actually uses,
    so the rest of the codebase can stop special-casing where a field came from.
    """

    __slots__ = ("name", "annotation", "default", "default_factory")

    def __init__(
        self,
        name: str,
        annotation: Any,
        default: Any = MISSING,
        default_factory: Any = None,
    ) -> None:
        self.name = name
        self.annotation = annotation
        self.default = default
        self.default_factory = default_factory

    def is_required(self) -> bool:
        """True when the caller must supply a value."""
        return self.default is MISSING and self.default_factory is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FieldView({self.name!r}, {self.annotation!r}, required={self.is_required()})"


@runtime_checkable
class ModelAdapter(Protocol):
    """The five operations the manager needs from a model class."""

    model_cls: type

    def fields(self) -> Mapping[str, FieldView]:
        """Declared fields, in declaration order."""

    def field_metadata(self, name: str) -> Mapping[str, Any]:
        """Normalized ``db_*`` metadata for one field (empty when none)."""

    def construct(self, values: Mapping[str, Any]) -> Any:
        """Build a validated instance."""

    def to_dict(self, instance: Any) -> dict[str, Any]:
        """
        Storage-ready field-name -> value mapping over the declared fields.

        Shallow with respect to *plain* values — a ``list[str]`` or ``dict`` is
        passed through rather than copied. Nested **models** must be converted to
        plain dicts, because they land in JSON columns and neither a nested
        dataclass nor a nested ``BaseModel`` is JSON-serializable.
        """

    def hydrate(self, values: Mapping[str, Any]) -> Any:
        """Build an instance from trusted database values."""

    def set(self, instance: Any, name: str, value: Any) -> None:
        """Assign an attribute, bypassing any validate-on-assignment behavior."""

    def copy_with(self, instance: Any, **updates: Any) -> Any:
        """Return a copy with *updates* applied."""


def _to_storable(value: Any) -> Any:
    """
    Convert a value into something a database column can accept.

    Only nested dataclasses (and containers holding them) are rewritten; every
    other value is returned by identity, so no copying happens on the hot write
    path. This is the dataclass counterpart to Pydantic's ``model_dump()``
    recursion, and exists because ``json.dumps`` cannot serialize a dataclass
    instance — a nested model would otherwise blow up at INSERT.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _to_storable(getattr(value, f.name)) for f in dataclass_fields(value)
        }
    if isinstance(value, (list, tuple)):
        converted = [_to_storable(item) for item in value]
        # Preserve identity when nothing needed converting.
        if all(a is b for a, b in zip(converted, value)) and len(converted) == len(value):
            return value
        return converted
    if isinstance(value, dict):
        converted_map = {k: _to_storable(v) for k, v in value.items()}
        if all(converted_map[k] is value[k] for k in value):
            return value
        return converted_map
    return value


def _from_storable(value: Any, annotation: Any) -> Any:
    """Rebuild a nested dataclass from the plain dict a JSON column returns."""
    if isinstance(value, dict) and isinstance(annotation, type) and is_dataclass(annotation):
        names = {f.name for f in dataclass_fields(annotation)}
        return annotation(**{k: v for k, v in value.items() if k in names})
    return value


# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------


class PydanticAdapter:
    """
    Adapter for ``pydantic.BaseModel`` subclasses.

    Reproduces the behavior the registry had before the adapter boundary existed,
    including using ``model_dump()`` *without* ``mode="json"`` so that
    ``datetime``/``date``/``Decimal`` reach SQLAlchemy as native Python objects
    rather than strings.
    """

    def __init__(self, model_cls: type) -> None:
        self.model_cls = model_cls
        self._fields: dict[str, FieldView] | None = None
        self._metadata: dict[str, Mapping[str, Any]] | None = None

    def fields(self) -> Mapping[str, FieldView]:
        if self._fields is None:
            self._fields = {
                name: FieldView(
                    name=name,
                    annotation=info.annotation,
                    default=MISSING if info.is_required() else info.default,
                    default_factory=getattr(info, "default_factory", None),
                )
                for name, info in self.model_cls.model_fields.items()
            }
        return self._fields

    def field_metadata(self, name: str) -> Mapping[str, Any]:
        if self._metadata is None:
            from registers.db.fields import get_db_field_metadata

            self._metadata = {
                field_name: get_db_field_metadata(info)
                for field_name, info in self.model_cls.model_fields.items()
            }
        return self._metadata.get(name, {})

    def construct(self, values: Mapping[str, Any]) -> Any:
        return self.model_cls(**dict(values))

    def hydrate(self, values: Mapping[str, Any]) -> Any:
        return self.model_cls.model_validate(dict(values))

    def to_dict(self, instance: Any) -> dict[str, Any]:
        return instance.model_dump()

    def set(self, instance: Any, name: str, value: Any) -> None:
        # object.__setattr__ bypasses validate_assignment, matching the behavior
        # the manager has always relied on when stamping generated keys.
        object.__setattr__(instance, name, value)

    def copy_with(self, instance: Any, **updates: Any) -> Any:
        return instance.model_copy(update=updates)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class DataclassAdapter:
    """
    Adapter for stdlib ``@dataclass`` model classes.

    Field metadata is carried in the standard ``dataclasses.field(metadata=...)``
    mapping under the ``"registers"`` key, which is a better fit than Pydantic's
    ``json_schema_extra``: it is the documented extension point, it is typed, and
    it does not leak database concerns into a model's JSON schema.

    Validation is performed by the per-field coercers in
    :mod:`registers.db.specs`, installed by the manager. ``construct`` applies them
    so that a dataclass model gets the same write-time type checking a Pydantic
    model does.

    Do **not** use ``slots=True`` on a registered model: the manager stamps an
    identity attribute onto instances and relationship prefetch caches results the
    same way, neither of which a slotted class can hold.
    """

    #: Key under which db_field metadata is stored in ``field(metadata=...)``.
    METADATA_KEY = "registers"

    def __init__(self, model_cls: type) -> None:
        if not is_dataclass(model_cls):
            raise ModelRegistrationError(
                f"'{model_cls.__name__}' is not a dataclass.",
                model=model_cls.__name__,
            )
        if getattr(model_cls, "__slots__", None):
            raise ModelRegistrationError(
                f"Model '{model_cls.__name__}' uses __slots__, which is not supported. "
                "The registry stamps identity onto instances and relationship "
                "prefetch caches loaded rows on them; a slotted class cannot hold "
                "either. Remove slots=True from the @dataclass decorator.",
                model=model_cls.__name__,
            )
        self.model_cls = model_cls
        self._fields: dict[str, FieldView] | None = None
        self._validators: dict[str, Any] = {}
        self._hydrate_types: dict[str, Any] = {}

    def fields(self) -> Mapping[str, FieldView]:
        if self._fields is None:
            resolved = self._resolved_hints()
            views: dict[str, FieldView] = {}
            for f in dataclass_fields(self.model_cls):
                views[f.name] = FieldView(
                    name=f.name,
                    annotation=resolved.get(f.name, f.type),
                    default=MISSING if f.default is DATACLASS_MISSING else f.default,
                    default_factory=(
                        None if f.default_factory is DATACLASS_MISSING else f.default_factory
                    ),
                )
            self._fields = views
        return self._fields

    def _resolved_hints(self) -> dict[str, Any]:
        """
        Resolve annotations to real types.

        ``dataclasses.Field.type`` is whatever was written in the class body. Under
        ``from __future__ import annotations`` — on by default in much modern code —
        that is the *string* ``"int | None"``, not the type. Column-type resolution
        and coercion both need the real object, so evaluate the hints once here.

        Falls back to the raw values if a forward reference cannot be resolved; the
        JSON codec then handles the field, which is the same outcome as any other
        unrecognized annotation.
        """
        try:
            return get_type_hints(self.model_cls, include_extras=True)
        except Exception:
            return {}

    def field_metadata(self, name: str) -> Mapping[str, Any]:
        for f in dataclass_fields(self.model_cls):
            if f.name == name:
                return dict(f.metadata.get(self.METADATA_KEY, {}))
        return {}

    def bind_validators(
        self,
        validators: Mapping[str, Any],
        python_types: Mapping[str, Any] | None = None,
    ) -> None:
        """
        Install per-field validators (supplied by the manager from its FieldSpecs).

        Called once at registration. Until then ``construct`` performs no coercion,
        which is only the case during registration itself.

        *python_types* enables the hydration fast path: on a read the database has
        already returned correctly-typed values for the overwhelming majority of
        columns, so an ``isinstance`` check is far cheaper than a coercer call.
        """
        self._validators = dict(validators)
        self._hydrate_types = {
            name: t
            for name, t in (python_types or {}).items()
            # bool is a subclass of int, so an isinstance fast path would wave
            # True through an int column. Always coerce those.
            if isinstance(t, type) and t is not bool and not issubclass(t, bool)
        }

    def construct(self, values: Mapping[str, Any]) -> Any:
        data = dict(values)
        self._apply_defaults(data)
        for name, value in list(data.items()):
            validate = self._validators.get(name)
            if validate is not None:
                data[name] = validate(value)
        return self.model_cls(**data)

    def hydrate(self, values: Mapping[str, Any]) -> Any:
        # Rows come back through each field's from_db codec before reaching here, so
        # values are already the right Python types. Still run validate to normalize
        # driver quirks (e.g. SQLite handing back a str for a DATETIME column).
        fields = self.fields()
        data = {k: v for k, v in values.items() if k in fields}
        self._apply_defaults(data)
        hydrate_types = self._hydrate_types
        validators = self._validators
        for name, value in list(data.items()):
            expected = hydrate_types.get(name)
            # Fast path: the driver already handed back the declared type, which is
            # the case for nearly every column on nearly every row.
            if expected is not None and type(value) is expected:
                continue
            annotation = fields[name].annotation
            if isinstance(value, dict) and isinstance(annotation, type) and is_dataclass(annotation):
                data[name] = _from_storable(value, annotation)
                continue
            validate = validators.get(name)
            if validate is None:
                continue
            try:
                data[name] = validate(value)
            except Exception:
                # Trust the database over the coercer on read; a stored value that
                # no longer matches the annotation is a schema-drift problem, not
                # something to crash a SELECT over.
                data[name] = value
        return self.model_cls(**data)

    def to_dict(self, instance: Any) -> dict[str, Any]:
        """
        Shallow, storage-ready mapping of declared fields.

        Deliberately *not* ``dataclasses.asdict()``: that deep-copies everything and
        recurses unconditionally. Here plain values are passed through by reference
        (no copy), and only nested dataclasses are converted — because a nested
        dataclass in a JSON column is not JSON-serializable and would otherwise fail
        at insert time. This mirrors what ``model_dump()`` does for the Pydantic
        adapter, keeping the two flavours at parity.
        """
        return {
            name: _to_storable(getattr(instance, name)) for name in self.fields()
        }

    def set(self, instance: Any, name: str, value: Any) -> None:
        object.__setattr__(instance, name, value)

    def copy_with(self, instance: Any, **updates: Any) -> Any:
        data = self.to_dict(instance)
        data.update(updates)
        return self.model_cls(**data)

    def _apply_defaults(self, data: dict[str, Any]) -> None:
        for name, view in self.fields().items():
            if name in data:
                continue
            if view.default_factory is not None:
                data[name] = view.default_factory()
            elif view.default is not MISSING:
                data[name] = view.default


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def adapter_for(model_cls: type) -> ModelAdapter:
    """
    Return the right adapter for *model_cls*.

    Pydantic is probed first and imported lazily, so that a project using only
    dataclass models never pays to import it.
    """
    if not isinstance(model_cls, type):
        raise ModelRegistrationError(
            "@database_registry can only decorate classes."
        )

    try:
        from pydantic import BaseModel
    except ImportError:  # pragma: no cover - pydantic is an extra
        BaseModel = None  # type: ignore[assignment]

    if BaseModel is not None and issubclass(model_cls, BaseModel):
        if hasattr(model_cls, "__dataclass_fields__"):
            raise ModelRegistrationError(
                "Do not combine stdlib @dataclass with pydantic.BaseModel. "
                "Define the model as a plain `class User(BaseModel): ...`."
            )
        return PydanticAdapter(model_cls)

    if is_dataclass(model_cls):
        return DataclassAdapter(model_cls)

    raise ModelRegistrationError(
        "@database_registry requires a pydantic.BaseModel subclass or a "
        f"stdlib @dataclass. '{model_cls.__name__}' is neither."
    )


def iter_field_names(adapter: ModelAdapter) -> Iterator[str]:
    """Convenience iterator over declared field names."""
    return iter(adapter.fields())
