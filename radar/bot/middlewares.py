from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from radar.config import Settings
from radar.db import repo
from radar.db.connection import Database

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Белый список пользователей (если ALLOWED_USER_IDS задан). Администраторы проходят всегда."""

    def __init__(self, settings: Settings) -> None:
        self.allowed = set(settings.allowed_user_ids) | set(settings.admin_ids)
        self.restricted = bool(settings.allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self.restricted:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed:
            # Отвечаем только на команды в личке; в группах и на обычный текст молчим, чтобы не спамить.
            if isinstance(event, Message):
                if event.chat.type == "private" and (event.text or "").startswith("/"):
                    await event.answer("⛔️ Доступ к боту ограничен. Обратитесь к администратору.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен", show_alert=True)
            return None
        return await handler(event, data)


class SubscriberMiddleware(BaseMiddleware):
    """Создаёт/обновляет подписчика для каждого входящего события и кладёт его в data['subscriber']."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or chat is None or chat.type not in ("private", "group", "supergroup"):
            return None  # каналы и прочее не обслуживаем
        # В группе подписчиком становится сам чат (отрицательный id), подписью служит его название.
        username = user.username if chat.type == "private" else None
        display_name = user.first_name if chat.type == "private" else (chat.title or str(chat.id))
        sub, created = await repo.upsert_subscriber(
            self.db,
            chat.id,
            username,
            display_name,
            self.settings.default_digest_times,
            self.settings.timezone,
        )
        if created:
            from radar.pipeline.schedule import next_digest_after_settings_change
            from radar.utils.timeutil import now_utc, to_iso

            nxt = next_digest_after_settings_change(
                now_utc(), sub.digest_times, sub.interval_days, sub.timezone
            )
            await repo.update_subscriber(self.db, sub.chat_id, next_digest_at=to_iso(nxt))
            sub = await repo.get_subscriber(self.db, sub.chat_id) or sub
        data["subscriber"] = sub
        data["subscriber_created"] = created
        return await handler(event, data)
