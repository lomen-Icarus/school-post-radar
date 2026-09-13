from __future__ import annotations

import logging
import shutil
from pathlib import Path

from radar.db.connection import Database
from radar.db.migrations import apply_migrations
from radar.registry.municipalities import MUNICIPALITIES, municipality_for_area_source
from radar.utils.timeutil import now_utc, to_iso

log = logging.getLogger(__name__)


def ensure_database_file(db_path: Path, seed_path: Path) -> bool:
    """Если рабочей базы нет — копируем реестр-заготовку. Возвращает True, если скопировали."""
    if db_path.exists():
        return False
    if not seed_path.exists():
        raise FileNotFoundError(f"Нет ни рабочей базы {db_path}, ни заготовки реестра {seed_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed_path, db_path)
    log.info("Создана рабочая база %s из %s", db_path, seed_path)
    return True


async def seed_municipalities(db: Database) -> None:
    now = to_iso(now_utc())
    await db.executemany(
        """
        INSERT INTO municipalities (municipality_id, name, short_name, municipality_type, sort_order, checked_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(municipality_id) DO UPDATE SET
            name = excluded.name, short_name = excluded.short_name,
            municipality_type = excluded.municipality_type, sort_order = excluded.sort_order
        """,
        [
            (m.municipality_id, m.name, m.short_name, m.municipality_type, m.sort_order, now)
            for m in MUNICIPALITIES
        ],
    )
    await db.commit()


async def map_units_to_municipalities(db: Database) -> tuple[int, list[str]]:
    """Проставить units.municipality_id по area_source. Возвращает (обновлено, нераспознанные area_source)."""
    rows = await db.fetchall("SELECT unit_id, area_source FROM units WHERE municipality_id IS NULL")
    unknown: set[str] = set()
    updates: list[tuple[str, str]] = []
    for row in rows:
        mid = municipality_for_area_source(row["area_source"])
        if mid is None:
            unknown.add(row["area_source"] or "")
            continue
        updates.append((mid, row["unit_id"]))
    if updates:
        await db.executemany("UPDATE units SET municipality_id = ? WHERE unit_id = ?", updates)
        await db.commit()
    if unknown:
        log.warning("Не распознаны районы: %s", sorted(unknown))
    return len(updates), sorted(unknown)


async def initialize_database(db_path: Path, seed_path: Path) -> Database:
    """Полная инициализация: файл -> миграции -> справочники."""
    ensure_database_file(db_path, seed_path)
    db = await Database(db_path).connect()
    applied = await apply_migrations(db)
    if applied:
        log.info("Применены миграции: %s", applied)
    await seed_municipalities(db)
    updated, _unknown = await map_units_to_municipalities(db)
    if updated:
        log.info("Сопоставлено площадок с округами: %d", updated)
    return db
