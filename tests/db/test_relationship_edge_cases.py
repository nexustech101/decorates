from __future__ import annotations

from pydantic import BaseModel

from conftest import db_url
from decorates.db import BelongsTo, HasMany, database_registry


class TestRelationshipEdgeCases:
    def test_relationship_access_with_null_foreign_key_returns_none_or_empty(self, tmp_path):
        url = db_url(tmp_path, "relations")

        @database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int | None = None
            name: str

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int | None = None
            author_id: int | None = None
            title: str

        Author.posts = HasMany(Post, foreign_key="author_id")
        Post.author = BelongsTo(Author, local_key="author_id")

        orphan = Post.objects.create(title="No owner")
        lonely_author = Author.objects.create(name="Lonely")

        assert orphan.author is None
        assert lonely_author.posts == []

    def test_relationship_access_with_missing_foreign_row_is_safe(self, tmp_path):
        url = db_url(tmp_path, "relations")

        @database_registry(url, table_name="authors", key_field="id")
        class Author(BaseModel):
            id: int | None = None
            name: str

        @database_registry(url, table_name="posts", key_field="id")
        class Post(BaseModel):
            id: int | None = None
            author_id: int
            title: str

        Post.author = BelongsTo(Author, local_key="author_id")

        dangling = Post.objects.create(author_id=999, title="Dangling")

        assert dangling.author is None
