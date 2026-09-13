"""Минимальный асинхронный клиент VK API с ограничением частоты и пакетированием через execute."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from radar.vk.models import VkPost, dumps_compact, parse_post

log = logging.getLogger(__name__)

DEFAULT_API_BASE = (
    "https://api.vk.ru/method/"  # api.vk.com по-прежнему отвечает, но официально сервер — api.vk.ru
)

# Коды ошибок VK, при которых сообщество нужно пометить как недоступное, а не повторять запрос.
PERMANENT_ERRORS = {15, 18, 19, 30, 100, 113, 200, 203}
# Ошибки, означающие проблему с токеном/приложением — повторять бессмысленно, нужно чинить конфиг.
AUTH_ERRORS = {5, 17, 28}
# Временные ошибки — повторяем с паузой.
TRANSIENT_ERRORS = {1, 6, 9, 10, 29}


class VkApiError(Exception):
    def __init__(self, code: int, message: str, method: str = "") -> None:
        super().__init__(f"VK API {method} error {code}: {message}")
        self.code = code
        self.message = message
        self.method = method

    @property
    def is_permanent(self) -> bool:
        return self.code in PERMANENT_ERRORS

    @property
    def is_auth(self) -> bool:
        return self.code in AUTH_ERRORS

    @property
    def is_transient(self) -> bool:
        return self.code in TRANSIENT_ERRORS


class RateLimiter:
    """Простой ограничитель: не чаще `rps` вызовов в секунду, вызовы сериализуются."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self._interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


@dataclass
class WallResult:
    owner_id: int
    posts: list[VkPost] = field(default_factory=list)
    error: VkApiError | None = None
    total_count: int | None = None


@dataclass
class GroupInfo:
    group_id: int
    screen_name: str | None
    name: str | None
    is_closed: int | None
    deactivated: str | None
    requested: str  # исходный идентификатор запроса


class VkClient:
    def __init__(
        self,
        token: str,
        *,
        version: str = "5.199",
        rps: float = 2.0,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 4,
        api_base: str = DEFAULT_API_BASE,
        use_execute: bool = False,
    ) -> None:
        self._token = token
        self._version = version
        self._limiter = RateLimiter(rps)
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_http = http is None
        self._max_retries = max_retries
        self._api_base = api_base if api_base.endswith("/") else api_base + "/"
        # None — ещё не проверяли; False — execute недоступен этому токену (сервисный ключ) или отключён.
        self.execute_supported: bool | None = None if use_execute else False

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def call(self, method: str, **params: Any) -> Any:
        """Вызов метода API с ограничением частоты и повторами при временных ошибках."""
        payload = {k: v for k, v in params.items() if v is not None}
        payload["v"] = self._version
        headers = {"Authorization": f"Bearer {self._token}"}  # рекомендованный способ передачи ключа (2026)
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.wait()
            try:
                resp = await self._http.post(self._api_base + method, data=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt > self._max_retries:
                    raise VkApiError(-1, f"сетевая ошибка: {exc}", method) from exc
                delay = min(2.0**attempt, 30.0)
                log.warning("VK %s: сетевая ошибка %s, повтор через %.0fс", method, exc, delay)
                await asyncio.sleep(delay)
                continue
            if "error" in data:
                err = data["error"]
                code = int(err.get("error_code", -1))
                msg = str(err.get("error_msg", ""))
                if code in TRANSIENT_ERRORS and attempt <= self._max_retries:
                    delay = min(1.5 * attempt, 10.0) if code == 6 else min(5.0 * attempt, 60.0)
                    log.warning("VK %s: ошибка %s (%s), повтор через %.1fс", method, code, msg, delay)
                    await asyncio.sleep(delay)
                    continue
                raise VkApiError(code, msg, method)
            # execute возвращает response + execute_errors на одном уровне
            if "execute_errors" in data:
                return data
            return data.get("response")

    # --------------------------------------------------------------- execute
    async def execute(self, code: str) -> tuple[list[Any], list[dict[str, Any]]]:
        data = await self.call("execute", code=code)
        if isinstance(data, dict) and "execute_errors" in data:
            return list(data.get("response") or []), list(data.get("execute_errors") or [])
        return list(data or []), []

    # -------------------------------------------------------------- wall.get
    async def wall_get(self, owner_id: int, count: int = 30) -> WallResult:
        try:
            data = await self.call("wall.get", owner_id=owner_id, count=count, filter="owner")
        except VkApiError as exc:
            return WallResult(owner_id=owner_id, error=exc)
        return _wall_result(owner_id, data)

    async def wall_get_many(
        self, owner_ids: list[int], count: int = 20, batch_size: int = 10
    ) -> dict[int, WallResult]:
        """Получить ленты нескольких сообществ. Пакеты до 25 через execute, при неудаче — по одному."""
        results: dict[int, WallResult] = {}
        batch_size = max(1, min(25, batch_size))
        for start in range(0, len(owner_ids), batch_size):
            chunk = owner_ids[start : start + batch_size]
            if self.execute_supported is False or len(chunk) == 1:
                for oid in chunk:
                    results[oid] = await self.wall_get(oid, count)
                continue
            try:
                batch = await self._wall_get_batch(chunk, count)
            except VkApiError as exc:
                if exc.is_auth or exc.code in (12, 13):
                    # 12/13 — ошибки компиляции/выполнения VKScript; 28 — метод недоступен токену
                    log.warning("execute недоступен (%s), переходим на одиночные вызовы", exc)
                    self.execute_supported = False
                    for oid in chunk:
                        results[oid] = await self.wall_get(oid, count)
                    continue
                for oid in chunk:
                    results[oid] = WallResult(owner_id=oid, error=exc)
                continue
            self.execute_supported = True
            results.update(batch)
        return results

    async def _wall_get_batch(self, owner_ids: list[int], count: int) -> dict[int, WallResult]:
        code = (
            f"var owners = {dumps_compact(owner_ids)};"
            "var i = 0; var out = [];"
            "while (i < owners.length) {"
            f'  out.push(API.wall.get({{"owner_id": owners[i], "count": {int(count)}, "filter": "owner"}}));'
            "  i = i + 1;"
            "}"
            "return out;"
        )
        response, errors = await self.execute(code)
        results: dict[int, WallResult] = {}
        error_iter = iter(errors)
        for idx, oid in enumerate(owner_ids):
            item = response[idx] if idx < len(response) else False
            if item is False or item is None:
                err = next(error_iter, None)
                if err is None:
                    vk_err = VkApiError(-2, "execute вернул false без описания ошибки", "wall.get")
                else:
                    vk_err = VkApiError(
                        int(err.get("error_code", -1)), str(err.get("error_msg", "")), "wall.get"
                    )
                results[oid] = WallResult(owner_id=oid, error=vk_err)
                continue
            results[oid] = _wall_result(oid, item)
        return results

    # ------------------------------------------------------------- groups
    async def groups_get_by_id(self, ids: list[str]) -> list[GroupInfo]:
        """groups.getById для списка id/коротких адресов (до 500). Поддерживает оба формата ответа."""
        if not ids:
            return []
        data = await self.call(
            "groups.getById", group_ids=",".join(ids), fields="screen_name,is_closed,deactivated,wall"
        )
        items: list[dict[str, Any]]
        if isinstance(data, dict):
            items = list(data.get("groups") or [])
        else:
            items = list(data or [])
        found: list[GroupInfo] = []
        for g in items:
            found.append(
                GroupInfo(
                    group_id=int(g["id"]),
                    screen_name=g.get("screen_name"),
                    name=g.get("name"),
                    is_closed=g.get("is_closed"),
                    deactivated=g.get("deactivated"),
                    requested="",
                )
            )
        # Сопоставляем запрошенные идентификаторы с найденными по screen_name / числовому id.
        by_screen = {(g.screen_name or "").lower(): g for g in found}
        by_id = {str(g.group_id): g for g in found}
        matched: list[GroupInfo] = []
        for requested in ids:
            key = requested.lower()
            g = by_screen.get(key) or by_id.get(key.removeprefix("club").removeprefix("public"))
            if g is None:
                continue
            matched.append(
                GroupInfo(
                    group_id=g.group_id,
                    screen_name=g.screen_name,
                    name=g.name,
                    is_closed=g.is_closed,
                    deactivated=g.deactivated,
                    requested=requested,
                )
            )
        return matched

    async def check_token(self, probe_owner_id: int = -1) -> dict[str, Any]:
        """Диагностика ключа: какие методы доступны. Возвращает словарь method -> 'ok' | текст ошибки."""
        report: dict[str, Any] = {}
        for method, params in (
            ("groups.getById", {"group_ids": str(-probe_owner_id), "fields": "is_closed,wall"}),
            ("wall.get", {"owner_id": probe_owner_id, "count": 1, "filter": "owner"}),
            ("execute", {"code": "return API.users.get({});"}),
        ):
            try:
                await self.call(method, **params)
                report[method] = "ok"
            except VkApiError as exc:
                report[method] = f"ошибка {exc.code}: {exc.message}"
        return report

    async def resolve_screen_name(self, screen_name: str) -> GroupInfo | None:
        data = await self.call("utils.resolveScreenName", screen_name=screen_name)
        if not data or not isinstance(data, dict) or data.get("type") not in ("group", "page", "event"):
            return None
        gid = int(data["object_id"])
        infos = await self.groups_get_by_id([str(gid)])
        if infos:
            info = infos[0]
            info.requested = screen_name
            return info
        return GroupInfo(
            group_id=gid,
            screen_name=screen_name,
            name=None,
            is_closed=None,
            deactivated=None,
            requested=screen_name,
        )


def _wall_result(owner_id: int, data: Any) -> WallResult:
    if not isinstance(data, dict):
        return WallResult(
            owner_id=owner_id, error=VkApiError(-3, f"неожиданный ответ: {type(data).__name__}", "wall.get")
        )
    posts: list[VkPost] = []
    for item in data.get("items") or []:
        try:
            posts.append(parse_post(item))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Не удалось разобрать пост %s: %s", item.get("id"), exc)
    return WallResult(owner_id=owner_id, posts=posts, total_count=data.get("count"))
