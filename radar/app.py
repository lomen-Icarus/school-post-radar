"""Точка входа: сборка зависимостей и запуск бота с планировщиком."""

from __future__ import annotations

import asyncio
import logging
import signal

from radar.bot.app import create_bot, create_dispatcher, setup_bot_commands
from radar.classify.llm import ClaudeClassifier
from radar.classify.service import Classifier
from radar.config import Settings
from radar.db.bootstrap import initialize_database
from radar.pipeline.notifier import Notifier
from radar.pipeline.scanner import Scanner
from radar.pipeline.scheduler import build_scheduler
from radar.vk.client import VkClient

log = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def build_classifier(settings: Settings) -> Classifier:
    llm = None
    if settings.llm_enabled:
        llm = ClaudeClassifier(
            model=settings.claude_model,
            effort=settings.claude_effort,
            use_fallbacks=settings.claude_fallbacks,
            api_key=settings.anthropic_api_key,
        )
        log.info("Классификатор: Claude %s (effort=%s)", settings.claude_model, settings.claude_effort)
    else:
        log.warning("ANTHROPIC_API_KEY не задан — классификация только по ключевым словам")
    return Classifier(
        llm, concurrency=settings.classifier_concurrency, min_confidence=settings.min_confidence
    )


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level)
    db = await initialize_database(settings.db_path, settings.registry_seed_path)
    vk = (
        VkClient(
            settings.vk_access_token,
            version=settings.vk_api_version,
            rps=settings.vk_rps,
            api_base=settings.vk_api_base,
            use_execute=settings.vk_use_execute,
        )
        if settings.vk_enabled
        else None
    )
    if vk is None:
        log.warning("VK_ACCESS_TOKEN не задан — опрос сообществ отключён")
    classifier = build_classifier(settings)
    bot = create_bot(settings)
    notifier = Notifier(bot, db, settings)
    scanner = Scanner(db, vk, classifier, settings, on_new_candidates=notifier.deliver_instant)
    dp = create_dispatcher(settings, db, scanner=scanner, notifier=notifier, classifier=classifier)
    scheduler = build_scheduler(settings, scanner, notifier, db)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows
            pass

    await setup_bot_commands(bot, settings)
    scheduler.start()
    log.info("Бот запущен. Окна сканирования: %s (%s)", settings.scan_windows, settings.timezone)
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    try:
        await asyncio.wait(
            {polling, asyncio.create_task(stop_event.wait())}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        scheduler.shutdown(wait=False)
        try:
            await dp.stop_polling()
        except RuntimeError:  # polling уже завершился (например, из-за ошибки) — просто убираем задачу
            pass
        polling.cancel()
        try:
            await polling
        except (asyncio.CancelledError, Exception):
            pass
        if vk is not None:
            await vk.close()
        await bot.session.close()
        await db.close()
        log.info("Бот остановлен")
