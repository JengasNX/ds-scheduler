"""Persistent task scheduler built on APScheduler + SQLite jobstore.

Features on top of plain scheduling:
- run history and stats via the local RunLog,
- automatic retries with exponential backoff (max 3 attempts),
- ``max_runs``: task is removed after N successful runs,
- ``then``: task chaining (run another task after this one finishes),
- ``end_date`` for recurring schedules and optional jitter.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.base import JobLookupError

from . import config
from .runlog import RunLog
from . import runner
from .runner import run_action

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 120  # 2, 4, 8 ... minutes, capped at one hour


@dataclass(frozen=True)
class JobInfo:
    task_id: str
    action: str
    trigger: str
    next_run: datetime | None
    meta: dict[str, object]


def parse_datetime(value: str, label: str = "date") -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' in the configured timezone."""
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except AttributeError:
        raise ValueError(f"{label} must be a 'YYYY-MM-DD HH:MM' string.") from None
    except ValueError as exc:
        raise ValueError(f"{label} must use the 'YYYY-MM-DD HH:MM' format: {exc}") from exc
    return parsed.replace(tzinfo=ZoneInfo(config.timezone_name()))


class SchedulerService:
    """Owns the AsyncIOScheduler and maps friendly task IDs onto jobs."""

    def __init__(self, db_path: str, timezone_name: str) -> None:
        self.timezone_name = timezone_name
        self._timezone = ZoneInfo(timezone_name)
        self._jobstore = SQLAlchemyJobStore(url=f"sqlite:///{Path(db_path).resolve()}")
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": self._jobstore},
            timezone=self._timezone,
        )
        self._scheduler.add_listener(self._on_scheduler_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        self.runlog = RunLog(db_path)
        self._attempts: dict[str, int] = {}
        self._attempts_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        runner.set_runlog(self.runlog)
        runner.set_failure_handler(self.handle_failure)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._scheduler.start()

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
        self.runlog.close()
        # Deliberately no engine.dispose(): the SQLite file lives for the whole
        # bot lifetime, and disposing while the store may still be polled can
        # race and spam "unable to open database file" during shutdown.

    def _on_scheduler_event(self, event) -> None:
        if event.exception:
            log.error("Scheduled job %r failed: %r", event.job_id, event.exception)
        else:
            if self._loop is not None and self._loop.is_running():
                self._loop.create_task(self._handle_success(str(event.job_id)))

    # -- management ---------------------------------------------------------

    async def add_job(
        self,
        *,
        task_id: str,
        action: str,
        params: dict[str, object],
        cron: str | None = None,
        interval_minutes: int | None = None,
        at: str | None = None,
        end: str | None = None,
        max_runs: int | None = None,
        jitter_seconds: int | None = None,
        chain_task_id: str | None = None,
        chain_delay_minutes: int = 0,
    ) -> JobInfo:
        """Create or replace a job identified by ``task_id`` (schedule survives restarts)."""
        provided = [value for value in (cron, interval_minutes, at) if value is not None]
        if len(provided) != 1:
            raise ValueError("Specify exactly one schedule: cron, interval_minutes, or at.")
        if max_runs is not None and max_runs < 1:
            raise ValueError("max_runs must be at least 1.")

        end_date = parse_datetime(end, "end") if end else None
        meta: dict[str, object] = {}
        if max_runs is not None:
            meta["max_runs"] = int(max_runs)
        if chain_task_id:
            meta["then"] = {"task_id": chain_task_id, "delay_minutes": int(chain_delay_minutes or 0)}
        jitter = int(jitter_seconds) if jitter_seconds else None

        if cron is not None:
            trigger = CronTrigger.from_crontab(cron, timezone=self._timezone)
            if end_date is not None:
                trigger.end_date = end_date
        elif interval_minutes is not None:
            trigger = IntervalTrigger(
                minutes=interval_minutes, end_date=end_date, jitter=jitter, timezone=self._timezone
            )
        else:
            if end_date is not None or jitter:
                log.warning("end/jitter_seconds only apply to recurring schedules; they were ignored.")
            trigger = DateTrigger(run_date=parse_datetime(at, "at"))

        job = self._scheduler.add_job(
            run_action,
            trigger=trigger,
            args=[task_id, action, params, meta],
            id=task_id,
            name=task_id,
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
        log.info("Added job %r (%s)", task_id, action)
        return self._info(job)

    def remove_job(self, task_id: str) -> bool:
        try:
            self._scheduler.remove_job(task_id)
        except JobLookupError:
            return False
        log.info("Removed job %r", task_id)
        return True

    def pause_job(self, task_id: str) -> bool:
        try:
            self._scheduler.pause_job(task_id)
        except JobLookupError:
            return False
        return True

    def resume_job(self, task_id: str) -> bool:
        try:
            self._scheduler.resume_job(task_id)
        except JobLookupError:
            return False
        return True

    def get_job(self, task_id: str) -> JobInfo | None:
        job = self._scheduler.get_job(task_id)
        return self._info(job) if job else None

    def list_jobs(self) -> list[JobInfo]:
        return [self._info(job) for job in self._scheduler.get_jobs()]

    async def run_now(self, task_id: str) -> tuple[str, dict[str, object]]:
        """Run an existing job immediately, bypassing its schedule."""
        job = self._scheduler.get_job(task_id)
        if job is None:
            raise JobLookupError(task_id)
        action, params = job.args[1], job.args[2]
        await run_action(task_id, action, params)
        return action, params

    def next_wakeup(self) -> datetime | None:
        times = [job.next_run for job in self.list_jobs() if job.next_run is not None]
        return min(times) if times else None

    # -- retry / alerting (called from APScheduler worker threads) ------------

    def handle_failure(
        self,
        task_id: str,
        action: str,
        params: dict[str, object],
        meta: dict[str, object],
        exc: Exception,
    ) -> None:
        with self._attempts_lock:
            attempts = self._attempts.get(task_id, 0) + 1
            self._attempts[task_id] = attempts
        if attempts <= MAX_RETRIES:
            delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), 3600)
            when = datetime.now(tz=self._timezone) + timedelta(seconds=delay)
            self._scheduler.add_job(
                run_action,
                trigger=DateTrigger(run_date=when),
                args=[task_id, action, params, meta],
                id=f"{task_id}#retry{attempts}",
                name=f"{task_id} (retry {attempts}/{MAX_RETRIES})",
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
            log.warning(
                "Task %r failed (%s: %s); retry %d/%d in %ds",
                task_id, type(exc).__name__, exc, attempts, MAX_RETRIES, delay,
            )
            return
        with self._attempts_lock:
            self._attempts[task_id] = 0
        log.error("Task %r failed %d times in a row; giving up", task_id, MAX_RETRIES + 1)
        self._alert_giveup(task_id, str(type(exc).__name__), str(exc)[:200])

    def _alert_giveup(self, task_id: str, error_type: str, error_text: str) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(
            self._loop.create_task, self._send_giveup_alert(task_id, error_type, error_text)
        )

    async def _send_giveup_alert(self, task_id: str, error_type: str, error_text: str) -> None:
        from .actions import ACTION_REGISTRY

        alert_channel = config.alert_channel_id()
        bot = runner.get_bot()
        if alert_channel is None or bot is None:
            log.info("No alert channel configured; task %r gave up unnoticed.", task_id)
            return
        lines = [
            f"⚠️ Задача **{task_id}** не удалась {MAX_RETRIES} раз(а) подряд и больше не будет повторяться.",
            f"Ошибка: `{error_type}: {error_text}`",
        ]
        try:
            await ACTION_REGISTRY["send_message"](
                bot, {"channel_id": alert_channel, "content": "\n".join(lines)}
            )
        except Exception:
            log.exception("Failed to deliver the give-up alert for task %r", task_id)

    # -- post-success meta flows ---------------------------------------------

    async def _handle_success(self, job_id: str) -> None:
        base = job_id.split("#")[0]
        job = self._scheduler.get_job(base)
        successes, _ = self.runlog.stats(base)
        if job is not None:
            meta = job.args[3] if len(job.args) >= 4 else {}
        else:
            meta = {}
        if not meta:
            return

        max_runs = meta.get("max_runs")
        if max_runs and successes >= int(max_runs):
            self._remove_family(base)
            log.info("Task %r reached max_runs=%d and was removed.", base, max_runs)
            return

        then = meta.get("then")
        if then:
            self._schedule_chain(base, then, job)

    def _schedule_chain(self, source_id: str, then: dict[str, object], source_job) -> None:
        target_id = str(then.get("task_id", ""))
        delay = int(then.get("delay_minutes", 0) or 0)
        target = self._scheduler.get_job(target_id)
        if target is None:
            log.warning("Chain from %r points to unknown task %r", source_id, target_id)
            return
        when = datetime.now(tz=self._timezone) + timedelta(minutes=delay)
        self._scheduler.add_job(
            run_action,
            trigger=DateTrigger(run_date=when),
            args=list(target.args),
            id=f"{target_id}#chain",
            name=f"{target_id} (chained from {source_id})",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
        log.info("Chained task %r will run in %d minute(s) after %r", target_id, delay, source_id)

    def _remove_family(self, base: str) -> None:
        for job in list(self._scheduler.get_jobs()):
            if job.id == base or str(job.id).startswith(f"{base}#"):
                try:
                    self._scheduler.remove_job(job.id)
                except JobLookupError:
                    pass

    # -- helpers ------------------------------------------------------------

    def _info(self, job) -> JobInfo:
        args = job.args or []
        action = str(args[1]) if len(args) >= 2 else "?"
        meta = args[3] if len(args) >= 4 else {}
        return JobInfo(
            task_id=str(job.id),
            action=action,
            trigger=str(job.trigger),
            next_run=job.next_run_time,
            meta=dict(meta),
        )
