"""Простые команды /сообщение: запланировать сообщение без JSON.

Здесь параметры собираются автоматически: пользователь выбирает канал
списком и пишет текст — бот сам формирует JSON для действия send_message.
"""
from __future__ import annotations

import re

import discord
from discord import app_commands

from .. import actions as actions_module
from .schedule_commands import TASK_ID_PATTERN, action_display, format_next

TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")

DAY_CHOICES = [
    app_commands.Choice(name="Понедельник", value="1"),
    app_commands.Choice(name="Вторник", value="2"),
    app_commands.Choice(name="Среда", value="3"),
    app_commands.Choice(name="Четверг", value="4"),
    app_commands.Choice(name="Пятница", value="5"),
    app_commands.Choice(name="Суббота", value="6"),
    app_commands.Choice(name="Воскресенье", value="0"),
]


def _parse_hour_minute(время: str) -> tuple[int, int] | None:
    match = TIME_PATTERN.fullmatch(время.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_when(когда: str) -> str | None:
    """Разовый запуск: ГГГГ-ММ-ДД ЧЧ:ММ (или ГГГГ-ММ-ДД ЧЧ:ММ).
    Валидацию даты целиком делает планировщик; тут только грубая проверка."""
    raw = когда.strip().replace("T", " ")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}", raw):
        return None
    return raw


class MessageGroup(app_commands.Group):
    """Простые команды: сообщения по расписанию без JSON."""

    def __init__(self, bot) -> None:
        super().__init__(
            name="сообщение",
            description="Запланировать сообщение простой командой (без JSON).",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )
        self.bot = bot

    # -- общение с планировщиком -------------------------------------------

    async def _schedule(
        self,
        interaction: discord.Interaction,
        имя: str,
        канал: discord.TextChannel,
        текст: str,
        *,
        cron: str | None = None,
        at: str | None = None,
        подпись: str,
    ) -> None:
        task_id = имя.strip()
        if not TASK_ID_PATTERN.fullmatch(task_id):
            await interaction.response.send_message(
                "имя: от 1 до 64 символов из букв, цифр и `_ . : -`.", ephemeral=True
            )
            return
        параметры = {"channel_id": канал.id, "content": текст}
        missing = [key for key in actions_module.required_params("send_message") if key not in параметры]
        if missing:
            await interaction.response.send_message(
                f"Не хватает данных: {', '.join(missing)}.", ephemeral=True
            )
            return
        try:
            job = await self.bot.scheduler.add_job(
                task_id=task_id,
                action="send_message",
                params=параметры,
                cron=cron,
                at=at,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"Не получилось: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{подпись}: задача **{task_id}** создана → {канал.mention}\n"
            f"Следующий запуск: {format_next(job)}",
            ephemeral=True,
        )

    # -- команды -------------------------------------------------------------

    @app_commands.command(name="раз", description="Одно сообщение в указанное время.")
    @app_commands.describe(
        имя="Имя задачи (для истории и удаления).",
        канал="Куда отправить.",
        текст="Текст сообщения; можно {date}, {server.name}, {user.mention} и т.д.",
        когда='Когда отправить, формат "ГГГГ-ММ-ДД ЧЧ:ММ", например 2026-12-01 18:00.',
    )
    async def раз(
        self,
        interaction: discord.Interaction,
        имя: str,
        канал: discord.TextChannel,
        текст: str,
        когда: str,
    ) -> None:
        at = _parse_when(когда)
        if at is None:
            await interaction.response.send_message(
                'когда: укажите время в формате "ГГГГ-ММ-ДД ЧЧ:ММ", например 2026-12-01 18:00.',
                ephemeral=True,
            )
            return
        await self._schedule(
            interaction,
            имя,
            канал,
            текст,
            at=at,
            подпись="Разовое сообщение",
        )

    @app_commands.command(name="каждый_день", description="Сообщение каждый день в одно время.")
    @app_commands.describe(
        имя="Имя задачи (для истории и удаления).",
        канал="Куда отправить.",
        текст="Текст сообщения; можно {date}, {weekday}, {server.name} и т.д.",
        время='Время в формате "ЧЧ:ММ", например 12:00.',
    )
    async def каждый_день(
        self,
        interaction: discord.Interaction,
        имя: str,
        канал: discord.TextChannel,
        текст: str,
        время: str,
    ) -> None:
        hm = _parse_hour_minute(время)
        if hm is None:
            await interaction.response.send_message('время: укажите "ЧЧ:ММ", например 12:00.', ephemeral=True)
            return
        hour, minute = hm
        await self._schedule(
            interaction,
            имя,
            канал,
            текст,
            cron=f"{minute} {hour} * * *",
            подпись=f"Ежедневно в {hour:02d}:{minute:02d}",
        )

    @app_commands.command(name="каждую_неделю", description="Сообщение каждую неделю в выбранный день.")
    @app_commands.choices(день=DAY_CHOICES)
    @app_commands.describe(
        имя="Имя задачи (для истории и удаления).",
        канал="Куда отправить.",
        текст="Текст сообщения; можно {date}, {weekday}, {server.name} и т.д.",
        день="День недели.",
        время='Время в формате "ЧЧ:ММ", например 18:00.',
    )
    async def каждую_неделю(
        self,
        interaction: discord.Interaction,
        имя: str,
        канал: discord.TextChannel,
        текст: str,
        день: app_commands.Choice[str],
        время: str,
    ) -> None:
        hm = _parse_hour_minute(время)
        if hm is None:
            await interaction.response.send_message('время: укажите "ЧЧ:ММ", например 18:00.', ephemeral=True)
            return
        hour, minute = hm
        await self._schedule(
            interaction,
            имя,
            канал,
            текст,
            cron=f"{minute} {hour} * * {день.value}",
            подпись=f"Еженедельно ({день.name}) в {hour:02d}:{minute:02d}",
        )

    @app_commands.command(name="каждые_минуты", description="Сообщение каждые N минут (повторяемое).")
    @app_commands.describe(
        имя="Имя задачи (для истории и удаления).",
        канал="Куда отправить.",
        текст="Текст сообщения; можно {time}, {date} и т.д.",
        минут="Период повтора в минутах (мин. 5).",
    )
    async def каждые_минуты(
        self,
        interaction: discord.Interaction,
        имя: str,
        канал: discord.TextChannel,
        текст: str,
        минут: app_commands.Range[int, 5, 527040],
    ) -> None:
        await self._schedule(
            interaction,
            имя,
            канал,
            текст,
            cron=f"*/{минут} * * * *",
            подпись=f"Каждые {минут} мин.",
        )


async def setup(bot) -> None:
    try:
        bot.tree.add_command(MessageGroup(bot))
    except app_commands.CommandAlreadyRegistered:
        pass  # расширение перезагружено
