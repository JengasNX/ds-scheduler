"""Discord bot hosting the persistent scheduler."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from . import config
from .events import EventRules
from .scheduler import SchedulerService
from . import runner

log = logging.getLogger(__name__)


class SchedulerBot(commands.Bot):
    """Bot with a /schedule command group backed by a persistent job store."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # member_join/remove/update events (portal toggle required)
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )
        self.scheduler = SchedulerService(config.db_path(), config.timezone_name())
        self.event_rules = EventRules(config.db_path())
        self.started_at = discord.utils.utcnow()

    async def setup_hook(self) -> None:
        runner.set_bot(self)
        await self.load_extension("ds_scheduler.cogs.schedule_commands")
        await self.load_extension("ds_scheduler.cogs.event_commands")
        await self.load_extension("ds_scheduler.cogs.quick_message")
        await self.load_extension("ds_scheduler.cogs.builder_commands")
        from .module_loader import load_modules

        self.loaded_modules = await load_modules(self)
        if self.loaded_modules:
            log.info("Loaded modules: %s", ", ".join(self.loaded_modules))
        await self.scheduler.start()

        dev_guild_id = config.dev_guild_id()
        if dev_guild_id:
            guild = discord.Object(id=dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to dev guild %s", dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally")

    async def close(self) -> None:
        await self.scheduler.shutdown()
        self.event_rules.close()
        await super().close()


def run(token: str) -> None:
    bot = SchedulerBot()
    bot.run(token, log_handler=None)
