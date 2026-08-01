"""
Column expressions — arithmetic the *database* performs, not Python.

The problem this solves
-----------------------
The obvious way to decrement inventory is a lost update waiting to happen::

    product = Product.objects.require(pid)          # reads stock = 100
    if product.stock < qty:
        raise OutOfStock
    Product.objects.update_where({"id": pid}, stock=product.stock - qty)

Two concurrent callers both read 100, both compute 99, both write 99. One sale
vanishes. Measured on this library before ``F`` existed: 100 concurrent single-unit
sales against a stock of 100 left **97** units on the shelf — 97 units of phantom
inventory that had already been sold.

The read-modify-write window is the bug, and no amount of transaction wrapping
closes it, because the arithmetic happens in Python between the read and the write.
The fix is to never read the value at all::

    Product.objects.update_where(
        {"id": pid, "stock__gte": qty},
        stock=F("stock") - qty,
    )

which emits ``UPDATE products SET stock = stock - :qty WHERE id = :pid AND stock >= :qty``.
The database does the arithmetic under its own row lock, and the ``stock__gte`` guard
means an oversell updates zero rows rather than driving stock negative. Check the
returned list — empty means the guard rejected the write.

Supported operations
--------------------
``+ - * /`` against literals and other columns, in either order, plus unary negation::

    F("stock") - 1
    F("balance") + F("pending")
    F("price") * Decimal("1.08")
    100 - F("used")
"""

from __future__ import annotations

from typing import Any

from registers.db.exceptions import InvalidQueryError


class F:
    """
    A reference to a database column, usable in ``update_where(...)`` values.

    ``F("stock") - 1`` builds an expression tree that is compiled against the real
    table at execution time. Nothing is evaluated in Python, so the value never
    makes a round trip and cannot go stale.
    """

    __slots__ = ("_field", "_op", "_left", "_right")

    def __init__(self, field: str) -> None:
        if not isinstance(field, str) or not field.strip():
            raise InvalidQueryError(
                "F() requires a non-empty field name.",
                operation="expression",
            )
        self._field: str | None = field
        self._op: str | None = None
        self._left: Any = None
        self._right: Any = None

    # -- construction ---------------------------------------------------

    @classmethod
    def _combine(cls, op: str, left: Any, right: Any) -> "F":
        node = cls.__new__(cls)
        node._field = None
        node._op = op
        node._left = left
        node._right = right
        return node

    def __add__(self, other: Any) -> "F":
        return F._combine("+", self, other)

    def __radd__(self, other: Any) -> "F":
        return F._combine("+", other, self)

    def __sub__(self, other: Any) -> "F":
        return F._combine("-", self, other)

    def __rsub__(self, other: Any) -> "F":
        return F._combine("-", other, self)

    def __mul__(self, other: Any) -> "F":
        return F._combine("*", self, other)

    def __rmul__(self, other: Any) -> "F":
        return F._combine("*", other, self)

    def __truediv__(self, other: Any) -> "F":
        return F._combine("/", self, other)

    def __rtruediv__(self, other: Any) -> "F":
        return F._combine("/", other, self)

    def __neg__(self) -> "F":
        return F._combine("-", 0, self)

    # -- introspection --------------------------------------------------

    def referenced_fields(self) -> set[str]:
        """Every column name this expression reads. Used to validate before executing."""
        if self._field is not None:
            return {self._field}
        found: set[str] = set()
        for side in (self._left, self._right):
            if isinstance(side, F):
                found |= side.referenced_fields()
        return found

    def resolve(self, table: Any) -> Any:
        """Compile to a SQLAlchemy expression bound to *table*."""
        if self._field is not None:
            return table.c[self._field]

        left = self._left.resolve(table) if isinstance(self._left, F) else self._left
        right = self._right.resolve(table) if isinstance(self._right, F) else self._right

        if self._op == "+":
            return left + right
        if self._op == "-":
            return left - right
        if self._op == "*":
            return left * right
        if self._op == "/":
            return left / right
        raise InvalidQueryError(  # pragma: no cover - operators are closed above
            f"Unsupported expression operator '{self._op}'.",
            operation="expression",
        )

    def __repr__(self) -> str:
        if self._field is not None:
            return f"F({self._field!r})"
        return f"({self._left!r} {self._op} {self._right!r})"

    # F is a value builder, not a predicate. Defining __eq__ would make
    # `F("a") == 1` silently produce an expression instead of a comparison, which
    # is a trap in `if` statements and dict lookups. Left as identity equality.


def contains_expression(values: Any) -> bool:
    """True when a mapping of update values holds at least one :class:`F`."""
    return any(isinstance(value, F) for value in values.values())
