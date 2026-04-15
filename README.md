# Framework

Decorator-driven tooling for Python:

- `decorates.cli` for ergonomic command-line apps
- `decorates.db` for Pydantic + SQLAlchemy persistence

The philosophy is simple: minimal setup, predictable behavior, and a fast path to shipping.

## Install

```bash
pip install decorates
```

## Quick Start Guide

1. Build one CLI command with a decorator.
2. Build one DB model with a decorator.
3. Use `Model.objects` for CRUD.

### CLI in 60 seconds

```python
from decorates.cli import CommandRegistry

cli = CommandRegistry()

# ── built-in help alias ────────────────────────────────────────────────────

@cli.register(
    options=["-g", "--greet"],
    name="greet",
    description="Greet someone",
)
def greet(name: str) -> str:
    return f"Hello, {name}!"

@cli.register(
    options=["-h", "--help"],
    name="help",
    description="List all registered commands",
)
def list_clis() -> None:
    cli.list_clis()


if __name__ == "__main__":
    cli.run()
```

```bash
python app.py greet Alice
python app.py --greet Alice
python app.py g Alice

python app.py help
python app.py --help
python app.py h
```

```bash
Hello, Alice!

Available commands:
  greet [-g, --greet]: Greet someone
  help [-h, --help]: List all registered commands
```

### Database + FastAPI in 5 minutes

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from decorates.db import (
    RecordNotFoundError,
    UniqueConstraintError,
    database_registry,
)

DB_URL = "sqlite:///shop.db"


@database_registry(DB_URL, table_name="customers", unique_fields=["email"])
class Customer(BaseModel):
    id: int | None = None
    name: str
    email: str


@database_registry(DB_URL, table_name="products")
class Product(BaseModel):
    id: int | None = None
    name: str
    price: float


@database_registry(DB_URL, table_name="orders")
class Order(BaseModel):
    id: int | None = None
    customer_id: int
    product_id: int
    quantity: int
    total: float


class CreateCustomer(BaseModel):
    name: str
    email: str


class CreateProduct(BaseModel):
    name: str
    price: float


class CreateOrder(BaseModel):
    customer_id: int
    product_id: int
    quantity: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    for model in (Customer, Product, Order):
        model.create_schema()
    yield
    for model in (Customer, Product, Order):
        model.objects.dispose()


app = FastAPI(lifespan=lifespan)


@app.post("/customers", response_model=Customer, status_code=201)
def create_customer(payload: CreateCustomer):
    try:
        return Customer.objects.create(**payload.model_dump())
    except UniqueConstraintError:
        raise HTTPException(status_code=409, detail="Email already exists")


@app.get("/customers/{customer_id}", response_model=Customer)
def get_customer(customer_id: int):
    try:
        return Customer.objects.require(customer_id)
    except RecordNotFoundError:
        raise HTTPException(status_code=404, detail="Customer not found")


@app.post("/products", response_model=Product, status_code=201)
def create_product(payload: CreateProduct):
    return Product.objects.create(**payload.model_dump())


@app.post("/orders", response_model=Order, status_code=201)
def create_order(payload: CreateOrder):
    customer = Customer.objects.get(payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    product = Product.objects.get(payload.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    return Order.objects.create(
        customer_id=customer.id,
        product_id=product.id,
        quantity=payload.quantity,
        total=product.price * payload.quantity,
    )


@app.get("/orders/desc", response_model=list[Order])
def list_orders_desc(limit: int = 20, offset: int = 0):  # Filter by oldest   (1, 2, 3...n)
    return Order.objects.filter(order_by="id", limit=limit, offset=offset)


@app.get("/orders/asc", response_model=list[Order])
def list_orders_asc(limit: int = 20, offset: int = 0):  # Filter by newest  (n...3, 2, 1)
    return Order.objects.filter(order_by="-id", limit=limit, offset=offset)
```

## Core Concepts

### `decorates.cli`

- Register functions as commands with `@cli.register(...)`.
- Type annotations drive argument parsing.
- Optional command aliases with `options=["-x", "--long"]`.
- Optional DI (`DIContainer`) and middleware (`MiddlewareChain`).
- `CommandRegistry.run()` preserves decorates exceptions and wraps unexpected handler crashes as `CommandExecutionError` (with original exception chaining).
- Operational logs use standard Python logging namespaces under `decorates.cli.*`.

### `decorates.db`

- Register `BaseModel` classes with `@database_registry(...)`.
- Access all persistence through `Model.objects`.
- `id: int | None = None` gives database-managed autoincrement IDs.
- Schema helpers are available as class methods: `create_schema`, `drop_schema`, `schema_exists`, `truncate`.
- Unexpected SQLAlchemy runtime failures are normalized into `SchemaError` for cleaner, predictable error handling.
- Operational logs use standard Python logging namespaces under `decorates.db.*`.
- DB exceptions provide structured metadata (`exc.context`, `exc.to_dict()`) for production diagnostics.

## `decorates.db` Usage Snapshot

```python
# Filtering operators
Order.objects.filter(total__gte=100)
Customer.objects.filter(email__ilike="%@example.com")
Order.objects.filter(quantity__in=[1, 2, 3])

# Sorting and pagination
Order.objects.filter(order_by="-id", limit=20, offset=0)

# Bulk writes
Product.objects.bulk_create([...])
Product.objects.bulk_upsert([...])

# Additive migration helpers
Customer.objects.ensure_column("phone", str | None, nullable=True)
Customer.objects.rename_table("customers_archive")
```

After `rename_table(...)` succeeds, the same `Model.objects` manager and
schema helpers are immediately bound to the new table name.

If your model contains a field named `password`, password values are automatically hashed on write, and instances receive `verify_password(...)`.

## Documentation

- DB guide: `src/decorates/db/USAGE.md`
- CLI source API: `src/decorates/cli`
- DB source API: `src/decorates/db`

## Requirements

- Python 3.10+
- `pydantic>=2.0`
- `sqlalchemy>=2.0`

## Testing

- Default `pytest` includes SQLite plus PostgreSQL/MySQL rename-state integration tests.
- Start Docker Desktop (or another Docker engine) before running tests so
  `docker-compose.test-db.yml` services can boot.
- The decorates is backed by a rigorous, production-focused test suite (170+ tests) that covers unit, edge-case, and multi-dialect integration behavior.

## License

MIT
