from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from conftest import db_url
from decorates.db import SchemaError, database_registry


class TestDbErrorHandling:
    def test_create_wraps_unexpected_sqlalchemy_error(self, tmp_path, monkeypatch):
        @database_registry(db_url(tmp_path), table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = None
            name: str

        def fail_create_with_conn(*_args, **_kwargs):
            raise SQLAlchemyError("db exploded")

        monkeypatch.setattr(User.objects, "_create_with_conn", fail_create_with_conn)

        with pytest.raises(SchemaError, match="Database operation 'create' failed") as exc_info:
            User.objects.create(name="Alice")

        err = exc_info.value
        assert err.operation == "create"
        assert err.model == "User"
        assert err.table == "users"
        assert err.context["operation"] == "create"
        assert "driver_error" in err.context["details"]
