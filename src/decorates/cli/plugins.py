"""
Dynamically discovers and loads plugin modules from a Python package.

When a plugin module is imported, any ``@registry.register(...)`` calls
at module level execute automatically — no manual wiring in ``main.py``
is needed.

Usage::

    from decorates.cli import load_plugins
    from app.commands import user_commands  # treated as a package

    load_plugins("app.commands", registry)

Convention:
    Any ``.py`` file inside the given package is treated as a plugin.
    Files starting with ``_`` (including ``__init__``) are skipped.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType

from decorates.cli.exceptions import PluginLoadError
from decorates.cli.registry import CommandRegistry

logger = logging.getLogger(__name__)


def load_plugins(package_path: str, registry: CommandRegistry) -> list[ModuleType]:
    """
    Import every non-private module inside *package_path* and return
    the list of successfully loaded modules.

    Args:
        package_path: Dotted module path to the plugins package
                      (e.g. ``"app.commands"``).
        registry:     The registry that plugin modules will register
                      commands into. Passed here for clarity; plugins
                      must reference the same registry instance.

    Returns:
        List of successfully imported module objects.

    Raises:
        PluginLoadError: If the package itself cannot be imported.
    """
    try:
        package = importlib.import_module(package_path)
    except ImportError as exc:
        raise PluginLoadError(package_path, str(exc)) from exc

    package_dir = getattr(package, "__path__", None)
    if package_dir is None:
        raise PluginLoadError(package_path, "Not a package (no __path__).")

    loaded: list[ModuleType] = []

    for finder, module_name, _ in pkgutil.iter_modules(package_dir):
        if module_name.startswith("_"):
            continue

        full_name = f"{package_path}.{module_name}"
        try:
            module = importlib.import_module(full_name)
            loaded.append(module)
            logger.debug("Loaded plugin: %s", full_name)
        except Exception as exc:
            # Log but continue — one bad plugin shouldn't abort the app
            logger.warning("Skipping plugin '%s': %s", full_name, exc, exc_info=True)

    return loaded

