"""Сборка Telegram-бота: диспетчер, роутеры, middleware, команды."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

from radar.bot.handlers import admin, feedback, regions, start, status
from radar.bot.handlers import settings as settings_handlers
from radar.bot.middlewares import AccessMiddleware, SubscriberMiddleware
from radar.config import Settings
from radar.db.connection import Database

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="settings", description="Настройки уведомлений"),
    BotCommand(command="regions", description="Территории"),
    BotCommand(command="status", description="Что происходит сейчас"),
    BotCommand(command="digest", description="Прислать накопленное сейчас"),
    BotCommand(command="pause", description="Пауза уведомлений"),
    BotCommand(command="resume", description="Возобновить уведомления"),
    BotCommand(command="help", description="Помощь"),
]
ADMIN_COMMANDS = [
    *USER_COMMANDS,
    BotCommand(command="scan_now", description="Полный обход сообществ сейчас"),
    BotCommand(command="resolve", description="Разрешить короткие адреса ВК"),
    BotCommand(command="stats", description="Статистика бота"),
    BotCommand(command="check", description="Проверить классификатор на посте"),
]


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher(settings: Settings, db: Database, **deps: object) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["db"] = db
    for name, dep in deps.items():
        dp[name] = dep
    access = AccessMiddleware(settings)
    subscriber_mw = SubscriberMiddleware(db, settings)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.message.middleware(subscriber_mw)
    dp.callback_query.middleware(subscriber_mw)
    dp.include_router(start.build_router())
    dp.include_router(settings_handlers.build_router())
    dp.include_router(regions.build_router())
    dp.include_router(status.build_router())
    dp.include_router(feedback.build_router())
    dp.include_router(admin.build_router(settings))
    return dp


async def setup_bot_commands(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(USER_COMMANDS)
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:  # админ мог ещё не начать диалог с ботом
            log.debug("Не удалось задать команды для %s: %s", admin_id, exc)
    try:
        await bot.set_my_short_description("Достижения учеников школ Чувашии из ВК — в Telegram")
        await bot.set_my_description(
            "Радар школьных достижений: дважды в день просматривает ленты ВК школ Чувашии и присылает посты, "
            "где ученики победили, стали призёрами или поступили — чтобы факультет успел поздравить их."
        )
    except Exception as exc:
        log.debug("Не удалось задать описание бота: %s", exc)
