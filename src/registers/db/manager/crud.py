"""
Write path: create, upsert, save, update, delete, and bulk variants.

Also owns model<->row conversion, since serialisation only matters on the way
into and out of a write.
"""

from __future__ import annotations

from datetime import datetime
import logging
import uuid
from typing import Any, Generic, Mapping, TypeVar

from sqlalchemy import (
    delete,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from registers.db.engine import dialect_insert
from registers.db.expressions import F
from registers.db.exceptions import (
    ImmutableFieldError,
    SchemaError,
    StaleDataError,
    InvalidPrimaryKeyAssignmentError,
    InvalidQueryError,
    RecordNotFoundError,
    UniqueConstraintError,
)
from registers.db.manager.context import (
    _ORIGINAL_KEY_ATTR,
    _ORIGINAL_VERSION_ATTR,
)
from registers.db.security import (
    hash_password,
    is_password_hash,
)
from registers.db.specs import (
    FieldCoercionError,
    _identity as _spec_identity,
)

logger = logging.getLogger(__name__)
# Unbound: a model may be a pydantic BaseModel or a stdlib dataclass.
ModelT = TypeVar("ModelT")


def _validate_bool_flag(value: Any) -> None:
    """``field__is_null=`` takes a truthy flag; reject obvious mistakes only."""
    if not isinstance(value, bool) and value not in (0, 1, None):
        raise FieldCoercionError(value, "a boolean")


class _WriteMixin(Generic[ModelT]):
    """Write path: create, upsert, save, update, delete, and bulk variants."""

    def create(self, **data: Any) -> ModelT:
        """
        Strict INSERT.  Raises on duplicate primary key or unique violation.

        Use this when you explicitly want an error if the record already exists.
        """
        data = self._prepare_create_data(dict(data))
        self._call_hook("before_create", data)
        instance = self.adapter.construct(data)
        self._prepare_instance_for_save(instance, is_create=True)
        try:
            with self._connection_scope() as conn:
                created = self._create_with_conn(conn, instance)
                self._audit(conn, "create", created, self._public_model_dump(created))
            self._call_hook("after_create", created)
            self._call_hook("after_save", created)
            return created
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("create", exc)

    def strict_create(self, **data: Any) -> ModelT:
        """Alias for ``create()`` for callers that prefer explicit wording."""
        return self.create(**data)

    def upsert(self, instance: ModelT | None = None, /, **data: Any) -> ModelT:
        """
        INSERT-or-UPDATE by primary key.

        When *autoincrement* is enabled and no primary key is supplied, this
        falls back to a plain ``create()`` so the database generates the ID.

        Atomic: uses ``INSERT … ON CONFLICT DO UPDATE`` — no separate SELECT
        pre-check, eliminating read-then-write race conditions.
        """
        target = instance if instance is not None else self.adapter.construct(data)
        audit_operation = "update" if getattr(target, _ORIGINAL_KEY_ATTR, None) is not None else "upsert"
        self._prepare_instance_for_save(target, is_create=False)
        try:
            with self._connection_scope() as conn:
                saved = self._upsert_with_conn(conn, target)
                self._audit(conn, audit_operation, saved, self._public_model_dump(saved))
            self._call_hook("after_save", saved)
            return saved
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("upsert", exc)

    def save(self, instance: ModelT) -> ModelT:
        """
        Persist *instance* using upsert semantics.

        Policy: an existing row (matched by primary key) is updated; a new
        row is inserted.  The primary key determines which path is taken.
        """
        self._call_hook("before_save", instance)
        return self.upsert(instance)

    def update_where(self, criteria: Mapping[str, Any], **updates: Any) -> list[ModelT]:
        """
        Update all rows matching *criteria* and return the refreshed records.

        Both *criteria* and *updates* are validated against known model fields
        before any SQL is issued.
        """
        criteria = dict(criteria)
        include_deleted = bool(criteria.pop("include_deleted", False))
        if not criteria:
            raise InvalidQueryError(
                "update_where() requires at least one filter criterion.",
                operation="update_where",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        if not updates:
            raise InvalidQueryError(
                "update_where() requires at least one field to update.",
                operation="update_where",
                model=self.model_cls.__name__,
                table=self.table_name,
            )

        # F() values are column expressions, not data: they must skip the value
        # validators and codecs entirely and be compiled against the table instead.
        expressions = {k: v for k, v in updates.items() if isinstance(v, F)}
        plain = {k: v for k, v in updates.items() if not isinstance(v, F)}

        self._assert_known_fields(criteria)
        self._assert_known_update_fields(plain)
        self._assert_known_expression_fields(expressions)
        criteria = self._with_policy_criteria(criteria, include_deleted=include_deleted)
        plain = self._prepare_update_values(dict(plain))
        updates = self._normalize_write_mapping(plain)
        updates.update(
            {name: expression.resolve(self._table) for name, expression in expressions.items()}
        )
        # A bulk update still has to move the version, or an instance loaded before
        # it would save successfully afterwards and silently undo this write.
        version_field = self.version_field
        if version_field is not None and version_field not in updates:
            updates[version_field] = self._table.c[version_field] + 1

        try:
            with self._connection_scope() as conn:
                stmt = update(self._table).values(**updates)
                stmt = self._apply_where(stmt, criteria)

                if getattr(conn.dialect, "update_returning", False):
                    rows = conn.execute(stmt.returning(self._table)).mappings().all()
                    models = [self._row_to_model(row) for row in rows]
                    audit_operation = self._audit_operation_for_updates(updates)
                    for model in models:
                        self._audit(conn, audit_operation, model, dict(updates))
                    return models

                # Fallback for dialects without UPDATE ... RETURNING support.
                key_column = self._table.c[self.key_field]
                key_stmt = select(key_column)
                key_stmt = self._apply_where(key_stmt, criteria)
                affected_keys = conn.execute(key_stmt).scalars().all()
                if not affected_keys:
                    return []

                conn.execute(stmt)
                refresh_stmt = select(self._table).where(key_column.in_(affected_keys))
                rows = conn.execute(refresh_stmt).mappings().all()
                models = [self._row_to_model(row) for row in rows]
                audit_operation = self._audit_operation_for_updates(updates)
                for model in models:
                    self._audit(conn, audit_operation, model, dict(updates))
                return models
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("update_where", exc)

    def _assert_known_expression_fields(self, expressions: Mapping[str, Any]) -> None:
        """
        Validate F() targets and every column they read.

        Both sides matter: assigning to an unknown column and reading from one are
        equally broken, and catching it here produces a clear error instead of a
        SQLAlchemy ``KeyError`` from deep inside statement compilation.
        """
        for name, expression in expressions.items():
            targets = {name} | expression.referenced_fields()
            unknown = sorted(t for t in targets if t not in self._table.c)
            if unknown:
                raise InvalidQueryError(
                    f"Unknown field(s) {unknown!r} in expression for "
                    f"'{self.model_cls.__name__}.{name}'.",
                    operation="expression",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=name,
                    details={"unknown_fields": unknown},
                )
            excluded = sorted(t for t in targets if t in self._db_excluded_fields)
            if excluded:
                raise InvalidQueryError(
                    f"Field(s) {excluded!r} have no database column and cannot be "
                    "used in an expression.",
                    operation="expression",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=name,
                )
            encrypted = sorted(t for t in targets if t in self._encrypted_fields)
            if encrypted:
                raise InvalidQueryError(
                    f"Encrypted field(s) {encrypted!r} cannot be used in an expression: "
                    "the database stores ciphertext, so arithmetic on it is meaningless.",
                    operation="expression",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                    field=name,
                )

    def delete(self, key_value: Any) -> bool:
        """Delete the row with the given primary key. Returns True if deleted."""
        if self.soft_delete:
            row = self.get(key_value, include_deleted=True)
            if row is None:
                return False
            self._call_hook("before_delete", row)
            updated = self.update_where({self.key_field: key_value}, deleted_at=self._timestamp_value("deleted_at"))
            if updated:
                self._call_hook("after_delete", updated[0])
            return bool(updated)
        row = self.get(key_value)
        if row is not None:
            self._call_hook("before_delete", row)
        deleted = self.delete_where(**{self.key_field: key_value}) > 0
        if deleted and row is not None:
            self._call_hook("after_delete", row)
        return deleted

    def delete_where(self, **criteria: Any) -> int:
        """Delete all rows matching *criteria*. Returns the deleted row count."""
        if criteria.pop("include_deleted", False):
            include_deleted = True
        else:
            include_deleted = False
        if not criteria:
            raise InvalidQueryError(
                "delete_where() requires at least one filter criterion.",
                operation="delete_where",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        self._assert_known_fields(criteria)
        criteria = self._with_policy_criteria(criteria, include_deleted=include_deleted)

        stmt = delete(self._table)
        stmt = self._apply_where(stmt, criteria)
        try:
            with self._connection_scope() as conn:
                if self.soft_delete:
                    rows = conn.execute(self._apply_where(select(self._table), criteria)).mappings().all()
                    values = self._normalize_write_mapping({"deleted_at": self._timestamp_value("deleted_at")})
                    result = conn.execute(update(self._table).values(**values).where(*stmt._where_criteria))
                    for row in rows:
                        self._audit(conn, "delete", self._row_to_model(row), values)
                    return result.rowcount or 0
                result = conn.execute(stmt)
            return result.rowcount or 0
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("delete_where", exc)

    def bulk_delete(
        self,
        ids: list[Any] | tuple[Any, ...] | set[Any] | None = None,
        *,
        dangerous_allow_full_table_delete: bool = False,
        **criteria: Any,
    ) -> int:
        """Delete by primary-key collection or criteria, returning affected rows."""
        if ids is not None:
            if criteria:
                raise InvalidQueryError(
                    "bulk_delete() accepts ids or criteria, not both.",
                    operation="bulk_delete",
                    model=self.model_cls.__name__,
                    table=self.table_name,
                )
            return self.delete_where(**{f"{self.key_field}__in": list(ids)})
        if not criteria and not dangerous_allow_full_table_delete:
            raise InvalidQueryError(
                "bulk_delete() requires ids, criteria, or dangerous_allow_full_table_delete=True.",
                operation="bulk_delete",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        if dangerous_allow_full_table_delete and not criteria:
            stmt = delete(self._table)
            try:
                with self._connection_scope() as conn:
                    result = conn.execute(stmt)
                return result.rowcount or 0
            except SQLAlchemyError as exc:
                self._raise_sqlalchemy_error("bulk_delete", exc)
        return self.delete_where(**criteria)

    def bulk_create(self, records: list[Mapping[str, Any]]) -> list[ModelT]:
        """Create multiple records atomically and return stamped models."""
        if not records:
            return []

        mutable_records = [self._prepare_create_data(dict(record)) for record in records]
        self._call_hook("before_bulk_create", mutable_records)
        instances = [self.adapter.construct(record) for record in mutable_records]

        for instance in instances:
            self._prepare_instance_for_save(instance, is_create=True)
        for instance in instances:
            self._reject_explicit_autoincrement_key(instance)

        values_list = [self._prepare_insert_values(instance) for instance in instances]

        try:
            with self._connection_scope() as conn:
                supports_insert_returning = bool(
                    getattr(conn.dialect, "insert_returning", False)
                    and getattr(conn.dialect, "insert_executemany_returning", False)
                )
                if supports_insert_returning:
                    stmt = self._table.insert().returning(self._table)
                    rows = conn.execute(stmt, values_list).mappings().all()
                    created = [self._row_to_model(row) for row in rows]
                else:
                    created = [self._create_with_conn(conn, instance) for instance in instances]
                for model in created:
                    self._audit(conn, "create", model, self._public_model_dump(model))
            self._call_hook("after_bulk_create", created)
            return created
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("bulk_create", exc)

    def bulk_upsert(self, records: list[Mapping[str, Any]]) -> list[ModelT]:
        """Upsert multiple records atomically and return stamped models."""
        if not records:
            return []

        targets = [self.adapter.construct(record) for record in records]
        for target in targets:
            self._prepare_instance_for_save(target, is_create=False)
        persisted: list[ModelT] = []

        try:
            with self._connection_scope() as conn:
                for target in targets:
                    persisted.append(self._upsert_with_conn(conn, target))
        except IntegrityError as exc:
            raise self._classify_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("bulk_upsert", exc)

        return persisted

    def get_or_create(
        self,
        *,
        lookup: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> tuple[ModelT, bool]:
        """Return an existing record for a unique lookup or create it."""
        self._assert_atomic_lookup(lookup)
        existing = self.get(**dict(lookup))
        if existing is not None:
            return existing, False
        try:
            return self.create(**{**dict(defaults or {}), **dict(lookup)}), True
        except UniqueConstraintError:
            return self.require(**dict(lookup)), False

    def update_or_create(
        self,
        *,
        lookup: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> tuple[ModelT, bool]:
        """Update an existing record for a unique lookup or create it."""
        self._assert_atomic_lookup(lookup)
        existing = self.get(**dict(lookup))
        if existing is None:
            return self.create(**{**dict(defaults or {}), **dict(lookup)}), True
        for field, value in dict(defaults or {}).items():
            object.__setattr__(existing, field, value)
        existing.save()
        return existing, False

    def restore(self, key_value: Any) -> ModelT:
        """Restore a soft-deleted record."""
        if not self.soft_delete:
            raise InvalidQueryError("restore() requires soft_delete=True.", operation="restore")
        rows = self.update_where({self.key_field: key_value, "include_deleted": True}, deleted_at=None)
        if not rows:
            raise RecordNotFoundError(
                f"No {self.model_cls.__name__} found matching {self.key_field}={key_value!r}.",
                operation="restore",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        return rows[0]

    def hard_delete(self, key_value: Any) -> bool:
        """Physically delete a row, bypassing soft delete."""
        row = self.get(key_value, include_deleted=True)
        if row is None:
            return False
        stmt = delete(self._table).where(
            self._table.c[self.key_field]
            == self._normalize_mapping_for_db({self.key_field: key_value})[self.key_field]
        )
        try:
            with self._connection_scope() as conn:
                result = conn.execute(stmt)
                deleted = bool(result.rowcount)
                if deleted:
                    self._audit(conn, "hard_delete", row, {})
                return deleted
        except SQLAlchemyError as exc:
            self._raise_sqlalchemy_error("hard_delete", exc)

    def purge_deleted(self, *, before: datetime) -> int:
        """Physically delete soft-deleted rows before a timestamp."""
        if not self.soft_delete:
            raise InvalidQueryError("purge_deleted() requires soft_delete=True.", operation="purge_deleted")
        rows = self.filter(include_deleted=True, deleted_at__lt=before)
        count = 0
        for row in rows:
            if self.hard_delete(getattr(row, self.key_field)):
                count += 1
        return count

    def _assert_atomic_lookup(self, lookup: Mapping[str, Any]) -> None:
        if not lookup:
            raise InvalidQueryError(
                "Atomic create helpers require a non-empty lookup.",
                operation="get_or_create",
                model=self.model_cls.__name__,
                table=self.table_name,
            )
        self._assert_known_fields(lookup)
        fields = tuple(lookup.keys())
        if fields == (self.key_field,) or set(fields) == set(self.config.unique_fields):
            return
        raise InvalidQueryError(
            "get_or_create()/update_or_create() require lookup fields matching the "
            "primary key or configured unique_fields.",
            operation="get_or_create",
            model=self.model_cls.__name__,
            table=self.table_name,
            details={"lookup_fields": list(fields), "unique_fields": list(self.config.unique_fields)},
        )

    def _row_to_model(self, row: Mapping[str, Any]) -> ModelT:
        """
        Hydrate a DB row into a model, running each column through its codec.

        A stored value that no longer satisfies the model's declared type is a real
        condition — schema drift, a hand-edited row, or an expression that produced
        a wider type than the column declares (``F("a") / F("b")`` yielding a float
        for an int column). Surfacing the model layer's own exception here would
        leak ``pydantic.ValidationError`` out of a persistence call, so it is
        wrapped with the context needed to find the offending row.
        """
        values = dict(row)
        for field_name, spec in self._specs.items():
            if spec.from_db is not _spec_identity and field_name in values:
                values[field_name] = spec.from_db(values[field_name])
        try:
            return self._stamp_identity(self.adapter.hydrate(values))
        except (SQLAlchemyError, InvalidQueryError):
            raise
        except Exception as exc:
            raise SchemaError(
                f"Row in '{self.table_name}' does not match the declared shape of "
                f"'{self.model_cls.__name__}'. The stored value and the field's "
                "annotation disagree — check for schema drift, or for an expression "
                "producing a wider type than the column holds.",
                operation="hydrate",
                model=self.model_cls.__name__,
                table=self.table_name,
                details={
                    "key": values.get(self.key_field),
                    "validation_error": str(exc),
                },
            ) from exc

    def _model_to_row(self, model: ModelT) -> dict[str, Any]:
        # Use plain model_dump() (no mode='json') so Python date/datetime/Decimal
        # objects are preserved as native types.  SQLAlchemy's column types handle
        # the DB-level serialisation correctly.
        self._ensure_generated_uuid_key(model)
        self._normalize_model_for_write(model)
        return self._normalize_mapping_for_db(self._public_model_dump(model))

    def _prepare_insert_values(self, model: ModelT) -> dict[str, Any]:
        values = self._model_to_row(model)
        # Strip None autoincrement key so the DB generates it
        if self.config.autoincrement and values.get(self.key_field) is None:
            values.pop(self.key_field, None)
        return values

    def _create_with_conn(self, conn: Connection, instance: ModelT) -> ModelT:
        self._reject_explicit_autoincrement_key(instance)
        values = self._prepare_insert_values(instance)
        stmt = self._table.insert().values(**values)
        result = conn.execute(stmt)
        return self._apply_generated_key(instance, result)

    def _apply_generated_key(self, instance: ModelT, result: Any) -> ModelT:
        if self.config.autoincrement and getattr(instance, self.key_field, None) is None:
            pks = list(result.inserted_primary_key or ())
            if pks:
                instance = self.adapter.copy_with(instance, **{self.key_field: pks[0]})
        return self._stamp_identity(instance)

    def _reject_explicit_autoincrement_key(self, instance: ModelT) -> None:
        if not self.config.autoincrement:
            return

        key_value = getattr(instance, self.key_field, None)
        if key_value is not None:
            raise InvalidPrimaryKeyAssignmentError(
                f"Cannot explicitly assign '{self.model_cls.__name__}.{self.key_field}' "
                "when the primary key is database-managed.",
                operation="create",
                model=self.model_cls.__name__,
                table=self.table_name,
                field=self.key_field,
            )

    def _assert_immutable_key(self, instance: ModelT) -> None:
        original = getattr(instance, _ORIGINAL_KEY_ATTR, None)
        current = getattr(instance, self.key_field, None)
        if original is not None and current != original:
            raise ImmutableFieldError(
                f"Field '{self.model_cls.__name__}.{self.key_field}' is immutable once "
                "the record has been persisted.",
                operation="save",
                model=self.model_cls.__name__,
                table=self.table_name,
                field=self.key_field,
                details={"original": original, "current": current},
            )

    def _stamp_identity(self, instance: ModelT) -> ModelT:
        object.__setattr__(instance, _ORIGINAL_KEY_ATTR, getattr(instance, self.key_field, None))
        if self.version_field is not None:
            # Remember the version this instance was last known-good at. The next
            # save compares against it; a mismatch means someone else wrote first.
            object.__setattr__(
                instance, _ORIGINAL_VERSION_ATTR, getattr(instance, self.version_field, None)
            )
        return instance

    def _upsert_with_conn(self, conn: Connection, target: ModelT) -> ModelT:
        self._assert_immutable_key(target)
        if self.version_field is not None:
            return self._versioned_upsert_with_conn(conn, target)
        values = self._model_to_row(target)
        key_value = values.get(self.key_field)

        if self.config.autoincrement and key_value is None:
            if self.config.unique_fields:
                return self._upsert_on_unique_fields(conn, target, values)
            return self._create_with_conn(conn, target)

        key_value = self._execute_upsert(conn, values, [self.key_field])
        if key_value is not None and getattr(target, self.key_field, None) is None:
            object.__setattr__(target, self.key_field, key_value)
        return self._stamp_identity(target)

    def _versioned_upsert_with_conn(self, conn: Connection, target: ModelT) -> ModelT:
        """
        Save with an optimistic-lock compare-and-swap.

        Deliberately *not* routed through ``INSERT ... ON CONFLICT DO UPDATE``: the
        whole point is a conditional write, and the conflict clause has no way to
        say "only if the version is still what I read". So an existing row takes an
        explicit guarded UPDATE::

            UPDATE t SET ..., version = :next WHERE pk = :pk AND version = :loaded

        A rowcount of zero means either the row is gone or its version moved on —
        both are cases where blindly writing would destroy someone else's change,
        so both raise :class:`StaleDataError` with the distinction in ``.details``.
        """
        version_field = self.version_field
        assert version_field is not None  # guarded by the caller

        original_version = getattr(target, _ORIGINAL_VERSION_ATTR, None)
        key_value = getattr(target, self.key_field, None)

        # Never loaded from the database, or no key yet -> this is an insert.
        if original_version is None or key_value is None:
            if getattr(target, version_field, None) is None:
                object.__setattr__(target, version_field, 1)
            return self._create_with_conn(conn, target)

        next_version = int(original_version) + 1
        object.__setattr__(target, version_field, next_version)

        values = self._model_to_row(target)
        db_key = values.get(self.key_field)
        update_values = {k: v for k, v in values.items() if k != self.key_field}

        stmt = (
            update(self._table)
            .where(self._table.c[self.key_field] == db_key)
            .where(self._table.c[version_field] == original_version)
            .values(**update_values)
        )
        result = conn.execute(stmt)

        if not result.rowcount:
            # Roll the in-memory bump back so the instance still reflects what the
            # caller had; otherwise a retry would compare against a version that
            # never existed.
            object.__setattr__(target, version_field, original_version)
            still_exists = conn.execute(
                select(self._table.c[self.key_field]).where(
                    self._table.c[self.key_field] == db_key
                )
            ).first()
            reason = "version_conflict" if still_exists else "row_deleted"
            raise StaleDataError(
                f"{self.model_cls.__name__}({self.key_field}={key_value!r}) was modified "
                f"by another writer since it was read at {version_field}={original_version}."
                if still_exists
                else f"{self.model_cls.__name__}({self.key_field}={key_value!r}) no longer exists.",
                expected_version=original_version,
                operation="save",
                model=self.model_cls.__name__,
                table=self.table_name,
                field=version_field,
                details={"reason": reason, "expected_version": original_version},
            )

        return self._stamp_identity(target)

    def _upsert_on_unique_fields(
        self,
        conn: Connection,
        target: ModelT,
        values: dict[str, Any],
    ) -> ModelT:
        insert_values = dict(values)
        insert_values.pop(self.key_field, None)

        key_value = self._execute_upsert(conn, insert_values, self.config.unique_fields)
        lookup = {field: insert_values[field] for field in self.config.unique_fields}
        refreshed = self._row_from_connection(conn, **lookup)
        if refreshed is None:
            refreshed = self.require(**lookup)

        for field_name in self.adapter.fields():
            object.__setattr__(target, field_name, getattr(refreshed, field_name))
        if key_value is not None and getattr(target, self.key_field, None) is None:
            object.__setattr__(target, self.key_field, key_value)
        return self._stamp_identity(target)

    def _execute_upsert(
        self,
        conn: Connection,
        values: dict[str, Any],
        conflict_fields: tuple[str, ...] | list[str],
    ) -> Any:
        stmt = self._build_upsert_statement(values, conflict_fields)
        if stmt is not None:
            result = conn.execute(stmt)
            pks = list(result.inserted_primary_key or ())
            if pks:
                return pks[0]
            return values.get(self.key_field)
        return self._upsert_fallback_with_conn(conn, values, conflict_fields)

    def _build_upsert_statement(
        self,
        values: dict[str, Any],
        conflict_fields: tuple[str, ...] | list[str],
    ) -> Any:
        insert_stmt = dialect_insert(self._engine, self._table)
        if insert_stmt is None:
            return None

        update_cols = {key: value for key, value in values.items() if key != self.key_field}
        if not update_cols:
            return None
        stmt = insert_stmt.values(**values)
        dialect_name = self._engine.dialect.name

        if dialect_name in {"mysql", "mariadb"}:
            return stmt.on_duplicate_key_update(**update_cols)
        return stmt.on_conflict_do_update(
            index_elements=list(conflict_fields),
            set_=update_cols,
        )

    def _upsert_fallback_with_conn(
        self,
        conn: Connection,
        values: dict[str, Any],
        conflict_fields: tuple[str, ...] | list[str],
    ) -> Any:
        lookup = {
            field: values[field]
            for field in conflict_fields
            if field in values and values[field] is not None
        }
        existing = self._row_from_connection(conn, lock_for_update=True, **lookup) if lookup else None
        if existing is not None:
            updates = {key: value for key, value in values.items() if key != self.key_field}
            if updates:
                stmt = (
                    update(self._table)
                    .where(
                        self._table.c[self.key_field]
                        == self._normalize_mapping_for_db(
                            {self.key_field: getattr(existing, self.key_field)}
                        )[self.key_field]
                    )
                    .values(**updates)
                )
                conn.execute(stmt)
            return getattr(existing, self.key_field)

        insert_values = dict(values)
        if self.config.autoincrement and insert_values.get(self.key_field) is None:
            insert_values.pop(self.key_field, None)
        result = conn.execute(self._table.insert().values(**insert_values))
        pks = list(result.inserted_primary_key or ())
        if pks:
            return pks[0]
        return insert_values.get(self.key_field)

    def _row_from_connection(
        self,
        conn: Connection,
        *,
        lock_for_update: bool = False,
        **criteria: Any,
    ) -> ModelT | None:
        stmt = select(self._table).limit(1)
        stmt = self._apply_where(stmt, criteria)
        if lock_for_update:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).mappings().first()
        return self._row_to_model(row) if row is not None else None

    def _normalize_model_for_write(self, model: ModelT) -> None:
        for field_name in self._password_hash_fields:
            password = getattr(model, field_name, None)
            if isinstance(password, str) and password and not is_password_hash(password):
                object.__setattr__(model, field_name, hash_password(password))

    def _normalize_write_mapping(self, values: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(values)
        normalized = {
            key: value
            for key, value in normalized.items()
            if key not in self._db_excluded_fields
        }
        for field_name in self._password_hash_fields:
            password = normalized.get(field_name)
            if isinstance(password, str) and password and not is_password_hash(password):
                normalized[field_name] = hash_password(password)
        return self._normalize_mapping_for_db(normalized)

    def _ensure_generated_uuid_key(self, model: ModelT) -> None:
        if self.config.id_strategy != "uuid4":
            return
        if getattr(model, self.key_field, None) is None:
            object.__setattr__(model, self.key_field, uuid.uuid4())

    def _normalize_mapping_for_db(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Drop non-column fields and run each remaining value through its codec."""
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            spec = self._specs.get(key)
            if spec is not None and not spec.is_stored:
                continue
            normalized[key] = spec.to_db(value) if spec is not None else value
        return normalized

