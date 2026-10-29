"""Bridge between the scheduler / event system and the action registry.

APScheduler persists jobs as ``(run_action, args)`` calls, so this module keeps
the single picklable entry point plus the bot reference. It also expands
variable placeholders, records run history and hands failures to the
scheduler's retry/alerting handler.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import discord

from . import config
from .placeholders import PlaceholderContext, expand, resolve_context
from .actions import ACTION_REGISTRY

log = logging.getLogger(__name__)

_bot: discord.Client | None = None
_runlog = None
_failure_handler: Callable[..., None] | None = None


def _stamp() -> str:
    return datetime.now(ZoneInfo(config.timezone_name())).isoformat(timespec="seconds")


def _record(task_id: str, action: str, started_at: str, status: str, error: str | None) -> None:
    if _runlog is None:
        return
    try:
        _runlog.record(task_id, action, started_at, _stamp(), status, error)
    except Exception:
        log.exception("Failed to record a run of task %r", task_id)


async def run_action(
    task_id: str,
    action_name: str,
    params: Mapping[str, Any],
    meta: Mapping[str, Any] | None = None,
    ctx: PlaceholderContext | None = None,
) -> Any:
    """Run a registered action; called by APScheduler's executor or event dispatch."""
    action = ACTION_REGISTRY.get(action_name)
    if action is None:
        raise ValueError(f"Unknown scheduled action: {action_name!r}")
    if _bot is None:
        raise RuntimeError("The bot has not been attached yet; is setup_hook complete?")
    context = resolve_context(_bot, params, ctx)
    templated = expand(params, context)
    log.info("Running scheduled task %r (%s) with %d parameter(s)", task_id, action_name, len(templated))
    started_at = _stamp()
    try:
        result = await action(_bot, templated)
    except Exception as exc:
        only_type = f"{type(exc).__name__}: {exc}"[:500]
        _record(task_id, action_name, started_at, "error", only_type)
        if _failure_handler is not None:
            try:
                _failure_handler(task_id, action_name, params, dict(meta or {}), exc)
            except Exception:
                log.exception("The failure handler crashed for task %r", task_id)
        raise
    _record(task_id, action_name, started_at, "success", None)
    return result


def set_bot(bot: discord.Client) -> None:
    global _bot
    _bot = bot


def get_bot() -> discord.Client | None:
    return _bot


def set_runlog(runlog) -> None:
    global _runlog
    _runlog = runlog


def set_failure_handler(handler) -> None:
    global _failure_handler
    _failure_handler = handler


__all__ = ["run_action", "set_bot", "get_bot", "set_runlog", "set_failure_handler"]
