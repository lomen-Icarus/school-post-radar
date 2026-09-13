"""Доставка карточек и дайджестов подписчикам."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from radar.bot import formatting
from radar.bot.keyboards import card_keyboard
from radar.config import Settings
from radar.db import repo
from radar.db.connection import Database
from radar.pipeline.schedule import next_digest_after_send
from radar.utils.timeutil import humanize_local, now_utc, to_iso

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------ helpers
    async def _send(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        """Отправить сообщение с обработкой лимитов Telegram. None — если чат недоступен."""
        for attempt in range(3):
            try:
                msg = await self.bot.send_message(
                    chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True), **kwargs
                )
                await asyncio.sleep(0.05)
                return msg.message_id
            except TelegramRetryAfter as exc:
                log.warning("Telegram: flood control, ждём %sс", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 0.5)
            except TelegramForbiddenError:
                log.info("Чат %s заблокировал бота — ставим на паузу", chat_id)
                await repo.update_subscriber(self.db, chat_id, is_active=0)
                return None
            except TelegramBadRequest as exc:
                if "chat not found" in str(exc).lower():
                    await repo.update_subscriber(self.db, chat_id, is_active=0)
                    return None
                log.error("Telegram: не удалось отправить в %s: %s (попытка %d)", chat_id, exc, attempt + 1)
                if attempt == 2:
                    return None
                await asyncio.sleep(1.0)
        return None

    def _filter_regions(self, items: list[repo.Candidate], enabled: set[str]) -> list[repo.Candidate]:
        out: list[repo.Candidate] = []
        for c in items:
            if not c.municipality_ids:
                out.append(c)  # территория неизвестна — показываем всем
            elif any(m in enabled for m in c.municipality_ids):
                out.append(c)
        return out

    def _window_hours(self, sub: repo.Subscriber) -> int:
        """Глубина выборки: не меньше LOOKBACK_HOURS, а для редких дайджестов — весь интервал с запасом."""
        if sub.mode == "digest" and sub.interval_days > 1:
            return max(self.settings.lookback_hours, sub.interval_days * 24 + 12)
        return self.settings.lookback_hours

    async def _pending_for(self, sub: repo.Subscriber) -> list[repo.Candidate]:
        since = now_utc() - timedelta(hours=self._window_hours(sub))
        items = await repo.list_pending_for_chat(self.db, sub.chat_id, since, self.settings.min_confidence)
        enabled = await repo.enabled_region_ids(self.db, sub.profile_id)
        return self._filter_regions(items, enabled)

    # ------------------------------------------------------------ instant
    async def deliver_instant(self, candidate_ids: list[int] | None = None) -> int:
        """Отправить подписчикам в режиме «сразу» всё ещё не доставленное (pull-модель: сбой не теряет посты)."""
        subs = await repo.list_active_subscribers(self.db, mode="instant")
        if not subs:
            return 0
        sent = 0
        async with self._send_lock:
            for sub in subs:
                for c in await self._pending_for(sub):
                    mid = await self._send(
                        sub.chat_id,
                        formatting.format_card(c, sub.timezone),
                        reply_markup=card_keyboard(c.candidate_id, c.url),
                    )
                    if mid is None:
                        break
                    await repo.record_delivery(self.db, sub.chat_id, c.owner_id, c.post_id, "instant", mid)
                    await self.db.commit()
                    sent += 1
        return sent

    # ------------------------------------------------------------- digest
    async def deliver_digest(self, sub: repo.Subscriber, *, manual: bool = False) -> int:
        """Собрать и отправить дайджест подписчику. Возвращает число достижений в нём.

        Расписание сдвигается всегда (кроме ручного вызова), даже если отправка не удалась:
        недоставленные посты останутся в очереди и уйдут в следующий раз.
        """
        try:
            return await self._deliver_digest_inner(sub, manual=manual)
        finally:
            if not manual:
                sent_at = now_utc()
                nxt = next_digest_after_send(sent_at, sub.digest_times, sub.interval_days, sub.timezone)
                await repo.update_subscriber(
                    self.db, sub.chat_id, last_digest_at=to_iso(sent_at), next_digest_at=to_iso(nxt)
                )

    async def _deliver_digest_inner(self, sub: repo.Subscriber, *, manual: bool) -> int:
        channel = "manual" if manual else "digest"
        async with self._send_lock:
            # Выборка под замком: ручной /digest и плановый дайджест не отправят одно и то же дважды.
            items = await self._pending_for(sub)
            if not items:
                if sub.notify_empty or manual:
                    await self._send(
                        sub.chat_id,
                        f"🏆 Новых достижений за последние {self._window_hours(sub)} ч не найдено.",
                    )
                return 0
            when = humanize_local(now_utc(), sub.timezone)
            header = formatting.digest_header(len(items), sub.timezone, when)
            delivered = 0
            if sub.digest_format == "list":
                for text, chunk_items in formatting.format_digest_list(items, sub.timezone, header):
                    if await self._send(sub.chat_id, text) is None:
                        break
                    for c in chunk_items:
                        await repo.record_delivery(self.db, sub.chat_id, c.owner_id, c.post_id, channel, None)
                    await self.db.commit()
                    delivered += len(chunk_items)
            else:
                if await self._send(sub.chat_id, header) is None:
                    return 0
                for c in items:
                    mid = await self._send(
                        sub.chat_id,
                        formatting.format_card(c, sub.timezone),
                        reply_markup=card_keyboard(c.candidate_id, c.url),
                    )
                    if mid is None:
                        break
                    await repo.record_delivery(self.db, sub.chat_id, c.owner_id, c.post_id, channel, mid)
                    delivered += 1
            await self.db.commit()
        return delivered

    async def dispatch_due_digests(self) -> int:
        """Вызывается планировщиком раз в минуту: отправить дайджесты, чьё время пришло."""
        due = await repo.list_due_digests(self.db, now_utc())
        total = 0
        for sub in due:
            try:
                total += await self.deliver_digest(sub)
            except Exception:  # один подписчик не должен ломать рассылку остальным
                log.exception("Ошибка отправки дайджеста %s", sub.chat_id)
        return total
