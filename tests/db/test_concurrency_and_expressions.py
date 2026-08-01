"""
Adversarial suite for concurrency, ``F()`` expressions, optimistic locking,
multi-level prefetch and the migration ledger.

Written from ``SPEC_concurrency.md`` alone, as an independent verifier: the
assertions describe the documented contract, not the implementation. The
headline assertion style here is *conservation* — ``initial - final ==
accepted_count`` — because that is what catches silent loss. "No exception was
raised" is not evidence that money moved correctly.

Test names are prefixed with the spec item number they cover.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, event, text

import registers
from conftest import backend_table_name, db_url
from registers import (
    BelongsTo,
    DatabaseRegistry,
    F,
    HasMany,
    InvalidQueryError,
    ModelRegistrationError,
    RelationshipError,
    StaleDataError,
    database_registry,
    db_field,
    prefetch,
)

#: Minimum worker count required by the spec for the conservation tests.
WORKERS = 16


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _dispose_registers_engines():
    """Shared engine teardown, per the spec's practical notes."""
    yield
    registers.dispose_all()


def run_concurrently(worker, count, workers=WORKERS):
    """
    Run ``worker(i)`` for ``i in range(count)`` across a thread pool.

    Every thread parks on a gate until all tasks are submitted, so contention is
    created structurally rather than by sleeping. ``worker`` is expected to
    catch its own exceptions and return an outcome.
    """
    gate = threading.Event()

    def task(index):
        gate.wait()
        return worker(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, i) for i in range(count)]
        gate.set()
        return [future.result() for future in futures]


def outcome(fn):
    """Return ``("ok", value)`` or ``("error", exc)`` without letting exc escape."""
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - the test asserts on the collected errors
        return ("error", exc)


def errors_of(outcomes):
    return [value for status, value in outcomes if status == "error"]


@contextmanager
def captured_statements(engine):
    """Collect every SQL statement the engine executes inside the block."""
    statements: list[str] = []

    def listener(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", listener)


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------


def make_stock_model(url, table="products"):
    @database_registry(url, table_name=table, key_field="id")
    class Product(BaseModel):
        id: int | None = db_field(id_strategy="autoincrement", default=None)
        sku: str = "sku"
        stock: int = 0
        price: int = 0
        quantity: int = 0
        total: int = 0

    return Product


def make_versioned_model(url, table="accounts", version_field="version"):
    @database_registry(
        url,
        table_name=table,
        key_field="id",
        version_field=version_field,
    )
    class Account(BaseModel):
        id: int | None = db_field(id_strategy="autoincrement", default=None)
        name: str = "acct"
        amount: int = 0
        version: int = 1

    return Account


def make_shop(tmp_path, name="shop"):
    """A four-table shop with a nested ``items__product`` relationship path."""
    url = db_url(tmp_path, name)
    db = DatabaseRegistry()

    @db.database_registry(url, table_name="customers", key_field="id")
    class Customer(BaseModel):
        id: int | None = db_field(id_strategy="autoincrement", default=None)
        name: str

    @db.database_registry(url, table_name="products", key_field="id")
    class Product(BaseModel):
        id: int | None = db_field(id_strategy="autoincrement", default=None)
        name: str

    @db.database_registry(url, table_name="orders", key_field="id")
    class Order(BaseModel):
        id: int | None = db_field(id_strategy="autoincrement", default=None)
        customer_id: int = db_field(foreign_key="customers.id", index=True)
        reference: str = "ref"

    @db.database_registry(url, table_name="order_items", key_field="id")
    class OrderItem(BaseModel):
        id: int | None = db_field(id_strategy="autoincrement", default=None)
        order_id: int = db_field(foreign_key="orders.id", index=True)
        product_id: int | None = db_field(
            foreign_key="products.id", index=True, default=None
        )
        quantity: int = 1

    Order.items = HasMany(OrderItem, foreign_key="order_id")
    Order.customer = BelongsTo(Customer, local_key="customer_id")
    OrderItem.product = BelongsTo(Product, local_key="product_id")

    return SimpleNamespace(
        db=db,
        url=url,
        Customer=Customer,
        Product=Product,
        Order=Order,
        OrderItem=OrderItem,
    )


# ===========================================================================
# A. Conservation under concurrency
# ===========================================================================


class TestConservationUnderConcurrency:
    def test_01_no_lost_updates_with_f_decrements(self, tmp_path):
        """100 concurrent single-unit F decrements must land on exactly 0."""
        Product = make_stock_model(db_url(tmp_path, "lost_updates"))
        product = Product.objects.create(sku="widget", stock=100)
        pid = product.id

        def worker(_index):
            return outcome(
                lambda: Product.objects.update_where({"id": pid}, stock=F("stock") - 1)
            )

        outcomes = run_concurrently(worker, 100)

        assert errors_of(outcomes) == []
        accepted = sum(1 for status, rows in outcomes if status == "ok" and rows)
        final = Product.objects.require(pid).stock

        assert accepted == 100
        assert final == 0, f"lost {100 - accepted} updates; stock left at {final}"
        assert 100 - final == accepted

    def test_02_no_oversell_under_guarded_contention(self, tmp_path):
        """150 buyers, 100 units: exactly 100 succeed, 50 get [], stock hits 0."""
        Product = make_stock_model(db_url(tmp_path, "oversell"))
        product = Product.objects.create(sku="ticket", stock=100)
        pid = product.id

        def worker(_index):
            return outcome(
                lambda: Product.objects.update_where(
                    {"id": pid, "stock__gte": 1}, stock=F("stock") - 1
                )
            )

        outcomes = run_concurrently(worker, 150)

        assert errors_of(outcomes) == []
        accepted = [rows for status, rows in outcomes if status == "ok" and rows]
        rejected = [rows for status, rows in outcomes if status == "ok" and not rows]
        final = Product.objects.require(pid).stock

        assert all(rows == [] for rows in rejected)
        assert final >= 0, "oversold: stock went negative"
        assert len(accepted) == 100
        assert len(rejected) == 50
        assert final == 0
        assert 100 - final == len(accepted)

    def test_03_conservation_identity_with_variable_quantities(self, tmp_path):
        """initial - final == units accepted, for mixed order sizes."""
        Product = make_stock_model(db_url(tmp_path, "conservation"))
        initial = 500
        product = Product.objects.create(sku="bulk", stock=initial)
        pid = product.id
        quantities = [(i % 7) + 1 for i in range(150)]

        def worker(index):
            qty = quantities[index]
            result = outcome(
                lambda: Product.objects.update_where(
                    {"id": pid, "stock__gte": qty}, stock=F("stock") - qty
                )
            )
            return (result[0], result[1], qty)

        outcomes = run_concurrently(worker, len(quantities))

        assert [value for status, value, _qty in outcomes if status == "error"] == []
        accepted_units = sum(
            qty for status, rows, qty in outcomes if status == "ok" and rows
        )
        final = Product.objects.require(pid).stock

        assert final >= 0
        # The headline assertion: no unit may vanish and none may be conjured.
        assert initial - final == accepted_units
        # Sanity: the run must actually have been contended, not a no-op.
        assert accepted_units > 0
        assert final < initial

    def test_04_concurrent_increments_land_exactly(self, tmp_path):
        Product = make_stock_model(db_url(tmp_path, "increments"))
        counter = Product.objects.create(sku="counter", stock=0)
        pid = counter.id

        def worker(_index):
            return outcome(
                lambda: Product.objects.update_where({"id": pid}, stock=F("stock") + 1)
            )

        outcomes = run_concurrently(worker, 200)

        assert errors_of(outcomes) == []
        accepted = sum(1 for status, rows in outcomes if status == "ok" and rows)
        final = Product.objects.require(pid).stock

        assert accepted == 200
        assert final == 200
        assert final - 0 == accepted

    def test_05_mixed_increment_and_decrement_traffic_nets_out(self, tmp_path):
        Product = make_stock_model(db_url(tmp_path, "mixed"))
        initial = 1000
        product = Product.objects.create(sku="mixed", stock=initial)
        pid = product.id
        deltas = [3 if i % 2 == 0 else -2 for i in range(160)]

        def worker(index):
            delta = deltas[index]
            expression = F("stock") + delta if delta > 0 else F("stock") - abs(delta)
            result = outcome(
                lambda: Product.objects.update_where({"id": pid}, stock=expression)
            )
            return (result[0], result[1], delta)

        outcomes = run_concurrently(worker, len(deltas))

        assert [value for status, value, _d in outcomes if status == "error"] == []
        applied = sum(delta for status, rows, delta in outcomes if status == "ok" and rows)
        final = Product.objects.require(pid).stock

        assert applied == sum(deltas)
        assert final == initial + applied
        assert final - initial == applied

    def test_06_optimistic_lock_conserves_under_contention(self, tmp_path):
        """applied + rejected == attempts, and the stored value == applied."""
        Account = make_versioned_model(db_url(tmp_path, "optimistic"))
        account = Account.objects.create(name="ledger", amount=0)
        aid = account.id
        attempts = 96

        def worker(_index):
            try:
                loaded = Account.objects.require(aid)
                loaded.amount = loaded.amount + 1
                loaded.save()
                return ("applied", None)
            except StaleDataError as exc:
                return ("rejected", exc)
            except Exception as exc:  # noqa: BLE001
                return ("error", exc)

        outcomes = run_concurrently(worker, attempts)

        unexpected = [exc for status, exc in outcomes if status == "error"]
        assert unexpected == []
        applied = sum(1 for status, _ in outcomes if status == "applied")
        rejected = sum(1 for status, _ in outcomes if status == "rejected")

        assert applied + rejected == attempts
        stored = Account.objects.require(aid)
        # Every accepted write must be visible; none may be silently dropped.
        assert stored.amount == applied
        assert stored.version == 1 + applied

    def test_07_bounded_retry_loop_converges(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "retry"))
        account = Account.objects.create(name="ledger", amount=0)
        aid = account.id
        total = 32
        max_attempts = 500

        def worker(_index):
            for _attempt in range(max_attempts):
                loaded = Account.objects.require(aid)
                loaded.amount = loaded.amount + 1
                try:
                    loaded.save()
                except StaleDataError:
                    continue
                return ("applied", None)
            return ("exhausted", None)

        outcomes = run_concurrently(worker, total)

        assert [status for status, _ in outcomes if status == "exhausted"] == []
        applied = sum(1 for status, _ in outcomes if status == "applied")
        stored = Account.objects.require(aid)

        assert applied == total
        assert stored.amount == total
        assert stored.version == 1 + total


@pytest.mark.parametrize("backend_url", ["postgres"], indirect=True)
class TestConservationOnPostgres:
    """
    Same conservation properties against a real MVCC backend.

    SQLite serialises writers, so the SQLite versions above are necessary but
    not sufficient. These skip when Docker is unavailable; they do not replace
    the SQLite tests.
    """

    def test_02_no_oversell_on_postgres(self, backend_url):
        Product = make_stock_model(backend_url, table=backend_table_name("stock"))
        product = Product.objects.create(sku="ticket", stock=100)
        pid = product.id

        def worker(_index):
            return outcome(
                lambda: Product.objects.update_where(
                    {"id": pid, "stock__gte": 1}, stock=F("stock") - 1
                )
            )

        outcomes = run_concurrently(worker, 150)

        assert errors_of(outcomes) == []
        accepted = sum(1 for status, rows in outcomes if status == "ok" and rows)
        final = Product.objects.require(pid).stock

        assert final >= 0
        assert accepted == 100
        assert final == 0
        assert 100 - final == accepted

    def test_06_optimistic_lock_on_postgres(self, backend_url):
        Account = make_versioned_model(backend_url, table=backend_table_name("acct"))
        account = Account.objects.create(name="ledger", amount=0)
        aid = account.id
        attempts = 64

        def worker(_index):
            try:
                loaded = Account.objects.require(aid)
                loaded.amount = loaded.amount + 1
                loaded.save()
                return ("applied", None)
            except StaleDataError as exc:
                return ("rejected", exc)
            except Exception as exc:  # noqa: BLE001
                return ("error", exc)

        outcomes = run_concurrently(worker, attempts)

        assert [exc for status, exc in outcomes if status == "error"] == []
        applied = sum(1 for status, _ in outcomes if status == "applied")
        rejected = sum(1 for status, _ in outcomes if status == "rejected")

        assert applied + rejected == attempts
        stored = Account.objects.require(aid)
        assert stored.amount == applied
        assert stored.version == 1 + applied


# ===========================================================================
# B. Transactions under stress
# ===========================================================================


class TestTransactions:
    def _two_table_registry(self, tmp_path, name="tx"):
        url = db_url(tmp_path, name)
        db = DatabaseRegistry()

        @db.database_registry(url, table_name="users", key_field="id")
        class User(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            email: str

        @db.database_registry(url, table_name="orders", key_field="id")
        class Order(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            user_id: int = db_field(foreign_key="users.id")
            total: int = 0

        return db, User, Order

    def test_08_rollback_leaves_neither_table_behind(self, tmp_path):
        db, User, Order = self._two_table_registry(tmp_path, "rollback")

        with pytest.raises(RuntimeError, match="boom"):
            with db.transaction():
                user = User.objects.create(email="alice@example.com")
                Order.objects.create(user_id=user.id, total=999)
                raise RuntimeError("boom")

        assert User.objects.count() == 0
        assert Order.objects.count() == 0

    def test_09_f_update_in_rolled_back_transaction_is_undone(self, tmp_path):
        url = db_url(tmp_path, "tx_f")
        db = DatabaseRegistry()

        @db.database_registry(url, table_name="products", key_field="id")
        class Product(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            stock: int = 0

        product = Product.objects.create(stock=100)

        with pytest.raises(RuntimeError, match="abort"):
            with db.transaction():
                Product.objects.update_where({"id": product.id}, stock=F("stock") - 10)
                # Item 10: the transaction sees its own uncommitted write.
                assert Product.objects.require(product.id).stock == 90
                raise RuntimeError("abort")

        assert Product.objects.require(product.id).stock == 100

    def test_10_reads_inside_transaction_see_own_writes(self, tmp_path):
        db, User, Order = self._two_table_registry(tmp_path, "own_writes")

        with db.transaction():
            user = User.objects.create(email="bob@example.com")
            assert User.objects.count() == 1
            assert User.objects.get(id=user.id).email == "bob@example.com"
            Order.objects.create(user_id=user.id, total=42)
            assert Order.objects.count() == 1
            assert [o.total for o in Order.objects.all()] == [42]

        assert User.objects.count() == 1
        assert Order.objects.count() == 1

    def test_11_concurrent_transactions_on_separate_rows_all_commit(self, tmp_path):
        Product = make_stock_model(db_url(tmp_path, "tx_concurrent"))
        rows = [Product.objects.create(sku=f"sku-{i}", stock=10) for i in range(WORKERS)]
        ids = [row.id for row in rows]

        def worker(index):
            pid = ids[index]

            def body():
                with Product.objects.transaction():
                    Product.objects.update_where({"id": pid}, stock=F("stock") + 5)
                    Product.objects.update_where({"id": pid}, price=index)
                return True

            return outcome(body)

        outcomes = run_concurrently(worker, len(ids), workers=WORKERS)

        assert errors_of(outcomes) == []
        stored = {row.id: row for row in Product.objects.all(order_by="id")}
        assert len(stored) == len(ids)
        for index, pid in enumerate(ids):
            assert stored[pid].stock == 15, f"row {pid} lost its committed update"
            assert stored[pid].price == index

    def test_12_no_op_guarded_update_does_not_abort_transaction(self, tmp_path):
        url = db_url(tmp_path, "tx_noop")
        db = DatabaseRegistry()

        @db.database_registry(url, table_name="products", key_field="id")
        class Product(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            sku: str = "sku"
            stock: int = 0

        product = Product.objects.create(sku="widget", stock=5)

        with db.transaction():
            rejected = Product.objects.update_where(
                {"id": product.id, "stock__gte": 999}, stock=F("stock") - 999
            )
            assert rejected == []
            Product.objects.update_where({"id": product.id}, stock=F("stock") + 1)
            Product.objects.create(sku="second", stock=7)

        assert Product.objects.require(product.id).stock == 6
        assert Product.objects.count() == 2
        assert Product.objects.require(sku="second").stock == 7


# ===========================================================================
# C. F correctness and rejection
# ===========================================================================


class TestExpressionCorrectness:
    def _row(self, Product, **values):
        return Product.objects.create(**values)

    def test_13_every_operator_produces_the_right_stored_value(self, tmp_path):
        Product = make_stock_model(db_url(tmp_path, "operators"))

        cases = [
            ("add", lambda: F("stock") + 5, 10, 15),
            ("sub", lambda: F("stock") - 3, 10, 7),
            ("mul", lambda: F("stock") * 3, 10, 30),
            ("div", lambda: F("stock") / 2, 10, 5),
            ("radd", lambda: 5 + F("stock"), 10, 15),
            ("rsub", lambda: 100 - F("stock"), 10, 90),
            ("rmul", lambda: 3 * F("stock"), 10, 30),
            ("rdiv", lambda: 100 / F("stock"), 10, 10),
            ("neg", lambda: -F("stock"), 10, -10),
            ("chain", lambda: (F("stock") + 5) * 2, 10, 30),
        ]

        for name, build, start, expected in cases:
            row = self._row(Product, sku=name, stock=start)
            updated = Product.objects.update_where({"id": row.id}, stock=build())
            assert updated, f"{name}: update matched nothing"
            actual = Product.objects.require(row.id).stock
            assert actual == expected, f"{name}: expected {expected}, stored {actual}"

    def test_13b_expression_value_is_never_read_into_python(self, tmp_path):
        """
        A read-modify-write would need a SELECT before the UPDATE; a compiled
        column expression does not. Assert the emitted UPDATE carries the
        arithmetic rather than a bound literal.
        """
        Product = make_stock_model(db_url(tmp_path, "sql_shape"))
        row = self._row(Product, sku="widget", stock=100)

        with captured_statements(Product.objects._engine) as statements:
            Product.objects.update_where({"id": row.id}, stock=F("stock") - 5)

        updates = [s for s in statements if s.lstrip().lower().startswith("update")]
        assert len(updates) == 1, f"expected a single UPDATE, got: {statements}"
        set_clause = updates[0].split("SET", 1)[-1].split("WHERE", 1)[0]
        # The right-hand side must name the column: `stock = stock - 5`, not a
        # literal computed in Python from a prior read.
        assert set_clause.count("stock") >= 2, (
            f"UPDATE did not contain column arithmetic: {updates[0]}"
        )
        assert "-" in set_clause
        assert "95" not in updates[0], (
            f"the new value was computed in Python and bound as a literal: {updates[0]}"
        )
        assert Product.objects.require(row.id).stock == 95

    def test_14_expression_referencing_a_different_column(self, tmp_path):
        Product = make_stock_model(db_url(tmp_path, "cross_column"))
        row = self._row(Product, sku="widget", price=7, quantity=6, total=0)

        Product.objects.update_where({"id": row.id}, total=F("price") * F("quantity"))

        stored = Product.objects.require(row.id)
        assert stored.total == 42
        assert stored.price == 7
        assert stored.quantity == 6

        # Every operator must also work between two column references.
        Product.objects.update_where({"id": row.id}, total=F("price") + F("quantity"))
        assert Product.objects.require(row.id).total == 13
        Product.objects.update_where({"id": row.id}, total=F("price") - F("quantity"))
        assert Product.objects.require(row.id).total == 1
        Product.objects.update_where({"id": row.id}, quantity=F("total") + F("price"))
        assert Product.objects.require(row.id).quantity == 8
        Product.objects.update_where({"id": row.id}, price=12, quantity=4)
        Product.objects.update_where({"id": row.id}, total=F("price") / F("quantity"))
        assert Product.objects.require(row.id).total == 3

    def test_15_guard_boundary_equal_quantity_succeeds(self, tmp_path):
        Product = make_stock_model(db_url(tmp_path, "boundary"))
        row = self._row(Product, sku="widget", stock=5)

        updated = Product.objects.update_where(
            {"id": row.id, "stock__gte": 5}, stock=F("stock") - 5
        )

        assert updated != []
        assert Product.objects.require(row.id).stock == 0

        # And one unit past the boundary must be rejected, not silently applied.
        again = Product.objects.update_where(
            {"id": row.id, "stock__gte": 1}, stock=F("stock") - 1
        )
        assert again == []
        assert Product.objects.require(row.id).stock == 0

    def test_16_invalid_fields_rejected_on_both_sides(self, tmp_path):
        url = db_url(tmp_path, "invalid_fields")
        db = DatabaseRegistry()

        @db.database_registry(
            url,
            table_name="items",
            key_field="id",
            encryption_key="local-test-key",
        )
        class Item(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            stock: int = 0
            secret: str = db_field(encrypted=True, default="classified")
            scratch: str = db_field(exclude_from_db=True, default="transient")

        row = Item.objects.create(stock=10)
        original = Item.objects.require(row.id).stock

        # Unknown field, as the assignment target and inside the expression.
        with pytest.raises(InvalidQueryError):
            Item.objects.update_where({"id": row.id}, nonexistent=F("stock") - 1)
        with pytest.raises(InvalidQueryError):
            Item.objects.update_where({"id": row.id}, stock=F("nonexistent") - 1)

        # exclude_from_db, both sides.
        with pytest.raises(InvalidQueryError):
            Item.objects.update_where({"id": row.id}, scratch=F("stock"))
        with pytest.raises(InvalidQueryError):
            Item.objects.update_where({"id": row.id}, stock=F("scratch"))

        # encrypted, both sides.
        with pytest.raises(InvalidQueryError):
            Item.objects.update_where({"id": row.id}, secret=F("stock"))
        with pytest.raises(InvalidQueryError):
            Item.objects.update_where({"id": row.id}, stock=F("secret"))

        # A rejected expression must not have written anything.
        assert Item.objects.require(row.id).stock == original

    def test_17_f_update_bumps_version_on_versioned_model(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "f_version"))
        account = Account.objects.create(name="ledger", amount=10)
        assert account.version == 1

        Account.objects.update_where({"id": account.id}, amount=F("amount") + 5)

        stored = Account.objects.require(account.id)
        assert stored.amount == 15
        assert stored.version == 2

    def test_18_expression_and_plain_value_in_one_call(self, tmp_path):
        Product = make_stock_model(db_url(tmp_path, "mixed_update"))
        row = Product.objects.create(sku="before", stock=100, price=1)

        updated = Product.objects.update_where(
            {"id": row.id}, stock=F("stock") - 5, sku="after", price=F("price") * 10
        )

        assert updated != []
        stored = Product.objects.require(row.id)
        assert stored.stock == 95
        assert stored.sku == "after"
        assert stored.price == 10


# ===========================================================================
# D. Optimistic locking semantics
# ===========================================================================


class TestOptimisticLocking:
    def test_19a_create_sets_version_to_one(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "v_create"))
        account = Account.objects.create(name="a", amount=1)

        assert account.version == 1
        assert Account.objects.require(account.id).version == 1

    def test_19b_save_increments_version_by_exactly_one(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "v_save"))
        account = Account.objects.create(name="a", amount=1)

        loaded = Account.objects.require(account.id)
        loaded.amount = 2
        loaded.save()

        assert loaded.version == 2, "in-memory version was not incremented"
        stored = Account.objects.require(account.id)
        assert stored.version == 2
        assert stored.amount == 2

    def test_19c_stale_save_raises_and_leaves_row_untouched(self, tmp_path):
        """Covers items 19 and 21."""
        Account = make_versioned_model(db_url(tmp_path, "v_stale"))
        account = Account.objects.create(name="a", amount=1)

        stale = Account.objects.require(account.id)
        winner = Account.objects.require(account.id)
        winner.amount = 100
        winner.save()

        stale.amount = 999
        with pytest.raises(StaleDataError) as excinfo:
            stale.save()

        assert excinfo.value.expected_version == 1
        details = excinfo.value.to_dict()["details"]
        assert details["reason"] == "version_conflict"

        stored = Account.objects.require(account.id)
        assert stored.amount == 100, "rejected save leaked into the database"
        assert stored.version == 2

    def test_19d_rejected_save_rolls_back_in_memory_version(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "v_rollback"))
        account = Account.objects.create(name="a", amount=1)

        stale = Account.objects.require(account.id)
        loaded_version = stale.version

        winner = Account.objects.require(account.id)
        winner.amount = 50
        winner.save()

        stale.amount = 7
        with pytest.raises(StaleDataError):
            stale.save()
        assert stale.version == loaded_version, (
            "in-memory version was not rolled back; a retry loop would compound"
        )

        # A second rejected save must not compound the drift either.
        with pytest.raises(StaleDataError) as second:
            stale.save()
        assert second.value.expected_version == loaded_version
        assert stale.version == loaded_version

        # Re-read and retry now succeeds.
        fresh = Account.objects.require(account.id)
        fresh.amount = 7
        fresh.save()
        assert Account.objects.require(account.id).amount == 7

    def test_19e_update_where_blocks_a_previously_loaded_instance(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "v_bulk"))
        account = Account.objects.create(name="a", amount=1)
        loaded = Account.objects.require(account.id)

        Account.objects.update_where({"id": account.id}, amount=42)
        assert Account.objects.require(account.id).version == 2

        loaded.amount = 7
        with pytest.raises(StaleDataError) as excinfo:
            loaded.save()
        assert excinfo.value.expected_version == 1
        assert Account.objects.require(account.id).amount == 42

    def test_19f_misconfiguration_is_rejected_at_registration(self, tmp_path):
        url = db_url(tmp_path, "v_misconfig")

        with pytest.raises(ModelRegistrationError):

            @database_registry(url, table_name="m1", key_field="id", version_field="nope")
            class MissingField(BaseModel):
                id: int | None = db_field(id_strategy="autoincrement", default=None)
                amount: int = 0

        with pytest.raises(ModelRegistrationError):

            @database_registry(
                url, table_name="m2", key_field="id", version_field="version"
            )
            class NonInteger(BaseModel):
                id: int | None = db_field(id_strategy="autoincrement", default=None)
                version: str = "1"

        with pytest.raises(ModelRegistrationError):

            @database_registry(url, table_name="m3", key_field="id", version_field="id")
            class VersionIsPrimaryKey(BaseModel):
                id: int | None = db_field(id_strategy="autoincrement", default=None)
                amount: int = 0

    def test_20_deleted_row_reports_row_deleted(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "v_deleted"))
        account = Account.objects.create(name="a", amount=1)
        stale = Account.objects.require(account.id)

        assert Account.objects.delete(account.id) is True

        stale.amount = 5
        with pytest.raises(StaleDataError) as excinfo:
            stale.save()

        assert excinfo.value.to_dict()["details"]["reason"] == "row_deleted"
        assert excinfo.value.expected_version == 1
        assert Account.objects.get(id=account.id) is None

    def test_22_two_sequential_saves_on_the_same_instance(self, tmp_path):
        Account = make_versioned_model(db_url(tmp_path, "v_sequential"))
        account = Account.objects.create(name="a", amount=0)
        loaded = Account.objects.require(account.id)

        loaded.amount = 1
        loaded.save()
        assert loaded.version == 2

        loaded.amount = 2
        loaded.save()  # must not raise: nobody else wrote in between
        assert loaded.version == 3

        stored = Account.objects.require(account.id)
        assert stored.amount == 2
        assert stored.version == 3

        # A third save, still without a re-read.
        loaded.amount = 3
        loaded.save()
        assert Account.objects.require(account.id).amount == 3
        assert Account.objects.require(account.id).version == 4


# ===========================================================================
# E. Prefetch
# ===========================================================================


class TestPrefetch:
    def _seed(self, shop, orders=12, items_per_order=3, products=4):
        customer = shop.Customer.objects.create(name="acme")
        product_rows = [
            shop.Product.objects.create(name=f"product-{i}") for i in range(products)
        ]
        order_rows = []
        for order_index in range(orders):
            order = shop.Order.objects.create(
                customer_id=customer.id, reference=f"ref-{order_index}"
            )
            order_rows.append(order)
            payload = [
                {
                    "order_id": order.id,
                    "product_id": product_rows[(order_index + i) % products].id,
                    "quantity": i + 1,
                }
                for i in range(items_per_order)
            ]
            shop.OrderItem.objects.bulk_create(payload)
        return customer, product_rows, order_rows

    def test_23_nested_prefetch_query_count_is_bounded(self, tmp_path):
        shop = make_shop(tmp_path, "prefetch_counts")
        n_orders = 12
        self._seed(shop, orders=n_orders, items_per_order=3)
        engine = shop.Order.objects._engine

        unprefetched_orders = shop.Order.objects.all(order_by="id")
        with captured_statements(engine) as lazy_statements:
            for order in unprefetched_orders:
                for item in list(order.items):
                    _ = item.product
        lazy_count = len(lazy_statements)

        prefetched_orders = shop.Order.objects.all(order_by="id")
        with captured_statements(engine) as eager_statements:
            prefetch(prefetched_orders, "items__product")
            for order in prefetched_orders:
                for item in list(order.items):
                    _ = item.product
        eager_count = len(eager_statements)

        assert lazy_count >= n_orders, (
            f"lazy access issued only {lazy_count} queries for {n_orders} orders; "
            "the comparison would be meaningless"
        )
        assert eager_count <= 6, (
            f"nested prefetch issued {eager_count} queries "
            f"(expected one batch per level): {eager_statements}"
        )
        assert eager_count * 4 <= lazy_count, (
            f"prefetch saved little: {eager_count} vs {lazy_count} statements"
        )

    def test_23b_query_count_does_not_grow_with_parent_count(self, tmp_path):
        shop_small = make_shop(tmp_path, "prefetch_small")
        self._seed(shop_small, orders=4, items_per_order=3)
        small_orders = shop_small.Order.objects.all(order_by="id")
        with captured_statements(shop_small.Order.objects._engine) as small:
            prefetch(small_orders, "items__product")
        small_count = len(small)

        shop_large = make_shop(tmp_path, "prefetch_large")
        self._seed(shop_large, orders=40, items_per_order=3)
        large_orders = shop_large.Order.objects.all(order_by="id")
        with captured_statements(shop_large.Order.objects._engine) as large:
            prefetch(large_orders, "items__product")
        large_count = len(large)

        assert large_count == small_count, (
            f"query count scaled with parent count: {small_count} -> {large_count}"
        )
        assert large_count <= 6

    def test_24_prefetched_values_equal_lazy_values(self, tmp_path):
        shop = make_shop(tmp_path, "prefetch_equal")
        self._seed(shop, orders=6, items_per_order=3)

        lazy_orders = shop.Order.objects.all(order_by="id")
        lazy_view = [
            (
                order.reference,
                order.customer.name,
                sorted(
                    (item.quantity, item.product.name if item.product else None)
                    for item in order.items
                ),
            )
            for order in lazy_orders
        ]

        eager_orders = shop.Order.objects.all(order_by="id")
        prefetch(eager_orders, "customer", "items__product")

        # Two paths in one call: after prefetching, rendering must issue nothing.
        with captured_statements(shop.Order.objects._engine) as statements:
            for order in eager_orders:
                _ = order.customer.name
                for item in order.items:
                    _ = item.product
        assert statements == [], f"prefetched access still queried: {statements}"

        eager_view = [
            (
                order.reference,
                order.customer.name,
                sorted(
                    (item.quantity, item.product.name if item.product else None)
                    for item in order.items
                ),
            )
            for order in eager_orders
        ]

        assert eager_view == lazy_view
        assert len(eager_view) == 6

    def test_25_prefetched_null_belongs_to_does_not_requery(self, tmp_path):
        shop = make_shop(tmp_path, "prefetch_null")
        customer = shop.Customer.objects.create(name="acme")
        product = shop.Product.objects.create(name="widget")
        order = shop.Order.objects.create(customer_id=customer.id)
        shop.OrderItem.objects.bulk_create(
            [
                {"order_id": order.id, "product_id": product.id, "quantity": 1},
                {"order_id": order.id, "product_id": None, "quantity": 2},
                {"order_id": order.id, "product_id": None, "quantity": 3},
            ]
        )

        items = shop.OrderItem.objects.all(order_by="id")
        prefetch(items, "product")
        null_items = [item for item in items if item.product_id is None]
        assert len(null_items) == 2

        engine = shop.OrderItem.objects._engine
        with captured_statements(engine) as statements:
            resolved = [item.product for item in null_items]
            # A second access must not re-query either.
            resolved += [item.product for item in null_items]

        assert resolved == [None, None, None, None]
        assert statements == [], (
            f"prefetched NULL foreign key re-queried the database: {statements}"
        )

    def test_26_unknown_relationship_and_empty_input(self, tmp_path):
        shop = make_shop(tmp_path, "prefetch_errors")
        self._seed(shop, orders=2, items_per_order=1)
        orders = shop.Order.objects.all(order_by="id")

        with pytest.raises(RelationshipError):
            prefetch(orders, "not_a_relationship")

        with pytest.raises(RelationshipError):
            prefetch(orders, "items__not_a_relationship")

        # Empty input is a no-op, not an error, and issues no queries.
        with captured_statements(shop.Order.objects._engine) as statements:
            prefetch([])
            prefetch([], "items")
            prefetch([], "items__product")
        assert statements == []


# ===========================================================================
# F. Ledger and schema
# ===========================================================================


class TestLedgerAndSchema:
    def _table_with_drift(self, tmp_path, name="ledger"):
        """
        Create a table out-of-band that is missing a column the model declares,
        so ``migrate()`` has real work to do without registering two models on
        the same table.
        """
        url = db_url(tmp_path, name)
        engine = create_engine(url, future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE accounts ("
                    "id INTEGER PRIMARY KEY, "
                    "email VARCHAR(255) NOT NULL)"
                )
            )
            conn.execute(
                text("INSERT INTO accounts (id, email) VALUES (1, 'alice@example.com')")
            )
        engine.dispose()

        @database_registry(url, table_name="accounts", key_field="id", auto_create=False)
        class Account(BaseModel):
            id: int
            email: str
            nickname: str | None = None
            score: int = 0

        return url, Account

    def test_27_dry_run_applies_nothing_then_migrate_adds_columns(self, tmp_path):
        url, Account = self._table_with_drift(tmp_path, "migrate")

        dry = Account.objects.migrate(dry_run=True)
        assert dry.ok is False
        assert sorted(dry.missing_columns) == ["nickname", "score"]
        assert sorted(Account.objects.column_names()) == ["email", "id"], (
            "dry_run=True modified the schema"
        )

        result = Account.objects.migrate(dry_run=False)

        columns = set(Account.objects.column_names())
        assert {"nickname", "score"} <= columns
        assert result.ok is True
        assert result.missing_columns == []

        # Existing row data survives the migration.
        stored = Account.objects.require(1)
        assert stored.email == "alice@example.com"
        assert Account.objects.count() == 1

    def test_28_ledger_records_applied_migrations(self, tmp_path):
        fresh_url = db_url(tmp_path, "fresh")

        @database_registry(fresh_url, table_name="widgets", key_field="id")
        class Widget(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            name: str = "w"

        assert Widget.objects.applied_migrations() == []

        url, Account = self._table_with_drift(tmp_path, "ledger_applied")
        assert Account.objects.applied_migrations() == []

        Account.objects.migrate(dry_run=False)
        entries = Account.objects.applied_migrations()

        assert len(entries) >= 1
        joined = " ".join(str(entry.get("name", "")) for entry in entries)
        assert "accounts" in joined
        assert "nickname" in joined and "score" in joined
        for entry in entries:
            assert entry.get("version")
            assert entry.get("applied_at") is not None

    def test_29_duplicate_table_registration_rules(self, tmp_path):
        url = db_url(tmp_path, "dup_table")

        @database_registry(url, table_name="people", key_field="id")
        class PersonA(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            name: str
            email: str

        with pytest.raises(ModelRegistrationError) as excinfo:

            @database_registry(url, table_name="people", key_field="id")
            class PersonMismatch(BaseModel):
                id: int | None = db_field(id_strategy="autoincrement", default=None)
                name: str
                nickname: str

        message = str(excinfo.value)
        assert "people" in message
        assert "nickname" in message or "email" in message, (
            f"error did not name the mismatched column: {message}"
        )

        # An identical schema on the same table is allowed.
        @database_registry(url, table_name="people", key_field="id")
        class PersonB(BaseModel):
            id: int | None = db_field(id_strategy="autoincrement", default=None)
            name: str
            email: str

        PersonA.objects.create(name="alice", email="alice@example.com")
        assert PersonB.objects.count() == 1
        assert PersonB.objects.require(name="alice").email == "alice@example.com"
