from __future__ import annotations

import logging
import re
from html import escape

from aiogram import Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message

from radar.classify.service import Classifier
from radar.config import Settings
from radar.db import repo
from radar.db.connection import Database
from radar.pipeline.scanner import Scanner, TickStats
from radar.vk.client import VkClient
from radar.vk.models import parse_post

log = logging.getLogger(__name__)


class AdminFilter(BaseFilter):
    """Пропускает только администраторов из ADMIN_IDS.

    Обязательно наследник BaseFilter: обычный класс с async __call__ aiogram не считает
    awaitable и «выполняет» в потоке, получая объект корутины, который всегда истинен.
    """

    def __init__(self, settings: Settings) -> None:
        self.admins = set(settings.admin_ids)

    async def __call__(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.admins)


def build_router(settings: Settings) -> Router:
    """Фабрика роутера: диспетчер можно собирать многократно (тесты, перезапуск)."""
    router = Router(name="admin")
    router.message.filter(AdminFilter(settings))

    @router.message(Command("scan_now"))
    async def cmd_scan_now(message: Message, scanner: Scanner) -> None:
        if scanner.vk is None:
            await message.answer("⚠️ VK_ACCESS_TOKEN не задан — сканирование невозможно.")
            return
        status = await message.answer("🔄 Запускаю полный обход…")

        async def progress(done: int, total: int, stats: TickStats) -> None:
            try:
                await status.edit_text(
                    f"🔄 Обход: {done}/{total} · постов {stats.posts_fetched} · новых {stats.posts_new} · "
                    f"достижений {stats.achievements} · ошибок {stats.errors}"
                )
            except Exception:  # слишком частые правки — не критично
                pass

        try:
            stats = await scanner.full_scan(progress)
        except Exception as exc:
            log.exception("scan_now failed")
            await status.edit_text(f"❌ Ошибка: {escape(str(exc))[:500]}")
            return
        await status.edit_text(
            f"✅ Готово: сообществ {stats.processed} · постов {stats.posts_fetched} · новых {stats.posts_new} · "
            f"классифицировано {stats.classified} · достижений {stats.achievements} · ошибок {stats.errors}"
        )

    @router.message(Command("resolve"))
    async def cmd_resolve(message: Message, scanner: Scanner, db: Database, settings: Settings) -> None:
        n = await scanner.resolve_pending(limit=500)
        stats = await repo.community_stats(db, settings.monitor_scope)
        await message.answer(
            f"🔎 Разрешено адресов: {n}. Осталось без id: {stats['unresolved']}, не найдено: {stats['failed']}, "
            f"дубликатов: {stats['duplicates']}."
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message, db: Database, settings: Settings) -> None:
        subs = await repo.count_subscribers(db)
        cstats = await repo.community_stats(db, settings.monitor_scope)
        posts = await db.scalar("SELECT COUNT(*) FROM posts")
        classified = await db.scalar("SELECT COUNT(*) FROM posts WHERE classified_at IS NOT NULL")
        llm_calls = await db.scalar("SELECT COUNT(*) FROM posts WHERE classifier_version LIKE 'llm-%'")
        cands = await db.scalar("SELECT COUNT(*) FROM achievement_candidates")
        rejected = await db.scalar(
            "SELECT COUNT(*) FROM achievement_candidates WHERE review_status = 'rejected'"
        )
        done = await db.scalar("SELECT COUNT(*) FROM achievement_candidates WHERE review_status = 'done'")
        deliveries = await db.scalar("SELECT COUNT(*) FROM deliveries")
        errors = await db.fetchall(
            "SELECT c.canonical_url, cur.last_error, cur.consecutive_errors FROM community_cursors cur "
            "JOIN communities c USING(community_key) WHERE cur.last_error IS NOT NULL "
            "ORDER BY cur.consecutive_errors DESC, cur.last_checked_at DESC LIMIT 8"
        )
        lines = [
            "<b>📈 Статистика</b>",
            f"Подписчиков: {subs.get('total', 0)} (активных {subs.get('active', 0)}, режим «сразу» {subs.get('instant', 0)})",
            f"Сообществ: {cstats['total']} · без id {cstats['unresolved']} · не найдено {cstats['failed']} · дубликатов {cstats['duplicates']}",
            f"Постов сохранено: {posts} · классифицировано {classified} · через модель {llm_calls}",
            f"Кандидатов: {cands} · отклонено {rejected} · поздравлено {done} · доставок {deliveries}",
        ]
        if errors:
            lines.append("")
            lines.append("<b>Ошибки опроса</b>")
            for e in errors:
                lines.append(
                    f"• {escape(e['canonical_url'])} — {escape((e['last_error'] or '')[:80])} (×{e['consecutive_errors']})"
                )
        await message.answer("\n".join(lines))

    _WALL_RE = re.compile(r"wall(-?\d+)_(\d+)")

    @router.message(Command("check"))
    async def cmd_check(message: Message, classifier: Classifier, scanner: Scanner) -> None:
        """Проверить классификатор на тексте или ссылке на пост: /check <текст | https://vk.com/wall-1_2>."""
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Использование: <code>/check https://vk.com/wall-123_456</code> или <code>/check текст поста</code>"
            )
            return
        arg = parts[1].strip()
        text = arg
        schools: list[str] = []
        match = _WALL_RE.search(arg)
        if match:
            vk: VkClient | None = scanner.vk
            if vk is None:
                await message.answer("⚠️ VK_ACCESS_TOKEN не задан.")
                return
            owner_id, post_id = int(match.group(1)), int(match.group(2))
            try:
                data = await vk.call("wall.getById", posts=f"{owner_id}_{post_id}")
            except Exception as exc:
                await message.answer(f"❌ VK: {escape(str(exc))}")
                return
            items = data.get("items") if isinstance(data, dict) else data
            if not items:
                await message.answer("Пост не найден или недоступен.")
                return
            text = parse_post(items[0]).text
        try:
            result = await classifier.classify(text, schools, None, force_llm=True)
        except Exception as exc:
            await message.answer(f"❌ Классификатор: {escape(str(exc))[:500]}")
            return
        verdict = "✅ достижение" if result.is_achievement else "❌ не достижение"
        await message.answer(
            f"{verdict} · уверенность {round(result.confidence * 100)}% · {escape(result.kind)}\n"
            f"<b>{escape(result.headline)}</b>\n{escape(result.summary)}\n"
            f"👤 {escape(', '.join(result.students) or '—')}\n"
            f"<i>{escape(result.reason)}</i>\n"
            f"Ключевые слова: {escape(', '.join(result.keyword_hits) or '—')} · {escape(result.classifier_version)}"
        )

    return router
