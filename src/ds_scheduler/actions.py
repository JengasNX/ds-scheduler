"""Discord actions the scheduler can invoke.

Every action is an async callable ``(bot, params)`` registered under a stable
name. The scheduler stores only the action name plus JSON parameters, so this
module must be importable whenever jobs are executed.
"""
from __future__ import annotations

import io
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import discord

from . import config

log = logging.getLogger(__name__)

Action = Callable[[discord.Client, Mapping[str, Any]], Awaitable[Any]]
ACTION_REGISTRY: dict[str, Action] = {}

DEFAULT_EMBED_COLOUR = "#BDA8E6"


def register(name: str, *, requires: tuple[str, ...] = ()) -> Callable[[Action], Action]:
    """Register an action so scheduled jobs can reference it by name."""

    def decorator(func: Action) -> Action:
        if name in ACTION_REGISTRY:
            raise ValueError(f"Duplicate action name: {name}")
        setattr(func, "requires", requires)
        ACTION_REGISTRY[name] = func
        return func

    return decorator


def required_params(action: str) -> tuple[str, ...]:
    """Parameter names the action cannot run without."""
    func = ACTION_REGISTRY.get(action)
    return getattr(func, "requires", ()) if func else ()


# --- helpers -----------------------------------------------------------------


def _int(params: Mapping[str, Any], key: str) -> int:
    value = params.get(key)
    if value is None:
        raise ValueError(f"Missing required parameter: {key}")
    return int(value)


async def _resolve_guild(bot: discord.Client, guild_id: int) -> discord.Guild:
    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except discord.NotFound:
            guild = None
    if guild is None:
        raise ValueError(f"Guild {guild_id} is not visible to the bot.")
    return guild


async def _text_channel(bot: discord.Client, channel_id: int) -> discord.abc.Messageable:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            channel = None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise ValueError(f"Channel {channel_id} cannot receive messages ({type(channel).__name__}).")
    return channel


async def _fetch_message(channel, message_id: int) -> discord.Message:
    try:
        return await channel.fetch_message(message_id)
    except discord.NotFound:
        raise ValueError(f"Message {message_id} was not found in channel {channel.id}.") from None


def _make_embed(spec: Mapping[str, Any]) -> discord.Embed:
    """Полный embed: title/url, author, image, thumbnail, fields."""
    embed = discord.Embed(
        title=str(spec.get("title", "")),
        description=str(spec.get("description", "")),
        url=str(spec.get("title_url")) if spec.get("title_url") else None,
        colour=discord.Colour.from_str(str(spec.get("colour", DEFAULT_EMBED_COLOUR))),
    )
    author = spec.get("author")
    if author:
        if isinstance(author, str):
            embed.set_author(name=author)
        else:
            embed.set_author(
                name=str(author.get("name", "")),
                url=str(author["url"]) if author.get("url") else None,
                icon_url=str(author["icon_url"]) if author.get("icon_url") else None,
            )
    image = spec.get("image")
    if image:
        embed.set_image(url=str(image if isinstance(image, str) else image.get("url", "")))
    thumbnail = spec.get("thumbnail")
    if thumbnail:
        embed.set_thumbnail(url=str(thumbnail if isinstance(thumbnail, str) else thumbnail.get("url", "")))
    footer = spec.get("footer")
    if footer:
        if isinstance(footer, str):
            embed.set_footer(text=footer)
        else:
            embed.set_footer(
                text=str(footer.get("text", "")),
                icon_url=str(footer["icon_url"]) if footer.get("icon_url") else None,
            )
    for field in spec.get("fields") or []:
        if isinstance(field, dict) and field.get("name") is not None:
            embed.add_field(
                name=str(field["name"]),
                value=str(field.get("value", "")),
                inline=bool(field.get("inline", False)),
            )
    return embed


def _allowed_mentions(params: Mapping[str, Any]) -> discord.AllowedMentions:
    """`allow_mentions` in params: 'all', or a comma list of everyone/users/roles."""
    mode = params.get("allow_mentions")
    if not mode or str(mode).strip().lower() in {"none", "no"}:
        return discord.AllowedMentions.none()
    mode = str(mode).strip().lower()
    if mode == "all":
        return discord.AllowedMentions.all()
    tokens = {chunk.strip().lower() for chunk in mode.split(",") if chunk.strip()}
    if not tokens:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone="everyone" in tokens,
        users="users" in tokens,
        roles="roles" in tokens,
    )


def _reason(params: Mapping[str, Any]) -> str:
    return str(params.get("reason") or "ds-scheduler scheduled action")


def parse_local_datetime(value: Any, label: str = "time") -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' in the configured timezone (used by event actions)."""
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(f"{label} must use the 'YYYY-MM-DD HH:MM' format: {exc}") from exc
    return parsed.replace(tzinfo=ZoneInfo(config.timezone_name()))


# --- message actions ---------------------------------------------------------


@register("send_message", requires=("channel_id", "content"))
async def send_message(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Post a text message; optional ``embed`` dict attaches an embed."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    embed = _make_embed(params["embed"]) if params.get("embed") else None
    message = await channel.send(
        content=str(params.get("content", "")),
        embed=embed,
        allowed_mentions=_allowed_mentions(params),
    )
    log.info("send_message -> channel %s message %s", channel.id, message.id)
    return {"message_id": message.id}


@register("send_embed", requires=("channel_id", "title", "description"))
async def send_embed(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Post a rich embed (title/description/colour/footer)."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    embed = _make_embed(params)
    message = await channel.send(embed=embed, allowed_mentions=_allowed_mentions(params))
    log.info("send_embed -> channel %s message %s", channel.id, message.id)
    return {"message_id": message.id}


@register("edit_message", requires=("channel_id", "message_id", "content"))
async def edit_message(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Replace the text of an earlier bot message."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    message = await _fetch_message(channel, _int(params, "message_id"))
    embed = _make_embed(params["embed"]) if params.get("embed") else None
    await message.edit(content=str(params.get("content", "")), embed=embed)
    log.info("edit_message -> message %s", message.id)
    return {"message_id": message.id}


@register("delete_message", requires=("channel_id", "message_id"))
async def delete_message(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Delete a message the bot can manage."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    message = await _fetch_message(channel, _int(params, "message_id"))
    await message.delete(reason=_reason(params))
    log.info("delete_message -> message %s", message.id)
    return {"deleted": message.id}


@register("add_reaction", requires=("channel_id", "message_id", "emoji"))
async def add_reaction(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """React to a message with a unicode emoji or custom emoji string."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    message = await _fetch_message(channel, _int(params, "message_id"))
    await message.add_reaction(str(params["emoji"]))
    log.info("add_reaction %s -> message %s", params["emoji"], message.id)
    return {"reacted": True}


# --- channel actions ---------------------------------------------------------


@register("create_channel", requires=("guild_id", "name"))
async def create_channel(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Create a text or voice channel; optional ``category_id``/``topic``."""
    guild = await _resolve_guild(bot, _int(params, "guild_id"))
    channel_type = str(params.get("channel_type", "text"))
    if channel_type not in {"text", "voice"}:
        raise ValueError(f"Unsupported channel_type: {channel_type!r}")
    category_id = params.get("category_id")
    category = guild.get_channel(int(category_id)) if category_id is not None else None
    if category_id is not None and not isinstance(category, discord.CategoryChannel):
        raise ValueError(f"Category {category_id} is missing or not a category.")
    if channel_type == "voice":
        channel = await guild.create_voice_channel(
            str(params["name"]), category=category, reason=_reason(params)
        )
    else:
        channel = await guild.create_text_channel(
            str(params["name"]),
            category=category,
            topic=str(params["topic"]) if params.get("topic") else None,
            reason=_reason(params),
        )
    log.info("create_channel %s -> %s", params["name"], channel.id)
    return {"channel_id": channel.id}


@register("delete_channel", requires=("channel_id",))
async def delete_channel(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Delete a channel by ID."""
    channel_id = _int(params, "channel_id")
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    await channel.delete(reason=_reason(params))
    log.info("delete_channel -> %s", channel_id)
    return {"deleted": channel_id}


@register("rename_channel", requires=("channel_id", "new_name"))
async def rename_channel(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Rename an existing channel."""
    channel_id = _int(params, "channel_id")
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    await channel.edit(name=str(params["new_name"]), reason=_reason(params))
    log.info("rename_channel %s -> %r", channel_id, params["new_name"])
    return {"renamed": channel_id}


@register("set_channel_topic", requires=("channel_id", "topic"))
async def set_channel_topic(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Set the topic of a text channel."""
    channel_id = _int(params, "channel_id")
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError(f"Channel {channel_id} is not a text channel.")
    await channel.edit(topic=str(params["topic"]), reason=_reason(params))
    log.info("set_channel_topic -> %s", channel_id)
    return {"topic_set": channel_id}


# --- role actions ------------------------------------------------------------


async def _member(bot: discord.Client, params: Mapping[str, Any]) -> tuple[discord.Guild, discord.Member]:
    guild = await _resolve_guild(bot, _int(params, "guild_id"))
    user_id = _int(params, "user_id")
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            member = None
    if member is None:
        raise ValueError(f"User {user_id} is not a member of guild {guild.id}.")
    return guild, member


@register("add_role", requires=("guild_id", "user_id", "role_id"))
async def add_role(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Grant a role to a member."""
    guild, member = await _member(bot, params)
    role = guild.get_role(_int(params, "role_id"))
    if role is None:
        raise ValueError(f"Role {params['role_id']} was not found in guild {guild.id}.")
    await member.add_roles(role, reason=_reason(params))
    log.info("add_role %s -> user %s", role.id, member.id)
    return {"added_role": role.id}


@register("remove_role", requires=("guild_id", "user_id", "role_id"))
async def remove_role(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Revoke a role from a member."""
    guild, member = await _member(bot, params)
    role = guild.get_role(_int(params, "role_id"))
    if role is None:
        raise ValueError(f"Role {params['role_id']} was not found in guild {guild.id}.")
    await member.remove_roles(role, reason=_reason(params))
    log.info("remove_role %s -> user %s", role.id, member.id)
    return {"removed_role": role.id}


# --- member actions ----------------------------------------------------------


@register("dm_user", requires=("user_id", "content"))
async def dm_user(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Send a direct message; fails cleanly if the user has DMs closed."""
    user_id = _int(params, "user_id")
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    message = await user.send(
        content=str(params.get("content", "")),
        allowed_mentions=_allowed_mentions(params),
    )
    log.info("dm_user -> user %s message %s", user_id, message.id)
    return {"message_id": message.id}


@register("ban_user", requires=("guild_id", "user_id"))
async def ban_user(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Ban a user; optional ``delete_message_seconds`` trims their messages."""
    guild = await _resolve_guild(bot, _int(params, "guild_id"))
    await guild.ban(
        discord.Object(id=_int(params, "user_id")),
        reason=_reason(params),
        delete_message_seconds=int(params.get("delete_message_seconds", 0)),
    )
    log.info("ban_user -> user %s in guild %s", params["user_id"], guild.id)
    return {"banned": True}


@register("unban_user", requires=("guild_id", "user_id"))
async def unban_user(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Lift a ban."""
    guild = await _resolve_guild(bot, _int(params, "guild_id"))
    await guild.unban(discord.Object(id=_int(params, "user_id")), reason=_reason(params))
    log.info("unban_user -> user %s in guild %s", params["user_id"], guild.id)
    return {"unbanned": True}


@register("timeout_user", requires=("guild_id", "user_id", "duration_seconds"))
async def timeout_user(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Put a member in timeout for ``duration_seconds``."""
    _, member = await _member(bot, params)
    duration = int(params["duration_seconds"])
    if duration <= 0 or duration > 28 * 24 * 3600:
        raise ValueError("duration_seconds must be between 1 and 2419200 (28 days).")
    until = discord.utils.utcnow() + timedelta(seconds=duration)
    await member.timeout(until, reason=_reason(params))
    log.info("timeout_user -> user %s for %ss", member.id, duration)
    return {"timed_out_until": until.astimezone(timezone.utc).isoformat()}


# --- extended message actions ------------------------------------------------


@register("pin_message", requires=("channel_id", "message_id"))
async def pin_message(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Pin a message by its ID."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    message = await _fetch_message(channel, _int(params, "message_id"))
    await message.pin(reason=_reason(params))
    log.info("pin_message -> message %s", message.id)
    return {"pinned": message.id}


@register("unpin_message", requires=("channel_id", "message_id"))
async def unpin_message(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Unpin a message by its ID."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    message = await _fetch_message(channel, _int(params, "message_id"))
    await message.unpin(reason=_reason(params))
    log.info("unpin_message -> message %s", message.id)
    return {"unpinned": message.id}


@register("purge_messages", requires=("channel_id", "limit"))
async def purge_messages(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Delete the last ``limit`` messages; deletes each individually so age does not matter."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    limit = int(params["limit"])
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500.")
    deleted = await channel.purge(limit=limit, bulk=False)
    log.info("purge_messages -> %d messages in channel %s", len(deleted), channel.id)
    return {"deleted": len(deleted)}


@register("send_file", requires=("channel_id", "url"))
async def send_file(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Download ``url`` and post it as an attachment; optional ``content`` caption."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    url = str(params["url"])
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.read()
    filename = str(params.get("filename") or url.rsplit("/", 1)[-1] or "file.bin")[:100]
    file = discord.File(io.BytesIO(data), filename=filename)
    message = await channel.send(
        content=str(params.get("content") or ""),
        file=file,
        allowed_mentions=_allowed_mentions(params),
    )
    log.info("send_file %s -> channel %s (%d bytes)", filename, channel.id, len(data))
    return {"message_id": message.id}


@register("create_poll", requires=("channel_id", "question", "options"))
async def create_poll(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Create a Discord poll; ``options`` is a list of 2-10 answers."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    options = [str(option) for option in params["options"]]
    if len(options) < 2 or len(options) > 10:
        raise ValueError("options must contain 2 to 10 answers.")
    duration_hours = int(params.get("duration_hours", 24))
    if duration_hours < 1 or duration_hours > 168:
        raise ValueError("duration_hours must be between 1 and 168 (7 days).")
    poll = discord.Poll(question=str(params["question"]), duration=timedelta(hours=duration_hours))
    for option in options:
        poll.add_answer(text=option)
    message = await channel.send(poll=poll, allowed_mentions=_allowed_mentions(params))
    log.info("create_poll -> channel %s poll %s", channel.id, message.id)
    return {"message_id": message.id}


@register("create_thread", requires=("channel_id", "name"))
async def create_thread(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Start a thread; provide ``message_id`` to attach it to a message."""
    channel = await _text_channel(bot, _int(params, "channel_id"))
    if not isinstance(channel, discord.TextChannel):
        raise ValueError(f"Channel {channel.id} is not a text channel.")
    message_id = params.get("message_id")
    reason = _reason(params)
    if message_id is not None:
        message = await _fetch_message(channel, int(message_id))
        thread = await message.create_thread(name=str(params["name"]), auto_archive_duration=1440, reason=reason)
    else:
        thread = await channel.create_thread(
            name=str(params["name"]), auto_archive_duration=1440, reason=reason
        )
    log.info("create_thread %r -> %s", params["name"], thread.id)
    return {"thread_id": thread.id}


@register("set_slowmode", requires=("channel_id", "delay_seconds"))
async def set_slowmode(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Set or clear (0) slowmode for a text channel."""
    channel_id = _int(params, "channel_id")
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError(f"Channel {channel_id} is not a text channel.")
    delay = int(params["delay_seconds"])
    if delay < 0 or delay > 21600:
        raise ValueError("delay_seconds must be between 0 and 21600 (6 hours).")
    await channel.edit(slowmode_delay=delay, reason=_reason(params))
    log.info("set_slowmode %ss -> channel %s", delay, channel_id)
    return {"slowmode": delay}


# --- extended channel / role actions -----------------------------------------


def _perm_overwrite(params: Mapping[str, Any]) -> discord.PermissionOverwrite:
    overwrite = discord.PermissionOverwrite()
    allow = [name.strip() for name in str(params.get("allow", "")).split(",") if name.strip()]
    deny = [name.strip() for name in str(params.get("deny", "")).split(",") if name.strip()]
    if not allow and not deny:
        raise ValueError("Provide 'allow' and/or 'deny' as comma-separated permission names.")
    for name in allow + deny:
        if not hasattr(overwrite, name):
            raise ValueError(f"Unknown Discord permission: {name}")
    for name in allow:
        setattr(overwrite, name, True)
    for name in deny:
        setattr(overwrite, name, False)
    return overwrite


@register("edit_channel_permissions", requires=("channel_id", "role_id"))
async def edit_channel_permissions(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Switch comma-separated permissions on/off for a role; ``allow``/``deny`` params."""
    channel_id = _int(params, "channel_id")
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel)):
        raise ValueError(f"Channel {channel_id} cannot be permission-edited.")
    guild_id = channel.guild.id
    role = channel.guild.get_role(_int(params, "role_id"))
    if role is None:
        raise ValueError(f"Role {params['role_id']} was not found in guild {guild_id}.")
    overwrite = _perm_overwrite(params)
    await channel.set_permissions(role, overwrite=overwrite, reason=_reason(params))
    log.info("edit_channel_permissions -> channel %s role %s", channel_id, role.id)
    return {"updated": channel_id}


@register("create_scheduled_event", requires=("guild_id", "name", "start_time"))
async def create_scheduled_event(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Create a guild event; ``location`` is free text (offline/external) or a channel id."""
    guild = await _resolve_guild(bot, _int(params, "guild_id"))
    start_time = parse_local_datetime(params["start_time"], "start_time")
    if start_time <= discord.utils.utcnow():
        raise ValueError("start_time must be in the future.")
    end_time = parse_local_datetime(params["end_time"], "end_time") if params.get("end_time") else start_time + timedelta(hours=1)
    if end_time <= start_time:
        raise ValueError("end_time must be after start_time.")
    location = params.get("location")
    if isinstance(location, str):
        if location.lstrip("cC").isdigit():
            location = guild.get_channel(int(location))
    event = await guild.create_scheduled_event(
        name=str(params["name"]),
        start_time=start_time,
        end_time=end_time,
        description=str(params.get("description") or ""),
        location=location or None,
        reason=_reason(params),
    )
    log.info("create_scheduled_event %r -> %s", params["name"], event.id)
    return {"event_id": event.id}


@register("create_role", requires=("guild_id", "name"))
async def create_role(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Create a role; optional ``colour`` and ``hoist``/``mentionable`` booleans."""
    guild = await _resolve_guild(bot, _int(params, "guild_id"))
    role = await guild.create_role(
        name=str(params["name"]),
        colour=discord.Colour.from_str(str(params.get("colour", DEFAULT_EMBED_COLOUR))),
        hoist=bool(params.get("hoist", False)),
        mentionable=bool(params.get("mentionable", False)),
        reason=_reason(params),
    )
    log.info("create_role %r -> %s", params["name"], role.id)
    return {"role_id": role.id}


@register("delete_role", requires=("guild_id", "role_id"))
async def delete_role(bot: discord.Client, params: Mapping[str, Any]) -> dict[str, Any]:
    """Delete a role by ID."""
    guild = await _resolve_guild(bot, _int(params, "guild_id"))
    role = guild.get_role(_int(params, "role_id"))
    if role is None:
        raise ValueError(f"Role {params['role_id']} was not found in guild {guild.id}.")
    await role.delete(reason=_reason(params))
    log.info("delete_role -> %s", role.id)
    return {"deleted": role.id}
