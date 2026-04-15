# decorates.db Usage

`decorates.db` is a lightweight persistence layer for Pydantic models built on SQLAlchemy.
The goal is simple: decorate a model, then use `Model.objects` for CRUD.
The module is covered by a robust test suite and hardened for production-grade error reporting.

This guide is designed to get you productive in about 10 minutes.

## Install

```bash
pip install decorates
```

## 1. Quick Start

```python
from pydantic import BaseModel
from decorates.db import database_registry


@database_registry(
    "sqlite:///app.db",
    table_name="users",
    unique_fields=["email"],
)
class User(BaseModel):
    id: int | None = None
    email: str
    name: str


# Create
user = User.objects.create(email="alice@example.com", name="Alice")

# Read
same_user = User.objects.require(user.id)

# Update
same_user.name = "Alicia"
same_user.save()

# Delete
same_user.delete()
```

## 2. Primary Key Rules

Keep this model contract in mind:

- `id: int | None = None` means database-managed autoincrement primary key.
- `id: int` means manual primary key (you provide it on create).
- For database-managed keys, passing `id=` to `create()` raises `InvalidPrimaryKeyAssignmentError`.
- Persisted primary keys are immutable; changing them then calling `save()` raises `ImmutableFieldError`.

## 3. Decorator Options

```python
@database_registry(
    database_url="sqlite:///app.db",  # or "app.db"
    table_name="users",               # default is model-name-based
    key_field="id",                   # default: "id"
    manager_attr="objects",           # default: "objects"
    auto_create=True,                 # default: True
    autoincrement=False,              # auto-inferred for id: int | None
    unique_fields=["email"],          # optional conflict target(s)
)
class User(BaseModel):
    id: int | None = None
    email: str
    name: str
```

## 4. CRUD API

All persistence methods live on `Model.objects`.

```python
# Strict insert
user = User.objects.create(email="alice@example.com", name="Alice")
user = User.objects.strict_create(email="bob@example.com", name="Bob")

# Upsert by primary key
user = User.objects.upsert(id=1, email="alice@example.com", name="Alice")

# Upsert by unique fields when id is absent
user = User.objects.upsert(email="alice@example.com", name="Alicia")

# Read
one = User.objects.get(1)
one = User.objects.get(email="alice@example.com")
must_exist = User.objects.require(email="alice@example.com")

# Query helpers
rows = User.objects.filter(name="Alicia")
rows = User.objects.all()
exists = User.objects.exists(email="alice@example.com")
count = User.objects.count()
first = User.objects.first()
last = User.objects.last()

# Update/delete helpers
updated = User.objects.update_where({"name": "Alicia"}, name="Alice")
deleted_count = User.objects.delete_where(name="Alice")
deleted_bool = User.objects.delete(1)
```

Injected instance methods:

```python
user.save()
user.refresh()
user.delete()
```

## 5. Filtering, Operators, Sorting, Pagination

Supported filter operators use `field__operator=value` syntax:

- `eq` (default), `not`
- `gt`, `gte`, `lt`, `lte`
- `like`, `ilike`
- `in`, `not_in`
- `is_null`
- `between`
- `contains`, `startswith`, `endswith`

```python
User.objects.filter(age__gte=18, age__lt=65)
User.objects.filter(name__ilike="ali%")
User.objects.filter(status__in=["active", "trial"])
User.objects.filter(deleted_at__is_null=True)
User.objects.filter(score__between=(70, 100))
```

Sorting:

```python
User.objects.filter(order_by="name")
User.objects.filter(order_by="-created_at")
User.objects.all(order_by=["role", "-name"])
User.objects.first(order_by="created_at")
User.objects.last(order_by="created_at")
```

Pagination:

```python
page = User.objects.filter(order_by="id", limit=20, offset=40)
```

## 6. Bulk Operations

```python
users = User.objects.bulk_create(
    [
        {"email": "a@example.com", "name": "A"},
        {"email": "b@example.com", "name": "B"},
    ]
)

upserted = User.objects.bulk_upsert(
    [
        {"id": 1, "email": "a@example.com", "name": "A Updated"},
        {"id": 3, "email": "c@example.com", "name": "C"},
    ]
)
```

## 7. Schema and Migration Helpers

Class-level helpers:

```python
User.create_schema()    # idempotent
User.schema_exists()
User.truncate()         # delete rows, keep table
User.drop_schema()      # drop table
```

Manager-level evolution helpers:

```python
User.objects.add_column("timezone", str, nullable=True)
User.objects.ensure_column("timezone", str, nullable=True)  # returns bool
User.objects.column_names()
User.objects.rename_table("users_archive")
```

`rename_table(...)` is a full state transition: after it succeeds, the same
`Model.objects` manager and class-level schema helpers (`schema_exists`, etc.)
operate on the new table name immediately.

For startup-safe migrations, prefer `ensure_column(...)`.

## 8. Relationships

Relationships are descriptors assigned after model decoration.

```python
from pydantic import BaseModel
from decorates.db import database_registry, HasMany, BelongsTo, HasManyThrough

DB = "sqlite:///app.db"

@database_registry(DB, table_name="authors")
class Author(BaseModel):
    id: int | None = None
    name: str

@database_registry(DB, table_name="posts")
class Post(BaseModel):
    id: int | None = None
    author_id: int
    title: str

@database_registry(DB, table_name="tags")
class Tag(BaseModel):
    id: int | None = None
    name: str

@database_registry(DB, table_name="post_tags")
class PostTag(BaseModel):
    id: int | None = None
    post_id: int
    tag_id: int

Author.posts = HasMany(Post, foreign_key="author_id")
Post.author = BelongsTo(Author, local_key="author_id")
Post.tags = HasManyThrough(Tag, through=PostTag, source_key="post_id", target_key="tag_id")
```

## 9. Password Field Behavior

If your model has a field named `password`, writes are hashed automatically.

```python
@database_registry("sqlite:///app.db", table_name="accounts")
class Account(BaseModel):
    id: int | None = None
    email: str
    password: str

acct = Account.objects.create(email="alice@example.com", password="secret123")
assert acct.password != "secret123"
assert acct.verify_password("secret123")
```

Hashing is applied to `create`, `strict_create`, `upsert`, `save`, and `update_where`.

## 10. Transactions and Lifecycle

```python
with User.objects.transaction():
    User.objects.create(email="a@example.com", name="A")
    User.objects.create(email="b@example.com", name="B")
```

Dispose connection pools at shutdown:

```python
from decorates.db import dispose_all

dispose_all()
```

## 11. FastAPI Pattern

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

from myapp.models import User

@asynccontextmanager
async def lifespan(app: FastAPI):
    User.create_schema()
    yield
    User.objects.dispose()

app = FastAPI(lifespan=lifespan)
```

## 12. Common Exceptions

- `DuplicateKeyError`
- `UniqueConstraintError`
- `RecordNotFoundError`
- `InvalidQueryError`
- `InvalidPrimaryKeyAssignmentError`
- `ImmutableFieldError`
- `SchemaError`
- `MigrationError`

All inherit from `RegistryError`.

Operational note:

- unexpected SQLAlchemy runtime failures are normalized into `SchemaError` with operation context
- logs are emitted under `decorates.db.*` so production logging/observability pipelines can capture failures cleanly
- all DB exceptions expose structured metadata via `exc.context` and `exc.to_dict()` for richer diagnostics
