import asyncio

from radar.classify.service import Classifier
from radar.pipeline.notifier import Notifier
from radar.pipeline.scanner import Scanner
from radar.pipeline.scheduler import build_scheduler


async def test_scheduler_builds_and_runs_jobs(db, settings):
    scanner = Scanner(db, None, Classifier(None), settings)
    notifier = Notifier(object(), db, settings)  # type: ignore[arg-type]
    scheduler = build_scheduler(settings, scanner, notifier, db)
    scheduler.start()
    try:
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {"scan_tick", "digests", "retry_unclassified", "maintenance"}
        # запускаем тик вручную через планировщик: без VK-токена он не падает
        job = scheduler.get_job("scan_tick")
        job.modify(next_run_time=None)
        stats = await scanner.tick()
        assert stats is not None and stats.processed == 0
        await asyncio.sleep(0.05)
    finally:
        scheduler.shutdown(wait=False)
