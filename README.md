# decorators

A Python framework built with **Developer Experience (DX)** in mind. `decorators` uses a clean, ergonomic decorator registry design pattern to eliminate boilerplate when building CLI tools and database-backed applications — from lightweight scripts and data engineering pipelines to full ecommerce systems and enterprise-scale relational models.

Designed to integrate seamlessly with **FastAPI** and other ASGI/WSGI frameworks out of the box.

```
pip install decorators
```

---

## Contents

- [Why decorators?](#why-decorators)
- [Packages](#packages)
- [decorators.cli — CLI Framework](#decoratorscli)
  - [Quick Start](#cli-quick-start)
  - [Argument Types](#argument-types)
  - [Command Aliases](#command-aliases)
  - [Dependency Injection](#dependency-injection)
  - [Middleware](#middleware)
  - [Plugin System](#plugin-system)
  - [Error Handling](#error-handling)
- [decorators.db — Database Registry](#decoratorsdb)
  - [Quick Start](#db-quick-start)
  - [CRUD API](#crud-api)
  - [Querying](#querying)
  - [Schema Management](#schema-management)
  - [Relationships](#relationships)
  - [FastAPI Integration](#fastapi-integration)
- [Installation](#installation)
- [Requirements](#requirements)

---

## Why decorators?

Most Python projects involve some combination of two recurring problems: **wiring up CLI commands** and **persisting data models**. The standard solutions — `argparse`, raw SQLAlchemy, `click` — are powerful but verbose. You spend more time writing plumbing than writing logic.

`decorators` solves both with a consistent design philosophy: **register once, use everywhere**. A single decorator on a function makes it a CLI command. A single decorator on a Pydantic model gives it a full persistence layer. The framework handles the wiring; you write the behaviour.

**It is particularly well-suited for:**

- **Data engineering and modeling** — define typed, validated models with automatic table creation and schema evolution
- **Ecommerce and multi-entity systems** — first-class relationship descriptors for `HasMany`, `BelongsTo`, and `HasManyThrough`
- **Enterprise relational schemas** — transaction support, upsert semantics, and unique constraint management
- **FastAPI services** — models attach `create_schema` / `drop_schema` / `schema_exists` class methods that slot directly into FastAPI's `lifespan` startup hooks
- **Rapid CLI tooling** — go from a plain Python function to a fully-parsed, aliased, DI-wired CLI command in one decorator

---

## Packages

| Package | Purpose |
|---|---|
| `decorators.cli` | Decorator-based CLI framework with argparse, DI, middleware, and plugin loading |
| `decorators.db` | SQLAlchemy-backed persistence manager for Pydantic models |

Both packages are independent. Use one, the other, or both together.

---

## decorators.cli

A lightweight, decorator-driven CLI framework. Register Python functions as subcommands — argument parsing, type coercion, alias resolution, dependency injection, and middleware hooks are all handled by the framework.

### CLI Quick Start

```python
from decorators.cli import CommandRegistry

cli = CommandRegistry()


@cli.register(
    ops=["-g", "--greet"],
    name="greet",
    description="Greet someone by name",
)
def greet(name: str) -> str:
    return f"Hello, {name}!"


if __name__ == "__main__":
    cli.run()
```

```bash
python app.py greet Alice
python app.py --greet Alice
python app.py g Alice
# → Hello, Alice!
```

### Argument Types

Argument behaviour is inferred directly from Python type annotations — no schema definitions, no `add_argument` calls.

| Annotation | CLI behaviour |
|---|---|
| `str` | Required positional argument |
| `int` | Required positional integer (auto-coerced) |
| `float` | Required positional float (auto-coerced) |
| `bool` | Optional `--flag` (store_true) |
| `Optional[T]` | Optional `--arg value` |
| Defaulted parameter | Optional `--arg value` |

```python
@cli.register(name="create-report", description="Generate a report")
def create_report(
    title: str,           # required positional
    pages: int,           # required positional, coerced to int
    verbose: bool = False,  # optional --verbose flag
    output: Optional[str] = None,  # optional --output path
) -> str:
    ...
```

```bash
python app.py create-report "Q3 Summary" 12 --verbose --output ./reports
```

### Command Aliases

The `ops` field registers shorthand and flag-style aliases alongside the canonical command name. All three forms are resolved automatically:

```python
@cli.register(
    ops=["-s", "--sync"],
    name="sync",
    description="Sync the database",
)
def sync(target: str) -> None:
    ...
```

```bash
python app.py sync production
python app.py --sync production
python app.py s production
```

### Dependency Injection

Use `DIContainer` to bind service instances to types. Any command parameter whose type is registered in the container is injected automatically and hidden from the CLI — callers never need to pass it.

```python
from decorators.cli import CommandRegistry, DIContainer, Dispatcher, build_parser

registry = CommandRegistry()
container = DIContainer()

container.register(DatabaseService, DatabaseService(url="sqlite:///app.db"))


@registry.register(name="seed", description="Seed the database")
def seed(count: int, db: DatabaseService) -> str:
    db.insert_fixtures(count)
    return f"Seeded {count} records."


parser = build_parser(registry, container)
dispatcher = Dispatcher(registry, container)
args = parser.parse_args()

if args.command:
    cli_args = {k: v for k, v in vars(args).items() if k != "command"}
    dispatcher.dispatch(args.command, cli_args)
```

```bash
python app.py seed 100   # `db` is injected; only `count` appears on the CLI
```

### Middleware

`MiddlewareChain` provides ordered pre- and post-execution hooks. Pre-hooks receive the command name and resolved kwargs; post-hooks receive the command name and return value.

```python
from decorators.cli import CommandRegistry, MiddlewareChain, logging_middleware_pre, logging_middleware_post

cli = CommandRegistry()
chain = MiddlewareChain()

chain.add_pre(logging_middleware_pre)    # built-in: logs command + args, starts timer
chain.add_post(logging_middleware_post)  # built-in: logs completion + elapsed time

# Custom hook
def audit_hook(command: str, result: Any) -> None:
    audit_log.write(command, result)

chain.add_post(audit_hook)

cli.run(middleware=chain)
```

### Plugin System

`load_plugins` dynamically imports every non-private module in a package. Any `@registry.register(...)` calls at module level execute on import — no manual wiring in `main.py` required.

```python
from decorators.cli import CommandRegistry, load_plugins

cli = CommandRegistry()
load_plugins("app.commands", cli)  # auto-discovers app/commands/*.py

if __name__ == "__main__":
    cli.run()
```

```
app/
  commands/
    users.py     # @cli.register(name="create-user", ...)
    reports.py   # @cli.register(name="export", ...)
    deploy.py    # @cli.register(name="deploy", ...)
```

### Error Handling

The framework does not impose an error handling policy. A clean pattern is to wrap command handlers with your own decorator:

```python
import functools, sys
from typing import Any, Callable


def handle_errors(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            sys.exit(0)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    return wrapper


@cli.register(name="deploy", description="Deploy to an environment")
@handle_errors
def deploy(env: str) -> str:
    ...
```

---

## decorators.db

A SQLAlchemy-backed persistence manager for Pydantic models. One decorator gives your model a full CRUD interface, automatic table creation, schema evolution helpers, and opt-in relationship descriptors — with no separate repository classes, no manual session management, and no raw SQL.

### DB Quick Start

```python
from pydantic import BaseModel
from decorators.db import database_registry


@database_registry(
    "app.db",
    table_name="users",
    key_field="id",
    autoincrement=True,
    unique_fields=["email"],
)
class User(BaseModel):
    id: int | None = None
    name: str
    email: str


# Create
user = User.objects.create(name="Alice", email="alice@example.com")

# Read
user = User.objects.get(1)
user = User.objects.get(email="alice@example.com")

# Update
user.name = "Alicia"
user.save()

# Delete
user.delete()
```

### CRUD API

All write operations live on the manager (`Model.objects`). Three instance methods — `save()`, `delete()`, and `refresh()` — are injected directly onto model instances for convenience.

#### Manager operations

```python
# Strict insert — raises DuplicateKeyError on collision
user = User.objects.create(name="Bob", email="bob@example.com")

# Atomic upsert — INSERT … ON CONFLICT DO UPDATE, no race conditions
user = User.objects.upsert(name="Bob", email="bob@example.com")

# Bulk field update — returns refreshed records
updated = User.objects.update_where({"role": "trial"}, role="active")

# Delete by primary key
User.objects.delete(user_id)

# Delete by criteria — returns row count
count = User.objects.delete_where(role="inactive")
```

#### Instance operations

```python
# Upsert this instance
user.save()

# Delete this instance's row
user.delete()

# Re-fetch from the database (raises RecordNotFoundError if gone)
fresh = user.refresh()
```

### Querying

```python
# All rows
users = User.objects.all()

# Filter with equality criteria
admins = User.objects.filter(role="admin")

# Pagination
page = User.objects.filter(role="active", limit=20, offset=40)

# First / last
newest = User.objects.last()
first_trial = User.objects.first(role="trial")

# Get one or None
user = User.objects.get(1)
user = User.objects.get(email="alice@example.com")

# Get or raise RecordNotFoundError
user = User.objects.require(1)

# Existence and count
exists = User.objects.exists(email="alice@example.com")
total  = User.objects.count(role="active")
```

### Schema Management

Table creation happens automatically on decoration (`auto_create=True` by default). Schema helpers are accessible as both class methods and via the manager:

```python
# Idempotent table creation
User.create_schema()         # or User.objects.create_schema()

# Inspection
User.schema_exists()         # or User.objects.schema_exists()

# Destructive operations
User.truncate()              # delete all rows, keep schema
User.drop_schema()           # drop the table entirely

# Additive column evolution (no migration framework required)
User.objects.add_column("verified_at", Optional[datetime])
User.objects.ensure_column("verified_at", Optional[datetime])  # idempotent

# Explicit transaction for batched atomicity
with User.objects.transaction() as conn:
    User.objects.create(name="Alice", email="alice@example.com")
    Profile.objects.create(user_id=1, bio="...")
```

### Relationships

Relationships are lazy-loaded, read-only descriptors assigned after class decoration. This pattern avoids conflicts with Pydantic's metaclass and naturally resolves forward-reference ordering.

```python
from decorators.db import database_registry
from decorators.db.relations import HasMany, BelongsTo, HasManyThrough


@database_registry("store.db", table_name="authors", key_field="id", autoincrement=True)
class Author(BaseModel):
    id: int | None = None
    name: str


@database_registry("store.db", table_name="posts", key_field="id", autoincrement=True)
class Post(BaseModel):
    id: int | None = None
    author_id: int
    title: str


@database_registry("store.db", table_name="post_tags", key_field="id", autoincrement=True)
class PostTag(BaseModel):
    id: int | None = None
    post_id: int
    tag_id: int


@database_registry("store.db", table_name="tags", key_field="id", autoincrement=True)
class Tag(BaseModel):
    id: int | None = None
    name: str


# Optionally declare relationships after all classes are decorated (not required)
Author.posts = HasMany(Post, foreign_key="author_id")
Post.author  = BelongsTo(Author, local_key="author_id")
Post.tags    = HasManyThrough(Tag, through=PostTag, source_key="post_id", target_key="tag_id")
```

```python
author = Author.objects.require(1)
author.posts        # → list[Post]

post = Post.objects.require(1)
post.author         # → Author | None
post.tags           # → list[Tag]
```

| Descriptor | Relationship | Example |
|---|---|---|
| `HasMany` | One-to-many | `Author → Posts` |
| `BelongsTo` | Many-to-one | `Post → Author` |
| `HasManyThrough` | Many-to-many via join table | `Post ↔ Tags` |

### FastAPI Integration

`decorators.db` integrates cleanly with FastAPI's `lifespan` pattern for schema initialization and engine disposal:

```python
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from models import User, Product, Order

def initialize_schemas():
    """Create every table schema exactly once on app startup (idempotent)."""
    logging.info("    Initializing ecommerce database schemas...")

    models = [
        User,
        Product,
        Order,
    ]

    for model in models:
        try:
            # The Production Spec guarantees these schema methods exist on the
            # registry/manager attached to the model. We call them directly on
            # the class (the most ergonomic pattern for FastAPI usage).
            if not model.schema_exists():
                model.create_schema()
                logging.info(f"Schema created - {model.__name__}")
            else:
                logging.info(f"Schema already exists - {model.__name__}")
        except AttributeError:
            # Safety net in case the manager is attached under a different name
            # (e.g. model.manager or model.registry). The core CRUD routes will
            # still work.
            logging.warning(
                f"Schema methods not directly on {model.__name__}. "
                "Manual schema creation may be required."
            )
        except Exception as exc:  # catches SchemaError, etc.
            logging.error(f"Failed to initialize {model.__name__}: {exc}")


def dispose_engines():
    """Dispose all SQLAlchemy engines on app shutdown to close DB connections."""
    logging.info("Disposing database engines...")

    models = [
        User,
        Product,
        Order,
    ]

    for model in models:
        try:
            if model.schema_exists():
                model.drop_schema()
                logging.info(f"Engine dropped → {model.__name__}")
            else:
                logging.info(f"Engine does not exist → {model.__name__}")
        except Exception as exc:  # catches SchemaError, etc.
            logging.error(f"Failed to dispose {model.__name__}: {exc}")

def dispose_engines():
    for model in [User, Product, Order]:
        model.objects.dispose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_schemas()
    yield
    dispose_engines()


app = FastAPI(lifespan=lifespan)


@app.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int):
    return User.objects.require(user_id)


@app.post("/users/", response_model=User)
async def create_user(user: User):
    return User.objects.create(**user.model_dump(exclude={"id"}))
```

---

## Error Reference

### decorators.cli

| Exception | Raised when |
|---|---|
| `DuplicateCommandError` | A command name is registered more than once |
| `UnknownCommandError` | A requested command has no registered handler |
| `DependencyNotFoundError` | The DI container cannot resolve a required type |
| `PluginLoadError` | A plugin module fails to import |

### decorators.db

| Exception | Raised when |
|---|---|
| `ModelRegistrationError` | The decorated class is not a valid Pydantic `BaseModel` |
| `ConfigurationError` | Decorator options reference non-existent fields or are invalid |
| `DuplicateKeyError` | An `INSERT` collides with an existing primary key |
| `UniqueConstraintError` | An `INSERT` or `UPDATE` violates a `UNIQUE` constraint |
| `RecordNotFoundError` | `require()` finds no matching row |
| `InvalidQueryError` | Filter criteria reference unknown fields or are malformed |
| `SchemaError` | A DDL operation (`CREATE` / `DROP` / `ALTER`) fails |
| `MigrationError` | A schema evolution step cannot be applied |
| `RelationshipError` | A relationship descriptor is misconfigured or accessed before setup |

All exceptions inherit from `FrameworkError` (CLI) or `RegistryError` (DB) for broad catch-all handling.

---

## Installation

```bash
pip install decorators
```

**Development install (with test dependencies):**

```bash
pip install "decorators[dev]"
```

**From source:**

```bash
git clone https://github.com/yourname/decorators
pip install ./decorators
```

---

## Requirements

- Python ≥ 3.10
- pydantic ≥ 2.0
- sqlalchemy ≥ 2.0

---

## License

MIT