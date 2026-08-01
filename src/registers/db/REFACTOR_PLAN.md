# `registers.db` — Refactor & Hardening Record

Two passes: a structural refactor (phases 0–5), then a production-readiness pass closing
the concurrency correctness gaps that made this unsafe for e-commerce.

**260 tests / 83% coverage → 675 tests / 88% coverage.** Suite green at every step.

---

## Part 2 — Production hardening

### The blocker that started it

The documented Ecommerce Blueprint taught a lost update. Replayed with 16 threads
selling 100 units from a stock of 100:

```
stock should be 0, actually is: 97
```

Three of a hundred decrements survived. Every thread read `stock`, computed in Python,
and wrote back over the others. In a real shop: 97 units sold that don't exist.

Now:

```
100 concurrent sales from stock=100 -> stock=0, sales accepted=100
150 buyers / 100 units             -> accepted=100, stock=0    (never negative)
```

### What was added

**`F()` — arithmetic the database performs** (`expressions.py`)

```python
Product.objects.update_where({"id": pid, "stock__gte": qty}, stock=F("stock") - qty)
```

Emits `SET stock = stock - :qty`. The value never round-trips through Python, so it
cannot go stale. Supports `+ - * /` against literals and other columns in either order,
plus negation. Guards live in the criteria; `update_where` returns the rows it actually
changed, so `[]` is the out-of-stock signal. Unknown, `exclude_from_db`, and `encrypted`
fields are rejected on both the assignment target and every column read.

**Optimistic locking** (`version_field=`)

Every save becomes `UPDATE ... SET version = :next WHERE pk = :pk AND version = :loaded`.
Zero rows matched means someone else wrote first → `StaleDataError`, never a silent
overwrite. `.details["reason"]` separates `version_conflict` from `row_deleted`, and a
rejected save rolls the in-memory version back so a retry loop converges instead of
compounding.

This is what makes the *naive* pattern safe rather than merely discouraged:

```
naive read-modify-write x100: 48 applied, 52 rejected, stock=52
```

48 applied, stock moved by exactly 48. Nothing lost silently. `update_where` bumps the
version too, so a bulk update correctly invalidates instances loaded beforehand.

Raising rather than auto-retrying is deliberate: a retry re-runs whatever else was in the
caller's block, which is fine for a status flip and dangerous if it also charged a card.

**Multi-level prefetch**

```python
prefetch(orders, "customer", "items__product")
```

One batched query per level, regardless of row count.

| 50 orders × 5 items | Queries |
|---|---|
| lazy | 351 |
| prefetched | **4** |

Also fixed: a prefetched `BelongsTo` resolving to `None` was indistinguishable from
"not cached" and re-queried on every access — the exact N+1 prefetch exists to remove.

**Migration ledger, honestly**

`registers_schema_migrations` was created and never written to. It now records every
applied column change, readable via `applied_migrations()`. Scope is stated plainly in
the docs: additive columns only; renames, drops, and backfills stay with Alembic, and
`diff_schema()` keeps reporting them rather than silently applying them.

**Postgres/MySQL CI** — `.github/workflows/tests.yml` runs three jobs: the SQLite suite
across Python 3.10–3.12, a no-pydantic job proving the optional dependency really is
optional, and integration jobs against real Postgres and MySQL. SQLite serialises writers
and ignores `SELECT ... FOR UPDATE`, so it structurally cannot exercise row-level locking.

### Bugs found along the way

- **Silent table collision.** Two models bound to the same table on the same URL silently
  reused the first model's `Table` — the second model's fields never got columns. Now
  rejected when the schemas differ, still allowed when identical (rebinding after a
  rename is legitimate).
- **Leaky hydration errors.** A row that no longer matched the model raised
  `pydantic.ValidationError` straight out of `require()`. With pydantic now optional and
  dataclass models raising `FieldCoercionError`, callers had no single exception to
  handle. Both are wrapped in `SchemaError` with the original chained. **Behavior change**
  — code catching `ValidationError` from a read must catch `SchemaError` instead.

### Verification

An independent subagent wrote 37 adversarial tests from a written spec without reading
the implementation. All passed — and because a clean first pass on an adversarial suite
is itself suspicious, it validated the harness by running the *naive* pattern through the
identical 16-thread setup and reproducing the historical 97. The contention is real.

The assertion style throughout is **conservation**, not absence-of-exception:
`initial - final == accepted_count`. That is what catches silent loss.

---

## Part 1 — Structural refactor (phases 0–5)

| | Before | After |
|---|---|---|
| Largest module | `registry.py`, 2,557 lines | `queries.py`, 674 |
| Manager | one class, 130 methods | 5 mixins by concern |
| `filter()` / `count()` | 0.284 / 0.237 ms | 0.180 / 0.154 ms |
| Memory, 20k rows | 1,377 B/row | 624 B/row (dataclasses) |
| Pydantic | required | optional extra |

**Phase 0 — bugs.** The query-logging install guard keyed on `id(self)`, so it never
fired and listeners stacked on shared engines forever. `_assert_known_fields` built a
fresh `TypeAdapter` per field *per query* (~60x). `dispose_all()` didn't mark contexts
disposed. Encryption rebuilt a Fernet per value. Plus mojibake and dead code.

**Phase 1 — `FieldSpec`.** Each field resolved once at registration into column type +
cached `validate`/`to_db`/`from_db`. A test subagent caught a real bug: `resolve_codec`
probed scalars before `Enum`, so `IntEnum`/`StrEnum` lost membership validation entirely —
an `IntEnum` column would have accepted `999`.

**Phase 2 — one validated config.** The 18-parameter signature was written three times
and only 8 options were validated; the rest were mutable attributes checked afterwards.

**Phase 3 — `manager/` package.** Pure code movement into base/crud/queries/schema_ops/
policies/coordinator. `operators.py` now derives `VALID_OPERATORS` from its dispatch table.

**Phases 4–5 — adapter boundary.** `ModelAdapter` with pydantic and dataclass
implementations; `dc_field()` mirrors `db_field()` via `dataclasses.field(metadata=...)`.
Dataclass models get real write validation from the Phase 1 codecs. Two more subagent
finds: postponed annotations made `Field.type` a *string* (silently falling through to
JSON), and nested models crashed on write. On that second one my spec had the direction
backwards — the agent's report blamed the pydantic adapter, but checking which way it
actually broke showed `model_dump()` recursion was *required*.

---

## Honest remaining limits

- **No JOINs.** `select()` is single-table; cross-table reporting needs `raw()`.
  Prefetch solves N+1 for traversal, not for aggregate queries.
- **Async is thread offload.** `asyncio.to_thread` over sync SQLAlchemy, now documented
  as such. A high-concurrency checkout endpoint is capped by the thread pool.
- **No pessimistic locking.** `SELECT ... FOR UPDATE` has no public API. `F()` plus
  guards covers the common cases; genuinely serialisable workflows need `raw()`.
- **`slots=True` dataclasses rejected** (~224 B/instance available). Supporting them
  means moving identity-stamping and prefetch caches into `id()`-keyed side maps.
- **Alias methods retained** (`strict_create`, `get_all`, `schema_diff`, `reset_registry`)
  — documented, so deprecation is a v8 concern.

## Where this now stands

Suitable for e-commerce-shaped workloads: inventory, balances, and order state have
correct primitives, the failure modes are loud, and the concurrency behaviour is tested
by conservation rather than by absence of exceptions. Validate against Postgres under
your own load before trusting it with real money — CI covers correctness, not your
throughput.
