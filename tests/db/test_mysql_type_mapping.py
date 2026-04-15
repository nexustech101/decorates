from __future__ import annotations

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from decorates.db.typing_utils import sqlalchemy_type_for_annotation


def test_string_annotations_compile_on_mysql_with_bounded_varchar():
    table = Table(
        "users",
        MetaData(),
        Column("email", sqlalchemy_type_for_annotation(str)),
    )

    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
    assert "VARCHAR(" in ddl.upper()
