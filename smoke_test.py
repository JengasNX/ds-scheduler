"""Smoke test for ds-scheduler without a Discord connection."""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ds_scheduler import actions as actions_module
from ds_scheduler import runner
from ds_scheduler.events import EventRules
from ds_scheduler.placeholders import PlaceholderContext, expand, unresolved_tokens
from ds_scheduler.scheduler import SchedulerService, parse_datetime


def fake_member():
    return SimpleNamespace(
        user=SimpleNamespace(id=42, name="Вася", created_at=None, display_avatar=None),
        mention="<@42>",
        display_name="ВасяПрозвище",
        joined_at=None,
        top_role=SimpleNamespace(name="Старожил"),
    )


def fake_guild():
    return SimpleNamespace(
        id=99, name="Гильдия Тест", description=None, icon=None,
        member_count=7, owner=SimpleNamespace(mention="<@1>", display_name="Хозяин"), created_at=None,
    )


def fake_channel():
    return SimpleNamespace(id=123, name="общий", mention="<#123>", topic="тестовая тема", guild=None)


async def main() -> int:
    failures: list[str] = []

    for name, func in actions_module.ACTION_REGISTRY.items():
        if not asyncio.iscoroutinefunction(func):
            failures.append(f"action {name} is not a coroutine function")
    print(f"registry OK: {len(actions_module.ACTION_REGISTRY)} actions")

    # Unknown action must raise a clean error even without bot attached.
    try:
        await runner.run_action("test", "no_such_action", {})
        failures.append("run_action did not raise for unknown action")
    except ValueError:
        print("unknown action -> ValueError OK")

    # -- placeholders ------------------------------------------------------
    ctx = PlaceholderContext(guild=fake_guild(), channel=fake_channel(), member=fake_member())
    text = expand("{user.mention} в {channel.name} на {server.name} ({server.member_count})",
                  ctx)
    if text != "<@42> в общий на Гильдия Тест (7)":
        failures.append(f"placeholder expansion wrong: {text}")
    untouched = expand("{unknown.foo} и {user.id}", ctx)
    if "{unknown.foo}" not in untouched or "42" not in untouched:
        failures.append(f"unknown tokens must stay literal: {untouched}")
    custom_date = expand("{date:%d/%m}", ctx)
    from datetime import datetime
    if custom_date != datetime.now().strftime("%d/%m"):
        failures.append(f"custom date format wrong: {custom_date}")
    no_ctx_user = expand("привет {user.name}", None)
    if "привет {user.name}" not in no_ctx_user:
        failures.append(f"user tokens must stay literal without event ctx: {no_ctx_user}")
    needed = unresolved_tokens("send_message", {"content": "привет, {user.name}"})
    if needed != ["{user.name}"]:
        failures.append(f"unresolved_tokens detection wrong: {needed}")
    print(f"placeholders OK: {text}")

    # -- full embed builder --------------------------------------------------
    spec = {
        "title": "Заголовок", "title_url": "https://example.com",
        "description": "текст", "colour": "#3865EF",
        "author": {"name": "Автор", "icon_url": "https://a.png"},
        "image": "https://img.png", "thumbnail": "https://thumb.png",
        "footer": {"text": "низ", "icon_url": "https://f.png"},
        "fields": [
            {"name": "Поле", "value": "значение", "inline": True},
            {"name": "Широкое", "value": "второе"},
        ],
    }
    embed = actions_module._make_embed(spec)
    if len(embed.fields) != 2 or not embed.fields[0].inline or embed.fields[1].inline:
        failures.append("embed fields parsing wrong")
    if embed.author.name != "Автор" or embed.image.url != "https://img.png" or embed.thumbnail.url != "https://thumb.png":
        failures.append("embed image/author parsing wrong")
    compat = actions_module._make_embed({"title": "Т", "description": "Д", "footer": "старый футер"})
    if compat.footer.text != "старый футер":
        failures.append("compat footer broken")
    print("embed builder OK")

    # -- allowed mentions -----------------------------------------------------
    am = actions_module._allowed_mentions({})
    if am.everyone or am.users or am.roles:
        failures.append("default mentions must be none")
    if not actions_module._allowed_mentions({"allow_mentions": "all"}).everyone:
        failures.append("'all' mentions must allow everyone")
    selective = actions_module._allowed_mentions({"allow_mentions": "users,roles"})
    if selective.everyone or not selective.users or not selective.roles:
        failures.append("selective mentions parsing wrong")
    print("mentions OK")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "jobs.db"

        # -- event rules store ---------------------------------------------------
        rules_store = EventRules(str(db))
        rules_store.add("member_join", "welcome", "send_message", '{"channel_id": 1, "content": "hi {user.name}"}')
        rules_store.add("voice_join", "announce", "send_message", '{"channel_id": 1, "content": "x"}')
        rules_store.add("member_join", "welcome_dup", "send_embed", "{}")
        if {r[0] for r in rules_store.for_event("member_join")} != {"welcome", "welcome_dup"}:
            failures.append("event rules for_event wrong")
        if rules_store.remove("welcome", "voice_join") != 0:
            failures.append("removed a rule with wrong event type")
        if rules_store.remove("welcome", "member_join") != 1:
            failures.append("rule removal failed")
        if rules_store.remove("welcome_dup", None) != 1:
            failures.append("rule removal without event failed")
        rules_store.close()
        print("event rules OK")

        service = SchedulerService(str(db), "Europe/Kaliningrad")
        await service.start()

        await service.add_job(task_id="daily", action="send_message",
                              params={"channel_id": 1, "content": "hi"}, cron="0 12 * * *")
        await service.add_job(task_id="loop", action="send_message",
                              params={"channel_id": 1, "content": "hi"}, interval_minutes=5,
                              end="2026-12-31 23:59", jitter_seconds=60)
        await service.add_job(task_id="once", action="send_embed",
                              params={"channel_id": 1, "title": "t", "description": "d"},
                              at="2026-12-25 10:00")

        bad_sources = [("multitrigger", dict(cron="0 * * * *", interval_minutes=1)),
                       ("notrigger", dict()),
                       ("badcron", dict(cron="garbage")),
                       ("badend", dict(cron="0 * * * *", end="30-02-2026"))]
        for label, kwargs in bad_sources:
            try:
                await service.add_job(task_id=label, action="send_message",
                                      params={"channel_id": 1, "content": "x"}, **kwargs)
                failures.append(f"add_job '{label}' did not raise")
            except ValueError:
                pass

        names = {job.task_id for job in service.list_jobs()}
        if names != {"daily", "loop", "once"}:
            failures.append(f"unexpected job set after adds: {names}")
        print(f"add/list OK: {sorted(names)}")

        nexts = {job.task_id: job.next_run for job in service.list_jobs()}
        if nexts["once"] is None or nexts["daily"] is None:
            failures.append(f"next_run_time not computed: {nexts}")
        print("next runs:", {k: (v.strftime('%Y-%m-%d %H:%M') if v else None) for k, v in nexts.items()})

        interval_job = service.get_job("loop")
        raw_trigger = service._scheduler.get_job("loop").trigger
        if raw_trigger.end_date is None or raw_trigger.end_date.strftime("%Y-%m-%d") != "2026-12-31":
            failures.append("end_date was not applied to the interval trigger")
        if interval_job.meta:
            failures.append(f"unexpected meta on plain job: {interval_job.meta}")

        # Replacing a job must not duplicate it.
        await service.add_job(task_id="daily", action="send_message",
                              params={"channel_id": 2, "content": "hi"}, cron="30 9 * * *")
        if len([j for j in service.list_jobs() if j.task_id == "daily"]) != 1:
            failures.append("replace_existing created a duplicate job")

        service.pause_job("loop")
        paused = next(j for j in service.list_jobs() if j.task_id == "loop")
        if paused.next_run is not None:
            failures.append("paused job still reports a next run")
        service.resume_job("loop")
        resumed = next(j for j in service.list_jobs() if j.task_id == "loop")
        if resumed.next_run is None:
            failures.append("resumed job has no next run")
        print("pause/resume OK")

        # -- run history -------------------------------------------------------
        runlog = service.runlog
        runlog.record("daily", "send_message", "2026-01-01T10:00:00", "2026-01-01T10:00:05", "success", None)
        runlog.record("daily", "send_message", "2026-01-02T10:00:00", "2026-01-02T10:00:07", "error", "HTTPException: 404")
        runlog.record("other", "send_message", "2026-01-01T11:00:00", "2026-01-01T11:00:01", "success", None)
        successes, failures_count = runlog.stats("daily")
        if (successes, failures_count) != (1, 1):
            failures.append(f"runlog.stats gave wrong counts: {(successes, failures_count)}")
        recent = runlog.recent("daily", limit=5)
        if len(recent) != 2 or recent[0]["status"] != "error":
            failures.append(f"runlog.recent unexpected: {recent}")
        print("runlog OK")

        # -- max_runs auto-removal ---------------------------------------------
        await service.add_job(task_id="limited", action="send_message",
                              params={"channel_id": 1, "content": "hi"}, interval_minutes=1, max_runs=2,
                              cron=None)
        runlog.record("limited", "send_message", "2026-01-01T10:00:00", "2026-01-01T10:00:01", "success", None)
        runlog.record("limited", "send_message", "2026-01-01T10:05:00", "2026-01-01T10:05:01", "success", None)
        await service._handle_success("limited")
        if service.get_job("limited") is not None:
            failures.append("job was not removed after reaching max_runs")
        elif service.get_job("limited#retry1") is not None:
            failures.append("retry leftovers were not removed")
        print("max_runs OK")

        # -- chaining ------------------------------------------------------------
        await service.add_job(task_id="chained", action="send_message",
                              params={"channel_id": 1, "content": "later"}, interval_minutes=7)
        await service.add_job(task_id="starter", action="send_message",
                              params={"channel_id": 1, "content": "first"}, interval_minutes=10,
                              chain_task_id="chained", chain_delay_minutes=3)
        meta = service.get_job("starter").meta
        if meta.get("then") != {"task_id": "chained", "delay_minutes": 3}:
            failures.append(f"chain meta not stored: {meta}")
        await service._handle_success("starter")
        chained_once = service.get_job("chained#chain")
        if chained_once is None:
            failures.append("chained one-off job was not scheduled")
        if service.get_job("chained") is None:
            failures.append("chained job's own schedule disappeared")
        print("chaining OK")

        # -- retries ---------------------------------------------------------------
        job = service.get_job("daily")
        args = (job.task_id, job.action, {"channel_id": 1, "content": "hi"}, job.meta)
        for _ in range(3):
            service.handle_failure(args[0], args[1], args[2], args[3], RuntimeError("boom"))
        for suffix in ("#retry1", "#retry2", "#retry3"):
            if service.get_job(f"daily{suffix}") is None:
                failures.append(f"retry job {suffix} was not scheduled after failures")
        service.handle_failure(args[0], args[1], args[2], args[3], RuntimeError("boom"))
        if service.get_job("daily#retry4") is not None:
            failures.append("retries scheduled beyond the cap")
        print("retries OK (cap respected)")

        # -- member_update filter ------------------------------------------------
        from ds_scheduler.cogs.event_commands import EventCog
        before = SimpleNamespace(display_name="A", roles=("role_a",), status="online")
        after = SimpleNamespace(display_name="B", roles=("role_a",), status="online")
        if not EventCog._update_matches({"member_update_type": "any"}, before, after):
            failures.append("member_update 'any' filter failed")
        if not EventCog._update_matches({"member_update_type": "nickname"}, before, after):
            failures.append("member_update 'nickname' filter failed")
        if EventCog._update_matches({"member_update_type": "roles"}, before, after):
            failures.append("member_update 'roles' filter matched wrongly")
        print("member_update filter OK")

        # -- parse_datetime --------------------------------------------------------
        try:
            parse_datetime("tomorrow")
            failures.append("parse_datetime accepted garbage")
        except ValueError:
            pass
        if parse_datetime("2026-03-15 08:30").strftime("%d.%m %H:%M") != "15.03 08:30":
            failures.append("parse_datetime round-trip mismatch")

        # -- persistence: a fresh instance over the same DB must see every job ----
        await service.shutdown()
        service2 = SchedulerService(str(db), "Europe/Kaliningrad")
        await service2.start()
        persisted = {job.task_id: job.action for job in service2.list_jobs()}
        expected = {"daily": "send_message", "loop": "send_message", "once": "send_embed",
                    "chained": "send_message", "starter": "send_message",
                    "chained#chain": "send_message", "daily#retry1": "send_message",
                    "daily#retry2": "send_message", "daily#retry3": "send_message"}
        if persisted != expected:
            failures.append(f"jobs did not survive a restart: {persisted}")

        if not service2.remove_job("once") or service2.get_job("once") is not None:
            failures.append("remove_job failed")
        if service2.remove_job("missing"):
            failures.append("removing a missing job reported success")
        await service2.shutdown()
        print("persistence + remove OK")

    if failures:
        print("FAILURES:")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(main()))
