from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

import radar.pipeline.scanner as scanner_mod
from radar.classify.service import Classifier
from radar.db import repo
from radar.pipeline.scanner import Scanner
from radar.vk.client import VkClient
from tests.conftest import make_post

MSK = ZoneInfo("Europe/Moscow")


async def _scanner(db, settings, fake_vk, hook=None) -> Scanner:
    vk = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()), use_execute=True)
    return Scanner(
        db, vk, Classifier(None, min_confidence=settings.min_confidence), settings, on_new_candidates=hook
    )


async def test_window_spreads_work_and_finishes(db, settings, fake_vk, monkeypatch):
    targets = await repo.list_scan_targets(db, "all")
    for t in targets:
        fake_vk.walls[t.owner_id] = [
            make_post(t.owner_id, 100, "Поздравляем Петрову Марию с победой в олимпиаде по химии!"),
            make_post(t.owner_id, 90, "Старый победный пост", age_seconds=5 * 86400),
            make_post(t.owner_id, 80, "Расписание изменено."),
        ]
    found: list[list[int]] = []

    async def hook(ids):
        found.append(ids)

    sc = await _scanner(db, settings, fake_vk, hook)
    start = datetime.combine(datetime.now(MSK).date(), datetime.min.time(), tzinfo=MSK).replace(hour=8)
    clock = {"now": start}
    monkeypatch.setattr(scanner_mod, "now_utc", lambda: clock["now"].astimezone(UTC))
    per_tick = []
    t = start
    while t <= start + timedelta(hours=4, minutes=50):
        clock["now"] = t
        st = await sc.tick()
        per_tick.append(st.processed)
        t += timedelta(minutes=5)
    assert sum(per_tick) == len(targets)
    assert max(per_tick) <= 12  # нагрузка размазана, а не одним махом
    run = (await repo.last_runs(db, 1))[0]
    assert run.status == "done" and run.next_index == len(targets)
    assert await db.scalar("SELECT COUNT(*) FROM posts") == 2 * len(targets)  # пост старше 48 ч отброшен
    assert await db.scalar("SELECT COUNT(*) FROM achievement_candidates") == len(targets)
    assert sum(len(x) for x in found) == len(targets)
    # повторный тик после окна ничего не делает и не дублирует
    clock["now"] = start + timedelta(hours=6)
    st = await sc.tick()
    assert st.processed == 0
    assert await db.scalar("SELECT COUNT(*) FROM posts") == 2 * len(targets)
    await sc.vk.close()


async def test_catch_up_after_downtime(db, settings, fake_vk, monkeypatch):
    targets = await repo.list_scan_targets(db, "all")
    for t in targets:
        fake_vk.walls[t.owner_id] = [make_post(t.owner_id, 1, "Обычный пост")]
    sc = await _scanner(db, settings, fake_vk)
    start = datetime.combine(datetime.now(MSK).date(), datetime.min.time(), tzinfo=MSK).replace(hour=8)
    clock = {"now": start + timedelta(minutes=30)}
    monkeypatch.setattr(scanner_mod, "now_utc", lambda: clock["now"].astimezone(UTC))
    first = await sc.tick()
    assert 0 < first.processed < len(targets)
    # «бот упал» и вернулся после окна: догоняем порциями catchup_max_per_tick
    clock["now"] = start + timedelta(hours=7)
    total = first.processed
    for _ in range(20):
        st = await sc.tick()
        total += st.processed
        if (await repo.last_runs(db, 1))[0].status == "done":
            break
        assert st.processed <= settings.catchup_max_per_tick
    assert total == len(targets)
    await sc.vk.close()


async def test_permanent_error_disables_community(db, settings, fake_vk, monkeypatch):
    targets = await repo.list_scan_targets(db, "all")
    victim = targets[0]
    for t in targets:
        fake_vk.walls[t.owner_id] = []
    fake_vk.walls[victim.owner_id] = {"error_code": 15, "error_msg": "Access denied: wall is disabled"}
    sc = await _scanner(db, settings, fake_vk)
    for _ in range(3):
        stats = await sc.full_scan()
        assert stats.errors >= 1
    enabled = await db.scalar(
        "SELECT scan_enabled FROM communities WHERE community_key = ?", (victim.community_key,)
    )
    assert enabled == 0
    assert victim.community_key not in {t.community_key for t in await repo.list_scan_targets(db, "all")}
    await sc.vk.close()


async def test_resolve_pending_marks_duplicates_and_failures(db, settings, fake_vk):
    pending = await repo.list_unresolved_communities(db, 500)
    assert len(pending) == 98
    existing_gid = int(await db.scalar("SELECT group_id FROM communities WHERE group_id IS NOT NULL LIMIT 1"))
    for i, row in enumerate(pending):
        ident = row["screen_name"]
        if i == 0:
            fake_vk.groups[ident] = {"id": existing_gid, "screen_name": ident, "name": "Дубль"}
        elif i == 1:
            continue  # не найдём
        else:
            fake_vk.groups[ident] = {
                "id": 7_000_000 + i,
                "screen_name": ident,
                "name": f"Школа {i}",
                "is_closed": 0,
            }
    sc = await _scanner(db, settings, fake_vk)
    n = await sc.resolve_pending(limit=500)
    assert n == 97
    stats = await repo.community_stats(db, "all")
    assert stats["unresolved"] == 1 and stats["failed"] == 1 and stats["duplicates"] == 1
    assert len(await repo.list_scan_targets(db, "all")) == 267 + 96
    await sc.vk.close()


async def test_transient_resolve_error_keeps_pending(db, settings, fake_vk):
    fake_vk.error_6_once = True  # первая попытка — лимит частоты; клиент повторит сам

    def always_fail(request):
        return httpx.Response(200, json={"error": {"error_code": 10, "error_msg": "Internal server error"}})

    vk = VkClient(
        "t", rps=1000, http=httpx.AsyncClient(transport=httpx.MockTransport(always_fail)), max_retries=0
    )
    sc = Scanner(db, vk, Classifier(None), settings)
    assert await sc.resolve_pending(limit=500) == 0
    stats = await repo.community_stats(db, "all")
    assert stats["unresolved"] == 98 and stats["failed"] == 0  # ничего не помечено окончательно
    await vk.close()


async def test_duplicate_community_inherits_links(db, settings, fake_vk):
    pending = await repo.list_unresolved_communities(db, 500)
    victim = pending[0]
    canonical_gid = int(
        await db.scalar("SELECT group_id FROM communities WHERE group_id IS NOT NULL LIMIT 1")
    )
    canonical_key = await repo.community_key_by_group_id(db, canonical_gid)
    before = {t.community_key: t for t in await repo.list_scan_targets(db, "all")}[canonical_key]
    await repo.mark_community_resolved(db, victim["community_key"], canonical_gid, "Дубль", 0, canonical_key)
    after = {t.community_key: t for t in await repo.list_scan_targets(db, "all")}[canonical_key]
    victim_units = await db.fetchall(
        "SELECT unit_id FROM unit_communities WHERE community_key = ?", (victim["community_key"],)
    )
    assert victim_units and all(u["unit_id"] in after.unit_ids for u in victim_units)
    assert set(before.unit_ids) <= set(after.unit_ids)
