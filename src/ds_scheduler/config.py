"""Runtime configuration read from the environment (.env supported)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEZONE = "Europe/Kaliningrad"


def token() -> str:
    value = os.getenv("DISCORD_TOKEN", "").strip()
    if not value:
        raise RuntimeError("DISCORD_TOKEN is not set; copy .env.example to .env and fill it in.")
    return value


def timezone_name() -> str:
    return os.getenv("SCHEDULER_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE


def db_path() -> str:
    return os.getenv("SCHEDULER_DB_PATH", "scheduler_jobs.db")


def dev_guild_id() -> int | None:
    raw = os.getenv("DEV_GUILD_ID", "").strip()
    return int(raw) if raw else None


def alert_channel_id() -> int | None:
    """Optional channel that receives an alert when a task gives up on retries."""
    raw = os.getenv("ALERT_CHANNEL_ID", "").strip()
    return int(raw) if raw else None
