from __future__ import annotations

from pydantic import BaseModel

from conftest import db_url
from decorates.db import database_registry


class TestPasswordHashing:
    def test_password_field_save_does_not_double_hash_on_resave(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="accounts", key_field="id")
        class Account(BaseModel):
            id: int | None = None
            email: str
            password: str

        account = Account.objects.create(email="alice@example.com", password="secret123")
        first_hash = account.password

        account.email = "alice+1@example.com"
        account.save()
        second_hash = account.password

        account.save()
        third_hash = account.password

        assert first_hash == second_hash
        assert second_hash == third_hash
        assert account.verify_password("secret123") is True
