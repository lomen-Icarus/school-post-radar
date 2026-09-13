"""Планировщик фоновых задач (APScheduler 3.x, asyncio)."""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from radar.config import Settings
from radar.db import repo
from radar.db.connection import Database
from radar.pipeline.notifier import Notifier
from radar.pipeline.scanner import Scanner
from radar.utils.timeutil import now_utc

log = logging.getLogger(__name__)


def build_scheduler(
    settings: Settings, scanner: Scanner, notifier: Notifier, db: Database
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        scanner.tick,
        IntervalTrigger(seconds=settings.scan_tick_seconds),
        id="scan_tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=settings.scan_tick_seconds,
        next_run_time=now_utc() + timedelta(seconds=5),
    )
    scheduler.add_job(
        notifier.dispatch_due_digests,
        IntervalTrigger(seconds=60),
        id="digests",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        scanner.retry_unclassified,
        IntervalTrigger(minutes=15),
        id="retry_unclassified",
        max_instances=1,
        coalesce=True,
    )

    async def maintenance() -> None:
        removed = await repo.purge_old_posts(db, now_utc() - timedelta(days=30))
        if removed:
            log.info("Удалено старых постов: %d", removed)

    scheduler.add_job(maintenance, CronTrigger(hour=3, minute=30), id="maintenance", max_instances=1)
    return scheduler
