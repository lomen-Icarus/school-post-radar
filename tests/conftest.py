from __future__ import annotations

import json
import time as _time
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest

from radar.config import Settings
from radar.db.bootstrap import initialize_database
from radar.db.connection import Database

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "registry_seed.sqlite"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="1:test",
        VK_ACCESS_TOKEN="token",
        DB_PATH=str(tmp_path / "radar.sqlite"),
        REGISTRY_SEED_PATH=str(SEED),
        ANTHROPIC_API_KEY="",
        CLASSIFIER_EFFORT="low",
        ADMIN_IDS="",
        ALLOWED_USER_IDS="",
    )


@pytest.fixture
async def db(settings: Settings) -> Database:
    database = await initialize_database(settings.db_path, settings.registry_seed_path)
    yield database
    await database.close()


class FakeVk:
    """Мок VK API поверх httpx.MockTransport. Управляется словарём walls: owner_id -> список постов / ошибка."""

    def __init__(self) -> None:
        self.walls: dict[int, list[dict] | dict] = {}
        self.groups: dict[str, dict] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail_execute = False
        self.error_6_once = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        form = {k: unquote_plus(v) for k, v in (x.split("=", 1) for x in request.content.decode().split("&"))}
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, form))
        if self.error_6_once:
            self.error_6_once = False
            return httpx.Response(
                200, json={"error": {"error_code": 6, "error_msg": "Too many requests per second"}}
            )
        if method == "execute":
            if self.fail_execute:
                return httpx.Response(
                    200,
                    json={
                        "error": {"error_code": 28, "error_msg": "method is unavailable with service token"}
                    },
                )
            owners = json.loads(form["code"].split("var owners = ")[1].split(";")[0])
            resp, errors = [], []
            for o in owners:
                wall = self.walls.get(o)
                if isinstance(wall, dict):
                    resp.append(False)
                    errors.append({"method": "wall.get", **wall})
                else:
                    resp.append({"count": len(wall or []), "items": wall or []})
            payload = {"response": resp}
            if errors:
                payload["execute_errors"] = errors
            return httpx.Response(200, json=payload)
        if method == "wall.get":
            o = int(form["owner_id"])
            wall = self.walls.get(o)
            if isinstance(wall, dict):
                return httpx.Response(200, json={"error": wall})
            return httpx.Response(200, json={"response": {"count": len(wall or []), "items": wall or []}})
        if method == "groups.getById":
            ids = form["group_ids"].split(",")
            found = [self.groups[i] for i in ids if i in self.groups]
            if len(found) != len(ids) and len(ids) > 1:
                return httpx.Response(
                    200, json={"error": {"error_code": 100, "error_msg": "invalid group_ids"}}
                )
            if not found:
                return httpx.Response(
                    200, json={"error": {"error_code": 100, "error_msg": "invalid group_ids"}}
                )
            return httpx.Response(200, json={"response": {"groups": found}})
        return httpx.Response(200, json={"response": None})


@pytest.fixture
def fake_vk() -> FakeVk:
    return FakeVk()


def make_post(owner_id: int, post_id: int, text: str, age_seconds: int = 3600, **extra) -> dict:
    return {
        "id": post_id,
        "owner_id": owner_id,
        "date": int(_time.time()) - age_seconds,
        "text": text,
        "post_type": "post",
        **extra,
    }
