<div align="center">

# `registers.db`

**Pydantic-first persistence layer with registry/manager ergonomics, schema helpers, query operators, and application-ready integration patterns.**

[![Module](https://img.shields.io/badge/module-registers.db-5C6BC0?style=for-the-badge)](#) [![Type](https://img.shields.io/badge/type-Persistence%20Layer-1F2937?style=for-the-badge)](#) [![Modeling](https://img.shields.io/badge/modeling-Pydantic--First-0F766E?style=for-the-badge)](#) [![Engine](https://img.shields.io/badge/powered%20by-SQLAlchemy-7C3AED?style=for-the-badge)](#) [![Patterns](https://img.shields.io/badge/patterns-Registry%20%7C%20Manager-9333EA?style=for-the-badge)](#) [![Maturity](https://img.shields.io/badge/status-Production%20Guide-2563EB?style=for-the-badge)](#)

</div>

## Tags

`Pydantic Models` `Manager API` `CRUD` `Query Operators` `Bulk Operations` `Schema Evolution` `Relationships` `FastAPI Integration`

> **Positioning:** Use `registers.db` when you want a lightweight persistence abstraction that preserves Pydantic-centered development while still exposing operationally useful database primitives.

---

`registers.db` is a Pydantic-first persistence layer powered by SQLAlchemy engines and a registry/manager pattern. It lets you define data as Pydantic models and persist, query, evolve, and integrate those models without writing full ORM mapping boilerplate.

This refactored manual is designed for backend engineers, FastAPI developers, library maintainers, and AI coding agents that need complete usage guidance for the public API.

## Audience

Use this manual if you are:

- registering Pydantic models as database-backed records;
- building FastAPI services with a manager-style persistence API;
- using query operators, upserts, bulk writes, relationships, or schema helpers;
- designing test-isolated registries;
- documenting safe lifecycle, error, and security practices.

## Operating Model

`registers.db` centers on one concept: a registered Pydantic model receives a manager object, usually `Model.objects`, that owns persistence operations.

| Layer | Responsibility |
|---|---|
| Model | Pydantic schema and validation. |
| Registry | Model-to-table binding, engine ownership, metadata validation. |
| Manager | CRUD, queries, upserts, bulk operations, schema helpers, transactions. |
| Integration | FastAPI lifecycle, exception mapping, service-layer composition. |

## Production Contract

A production service should define:

- explicit `table_name`, `key_field`, and uniqueness rules;
- stable manager naming, usually `objects`;
- startup-safe schema lifecycle checks;
- exception handlers for user-facing HTTP boundaries;
- explicit disposal at shutdown/test teardown;
- service-layer invariants for multi-record writes;
- explicit `db_field(...)` policy for generated keys, password hashing, and response serialization.

---
## What Is registers.db?

`registers.db` is a persistence layer for **Pydantic models**, powered by SQLAlchemy engines and a registry/manager pattern. Define your data as Pydantic classes. Persist, query, and evolve them through a clean manager API — with zero ORM boilerplate.

```python
from pydantic import BaseModel
from registers import database_registry, db_field

@database_registry("sqlite:///app.db", table_name="users", unique_fields=["email"])
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str
    name: str

user = User.objects.create(email="alice@example.com", name="Alice")
user.name = "Alicia"
user.save()
```

That's it. No base classes to inherit. No metaclass magic. No mappers to configure.

---

## Feature Highlights

| Capability | Details |
|---|---|
| 🏗 **Declarative registration** | `@database_registry(...)` decorator wires models to tables |
| 🔍 **Expressive query operators** | `field__gte`, `field__in`, `field__ilike`, `field__between`, and more |
| 🔄 **Upsert & bulk ops** | `bulk_create`, `bulk_upsert`, key/constraint-aware upserts |
| **Password hashing** | Opt-in with `db_field(hash_password=True)`; configurable PBKDF2/Argon2id policy and upgrade helpers |
| 🔗 **Relationships** | `HasMany`, `BelongsTo`, `HasManyThrough` — lazy, safe, read-optimized |
| 📐 **Schema evolution** | `ensure_column`, `add_column`, `rename_table` — additive and startup-safe |
| 🚨 **Structured exceptions** | Every error carries `.context` and `.to_dict()` for observability |
| ⚡ **FastAPI-ready** | Lifespan hooks, exception handlers, and service-layer patterns included |

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
  - [Decorator Mode](#decorator-mode)
  - [Instance Registry Mode](#instance-registry-mode)
- [Model Registration](#model-registration)
- [Field Metadata with `db_field`](#field-metadata-with-db_field)
- [CRUD API](#crud-api)
- [Querying, Sorting & Pagination](#querying-sorting--pagination)
- [Upsert & Identity Rules](#upsert--identity-rules)
- [Bulk Operations](#bulk-operations)
- [Schema Lifecycle & Evolution](#schema-lifecycle--evolution)
- [Relationships](#relationships)
- [Password Security](#password-security)
- [Transactions & Engine Lifecycle](#transactions--engine-lifecycle)
- [FastAPI Integration](#fastapi-integration)
- [Exception Model](#exception-model)
- [Ecommerce Blueprint](#ecommerce-blueprint)
- [Public API Reference](#public-api-reference)

---

## Installation

```bash
pip install registers
```

**Core imports:**

```python
from pydantic import BaseModel
from registers import (
    database_registry,
    DatabaseRegistry,
    db_field,
    Q,
    Agg,
    Page,
    HasMany,
    BelongsTo,
    HasManyThrough,
    OneToMany,
    ManyToOne,
    ManyToMany,
    dispose_all,
)
```

---

## Quick Start

### Decorator Mode

The fastest path from model to database. Ideal for single-surface services with minimal wiring.

```python
from pydantic import BaseModel
from registers import database_registry, db_field

@database_registry(
    "sqlite:///app.db",
    table_name="users",
    key_field="id",
    unique_fields=["email"],
)
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str
    name: str

# Create
created = User.objects.create(email="alice@example.com", name="Alice")

# Fetch — raises RecordNotFoundError if missing
fetched = User.objects.require(created.id)

# Update
fetched.name = "Alicia"
fetched.save()

# Delete
fetched.delete()
```

> Generated keys are explicit. Use `db_field(id_strategy="autoincrement")` for integer IDs or `db_field(id_strategy="uuid4")` for generated UUID primary keys.

UUID keys are useful for public IDs, sync-safe records, distributed writers, and APIs where sequential integer IDs should not leak object counts.

```python
from uuid import UUID

from pydantic import BaseModel
from registers import database_registry, db_field

@database_registry("sqlite:///app.db", table_name="api_keys", key_field="id")
class ApiKey(BaseModel):
    id: UUID | None = db_field(id_strategy="uuid4", default=None)
    name: str
    owner_email: str

api_key = ApiKey.objects.create(name="Production", owner_email="ops@example.com")

assert isinstance(api_key.id, UUID)
assert ApiKey.objects.require(api_key.id).name == "Production"
```

---

### Instance Registry Mode

Preferred when you need explicit registry ownership, isolated model sets, or test scoping.

```python
from pydantic import BaseModel
from registers import DatabaseRegistry, db_field

db = DatabaseRegistry()

@db.database_registry(
    "sqlite:///app.db",
    table_name="users",
    key_field="id",
    unique_fields=["email"],
)
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str
    name: str

user = User.objects.create(email="alice@example.com", name="Alice")
```

Create one `DatabaseRegistry()` per DB namespace. Behaviors are identical to module-level `@database_registry(...)`.

---

## Model Registration

### Decorator Signature

```python
@database_registry(
    database_url="sqlite:///app.db",   # bare path also accepted
    table_name="users",
    key_field="id",
    manager_attr="objects",            # default; must not collide with fields
    auto_create=True,
    autoincrement=False,               # explicit legacy alias for integer DB IDs
    unique_fields=["email"],
)
class User(BaseModel):
    ...
```

### Defaults

| Option | Default |
|---|---|
| `table_name` | snake_case pluralized model name |
| `database_url` | SQLite file derived from table name |
| `manager_attr` | `"objects"` |
| `auto_create` | `True` (defers unresolved FK DDL) |

### Primary Key Policy

| Field Type | Behavior |
|---|---|
| `id: int \| None = db_field(id_strategy="autoincrement", default=None)` | DB-managed integer ID; omit on create |
| `id: UUID \| None = db_field(id_strategy="uuid4", default=None)` | Application-generated UUID4 stored as binary UUID |
| `id: int` | Caller-supplied; required on create |

> **Violations:** Assigning a DB-managed key on create raises `InvalidPrimaryKeyAssignmentError`. Mutating a persisted key then saving raises `ImmutableFieldError`.

### Validation Rules

- Class must be a `pydantic.BaseModel` subclass
- `key_field` must exist on the model
- `manager_attr` must not collide with model fields or attributes
- `unique_fields` must reference valid fields with no duplicates
- Nullable primary keys must declare an explicit `db_field(id_strategy=...)`
- `id_strategy="autoincrement"` requires an integer key field that allows `None`
- `id_strategy="uuid4"` requires a `uuid.UUID` key field that allows `None`

### UUID Primary Keys

`id_strategy="uuid4"` generates a UUID in Python before insert, stores it as a 16-byte binary value, and returns `uuid.UUID` instances when records are read back.

```python
from uuid import UUID

@database_registry("sqlite:///app.db", table_name="sessions", key_field="id")
class Session(BaseModel):
    id: UUID | None = db_field(id_strategy="uuid4", default=None)
    user_id: int = db_field(foreign_key="users.id", index=True)
    token_name: str

session = Session.objects.create(user_id=1, token_name="laptop")
same_session = Session.objects.require(session.id)
matches = Session.objects.filter(id__in=[session.id])

assert same_session.id == session.id
assert matches[0].id == session.id
```

Use integer autoincrement IDs for compact internal tables and high-write local workloads. Use UUID keys for public-facing resources, offline creation, or systems where records may be created by multiple independent processes.

---

## Field Metadata with `db_field`

Attach DB metadata directly at field definition for index, unique, and foreign key behavior.

```python
from pydantic import BaseModel
from registers import database_registry, db_field

@database_registry("sqlite:///app.db", table_name="accounts", key_field="id")
class Account(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str = db_field(unique=True, index=True)
    manager_id: int | None = db_field(foreign_key="users.id", default=None)
```

### Supported Metadata Flags

| Flag | Type | Notes |
|---|---|---|
| `primary_key` | `bool` | Must align with configured `key_field` |
| `autoincrement` | `bool` | Non-key usage is rejected |
| `unique` | `bool` | Merges into `unique_fields` config |
| `index` | `bool` | Creates a DB index |
| `foreign_key` | `str` | Must use `"table.column"` format |
| `hash_password` | `bool` | Hashes this field on writes and avoids double hashing |
| `id_strategy` | `"manual"`, `"autoincrement"`, or `"uuid4"` | Key field only; controls generated primary-key behavior |
| `length` | `int` | String/binary length override, e.g. `db_field(length=512)` |
| `precision` / `scale` | `int` | Decimal/Numeric precision controls |
| `timezone` | `bool` | Datetime timezone flag, e.g. `db_field(timezone=True)` |
| `column_type` | SQLAlchemy type | Explicit escape hatch, e.g. `db_field(column_type=LargeBinary(2048))` |

---

## CRUD API

All persistence operations live on `Model.objects`. Instance methods are convenience wrappers only.

### Write Operations

```python
Model.objects.create(**data)
Model.objects.strict_create(**data)          # alias of create
Model.objects.upsert(instance | **data)
Model.objects.save(instance)
Model.objects.update_where(criteria, **updates)
Model.objects.delete(key_value)
Model.objects.delete_where(**criteria)
Model.objects.bulk_create(list[dict])
Model.objects.bulk_upsert(list[dict])
```

### Read Operations

```python
Model.objects.get(pk_or_criteria)
Model.objects.require(pk_or_criteria)        # raises RecordNotFoundError if missing
Model.objects.filter(...)
Model.objects.all(...)
Model.objects.get_all()
Model.objects.exists(**criteria)
Model.objects.count(**criteria)
Model.objects.first(...)
Model.objects.last(...)
Model.objects.refresh(instance)
```

### Instance Helpers

```python
instance.save()
instance.delete()
instance.refresh()
instance.verify_password(raw)               # only when password uses db_field(hash_password=True)
```

### Schema Helpers (class-level)

```python
Model.create_schema()
Model.schema_exists()
Model.truncate()
Model.drop_schema()
```

---

## Querying, Sorting & Pagination

Filters use `field__operator=value` syntax and are strongly validated at query time.

### Operators

| Operator | Example |
|---|---|
| `eq` (default) | `status="active"` |
| `not` | `status__not="banned"` |
| `gt`, `gte`, `lt`, `lte` | `age__gte=18` |
| `like`, `ilike` | `name__ilike="ali%"` |
| `in`, `not_in` | `status__in=["active", "trial"]` |
| `is_null` | `deleted_at__is_null=True` |
| `between` | `score__between=(70, 100)` |
| `contains`, `startswith`, `endswith` | `name__startswith="Al"` |

```python
User.objects.filter(age__gte=18, age__lt=65)
User.objects.filter(status__in=["active", "trial"])
User.objects.filter(deleted_at__is_null=True)
User.objects.filter(score__between=(70, 100))
User.objects.filter(name__ilike="ali%")
```

### Sorting

```python
User.objects.filter(order_by="name")           # ascending
User.objects.filter(order_by="id")             # ascending
User.objects.filter(order_by="-id")            # descending
User.objects.filter(order_by="-created_at")    # descending
User.objects.all(order_by=["role", "-name"])   # multi-column
```

### Pagination

```python
User.objects.filter(order_by="id", limit=20, offset=40)
```

Offset pagination is simple and works well for small back-office screens. For public list endpoints or frequently changing tables, prefer cursor pagination with `paginate(...)`; it returns a `Page` object documented below.

> **Validation:** Unknown fields/operators raise `InvalidQueryError`. Iterable equality values are rejected — use `id__in=[1, 2]` instead of `id=[1, 2]`. Both `limit` and `offset` must be `>= 0`.

### Composable Predicates with `Q`

Use `Q(...)` when a query needs grouped boolean logic that cannot be expressed cleanly as plain keyword criteria. A `Q` object accepts the same `field__operator=value` syntax as `filter(...)`, and combines with normal Python operators:

| Operator | Meaning | Example |
|---|---|---|
| `&` | AND | `Q(status="active") & Q(age__gte=18)` |
| `|` | OR | `Q(status="active") | Q(status="trial")` |
| `~` | NOT | `~Q(status="disabled")` |

```python
from registers import Q

# status = active OR status = trial
users = User.objects.filter(
    Q(status="active") | Q(status="trial"),
    order_by="-created_at",
)

# (age >= 18 AND role = member) AND NOT disabled
eligible = User.objects.filter(
    Q(age__gte=18, role="member") & ~Q(status="disabled"),
    order_by="email",
)

# Positional Q objects compose with keyword criteria through AND.
admins = User.objects.filter(
    Q(status="active") | Q(status="trial"),
    role="admin",
)
```

`exclude(...)` inverts the predicates for you:

```python
not_disabled = User.objects.exclude(status="disabled")
not_banned_or_disabled = User.objects.exclude(
    Q(status="banned") | Q(status="disabled")
)
```

Projection helpers also accept a `q=` predicate:

```python
rows = User.objects.select("id", "email", q=Q(status="active") | Q(status="trial"))
emails = User.objects.values_list("email", q=Q(role="admin"))
```

Use plain keyword filters for simple AND queries, and reach for `Q` only when grouping, OR, or NOT makes the query clearer. Positional `filter(...)` arguments must be `Q` objects; passing another value raises `InvalidQueryError`.

### Projection and Aggregate Queries

Use projections when an endpoint only needs a few fields and should not hydrate full model instances:

```python
rows = User.objects.select("id", "email", "role", status="active")
# [{"id": 1, "email": "alice@example.com", "role": "admin"}, ...]

emails = User.objects.values_list("email", status="active")
# ["alice@example.com", "bob@example.com"]

counts = User.objects.count_by("role", status__in=["active", "trial"])
# {"admin": 2, "member": 18}
```

Projected fields must be real database columns. Encrypted fields cannot be selected directly because the database stores ciphertext.

Use `Agg` with `aggregate(...)` for database-side summaries:

```python
from registers import Agg

total = User.objects.aggregate(Agg.count("id"))
# 42

stats = User.objects.aggregate(
    total=Agg.count("id"),
    active=Agg.count("id", status="active"),
    average_age=Agg.avg("age"),
    youngest=Agg.min("age"),
    oldest=Agg.max("age"),
    unique_roles=Agg.count_distinct("role"),
)
# {
#     "total": 42,
#     "active": 31,
#     "average_age": 34.5,
#     "youngest": 18,
#     "oldest": 72,
#     "unique_roles": 3,
# }
```

`Agg.count()` defaults to `"*"`, which counts the model key field internally. For normal count purposes these are equivalent:

```python
User.objects.aggregate(Agg.count())
User.objects.aggregate(Agg.count("id"))
```

Each `Agg` can carry its own criteria, and `aggregate(...)` can also receive global criteria. Global criteria apply to the whole aggregate query; criteria on a specific `Agg` apply only to that expression.

```python
summary = Order.objects.aggregate(
    total=Agg.count("id"),
    paid_revenue=Agg.sum("total", status="paid"),
    failed_revenue=Agg.sum("total", status="failed"),
    customer_id=customer.id,       # global filter for every aggregate
)
```

A single unnamed aggregate returns a scalar. Multiple aggregates or any named aggregate return a dictionary keyed by the generated label or the explicit keyword name. Prefer named aggregates in application code so response keys stay stable.

### Cursor Pagination with `Page`

`paginate(...)` returns a `Page` object:

```python
from registers import Page

page: Page = User.objects.paginate(order_by="-created_at", limit=20)

for user in page.items:
    ...

if page.has_next:
    next_page = User.objects.paginate(
        order_by="-created_at",
        limit=20,
        cursor=page.next_cursor,
    )
```

`Page` has three fields:

| Field | Meaning |
|---|---|
| `items` | Hydrated model instances for the current page. |
| `next_cursor` | Opaque string to pass to the next `paginate(...)` call, or `None`. |
| `has_next` | `True` when another page is available. |

Cursor pagination currently supports one stable order field:

```python
User.objects.paginate(order_by="id", limit=50)
User.objects.paginate(order_by="-created_at", limit=20, status="active")
```

Use a unique or near-unique ordering field such as `id`, `created_at`, or `-created_at`. The cursor stores the last item's ordered field value and applies `field__gt` for ascending order or `field__lt` for descending order on the next request. Invalid cursors raise `InvalidQueryError`, and `limit` must be non-negative.

---

## Upsert & Identity Rules

```python
@database_registry("sqlite:///app.db", table_name="users", unique_fields=["email"])
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str
    name: str

User.objects.create(email="alice@example.com", name="Alice")

# Resolves by unique_fields when key is absent
updated = User.objects.upsert(email="alice@example.com", name="Alicia")
```

**Upsert resolution order:**

1. If key is present → upsert by key
2. If autoincrement key is absent and `unique_fields` is configured → upsert by unique conflict key
3. Otherwise → falls back to create path

Persisted primary keys are **immutable** after first write.

---

## Bulk Operations

Optimized for service-layer write batches with normalized error behavior.

```python
rows = User.objects.bulk_create([
    {"email": "a@example.com", "name": "A"},
    {"email": "b@example.com", "name": "B"},
])

rows = User.objects.bulk_upsert([
    {"id": 1, "email": "a@example.com", "name": "A+"},
    {"id": 3, "email": "c@example.com", "name": "C"},
])
```

- Empty list input returns `[]`
- Integrity violations raise normalized DB exceptions
- Operations execute inside engine transaction contexts

---

## Schema Lifecycle & Evolution

### Registry-Level Production Lifecycle

`auto_create=True` remains the legacy quickstart default. For production services, prefer explicit registry ownership and `auto_create=False`, then create or validate schemas in startup code.

```python
db = DatabaseRegistry()

@db.database_registry(DB_URL, table_name="users", auto_create=False)
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str

db.create_all()                 # creates registered schemas + migration ledger table
db.check_all()                  # True when live schemas match registered models
diffs = db.diff_all()           # table -> SchemaDiff
db.assert_schema_current()      # raises MigrationError on drift
db.dispose_all()                # shutdown cleanup for this registry's engines
```

`create_all()` also ensures a lightweight `registers_schema_migrations` ledger table. The ledger is intentionally small; use Alembic or direct SQLAlchemy migrations for complex upgrade/downgrade workflows.

### Class-Level Schema Control

```python
User.create_schema()
User.schema_exists()
User.truncate()
User.drop_schema()
```

### Manager-Level Evolution Helpers

```python
# Idempotent — safe to call at every startup
created: bool = User.objects.ensure_column("timezone", str, nullable=True)

# Explicit — fails if column already exists
User.objects.add_column("timezone", str, nullable=True)

# Rebinds manager state; subsequent .objects calls use new table immediately
User.objects.rename_table("users_archive")

columns: list[str] = User.objects.column_names()
```

> **Prefer `ensure_column`** for startup-safe idempotent migrations. Use `add_column` when you explicitly want failure on pre-existing columns.

---

## Relationships

Define relationship descriptors **after** class decoration. All relationships are lazy-loaded and read-only.

`HasMany`, `BelongsTo`, and `HasManyThrough` remain the original names. The clearer cardinality aliases `OneToMany`, `ManyToOne`, and `ManyToMany` are equivalent and are often better in domain code.

```python
from pydantic import BaseModel
from registers import DatabaseRegistry, OneToMany, ManyToOne, ManyToMany, db_field

DB = "sqlite:///app.db"
db = DatabaseRegistry()

@db.database_registry(DB, table_name="authors")
class Author(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    name: str

@db.database_registry(DB, table_name="posts")
class Post(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    author_id: int = db_field(foreign_key="authors.id", index=True)
    title: str

@db.database_registry(DB, table_name="tags")
class Tag(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    name: str

@db.database_registry(DB, table_name="post_tags")
class PostTag(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    post_id: int = db_field(foreign_key="posts.id", index=True)
    tag_id: int = db_field(foreign_key="tags.id", index=True)

# Wire relationships after decoration
Author.posts = OneToMany(Post, foreign_key="author_id")
Post.author  = ManyToOne(Author, local_key="author_id")
Post.tags    = ManyToMany(Tag, through=PostTag, source_key="post_id", target_key="tag_id")
```

Batch-load relationships before rendering list views:

```python
from registers import prefetch

posts = Post.objects.all(order_by="-id")
prefetch(posts, "tags")

for post in posts:
    post.tags        # served from the prefetch cache; no per-post query
```

| Descriptor | Behavior |
|---|---|
| `HasMany` / `OneToMany` | Parent-to-children lookup. Returns a list. |
| `BelongsTo` / `ManyToOne` | Child-to-parent lookup. Returns one instance or `None` when the local FK is `None` or the parent row is missing. |
| `HasManyThrough` / `ManyToMany` | Many-to-many lookup through an explicit join model. Deduplicates repeated target IDs and skips missing target rows. |

### Relationship Integrity Rules

Relationships are read helpers; database integrity comes from schema constraints.

- Use `db_field(foreign_key="parent_table.id")` on child keys that must never become orphans.
- Use `index=True` on child FK fields used by parent mutation checks or common relationship lookups.
- Nullable FK fields are optional relationships. SQLite permits `NULL` child keys without requiring a parent row.
- Non-null FK fields are required relationships. Orphan inserts, invalid FK updates, and parent deletes with existing children fail at the database boundary.
- Many-to-many relationships should use an explicit join model with FK constraints on both sides when stale join rows are not acceptable.
- If you intentionally allow stale join rows, omit the target FK and the descriptor will skip missing targets when reading.
- Relationship descriptors discover custom manager attributes, so models registered with `manager_attr="records"` still work.
- Misconfigured relationship keys raise `RelationshipError` at access time with the missing field name.

### Adversarial Relationship Cases Covered In Tests

- SQLite FK enforcement is enabled on connections.
- Parent delete is restricted while child rows exist.
- FK updates to missing parents fail and roll back.
- Bulk inserts roll back as a unit when one row violates FK integrity.
- FK child key indexes are created when requested.
- `OneToMany`, `ManyToOne`, and `ManyToMany` aliases work with custom manager names and non-`id` primary keys.
- Duplicate join rows are deduplicated.
- Missing target rows in loose join tables are skipped.
- Invalid relationship keys fail with `RelationshipError`.

Primary references used for these tests: SQLite foreign-key enforcement and child-key index guidance, SQLAlchemy metadata/FK dependency behavior, and Pydantic validation semantics.

---

## Password Security

Password hashing is explicit. Mark the password field with `db_field(hash_password=True)` so schema intent is visible at the model definition.

```python
@database_registry("sqlite:///app.db", table_name="accounts")
class Account(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str
    password: str = db_field(hash_password=True)

acct = Account.objects.create(email="alice@example.com", password="secret123")

assert acct.password != "secret123"    # stored as hash
assert acct.verify_password("secret123")
```

**Hashing applies to configured fields on:** `create`, `strict_create`, `upsert`, `save`, `update_where`

Plain fields named `password` are stored as plain strings. This avoids hidden behavior; opt in where credential hashing is intended.

**Standalone helpers:**

```python
from registers import (
    PasswordHashPolicy,
    configure_password_policy,
    hash_password,
    is_password_hash,
    verify_and_upgrade_password,
    verify_password,
)

hashed = hash_password("secret123")
is_password_hash(hashed)               # True
verify_password("secret123", hashed)   # True

configure_password_policy(PasswordHashPolicy(scheme="pbkdf2_sha256", iterations=600_000))
ok, upgraded = verify_and_upgrade_password("secret123", older_hash)
if ok and upgraded is not None:
    Account.objects.update_where({"id": account.id}, password=upgraded)
```

---

## Transactions & Engine Lifecycle

### Explicit Transactions

Use `transaction()` to bind manager CRUD to one transaction. Reads inside the block see uncommitted writes, and any exception rolls back the whole unit.

```python
with db.transaction():
    user = User.objects.create(email="alice@example.com")
    Order.objects.create(user_id=user.id, total=12.50)

with User.objects.transaction():
    User.objects.create(email="bob@example.com")
```

### Engine Notes

- Engines are cached per database URL
- SQLite file engines enable WAL mode and enforce foreign keys automatically
- In-memory SQLite uses a shared static pool for process-local visibility

### Disposal

```python
User.objects.dispose()    # dispose one manager's engine

from registers import dispose_all
dispose_all()             # global cleanup — use at app shutdown / test teardown
```

---

## FastAPI Integration

### Lifespan Pattern

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not User.schema_exists():
        User.create_schema()
    yield
    User.objects.dispose()

app = FastAPI(lifespan=lifespan)
```

### Exception Mapping

```python
from fastapi.responses import JSONResponse
from registers import RecordNotFoundError, UniqueConstraintError, RegistryError

@app.exception_handler(UniqueConstraintError)
async def unique_error(_req, _exc):
    return JSONResponse(status_code=409, content={"detail": "Unique constraint violation"})

@app.exception_handler(RecordNotFoundError)
async def not_found_error(_req, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})

@app.exception_handler(RegistryError)
async def registry_error(_req, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
```

### Route Pattern

```python
@app.get("/users/{user_id}")
async def get_user(user_id: int):
    return User.objects.require(user_id)          # auto-raises 404 on miss

@app.patch("/users/{user_id}")
async def update_user(user_id: int, payload: UserUpdate):
    user = User.objects.require(user_id)
    user.name = payload.name
    user.save()
    return user
```

---

## Exception Model

Every exception inherits from `RegistryError` and carries `.context` and `.to_dict()` for structured observability.

```python
try:
    User.objects.require(email="missing@example.com")
except RecordNotFoundError as exc:
    payload = exc.to_dict()
    logger.error("record_not_found", extra=payload)
```

### Exception Reference

| Exception | Trigger |
|---|---|
| `ConfigurationError` | Invalid registry/model configuration |
| `ModelRegistrationError` | Registration contract violation |
| `SchemaError` | DDL failure |
| `MigrationError` | Column evolution failure |
| `RelationshipError` | Invalid relationship definition |
| `DuplicateKeyError` | Primary key collision |
| `InvalidPrimaryKeyAssignmentError` | Assigning a DB-managed key on create |
| `ImmutableFieldError` | Mutating a persisted primary key |
| `UniqueConstraintError` | Unique constraint violation |
| `RecordNotFoundError` | `require(...)` finds no match |
| `InvalidQueryError` | Unknown field, operator, or invalid value shape |

---

## Ecommerce Blueprint

A reference architecture for multi-entity API services.

### `models.py`

```python
from pydantic import BaseModel
from registers import DatabaseRegistry, db_field

DB = "sqlite:///ecommerce.db"
db = DatabaseRegistry()

@db.database_registry(DB, table_name="customers", key_field="id", unique_fields=["email"])
class Customer(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    name: str
    email: str
    password: str = db_field(hash_password=True)
    created_at: str
    updated_at: str

@db.database_registry(DB, table_name="products", key_field="id")
class Product(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    name: str
    price: float
    stock: int
    created_at: str
    updated_at: str

@db.database_registry(DB, table_name="orders", key_field="id")
class Order(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    customer_id: int
    total_amount: float
    created_at: str
    updated_at: str
```

### `api.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from registers import RecordNotFoundError, UniqueConstraintError, RegistryError
from .models import Customer, Product, Order

MODEL_REGISTRY = [Customer, Product, Order]

@asynccontextmanager
async def lifespan(app: FastAPI):
    for model in MODEL_REGISTRY:
        if not model.schema_exists():
            model.create_schema()
    yield
    for model in MODEL_REGISTRY:
        model.objects.dispose()

app = FastAPI(lifespan=lifespan)
```

### `services/orders.py` — Write Invariants & Compensation

```python
from fastapi import HTTPException
from ..models import Order, Product

def create_order(customer_id: int, items: list[dict], now: str) -> Order:
    snapshots: dict[int, Product] = {}
    total = 0.0

    for item in items:
        product = Product.objects.require(item["product_id"])
        if product.stock < item["quantity"]:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock for product {product.id}"
            )
        snapshots[product.id] = product
        total += product.price * item["quantity"]

    created: Order | None = None
    try:
        created = Order.objects.create(
            customer_id=customer_id,
            total_amount=round(total, 2),
            created_at=now,
            updated_at=now,
        )
        for item in items:
            product = snapshots[item["product_id"]]
            Product.objects.update_where(
                {"id": product.id},
                stock=product.stock - item["quantity"]
            )
        return created
    except Exception as exc:
        if created is not None:
            Order.objects.delete(created.id)
        for product_id, snapshot in snapshots.items():
            Product.objects.update_where({"id": product_id}, stock=snapshot.stock)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

### Endpoint-to-Manager Mapping

| HTTP Operation | Manager Call |
|---|---|
| `POST /customers` | `Customer.objects.create(...)` |
| `GET /customers/{id}` | `Customer.objects.require(id)` |
| `PATCH /customers/{id}` | mutate instance → `instance.save()` |
| `DELETE /customers/{id}` | `instance.delete()` |
| `GET /products` | `Product.objects.filter(order_by="-id", limit=..., offset=..., **filters)` |
| `POST /orders/checkout` | service layer: `require` + `create` + `update_where` + compensation |
| `GET /orders/{id}` | `Order.objects.require(id)` + child collections via `filter(...)` |

**Building filter dicts for optional criteria:**

```python
filters = {}
if min_price is not None:
    filters["price__gte"] = min_price
if category is not None:
    filters["category__eq"] = category

rows = Product.objects.filter(
    order_by="-id",
    limit=limit,
    offset=offset,
    **filters,
)
```

> Always build a `filters` dict before spreading — never pass `None` values as operator arguments.

### Smoke Test Runbook

```bash
# Start the server
uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload

# Health check
curl http://127.0.0.1:8000/health

# Create a customer
curl -X POST http://127.0.0.1:8000/customers \
  -H "Content-Type: application/json" \
  -d '{"name":"Alice","email":"alice@example.com","password":"secret123"}'

# Paginated product list
curl "http://127.0.0.1:8000/products?limit=20&offset=0"
```

**Expected outcomes:** health returns a success payload · customer create returns the record without a raw password · product list respects pagination and optional filters.

---

## Architecture Decision Guide

| Scenario | Recommended Pattern |
|---|---|
| Single service, minimal boilerplate | `@database_registry(...)` decorator mode |
| Explicit registry ownership, test isolation | `db = DatabaseRegistry()` instance mode |
| Multi-entity API with lifecycle hooks | Instance registry + lifespan + exception handlers |
| Schema validation at startup | `db.create_all()` plus `db.assert_schema_current()` |
| Atomic manager CRUD | `db.transaction()` or `Model.objects.transaction()` context manager |

---

## Migration from Legacy Imports

Legacy package names (`functionals`, `decorates`) should be migrated to `registers`. Use the canonical import style consistently:

```python
# ✅ Correct
from registers import database_registry, DatabaseRegistry, db_field

# ❌ Legacy — migrate away
from functionals import ...
from decorates import ...
```

---

<div align="center">

Built with [Pydantic](https://docs.pydantic.dev/) · Powered by [SQLAlchemy](https://www.sqlalchemy.org/)

</div>

---

## Public API Reference

### Top-Level Imports

```python
from registers import (
    database_registry,
    DatabaseRegistry,
    db_field,
    Q,
    Agg,
    Page,
    HasMany,
    BelongsTo,
    HasManyThrough,
    OneToMany,
    ManyToOne,
    ManyToMany,
    dispose_all,
    hash_password,
    is_password_hash,
    prefetch,
    PasswordHashPolicy,
    configure_password_policy,
    verify_and_upgrade_password,
    verify_password,
)
```

### Registry APIs

| API | Purpose |
|---|---|
| `database_registry(...)` | Module-level decorator for binding a Pydantic model to a table. |
| `DatabaseRegistry()` | Explicit registry object for isolated model sets. |
| `db.database_registry(...)` | Instance-level decorator equivalent to the module-level API. |
| `db.create_all()` / `db.check_all()` | Explicit schema creation and drift health checks for registered models. |
| `db.diff_all()` / `db.assert_schema_current()` | Structured schema drift reports and startup assertions. |
| `db.transaction()` | Bind manager CRUD across this registry to active transaction connection(s). |
| `db.dispose_all()` | Dispose engines used by this registry. |
| `dispose_all()` | Dispose globally cached engines; useful during shutdown or test teardown. |

### Manager APIs

| Category | Methods |
|---|---|
| Create/update | `create`, `strict_create`, `upsert`, `save`, `update_where` |
| Delete | `delete`, `delete_where` |
| Read | `get`, `require`, `filter`, `exclude`, `all`, `get_all`, `exists`, `count`, `first`, `last`, `refresh` |
| Projection/query helpers | `select`, `values_list`, `count_by`, `aggregate`, `paginate` |
| Bulk | `bulk_create`, `bulk_upsert` |
| Schema | `ensure_column`, `add_column`, `rename_table`, `column_names`, `diff_schema`, `assert_schema_current` |
| Transaction/lifecycle | `transaction`, `dispose` |

### Model Helpers

| Helper | Purpose |
|---|---|
| `instance.save()` | Persist changed instance state through manager upsert semantics. |
| `instance.delete()` | Delete the persisted record by primary key. |
| `instance.refresh()` | Reload the persisted record state. |
| `instance.verify_password(raw)` | Verify raw password against stored hash when the `password` field uses `db_field(hash_password=True)`. |
| `instance.verify_and_upgrade_password(raw)` | Verify and persist a policy-upgraded password hash when needed. |
| `Model.create_schema()` | Create the registered table. |
| `Model.schema_exists()` | Check whether the table exists. |
| `Model.truncate()` | Remove table data. |
| `Model.drop_schema()` | Drop the registered table. |

## Production Readiness Checklist

Before deploying a `registers.db` model layer, verify the following:

- [ ] Every registered model has an intentional `table_name`.
- [ ] Primary key behavior is explicit and tested.
- [ ] `unique_fields` or `db_field(unique=True)` is defined for natural identity where upsert is expected.
- [ ] Query filters are validated at service boundaries before being spread into manager calls.
- [ ] Optional filters are built in a dict and omit `None` values.
- [ ] Multi-record business invariants live in a service layer.
- [ ] Multi-record writes that must commit together use `db.transaction()` or `Model.objects.transaction()`.
- [ ] Production services use explicit startup schema lifecycle, commonly `auto_create=False` plus `create_all()`/`assert_schema_current()`.
- [ ] FastAPI exception handlers map registry errors to stable HTTP responses.
- [ ] Shutdown hooks call `dispose()` or `dispose_all()`.
- [ ] Password hashing behavior is documented in the API layer and raw passwords are never returned in responses.
- [ ] Schema evolution uses idempotent helpers such as `ensure_column(...)` unless failure-on-existing is desired.

## Recommended Positioning

Use `registers.db` as a lightweight persistence layer for Pydantic-centric services, prototypes that need real persistence, internal tools, and FastAPI backends where manager-style CRUD is preferred over direct ORM mapping. For complex transactional domains, highly customized SQL, or advanced migration workflows, keep service-layer boundaries explicit and use lower-level SQLAlchemy transactions where needed.

---

# Advanced Production API Guide

This section documents the production-oriented Python APIs implemented from `FUTURE.md`. It intentionally excludes fx project-helper and fx CLI workflows. All examples use the normal Python module surface.

## Imports

```python
from pydantic import BaseModel

from registers.db import (
    Agg,
    DatabaseRegistry,
    Q,
    audit_actor,
    database_registry,
    db_field,
    tenant_scope,
    unscoped,
)
from registers.db.testing import TestRegistry, assert_query_count, factory
```

## Registration Options

The original decorator remains valid. New options are additive:

```python
db = DatabaseRegistry()

@db.database_registry(
    "sqlite:///app.db",
    table_name="users",
    key_field="id",
    unique_fields=["email"],
    auto_create=False,
    timestamps=True,
    soft_delete=True,
    audit_log=True,
    tenant_field="tenant_id",
    encryption_key="dev-local-key",
)
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    tenant_id: str
    email: str
    name: str
    token: str = db_field(encrypted=True)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
```

Production services should prefer `auto_create=False`, call `db.create_all()` or migrations explicitly during startup, then call `db.assert_schema_current()` before serving traffic.

## Field Metadata

`db_field(...)` supports schema and behavior metadata:

```python
id: int | None = db_field(id_strategy="autoincrement", default=None)
email: str = db_field(unique=True, index=True, length=320)
amount: Decimal = db_field(precision=12, scale=2)
captured_at: datetime = db_field(timezone=True)
payload: bytes = db_field(column_type=LargeBinary(2048))
secret: str = db_field(encrypted=True)
display_name: str = db_field(exclude_from_db=True, default="")
```

Encrypted fields are written as ciphertext and decrypted when hydrating models. They cannot be used in filters or direct projections because equality on ciphertext is not stable or portable. `exclude_from_db=True` keeps a Pydantic field on the model without creating or writing a database column.

## CRUD Helpers

Existing `create`, `upsert`, `save`, `update_where`, `delete`, and `delete_where` continue to work. New helpers fill common service-layer gaps:

```python
user = User.objects.create(email="a@example.com", name="Alice")

user.update({"name": "Alicia"})
user.apply_patch(UserPatch(name="Ally"))  # uses exclude_unset=True for Pydantic patches

existing, created = User.objects.get_or_create(
    lookup={"email": "a@example.com"},
    defaults={"name": "Alice"},
)

updated, created = User.objects.update_or_create(
    lookup={"email": "a@example.com"},
    defaults={"name": "Alicia"},
)

deleted = User.objects.bulk_delete([1, 2, 3])
deleted = User.objects.bulk_delete(status="disabled")
deleted = User.objects.bulk_delete(dangerous_allow_full_table_delete=True)
```

`get_or_create` and `update_or_create` only accept lookups that match the primary key or the model's configured `unique_fields`. This keeps the API honest about race safety.

## Query API

Keyword filters still use the existing `field__operator=value` syntax. `Q` adds grouped boolean logic:

```python
active_or_trial = User.objects.filter(Q(status="active") | Q(status="trial"))
adults = User.objects.filter(Q(age__gte=18) & ~Q(status="banned"))
not_disabled = User.objects.exclude(status="disabled")
```

Projection helpers return lightweight shapes:

```python
rows = User.objects.select("id", "email", status="active")
emails = User.objects.values_list("email", q=Q(status="active"))
by_role = User.objects.count_by("role")
```

Aggregates use `Agg`:

```python
total = User.objects.aggregate(Agg.count("id"))
stats = User.objects.aggregate(
    total=Agg.count("id"),
    admins=Agg.count("id", role="admin"),
    avg_age=Agg.avg("age"),
)
```

A single unnamed aggregate returns a scalar. Multiple or named aggregates return a dictionary keyed by label.

## Cursor Pagination

```python
page = User.objects.paginate(order_by="-created_at", limit=20)

for user in page.items:
    ...

if page.has_next:
    next_page = User.objects.paginate(
        order_by="-created_at",
        limit=20,
        cursor=page.next_cursor,
    )
```

Cursor pagination currently supports one stable order field. Use unique or near-unique order fields such as `id`, `created_at`, or `-created_at` for predictable list endpoints.

## Transactions

Manager calls compose inside registry or manager transactions through context-bound connections:

```python
with db.transaction():
    user = User.objects.create(email="a@example.com", name="Alice")
    Order.objects.create(user_id=user.id, total=42)

with User.objects.transaction():
    User.objects.update_where({"id": 1}, name="Alicia")
```

If any call raises, the shared transaction rolls back.

## Async Mode

Use `async_mode=True` when an async application wants awaitable manager calls:

```python
@database_registry("sqlite:///app.db", table_name="users", async_mode=True)
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    name: str

async with User.objects.transaction():
    user = await User.objects.create(name="Alice")

count = await User.objects.count()
fresh = await User.objects.require(user.id)
```

The synchronous API remains the default. Async mode exposes an awaitable facade over the same manager behavior so validation, hooks, encryption, tenancy, and schema policy remain consistent.

## Timestamps And Hooks

`timestamps=True` automatically manages declared `created_at` and `updated_at` fields. The model must declare both fields.

Optional model hooks:

```python
class User(BaseModel):
    ...

    @staticmethod
    def before_create(data: dict) -> None:
        data["email"] = data["email"].lower()

    @staticmethod
    def after_create(user: "User") -> None:
        ...

    @staticmethod
    def before_save(user: "User") -> None:
        ...

    @staticmethod
    def after_save(user: "User") -> None:
        ...

    @staticmethod
    def before_delete(user: "User") -> None:
        ...

    @staticmethod
    def after_delete(user: "User") -> None:
        ...

    @staticmethod
    def before_bulk_create(rows: list[dict]) -> None:
        ...

    @staticmethod
    def after_bulk_create(users: list["User"]) -> None:
        ...
```

Hooks run inside the write flow. Exceptions abort the operation and roll back the active transaction.

## Soft Delete

`soft_delete=True` requires a nullable `deleted_at` field. Normal reads hide deleted rows.

```python
user.delete()                    # sets deleted_at
User.objects.delete(user.id)      # same
User.objects.count()              # excludes deleted rows
User.objects.filter(include_deleted=True)

restored = User.objects.restore(user.id)
User.objects.hard_delete(user.id)
User.objects.purge_deleted(before=datetime.now(timezone.utc))
```

Use `hard_delete` only for retention policies, privacy erasure, or cleanup jobs.

## Audit Logging

`audit_log=True` creates a companion table named `{table_name}_audit` by default. Use `audit_log_table="custom_audit"` to override it.

```python
with audit_actor("user:123"):
    user = User.objects.create(email="a@example.com", name="Alice")
    user.update({"name": "Alicia"})
    user.delete()

rows = User.objects.raw_dicts(
    "SELECT operation, actor FROM users_audit ORDER BY id"
)
```

Audit rows include table name, record id, operation, changed fields, actor, and timestamp.

## Multi-Tenancy

`tenant_field="tenant_id"` makes manager operations require an active tenant unless `unscoped()` is used.

```python
with tenant_scope("acme"):
    user = User.objects.create(email="a@example.com", name="Alice")
    assert user.tenant_id == "acme"
    User.objects.filter(status="active")  # automatically scoped

with unscoped():
    all_users = User.objects.all()
```

Creates auto-fill the tenant field. Passing a conflicting tenant value raises `InvalidQueryError`.

## Raw SQL

Raw SQL is available as an escape hatch. Use bound parameters.

```python
models = User.objects.raw(
    "SELECT * FROM users WHERE email = :email",
    {"email": "a@example.com"},
)

rows = User.objects.raw_dicts(
    "SELECT email, name FROM users WHERE status = :status",
    {"status": "active"},
)

result = User.objects.execute_raw(
    "UPDATE users SET name = :name WHERE id = :id",
    {"name": "Alicia", "id": 1},
)
```

Unsafe interpolation markers such as `%s` without parameters are rejected.

## Schema Lifecycle

Class-level and registry-level schema APIs:

```python
User.create_schema()
User.schema_exists()
User.truncate()
User.drop_schema()

db.create_all()
db.check_all()
db.diff_all()
db.schema_diff()
db.assert_schema_current()
db.dispose_all()
```

Safe additive migration helpers:

```python
diff = User.objects.schema_diff()
planned = User.objects.migrate(dry_run=True)
after = User.objects.migrate(dry_run=False)
```

`migrate(dry_run=False)` applies missing columns only. Destructive changes, renames, and complex data migrations should still use a dedicated migration tool such as Alembic.

## Relationships And Prefetch

Lazy relationships keep ergonomic access:

```python
author.posts
post.author
post.tags
```

Use `prefetch(records, "relationship_name")` for list endpoints to avoid N+1 reads:

```python
posts = Post.objects.filter(order_by="-created_at", limit=50)
prefetch(posts, "tags")

for post in posts:
    post.tags  # served from the prefetch cache
```

## Testing Utilities

`registers.db.testing` provides isolated test ergonomics:

```python
db = TestRegistry("sqlite:///:memory:")

@db.database_registry(table_name="users")
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str
    name: str

user_factory = factory(User, defaults={"name": "Test User"})

user = user_factory.create(email="a@example.com")
model_only = user_factory.build(email="b@example.com")
batch = user_factory.create_batch(3, email=lambda idx: f"user{idx}@example.com")

with assert_query_count(max=1):
    User.objects.count()
```

Use these helpers with the existing pytest cleanup fixture that calls `dispose_all()` between tests.

## Security Notes

Password hashing remains explicit:

```python
password: str = db_field(hash_password=True)
```

Use `PasswordHashPolicy`, `configure_password_policy`, and `verify_and_upgrade_password()` to keep hashes current. For encrypted fields, provide a stable application key or key provider and keep the key outside the database. Encrypted fields should not be used as lookup keys.

## Compatibility Notes

- Legacy synchronous manager calls continue to work.
- `auto_create=True` remains the default for quickstarts.
- Existing `Model.create_schema()` still means database DDL, not API input-schema generation.
- Async mode is opt-in.
- Soft delete, tenancy, audit logging, and encryption are opt-in per model.
- fx project-helper and fx CLI features are intentionally not covered here.
