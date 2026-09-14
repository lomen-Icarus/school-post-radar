from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from radar.bot import keyboards
from radar.bot.callbacks import MenuCb
from radar.bot.handlers.common import edit_or_send
from radar.config import Settings
from radar.db import repo
from radar.db.connection import Database
from radar.pipeline import schedule
from radar.pipeline.notifier import Notifier
from radar.pipeline.scanner import Scanner
from radar.utils.timeutil import from_iso, humanize_local, now_utc, tz


def build_router() -> Router:
    """Фабрика роутера: диспетчер можно собирать многократно (тесты, перезапуск)."""
    router = Router(name="status")

    async def build_status(db: Database, settings: Settings, scanner: Scanner, sub: repo.Subscriber) -> str:
        stats = await repo.community_stats(db, settings.monitor_scope)
        runs = await repo.last_runs(db, 2)
        since = now_utc() - timedelta(hours=settings.lookback_hours)
        found = await repo.count_candidates_since(db, since, settings.min_confidence)
        pending = await repo.list_pending_for_chat(db, sub.chat_id, since, settings.min_confidence, limit=500)
        enabled = await repo.enabled_region_ids(db, sub.profile_id)
        pending_for_me = [
            c for c in pending if not c.municipality_ids or any(m in enabled for m in c.municipality_ids)
        ]

        lines = ["<b>📊 Статус</b>", ""]
        active = schedule.active_window(now_utc(), scanner.windows, tz(settings.timezone))
        if active:
            w, _start, _end = active
            lines.append(f"🟢 Идёт сканирование (окно {w.start:%H:%M}–{w.end:%H:%M})")
        else:
            nxt = schedule.next_window_start(now_utc(), scanner.windows, tz(settings.timezone))
            lines.append(f"⚪️ Сканирование ждёт окна · следующее: {humanize_local(nxt, sub.timezone)}")
        if runs:
            r = runs[0]
            state = {"running": "выполняется", "done": "завершён", "aborted": "прерван"}.get(
                r.status, r.status
            )
            lines.append(
                f"Последний запуск: {humanize_local(from_iso(r.started_at), sub.timezone)} · {state} · "
                f"{r.next_index}/{r.total} сообществ · новых постов {r.posts_new} · достижений {r.achievements_found}"
                + (f" · ошибок {r.errors}" if r.errors else "")
            )
        lines.append("")
        lines.append(
            f"Сообществ в мониторинге: {stats['total']} · без числового id: {stats['unresolved']}"
            + (f" · дубликатов: {stats['duplicates']}" if stats["duplicates"] else "")
            + (f" · с ошибками: {stats['erroring']}" if stats["erroring"] else "")
        )
        lines.append(
            f"Достижений за {settings.lookback_hours} ч: {found} · ещё не отправлено вам: {len(pending_for_me)}"
        )
        lines.append("")
        if sub.mode == "digest":
            lines.append(
                f"Ваш следующий дайджест: {humanize_local(from_iso(sub.next_digest_at), sub.timezone)}"
            )
        else:
            lines.append("Режим: ⚡ сразу")
        llm = "Claude" if scanner.classifier.llm_enabled else "только ключевые слова"
        lines.append(f"Классификатор: {escape(llm)}")
        if scanner.last_error:
            lines.append(f"⚠️ Последняя ошибка: {escape(scanner.last_error[:200])}")
        return "\n".join(lines)

    @router.message(Command("status"))
    async def cmd_status(
        message: Message, db: Database, settings: Settings, scanner: Scanner, subscriber: repo.Subscriber
    ) -> None:
        await message.answer(
            await build_status(db, settings, scanner, subscriber), reply_markup=keyboards.status_menu()
        )

    @router.callback_query(MenuCb.filter(F.section == "status"))
    async def cb_status(
        query: CallbackQuery, db: Database, settings: Settings, scanner: Scanner, subscriber: repo.Subscriber
    ) -> None:
        await edit_or_send(
            query, await build_status(db, settings, scanner, subscriber), keyboards.status_menu()
        )
        await query.answer()

    @router.message(Command("digest"))
    async def cmd_digest(message: Message, subscriber: repo.Subscriber, notifier: Notifier) -> None:
        """Прислать всё накопленное прямо сейчас (не сдвигает расписание)."""
        await notifier.deliver_digest(subscriber, manual=True)

    return router
