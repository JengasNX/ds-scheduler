"""Discord-листенеры и команды /события: действия по событиям сервера."""
from __future__ import annotations

import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from .. import actions as actions_module
from ..placeholders import PlaceholderContext
from ..runner import run_action
from ..cogs.schedule_commands import действие_autocomplete as action_autocomplete, action_display

log = logging.getLogger(__name__)

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.а-яёА-ЯЁ:-]{1,64}$")

EVENT_CHOICES = [
    app_commands.Choice(name="вход_участника — зашёл на сервер", value="member_join"),
    app_commands.Choice(name="выход_участника — покинул сервер", value="member_remove"),
    app_commands.Choice(name="бан — участника забанили", value="member_ban"),
    app_commands.Choice(name="разбан — бан снят", value="member_unban"),
    app_commands.Choice(name="изменение_участника — ник/роли/статус", value="member_update"),
    app_commands.Choice(name="вход_в_голосовой — подключился к каналу", value="voice_join"),
    app_commands.Choice(name="выход_из_голосового — покинул канал", value="voice_leave"),
    app_commands.Choice(name="смена_голосового — перешёл в другой канал", value="voice_switch"),
]

UPDATE_TYPES = ("any", "nickname", "roles", "status")


async def task_id_autocomplete_callback(interaction: discord.Interaction, current: str):
    rules = interaction.client.event_rules.list_all()
    needle = current.lower()
    names = sorted({rule["task_id"] for rule in rules if needle in rule["task_id"].lower()})
    return [app_commands.Choice(name=name, value=name) for name in names[:25]]


class EventCog(commands.Cog):
    """Слушатели событий Discord, запускающие назначенные действия."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # -- диспетчер -----------------------------------------------------------

    async def _dispatch(self, event: str, ctx: PlaceholderContext) -> None:
        rules = self.bot.event_rules.for_event(event)
        for task_id, action, params_raw in rules:
            try:
                params = json.loads(params_raw)
            except json.JSONDecodeError:
                log.error("Правило %s/%s: кривой JSON параметров; пропускаю", event, task_id)
                continue
            try:
                await run_action(task_id, action, params, ctx=ctx)
            except Exception:
                # Подробности ошибки уже записал Runner; продолжаем остальным правилам.
                pass

    # -- участники -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        log.info("событие member_join %s@%s", member, member.guild.name)
        await self._dispatch("member_join", PlaceholderContext(guild=member.guild, member=member))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._dispatch("member_remove", PlaceholderContext(guild=member.guild, member=member))

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user) -> None:
        await self._dispatch("member_ban", PlaceholderContext(guild=guild, member=user))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user) -> None:
        await self._dispatch("member_unban", PlaceholderContext(guild=guild, member=user))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        ctx = PlaceholderContext(guild=after.guild, member=after)
        for task_id, action, params_raw in self.bot.event_rules.for_event("member_update"):
            try:
                params = json.loads(params_raw)
            except json.JSONDecodeError:
                continue
            if not self._update_matches(params, before, after):
                continue
            try:
                await run_action(task_id, action, params, ctx=ctx)
            except Exception:
                pass

    @staticmethod
    def _update_matches(params: dict, before: discord.Member, after: discord.Member) -> bool:
        changed = []
        if before.display_name != after.display_name:
            changed.append("nickname")
        if before.roles != after.roles:
            changed.append("roles")
        if before.status != after.status:
            changed.append("status")
        wanted = str(params.get("member_update_type", "any")).lower()
        if wanted == "any":
            return True
        return wanted in changed

    # -- голос ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        channel = after.channel or before.channel
        ctx = PlaceholderContext(guild=member.guild, member=member, channel=channel)
        if before.channel is None and after.channel is not None:
            await self._dispatch("voice_join", ctx)
        elif before.channel is not None and after.channel is None:
            await self._dispatch("voice_leave", ctx)
        elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
            await self._dispatch("voice_switch", ctx)

    # -- команды -------------------------------------------------------------

    event_group = app_commands.Group(
        name="события",
        description="Правила: действия по событиям сервера.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @event_group.command(name="создать", description="Создать/заменить правило: событие → действие.")
    @app_commands.choices(событие=EVENT_CHOICES)
    @app_commands.autocomplete(действие=action_autocomplete)
    @app_commands.describe(
        событие="Какое событие запускает правило.",
        имя="Уникальное имя правила (по нему удалить / история).",
        действие="Какое действие выполнить (см. /расписание действия).",
        параметры="JSON с параметрами; доступны {user.*}, {channel.*}, {server.*} и {date}.",
        фильтр_изменений="Фильтр только для события «изменение_участника»: any / nickname / roles / status.",
    )
    async def создать(
        self,
        interaction: discord.Interaction,
        событие: app_commands.Choice[str],
        имя: str,
        действие: str,
        параметры: str,
        фильтр_изменений: str | None = None,
    ) -> None:
        event_type = событие.value
        task_id = имя.strip()
        action = действие
        params = параметры
        if not TASK_ID_PATTERN.fullmatch(task_id):
            await interaction.response.send_message(
                "имя: от 1 до 64 символов из букв, цифр и `_ . : -`.", ephemeral=True
            )
            return
        if фильтр_изменений and фильтр_изменений.strip().lower() not in UPDATE_TYPES:
            await interaction.response.send_message(
                "фильтр_изменений: одно из any, nickname, roles, status.", ephemeral=True
            )
            return
        try:
            parsed = json.loads(params or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("нужен JSON-объект")
        except (ValueError, json.JSONDecodeError) as exc:
            await interaction.response.send_message(f"Некорректный JSON в параметрах: {exc}", ephemeral=True)
            return
        if action not in actions_module.ACTION_REGISTRY:
            await interaction.response.send_message(
                f"Неизвестное действие {action!r}. Смотрите /расписание действия.", ephemeral=True
            )
            return
        missing = [key for key in actions_module.required_params(action) if key not in parsed]
        if missing:
            await interaction.response.send_message(
                f"Отсутствуют обязательные параметры для {action}: {', '.join(missing)}", ephemeral=True
            )
            return
        if фильтр_изменений:
            parsed["member_update_type"] = фильтр_изменений.strip().lower()
        self.bot.event_rules.add(event_type, task_id, action, json.dumps(parsed, ensure_ascii=False))
        await interaction.response.send_message(
            f"Правило **{task_id}** на событие `{event_type}` → `{action_display(action)}` сохранено.",
            ephemeral=True,
        )

    @event_group.command(name="удалить", description="Удалить правила задачи.")
    @app_commands.choices(событие=EVENT_CHOICES)
    @app_commands.autocomplete(имя=task_id_autocomplete_callback)
    @app_commands.describe(имя="Имя правила.", событие="Удалять только правила этого события.")
    async def удалить(
        self,
        interaction: discord.Interaction,
        имя: str,
        событие: app_commands.Choice[str] | None = None,
    ) -> None:
        task_id = имя.strip()
        deleted = self.bot.event_rules.remove(task_id, событие.value if событие else None)
        if not deleted:
            await interaction.response.send_message(f"Правила для **{task_id}** не найдены.", ephemeral=True)
            return
        await interaction.response.send_message(f"Удалено правил: {deleted}.", ephemeral=True)

    @event_group.command(name="список", description="Список всех правил событий.")
    async def list_rules(self, interaction: discord.Interaction) -> None:
        rules = self.bot.event_rules.list_all()
        if not rules:
            await interaction.response.send_message("Правил пока нет.", ephemeral=True)
            return
        lines = [f"**{rule['task_id']}** — `{rule['event']}` → `{action_display(rule['action'])}`" for rule in rules]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @event_group.command(name="типы", description="Список поддерживаемых событий.")
    async def list_types(self, interaction: discord.Interaction) -> None:
        lines = [f"**`{choice.value}`** — {choice.name.split('—')[0].strip()}" for choice in EVENT_CHOICES]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(EventCog(bot))
