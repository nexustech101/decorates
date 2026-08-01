from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, Iterator

from sqlalchemy import event

from registers.db.engine import _engines
from registers.db.registry import DatabaseRegistry


class TestRegistry(DatabaseRegistry):
    """DatabaseRegistry variant with a default database URL for tests."""

    __test__ = False

    def __init__(self, database_url: str | Path = "sqlite:///:memory:") -> None:
        super().__init__()
        self.database_url = database_url

    def database_registry(self, database_url: str | Path | None = None, **kwargs: Any):
        return super().database_registry(database_url or self.database_url, **kwargs)


@dataclass
class ModelFactory:
    model_cls: type
    defaults: dict[str, Any] = field(default_factory=dict)

    def build(self, **overrides: Any) -> Any:
        values = self._values(0, overrides)
        return self.model_cls(**values)

    def create(self, **overrides: Any) -> Any:
        values = self._values(0, overrides)
        return self.model_cls.objects.create(**values)

    def create_batch(self, count: int, **overrides: Any) -> list[Any]:
        return [
            self.model_cls.objects.create(**self._values(index, overrides))
            for index in range(count)
        ]

    def _values(self, index: int, overrides: dict[str, Any]) -> dict[str, Any]:
        values = dict(self.defaults)
        for key, value in overrides.items():
            values[key] = value(index) if callable(value) else value
        return values


def factory(model_cls: type, defaults: dict[str, Any] | None = None) -> ModelFactory:
    return ModelFactory(model_cls, defaults or {})


@contextmanager
def assert_query_count(*, max: int) -> Generator[None]:
    """Assert that SQLAlchemy engines execute at most ``max`` statements."""

    count = {"value": 0}

    def before_cursor_execute(*_args: Any, **_kwargs: Any) -> None:
        count["value"] += 1

    engines = list(_engines.values())
    for engine in engines:
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
        if count["value"] > max:
            raise AssertionError(f"Expected at most {max} queries, executed {count['value']}.")
