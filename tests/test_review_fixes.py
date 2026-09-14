"""Тесты на исправления по итогам ревью."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from radar.classify.llm import VERDICT_SCHEMA, ClaudeClassifier, Verdict, _is_fallback_error, _supports_effort
from radar.classify.service import Classifier
from radar.db import repo
from radar.db.bootstrap import initialize_database
from radar.db.migrations import split_statements
from radar.pipeline.notifier import Notifier
from radar.pipeline.scanner import Scanner
from radar.utils.timeutil import from_iso, now_utc, to_iso
from radar.vk.client import VkClient
from tests.conftest import SEED, make_post


def _walk(schema, path=""):
    if isinstance(schema, dict):
        for k, v in schema.items():
            yield f"{path}/{k}", k
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            yield from _walk(v, f"{path}[{i}]")


def test_schema_has_no_unsupported_keywords():
    banned = {
        "minimum",
        "maximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
    }
    assert not [p for p, k in _walk(VERDICT_SCHEMA) if k in banned]
    assert set(VERDICT_SCHEMA["required"]) == set(VERDICT_SCHEMA["properties"])
    assert VERDICT_SCHEMA["additionalProperties"] is False


def test_verdict_truncates_instead_of_rejecting():
    v = Verdict.model_validate(
        {
            "is_achievement": True,
            "confidence": 1.7,
            "about_students": True,
            "kind": "weird",
            "headline": "x" * 500,
            "summary": "y" * 2000,
            "students": ["  A ", "", 5],
            "reason": "r" * 1000,
        }
    )
    assert v.confidence == 1.0 and v.kind == "other"
    assert len(v.headline) == 120 and len(v.summary) == 600 and len(v.reason) == 400
    assert v.students == ["A", "5"]


def test_effort_gating_and_request_shape():
    assert _supports_effort("claude-opus-5") and _supports_effort("claude-sonnet-5")
    assert not _supports_effort("claude-haiku-4-5") and not _supports_effort("claude-sonnet-4-5")
    req = ClaudeClassifier(model="claude-haiku-4-5", api_key="k").build_request("t", ["Школа"], "Канашский")
    assert "effort" not in req["output_config"] and req["output_config"]["format"]["type"] == "json_schema"
    req2 = ClaudeClassifier(model="claude-opus-5", effort="low", api_key="k").build_request("t", [], None)
    assert req2["output_config"]["effort"] == "low" and req2["max_tokens"] >= 4096
    assert req2["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Школа: неизвестна" in req2["messages"][0]["content"]


def test_fallback_error_detection():
    import anthropic

    def err(msg, param=None):
        body = {"error": {"type": "invalid_request_error", "message": msg, "param": param}}
        resp = httpx.Response(400, json=body, request=httpx.Request("POST", "https://x"))
        return anthropic.BadRequestError(msg, response=resp, body=body)

    assert _is_fallback_error(err("fallbacks is not supported for this organization"))
    assert _is_fallback_error(err("unknown beta header server-side-fallback-2026-07-01"))
    assert not _is_fallback_error(err("output_config.effort: unsupported for this model"))


def test_split_statements_respects_quotes():
    stmts = split_statements(
        "CREATE VIEW v AS SELECT group_concat(a, '; ') FROM t; -- comment; here\nINSERT INTO x VALUES ('a;b');"
    )
    assert stmts == ["CREATE VIEW v AS SELECT group_concat(a, '; ') FROM t", "INSERT INTO x VALUES ('a;b')"]


async def test_migration_applies_to_premigrated_seed(tmp_path: Path):
    """Заготовка следующей версии реестра уже содержит municipalities/units.municipality_id/профили."""
    seed = tmp_path / "seed.sqlite"
    seed.write_bytes(SEED.read_bytes())
    con = sqlite3.connect(seed)
    con.executescript(
        """
        CREATE TABLE municipalities (municipality_id TEXT PRIMARY KEY, name TEXT NOT NULL, municipality_type TEXT NOT NULL,
                                     source_id TEXT, checked_at TEXT);
        INSERT INTO municipalities VALUES ('kit-cheb', 'Чебоксары (kit)', 'city', NULL, NULL);
        ALTER TABLE units ADD COLUMN municipality_id TEXT REFERENCES municipalities(municipality_id);
        UPDATE units SET municipality_id = 'kit-cheb' WHERE area_source = 'г. Чебоксары';
        CREATE TABLE notification_profiles (profile_id TEXT PRIMARY KEY, default_enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE notification_region_settings (profile_id TEXT NOT NULL, municipality_id TEXT NOT NULL,
                                                   enabled INTEGER NOT NULL, updated_at TEXT NOT NULL,
                                                   PRIMARY KEY (profile_id, municipality_id));
        CREATE VIEW v_notification_regions AS SELECT 1 AS profile_id;
        """
    )
    con.commit()
    con.close()
    db = await initialize_database(tmp_path / "radar.sqlite", seed)
    try:
        assert await db.scalar("SELECT COUNT(*) FROM municipalities") == 23
        assert await db.scalar("SELECT COUNT(*) FROM units WHERE municipality_id = 'kit-cheb'") == 0
        assert await db.scalar("SELECT COUNT(*) FROM units WHERE municipality_id IS NULL") == 0
        sub, _ = await repo.upsert_subscriber(db, 1, None, None, "13:00", "Europe/Moscow")
        assert len(await repo.list_regions(db, sub.profile_id)) == 23
        row = await db.fetchone(
            "SELECT * FROM v_notification_targets WHERE profile_id = ? LIMIT 1", (sub.profile_id,)
        )
        assert row is not None and "municipality_names" in row
    finally:
        await db.close()


async def test_bootstrap_failure_closes_connection(tmp_path: Path, monkeypatch):
    import radar.db.bootstrap as bs

    async def boom(db):
        raise RuntimeError("seed failure")

    monkeypatch.setattr(bs, "seed_municipalities", boom)
    with pytest.raises(RuntimeError):
        await initialize_database(tmp_path / "radar.sqlite", SEED)
    # если соединение закрыто, повторная инициализация работает
    monkeypatch.undo()
    db = await initialize_database(tmp_path / "radar.sqlite", SEED)
    await db.close()


def test_stale_wal_removed_before_seed_copy(tmp_path: Path):
    from radar.db.bootstrap import ensure_database_file

    db_path = tmp_path / "radar.sqlite"
    (tmp_path / "radar.sqlite-wal").write_bytes(b"stale")
    (tmp_path / "radar.sqlite-shm").write_bytes(b"stale")
    assert ensure_database_file(db_path, SEED)
    assert not (tmp_path / "radar.sqlite-wal").exists() and not (tmp_path / "radar.sqlite-shm").exists()
    assert not (tmp_path / "radar.sqlite.tmp").exists()


async def test_notification_targets_view_contract(db):
    sub, _ = await repo.upsert_subscriber(db, 1, None, None, "13:00", "Europe/Moscow")
    rows = await db.fetchall("SELECT * FROM v_notification_targets WHERE profile_id = ?", (sub.profile_id,))
    assert len(rows) == 59
    multi = [r for r in rows if "; " in (r["unit_ids"] or "")]
    assert multi, "должны быть сообщества с несколькими площадками"
    for r in multi:
        assert len(r["unit_ids"].split("; ")) == len(r["school_names"].split("; "))
        assert r["municipality_names"]


async def test_chained_duplicates_collapse(db, settings, fake_vk):
    pending = await repo.list_unresolved_communities(db, 500)
    trio = [pending[i]["screen_name"] for i in (0, 1, 2)]
    gid = 9_999_001
    for name in trio:
        fake_vk.groups[name] = {"id": gid, "screen_name": name, "name": "Общая группа", "is_closed": 0}
    for row in pending[3:]:
        fake_vk.groups[row["screen_name"]] = {
            "id": 8_000_000 + hash(row["screen_name"]) % 100000,
            "screen_name": row["screen_name"],
            "name": "x",
        }
    vk = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()))
    sc = Scanner(db, vk, Classifier(None), settings)
    await sc.resolve_pending(limit=500)
    rows = await db.fetchall(
        "SELECT community_key, duplicate_of FROM communities WHERE resolved_group_id = ? ORDER BY community_key",
        (gid,),
    )
    canon = [r for r in rows if r["duplicate_of"] is None]
    assert len(canon) == 1
    assert all(r["duplicate_of"] == canon[0]["community_key"] for r in rows if r["duplicate_of"])
    target = next(t for t in await repo.list_scan_targets(db, "all") if t.owner_id == -gid)
    expected_units = {
        r["unit_id"]
        for r in await db.fetchall(
            "SELECT uc.unit_id FROM unit_communities uc JOIN units u USING(unit_id) "
            "WHERE uc.community_key IN (SELECT community_key FROM communities WHERE resolved_group_id = ?) "
            "AND u.scope = 'school'",
            (gid,),
        )
    }
    assert set(target.unit_ids) == expected_units
    # даже если в базе осталась старая цепочка — repair её сворачивает
    a, b, c = [r["community_key"] for r in rows]
    await db.execute("UPDATE communities SET duplicate_of = ? WHERE community_key = ?", (b, c))
    await db.execute("UPDATE communities SET duplicate_of = NULL WHERE community_key = ?", (a,))
    await db.execute("UPDATE communities SET duplicate_of = ? WHERE community_key = ?", (a, b))
    await db.commit()
    assert await repo.repair_duplicate_chains(db) == 1
    assert await db.scalar("SELECT duplicate_of FROM communities WHERE community_key = ?", (c,)) == a
    await vk.close()


async def test_transient_errors_do_not_count_toward_disable(db, settings, fake_vk):
    targets = await repo.list_scan_targets(db, "all")
    victim = targets[0]
    for t in targets:
        fake_vk.walls[t.owner_id] = []
    vk = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()), max_retries=0)
    sc = Scanner(db, vk, Classifier(None), settings)
    fake_vk.walls[victim.owner_id] = {"error_code": 10, "error_msg": "Internal server error"}
    await sc.full_scan()
    await sc.full_scan()
    fake_vk.walls[victim.owner_id] = {"error_code": 15, "error_msg": "Access denied"}
    await sc.full_scan()
    assert (
        await db.scalar(
            "SELECT scan_enabled FROM communities WHERE community_key = ?", (victim.community_key,)
        )
        == 1
    )
    assert (
        await db.scalar(
            "SELECT consecutive_errors FROM community_cursors WHERE community_key = ?",
            (victim.community_key,),
        )
        == 1
    )
    await vk.close()


async def test_execute_error_13_does_not_latch(fake_vk):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "execute":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200, json={"error": {"error_code": 13, "error_msg": "response size is too big"}}
                )
            return httpx.Response(200, json={"response": [{"count": 0, "items": []}]})
        return httpx.Response(200, json={"response": {"count": 0, "items": []}})

    client = VkClient(
        "t", rps=1000, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), use_execute=True
    )
    res = await client.wall_get_many([-1, -4], batch_size=25)
    assert res[-1].error is None and res[-4].error is None  # пакет прочитан по одному
    assert client.execute_supported is None  # execute не «залипает» в выключенном состоянии
    await client.wall_get_many([-2, -3], batch_size=25)
    assert calls["n"] == 2 and client.execute_supported is True
    await client.close()


async def test_check_token_distinguishes_community_errors(fake_vk):
    fake_vk.walls[-1] = {"error_code": 15, "error_msg": "Access denied"}
    fake_vk.walls[-2] = [make_post(-2, 1, "a")]
    fake_vk.groups["1"] = {"id": 1, "screen_name": "club1", "name": "x"}
    fake_vk.fail_execute = True
    client = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()), use_execute=True)
    report = await client.check_token([-1, -2])
    assert report["wall.get"] == "ok" and report["groups.getById"] == "ok"

    def auth_fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"error": {"error_code": 5, "error_msg": "User authorization failed"}}
        )

    client2 = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=httpx.MockTransport(auth_fail)))
    report2 = await client2.check_token([-1, -2])
    assert report2["wall.get"].startswith("ошибка 5")
    await client.close()
    await client2.close()


async def test_purge_removes_old_delivered_candidates(db):
    await repo.upsert_subscriber(db, 1, None, None, "13:00", "Europe/Moscow")
    old = now_utc() - timedelta(days=40)
    await repo.insert_post(
        db,
        owner_id=-1,
        post_id=1,
        community_key="vk:10812563",
        published_at=to_iso(old),
        url="u",
        text="t",
        content_hash="h",
        is_repost=False,
        attachments=[],
    )
    await repo.upsert_candidate(
        db,
        owner_id=-1,
        post_id=1,
        community_key="vk:10812563",
        kind="award",
        headline="h",
        summary="s",
        students=[],
        confidence=0.9,
        municipality_ids=[],
        unit_ids=[],
        school_names=[],
        published_at=to_iso(old),
        url="u",
        classifier_version="t",
    )
    await repo.record_delivery(db, 1, -1, 1, "digest", 1)
    await db.commit()
    await repo.purge_old_posts(db, now_utc() - timedelta(days=30))
    assert await db.scalar("SELECT COUNT(*) FROM achievement_candidates") == 0
    assert await db.scalar("SELECT COUNT(*) FROM posts") == 0
    assert await db.scalar("SELECT COUNT(*) FROM deliveries") == 0


async def test_digest_window_uses_discovery_time(db, settings):
    """Пост опубликован 47 ч назад, найден только сейчас — должен попасть в дайджест «раз в день»."""
    await repo.upsert_subscriber(db, 1, None, None, "13:00", "Europe/Moscow")
    published = now_utc() - timedelta(hours=47)
    await repo.insert_post(
        db,
        owner_id=-2,
        post_id=1,
        community_key="vk:10812563",
        published_at=to_iso(published),
        url="u",
        text="t",
        content_hash="h",
        is_repost=False,
        attachments=[],
    )
    await repo.upsert_candidate(
        db,
        owner_id=-2,
        post_id=1,
        community_key="vk:10812563",
        kind="award",
        headline="h",
        summary="s",
        students=[],
        confidence=0.9,
        municipality_ids=["ru21-mo-kanash"],
        unit_ids=[],
        school_names=[],
        published_at=to_iso(published),
        url="u",
        classifier_version="t",
    )
    await db.commit()
    since = now_utc() - timedelta(hours=48)
    assert len(await repo.list_pending_for_chat(db, 1, since, 0.5)) == 1
    await db.execute(
        "UPDATE achievement_candidates SET created_at = ?", (to_iso(now_utc() - timedelta(hours=49)),)
    )
    await db.commit()
    assert await repo.list_pending_for_chat(db, 1, since, 0.5) == []


async def test_digest_schedule_not_clobbered_when_user_changes_settings(db, settings):
    class SlowBot:
        def __init__(self, db):
            self.db = db

        async def send_message(self, chat_id, text, **kwargs):
            # пользователь меняет расписание во время отправки
            await repo.update_subscriber(
                self.db, chat_id, digest_times="09:00", next_digest_at="2099-01-01T09:00:00+00:00"
            )

            class M:
                message_id = 1

            return M()

    notifier = Notifier(SlowBot(db), db, settings)  # type: ignore[arg-type]
    sub, _ = await repo.upsert_subscriber(db, 3, None, None, "13:00,21:00", "Europe/Moscow")
    await repo.update_subscriber(db, 3, next_digest_at="2000-01-01T00:00:00+00:00", notify_empty=1)
    sub = await repo.get_subscriber(db, 3)
    await notifier.deliver_digest(sub)
    fresh = await repo.get_subscriber(db, 3)
    assert fresh.next_digest_at == "2099-01-01T09:00:00+00:00"
    # без вмешательства пользователя расписание сдвигается
    await repo.update_subscriber(db, 3, next_digest_at="2000-01-01T00:00:00+00:00")

    class QuietBot:
        async def send_message(self, chat_id, text, **kwargs):
            class M:
                message_id = 2

            return M()

    await Notifier(QuietBot(), db, settings).deliver_digest(await repo.get_subscriber(db, 3))  # type: ignore[arg-type]
    assert from_iso((await repo.get_subscriber(db, 3)).next_digest_at) > now_utc()
