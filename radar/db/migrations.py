from __future__ import annotations

import logging
import re
from pathlib import Path

from radar.db.connection import Database

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME_RE.match(path.name)
        if not match:
            raise ValueError(f"Некорректное имя миграции: {path.name}")
        found.append((int(match.group(1)), path))
    return found


async def applied_versions(db: Database) -> set[int]:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await db.commit()
    rows = await db.fetchall("SELECT version FROM schema_migrations")
    return {int(r["version"]) for r in rows}


async def apply_migrations(db: Database, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Применить все ещё не применённые миграции. Каждая — в одной транзакции."""
    done = await applied_versions(db)
    applied: list[int] = []
    for version, path in list_migrations(directory):
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            f"INSERT INTO schema_migrations(version, applied_at) VALUES ({version}, "
            "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));\n"
            "COMMIT;"
        )
        try:
            await db.executescript(script)
        except Exception:
            # executescript не откатывает сам — доводим до чистого состояния.
            try:
                await db.execute("ROLLBACK")
            except Exception:  # транзакции может уже не быть
                pass
            log.exception("Ошибка применения миграции %s", path.name)
            raise
        applied.append(version)
        log.info("Применена миграция %s", path.name)
    return applied
