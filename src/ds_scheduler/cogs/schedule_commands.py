"""Slash-команды управления запланированными задачами (/расписание ...).

Имена команд и опций — русские; технические строки (значения действий,
ключи JSON-параметров) остаются латиницей, чтобы конфиги были стабильны.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import discord
from apscheduler.jobstores.base import JobLookupError
from discord import app_commands

from .. import actions as actions_module
from ..scheduler import JobInfo
from ..runner import run_action

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.а-яёА-ЯЁ:-]{1,64}$")

ACTIONS_RU = {
    "send_message": "отправить_сообщение",
    "send_embed": "отправить_эмбед",
    "edit_message": "изменить_сообщение",
    "delete_message": "удалить_сообщение",
    "add_reaction": "добавить_реакцию",
    "pin_message": "закрепить_сообщение",
    "unpin_message": "открепить_сообщение",
    "purge_messages": "очистить_сообщения",
    "send_file": "отправить_файл",
    "create_poll": "создать_опрос",
    "create_thread": "создать_ветку",
    "set_slowmode": "медленный_режим",
    "edit_channel_permissions": "права_канала",
    "create_channel": "создать_канал",
    "delete_channel": "удалить_канал",
    "rename_channel": "переименовать_канал",
    "set_channel_topic": "описание_канала",
    "create_role": "создать_роль",
    "delete_role": "удалить_роль",
    "add_role": "выдать_роль",
    "remove_role": "снять_роль",
    "dm_user": "сообщение_в_лс",
    "ban_user": "забанить",
    "unban_user": "разбанить",
    "timeout_user": "таймаут",
    "create_scheduled_event": "создать_событие",
}


def action_display(key: str) -> str:
    return ACTIONS_RU.get(key, key)


async def действие_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    needle = current.lower()
    pairs = [(key, action_display(key)) for key in sorted(actions_module.ACTION_REGISTRY)]
    matches = [(key, display) for key, display in pairs if needle in key.lower() or needle in display]
    return [app_commands.Choice(name=display, value=key) for key, display in matches[:25]]


async def имя_autocomplete_задач(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    scheduler = getattr(interaction.client, "scheduler", None)
    if scheduler is None:
        return []
    needle = current.lower()
    names = [job.task_id for job in scheduler.list_jobs() if needle in job.task_id.lower()]
    return [app_commands.Choice(name=name, value=name) for name in names[:25]]


class TaskSelectView(discord.ui.View):
    """Меню /расписание список: выбор задачи и кнопки действий."""

    def __init__(self, scheduler) -> None:
        super().__init__(timeout=180)
        self.scheduler = scheduler
        self.selected: str | None = None
        jobs = sorted(scheduler.list_jobs(), key=lambda item: item.task_id)[:25]
        options = [
            discord.SelectOption(label=job.task_id, value=job.task_id, description=action_display(job.action)[:100])
            for job in jobs
        ]
        if options:
            self.children[0].options = options  # type: ignore[attr-defined]
        else:
            select = self.children[0]
            assert isinstance(select, discord.ui.Select)
            select.placeholder = "Задач пока нет"

    @discord.ui.select(placeholder="Выберите задачу…")
    async def select_task(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.selected = str(select.values[0])
        await self._refresh(interaction)

    async def _require_selection(self, interaction: discord.Interaction) -> str | None:
        if not self.selected:
            await interaction.response.send_message("Сначала выберите задачу из списка.", ephemeral=True)
            return None
        return self.selected

    async def _refresh(self, interaction: discord.Interaction) -> None:
        if self.selected is None:
            await interaction.response.defer()
            return
        await interaction.response.send_message(
            content=None, embed=self._task_embed(self.selected), view=self, ephemeral=True
        )

    def _task_embed(self, task_id: str) -> discord.Embed:
        scheduler = self.scheduler
        job: JobInfo | None = scheduler.get_job(task_id)
        if job is None:
            return discord.Embed(title=f"Задача {task_id}", description="Не найдена (завершилась или удалена).")
        next_run = "на паузе / завершена"
        if job.next_run is not None:
            epoch = int(job.next_run.timestamp())
            next_run = f"<t:{epoch}:f> (<t:{epoch}:R>)"
        lines = [
            f"Действие: `{action_display(job.action)}`",
            f"Триггер: `{job.trigger}`",
            f"Следующий запуск: {next_run}",
        ]
        if job.meta:
            lines.append(f"Параметры задачи: `{json.dumps(job.meta, ensure_ascii=False)}`")
        embed = discord.Embed(title=f"Задача {task_id}", description="\n".join(lines))
        successes, failures = scheduler.runlog.stats(task_id)
        embed.add_field(name="Запуски (успех / ошибка)", value=f"✅ {successes}  |  ❌ {failures}", inline=True)
        for run in scheduler.runlog.recent(task_id, limit=3):
            status = "✅" if run["status"] == "success" else "❌"
            embed.add_field(
                name=f"{status} {str(run['started_at']).split('.')[0]}",
                value=f"`{str(run['error'])[:100] or 'успешно'}`",
                inline=False,
            )
        return embed

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.success)
    async def run_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        task_id = await self._require_selection(interaction)
        if task_id is None:
            return
        try:
            action_name, _ = await self.scheduler.run_now(task_id)
        except (JobLookupError, ValueError, RuntimeError) as exc:
            await interaction.response.send_message(f"Ошибка запуска: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"Задача **{task_id}** выполнена.", ephemeral=True)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        task_id = await self._require_selection(interaction)
        if task_id is not None:
            self.scheduler.pause_job(task_id)
            await self._refresh(interaction)

    @discord.ui.button(emoji="↩️", style=discord.ButtonStyle.secondary)
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        task_id = await self._require_selection(interaction)
        if task_id is not None:
            self.scheduler.resume_job(task_id)
            await self._refresh(interaction)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        task_id = await self._require_selection(interaction)
        if task_id is not None:
            self.scheduler.remove_job(task_id)
            self.selected = None
            await interaction.response.send_message(f"Задача **{task_id}** удалена.", ephemeral=True)


class ScheduleGroup(app_commands.Group):
    """Группа slash-команд, закреплённая на дереве команд."""

    def __init__(self, bot) -> None:
        super().__init__(
            name="расписание",
            description="Управление запланированными задачами.",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )
        self.bot = bot

    # -- helpers ------------------------------------------------------------

    def _scheduler(self):
        return self.bot.scheduler

    @staticmethod
    def _parse_params(raw: str) -> dict[str, Any]:
        params = json.loads(raw or "{}")
        if not isinstance(params, dict):
            raise ValueError('это должен быть JSON-объект, например {"channel_id": 123, "content": "привет"}.')
        return params

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.send_message(message, ephemeral=True)

    @staticmethod
    def _validate_task_id(task_id: str) -> str | None:
        task_id = task_id.strip()
        return task_id if TASK_ID_PATTERN.fullmatch(task_id) else None

    # -- справка ------------------------------------------------------------

    @app_commands.command(name="действия", description="Показать все действия и их параметры.")
    async def list_actions(self, interaction: discord.Interaction) -> None:
        lines = []
        for name in sorted(actions_module.ACTION_REGISTRY):
            required = ", ".join(actions_module.required_params(name))
            lines.append(f"**{action_display(name)}** (`{name}`) — обязательно: {required}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="рецепты", description="Готовые рецепты типовых задач.")
    async def presets(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Готовые рецепты",
            description=(
                "Скопируйте их в /расписание создать (CID замените на ID своего канала).\n\n"
                "**Ежедневный дайджест** — каждый день в 12:00\n"
                '`/расписание создать имя:дайджест действие:send_message параметры:{"channel_id": CID, "content": "Дайджест за {date} ({weekday})"} крон:"0 12 * * *"`\n\n'
                "**Разовое напоминание**\n"
                '`/расписание создать имя:напоминалка действие:send_message параметры:{"channel_id": CID, "content": "Начинаем через час!"} когда:"2026-12-01 18:00"`\n\n'
                "**Недельная уборка** — последние 100 сообщений каждый понедельник в 05:00\n"
                '`/расписание создать имя:уборка действие:purge_messages параметры:{"channel_id": CID, "limit": 100} крон:"0 5 * * 1"`\n\n'
                "**Красивый анонс** — эмбед с картинкой и полями:\n"
                'параметры:{"channel_id": CID, "title": "Событие!", "description": "Скоро в {server.name}", "image": "https://…", "fields": [{"name": "Когда", "value": "1 декабря", "inline": true}]}\n\n'
                "**Переменные в тексте:** {user.mention}, {user.name}, {server.name}, {server.member_count}, {channel.name}, {date}, {weekday}…\n\n"
                "**Правила по событиям** — см. /события типы:\n"
                '`/события создать событие:вход_участника имя:привет действие:send_message параметры:{"channel_id": CID, "content": "Добро пожаловать на {server.name}, {user.mention}!"}`\n'
                '`/события создать событие:выход_участника имя:прощай действие:send_message parameters:{"channel_id": CID, "content": "{user.name} покинул нас…"}`\n\n'
                "**Анонс с цепочкой** — сообщение, через 5 минут после него запустится задача slowmode_done:\n"
                '`/расписание создать имя:анонс действие:send_message параметры:{"channel_id": CID, "content": "Скоро событие!"} когда:"2026-12-01 18:00" цепочка:slowmode_done задержка_цепочки:5`'
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- создание / просмотр ---------------------------------------------------

    @app_commands.command(name="создать", description="Создать или заменить запланированную задачу.")
    @app_commands.autocomplete(действие=действие_autocomplete)
    @app_commands.describe(
        имя="Уникальное имя задачи; по нему работают список, запустить, пауза, удалить.",
        действие="Какое действие выполнить (начните вводить — фильтр; полный список: /расписание действия).",
        параметры='JSON с параметрами, например {"channel_id": 123456789, "content": "Дайджест за {date}"}. Шаблоны: {date}, {time}, {datetime}, {weekday}.',
        крон="Cron-выражение, например '0 12 * * *' (каждый день в 12:00).",
        интервал_минут="Повторять каждые N минут.",
        когда='Разовый запуск, "ГГГГ-ММ-ДД ЧЧ:ММ".',
        окончание='Дата окончания повторяющейся задачи, "ГГГГ-ММ-ДД ЧЧ:ММ".',
        предел_запусков="Автоудаление задачи после стольких успешных запусков.",
        джиттер_секунд="Случайный сдвиг каждого запуска до N секунд.",
        цепочка="Запустить эту задачу сразу после успешного выполнения текущей.",
        задержка_цепочки="Задержка перед запуском цепочки (в минутах).",
    )
    async def создать(
        self,
        interaction: discord.Interaction,
        имя: str,
        действие: str,
        параметры: str,
        крон: str | None = None,
        интервал_минут: app_commands.Range[int, 1, 527040] | None = None,
        когда: str | None = None,
        окончание: str | None = None,
        предел_запусков: app_commands.Range[int, 1, 100000] | None = None,
        джиттер_секунд: app_commands.Range[int, 1, 600] | None = None,
        цепочка: str | None = None,
        задержка_цепочки: app_commands.Range[int, 0, 10080] | None = None,
    ) -> None:
        task_id = имя
        action = действие
        params = параметры
        cron = крон
        interval_minutes = интервал_минут
        at = когда
        end = окончание
        max_runs = предел_запусков
        jitter_seconds = джиттер_секунд
        chain_task_id = цепочка
        chain_delay_minutes = задержка_цепочки or 0

        clean_id = self._validate_task_id(task_id)
        if clean_id is None:
            await self._error(interaction, "имя: от 1 до 64 символов из букв, цифр и `_ . : -`.")
            return
        try:
            parsed_params = self._parse_params(params)
        except (ValueError, json.JSONDecodeError) as exc:
            await self._error(interaction, f"Некорректный JSON в параметрах: {exc}")
            return
        if action not in actions_module.ACTION_REGISTRY:
            await self._error(interaction, f"Неизвестное действие {action!r}. Смотрите /расписание действия.")
            return
        try:
            missing = [key for key in actions_module.required_params(action) if key not in parsed_params]
            if missing:
                raise ValueError(f"Отсутствуют обязательные параметры для {action}: {', '.join(missing)}")
            replaced = self._scheduler().get_job(clean_id) is not None
            job = await self._scheduler().add_job(
                task_id=clean_id,
                action=action,
                params=parsed_params,
                cron=cron,
                interval_minutes=interval_minutes,
                at=at,
                end=end,
                max_runs=max_runs,
                jitter_seconds=jitter_seconds,
                chain_task_id=chain_task_id,
                chain_delay_minutes=chain_delay_minutes,
            )
        except ValueError as exc:
            await self._error(interaction, f"Не удалось создать задачу: {exc}")
            return

        extras = []
        if max_runs is not None:
            extras.append(f"авто-стоп после {max_runs} запусков")
        if chain_task_id:
            extras.append(f"цепочка → {chain_task_id}")
        when_str = format_next(job)
        await interaction.response.send_message(
            f"Задача **{clean_id}** ({action_display(action)}) создана. Следующий запуск: {when_str}"
            + (f" ({'; '.join(extras)})" if extras else "")
            + (" (заменила старую задачу с этим именем)" if replaced else "")
            + (f" с джиттером ±{jitter_seconds}с" if jitter_seconds else ""),
            ephemeral=True,
        )

    @app_commands.command(name="список", description="Список всех запланированных задач.")
    async def list_tasks(self, interaction: discord.Interaction) -> None:
        jobs = self._scheduler().list_jobs()
        if not jobs:
            await interaction.response.send_message("Задач пока нет.", ephemeral=True)
            return
        lines = [
            f"**{job.task_id}** — `{action_display(job.action)}` — следующий: {format_next(job)}"
            for job in sorted(jobs, key=lambda item: item.task_id)
        ]
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            view=TaskSelectView(self._scheduler()),
        )

    @app_commands.command(name="инфо", description="Подробности и последние запуски задачи.")
    @app_commands.autocomplete(имя=имя_autocomplete_задач)
    @app_commands.describe(имя="Имя задачи.")
    async def инфо(self, interaction: discord.Interaction, имя: str) -> None:
        task_id = имя.strip()
        view = TaskSelectView(self._scheduler())
        view.selected = task_id
        await interaction.response.send_message(embed=view._task_embed(task_id), view=view, ephemeral=True)

    @app_commands.command(name="история", description="История запусков задачи.")
    @app_commands.autocomplete(имя=имя_autocomplete_задач)
    @app_commands.describe(имя="Имя задачи.", сколько="Сколько последних запусков показать (1-20).")
    async def история(
        self,
        interaction: discord.Interaction,
        имя: str,
        сколько: app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        task_id = имя.strip()
        runs = self._scheduler().runlog.recent(task_id, limit=сколько)
        if not runs:
            await interaction.response.send_message(f"У задачи **{task_id}** ещё нет записанных запусков.", ephemeral=True)
            return
        lines = []
        for run in runs:
            mark = "✅" if run["status"] == "success" else "❌"
            error = f" — `{str(run['error'])[:120]}`" if run.get("error") else ""
            lines.append(f"{mark} {str(run['started_at']).split('.')[0]}{error}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # -- управление -----------------------------------------------------------

    @app_commands.command(name="запустить", description="Выполнить задачу прямо сейчас, вне расписания.")
    @app_commands.autocomplete(имя=имя_autocomplete_задач)
    @app_commands.describe(имя="Имя задачи для немедленного запуска.")
    async def запустить(self, interaction: discord.Interaction, имя: str) -> None:
        task_id = имя.strip()
        try:
            action_name, _ = await self._scheduler().run_now(task_id)
        except JobLookupError:
            await interaction.response.send_message(f"Задача **{task_id}** не найдена.", ephemeral=True)
            return
        except (ValueError, RuntimeError) as exc:
            await interaction.response.send_message(f"Ошибка запуска: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Задача **{task_id}** ({action_display(action_name)}) выполнена.", ephemeral=True
        )

    @app_commands.command(name="пауза", description="Приостановить задачу без удаления.")
    @app_commands.autocomplete(имя=имя_autocomplete_задач)
    @app_commands.describe(имя="Имя задачи.")
    async def пауза(self, interaction: discord.Interaction, имя: str) -> None:
        task_id = имя.strip()
        if not self._scheduler().pause_job(task_id):
            await interaction.response.send_message(f"Задача **{task_id}** не найдена.", ephemeral=True)
            return
        await interaction.response.send_message(f"Задача **{task_id}** приостановлена.", ephemeral=True)

    @app_commands.command(name="продолжить", description="Возобновить приостановленную задачу.")
    @app_commands.autocomplete(имя=имя_autocomplete_задач)
    @app_commands.describe(имя="Имя задачи.")
    async def продолжить(self, interaction: discord.Interaction, имя: str) -> None:
        task_id = имя.strip()
        if not self._scheduler().resume_job(task_id):
            await interaction.response.send_message(f"Задача **{task_id}** не найдена.", ephemeral=True)
            return
        await interaction.response.send_message(f"Задача **{task_id}** возобновлена.", ephemeral=True)

    @app_commands.command(name="удалить", description="Удалить задачу навсегда.")
    @app_commands.autocomplete(имя=имя_autocomplete_задач)
    @app_commands.describe(имя="Имя задачи.")
    async def удалить(self, interaction: discord.Interaction, имя: str) -> None:
        task_id = имя.strip()
        if not self._scheduler().remove_job(task_id):
            await interaction.response.send_message(f"Задача **{task_id}** не найдена.", ephemeral=True)
            return
        await interaction.response.send_message(f"Задача **{task_id}** удалена.", ephemeral=True)

    @app_commands.command(name="статус", description="Состояние бота и планировщика.")
    async def статус(self, interaction: discord.Interaction) -> None:
        scheduler = self._scheduler()
        jobs = scheduler.list_jobs()
        upcoming = scheduler.next_wakeup()
        started_at: datetime | None = getattr(self.bot, "started_at", None)
        uptime = discord.utils.utcnow() - started_at if started_at else None
        hours, remainder = divmod(int(uptime.total_seconds()), 3600) if uptime else (0, 0)
        minutes = remainder // 60 if uptime else 0
        guilds = len(self.bot.guilds)
        lines = [
            f"Аптайм: **{hours} ч {minutes} мин** | Задержка: **{self.bot.latency * 1000:.0f} мс** | Серверов: **{guilds}**",
            f"Запланировано задач: **{len(jobs)}**",
            f"Следующая задача: {format_next_datetime(upcoming) if upcoming else '—'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


def format_next(job: JobInfo) -> str:
    if job.next_run is None:
        return "на паузе / завершена"
    epoch = int(job.next_run.timestamp())
    return f"<t:{epoch}:f> (<t:{epoch}:R>)"


def format_next_datetime(when: datetime | None) -> str:
    if when is None:
        return "—"
    epoch = int(when.timestamp())
    return f"<t:{epoch}:f> (<t:{epoch}:R>)"


async def setup(bot) -> None:
    try:
        bot.tree.add_command(ScheduleGroup(bot))
    except app_commands.CommandAlreadyRegistered:
        pass  # расширение перезагружено
