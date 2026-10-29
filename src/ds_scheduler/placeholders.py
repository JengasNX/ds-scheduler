"""Переменные-подстановки в параметрах действий.

Синтаксис: ``{объект.поле}``; у токенов даты можно указать формат явно:
``{date:%d.%m.%Y}``. Нераспознанные токены остаются как есть, поэтому
литеральные фигурные скобки безопасны.

Объекты:
- ``{user.*}``  — участник события (определён только в запусках по событиям)
- ``{server.*}` ``{channel.*}`` — сервер/канал из параметров действия
  или из контекста события
- ``{date}`` ``{time}`` ``{datetime}`` ``{weekday}`` — токены текущего времени
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import discord

from . import config

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"\{([a-z]+(?:\.[a-z_]+)?)(?::([^{}]+))?\}")
_RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

_DEFAULT_DT = "%d.%m.%Y %H:%M"

USER_KEYS = {"id", "name", "mention", "nickname", "joined_at", "created_at", "avatar_url", "top_role"}


@dataclass
class PlaceholderContext:
    """Objects available to placeholders; missing ones render as empty text."""
    guild: Any | None = None
    channel: Any | None = None
    member: Any | None = None


def _now() -> datetime:
    return datetime.now(ZoneInfo(config.timezone_name()))


def _dt(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime(_DEFAULT_DT)
    return str(value)


def _user_fields(member: Any) -> dict[str, str]:
    if member is None:
        return {}
    user = getattr(member, "user", None) or member
    avatar = getattr(user, "display_avatar", None)
    top_role = getattr(member, "top_role", None)
    return {
        "id": str(getattr(user, "id", "")),
        "name": str(getattr(user, "name", "")),
        "mention": getattr(member, "mention", None) or getattr(user, "mention", ""),
        "nickname": str(getattr(member, "display_name", "") or getattr(user, "name", "")),
        "joined_at": _dt(getattr(member, "joined_at", None)),
        "created_at": _dt(getattr(user, "created_at", None)),
        "avatar_url": str(avatar) if avatar is not None else "",
        "top_role": str(getattr(top_role, "name", "")),
    }


def _server_fields(guild: Any) -> dict[str, str]:
    if guild is None:
        return {}
    owner = getattr(guild, "owner", None)
    icon = getattr(guild, "icon", None)
    return {
        "name": str(getattr(guild, "name", "")),
        "id": str(getattr(guild, "id", "")),
        "member_count": str(getattr(guild, "member_count", "")),
        "description": str(getattr(guild, "description", "") or ""),
        "created_at": _dt(getattr(guild, "created_at", None)),
        "icon_url": str(icon) if icon is not None else "",
        "owner_mention": getattr(owner, "mention", ""),
        "owner_name": str(getattr(owner, "display_name", "")),
    }


def _channel_fields(channel: Any) -> dict[str, str]:
    if channel is None:
        return {}
    return {
        "name": str(getattr(channel, "name", "")),
        "id": str(getattr(channel, "id", "")),
        "mention": getattr(channel, "mention", ""),
        "topic": str(getattr(channel, "topic", "") or ""),
    }


def _token_values(ctx: PlaceholderContext) -> dict[str, str]:
    now = _now()
    values: dict[str, str] = {}
    values.update({f"user.{key}": value for key, value in _user_fields(ctx.member).items()})
    values.update({f"server.{key}": value for key, value in _server_fields(ctx.guild).items()})
    values.update({f"channel.{key}": value for key, value in _channel_fields(ctx.channel).items()})
    values.update({
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%d.%m.%Y %H:%M"),
        "weekday": _RU_WEEKDAYS[now.weekday()],
        "year": str(now.year),
        "month": f"{now.month:02d}",
        "day": f"{now.day:02d}",
    })
    return values


def expand(value: Any, ctx: PlaceholderContext | None = None) -> Any:
    """Recursively replace placeholders; keeps unresolved tokens literal."""
    tokens = _token_values(ctx or PlaceholderContext())

    def _datetime_token(match: re.Match) -> str:
        token, fmt = match.group(1), match.group(2)
        if token.startswith("date.") or (token == "date" and fmt):
            pattern = fmt if fmt else token.split(".", 1)[1]
            try:
                return _now().strftime(pattern)
            except (ValueError, TypeError):
                return match.group(0)
        return tokens.get(token, match.group(0))

    if isinstance(value, str):
        return _TOKEN.sub(_datetime_token, value)
    if isinstance(value, dict):
        return {key: expand(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, ctx) for item in value]
    return value


def resolve_context(bot: discord.Client, params: Mapping[str, Any], ctx: PlaceholderContext | None = None) -> PlaceholderContext:
    """Fill missing context objects from action params (guild_id/channel_id)."""
    resolved = ctx or PlaceholderContext()
    if resolved.guild is None and params.get("guild_id") and bot is not None:
        resolved.guild = bot.get_guild(int(params["guild_id"]))
    if resolved.channel is None and params.get("channel_id") and bot is not None:
        resolved.channel = bot.get_channel(int(params["channel_id"]))
    if resolved.guild is None and resolved.channel is not None:
        resolved.guild = getattr(resolved.channel, "guild", None)
    return resolved


def unresolved_tokens(action: str, params: Mapping[str, Any]) -> list[str]:
    """User/dynamic tokens that need an event context (sanity check for /schedule add)."""
    result: list[str] = []

    def enter(value: Any) -> None:
        if isinstance(value, str):
            for match in _TOKEN.finditer(value):
                name = match.group(1)
                key = name.split(".", 1)[1] if "." in name else ""
                if name.startswith("user.") and key in USER_KEYS:
                    result.append(match.group(0))

    walk = params
    if isinstance(walk, dict):
        for key, item in walk.items():
            enter(item)
            if isinstance(item, dict):
                for sub in item.values():
                    enter(sub)
    return sorted(set(result))
