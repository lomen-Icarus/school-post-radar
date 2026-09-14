from __future__ import annotations

import logging
import re
from pathlib import Path

from radar.db.connection import Database
from radar.utils.timeutil import now_utc, to_iso

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
_ALTER_ADD_RE = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE)


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME_RE.match(path.name)
        if not match:
            raise ValueError(f"Некорректное имя миграции: {path.name}")
        found.append((int(match.group(1)), path))
    return found


def split_statements(sql: str) -> list[str]:
    """Разбить SQL-файл на операторы по `;` вне строковых литералов; комментарии `--` отбрасываются."""
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":  # экранированная кавычка ''
                    buf.append("'")
                    i += 1
                else:
                    in_string = False
        elif ch == "'":
            in_string = True
            buf.append(ch)
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end == -1 else end
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


async def column_exists(db: Database, table: str, column: str) -> bool:
    rows = await db.fetchall(f"PRAGMA table_info({table})")
    return any(str(r["name"]).lower() == column.lower() for r in rows)


async def applied_versions(db: Database) -> set[int]:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await db.commit()
    rows = await db.fetchall("SELECT version FROM schema_migrations")
    return {int(r["version"]) for r in rows}


async def apply_migrations(db: Database, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Применить все ещё не применённые миграции. Каждая — в одной транзакции.

    `ALTER TABLE ... ADD COLUMN` пропускается, если колонка уже есть: так миграция ложится и на
    реестр-заготовку, в которой часть схемы бота уже присутствует.
    """
    done = await applied_versions(db)
    applied: list[int] = []
    for version, path in list_migrations(directory):
        if version in done:
            continue
        statements = split_statements(path.read_text(encoding="utf-8"))
        await db.execute("BEGIN")
        try:
            for stmt in statements:
                match = _ALTER_ADD_RE.match(stmt)
                if match and await column_exists(db, match.group(1), match.group(2)):
                    log.info(
                        "Миграция %s: колонка %s.%s уже есть — пропуск",
                        path.name,
                        match.group(1),
                        match.group(2),
                    )
                    continue
                await db.execute(stmt)
            await db.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, to_iso(now_utc())),
            )
            await db.execute("COMMIT")
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:  # транзакции может уже не быть
                pass
            log.exception("Ошибка применения миграции %s", path.name)
            raise
        applied.append(version)
        log.info("Применена миграция %s", path.name)
    return applied
