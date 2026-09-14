from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from radar.bot import keyboards, texts
from radar.bot.callbacks import MenuCb
from radar.bot.handlers.common import edit_or_send, render_main
from radar.config import Settings
from radar.db import repo
from radar.db.connection import Database


def build_router() -> Router:
    """Фабрика роутера: диспетчер можно собирать многократно (тесты, перезапуск)."""
    router = Router(name="start")

    @router.message(CommandStart())
    async def cmd_start(
        message: Message,
        db: Database,
        subscriber: repo.Subscriber,
        subscriber_created: bool,
        state: FSMContext,
    ) -> None:
        await state.clear()
        first_name = message.from_user.first_name if message.from_user else None
        await render_main(
            db, message, subscriber, intro=texts.welcome(first_name, subscriber_created, subscriber)
        )

    @router.message(Command("settings", "menu"))
    async def cmd_settings(
        message: Message, db: Database, subscriber: repo.Subscriber, state: FSMContext
    ) -> None:
        await state.clear()
        await render_main(db, message, subscriber)

    async def _help(db: Database, settings: Settings) -> str:
        stats = await repo.community_stats(db, settings.monitor_scope)
        return texts.help_text(settings, stats["total"])

    @router.message(Command("help"))
    async def cmd_help(message: Message, db: Database, settings: Settings) -> None:
        await message.answer(await _help(db, settings), reply_markup=keyboards.help_menu())

    @router.callback_query(MenuCb.filter(F.section == "main"))
    async def cb_main(
        query: CallbackQuery, db: Database, subscriber: repo.Subscriber, state: FSMContext
    ) -> None:
        await state.clear()
        await render_main(db, query, subscriber)
        await query.answer()

    @router.callback_query(MenuCb.filter(F.section == "help"))
    async def cb_help(query: CallbackQuery, db: Database, settings: Settings) -> None:
        await edit_or_send(query, await _help(db, settings), keyboards.help_menu())
        await query.answer()

    @router.message(Command("pause"))
    async def cmd_pause(message: Message, db: Database, subscriber: repo.Subscriber) -> None:
        await repo.update_subscriber(db, subscriber.chat_id, is_active=0)
        sub = await repo.get_subscriber(db, subscriber.chat_id)
        await render_main(db, message, sub or subscriber, intro="⏸ Уведомления поставлены на паузу.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message, db: Database, subscriber: repo.Subscriber) -> None:
        await repo.update_subscriber(db, subscriber.chat_id, is_active=1)
        sub = await repo.get_subscriber(db, subscriber.chat_id)
        await render_main(db, message, sub or subscriber, intro="▶️ Уведомления возобновлены.")

    @router.callback_query(MenuCb.filter(F.section.in_({"pause", "resume"})))
    async def cb_pause_resume(
        query: CallbackQuery, callback_data: MenuCb, db: Database, subscriber: repo.Subscriber
    ) -> None:
        active = callback_data.section == "resume"
        await repo.update_subscriber(db, subscriber.chat_id, is_active=1 if active else 0)
        sub = await repo.get_subscriber(db, subscriber.chat_id) or subscriber
        await render_main(db, query, sub)
        await query.answer("Возобновлено" if active else "Пауза")

    return router
