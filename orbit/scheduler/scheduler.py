"""Proactive side of Orbit: reminders, cron jobs (briefing) and watcher polling."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from tzlocal import get_localzone

from orbit.scheduler.jobs.briefing import run_briefing
from orbit.scheduler.watchers.base import WATCHER_TYPES, Watcher

if TYPE_CHECKING:
    from orbit.app import OrbitApp


class OrbitScheduler:
    def __init__(self, orbit: OrbitApp):
        self.orbit = orbit
        self.sched = AsyncIOScheduler(timezone=get_localzone())
        self.watchers: list[Watcher] = []

    async def start(self) -> None:
        for r in await self.orbit.db.list_reminders("pending"):
            self.schedule_reminder(int(r["id"]), datetime.fromisoformat(r["due_at"]))

        cfg = self.orbit.config.jobs.briefing
        if cfg.enabled:
            self.sched.add_job(
                self._run_briefing,
                CronTrigger.from_crontab(cfg.cron, timezone=get_localzone()),
                id="briefing",
                replace_existing=True,
                misfire_grace_time=600,
            )

        for wcfg in self.orbit.config.watchers:
            self._register_watcher(dict(wcfg))

        self.sched.start()
        logger.info("scheduler started: {} job(s)", len(self.sched.get_jobs()))

    async def stop(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)
        for watcher in self.watchers:
            try:
                await watcher.close()
            except Exception:
                logger.exception("error closing watcher {}", watcher.name)

    # ---- reminders ---------------------------------------------------------
    def schedule_reminder(self, reminder_id: int, due_at: datetime) -> None:
        self.sched.add_job(
            self._fire_reminder,
            DateTrigger(run_date=due_at, timezone=get_localzone()),
            args=[reminder_id],
            id=f"reminder-{reminder_id}",
            replace_existing=True,
            misfire_grace_time=None,  # fire even if we were offline when it was due
        )

    def cancel_reminder(self, reminder_id: int) -> None:
        job = self.sched.get_job(f"reminder-{reminder_id}")
        if job:
            job.remove()

    async def _fire_reminder(self, reminder_id: int) -> None:
        r = await self.orbit.db.get_reminder(reminder_id)
        if not r or r["status"] != "pending":
            return
        ok = await self.orbit.push(f"⏰ 提醒：{r['text']}", channel=r["channel"], chat_id=r["chat_id"])
        await self.orbit.db.set_reminder_status(reminder_id, "done" if ok else "undelivered")

    # ---- cron jobs ---------------------------------------------------------
    async def _run_briefing(self) -> None:
        try:
            await run_briefing(self.orbit)
        except Exception:
            logger.exception("briefing failed")

    # ---- watchers ----------------------------------------------------------
    def _register_watcher(self, cfg: dict[str, Any]) -> None:
        wtype = cfg.pop("type", None)
        cls = WATCHER_TYPES.get(wtype)
        if cls is None:
            logger.warning("unknown watcher type '{}' - skipped", wtype)
            return
        minutes = int(cfg.pop("interval_minutes", 5))
        # Optional: run the first check this many seconds after startup instead of one full interval later.
        first_delay = cfg.pop("first_run_delay_seconds", None)
        try:
            watcher = cls(**cfg)
            watcher.bind(self.orbit)
        except Exception:
            logger.exception("watcher '{}' could not be set up - skipped", wtype)
            return
        self.watchers.append(watcher)
        extra: dict[str, Any] = {}
        if first_delay is not None:
            extra["next_run_time"] = datetime.now(get_localzone()) + timedelta(seconds=float(first_delay))
        self.sched.add_job(
            self._poll_watcher,
            IntervalTrigger(minutes=minutes),
            args=[watcher],
            id=f"watcher-{watcher.name}",
            replace_existing=True,
            # Never two checks of one watcher at once: a run still going when the next is due is skipped,
            # and runs missed while the loop was busy collapse into one.
            max_instances=1,
            coalesce=True,
            **extra,
        )

    async def _poll_watcher(self, watcher: Watcher) -> None:
        try:
            events = await watcher.check(self.orbit.db)
        except Exception:
            logger.exception("watcher {} failed", watcher.name)
            return
        on_pushed = getattr(watcher, "on_pushed", None)  # optional hook; duck-typed watchers may lack it
        for ev in events:
            ok = await self.orbit.push(f"[{watcher.name}] {ev.title}\n{ev.body}".strip())
            if on_pushed is None:
                continue
            try:
                await on_pushed(ev, ok)
            except Exception:
                logger.exception("watcher {} on_pushed failed", watcher.name)

    # ---- introspection -----------------------------------------------------
    def jobs(self) -> list[dict[str, Any]]:
        return [
            {"id": j.id, "next_run": j.next_run_time.isoformat(timespec="minutes") if j.next_run_time else None}
            for j in self.sched.get_jobs()
        ]
