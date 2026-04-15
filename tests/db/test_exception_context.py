from __future__ import annotations

import pytest
from pydantic import BaseModel

from conftest import db_url
from decorates.db import RecordNotFoundError, SchemaError, database_registry
from decorates.db.exceptions import RegistryError


class TestExceptionContext:
    def test_registry_error_supports_structured_context(self):
        err = RegistryError(
            "failure",
            operation="create",
            model="User",
            table="users",
            field="email",
            details={"reason": "invalid"},
            request_id="abc123",
        )

        payload = err.to_dict()
        assert payload["type"] == "RegistryError"
        assert payload["message"] == "failure"
        assert payload["operation"] == "create"
        assert payload["model"] == "User"
        assert payload["table"] == "users"
        assert payload["field"] == "email"
        assert payload["details"]["reason"] == "invalid"
        assert payload["request_id"] == "abc123"

    def test_require_error_contains_criteria_context(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            email: str

        with pytest.raises(RecordNotFoundError) as exc_info:
            User.objects.require(email="missing@example.com")

        err = exc_info.value
        assert err.operation == "require"
        assert err.model == "User"
        assert err.table == "users"
        assert err.context["details"]["criteria"] == {"email": "missing@example.com"}

    def test_schema_error_context_is_available_on_direct_init(self):
        err = SchemaError("schema failed", operation="drop_schema", table="widgets")
        assert err.operation == "drop_schema"
        assert err.table == "widgets"
        assert err.to_dict()["operation"] == "drop_schema"
