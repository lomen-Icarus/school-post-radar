"""Общие вспомогательные функции обработчиков."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from radar.bot import keyboards, texts
from radar.db import repo
from radar.db.connection import Database

log = logging.getLogger(__name__)


async def edit_or_send(
    event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None
) -> None:
    """Редактировать сообщение с меню (для колбэков) или отправить новое (для команд).

    Если сообщение с меню недоступно (слишком старое или удалено), отправляем новое — иначе
    нажатие кнопки выглядело бы как «ничего не произошло».
    """
    if isinstance(event, CallbackQuery):
        message = event.message
        if isinstance(message, Message):
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
                log.debug("edit_text не удался (%s), отправляем новое", exc)
            await message.answer(text, reply_markup=markup)
            return
        chat_id = message.chat.id if message is not None else event.from_user.id
        await event.bot.send_message(chat_id, text, reply_markup=markup)
        return
    await event.answer(text, reply_markup=markup)


async def render_main(
    db: Database, event: Message | CallbackQuery, sub: repo.Subscriber, intro: str | None = None
) -> None:
    regions = await repo.list_regions(db, sub.profile_id)
    enabled = sum(1 for r in regions if r.enabled)
    body = texts.settings_overview(sub, enabled, len(regions))
    text = f"{intro}\n\n{body}" if intro else body
    await edit_or_send(event, text, keyboards.main_menu(sub, enabled, len(regions)))
