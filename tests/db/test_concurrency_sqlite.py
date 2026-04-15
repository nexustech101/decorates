from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel

from conftest import db_url
from decorates.db import database_registry


class TestSQLiteConcurrency:
    def test_sqlite_file_concurrent_upserts_are_consistent(self, tmp_path):
        @database_registry(
            db_url(tmp_path, "concurrent"),
            table_name="users",
            key_field="id",
            unique_fields=["email"],
        )
        class User(BaseModel):
            id: int | None = None
            email: str
            name: str

        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                key = i % 10
                User.objects.upsert(email=f"user{key}@example.com", name=f"name-{i}")
            except Exception as exc:  # pragma: no cover - assertion below checks this path
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(100)))

        assert errors == []
        assert User.objects.count() == 10

        emails = {row.email for row in User.objects.all()}
        assert emails == {f"user{i}@example.com" for i in range(10)}
