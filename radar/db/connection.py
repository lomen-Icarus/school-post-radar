from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)


class Database:
    """Тонкая обёртка над одним aiosqlite-подключением.

    aiosqlite сериализует запросы через собственный поток, поэтому одно подключение
    безопасно делить между обработчиками бота и фоновыми задачами.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> Database:
        self._conn = await aiosqlite.connect(self.path, timeout=30)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA busy_timeout = 30000")
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не подключена")
        return self._conn

    async def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, seq: Iterable[Iterable[Any] | dict[str, Any]]) -> None:
        await self.conn.executemany(sql, seq)

    async def executescript(self, sql: str) -> None:
        await self.conn.executescript(sql)

    async def fetchone(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def scalar(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> Any:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row is not None else None

    async def commit(self) -> None:
        await self.conn.commit()

    async def rollback(self) -> None:
        await self.conn.rollback()
