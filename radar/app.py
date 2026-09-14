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

SHUTDOWN_GRACE_SECONDS = 30


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


def build_vk_client(settings: Settings) -> VkClient | None:
    if not settings.vk_enabled:
        log.warning("VK_ACCESS_TOKEN не задан — опрос сообществ отключён")
        return None
    return VkClient(
        settings.vk_access_token,
        version=settings.vk_api_version,
        rps=settings.vk_rps,
        api_base=settings.vk_api_base,
        use_execute=settings.vk_use_execute,
    )


async def _wait_idle(scanner: Scanner, notifier: Notifier, grace_seconds: float) -> None:
    """Дождаться, пока завершатся текущий тик и отправка, чтобы не оборвать доставку между send и записью."""
    try:
        async with asyncio.timeout(grace_seconds):
            await scanner.wait_idle()
            await notifier.wait_idle()
    except TimeoutError:
        log.warning("Фоновые задачи не завершились за %.0f с — останавливаем принудительно", grace_seconds)


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level)
    db = await initialize_database(settings.db_path, settings.registry_seed_path)
    vk = build_vk_client(settings)
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

    polling_error: BaseException | None = None
    polling: asyncio.Task[None] | None = None
    try:
        await setup_bot_commands(bot, settings)
        scheduler.start()
        log.info("Бот запущен. Окна сканирования: %s (%s)", settings.scan_windows, settings.timezone)
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        stopper = asyncio.create_task(stop_event.wait())
        await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if polling.done() and not polling.cancelled():
            polling_error = polling.exception()
    finally:
        if scheduler.running:
            scheduler.pause()  # новые задачи не стартуют, текущие дорабатывают
            await _wait_idle(scanner, notifier, SHUTDOWN_GRACE_SECONDS)
            scheduler.shutdown(wait=False)
        if polling is not None and not polling.done():
            try:
                await dp.stop_polling()
            except RuntimeError:  # polling уже завершился
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
    if polling_error is not None:
        log.error("Опрос Telegram завершился с ошибкой", exc_info=polling_error)
        raise polling_error
