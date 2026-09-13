from datetime import timedelta

from radar.db import repo
from radar.db.bootstrap import initialize_database
from radar.utils.timeutil import now_utc, to_iso


async def test_bootstrap_is_idempotent(settings):
    db = await initialize_database(settings.db_path, settings.registry_seed_path)
    assert await db.scalar("SELECT COUNT(*) FROM municipalities") == 23
    assert await db.scalar("SELECT COUNT(*) FROM units WHERE municipality_id IS NULL") == 0
    assert await db.scalar("PRAGMA integrity_check") == "ok"
    assert await db.fetchall("PRAGMA foreign_key_check") == []
    await db.close()
    db = await initialize_database(settings.db_path, settings.registry_seed_path)
    assert await db.fetchall("SELECT version FROM schema_migrations") == [{"version": 1}]
    await db.close()


async def test_profiles_and_regions(db):
    sub, created = await repo.upsert_subscriber(db, 42, "u", "U", "13:00,21:00", "Europe/Moscow")
    assert created and sub.profile_id == "telegram:chat:42"
    regions = await repo.list_regions(db, sub.profile_id)
    assert len(regions) == 23 and all(r.enabled for r in regions)
    await repo.set_region_enabled(db, sub.profile_id, "ru21-city-cheboksary", False)
    assert len(await repo.enabled_region_ids(db, sub.profile_id)) == 22
    # повторная регистрация не затирает выбор
    _, created2 = await repo.upsert_subscriber(db, 42, "u2", "U", "13:00,21:00", "Europe/Moscow")
    assert not created2
    assert len(await repo.enabled_region_ids(db, sub.profile_id)) == 22
    await repo.set_all_regions(db, sub.profile_id, False)
    assert await repo.enabled_region_ids(db, sub.profile_id) == set()
    assert (
        await db.scalar(
            "SELECT COUNT(*) FROM notification_region_settings WHERE profile_id = ?", (sub.profile_id,)
        )
        == 0
    )
    await repo.set_all_regions(db, sub.profile_id, True)
    assert len(await repo.enabled_region_ids(db, sub.profile_id)) == 23


async def test_scan_targets_scopes(db):
    all_targets = await repo.list_scan_targets(db, "all")
    official = await repo.list_scan_targets(db, "official")
    assert len(all_targets) == 267  # только с числовым id до разрешения адресов
    assert 0 < len(official) < len(all_targets)
    assert all(t.owner_id < 0 for t in all_targets)
    assert all(t.municipality_ids for t in all_targets)
    keys = [t.community_key for t in all_targets]
    assert keys == sorted(keys)
    assert len(await repo.list_unresolved_communities(db, 500)) == 98


async def test_candidates_and_deliveries(db):
    await repo.upsert_subscriber(db, 7, None, None, "13:00", "Europe/Moscow")
    now = now_utc()
    await repo.insert_post(
        db,
        owner_id=-1,
        post_id=5,
        community_key="vk:10812563",
        published_at=to_iso(now),
        url="https://vk.com/wall-1_5",
        text="t",
        content_hash="h",
        is_repost=False,
        attachments=[],
    )
    cid = await repo.upsert_candidate(
        db,
        owner_id=-1,
        post_id=5,
        community_key="vk:10812563",
        kind="award",
        headline="h",
        summary="s",
        students=["A"],
        confidence=0.9,
        municipality_ids=["ru21-city-novocheboksarsk"],
        unit_ids=["u"],
        school_names=["Школа, с запятой"],
        published_at=to_iso(now),
        url="https://vk.com/wall-1_5",
        classifier_version="test",
    )
    await db.commit()
    pending = await repo.list_pending_for_chat(db, 7, now - timedelta(hours=48), 0.6)
    assert [c.candidate_id for c in pending] == [cid]
    assert pending[0].school_names == ["Школа, с запятой"]
    await repo.record_delivery(db, 7, -1, 5, "digest", 1)
    await db.commit()
    assert await repo.list_pending_for_chat(db, 7, now - timedelta(hours=48), 0.6) == []
    # низкая уверенность отфильтровывается
    assert await repo.list_pending_for_chat(db, 8, now - timedelta(hours=48), 0.95) == []
    await repo.set_candidate_review(db, cid, "rejected", 7)
    await repo.upsert_subscriber(db, 9, None, None, "13:00", "Europe/Moscow")
    assert await repo.list_pending_for_chat(db, 9, now - timedelta(hours=48), 0.6) == []
