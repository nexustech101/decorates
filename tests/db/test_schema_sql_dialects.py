from __future__ import annotations

from sqlalchemy import create_mock_engine

from decorates.db.schema import _build_rename_table_sql


def test_mysql_rename_table_sql_uses_mysql_syntax_and_quoting():
    engine = create_mock_engine("mysql+pymysql://", lambda *_args, **_kwargs: None)

    sql = _build_rename_table_sql(engine, "users", "users_archive")

    assert sql == "RENAME TABLE `users` TO `users_archive`"


def test_postgres_rename_table_sql_uses_alter_table():
    engine = create_mock_engine("postgresql+psycopg://", lambda *_args, **_kwargs: None)

    sql = _build_rename_table_sql(engine, "users", "users_archive")

    assert sql == 'ALTER TABLE "users" RENAME TO "users_archive"'
