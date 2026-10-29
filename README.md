# ds-scheduler — планировщик задач с Discord-функциями

![CI](https://github.com/JengasNX/ds-scheduler/actions/workflows/ci.yml/badge.svg)

Discord-бот, который по расписанию сам вызывает зарегистрированные async-действия,
например `await send_message(bot, channel_id, info)`.

Задачи хранятся в SQLite (APScheduler SQLAlchemyJobStore), поэтому переживают
перезапуск бота. Управление — через slash-команды (интерфейс команд на русском,
имена команд и действий — на английском). Каждый запуск логируется,
неудачные задачи автоматически ретраятся с нарастающей задержкой (до 3 попыток),
а при исчерпании попыток бот шлёт алерт (см. `ALERT_CHANNEL_ID`).

## Переменные-подстановки

В любом тексте `параметры` (и в `/события`) работают переменные:

| Группа | Переменные |
|---|---|
| `{user.*}` | `name`, `mention`, `nickname`, `id`, `joined_at`, `created_at`, `avatar_url`, `top_role` — только в событиях (там есть участник) |
| `{server.*}` | `name`, `id`, `member_count`, `description`, `created_at`, `icon_url`, `owner_mention`, `owner_name` |
| `{channel.*}` | `name`, `id`, `mention`, `topic` |
| Дата/время | `{date}`, `{time}`, `{datetime}`, `{weekday}`, `{year}`, `{month}`, `{day}`, `{date:ПОДСТРОКА}` (strftime: `{date:%d.%m}`) |

Нераспознанные токены остаются как есть (литеральный текст в фигурных скобках безопасен).

## Полный embed-builder

`embed` у `send_message` и `send_embed` принимает все поля Discord:

```json
{
  "title": "Анонс",
  "title_url": "https://...",
  "description": "Скоро в {server.name}!",
  "colour": "#3865EF",
  "author": {"name": "{server.owner_name}", "icon_url": "https://..."},
  "image": "https://...",
  "thumbnail": "https://...",
  "footer": {"text": "Вызывайте!", "icon_url": "https://..."},
  "fields": [
    {"name": "Когда", "value": "1 декабря", "inline": true},
    {"name": "Где", "value": "{channel.name}", "inline": true}
  ]
}
```

## Упоминания

`allow_mentions` в params: не указано → ничего не упоминается; `"all"` → всё;
`"users"`, `"roles"`, `"everyone"` или через запятую — выборочно.

## Правила на события (/события)

Действия также запускаются не по времени, а по событию. Команды:

| Команда | Описание |
|---|---|
| `/события создать событие имя действие параметры` | создать/заменить правило |
| `/события удалить имя [событие]` | удалить правила задачи |
| `/события список` | все правила |
| `/события типы` | список событий |

События (выбираются из списка): `member_join`, `member_remove`, `member_ban`, `member_unban`,
`member_update` (фильтр `фильтр_изменений`: any/nickname/roles/status),
`voice_join`, `voice_leave`, `voice_switch`.

```
/события создать событие:вход_участника имя:привет действие:send_message
  параметры:{"channel_id": CID, "content": "Добро пожаловать на {server.name}, {user.mention}!"}
```

**Важно:** для событий уровня участников нужен **Server Members Intent**
(Developer Portal → Bot → Privileged Gateway Intents → включить «Server Members Intent»).

## Модули-заготовки

Папка `src/ds_scheduler/modules/` — сюда складываются будущие фичи
(ranking, audio и т.д.). Каждый модуль — пакет с `setup(bot)`; его
действия автоматически попадают в реестр. Пример-скелет: `modules/example`.

## Установка

```bash
cd ds-scheduler
python -m venv .venv
.venv\Scripts\activate
pip install -e .
copy .env.example .env   # вписать DISCORD_TOKEN
python -m ds_scheduler
```

### Docker

```bash
docker build -t ds-scheduler .
docker run -d --name ds-scheduler \
  --env-file .env \
  -e SCHEDULER_DB_PATH=/app/data/scheduler_jobs.db \
  -v ./data:/app/data \
  ds-scheduler
```

### CI

`.github/workflows/ci.yml` прогоняет `smoke_test.py` на каждый push
и pull-request (бейдж вверху README).

Совет: на время разработки укажите `DEV_GUILD_ID` в `.env`, чтобы slash-команды
синхронизировались мгновенно (глобальный sync занимает до часа).

## Команды

| Команда | Описание |
|---|---|
| `/конструктор создать имя действие` | **Интерактивная сборка задачи: поля списками, расписание селектом, без JSON** |
| `/сообщение раз имя канал текст когда` | Одно сообщение в указанное время — форма без JSON |
| `/сообщение каждый_день имя канал текст время` | Сообщение каждый день (время: `12:00`) |
| `/сообщение каждую_неделю имя канал текст день время` | Сообщение раз в неделю |
| `/сообщение каждые_минуты имя канал текст минут` | Повтор каждые N минут (мин. 5) |
| `/расписание создать ...` | Создать/заменить задачу напрямую (JSON-параметры) |
| `/расписание список` | Список задач + интерактивный выбор с кнопками (▶️ ⏸️ ↩️ 🗑️) |
| `/расписание инфо имя` | Карточка задачи: триггер, параметры, статистика, последние 3 запуска |
| `/расписание история имя` | История запусков (успех/ошибка) |
| `/расписание запустить имя` | Выполнить задачу немедленно |
| `/расписание пауза` / `продолжить` / `удалить имя` | Управление задачами |
| `/расписание действия` | Все доступные действия |
| `/расписание рецепты` | Готовые рецепты задач для копирования |
| `/расписание статус` | Аптайм, число задач, следующий запуск |

Полноценная альтернатива JSON-`параметрам` — **`/конструктор создать`**: бот
открывает панель-приложение, где каждое поле выбирается списком (канал/роль/
участник — нативными селектами Discord, `сервер` подставляется сам, остальное —
в модальных формах с русскими подсказками). Там же выбирается расписание,
есть предпросмотр итогового JSON и кнопка тест-запуска. Создавать задачи можно
и без `/конструктор` — простые сообщения через `/сообщение ...`, сложные
случаи (эмбеды, цепочки) — через `/расписание создать` с JSON.

В тексте сообщений работают переменные: `{date}`, `{weekday}`, `{server.name}`,
`{channel.name}`, а в правилах `/события` ещё `{user.mention}`.

`имя` везде поддерживает **autocomplete**; `действие` фильтруется по вводу.

## Опции триггеров (`/расписание создать`)

| Опция | Действие |
|---|---|
| `крон`, `интервал_минут`, `когда` | Обязательно ровно одно |
| `окончание` | `ГГГГ-ММ-ДД ЧЧ:ММ` — когда повторяющаяся задача перестаёт срабатывать |
| `предел_запусков` | Автоснятие задачи после N успешных запусков |
| `джиттер_секунд` | Случайный сдвиг каждого запуска (±N секунд) |
| `цепочка`, `задержка_цепочки` | Запустить другую задачу после успешного выполнения |

В тексте `параметры` работают шаблоны: `{date}`, `{time}`, `{datetime}`, `{weekday}`
(например: `"Дайджест за {date}, {weekday}"`).

## Действия

| Действие | Обязательные параметры | Необязательные |
|---|---|---|
| `send_message` | `channel_id`, `content` | `embed {title, description, colour, footer}`, `allow_mentions` |
| `send_embed` | `channel_id`, `title`, `description` | `colour`, `footer`, `allow_mentions` |
| `edit_message` | `channel_id`, `message_id`, `content` | `embed` |
| `delete_message` | `channel_id`, `message_id` | `reason` |
| `add_reaction` | `channel_id`, `message_id`, `emoji` | — |
| `pin_message` / `unpin_message` | `channel_id`, `message_id` | `reason` |
| `purge_messages` | `channel_id`, `limit` (1-500) | `reason` |
| `send_file` | `channel_id`, `url` | `content`, `filename`, `allow_mentions` |
| `create_poll` | `channel_id`, `question`, `options` (2-10) | `duration_hours` (1-168), `allow_mentions` |
| `create_thread` | `channel_id`, `name` | `message_id`, `reason` |
| `set_slowmode` | `channel_id`, `delay_seconds` (0-21600) | `reason` |
| `edit_channel_permissions` | `channel_id`, `role_id` | `allow`, `deny` (через запятую), `reason` |
| `create_channel` | `guild_id`, `name` | `channel_type` (text/voice), `category_id`, `topic`, `reason` |
| `delete_channel` | `channel_id` | `reason` |
| `rename_channel` | `channel_id`, `new_name` | `reason` |
| `set_channel_topic` | `channel_id`, `topic` | `reason` |
| `create_role` | `guild_id`, `name` | `colour`, `hoist`, `mentionable`, `reason` |
| `delete_role` | `guild_id`, `role_id` | `reason` |
| `add_role` / `remove_role` | `guild_id`, `user_id`, `role_id` | `reason` |
| `dm_user` | `user_id`, `content` | `allow_mentions` |
| `ban_user` / `unban_user` | `guild_id`, `user_id` | `reason`, `delete_message_seconds` |
| `timeout_user` | `guild_id`, `user_id`, `duration_seconds` | `reason` (макс. 28 дней) |
| `create_scheduled_event` | `guild_id`, `name`, `start_time` | `end_time`, `description`, `location`, `reason` |

`start_time` / `end_time` у событий и `end` у задач — формат `YYYY-MM-DD HH:MM`.

## Надёжность

- Каждый запуск пишется в таблицу `job_runs` (см. `/расписание инфо` и `/расписание история`).
- Неудача → ретраи через 2, 4, 8 минут (итого 4 попытки включая первичную).
- Если все попытки исчерпаны → сообщение в канал `ALERT_CHANNEL_ID` (если задан).

## Как добавить своё действие

В `src/ds_scheduler/actions.py`:

```python
@register("my_action", requires=("channel_id",))
async def my_action(bot, params):
    channel = bot.get_channel(int(params["channel_id"]))
    await channel.send(str(params.get("text", "hello")))
```

Новое действие сразу появится в `/расписание действия`, `/расписание создать`
(через autocomplete) — ничего дополнительно регистрировать не нужно.
