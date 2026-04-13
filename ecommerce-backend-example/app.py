import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Path, APIRouter
from fastapi.middleware.cors import CORSMiddleware

from utils import rate_limiter, fastapi_exception_handler
from models import (
    Customer,
    Address,
    Product,
    PaymentMethod,
    Category,
    Tag,
    ProductCategory,
    ProductTag,
    Review,
    Order,
    OrderItem,
    OrderPayment,
)
from decorators.db import database_registry

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def initialize_schemas():
    """Create every table schema exactly once on app startup (idempotent)."""
    logging.info("    Initializing ecommerce database schemas...")

    models = [
        Customer,
        Address,
        Product,
        PaymentMethod,
        Category,
        Tag,
        ProductCategory,
        ProductTag,
        Review,
        Order,
        OrderItem,
        OrderPayment,
    ]

    for model in models:
        try:
            # The Production Spec guarantees these schema methods exist on the
            # registry/manager attached to the model. We call them directly on
            # the class (the most ergonomic pattern for FastAPI usage).
            if not model.schema_exists():
                model.create_schema()
                logging.info(f"Schema created → {model.__name__}")
            else:
                logging.info(f"Schema already exists → {model.__name__}")
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
        Customer,
        Address,
        Product,
        PaymentMethod,
        Category,
        Tag,
        ProductCategory,
        ProductTag,
        Review,
        Order,
        OrderItem,
        OrderPayment,
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_schemas()
    yield
    dispose_engines()


# Create the application instance with the lifespan context manager for startup/shutdown hooks.
app = FastAPI(lifespan=lifespan)
router = APIRouter(prefix="/api/v1")  # Versioned API prefix for future-proofing

# CORS configuration for frontend integration (adjust origins as needed)
origins = [
    "http://localhost:3000",
    "http://localhost:8000",
    "http://localhost:8080",
    "http://localhost:5000",
    "*"  # Allow all origins (for development only; restrict in production)
]

# Apply CORS middleware to the app with the specified configuration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,           # Allowed domains
    allow_credentials=True,          # Allow cookies & auth headers
    allow_methods=["*"],             # Allow all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],             # Allow all headers
)

# ---- Customer Endpoints ----

@router.post("/customers/", response_model=Customer)
@fastapi_exception_handler()
@rate_limiter(max_calls=5, period=15)
async def create_customer(customer: Customer):
    try:
        return Customer.objects.create(**customer.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/customers/{customer_id}", response_model=Customer)
@fastapi_exception_handler()
@rate_limiter(max_calls=5, period=15)  # 5 requests per minute per IP
async def get_customer(customer_id: int = Path(...)):
    customer = Customer.objects.get(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer

@router.get("/customers/", response_model=list[Customer])
@fastapi_exception_handler()
@rate_limiter(max_calls=5, period=15)  # 5 requests per minute per IP
async def list_customers():
    return Customer.objects.all()

@router.put("/customers/{customer_id}", response_model=Customer)
@fastapi_exception_handler()
@rate_limiter(max_calls=5, period=15)  # 5 requests per minute per IP
async def update_customer(customer_id: int, customer: Customer):
    obj = Customer.objects.get(customer_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Customer not found")
    for field, value in customer.model_dump().items():
        setattr(obj, field, value)
    obj.save()
    return obj

@router.delete("/customers/{customer_id}")
@fastapi_exception_handler()
@rate_limiter(max_calls=5, period=15)  # 5 requests per minute per IP
async def delete_customer(customer_id: int):
    customer = Customer.objects.get(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer.delete()
    return {"ok": True}

# ---- Schema Management ----

@router.post("/customers/schema/create")
@fastapi_exception_handler()
def create_customer_schema():
    Customer.create_schema()
    return {"ok": True}

@router.post("/customers/schema/drop")
@fastapi_exception_handler()
def drop_customer_schema():
    Customer.drop_schema()
    return {"ok": True}

@router.get("/customers/schema/exists")
@fastapi_exception_handler()
def customer_schema_exists():
    return {"exists": Customer.schema_exists()}

@router.post("/customers/schema/truncate")
@fastapi_exception_handler()
@rate_limiter(max_calls=5, period=15)  # 5 requests per minute per IP
def truncate_customer_schema():
    Customer.truncate()
    return {"ok": True}


app.include_router(router)