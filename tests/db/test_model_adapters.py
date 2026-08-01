"""
tests/db/test_model_adapters.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Independent verification of the Phase 4-5 interface spec:

* ``FieldView``
* ``adapter_for`` dispatch
* ``PydanticAdapter`` / ``DataclassAdapter`` behavioural parity
* ``DataclassAdapter`` specifics (slots rejection, validator binding)
* ``dc_field``
* end-to-end manager parity over pydantic *and* dataclass models
* pydantic being an optional dependency

NOTE: deliberately no ``from __future__ import annotations`` here -- the postponed
annotation behaviour is exercised explicitly in its own test via a synthesised module.
"""

import dataclasses
import os
import subprocess
import sys
import textwrap
import types
import uuid
from dataclasses import dataclass, field as dc_stdlib_field, is_dataclass
from pathlib import Path
from typing import Any, Mapping, get_args, get_origin

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import LargeBinary

import registers
from registers.db import (
    Agg,
    ConfigurationError,
    DatabaseRegistry,
    InvalidQueryError,
    ManyToOne,
    ModelRegistrationError,
    OneToMany,
    RecordNotFoundError,
    db_field,
    dc_field,
    dispose_all,
)
from registers.db.adapters import (
    DataclassAdapter,
    FieldView,
    PydanticAdapter,
    adapter_for,
)
from registers.db.specs import MISSING

FLAVOURS = ["pydantic", "dataclass"]


def db_url(tmp_path, name="test"):
    return "sqlite:///" + (Path(tmp_path) / f"{name}.db").as_posix()


@pytest.fixture(autouse=True)
def _dispose_engines_local():
    yield
    dispose_all()


# ===========================================================================
# Part 1 -- FieldView
# ===========================================================================


class TestFieldView:
    def test_attributes_are_readable(self):
        factory = list
        view = FieldView("tags", list, default=MISSING, default_factory=factory)

        assert view.name == "tags"
        assert view.annotation is list
        assert view.default is MISSING
        assert view.default_factory is factory

    def test_defaults_of_optional_constructor_arguments(self):
        view = FieldView("email", str)

        assert view.default is MISSING
        assert view.default_factory is None

    @pytest.mark.parametrize(
        ("default", "default_factory", "expected"),
        [
            (MISSING, None, True),
            (0, None, False),
            (MISSING, list, False),
            (0, list, False),
        ],
        ids=["no-default", "default-only", "factory-only", "both"],
    )
    def test_is_required_only_when_no_default_and_no_factory(
        self, default, default_factory, expected
    ):
        view = FieldView("f", int, default=default, default_factory=default_factory)

        assert view.is_required() is expected

    def test_none_default_still_counts_as_a_default(self):
        # ``None`` is a perfectly good default and must not be confused with MISSING.
        assert FieldView("f", int, default=None).is_required() is False

    def test_uses_slots_so_stray_attributes_are_rejected(self):
        view = FieldView("f", int)

        with pytest.raises(AttributeError):
            view.not_a_declared_attribute = 1

        assert not hasattr(view, "__dict__")


# ===========================================================================
# Part 2 -- adapter_for dispatch
# ===========================================================================


class _PlainPydantic(BaseModel):
    id: int = 0


@dataclass
class _PlainDataclass:
    id: int = 0


class _NotAModel:
    id = 0


def _a_function():  # pragma: no cover - never called
    return None


class TestAdapterFor:
    def test_pydantic_model_gets_pydantic_adapter(self):
        adapter = adapter_for(_PlainPydantic)

        assert isinstance(adapter, PydanticAdapter)
        assert not isinstance(adapter, DataclassAdapter)

    def test_dataclass_gets_dataclass_adapter(self):
        adapter = adapter_for(_PlainDataclass)

        assert isinstance(adapter, DataclassAdapter)
        assert not isinstance(adapter, PydanticAdapter)

    def test_plain_class_is_rejected(self):
        with pytest.raises(ModelRegistrationError):
            adapter_for(_NotAModel)

    @pytest.mark.parametrize(
        "value",
        [_a_function, 5, "UserD", None, [1, 2], _PlainDataclass(id=1)],
        ids=["function", "int", "str", "none", "list", "instance"],
    )
    def test_non_class_is_rejected(self, value):
        with pytest.raises(ModelRegistrationError):
            adapter_for(value)

    def test_dataclass_plus_basemodel_is_rejected_with_explanatory_message(self):
        @dataclass
        class Hybrid(BaseModel):
            id: int = 0

        with pytest.raises(ModelRegistrationError) as excinfo:
            adapter_for(Hybrid)

        message = str(excinfo.value).lower()
        assert "combin" in message, message
        assert "dataclass" in message and "pydantic" in message, message


# ===========================================================================
# Part 3 -- adapter behavioural parity
# ===========================================================================


class UserP(BaseModel):
    id: "int | None" = None
    email: str = ""
    tags: list = []


@dataclass
class UserD:
    id: "int | None" = None
    email: str = ""
    tags: list = dataclasses.field(default_factory=list)


@dataclass
class Nested:
    value: int = 0


class BoxP(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list = []
    nested: Any = None


@dataclass
class BoxD:
    items: list = dataclasses.field(default_factory=list)
    nested: Any = None


class TaggedP(BaseModel):
    email: str = db_field(unique=True, index=True, default="")
    plain: str = ""


@dataclass
class TaggedD:
    email: str = dc_field(unique=True, index=True, default="")
    plain: str = ""


class FrozenP(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""


@dataclass(frozen=True)
class FrozenD:
    name: str = ""


USER_MODELS = pytest.mark.parametrize("model_cls", [UserP, UserD], ids=FLAVOURS)
BOX_MODELS = pytest.mark.parametrize("model_cls", [BoxP, BoxD], ids=FLAVOURS)
FROZEN_MODELS = pytest.mark.parametrize("model_cls", [FrozenP, FrozenD], ids=FLAVOURS)


class TestAdapterParity:
    @USER_MODELS
    def test_fields_returns_declared_names_in_declaration_order(self, model_cls):
        views = adapter_for(model_cls).fields()

        assert list(views) == ["id", "email", "tags"]
        assert all(isinstance(view, FieldView) for view in views.values())
        assert [view.name for view in views.values()] == ["id", "email", "tags"]

    @USER_MODELS
    def test_fields_annotations_are_resolved_type_objects(self, model_cls):
        views = adapter_for(model_cls).fields()

        assert not any(isinstance(view.annotation, str) for view in views.values())
        assert views["email"].annotation is str
        assert views["tags"].annotation is list
        assert set(get_args(views["id"].annotation)) == {int, type(None)}

    @USER_MODELS
    def test_field_metadata_is_an_empty_mapping_for_undecorated_fields(self, model_cls):
        metadata = adapter_for(model_cls).field_metadata("email")

        assert isinstance(metadata, Mapping)
        assert not metadata
        assert dict(metadata) == {}

    def test_field_metadata_matches_across_flavours_for_decorated_fields(self):
        pydantic_meta = dict(adapter_for(TaggedP).field_metadata("email"))
        dataclass_meta = dict(adapter_for(TaggedD).field_metadata("email"))

        assert pydantic_meta, "db_field metadata was not surfaced by PydanticAdapter"
        assert pydantic_meta == dataclass_meta
        assert pydantic_meta.get("db_unique") is True
        assert pydantic_meta.get("db_index") is True
        assert not adapter_for(TaggedD).field_metadata("plain")

    @USER_MODELS
    def test_construct_applies_declared_defaults(self, model_cls):
        instance = adapter_for(model_cls).construct({"email": "a@example.com"})

        assert isinstance(instance, model_cls)
        assert instance.email == "a@example.com"
        assert instance.id is None
        assert instance.tags == []

    @USER_MODELS
    def test_construct_calls_default_factory_per_instance(self, model_cls):
        adapter = adapter_for(model_cls)

        first = adapter.construct({})
        second = adapter.construct({})
        first.tags.append("x")

        assert first.tags is not second.tags
        assert second.tags == []

    @BOX_MODELS
    def test_to_dict_converts_nested_models_to_plain_dicts(self, model_cls):
        """
        Nested models must come back as plain dicts, for both flavours.

        They land in JSON columns, and neither a nested dataclass nor a nested
        BaseModel is JSON-serializable — leaving them intact fails at INSERT.
        See ``test_nested_model_round_trips_through_json_column`` for the
        end-to-end consequence.
        """
        instance = model_cls(items=["a"], nested=Nested(value=7))
        adapter = adapter_for(model_cls)

        data = adapter.to_dict(instance)

        assert set(data) == {"items", "nested"}
        assert data["nested"] == {"value": 7}
        assert not is_dataclass(data["nested"])
        assert data["items"] == ["a"]

    def test_dataclass_to_dict_passes_plain_values_through_without_copying(self):
        """
        Plain values are returned by reference — no per-row copying on writes.

        Only nested models get rewritten. Pydantic's ``model_dump()`` copies
        containers unconditionally, so this stronger guarantee is asserted for the
        dataclass adapter alone rather than as a parity requirement.
        """
        instance = BoxD(items=["a"], nested=Nested(value=7))
        data = adapter_for(BoxD).to_dict(instance)

        assert data["items"] is instance.items

    @USER_MODELS
    def test_to_dict_covers_exactly_the_declared_fields(self, model_cls):
        adapter = adapter_for(model_cls)
        instance = adapter.construct({"id": 3, "email": "b@example.com", "tags": ["t"]})

        assert adapter.to_dict(instance) == {
            "id": 3,
            "email": "b@example.com",
            "tags": ["t"],
        }

    @USER_MODELS
    def test_hydrate_builds_instance_and_ignores_unknown_keys(self, model_cls):
        adapter = adapter_for(model_cls)

        instance = adapter.hydrate(
            {"id": 5, "email": "c@example.com", "tags": ["z"], "leftover_column": 1}
        )

        assert isinstance(instance, model_cls)
        assert (instance.id, instance.email, instance.tags) == (5, "c@example.com", ["z"])
        assert not hasattr(instance, "leftover_column")

    @USER_MODELS
    def test_hydrate_fills_defaults_for_absent_columns(self, model_cls):
        instance = adapter_for(model_cls).hydrate({"id": 9})

        assert instance.id == 9
        assert instance.email == ""
        assert instance.tags == []

    @FROZEN_MODELS
    def test_set_assigns_even_when_the_model_forbids_assignment(self, model_cls):
        adapter = adapter_for(model_cls)
        instance = adapter.construct({"name": "before"})

        # Precondition: plain assignment really is forbidden on this model.
        with pytest.raises(Exception):
            instance.name = "direct"
        assert instance.name == "before"

        adapter.set(instance, "name", "after")

        assert instance.name == "after"

    @USER_MODELS
    def test_set_assigns_on_ordinary_models(self, model_cls):
        adapter = adapter_for(model_cls)
        instance = adapter.construct({})

        adapter.set(instance, "id", 11)

        assert instance.id == 11

    @USER_MODELS
    def test_copy_with_returns_a_new_object_leaving_the_original_alone(self, model_cls):
        adapter = adapter_for(model_cls)
        original = adapter.construct({"id": 1, "email": "old@example.com", "tags": ["a"]})

        copy = adapter.copy_with(original, email="new@example.com")

        assert copy is not original
        assert isinstance(copy, model_cls)
        assert copy.email == "new@example.com"
        assert copy.id == 1
        assert copy.tags == ["a"]
        assert original.email == "old@example.com"

    @USER_MODELS
    def test_copy_with_no_updates_still_returns_a_distinct_object(self, model_cls):
        adapter = adapter_for(model_cls)
        original = adapter.construct({"id": 2, "email": "x@example.com"})

        copy = adapter.copy_with(original)

        assert copy is not original
        assert adapter.to_dict(copy) == adapter.to_dict(original)


# --- Part 3.2 -- postponed annotations (high value) -------------------------

_POSTPONED_SOURCE = textwrap.dedent(
    """
    from __future__ import annotations

    from dataclasses import dataclass, field

    from pydantic import BaseModel


    @dataclass
    class PostponedD:
        id: int | None = None
        email: str = ""
        tags: list = field(default_factory=list)


    class PostponedP(BaseModel):
        id: int | None = None
        email: str = ""
        tags: list = []
    """
)


@pytest.fixture()
def postponed_module():
    name = "registers_postponed_annotation_fixture"
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module
    try:
        exec(compile(_POSTPONED_SOURCE, f"<{name}>", "exec"), module.__dict__)
        yield module
    finally:
        sys.modules.pop(name, None)


class TestPostponedAnnotations:
    def test_dataclass_field_types_really_are_strings(self, postponed_module):
        # Guards against this whole test class passing trivially.
        raw = {f.name: f.type for f in dataclasses.fields(postponed_module.PostponedD)}

        assert all(isinstance(value, str) for value in raw.values()), raw
        assert raw["id"] == "int | None"

    @pytest.mark.parametrize("model_name", ["PostponedP", "PostponedD"], ids=FLAVOURS)
    def test_adapter_resolves_postponed_annotations_to_real_types(
        self, postponed_module, model_name
    ):
        model_cls = getattr(postponed_module, model_name)

        views = adapter_for(model_cls).fields()

        assert list(views) == ["id", "email", "tags"]
        assert not any(isinstance(view.annotation, str) for view in views.values())
        assert views["email"].annotation is str
        assert views["tags"].annotation is list
        assert set(get_args(views["id"].annotation)) == {int, type(None)}
        assert get_origin(views["id"].annotation) is not None

    def test_postponed_dataclass_still_constructs_and_round_trips(self, postponed_module):
        adapter = adapter_for(postponed_module.PostponedD)

        instance = adapter.construct({"id": 4})

        assert adapter.to_dict(instance) == {"id": 4, "email": "", "tags": []}


# ===========================================================================
# Part 4 -- DataclassAdapter specifics
# ===========================================================================


def _int_coercer(value):
    return int(value)


def _always_raises(value):
    raise ValueError(f"nope: {value!r}")


@dataclass
class Person:
    name: str = ""
    age: int = 0


class TestDataclassAdapterSpecifics:
    def test_rejects_non_dataclass(self):
        with pytest.raises(ModelRegistrationError):
            DataclassAdapter(_NotAModel)

    def test_rejects_pydantic_model(self):
        with pytest.raises(ModelRegistrationError):
            DataclassAdapter(_PlainPydantic)

    def test_rejects_slotted_dataclass_mentioning_slots(self):
        @dataclass(slots=True)
        class Slotted:
            name: str = ""
            age: int = 0

        # Precondition: this class genuinely uses __slots__.
        assert "__slots__" in Slotted.__dict__
        assert not hasattr(Slotted(), "__dict__")

        with pytest.raises(ModelRegistrationError) as excinfo:
            DataclassAdapter(Slotted)

        assert "slot" in str(excinfo.value).lower(), str(excinfo.value)

    def test_bind_validators_single_argument_form_coerces_on_construct(self):
        adapter = DataclassAdapter(Person)
        adapter.bind_validators({"age": _int_coercer})

        instance = adapter.construct({"age": "42"})

        assert instance.age == 42
        assert isinstance(instance.age, int)

    def test_bind_validators_with_python_types_form_coerces_on_construct(self):
        adapter = DataclassAdapter(Person)
        adapter.bind_validators({"age": _int_coercer}, {"age": int})

        instance = adapter.construct({"age": "42"})

        assert instance.age == 42
        assert isinstance(instance.age, int)

    def test_hydrate_is_forgiving_and_keeps_the_raw_stored_value(self):
        adapter = DataclassAdapter(Person)
        adapter.bind_validators({"age": _always_raises})

        instance = adapter.hydrate({"name": "Alice", "age": "drifted"})

        assert isinstance(instance, Person)
        assert instance.age == "drifted"
        assert instance.name == "Alice"

    def test_hydrate_forgiveness_does_not_swallow_good_values(self):
        adapter = DataclassAdapter(Person)
        adapter.bind_validators({"age": _int_coercer})

        instance = adapter.hydrate({"name": "Bob", "age": "7"})

        assert instance.age == 7
        assert isinstance(instance.age, int)

    def test_construct_is_not_forgiving_and_propagates_validator_errors(self):
        adapter = DataclassAdapter(Person)
        adapter.bind_validators({"age": _always_raises})

        with pytest.raises(ValueError):
            adapter.construct({"name": "Alice", "age": 3})

    def test_validators_are_scoped_to_the_adapter_instance(self):
        strict = DataclassAdapter(Person)
        strict.bind_validators({"age": _always_raises})
        lenient = DataclassAdapter(Person)

        assert lenient.construct({"age": 5}).age == 5
        with pytest.raises(ValueError):
            strict.construct({"age": 5})


# ===========================================================================
# Part 5 -- dc_field
# ===========================================================================


class TestDcField:
    def test_exported_from_both_public_locations(self):
        assert registers.dc_field is dc_field

    def test_usable_as_dataclass_default(self):
        @dataclass
        class Model:
            count: int = dc_field(default=5)
            name: str = dc_field(default="x", unique=True)

        fields = {f.name: f for f in dataclasses.fields(Model)}

        assert dataclasses.is_dataclass(Model)
        assert fields["count"].default == 5
        assert fields["name"].default == "x"
        assert (Model().count, Model().name) == (5, "x")

    def test_default_factory_is_supported_and_not_shared(self):
        @dataclass
        class Model:
            tags: list = dc_field(default_factory=list, index=True)

        first, second = Model(), Model()
        first.tags.append("a")

        assert dataclasses.fields(Model)[0].default_factory is list
        assert second.tags == []
        assert first.tags is not second.tags

    def test_metadata_lands_under_registers_key_with_db_prefixed_names(self):
        @dataclass
        class Model:
            id: str = dc_field(
                default="",
                primary_key=True,
                unique=True,
                index=True,
                foreign_key="users.id",
                id_strategy="uuid4",
                length=32,
                exclude_from_db=False,
            )

        metadata = dataclasses.fields(Model)[0].metadata["registers"]

        assert metadata["db_unique"] is True
        assert metadata["db_index"] is True
        assert metadata["db_foreign_key"] == "users.id"
        assert metadata["db_id_strategy"] == "uuid4"
        assert metadata["db_primary_key"] is True
        assert metadata["db_length"] == 32
        assert all(key.startswith("db_") for key in metadata), sorted(metadata)

    def test_user_metadata_is_preserved_alongside_registers_key(self):
        @dataclass
        class Model:
            name: str = dc_field(default="", unique=True, metadata={"custom": "kept"})

        metadata = dataclasses.fields(Model)[0].metadata

        assert metadata["custom"] == "kept"
        assert metadata["registers"]["db_unique"] is True

    def test_undecorated_field_has_no_registers_metadata(self):
        @dataclass
        class Model:
            name: str = ""

        assert "registers" not in dataclasses.fields(Model)[0].metadata

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"foreign_key": "nodot"},
            {"id_strategy": "bogus"},
            {"length": 0},
            {"length": -1},
            {"timezone": "yes"},
            {"unique": "yes"},
        ],
        ids=["fk-no-dot", "bad-id-strategy", "length-0", "length-neg", "tz-str", "unique-str"],
    )
    @pytest.mark.parametrize("factory", [db_field, dc_field], ids=["db_field", "dc_field"])
    def test_invalid_options_raise_configuration_error(self, factory, kwargs):
        with pytest.raises(ConfigurationError):
            factory(default=None, **kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"primary_key": True},
            {"autoincrement": True},
            {"unique": True},
            {"index": True},
            {"foreign_key": "users.id"},
            {"hash_password": True},
            {"id_strategy": "uuid4"},
            {"length": 1},
            {"precision": 10},
            {"scale": 2},
            {"timezone": True},
            {"column_type": LargeBinary(64)},
            {"exclude_from_db": True},
            {"encrypted": True},
        ],
        ids=[
            "primary_key",
            "autoincrement",
            "unique",
            "index",
            "foreign_key",
            "hash_password",
            "id_strategy",
            "length",
            "precision",
            "scale",
            "timezone",
            "column_type",
            "exclude_from_db",
            "encrypted",
        ],
    )
    def test_dc_field_accepts_the_same_option_set_as_db_field(self, kwargs):
        db_field(default=None, **kwargs)  # baseline: db_field accepts it

        produced = dc_field(default=None, **kwargs)

        assert isinstance(produced, dataclasses.Field)
        assert "registers" in produced.metadata

    def test_dc_field_accepts_the_whole_option_set_at_once(self):
        produced = dc_field(
            default=None,
            primary_key=True,
            unique=True,
            index=True,
            foreign_key="users.id",
            id_strategy="uuid4",
            length=64,
            precision=10,
            scale=2,
            timezone=True,
            exclude_from_db=False,
            encrypted=False,
        )

        assert produced.metadata["registers"]["db_foreign_key"] == "users.id"


# ===========================================================================
# Part 6 -- end-to-end manager parity
# ===========================================================================


@pytest.fixture(params=FLAVOURS)
def flavour(request):
    return request.param


def make_user_model(flavour, db, url, *, table_name="users", **registry_kwargs):
    """Equivalent user models for both flavours."""
    if flavour == "pydantic":

        @db.database_registry(url, table_name=table_name, key_field="id", **registry_kwargs)
        class User(BaseModel):
            id: "int | None" = db_field(
                primary_key=True, id_strategy="autoincrement", default=None
            )
            name: str = ""
            email: str = ""
            age: int = 0

        return User

    @db.database_registry(url, table_name=table_name, key_field="id", **registry_kwargs)
    @dataclass
    class User:
        id: "int | None" = dc_field(
            primary_key=True, id_strategy="autoincrement", default=None
        )
        name: str = ""
        email: str = ""
        age: int = 0

    return User


def make_uuid_model(flavour, db, url, *, table_name="accounts"):
    if flavour == "pydantic":

        @db.database_registry(url, table_name=table_name, key_field="id")
        class Account(BaseModel):
            id: "uuid.UUID | None" = db_field(
                primary_key=True, id_strategy="uuid4", default=None
            )
            label: str = ""

        return Account

    @db.database_registry(url, table_name=table_name, key_field="id")
    @dataclass
    class Account:
        id: "uuid.UUID | None" = dc_field(
            primary_key=True, id_strategy="uuid4", default=None
        )
        label: str = ""

    return Account


def make_excluded_model(flavour, db, url, *, table_name="widgets"):
    if flavour == "pydantic":

        @db.database_registry(url, table_name=table_name, key_field="id")
        class Widget(BaseModel):
            id: "int | None" = db_field(
                primary_key=True, id_strategy="autoincrement", default=None
            )
            name: str = ""
            scratch: str = db_field(default="", exclude_from_db=True)

        return Widget

    @db.database_registry(url, table_name=table_name, key_field="id")
    @dataclass
    class Widget:
        id: "int | None" = dc_field(
            primary_key=True, id_strategy="autoincrement", default=None
        )
        name: str = ""
        scratch: str = dc_field(default="", exclude_from_db=True)

    return Widget


def make_related_models(flavour, db, url):
    if flavour == "pydantic":

        @db.database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: "int | None" = db_field(
                primary_key=True, id_strategy="autoincrement", default=None
            )
            name: str = ""

        @db.database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: "int | None" = db_field(
                primary_key=True, id_strategy="autoincrement", default=None
            )
            author_id: int = db_field(foreign_key="authors.id", default=0)
            title: str = ""

    else:

        @db.database_registry(url, table_name="authors", key_field="id")
        @dataclass
        class Author:
            id: "int | None" = dc_field(
                primary_key=True, id_strategy="autoincrement", default=None
            )
            name: str = ""

        @db.database_registry(url, table_name="posts", key_field="id")
        @dataclass
        class Post:
            id: "int | None" = dc_field(
                primary_key=True, id_strategy="autoincrement", default=None
            )
            author_id: int = dc_field(foreign_key="authors.id", default=0)
            title: str = ""

    Author.posts = OneToMany(Post, foreign_key="author_id")
    Post.author = ManyToOne(Author, local_key="author_id")
    return Author, Post


class TestManagerParity:
    def test_create_returns_instance_with_generated_id(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))

        first = User.objects.create(name="Alice", email="a@example.com", age=30)
        second = User.objects.create(name="Bob", email="b@example.com", age=40)

        assert isinstance(first, User)
        assert isinstance(first.id, int) and isinstance(second.id, int)
        assert first.id != second.id
        assert second.id > first.id
        assert first.name == "Alice"

    def test_require_get_and_missing_row_semantics(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))
        created = User.objects.create(name="Alice", email="a@example.com", age=30)

        fetched = User.objects.require(created.id)

        assert (fetched.id, fetched.name, fetched.age) == (created.id, "Alice", 30)
        assert User.objects.get(created.id + 999) is None
        with pytest.raises(RecordNotFoundError):
            User.objects.require(created.id + 999)

    def test_save_persists_and_refresh_rereads(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))
        user = User.objects.create(name="Alice", email="a@example.com", age=30)

        user.name = "Alicia"
        user.save()

        assert User.objects.require(user.id).name == "Alicia"

        # ``refresh()`` hands back a freshly read instance (repo convention:
        # ``fresh = user.refresh()``); both flavours must agree on that.
        other = User.objects.require(user.id)
        other.age = 31
        other.save()
        assert user.age == 30

        fresh = user.refresh()

        assert isinstance(fresh, User)
        assert (fresh.id, fresh.name, fresh.age) == (user.id, "Alicia", 31)

    def test_delete_removes_row_and_count_reflects_it(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))
        keep = User.objects.create(name="Keep", email="k@example.com", age=20)
        drop = User.objects.create(name="Drop", email="d@example.com", age=21)

        assert User.objects.count() == 2

        drop.delete()

        assert User.objects.count() == 1
        assert User.objects.get(drop.id) is None
        assert User.objects.require(keep.id).name == "Keep"

    def test_filter_with_lookup_and_ordering(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))
        for name, age in [("A", 10), ("B", 30), ("C", 20), ("D", 40)]:
            User.objects.create(name=name, email=f"{name}@example.com", age=age)

        rows = User.objects.filter(age__gte=20, order_by="-age")

        assert [row.name for row in rows] == ["D", "B", "C"]
        assert [row.age for row in rows] == [40, 30, 20]
        assert [row.name for row in User.objects.filter(age__gte=20, order_by="age")] == [
            "C",
            "B",
            "D",
        ]

    def test_filter_with_non_numeric_value_on_int_field_raises(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))
        User.objects.create(name="Alice", email="a@example.com", age=30)

        with pytest.raises(InvalidQueryError):
            User.objects.filter(age="abc")

    def test_write_time_type_checking_rejects_non_numeric_string(self, flavour, tmp_path):
        """High value: a bad value must never reach an integer column."""
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))

        with pytest.raises(ValueError):
            User.objects.create(name="Alice", email="a@example.com", age="not-an-int")

        assert User.objects.count() == 0

    def test_write_time_coercion_stores_real_int(self, flavour, tmp_path):
        """High value: the string "42" must land in the column as int 42."""
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))

        created = User.objects.create(name="Alice", email="a@example.com", age="42")

        assert created.age == 42
        assert isinstance(created.age, int)
        assert not isinstance(created.age, str)

        reloaded = User.objects.require(created.id)
        assert reloaded.age == 42
        assert isinstance(reloaded.age, int)
        assert not isinstance(reloaded.age, str)
        assert [row.id for row in User.objects.filter(age__gte=42)] == [created.id]

        # SQLite is dynamically typed, so a string would happily survive in the
        # column: check the stored value itself, not just the round-tripped model.
        stored = User.objects.raw_dicts("SELECT age FROM users")
        assert [row["age"] for row in stored] == [42]
        assert isinstance(stored[0]["age"], int)
        assert not isinstance(stored[0]["age"], str)

    def test_upsert_updates_in_place_for_unique_fields(self, flavour, tmp_path):
        User = make_user_model(
            flavour, DatabaseRegistry(), db_url(tmp_path), unique_fields=["email"]
        )
        original = User.objects.create(name="Alice", email="a@example.com", age=30)

        updated = User.objects.upsert(email="a@example.com", name="Alicia", age=31)

        assert User.objects.count() == 1
        assert updated.id == original.id
        assert User.objects.require(original.id).name == "Alicia"
        assert User.objects.require(original.id).age == 31

    def test_bulk_create_returns_stamped_instances(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))

        created = User.objects.bulk_create(
            [
                {"name": "A", "email": "a@example.com", "age": 1},
                {"name": "B", "email": "b@example.com", "age": 2},
                {"name": "C", "email": "c@example.com", "age": 3},
            ]
        )

        ids = [row.id for row in created]
        assert len(created) == 3
        assert all(isinstance(row, User) for row in created)
        assert all(isinstance(value, int) for value in ids)
        assert len(set(ids)) == 3
        assert User.objects.count() == 3
        assert sorted(row.name for row in created) == ["A", "B", "C"]

    def test_uuid_primary_key_round_trips(self, flavour, tmp_path):
        Account = make_uuid_model(flavour, DatabaseRegistry(), db_url(tmp_path))

        account = Account.objects.create(label="primary")
        other = Account.objects.create(label="secondary")

        assert isinstance(account.id, uuid.UUID)
        assert account.id != other.id
        assert Account.objects.require(account.id).label == "primary"
        matched = Account.objects.filter(id__in=[account.id])
        assert [row.label for row in matched] == ["primary"]

    def test_projection_grouping_and_aggregation(self, flavour, tmp_path):
        User = make_user_model(flavour, DatabaseRegistry(), db_url(tmp_path))
        User.objects.create(name="admin", email="a@example.com", age=10)
        User.objects.create(name="admin", email="b@example.com", age=20)
        User.objects.create(name="user", email="c@example.com", age=60)

        rows = sorted(User.objects.select("name", "age"), key=lambda row: row["age"])

        assert all(isinstance(row, dict) for row in rows)
        assert rows == [
            {"name": "admin", "age": 10},
            {"name": "admin", "age": 20},
            {"name": "user", "age": 60},
        ]
        assert User.objects.count_by("name") == {"admin": 2, "user": 1}
        assert User.objects.aggregate(Agg.avg("age")) == 30

    def test_exclude_from_db_field_has_no_column_but_stays_on_instance(
        self, flavour, tmp_path
    ):
        Widget = make_excluded_model(flavour, DatabaseRegistry(), db_url(tmp_path))

        columns = Widget.objects.column_names()

        assert "name" in columns
        assert "scratch" not in columns

        widget = Widget.objects.create(name="w", scratch="transient")
        assert widget.scratch == "transient"
        assert Widget.objects.require(widget.id).name == "w"

    def test_relationships_resolve_across_models(self, flavour, tmp_path):
        Author, Post = make_related_models(flavour, DatabaseRegistry(), db_url(tmp_path))

        alice = Author.objects.create(name="Alice")
        bob = Author.objects.create(name="Bob")
        Post.objects.create(author_id=alice.id, title="First")
        Post.objects.create(author_id=alice.id, title="Second")
        bobs_post = Post.objects.create(author_id=bob.id, title="Bob's only")

        assert sorted(post.title for post in alice.posts) == ["First", "Second"]
        assert [post.title for post in bob.posts] == ["Bob's only"]
        assert bobs_post.author.id == bob.id
        assert bobs_post.author.name == "Bob"


# ===========================================================================
# Part 7 -- pydantic is optional (runs in a subprocess)
# ===========================================================================


_NO_PYDANTIC_SCRIPT = textwrap.dedent(
    '''
    import sys

    class _BlockPydantic:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "pydantic" or fullname.startswith("pydantic."):
                raise ImportError("No module named 'pydantic' (blocked by test)")
            return None

    sys.meta_path.insert(0, _BlockPydantic())
    for _name in [n for n in sys.modules if n == "pydantic" or n.startswith("pydantic.")]:
        del sys.modules[_name]

    try:
        import pydantic
    except ImportError:
        pass
    else:
        raise AssertionError("pydantic blocker did not work")

    import registers
    from dataclasses import dataclass
    from registers.db import ConfigurationError, DatabaseRegistry, db_field, dc_field

    assert "pydantic" not in sys.modules, "importing registers pulled in pydantic"

    db = DatabaseRegistry()

    @db.database_registry({url!r}, table_name="users", key_field="id")
    @dataclass
    class User:
        id: "int | None" = dc_field(primary_key=True, id_strategy="autoincrement", default=None)
        name: str = ""
        age: int = 0

    alice = User.objects.create(name="Alice", age=30)
    assert isinstance(alice.id, int), alice.id
    assert User.objects.require(alice.id).name == "Alice"

    alice.name = "Alicia"
    alice.save()
    assert User.objects.require(alice.id).name == "Alicia"

    assert [u.name for u in User.objects.filter(age__gte=30)] == ["Alicia"]
    assert User.objects.count() == 1
    alice.delete()
    assert User.objects.count() == 0

    try:
        db_field(unique=True, default=None)
    except ConfigurationError as exc:
        message = str(exc)
        assert "pydantic" in message.lower(), message
        assert "dc_field" in message, message
    except ImportError as exc:
        raise AssertionError("db_field raised a bare ImportError: %r" % (exc,))
    else:
        raise AssertionError("db_field did not raise ConfigurationError without pydantic")

    print("PART7-OK")
    '''
)


def test_registers_works_without_pydantic(tmp_path):
    src_root = Path(registers.__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src_root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env.pop("PYTEST_CURRENT_TEST", None)

    script = _NO_PYDANTIC_SCRIPT.format(
        url="sqlite:///" + (tmp_path / "nopydantic.db").as_posix()
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "PART7-OK" in result.stdout, result.stdout


class AddrP(BaseModel):
    """Module-level so the annotation resolves for both flavours."""

    city: str = ""


@dataclass
class AddrD:
    city: str = ""


class TestNestedModelStorage:
    """
    Nested models must survive a round trip through a JSON column.

    This is the end-to-end consequence of ``to_dict`` converting nested models:
    before it did, a dataclass model with a nested dataclass field raised
    ``TypeError: Object of type ... is not JSON serializable`` at INSERT, while the
    equivalent Pydantic model worked. A pure adapter-level assertion would not have
    caught the divergence, so this drives it through the real manager.
    """

    @pytest.mark.parametrize("flavour", FLAVOURS)
    def test_nested_model_round_trips_through_json_column(self, tmp_path, flavour):
        url = db_url(tmp_path, "nested")
        db = DatabaseRegistry()

        if flavour == "pydantic":
            addr_cls = AddrP

            @db.database_registry(url, table_name="holders")
            class Holder(BaseModel):
                id: "int | None" = db_field(id_strategy="autoincrement", default=None)
                addr: AddrP = AddrP()
        else:
            addr_cls = AddrD

            @db.database_registry(url, table_name="holders")
            @dataclass
            class Holder:
                id: "int | None" = dc_field(id_strategy="autoincrement", default=None)
                addr: AddrD = dc_stdlib_field(default_factory=AddrD)

        created = Holder.objects.create(addr=addr_cls(city="Boston"))
        reloaded = Holder.objects.require(created.id)

        assert isinstance(reloaded.addr, addr_cls)
        assert reloaded.addr.city == "Boston"
