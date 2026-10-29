"""Reference module skeleton: shows how to plug custom actions into the registry.

Copy this folder, rename it, and write your own async actions — they become
available in /schedule add and /events add right after a restart.
"""
from __future__ import annotations

import discord

from ...actions import register


@register("sonar_message", requires=("channel_id",))
async def sonar_message(bot: discord.Client, params: dict) -> dict:
    """Tiny example action: echoes a fixed string using placeholders."""
    channel = bot.get_channel(int(params["channel_id"]))
    message = await channel.send(str(params.get("text", "ping из модуля example")))
    return {"message_id": message.id}


async def setup(bot) -> None:
    """Optional hook; the decorator above already registered the action."""
    return None
