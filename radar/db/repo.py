"""Слой доступа к данным: подписчики, территории, сообщества, посты, кандидаты, доставки, запуски."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from radar.db.connection import Database
from radar.utils.timeutil import now_utc, to_iso

log = logging.getLogger(__name__)


def profile_id_for_chat(chat_id: int) -> str:
    return f"telegram:chat:{chat_id}"


# ----------------------------------------------------------------------------
# Подписчики и профили
# ----------------------------------------------------------------------------


@dataclass
class Subscriber:
    chat_id: int
    profile_id: str
    username: str | None
    first_name: str | None
    is_active: bool
    mode: str
    digest_times: str
    interval_days: int
    digest_format: str
    notify_empty: bool
    timezone: str
    next_digest_at: str | None
    last_digest_at: str | None
    created_at: str
    updated_at: str
    last_seen_at: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Subscriber:
        return cls(
            chat_id=int(row["chat_id"]),
            profile_id=row["profile_id"],
            username=row.get("username"),
            first_name=row.get("first_name"),
            is_active=bool(row["is_active"]),
            mode=row["mode"],
            digest_times=row["digest_times"],
            interval_days=int(row["interval_days"]),
            digest_format=row["digest_format"],
            notify_empty=bool(row["notify_empty"]),
            timezone=row["timezone"],
            next_digest_at=row.get("next_digest_at"),
            last_digest_at=row.get("last_digest_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_seen_at=row.get("last_seen_at"),
        )


async def get_subscriber(db: Database, chat_id: int) -> Subscriber | None:
    row = await db.fetchone("SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,))
    return Subscriber.from_row(row) if row else None


async def upsert_subscriber(
    db: Database,
    chat_id: int,
    username: str | None,
    first_name: str | None,
    default_digest_times: str,
    default_timezone: str,
) -> tuple[Subscriber, bool]:
    """Создать подписчика и профиль (идемпотентно). Возвращает (подписчик, создан_впервые)."""
    now = to_iso(now_utc())
    profile_id = profile_id_for_chat(chat_id)
    existing = await get_subscriber(db, chat_id)
    await db.execute(
        "INSERT INTO notification_profiles (profile_id, default_enabled) VALUES (?, 1) "
        "ON CONFLICT(profile_id) DO NOTHING",
        (profile_id,),
    )
    # Вставка идемпотентна: два одновременных первых сообщения из одного чата не дадут IntegrityError.
    await db.execute(
        """
        INSERT INTO subscribers (chat_id, profile_id, username, first_name, digest_times, timezone,
                                 created_at, updated_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            username = excluded.username, first_name = excluded.first_name, last_seen_at = excluded.last_seen_at
        """,
        (chat_id, profile_id, username, first_name, default_digest_times, default_timezone, now, now, now),
    )
    await db.commit()
    sub = await get_subscriber(db, chat_id)
    assert sub is not None
    return sub, existing is None


async def update_subscriber(db: Database, chat_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = to_iso(now_utc())
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE subscribers SET {cols} WHERE chat_id = ?", (*fields.values(), chat_id))
    await db.commit()


async def touch_subscriber(db: Database, chat_id: int) -> None:
    await db.execute(
        "UPDATE subscribers SET last_seen_at = ? WHERE chat_id = ?", (to_iso(now_utc()), chat_id)
    )
    await db.commit()


async def list_active_subscribers(db: Database, mode: str | None = None) -> list[Subscriber]:
    if mode:
        rows = await db.fetchall("SELECT * FROM subscribers WHERE is_active = 1 AND mode = ?", (mode,))
    else:
        rows = await db.fetchall("SELECT * FROM subscribers WHERE is_active = 1")
    return [Subscriber.from_row(r) for r in rows]


async def list_due_digests(db: Database, now: datetime) -> list[Subscriber]:
    rows = await db.fetchall(
        "SELECT * FROM subscribers WHERE is_active = 1 AND mode = 'digest' "
        "AND next_digest_at IS NOT NULL AND next_digest_at <= ?",
        (to_iso(now),),
    )
    return [Subscriber.from_row(r) for r in rows]


async def count_subscribers(db: Database) -> dict[str, int]:
    row = await db.fetchone(
        "SELECT COUNT(*) AS total, SUM(is_active) AS active, "
        "SUM(CASE WHEN mode = 'instant' AND is_active = 1 THEN 1 ELSE 0 END) AS instant FROM subscribers"
    )
    return {k: int(v or 0) for k, v in (row or {}).items()}


# ----------------------------------------------------------------------------
# Территории
# ----------------------------------------------------------------------------


@dataclass
class RegionState:
    municipality_id: str
    name: str
    short_name: str
    municipality_type: str
    sort_order: int
    enabled: bool


async def list_regions(db: Database, profile_id: str) -> list[RegionState]:
    rows = await db.fetchall(
        "SELECT * FROM v_notification_regions WHERE profile_id = ? ORDER BY sort_order", (profile_id,)
    )
    return [
        RegionState(
            municipality_id=r["municipality_id"],
            name=r["name"],
            short_name=r["short_name"],
            municipality_type=r["municipality_type"],
            sort_order=int(r["sort_order"]),
            enabled=bool(r["enabled"]),
        )
        for r in rows
    ]


async def enabled_region_ids(db: Database, profile_id: str) -> set[str]:
    rows = await db.fetchall(
        "SELECT municipality_id FROM v_notification_regions WHERE profile_id = ? AND enabled = 1",
        (profile_id,),
    )
    return {r["municipality_id"] for r in rows}


async def set_region_enabled(db: Database, profile_id: str, municipality_id: str, enabled: bool) -> None:
    await db.execute(
        """
        INSERT INTO notification_region_settings (profile_id, municipality_id, enabled, updated_at)
        VALUES (:profile_id, :municipality_id, :enabled, :updated_at)
        ON CONFLICT (profile_id, municipality_id)
        DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at
        """,
        {
            "profile_id": profile_id,
            "municipality_id": municipality_id,
            "enabled": 1 if enabled else 0,
            "updated_at": to_iso(now_utc()),
        },
    )
    await db.commit()


async def set_all_regions(db: Database, profile_id: str, enabled: bool) -> None:
    await db.execute(
        "UPDATE notification_profiles SET default_enabled = ? WHERE profile_id = ?",
        (1 if enabled else 0, profile_id),
    )
    await db.execute("DELETE FROM notification_region_settings WHERE profile_id = ?", (profile_id,))
    await db.commit()


# ----------------------------------------------------------------------------
# Сообщества
# ----------------------------------------------------------------------------


@dataclass
class ScanTarget:
    community_key: str
    owner_id: int  # отрицательный id сообщества
    canonical_url: str
    screen_name: str | None
    title: str | None
    unit_ids: list[str]
    school_names: list[str]
    municipality_ids: list[str]
    last_post_id: int | None = None
    consecutive_errors: int = 0


def _scope_condition(monitor_scope: str) -> str:
    if monitor_scope == "official":
        return "uc.monitor_enabled = 1 AND uc.official_status = 'official_source_link'"
    return "1 = 1"


async def list_scan_targets(db: Database, monitor_scope: str = "all") -> list[ScanTarget]:
    """Сообщества для опроса: с известным числовым id, включённые, без дублей. Стабильный порядок."""
    communities = await db.fetchall(
        """
        SELECT c.community_key, c.canonical_url, c.screen_name, c.title,
               COALESCE(c.group_id, c.resolved_group_id) AS gid,
               cur.last_post_id, COALESCE(cur.consecutive_errors, 0) AS consecutive_errors
        FROM communities c
        LEFT JOIN community_cursors cur ON cur.community_key = c.community_key
        WHERE c.scan_enabled = 1
          AND c.duplicate_of IS NULL
          AND COALESCE(c.group_id, c.resolved_group_id) IS NOT NULL
        ORDER BY c.community_key
        """
    )
    # Связи сообществ-дубликатов (duplicate_of) приписываются каноническому сообществу,
    # чтобы его посты относились ко всем школам и территориям, которые на него ссылаются.
    links = await db.fetchall(
        f"""
        SELECT COALESCE(c.duplicate_of, c.community_key) AS community_key, u.unit_id, u.name, u.municipality_id
        FROM unit_communities uc
        JOIN units u ON u.unit_id = uc.unit_id
        JOIN communities c ON c.community_key = uc.community_key
        WHERE u.scope = 'school' AND {_scope_condition(monitor_scope)}
        ORDER BY u.name
        """
    )
    by_key: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        by_key.setdefault(link["community_key"], []).append(link)
    targets: list[ScanTarget] = []
    for r in communities:
        linked = by_key.get(r["community_key"])
        if not linked:
            continue
        unit_ids: list[str] = []
        names: list[str] = []
        mids: list[str] = []
        for link in linked:
            if link["unit_id"] not in unit_ids:
                unit_ids.append(link["unit_id"])
            if link["name"] not in names:
                names.append(link["name"])
            if link["municipality_id"] and link["municipality_id"] not in mids:
                mids.append(link["municipality_id"])
        targets.append(
            ScanTarget(
                community_key=r["community_key"],
                owner_id=-int(r["gid"]),
                canonical_url=r["canonical_url"],
                screen_name=r["screen_name"],
                title=r["title"],
                unit_ids=unit_ids,
                school_names=names,
                municipality_ids=mids,
                last_post_id=r["last_post_id"],
                consecutive_errors=int(r["consecutive_errors"] or 0),
            )
        )
    return targets


async def list_unresolved_communities(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT community_key, canonical_url, screen_name FROM communities "
        "WHERE group_id IS NULL AND resolved_group_id IS NULL AND resolve_status IN ('pending', 'error') "
        "AND scan_enabled = 1 ORDER BY community_key LIMIT ?",
        (limit,),
    )


async def community_key_by_group_id(db: Database, group_id: int) -> str | None:
    return await db.scalar(
        "SELECT community_key FROM communities WHERE group_id = ? OR resolved_group_id = ? LIMIT 1",
        (group_id, group_id),
    )


async def mark_community_resolved(
    db: Database,
    community_key: str,
    group_id: int,
    title: str | None,
    is_closed: int | None,
    duplicate_of: str | None,
) -> None:
    await db.execute(
        "UPDATE communities SET resolved_group_id = ?, title = ?, is_closed = ?, duplicate_of = ?, "
        "resolve_status = 'resolved', resolve_error = NULL, resolved_at = ? WHERE community_key = ?",
        (group_id, title, is_closed, duplicate_of, to_iso(now_utc()), community_key),
    )
    await db.commit()


async def mark_community_resolve_failed(db: Database, community_key: str, error: str, final: bool) -> None:
    await db.execute(
        "UPDATE communities SET resolve_status = ?, resolve_error = ?, resolved_at = ? WHERE community_key = ?",
        ("failed" if final else "error", error[:500], to_iso(now_utc()), community_key),
    )
    await db.commit()


async def update_community_meta(
    db: Database, community_key: str, title: str | None, is_closed: int | None
) -> None:
    await db.execute(
        "UPDATE communities SET title = COALESCE(?, title), is_closed = COALESCE(?, is_closed) WHERE community_key = ?",
        (title, is_closed, community_key),
    )


async def community_stats(db: Database, monitor_scope: str = "all") -> dict[str, int]:
    total = await db.scalar(
        f"""
        SELECT COUNT(DISTINCT c.community_key) FROM communities c
        JOIN unit_communities uc ON uc.community_key = c.community_key
        JOIN units u ON u.unit_id = uc.unit_id
        WHERE u.scope = 'school' AND {_scope_condition(monitor_scope)}
        """
    )
    unresolved = await db.scalar(
        "SELECT COUNT(*) FROM communities WHERE group_id IS NULL AND resolved_group_id IS NULL"
    )
    failed = await db.scalar("SELECT COUNT(*) FROM communities WHERE resolve_status = 'failed'")
    duplicates = await db.scalar("SELECT COUNT(*) FROM communities WHERE duplicate_of IS NOT NULL")
    erroring = await db.scalar("SELECT COUNT(*) FROM community_cursors WHERE consecutive_errors >= 3")
    return {
        "total": int(total or 0),
        "unresolved": int(unresolved or 0),
        "failed": int(failed or 0),
        "duplicates": int(duplicates or 0),
        "erroring": int(erroring or 0),
    }


# ----------------------------------------------------------------------------
# Курсоры и посты
# ----------------------------------------------------------------------------


async def record_cursor_success(
    db: Database, community_key: str, last_post_id: int | None, last_post_date: str | None
) -> None:
    now = to_iso(now_utc())
    await db.execute(
        """
        INSERT INTO community_cursors (community_key, last_post_id, last_post_date, last_checked_at, last_success_at,
                                       last_error, consecutive_errors)
        VALUES (?, ?, ?, ?, ?, NULL, 0)
        ON CONFLICT(community_key) DO UPDATE SET
            last_post_id = MAX(COALESCE(community_cursors.last_post_id, 0), COALESCE(excluded.last_post_id, 0)),
            last_post_date = COALESCE(excluded.last_post_date, community_cursors.last_post_date),
            last_checked_at = excluded.last_checked_at,
            last_success_at = excluded.last_success_at,
            last_error = NULL,
            consecutive_errors = 0
        """,
        (community_key, last_post_id, last_post_date, now, now),
    )


async def record_cursor_error(db: Database, community_key: str, error: str) -> None:
    now = to_iso(now_utc())
    await db.execute(
        """
        INSERT INTO community_cursors (community_key, last_checked_at, last_error, consecutive_errors)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(community_key) DO UPDATE SET
            last_checked_at = excluded.last_checked_at,
            last_error = excluded.last_error,
            consecutive_errors = community_cursors.consecutive_errors + 1
        """,
        (community_key, now, error[:500]),
    )


async def post_exists(db: Database, owner_id: int, post_id: int) -> bool:
    return bool(
        await db.scalar("SELECT 1 FROM posts WHERE owner_id = ? AND post_id = ?", (owner_id, post_id))
    )


async def insert_post(
    db: Database,
    *,
    owner_id: int,
    post_id: int,
    community_key: str,
    published_at: str,
    url: str,
    text: str,
    content_hash: str,
    is_repost: bool,
    attachments: list[str],
) -> bool:
    """Вставить пост; False, если уже есть."""
    cur = await db.execute(
        """
        INSERT OR IGNORE INTO posts (owner_id, post_id, community_key, published_at, fetched_at, url, text,
                                     content_hash, is_repost, attachments)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            owner_id,
            post_id,
            community_key,
            published_at,
            to_iso(now_utc()),
            url,
            text,
            content_hash,
            1 if is_repost else 0,
            json.dumps(attachments, ensure_ascii=False),
        ),
    )
    return cur.rowcount > 0


async def mark_post_classified(
    db: Database,
    owner_id: int,
    post_id: int,
    *,
    is_achievement: bool,
    confidence: float,
    classifier_version: str,
    keyword_hits: list[str],
) -> None:
    await db.execute(
        "UPDATE posts SET classified_at = ?, is_achievement = ?, confidence = ?, classifier_version = ?, "
        "keyword_hits = ? WHERE owner_id = ? AND post_id = ?",
        (
            to_iso(now_utc()),
            1 if is_achievement else 0,
            confidence,
            classifier_version,
            json.dumps(keyword_hits, ensure_ascii=False),
            owner_id,
            post_id,
        ),
    )


async def list_unclassified_posts(db: Database, since: datetime, limit: int = 200) -> list[dict[str, Any]]:
    """Неклассифицированные посты в окне актуальности; более старые помечаются как пропущенные."""
    await db.execute(
        "UPDATE posts SET classified_at = ?, is_achievement = 0, confidence = 0, classifier_version = 'expired' "
        "WHERE classified_at IS NULL AND published_at < ?",
        (to_iso(now_utc()), to_iso(since)),
    )
    await db.commit()
    return await db.fetchall(
        "SELECT * FROM posts WHERE classified_at IS NULL AND published_at >= ? ORDER BY published_at LIMIT ?",
        (to_iso(since), limit),
    )


async def purge_old_posts(db: Database, older_than: datetime) -> int:
    cutoff = to_iso(older_than)
    await db.execute(
        "DELETE FROM achievement_candidates WHERE published_at < ? AND review_status IN ('sent', 'rejected', 'done')",
        (cutoff,),
    )
    cur = await db.execute(
        "DELETE FROM posts WHERE published_at < ? AND NOT EXISTS ("
        "SELECT 1 FROM achievement_candidates a WHERE a.owner_id = posts.owner_id AND a.post_id = posts.post_id)",
        (cutoff,),
    )
    await db.commit()
    return cur.rowcount


# ----------------------------------------------------------------------------
# Кандидаты достижений и доставки
# ----------------------------------------------------------------------------


@dataclass
class Candidate:
    candidate_id: int
    owner_id: int
    post_id: int
    community_key: str | None
    category: str | None
    kind: str | None
    headline: str | None
    summary: str | None
    students: list[str]
    confidence: float
    municipality_ids: list[str]
    unit_ids: list[str]
    school_names: list[str]
    published_at: str | None
    url: str
    review_status: str
    created_at: str
    text: str = ""
    is_repost: bool = False
    attachments: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Candidate:
        def _list(raw: str | None) -> list[str]:
            if not raw:
                return []
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return [str(x) for x in data]
            except (TypeError, ValueError):
                pass
            return [x for x in raw.split(",") if x]

        return cls(
            candidate_id=int(row["candidate_id"]),
            owner_id=int(row["owner_id"]),
            post_id=int(row["post_id"]),
            community_key=row.get("community_key"),
            category=row.get("category"),
            kind=row.get("kind"),
            headline=row.get("headline"),
            summary=row.get("summary"),
            students=_list(row.get("students")),
            confidence=float(row.get("confidence") or 0.0),
            municipality_ids=_list(row.get("municipality_ids")),
            unit_ids=_list(row.get("unit_ids")),
            school_names=_list(row.get("school_names")),
            published_at=row.get("published_at"),
            url=row.get("url") or f"https://vk.com/wall{row['owner_id']}_{row['post_id']}",
            review_status=row.get("review_status") or "pending",
            created_at=row.get("created_at") or "",
            text=row.get("text") or "",
            is_repost=bool(row.get("is_repost") or 0),
            attachments=_list(row.get("attachments")),
        )


async def upsert_candidate(
    db: Database,
    *,
    owner_id: int,
    post_id: int,
    community_key: str,
    kind: str,
    headline: str,
    summary: str,
    students: list[str],
    confidence: float,
    municipality_ids: list[str],
    unit_ids: list[str],
    school_names: list[str],
    published_at: str,
    url: str,
    classifier_version: str,
) -> int:
    now = to_iso(now_utc())
    await db.execute(
        """
        INSERT INTO achievement_candidates (owner_id, post_id, category, summary, classifier_version, review_status,
            created_at, confidence, kind, headline, students, community_key, municipality_ids, unit_ids,
            school_names, published_at, url)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_id, post_id) DO UPDATE SET
            category = excluded.category, summary = excluded.summary, classifier_version = excluded.classifier_version,
            confidence = excluded.confidence, kind = excluded.kind, headline = excluded.headline,
            students = excluded.students, municipality_ids = excluded.municipality_ids,
            unit_ids = excluded.unit_ids, school_names = excluded.school_names, url = excluded.url
        """,
        (
            owner_id,
            post_id,
            kind,
            summary,
            classifier_version,
            now,
            confidence,
            kind,
            headline,
            json.dumps(students, ensure_ascii=False),
            community_key,
            json.dumps(municipality_ids, ensure_ascii=False),
            json.dumps(unit_ids, ensure_ascii=False),
            json.dumps(school_names, ensure_ascii=False),
            published_at,
            url,
        ),
    )
    cid = await db.scalar(
        "SELECT candidate_id FROM achievement_candidates WHERE owner_id = ? AND post_id = ?",
        (owner_id, post_id),
    )
    return int(cid)


_CANDIDATE_SELECT = """
SELECT a.*, p.text, p.is_repost, p.attachments
FROM achievement_candidates a
JOIN posts p ON p.owner_id = a.owner_id AND p.post_id = a.post_id
"""


async def get_candidate(db: Database, candidate_id: int) -> Candidate | None:
    row = await db.fetchone(_CANDIDATE_SELECT + " WHERE a.candidate_id = ?", (candidate_id,))
    return Candidate.from_row(row) if row else None


async def list_pending_for_chat(
    db: Database,
    chat_id: int,
    since: datetime,
    min_confidence: float,
    limit: int = 2000,
) -> list[Candidate]:
    """Кандидаты, ещё не доставленные в чат, не отклонённые, опубликованные после `since`.

    Фильтр по территориям применяется в Python (municipality_ids хранится как JSON-список).
    """
    rows = await db.fetchall(
        _CANDIDATE_SELECT
        + """
        WHERE a.review_status <> 'rejected'
          AND a.published_at >= ?
          AND COALESCE(a.confidence, 0) >= ?
          AND NOT EXISTS (SELECT 1 FROM deliveries d
                          WHERE d.chat_id = ? AND d.owner_id = a.owner_id AND d.post_id = a.post_id)
        ORDER BY a.published_at
        LIMIT ?
        """,
        (to_iso(since), min_confidence, chat_id, limit),
    )
    return [Candidate.from_row(r) for r in rows]


async def record_delivery(
    db: Database, chat_id: int, owner_id: int, post_id: int, channel: str, message_id: int | None
) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO deliveries (chat_id, owner_id, post_id, sent_at, message_id, channel) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, owner_id, post_id, to_iso(now_utc()), message_id, channel),
    )


async def set_candidate_review(db: Database, candidate_id: int, status: str, reviewed_by: int | None) -> None:
    await db.execute(
        "UPDATE achievement_candidates SET review_status = ?, reviewed_at = ?, reviewed_by = ? WHERE candidate_id = ?",
        (status, to_iso(now_utc()), reviewed_by, candidate_id),
    )
    await db.commit()


async def count_candidates_since(db: Database, since: datetime, min_confidence: float) -> int:
    return int(
        await db.scalar(
            "SELECT COUNT(*) FROM achievement_candidates WHERE published_at >= ? AND review_status <> 'rejected' "
            "AND COALESCE(confidence, 0) >= ?",
            (to_iso(since), min_confidence),
        )
        or 0
    )


# ----------------------------------------------------------------------------
# Запуски сканирования
# ----------------------------------------------------------------------------


@dataclass
class ScanRun:
    run_id: int
    run_key: str | None
    kind: str
    window_start: str
    window_end: str
    total: int
    next_index: int
    posts_fetched: int
    posts_new: int
    achievements_found: int
    errors: int
    status: str
    started_at: str
    finished_at: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ScanRun:
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


async def get_run_by_key(db: Database, run_key: str) -> ScanRun | None:
    row = await db.fetchone("SELECT * FROM scan_runs WHERE run_key = ?", (run_key,))
    return ScanRun.from_row(row) if row else None


async def create_run(
    db: Database, *, run_key: str | None, kind: str, window_start: datetime, window_end: datetime, total: int
) -> ScanRun:
    cur = await db.execute(
        "INSERT INTO scan_runs (run_key, kind, window_start, window_end, total, started_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_key, kind, to_iso(window_start), to_iso(window_end), total, to_iso(now_utc())),
    )
    await db.commit()
    row = await db.fetchone("SELECT * FROM scan_runs WHERE run_id = ?", (cur.lastrowid,))
    assert row is not None
    return ScanRun.from_row(row)


async def update_run_progress(
    db: Database,
    run_id: int,
    *,
    next_index: int,
    posts_fetched: int = 0,
    posts_new: int = 0,
    achievements_found: int = 0,
    errors: int = 0,
) -> None:
    await db.execute(
        "UPDATE scan_runs SET next_index = ?, posts_fetched = posts_fetched + ?, posts_new = posts_new + ?, "
        "achievements_found = achievements_found + ?, errors = errors + ? WHERE run_id = ?",
        (next_index, posts_fetched, posts_new, achievements_found, errors, run_id),
    )
    await db.commit()


async def finish_run(db: Database, run_id: int, status: str = "done") -> None:
    await db.execute(
        "UPDATE scan_runs SET status = ?, finished_at = ? WHERE run_id = ?",
        (status, to_iso(now_utc()), run_id),
    )
    await db.commit()


async def list_unfinished_runs(db: Database) -> list[ScanRun]:
    rows = await db.fetchall("SELECT * FROM scan_runs WHERE status = 'running' ORDER BY started_at")
    return [ScanRun.from_row(r) for r in rows]


async def last_runs(db: Database, limit: int = 3) -> list[ScanRun]:
    rows = await db.fetchall("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT ?", (limit,))
    return [ScanRun.from_row(r) for r in rows]
