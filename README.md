```text
         ______     ______     ______     __     ______     ______   ______     ______     ______    
        /\  == \   /\  ___\   /\  ___\   /\ \   /\  ___\   /\__  _\ /\  ___\   /\  == \   /\  ___\   
        \ \  __<   \ \  __\   \ \ \__ \  \ \ \  \ \___  \  \/_/\ \/ \ \  __\   \ \  __<   \ \___  \  
         \ \_\ \_\  \ \_____\  \ \_____\  \ \_\  \/\_____\    \ \_\  \ \_____\  \ \_\ \_\  \/\_____\ 
          \/_/ /_/   \/_____/   \/_____/   \/_/   \/_____/     \/_/   \/_____/   \/_/ /_/   \/_____/ 
```

**Build command systems, Pydantic-backed data services, and scheduled automation workflows with one coherent Python framework.**

[![PyPI version](https://img.shields.io/pypi/v/registers?color=5C6BC0\&labelColor=111827\&style=for-the-badge)](https://pypi.org/project/registers/)
[![Python](https://img.shields.io/pypi/pyversions/registers?color=0F766E\&labelColor=111827\&style=for-the-badge)](https://pypi.org/project/registers/)
[![License: MIT](https://img.shields.io/badge/License-MIT-7C3AED?labelColor=111827\&style=for-the-badge)](LICENSE)
[![SQLAlchemy](https://img.shields.io/badge/Powered%20by-SQLAlchemy-D97706?labelColor=111827\&style=for-the-badge)](https://www.sqlalchemy.org/)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-E92063?labelColor=111827\&style=for-the-badge)](https://docs.pydantic.dev/)
[![Tests](https://img.shields.io/badge/Tests-675%2B-2563EB?labelColor=111827\&style=for-the-badge)](#testing)

`registers.cli` · `registers.db` · `registers.cron` · `fx-tool`

---

## Tags

`Python Framework` `Developer Experience` `CLI Tooling` `Pydantic Persistence` `SQLAlchemy` `FastAPI` `Cron Automation` `Plugin Architecture` `Internal Tools` `Ops Workflows` `AI-Agent Friendly`

## What Is Registers?

**Registers** is a DX-first Python framework for building production-minded backend and operations tooling with a consistent decorator-driven programming model.

It gives you three integrated surfaces:

| Module           | Purpose                                                                 | Primary abstraction                  |
| ---------------- | ----------------------------------------------------------------------- | ------------------------------------ |
| `registers.cli`  | Command-line tools, interactive shells, plugin-driven operator consoles | `CommandRegistry`                    |
| `registers.db`   | Pydantic-first persistence backed by SQLAlchemy engines                 | `DatabaseRegistry` + `Model.objects` |
| `registers.cron` | Scheduled jobs, event jobs, retryable automation, workflow operations   | `CronRegistry`                       |

The companion package, **`fx-tool`**, provides project scaffolding, project operations, workflow management, and cron/runtime commands for Registers-based projects.

> **Positioning:** Registers is not a thin helper library. It is a coherent application infrastructure layer for engineers who want the ergonomics of decorators with the discipline of explicit registries, manager APIs, plugin composition, runtime state, and production-facing operational workflows.

---

## Why Registers?

Most Python service projects eventually accumulate the same supporting layers:

* a CLI for internal operations;
* a persistence layer for application data;
* scheduled jobs for maintenance and workflows;
* scripts for deployment, validation, and project management;
* documentation that explains how humans and coding agents should operate the project.

Registers provides these primitives through one consistent mental model:

```python
@cli.register(...)
def command(...): ...

@db.database_registry(...)
class Model(BaseModel): ...

@cron.job(...)
def job(...): ...
```

That consistency makes small projects faster to start and medium projects easier to scale.

---

## Core Features

| Capability                       | Description                                                                                                                  |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| **Decorator-first APIs**         | Register commands, models, and jobs through explicit decorators.                                                             |
| **Registry isolation**           | Use module-level facades for small projects or owned registry instances for tests, plugins, tenants, and multi-surface apps. |
| **Pydantic-first persistence**   | Persist Pydantic models without full ORM boilerplate while retaining SQLAlchemy-powered storage.                             |
| **Manager-style CRUD**           | Use `Model.objects.create`, `require`, `filter`, `upsert`, `bulk_create`, `bulk_upsert`, and schema helpers.                 |
| **CLI command runtime**          | Build scriptable CLIs, grouped operator consoles, structured output, async commands, prompts, safety gates, plugins, and context-driven workflows. |
| **Cron/event automation**        | Define manual, interval, cron-expression, webhook, and file-change jobs with retries and runtime state.                      |
| **Production error semantics**   | Structured exceptions, deterministic parse failures, duplicate/collision detection, and dead-letter states.                  |
| **FastAPI-ready patterns**       | Use lifespan hooks, exception handlers, service-layer invariants, and manager-based persistence.                             |
| **Agent-friendly documentation** | Designed to be readable by engineers and AI coding agents building from implementation instructions.                         |

---

## Installation

Install Registers:

```bash
pip install registers
```

Install aliasal CLI shell features:

```bash
pip install "registers[cli]"
```

The `cli` extra enables prompt_toolkit shell completion, history, and multiline input while preserving plain-text fallback behavior when it is not installed.

Install the companion project manager:

```bash
pip install fx-tool
```

Or install `fx-tool` from source:

```bash
pip install git+https://github.com/nexustech101/fx.git
```

Clone the companion repository directly:

```bash
git clone https://github.com/nexustech101/fx.git
```

---

## `registers.cli`

`registers.cli` is built for practical internal command tools: quick scripts, small persisted apps, grouped operator consoles, plugin-composed admin surfaces, and larger CLIs that need context injection and async handlers.

The main usage modes are:

* **Quick start scripts** with module-level decorators such as `@cli.register`, `@cli.argument`, and `cli.run()`.
* **App-level presentation** where a script can use Rich directly for a specific human-facing command while keeping the framework plain and scriptable.
* **Grouped and plugin-based CLIs** using `CommandRegistry()`, `group(...)`, `register_plugin(...)`, aliases, `Choice(...)`, `default=...`, `dry_run()`, and `confirm(...)`.
* **Larger internal tools** where the CLI is a thin operator surface over services, with `context_factory` handling env, region, tenant, account, or profile.

Small example:

```python
from registers import CommandRegistry

@cli.register(name="add", description="Create a task")
@cli.argument("title", type=str, help="Task title")
@cli.argument("owner", type=str, default="platform", help="Owning team")
def add_task(title: str, owner: str = "platform") -> dict[str, str]:
    return {"title": title, "owner": owner}

if __name__ == "__main__":
    cli.run(shell_title="Task Desk", shell_usage=True)
```

Grouped operator example:

```python
registry = cli.CommandRegistry()
deploy = registry.group("deploy", aliases=["d"], description="Deployment workflows")

@deploy.register("service", description="Deploy one service")
@deploy.argument("name", type=cli.types.Choice(["api", "worker", "billing"]))
@deploy.argument("version", type=str, default="latest")
@deploy.dry_run()
def deploy_service(name: str, version: str = "latest", dry_run: bool = False) -> str:
    action = "Would deploy" if dry_run else "Deploying"
    return f"{action} {name}:{version}"
```

Help displays choices and defaults, so operators can discover valid invocations without guessing:

```text
deploy service <name: api|worker|billing> [version] [--dry-run]
```

Interactive shell built-ins are intentionally small: `help`, `commands`, `exec`, `exit`, and `quit`. Runtime output flags include `--output json`, `--output csv`, `--output plain`, `--quiet`, and `--no-color`; use `--cli-output` when a command owns an `output` argument.

For the full CLI guide, including a todo app, a Rich table script, plugin composition, and larger internal-tool patterns, see `src/registers/cli/USAGE.md`.

---

## Quick Start: CLI + Database Todo App

The example below creates a small todo application with:

* an isolated command registry;
* an isolated database registry;
* a Pydantic model persisted to SQLite;
* commands for create/list/update/complete;
* an aliasal interactive shell.

```python
from __future__ import annotations

from enum import StrEnum
from time import strftime

from pydantic import BaseModel

from registers import CommandRegistry, DatabaseRegistry, db_field

cli = CommandRegistry()
db = DatabaseRegistry()

DB_PATH = "sqlite:///todos.db"
NOW = lambda: strftime("%Y-%m-%d %H:%M:%S")


class TodoStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


@db.database_registry(DB_PATH, table_name="todos", key_field="id")
class TodoItem(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    title: str = db_field(index=True)
    description: str = db_field(default="")
    status: TodoStatus = db_field(default=TodoStatus.PENDING.value)
    created_at: str = db_field(default_factory=NOW)
    updated_at: str = db_field(default_factory=NOW)


@cli.register(name="add", description="Create a todo item")
@cli.argument("title", type=str, help="Todo title")
@cli.argument("description", type=str, default="", help="Todo description")
@cli.alias("--add")
@cli.alias("-a")
def add_todo(title: str, description: str = "") -> str:
    todo = TodoItem.objects.create(title=title, description=description)
    return f"Added: {todo.title} (ID: {todo.id})"


@cli.register(name="list", description="List todo items")
@cli.alias("--list")
@cli.alias("-l")
def list_todos() -> str:
    todos = TodoItem.objects.all(order_by="id")
    if not todos:
        return "No todo items found."
    return "\n".join(f"{todo.id}: {todo.title} [{todo.status}]" for todo in todos)


@cli.register(name="complete", description="Mark a todo item as completed")
@cli.argument("todo_id", type=int, help="Todo ID")
@cli.alias("--complete")
@cli.alias("-c")
def complete_todo(todo_id: int) -> str:
    todo = TodoItem.objects.require(todo_id)
    todo.status = TodoStatus.COMPLETED.value
    todo.updated_at = NOW()
    todo.save()
    return f"Completed todo ID {todo_id}."


@cli.register(name="update", description="Update a todo item")
@cli.argument("todo_id", type=int, help="Todo ID")
@cli.argument("title", type=str, default=None, help="New title")
@cli.argument("description", type=str, default=None, help="New description")
@cli.alias("--update")
@cli.alias("-u")
def update_todo(
    todo_id: int,
    title: str | None = None,
    description: str | None = None,
) -> str:
    todo = TodoItem.objects.require(todo_id)
    if title is not None:
        todo.title = title
    if description is not None:
        todo.description = description
    todo.updated_at = NOW()
    todo.save()
    return f"Updated todo ID {todo_id}."


if __name__ == "__main__":
    cli.run(
        shell_title="Todo Console",
        shell_description="Manage tasks.",
        shell_banner=True,
        shell_usage=True,
    )
```

Run commands directly:

```bash
python todo.py add "Buy groceries" "Milk, eggs, bread"
python todo.py --add "Buy groceries" "Milk, eggs, bread"
python todo.py -a "Buy groceries" "Milk, eggs, bread"
python todo.py add --title "Buy groceries" --description "Milk, eggs, bread"

python todo.py list
python todo.py --list
python todo.py -l

python todo.py complete 1
python todo.py --complete 1
python todo.py -c 1

python todo.py update 1 "Read two books" "Finish both novels this week"
python todo.py update 1 --title "Read two books" --description "Finish both novels this week"
```

Run without arguments to enter the interactive shell:

```bash
python todo.py
```

Database IDs and credential hashing are explicit at the field definition:

```python
from uuid import UUID
from registers import db_field

id: int | None = db_field(id_strategy="autoincrement", default=None)
public_id: UUID | None = db_field(id_strategy="uuid4", default=None)
password: str = db_field(hash_password=True)
```

Plain `password` fields are stored as plain strings. Use `hash_password=True` only for credential fields that should be hashed on create, save, upsert, and `update_where`.

Password hashing defaults to PBKDF2-SHA256 with a configurable policy. Use `PasswordHashPolicy`, `configure_password_policy()`, and `verify_and_upgrade_password()` when you need stronger deployment-specific settings or login-time hash upgrades.

Use UUID primary keys for public-facing or distributed records:

```python
from uuid import UUID

from pydantic import BaseModel
from registers import DatabaseRegistry, db_field

db = DatabaseRegistry()

@db.database_registry("sqlite:///app.db", table_name="api_keys", key_field="id")
class ApiKey(BaseModel):
    id: UUID | None = db_field(id_strategy="uuid4", default=None)
    name: str
    owner_email: str

api_key = ApiKey.objects.create(name="Production", owner_email="ops@example.com")
assert isinstance(api_key.id, UUID)
```

Relationship descriptors support both original and cardinality-explicit names:

```python
from registers import ManyToMany, ManyToOne, OneToMany, prefetch

Author.posts = OneToMany(Post, foreign_key="author_id")
Post.author = ManyToOne(Author, local_key="author_id")
Post.tags = ManyToMany(Tag, through=PostTag, source_key="post_id", target_key="tag_id")

prefetch(Post.objects.all(), "tags")  # batch-load list-view relationships
```

Use `db_field(foreign_key="table.column", index=True)` on child keys when the database should reject orphans and relationship lookups need an index.

For production services, keep the quickstart ergonomics but make lifecycle explicit:

```python
db = DatabaseRegistry()

@db.database_registry("sqlite:///app.db", table_name="users", auto_create=False)
class User(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    email: str

db.create_all()
db.assert_schema_current()

with db.transaction():
    user = User.objects.create(email="alice@example.com")
```

---

## Quick Start: Database + FastAPI Service

Registers works naturally with FastAPI because application models remain Pydantic models while persistence is handled by the registered manager API.

```python
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from registers import DatabaseRegistry, RecordNotFoundError, UniqueConstraintError, db_field

DB_URL = "sqlite:///shop.db"
db = DatabaseRegistry()


@db.database_registry(DB_URL, table_name="customers", unique_fields=["email"])
class Customer(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    name: str
    email: str


@db.database_registry(DB_URL, table_name="products")
class Product(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    name: str
    price: float


@db.database_registry(DB_URL, table_name="orders")
class Order(BaseModel):
    id: int | None = db_field(id_strategy="autoincrement", default=None)
    customer_id: int
    product_id: int
    quantity: int
    total: float


MODELS = (Customer, Product, Order)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for model in MODELS:
        if not model.schema_exists():
            model.create_schema()
    yield
    for model in MODELS:
        model.objects.dispose()


app = FastAPI(lifespan=lifespan)


@app.post("/customers", response_model=Customer, status_code=201)
def create_customer(customer: Customer):
    return Customer.objects.create(**customer.model_dump(exclude={"id"}))


@app.get("/customers/{customer_id}", response_model=Customer)
def get_customer(customer_id: int):
    return Customer.objects.require(customer_id)


@app.post("/products", response_model=Product, status_code=201)
def create_product(product: Product):
    return Product.objects.create(**product.model_dump(exclude={"id"}))


@app.post("/orders", response_model=Order, status_code=201)
def create_order(customer_id: int, product_id: int, quantity: int):
    Customer.objects.require(customer_id)
    product = Product.objects.require(product_id)
    return Order.objects.create(
        customer_id=customer_id,
        product_id=product_id,
        quantity=quantity,
        total=round(product.price * quantity, 2),
    )


@app.get("/orders", response_model=list[Order])
def list_orders(limit: int = 20, offset: int = 0):
    return Order.objects.filter(order_by="-id", limit=limit, offset=offset)
```

Example requests:

```bash
curl -X POST "http://localhost:8000/customers" \
  -H "Content-Type: application/json" \
  -d '{"name":"Alice Johnson","email":"alice@example.com"}'

curl "http://localhost:8000/customers/1"

curl -X POST "http://localhost:8000/products" \
  -H "Content-Type: application/json" \
  -d '{"name":"Wireless Keyboard","price":49.99}'

curl -X POST "http://localhost:8000/orders?customer_id=1&product_id=1&quantity=2"

curl "http://localhost:8000/orders?limit=20&offset=0"
```

---

## Quick Start: Cron + Workflow Operations

Use `registers.cron` to define manual, interval, cron-expression, webhook, and file-change jobs.

```python
from __future__ import annotations

from registers import CronRegistry

cron = CronRegistry()


@cron.job
def rebuild(payload: dict | None = None) -> str:
    dry_run = bool((payload or {}).get("dry_run", False))
    return f"rebuilt: dry_run={dry_run}"


@cron.watch("src/**/*.py", debounce_seconds=1.0)
def rebuild_on_source_change(event: dict) -> str:
    return f"changed: {event['payload']['path']}"


@cron.job(
    name="nightly",
    trigger=cron.cron("0 2 * * *"),
    target="local_async",
    retry_policy="exponential",
    retry_max_attempts=5,
    retry_backoff_seconds=10,
    retry_max_backoff_seconds=180,
    retry_jitter_seconds=2,
)
def nightly() -> str:
    return "nightly complete"


print(cron.run("rebuild", payload={"dry_run": True}))
cron.register("nightly", root=".", apply=False)
```

Self-contained CLI management is also supported:

```python
from __future__ import annotations

from registers import CommandRegistry, CronRegistry

cli = CommandRegistry()
cron = CronRegistry()


@cron.job(name="nightly", trigger=cron.cron("0 2 * * *"))
def nightly() -> str:
    return "ok"


cron.install_cli()


if __name__ == "__main__":
    cli.run(shell_title="Automation Console", shell_usage=True)
```

```bash
python app.py cron jobs
python app.py cron run nightly .
python app.py cron register nightly . --target auto --apply
```

`--target auto` maps to the host platform scheduler and installs a persistent command that calls back into the same script.

---

## Architecture

Registers is built around explicit registries and predictable runtime boundaries.

```text
┌─────────────────────────────────────────────────────────────────────┐
│                           Registers Framework                        │
├──────────────────────┬──────────────────────┬───────────────────────┤
│ registers.cli         │ registers.db          │ registers.cron          │
│ Command runtime       │ Persistence layer     │ Automation runtime      │
├──────────────────────┼──────────────────────┼───────────────────────┤
│ CommandRegistry       │ DatabaseRegistry      │ CronRegistry            │
│ decorators            │ model decorators      │ job decorators          │
│ parser/dispatch       │ Model.objects         │ triggers/events         │
│ plugins/shell         │ query/schema helpers  │ daemon/workflows        │
└──────────────────────┴──────────────────────┴───────────────────────┘
                                │
                                ▼
                          fx-tool companion
                  scaffolding · runtime ops · workflows
```

### Module-level vs instance-level APIs

| Pattern             | Use when                                            | Example                       |
| ------------------- | --------------------------------------------------- | ----------------------------- |
| Module-level facade | Single app surface, simple scripts, fast onboarding | `from registers import CommandRegistry` |
| Instance registry   | Tests, plugins, isolated scopes, explicit ownership | `cli = CommandRegistry()`     |

The instance-registry pattern is the preferred production style for larger applications because it avoids global singleton coupling and makes tests/plugin boundaries explicit.

---

## Production Readiness

Registers is designed around production concerns that show up early in real projects:

* deterministic command and alias registration;
* collision-safe plugin composition;
* explicit parse and error semantics;
* schema lifecycle helpers;
* structured exceptions with context payloads;
* service-layer patterns for multi-record writes;
* retry and dead-letter behavior for automation;
* runtime history and status reporting;
* explicit resource disposal for shutdown and tests;
* documentation patterns that can be followed by engineers or AI coding agents.

---

## Who This Is For

Registers is built for:

* backend engineers building internal services and application utilities;
* platform and DevOps engineers standardizing automation workflows;
* teams building plugin-based command ecosystems;
* FastAPI developers who prefer Pydantic-first data modeling;
* AI tooling teams that need clear, structured, agent-readable implementation surfaces;
* solo engineers who want fast project setup without sacrificing architectural discipline.

---

## Documentation

| Document                  | Path                                      |
| ------------------------- | ----------------------------------------- |
| Project architecture spec | `PROJECT_SPEC.md`                         |
| CLI usage manual          | `src/registers/cli/USAGE.md`              |
| DB usage manual           | `src/registers/db/USAGE.md`               |
| Cron usage manual         | `src/registers/cron/USAGE.md`             |
| FX tool repository        | `https://github.com/nexustech101/fx-tool` |

---

## Roadmap

Registers is production-ready today and designed to expand into higher-level automation and agentic tooling workflows.

Planned extensions include:

* **MCP support** — decorator-based primitives for defining and operating MCP servers;
* **workspace state capabilities** — structured storage and retrieval of project/worktree state;
* **AI tooling data structures** — graph and tree primitives for context modeling, traversal, and project representation;
* **LLM tooling decorators** — decorator-driven tool definitions and memory/knowledge wiring for agent workflows.

These additions are intended to extend the current `fx-tool + cli + db + cron` architecture rather than replace it.

---

## Requirements

* Python 3.10+
* Pydantic 2.x
* SQLAlchemy 2.x

aliasal integrations depend on the modules you use:

* FastAPI for API service examples;
* Watchdog for file-change job triggers;
* Docker or compatible services for multi-dialect database integration tests.

---

## Testing

The package is backed by a production-focused test suite covering unit behavior, edge cases, SQLite behavior, and multi-dialect integration scenarios.

```bash
pytest
```

For backend integration tests, start Docker Desktop or another compatible Docker engine before running tests that depend on services from `docker-compose.test-db.yml`.

---

## License

MIT

