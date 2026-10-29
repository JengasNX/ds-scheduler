"""Интерактивный конструктор задач: простая альтернатива ручному JSON.

`/конструктор создать имя действие` открывает панель, где:
- поля выбираются из списка: канал/роль/участник — нативные Discord-селекты,
  остальное — модальные формы с русскими подсказками;
- `сервер` подставляется сам;
- расписание задаётся второй строкой селекта (раз / день / неделя / интервал);
- есть предпросмотр JSON и тест-запуск прямо из панели.

В конце бот сам собирает параметры и создаёт задачу.
"""
from __future__ import annotations

import json
import logging
import re

import discord
from discord import app_commands

from .. import actions as actions_module
from .. import runner
from ..placeholders import PlaceholderContext
from .schedule_commands import TASK_ID_PATTERN, action_display, format_next
from .schedule_commands import действие_autocomplete as action_autocomplete

log = logging.getLogger(__name__)

# --- словари ключ → русское имя -------------------------------------------------

RU_PARAMS = {
    "channel_id": "канал",
    "content": "текст",
    "title": "заголовок",
    "description": "описание",
    "message_id": "ID сообщения",
    "emoji": "эмодзи",
    "guild_id": "сервер",
    "name": "название",
    "new_name": "новое имя",
    "topic": "тема канала",
    "user_id": "участник",
    "role_id": "роль",
    "duration_seconds": "секунды таймаута",
    "limit": "сколько сообщений",
    "url": "ссылка на файл",
    "question": "вопрос опроса",
    "options": "варианты (через запятую)",
    "channel_type": "тип канала (text/voice)",
    "category_id": "категория",
    "reason": "причина",
    "colour": "цвет (#RRGGBB)",
    "hoist": "выделять на списке (true/false)",
    "mentionable": "можно упоминать (true/false)",
    "embed": "эмбед (JSON)",
    "allow_mentions": "упоминания (all/users/roles/everyone)",
    "footer": "подпись снизу ({\"text\": ...})",
    "allow": "разрешить права (через запятую)",
    "deny": "запретить права (через запятую)",
    "filename": "имя файла",
    "duration_hours": "часов жизни опроса",
    "delete_message_seconds": "удалить сообщения за (сек)",
    "start_time": "начало события (ГГГГ-ММ-ДД ЧЧ:ММ)",
    "end_time": "конец события (ГГГГ-ММ-ДД ЧЧ:ММ)",
    "location": "место события",
}

NUMBER_KEYS = {
    "message_id", "limit", "duration_seconds", "delete_message_seconds",
    "duration_hours", "delay_seconds",
}
LIST_KEYS = {"options", "allow", "deny"}

OPTIONAL_SCHEMA = {
    "send_message": ("embed", "allow_mentions"),
    "send_embed": ("colour", "footer", "allow_mentions"),
    "edit_message": ("embed",),
    "delete_message": ("reason",),
    "add_reaction": (),
    "create_channel": ("channel_type", "category_id", "topic", "reason"),
    "delete_channel": ("reason",),
    "rename_channel": ("reason",),
    "set_channel_topic": ("reason",),
    "add_role": ("reason",),
    "remove_role": ("reason",),
    "dm_user": ("allow_mentions",),
    "ban_user": ("reason", "delete_message_seconds"),
    "unban_user": ("reason",),
    "timeout_user": ("reason",),
    "pin_message": ("reason",),
    "unpin_message": ("reason",),
    "purge_messages": ("reason",),
    "send_file": ("content", "filename", "allow_mentions"),
    "create_poll": ("duration_hours", "allow_mentions"),
    "create_thread": ("message_id", "reason"),
    "set_slowmode": ("reason",),
    "edit_channel_permissions": ("allow", "deny", "reason"),
    "create_scheduled_event": ("end_time", "description", "location", "reason"),
    "create_role": ("colour", "hoist", "mentionable", "reason"),
    "delete_role": ("reason",),
}

DAYS_RU = {"пн": "1", "вт": "2", "ср": "3", "чт": "4", "пт": "5", "сб": "6", "вс": "0"}
WHEN_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}")
TIME_PATTERN = re.compile(r"(\d{1,2}):(\d{2})")


def param_kind(key: str) -> str:
    """Тип поля: channel / role / user / number / list / guild / text."""
    if key in ("channel_id", "category_id"):
        return "channel"
    if key == "role_id":
        return "role"
    if key == "user_id":
        return "user"
    if key == "guild_id":
        return "guild"
    if key in NUMBER_KEYS:
        return "number"
    if key in LIST_KEYS:
        return "list"
    return "text"


def coerce_value(kind: str, raw: str) -> tuple[object, str | None]:
    """Строка формы → значение параметра; возвращает (значение, ошибка)."""
    raw = raw.strip()
    if kind == "number":
        try:
            return int(raw), None
        except ValueError:
            return None, "тут нужно целое число"
    if kind == "list":
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            return None, "укажите хотя бы один вариант через запятую"
        return parts, None
    if kind in ("channel", "role", "user", "guild"):
        return None, "это поле выбирается списком"
    if not raw:
        return None, "пустое значение"
    return raw, None


def _short(value: object, limit: int = 42) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_time(raw: str) -> tuple[int, int] | None:
    match = TIME_PATTERN.fullmatch(raw.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


# --- модальные формы ------------------------------------------------------------


class ParamModal(discord.ui.Modal):
    """Модальная форма для текстового / числового / спискового поля."""

    def __init__(self, wizard: "TaskBuilder", key: str, kind: str) -> None:
        label = RU_PARAMS.get(key, key)
        super().__init__(title=label[:45])
        self.wizard = wizard
        self.key = key
        self.kind = kind
        current = wizard.data.get(key)
        self.field = discord.ui.TextInput(
            label=label[:45],
            default=str(current) if current is not None else None,
            max_length=1500,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value, error = coerce_value(self.kind, str(self.field.value))
        if error:
            await self.wizard.show_panel(interaction, error=f"{self.key}: {error}")
        else:
            self.wizard.set_param(self.key, value)
            await self.wizard.show_panel(interaction)


class ScheduleModal(discord.ui.Modal):
    """Модальная форма выбора расписания (раз / день / неделя / интервал)."""

    def __init__(self, wizard: "TaskBuilder", mode: str) -> None:
        self.wizard = wizard
        self.mode = mode
        if mode == "once":
            super().__init__(title="Разовое: когда")
            self.a = discord.ui.TextInput(
                label="Когда (ГГГГ-ММ-ДД ЧЧ:ММ)", placeholder="2026-12-01 18:00", max_length=40
            )
        elif mode == "daily":
            super().__init__(title="Каждый день: во сколько")
            self.a = discord.ui.TextInput(label="Время (ЧЧ:ММ)", placeholder="12:00", max_length=10)
        elif mode == "weekly":
            super().__init__(title="Каждую неделю: когда")
            self.a = discord.ui.TextInput(
                label="День недели", placeholder="пн / вт / ср / чт / пт / сб / вс", max_length=10
            )
            self.b = discord.ui.TextInput(label="Время (ЧЧ:ММ)", placeholder="18:00", max_length=10)
        else:  # minutes
            super().__init__(title="Повтор с интервалом")
            self.a = discord.ui.TextInput(label="Каждые сколько минут (мин. 5)", placeholder="30", max_length=6)
        self.add_item(self.a)
        if hasattr(self, "b"):
            self.add_item(self.b)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wizard, mode = self.wizard, self.mode
        raw_a = str(self.a.value).strip()
        if mode == "once":
            raw_a = raw_a.replace("T", " ")
            if not WHEN_PATTERN.fullmatch(raw_a):
                await wizard.show_panel(
                    interaction, error='когда: формат "ГГГГ-ММ-ДД ЧЧ:ММ", например 2026-12-01 18:00.'
                )
                return
            wizard.schedule = ("at", raw_a)
            wizard.schedule_desc = f"раз — {raw_a}"
        elif mode == "daily":
            parsed = parse_time(raw_a)
            if parsed is None:
                await wizard.show_panel(interaction, error='время: укажите "ЧЧ:ММ", например 12:00.')
                return
            hour, minute = parsed
            wizard.schedule = ("cron", f"{minute} {hour} * * *")
            wizard.schedule_desc = f"каждый день в {hour:02d}:{minute:02d}"
        elif mode == "weekly":
            day = DAYS_RU.get(raw_a[:2].lower())
            if day is None and raw_a in ("0", "1", "2", "3", "4", "5", "6"):
                day = raw_a
            parsed = parse_time(str(self.b.value)) if hasattr(self, "b") else None
            if day is None or parsed is None:
                await wizard.show_panel(interaction, error="день: пн/вт/ср/чт/пт/сб/вс; время: ЧЧ:ММ.")
                return
            hour, minute = parsed
            wizard.schedule = ("cron", f"{minute} {hour} * * {day}")
            wizard.schedule_desc = f"каждую неделю ({raw_a[:2]}) в {hour:02d}:{minute:02d}"
        else:  # minutes
            try:
                minutes = int(raw_a)
            except ValueError:
                minutes = 0
            if minutes < 5:
                await wizard.show_panel(interaction, error="минут: от 5 и больше.")
                return
            wizard.schedule = ("cron", f"*/{minutes} * * * *")
            wizard.schedule_desc = f"каждые {minutes} мин"
        await wizard.show_panel(interaction)


# --- панель выбора канала/роли/участника -----------------------------------------


def _pick_class(kind: str):
    base, placeholder = {
        "channel": (discord.ui.ChannelSelect, "Выберите канал…"),
        "role": (discord.ui.RoleSelect, "Выберите роль…"),
        "user": (discord.ui.UserSelect, "Выберите участника…"),
    }[kind]

    class Pick(base):  # type: ignore[misc, valid-type]
        async def callback(self, interaction: discord.Interaction) -> None:
            wizard = getattr(self.view, "wizard", None)
            if wizard is not None:
                key = getattr(self.view, "key", "channel_id")
                wizard.set_param(key, self.values[0].id)
                await wizard.show_panel(interaction)

    return Pick, placeholder


class BackButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="↩ Назад", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        wizard = getattr(self.view, "wizard", None)
        if wizard is not None:
            await wizard.show_panel(interaction)


class ParamPickView(discord.ui.View):
    """Временная панель выбора канала/роли/участника для одного поля."""

    def __init__(self, wizard: "TaskBuilder", key: str, kind: str) -> None:
        super().__init__(timeout=300)
        self.wizard = wizard
        self.key = key
        pick_class, placeholder = _pick_class(kind)
        self.add_item(pick_class(placeholder=placeholder, min_values=1, max_values=1))
        self.add_item(BackButton())


# --- главная панель --------------------------------------------------------------


class TaskBuilder(discord.ui.View):
    """Конструктор задачи: панель с селектами, кнопками и постепенным заполнением."""

    def __init__(self, bot, task_name: str, action: str, guild_id: int) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.task_name = task_name
        self.action = action
        self.data: dict[str, object] = {}
        self.schedule: tuple[str, str] | None = None
        self.schedule_desc: str | None = None
        self.preview = False
        self.last_note: str | None = None
        self.last_error: str | None = None
        for key in actions_module.required_params(action):
            if key == "guild_id":
                self.data["guild_id"] = guild_id  # сервер подставляется сразу
        self.refresh_selects()

    # -- состояние ------------------------------------------------------------

    def set_param(self, key: str, value: object) -> None:
        self.data[key] = value
        self.last_error = None
        self.last_note = f"Поле «{RU_PARAMS.get(key, key)}» заполнено."

    def _missing_required(self) -> list[str]:
        return [key for key in actions_module.required_params(self.action) if key not in self.data]

    def _optional(self) -> list[str]:
        return [key for key in OPTIONAL_SCHEMA.get(self.action, ()) if key not in self.data]

    def parse_time(self, raw: str) -> tuple[int, int] | None:  # вызывается из модалок
        return parse_time(raw)

    # -- селекты: опции обновляются после каждого изменения -------------------------

    def refresh_selects(self) -> None:
        options: list[discord.SelectOption] = []
        for key in actions_module.required_params(self.action):
            if key in self.data:
                continue
            options.append(
                discord.SelectOption(
                    label=f"{RU_PARAMS.get(key, key)} — обязательно",
                    value=key,
                    description=f"поле `{key}`",
                )
            )
        for key in self._optional():
            options.append(
                discord.SelectOption(
                    label=f"{RU_PARAMS.get(key, key)} — по желанию",
                    value=key,
                    description=f"необязательное поле `{key}`",
                )
            )
        if not options:
            options = [discord.SelectOption(label="Все поля заполнены ✅", value="__none__")]
        self.pick_field.options = options[:25]
        self.pick_schedule.options = [
            discord.SelectOption(label="Раз, в указанное время", value="once", description="однократный запуск"),
            discord.SelectOption(label="Каждый день", value="daily", description="например в 12:00"),
            discord.SelectOption(label="Каждую неделю", value="weekly", description="выберите день и время"),
            discord.SelectOption(label="Каждые N минут", value="minutes", description="периодический повтор (мин. 5)"),
        ]

    # -- отображение ------------------------------------------------------------

    def embed(self) -> discord.Embed:
        lines = [f"Действие: **{action_display(self.action)}** (`{self.action}`)", ""]
        for key in self.data:
            icon = "✅" if key in actions_module.required_params(self.action) else "➕"
            lines.append(f"{icon} `{key}` ({RU_PARAMS.get(key, key)}) = `{_short(self.data[key])}`")
        missing = self._missing_required()
        if missing:
            lines.append("")
            lines.append("⬜ Осталось обязательно: " + ", ".join(f"`{k}` ({RU_PARAMS.get(k, k)})" for k in missing))
        cancel = self._optional()
        if cancel:
            lines.append("🔹 Дополнительно (по желанию): " + ", ".join(f"`{k}`" for k in cancel))
        embed = discord.Embed(
            title=f"Конструктор задачи «{self.task_name}»",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Расписание",
            value=f"⏰ {self.schedule_desc}" if self.schedule_desc else "— не выбрано (селект ниже)",
            inline=False,
        )
        if self.preview and self.data:
            try:
                pretty = json.dumps(self.data, ensure_ascii=False, indent=2)
            except TypeError:
                pretty = json.dumps(self.data, ensure_ascii=False, default=str, indent=2)
            embed.add_field(name="Предпросмотр параметров", value=f"```json\n{pretty[:1000]}\n```", inline=False)
        footer_bits = []
        if self.last_error:
            footer_bits.append("⚠️ " + self.last_error)
        if self.last_note:
            footer_bits.append("ℹ️ " + self.last_note)
        if footer_bits:
            embed.set_footer(text="  |  ".join(footer_bits)[:200])
        return embed

    # -- общие переходы -----------------------------------------------------------

    async def show_panel(
        self,
        interaction: discord.Interaction,
        *,
        error: str | None = None,
        note: str | None = None,
    ) -> None:
        if error is not None:
            self.last_error = error
            self.last_note = None
        elif note is not None:
            self.last_note = note
            self.last_error = None
        self.refresh_selects()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)

    # -- селект полей --------------------------------------------------------------

    @discord.ui.select(placeholder="1️⃣ Какое поле заполнить", row=0)
    async def pick_field(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        key = select.values[0]
        if not isinstance(key, str):
            return
        if key == "__none__":
            await self.show_panel(interaction, note="Все поля уже заполнены ✅")
            return
        kind = param_kind(key)
        if kind in ("channel", "role", "user"):
            await interaction.response.edit_message(
                content=None,
                embed=discord.Embed(
                    title=f"Поле «{RU_PARAMS.get(key, key)}» ({key})",
                    description="Выберите значение селектом ниже или вернитесь назад.",
                    colour=discord.Colour.blurple(),
                ),
                view=ParamPickView(self, key, kind),
            )
        else:
            await interaction.response.send_modal(ParamModal(self, key, kind))

    @discord.ui.select(placeholder="2️⃣ Когда запускать", row=1)
    async def pick_schedule(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        mode = select.values[0]
        if not isinstance(mode, str):
            return
        await interaction.response.send_modal(ScheduleModal(self, mode))

    # -- кнопки ---------------------------------------------------------------------

    @discord.ui.button(label="👁 Предпросмотр", style=discord.ButtonStyle.secondary, row=2)
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.preview = not self.preview
        await self.show_panel(interaction)

    @discord.ui.button(label="▶ Тест-запуск", style=discord.ButtonStyle.secondary, row=2)
    async def test_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._missing_required():
            await self.show_panel(
                interaction,
                error="Сначала заполните все обязательные поля (селект сверху).",
            )
            return
        await interaction.response.defer()
        channel = self.bot.get_channel(int(self.data["channel_id"])) if "channel_id" in self.data else None
        ctx = PlaceholderContext(
            guild=interaction.guild,
            channel=channel,
            member=interaction.user if isinstance(interaction.user, discord.Member) else None,
        )
        try:
            await runner.run_action(f"{self.task_name}·тест", self.action, dict(self.data), ctx=ctx)
            self.last_note = "Тест выполнен ✅ — действие сработало прямо сейчас (задача ещё не создана)"
            self.refresh_selects()
            await interaction.edit_original_response(embed=self.embed(), view=self, content=None)
        except Exception as exc:  # ошибки действия показываем на панели
            log.warning("Builder test failed for %s: %s", self.action, exc)
            self.last_note = None
            self.last_error = f"Тест не сработал: {str(exc)[:150]}"
            self.refresh_selects()
            await interaction.edit_original_response(embed=self.embed(), view=self, content=None)

    @discord.ui.button(label="✅ Создать задачу", style=discord.ButtonStyle.success, row=2)
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        missing = self._missing_required()
        if missing:
            await self.show_panel(
                interaction,
                error="Остались обязательные поля: " + ", ".join(missing),
            )
            return
        if not self.schedule:
            await self.show_panel(interaction, error="Сначала выберите расписание (селект «Когда запускать»).")
            return
        mode, value = self.schedule
        try:
            job = await self.bot.scheduler.add_job(
                task_id=self.task_name,
                action=self.action,
                params=dict(self.data),
                cron=value if mode == "cron" else None,
                at=value if mode == "at" else None,
            )
        except ValueError as exc:
            await self.show_panel(interaction, error=f"Планировщик отклонил задачу: {exc}")
            return
        done = discord.Embed(
            title=f"Задача «{self.task_name}» создана 🎉",
            description=(
                f"Действие: {action_display(self.action)} (`{self.action}`)\n"
                f"Когда: **{self.schedule_desc}**\n"
                f"Первый запуск: {format_next(job)}\n\n"
                f"Управлять: /расписание список · /расписание инфо имя:{self.task_name} · "
                f"/расписание удалить имя:{self.task_name}"
            ),
            colour=discord.Colour.green(),
        )
        done.set_footer(text="Панель конструктора закрыта.")
        await interaction.response.edit_message(content=None, embed=done, view=None)

    @discord.ui.button(label="⏹ Отмена", style=discord.ButtonStyle.danger, row=2)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(title="Конструктор закрыт", description="Задача не создана.", colour=0x2E3136),
            view=None,
        )


# --- slash-команда ----------------------------------------------------------------


class BuilderGroup(app_commands.Group):
    """Группа /конструктор."""

    def __init__(self, bot) -> None:
        super().__init__(
            name="конструктор",
            description="Создание задач шаг за шагом, без JSON.",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )
        self.bot = bot

    @app_commands.command(
        name="создать",
        description="Собрать задачу интерактивно: поля списками, без JSON.",
    )
    @app_commands.autocomplete(действие=action_autocomplete)
    @app_commands.describe(
        имя="Имя будущей задачи (для истории и удаления).",
        действие="Что бот будет делать (начните вводить; полный список: /расписание действия).",
    )
    async def создать(
        self,
        interaction: discord.Interaction,
        имя: str,
        действие: str,
    ) -> None:
        task_id = имя.strip()
        if not TASK_ID_PATTERN.fullmatch(task_id):
            await interaction.response.send_message(
                "имя: от 1 до 64 символов из букв, цифр и `_ . : -`.", ephemeral=True
            )
            return
        if действие not in actions_module.ACTION_REGISTRY:
            await interaction.response.send_message(
                f"Неизвестное действие {действие!r}. Смотрите /расписание действия.", ephemeral=True
            )
            return
        wizard = TaskBuilder(self.bot, task_id, действие, interaction.guild_id or 0)
        await interaction.response.send_message(embed=wizard.embed(), view=wizard, ephemeral=True)


async def setup(bot) -> None:
    try:
        bot.tree.add_command(BuilderGroup(bot))
    except app_commands.CommandAlreadyRegistered:
        pass  # расширение перезагружено
