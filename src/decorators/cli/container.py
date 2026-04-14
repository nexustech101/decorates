"""
A minimal dependency-injection container that maps types to instances.

The dispatcher uses this container to inject service dependencies into
command handlers automatically — no manual wiring in each command needed.

Usage::

    container = DIContainer()
    container.register(UserService, UserService(store=CsvUserStore()))

    # Later, in any handler:
    def create_user(username: str, svc: UserService) -> None:
        svc.create(username)
    # `svc` is injected automatically at dispatch time.
"""

from __future__ import annotations

from typing import Any, Type, TypeVar

from decorators.cli.exceptions import DependencyNotFoundError

T = TypeVar("T")


class DIContainer:
    """
    Lightweight type-keyed dependency injection container.

    Stores one instance per registered type. Instances are registered
    eagerly (at startup) and resolved lazily (at dispatch time).
    """

    def __init__(self) -> None:
        self._registry: dict[type, Any] = {}

    def register(self, dep_type: Type[T], instance: T) -> None:
        """
        Bind *instance* to *dep_type*.

        Args:
            dep_type: The type (class) used as the lookup key.
            instance: The concrete instance to inject.
        """
        self._registry[dep_type] = instance

    def resolve(self, dep_type: Type[T]) -> T:
        """
        Return the instance bound to *dep_type*.

        Raises:
            DependencyNotFoundError: If no instance has been registered
                                     for *dep_type*.
        """
        if dep_type not in self._registry:
            raise DependencyNotFoundError(dep_type)
        return self._registry[dep_type]  # type: ignore[return-value]

    def has(self, dep_type: type) -> bool:
        """Return True if *dep_type* is registered."""
        return dep_type in self._registry

    def __repr__(self) -> str:
        types = ", ".join(t.__name__ for t in self._registry)
        return f"DIContainer([{types}])"
