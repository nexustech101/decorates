"""
A simple pre/post execution hook system for the dispatcher.

Middleware hooks are plain callables registered on a
:class:`MiddlewareChain`. The dispatcher runs all pre-hooks before
handler execution and all post-hooks after (receiving the return value).

Pre-hook signature::

    def my_hook(command: str, kwargs: dict) -> None: ...

Post-hook signature::

    def my_hook(command: str, result: Any) -> None: ...

Built-in hooks
--------------
* :func:`logging_middleware_pre` — logs command start and records start time.
* :func:`logging_middleware_post` — logs command completion and elapsed time.

Usage::

    chain = MiddlewareChain()
    chain.add_pre(logging_middleware)
    chain.add_post(logging_middleware)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

PreHook = Callable[[str, dict[str, Any]], None]
PostHook = Callable[[str, Any], None]


class MiddlewareChain:
    """Holds ordered lists of pre- and post-execution hooks."""

    def __init__(self) -> None:
        self._pre: list[PreHook] = []
        self._post: list[PostHook] = []

    def add_pre(self, hook: PreHook) -> None:
        """Append a pre-execution hook."""
        self._pre.append(hook)

    def add_post(self, hook: PostHook) -> None:
        """Append a post-execution hook."""
        self._post.append(hook)

    def run_pre(self, command: str, kwargs: dict[str, Any]) -> None:
        for hook in self._pre:
            hook(command, kwargs)

    def run_post(self, command: str, result: Any) -> None:
        for hook in self._post:
            hook(command, result)


# ---------------------------------------------------------------------------
# Built-in middleware
# ---------------------------------------------------------------------------

def _make_timing_state() -> dict[str, float]:
    return {}

_timing: dict[str, float] = {}


def logging_middleware_pre(command: str, kwargs: dict[str, Any]) -> None:
    """Log command start and record the start time for timing."""
    _timing[command] = time.perf_counter()
    logger.debug("Running command '%s' with arg keys: %s", command, sorted(kwargs.keys()))


def logging_middleware_post(command: str, result: Any) -> None:
    """Log command completion and elapsed wall-clock time."""
    elapsed = time.perf_counter() - _timing.pop(command, time.perf_counter())
    logger.debug("✓ Command '%s' completed in %.4fs", command, elapsed)

