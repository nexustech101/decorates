from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import inspect, text

from conftest import db_url
from decorates.db import database_registry


class TestSchemaEvolutionAfterRename:
    def test_add_column_after_rename_targets_new_table(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="profiles", key_field="id")
        class Profile(BaseModel):
            id: int
            name: str

        Profile.objects.create(id=1, name="Alice")
        Profile.objects.rename_table("profiles_archive")

        Profile.objects.add_column("nickname", str | None, nullable=True)

        inspector = inspect(Profile.objects._engine)
        columns = {col["name"] for col in inspector.get_columns("profiles_archive")}
        assert "nickname" in columns

        with Profile.objects._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO profiles_archive (id, name, nickname) VALUES (:id, :name, :nickname)"
                ),
                {"id": 2, "name": "Bob", "nickname": "B"},
            )
            nickname = conn.execute(
                text("SELECT nickname FROM profiles_archive WHERE id = 2")
            ).scalar_one()

        assert nickname == "B"
        assert Profile.objects.count() == 2

    def test_ensure_column_after_rename_is_idempotent(self, tmp_path):
        @database_registry(db_url(tmp_path), table_name="events", key_field="id")
        class Event(BaseModel):
            id: int
            title: str

        Event.objects.rename_table("events_archive")

        assert Event.objects.ensure_column("archived_at", str | None, nullable=True) is True
        assert Event.objects.ensure_column("archived_at", str | None, nullable=True) is False

        inspector = inspect(Event.objects._engine)
        columns = [col["name"] for col in inspector.get_columns("events_archive")]
        assert columns.count("archived_at") == 1
