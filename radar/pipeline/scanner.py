"""Сканирование сообществ: разрешение адресов, выборка постов, классификация, создание кандидатов."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from radar.classify.service import ClassificationResult, Classifier
from radar.config import Settings
from radar.db import repo
from radar.db.connection import Database
from radar.pipeline import schedule
from radar.registry.municipalities import BY_ID
from radar.utils.timeutil import from_iso, now_utc, to_iso
from radar.vk.client import VkApiError, VkClient
from radar.vk.models import VkPost

log = logging.getLogger(__name__)

# Порог, после которого сообщество с постоянной ошибкой (закрыто/удалено) отключается от опроса.
MAX_PERMANENT_ERRORS = 3
# Отложенные, предложенные и рекламные записи не нужны; репосты приходят как post_type='post' с copy_history.
SKIPPED_POST_TYPES = {"postpone", "suggest", "reply", "post_ads"}


@dataclass
class TickStats:
    processed: int = 0
    posts_fetched: int = 0
    posts_new: int = 0
    classified: int = 0
    achievements: int = 0
    errors: int = 0
    new_candidate_ids: list[int] = field(default_factory=list)


NewCandidatesHook = Callable[[list[int]], Awaitable[None]]


class Scanner:
    def __init__(
        self,
        db: Database,
        vk: VkClient | None,
        classifier: Classifier,
        settings: Settings,
        on_new_candidates: NewCandidatesHook | None = None,
    ) -> None:
        self.db = db
        self.vk = vk
        self.classifier = classifier
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.windows = schedule.parse_scan_windows(settings.scan_windows)
        self._on_new_candidates = on_new_candidates
        self._lock = asyncio.Lock()
        self.last_tick_at = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ tick
    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def wait_idle(self) -> None:
        async with self._lock:
            pass

    async def tick(self) -> TickStats | None:
        """Один шаг планировщика: обработать долю сообществ, положенную к текущему моменту окна."""
        if self._lock.locked():
            log.info("Предыдущий тик ещё выполняется — пропуск")
            return None
        async with self._lock:
            self.last_tick_at = now_utc()
            try:
                return await self._tick_inner()
            except Exception as exc:  # тик не должен ронять планировщик
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Ошибка тика сканирования")
                return None

    async def _tick_inner(self) -> TickStats:
        if self.vk is None:
            log.warning("VK-токен не задан — сканирование отключено")
            return TickStats()
        now = now_utc()
        stats = TickStats()
        active = schedule.active_window(now, self.windows, self.tz)
        run = None
        if active is not None:
            run = await repo.get_run_by_key(self.db, schedule.run_key_for(active[0], active[1]))
        if run is None:
            # Разрешаем адреса только между запусками, чтобы порядок сообществ внутри окна не менялся.
            await self.resolve_pending(limit=100)
        if active is not None:
            window, start, end = active
            run_key = schedule.run_key_for(window, start)
            targets = await repo.list_scan_targets(self.db, self.settings.monitor_scope)
            if run is None:
                run = await repo.create_run(
                    self.db,
                    run_key=run_key,
                    kind="window",
                    window_start=start,
                    window_end=end,
                    total=len(targets),
                )
                log.info("Начато окно сканирования %s: %d сообществ", run_key, len(targets))
            if run.status == "running":
                target = schedule.target_index(len(targets), start, end, now)
                target = min(target, len(targets))
                if target > run.next_index:
                    await self._process_slice(run, targets, run.next_index, target, stats)
                elif run.next_index >= len(targets) and now >= end - timedelta(seconds=1):
                    await repo.finish_run(self.db, run.run_id)
        # Догоняем незавершённые запуски (бот был выключен, окно закончилось).
        for run in await repo.list_unfinished_runs(self.db):
            if run.status != "running":
                continue
            run_end = schedule_run_end(run)
            if active is not None and run.run_key == schedule.run_key_for(active[0], active[1]):
                continue
            targets = await repo.list_scan_targets(self.db, self.settings.monitor_scope)
            total = min(run.total, len(targets))
            if run.next_index >= total or now_utc() < run_end:
                if run.next_index >= total:
                    await repo.finish_run(self.db, run.run_id)
                continue
            upto = min(total, run.next_index + self.settings.catchup_max_per_tick)
            log.info(
                "Догоняем запуск %s: %d..%d из %d", run.run_key or run.run_id, run.next_index, upto, total
            )
            await self._process_slice(run, targets, run.next_index, upto, stats)
            if upto >= total:
                await repo.finish_run(self.db, run.run_id)
        # Доставка «сразу» вызывается на каждом тике: она сама находит всё недоставленное,
        # поэтому сбой отправки на прошлом тике не теряет карточки.
        await self._notify_new(stats.new_candidate_ids)
        return stats

    async def _notify_new(self, candidate_ids: list[int]) -> None:
        if self._on_new_candidates is None:
            return
        try:
            await self._on_new_candidates(candidate_ids)
        except (
            Exception
        ):  # сбой доставки не должен ломать сканирование; недоставленное уйдёт на следующем тике
            log.exception("Ошибка мгновенной доставки")

    async def full_scan(
        self, progress: Callable[[int, int, TickStats], Awaitable[None]] | None = None
    ) -> TickStats:
        """Немедленный полный проход по всем сообществам (команда администратора)."""
        if self.vk is None:
            raise RuntimeError("VK-токен не задан")
        async with self._lock:
            await self.resolve_pending(limit=500)
            targets = await repo.list_scan_targets(self.db, self.settings.monitor_scope)
            now = now_utc()
            run = await repo.create_run(
                self.db, run_key=None, kind="manual", window_start=now, window_end=now, total=len(targets)
            )
            stats = TickStats()
            step = max(1, self.settings.vk_execute_batch)
            for start in range(0, len(targets), step):
                await self._process_slice(run, targets, start, min(len(targets), start + step), stats)
                if progress is not None:
                    await progress(min(len(targets), start + step), len(targets), stats)
            await repo.finish_run(self.db, run.run_id)
            if stats.new_candidate_ids:
                await self._notify_new(stats.new_candidate_ids)
            return stats

    # ------------------------------------------------------------ resolving
    async def resolve_pending(self, limit: int = 100) -> int:
        """Разрешить короткие адреса сообществ в числовые id. Возвращает число разрешённых."""
        if self.vk is None:
            return 0
        pending = await repo.list_unresolved_communities(self.db, limit)
        if not pending:
            return 0
        resolved = 0
        by_request: dict[str, str] = {}
        for row in pending:
            ident = _identifier_for_resolve(row)
            if ident:
                by_request[ident] = row["community_key"]
        for start in range(0, len(by_request), 100):
            chunk = list(by_request)[start : start + 100]
            errored: set[str] = set()
            try:
                infos = await self.vk.groups_get_by_id(chunk)
            except VkApiError as exc:
                if exc.is_auth:
                    log.error("VK: ошибка авторизации при groups.getById: %s", exc)
                    return resolved
                if exc.code != 100:
                    # Временная ошибка (сеть, лимиты) — оставляем адреса в очереди, попробуем на следующем тике.
                    log.warning("groups.getById не удался (%s), повторим позже", exc)
                    continue
                infos = []
                # Один из адресов невалиден (ошибка 100) — разбираем пакет по одному.
                for ident in chunk:
                    try:
                        infos.extend(await self.vk.groups_get_by_id([ident]))
                    except VkApiError as single_exc:
                        errored.add(ident)
                        await repo.mark_community_resolve_failed(
                            self.db, by_request[ident], str(single_exc), final=single_exc.code == 100
                        )
            found = {i.requested: i for i in infos}
            for ident in chunk:
                if ident in errored:
                    continue
                key = by_request[ident]
                info = found.get(ident)
                if info is None:
                    await repo.mark_community_resolve_failed(
                        self.db, key, "сообщество не найдено", final=True
                    )
                    continue
                if info.deactivated:
                    await repo.mark_community_resolve_failed(
                        self.db, key, f"deactivated:{info.deactivated}", final=True
                    )
                    continue
                existing = await repo.community_key_by_group_id(self.db, info.group_id)
                duplicate_of = existing if existing and existing != key else None
                await repo.mark_community_resolved(
                    self.db, key, info.group_id, info.name, info.is_closed, duplicate_of
                )
                resolved += 1
        log.info("Разрешено адресов сообществ: %d из %d", resolved, len(pending))
        return resolved

    # ------------------------------------------------------------ processing
    async def _process_slice(
        self, run: repo.ScanRun, targets: list[repo.ScanTarget], start: int, end: int, stats: TickStats
    ) -> None:
        assert self.vk is not None
        batch = targets[start:end]
        if not batch:
            await repo.update_run_progress(self.db, run.run_id, next_index=end)
            return
        by_owner = {t.owner_id: t for t in batch}
        results = await self.vk.wall_get_many(
            list(by_owner), count=self.settings.wall_count, batch_size=self.settings.vk_execute_batch
        )
        cutoff = now_utc() - timedelta(hours=self.settings.lookback_hours)
        new_posts: list[tuple[repo.ScanTarget, VkPost]] = []
        slice_fetched = 0
        slice_errors = 0
        for owner_id, target in by_owner.items():
            result = results.get(owner_id)
            if result is None:
                continue
            if result.error is not None:
                stats.errors += 1
                slice_errors += 1
                await repo.record_cursor_error(
                    self.db, target.community_key, str(result.error), permanent=result.error.is_permanent
                )
                if result.error.is_permanent and target.consecutive_errors + 1 >= MAX_PERMANENT_ERRORS:
                    await self.db.execute(
                        "UPDATE communities SET scan_enabled = 0 WHERE community_key = ?",
                        (target.community_key,),
                    )
                    log.warning("Сообщество %s отключено: %s", target.canonical_url, result.error)
                if result.error.is_auth:
                    log.error("VK: ошибка авторизации (%s). Проверьте VK_ACCESS_TOKEN.", result.error)
                continue
            stats.posts_fetched += len(result.posts)
            slice_fetched += len(result.posts)
            newest_id = max((p.post_id for p in result.posts), default=None)
            newest_date = max((p.date for p in result.posts), default=None)
            await repo.record_cursor_success(
                self.db, target.community_key, newest_id, to_iso(newest_date) if newest_date else None
            )
            for post in result.posts:
                if post.date < cutoff or post.marked_as_ads or post.post_type in SKIPPED_POST_TYPES:
                    continue
                if target.last_post_id is not None and post.post_id <= target.last_post_id:
                    # Уже видели этот пост (или более новые) — но всё же проверим наличие в БД
                    if await repo.post_exists(self.db, post.owner_id, post.post_id):
                        continue
                inserted = await repo.insert_post(
                    self.db,
                    owner_id=post.owner_id,
                    post_id=post.post_id,
                    community_key=target.community_key,
                    published_at=to_iso(post.date),
                    url=post.url,
                    text=post.text,
                    content_hash=post.content_hash,
                    is_repost=post.is_repost,
                    attachments=post.attachments,
                )
                if inserted:
                    stats.posts_new += 1
                    new_posts.append((target, post))
        await self.db.commit()
        stats.processed += len(batch)
        found = await self._classify_new(new_posts, stats)
        await repo.update_run_progress(
            self.db,
            run.run_id,
            next_index=end,
            posts_fetched=slice_fetched,
            posts_new=len(new_posts),
            achievements_found=found,
            errors=slice_errors,
        )
        run.next_index = end

    async def _classify_new(self, new_posts: list[tuple[repo.ScanTarget, VkPost]], stats: TickStats) -> int:
        if not new_posts:
            return 0

        async def one(
            target: repo.ScanTarget, post: VkPost
        ) -> tuple[repo.ScanTarget, VkPost, ClassificationResult | None]:
            municipality = ", ".join(BY_ID[m].name for m in target.municipality_ids if m in BY_ID) or None
            try:
                result = await self.classifier.classify(post.text, target.school_names, municipality)
            except Exception as exc:  # оставляем пост неклассифицированным, попробуем позже
                log.warning("Классификация %s не удалась: %s", post.url, exc)
                return target, post, None
            return target, post, result

        results = await asyncio.gather(*(one(t, p) for t, p in new_posts))
        found = 0
        for target, post, result in results:
            if result is None:
                continue
            stats.classified += 1
            await repo.mark_post_classified(
                self.db,
                post.owner_id,
                post.post_id,
                is_achievement=result.is_achievement,
                confidence=result.confidence,
                classifier_version=result.classifier_version,
                keyword_hits=result.keyword_hits,
            )
            if result.is_achievement and result.confidence >= self.settings.min_confidence:
                cid = await repo.upsert_candidate(
                    self.db,
                    owner_id=post.owner_id,
                    post_id=post.post_id,
                    community_key=target.community_key,
                    kind=result.kind,
                    headline=result.headline,
                    summary=result.summary,
                    students=result.students,
                    confidence=result.confidence,
                    municipality_ids=target.municipality_ids,
                    unit_ids=target.unit_ids,
                    school_names=target.school_names,
                    published_at=to_iso(post.date),
                    url=post.url,
                    classifier_version=result.classifier_version,
                )
                stats.new_candidate_ids.append(cid)
                stats.achievements += 1
                found += 1
        await self.db.commit()
        return found

    async def retry_unclassified(self, limit: int = 100) -> int:
        """Повторная классификация постов, для которых модель ранее не ответила."""
        if self._lock.locked():
            return 0  # тик или полный обход уже классифицируют — не дублируем запросы к модели
        async with self._lock:
            return await self._retry_unclassified_inner(limit)

    async def _retry_unclassified_inner(self, limit: int) -> int:
        since = now_utc() - timedelta(hours=self.settings.lookback_hours)
        rows = await repo.list_unclassified_posts(self.db, since, limit)
        if not rows:
            return 0
        targets = {
            t.community_key: t for t in await repo.list_scan_targets(self.db, self.settings.monitor_scope)
        }
        pending: list[tuple[repo.ScanTarget, VkPost]] = []
        for row in rows:
            target = targets.get(row["community_key"])
            if target is None:
                continue
            published = from_iso(row["published_at"])
            if published is None:
                continue
            post = VkPost(
                owner_id=row["owner_id"],
                post_id=row["post_id"],
                date=published,
                text=row["text"] or "",
                own_text=row["text"] or "",
                repost_text="",
                is_repost=bool(row["is_repost"]),
                is_pinned=False,
                marked_as_ads=False,
                post_type="post",
            )
            pending.append((target, post))
        stats = TickStats()
        found = await self._classify_new(pending, stats)
        if stats.new_candidate_ids:
            await self._notify_new(stats.new_candidate_ids)
        return found


def schedule_run_end(run: repo.ScanRun) -> datetime:
    end = from_iso(run.window_end)
    assert end is not None
    return end


def _identifier_for_resolve(row: dict) -> str | None:
    screen = (row.get("screen_name") or "").strip()
    if screen:
        return screen
    url = (row.get("canonical_url") or "").rstrip("/")
    return url.rsplit("/", 1)[-1] if "/" in url else None
