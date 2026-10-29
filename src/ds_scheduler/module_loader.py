"""Optional extension modules loaded from ds_scheduler.modules.*

Each module is a Python package with an async ``setup(bot)`` coroutine.
Drop a folder there and it loads automatically on next start — this is the
scaffolding for future feature packs (ranking, audio, fun, ...).

Reference skeleton: ``ds_scheduler/modules/example``.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

import discord

MODULES_PACKAGE = "ds_scheduler.modules"


def discover_modules() -> list[str]:
    package = importlib.import_module(MODULES_PACKAGE)
    return [info.name for info in pkgutil.iter_modules(package.__path__)]


async def load_modules(bot: discord.Client) -> list[str]:
    """Import every module package and run its optional async setup(bot)."""
    loaded: list[str] = []
    for name in discover_modules():
        try:
            module = importlib.import_module(f"{MODULES_PACKAGE}.{name}")
        except Exception:
            logging.getLogger(__name__).exception("Failed to import module %r", name)
            continue
        setup = getattr(module, "setup", None)
        if setup is None:
            continue
        try:
            result = setup(bot)
            if hasattr(result, "__await__"):
                await result
            loaded.append(name)
        except Exception:
            logging.getLogger(__name__).exception("Failed to run setup() of module %r", name)
    return loaded
