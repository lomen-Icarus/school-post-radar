#!/usr/bin/env python3
"""Управление сервером Pterodactyl через Client API (используется GitHub Actions).

Режимы:
  inspect    — показать состояние сервера, переменные Startup и файлы (ничего не меняет);
  bootstrap  — прописать переменные Startup для автодеплоя из git и залить код, если его ещё нет;
  deploy     — перезапустить сервер (при старте яйцо делает git pull + pip install) и дождаться запуска.

Окружение: PANEL_URL, SERVER_ID, API_KEY. Только стандартная библиотека.
Логи Actions в публичном репозитории видны всем: значения переменных печатаются только из белого списка,
содержимое файлов не читается.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

REPO_URL = os.environ.get("REPO_URL", "https://github.com/lomen-Icarus/school-post-radar")
BRANCH = os.environ.get("DEPLOY_BRANCH") or "main"
SHA = (os.environ.get("GITHUB_SHA") or "")[:7]

# Переменные яйца Python Generic, которые нужны для автодеплоя.
WANTED_VARS = {
    "GIT_ADDRESS": REPO_URL,
    "BRANCH": BRANCH,
    "AUTO_UPDATE": "1",
    "USER_UPLOAD": "0",
    "PY_FILE": "bot.py",
    "REQUIREMENTS_FILE": "requirements.txt",
}
# Значения этих переменных безопасно печатать в публичный лог.
PRINTABLE_VARS = set(WANTED_VARS) | {"PY_PACKAGES"}
# Файлы, которые нельзя терять при переустановке кода.
PRECIOUS = (".env", "data/radar.sqlite")


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status


class Panel:
    def __init__(self, url: str, server_id: str, key: str) -> None:
        self.base = f"{url.rstrip('/')}/api/client/servers/{server_id}"
        self.key = key

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        retries: int = 3,
        *,
        raw_body: bytes | None = None,
        raw_response: bool = False,
    ) -> Any:
        data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        content_type = "text/plain" if raw_body is not None else "application/json"
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(
                self.base + path,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.key}",
                    "Accept": "application/json",
                    "Content-Type": content_type,
                    "User-Agent": "school-post-radar-deploy",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = resp.read()
                    if raw_response:
                        return payload
                    raw = payload.decode() or "null"
                    return json.loads(raw) if raw.strip().startswith(("{", "[")) else None
            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace")
                if exc.code in (429, 502, 503, 504) and attempt < retries:
                    time.sleep(3 * attempt)
                    continue
                raise ApiError(exc.code, text) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < retries:
                    time.sleep(3 * attempt)
                    continue
                raise ApiError(-1, str(exc)) from None
        raise AssertionError("unreachable")

    # ---------------------------------------------------------------- reads
    def info(self) -> dict[str, Any]:
        return self.request("GET", "")["attributes"]

    def state(self) -> str:
        try:
            return self.request("GET", "/resources")["attributes"]["current_state"]
        except ApiError as exc:
            print(f"  resources: {exc}")
            return "unknown"

    def startup(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        data = self.request("GET", "/startup")
        variables = {v["attributes"]["env_variable"]: v["attributes"] for v in data.get("data", [])}
        return variables, data.get("meta", {})

    def ls(self, directory: str = "/") -> list[dict[str, Any]]:
        q = urllib.parse.quote(directory, safe="")
        return [f["attributes"] for f in self.request("GET", f"/files/list?directory={q}")["data"]]

    # --------------------------------------------------------------- writes
    def set_var(self, key: str, value: str) -> None:
        self.request("PUT", "/startup/variable", {"key": key, "value": value})

    def power(self, signal: str) -> None:
        self.request("POST", "/power", {"signal": signal})

    def reinstall(self) -> None:
        self.request("POST", "/settings/reinstall")

    # Содержимое файлов никогда не печатается: в .env лежат токены.
    def read_file(self, path: str) -> bytes:
        q = urllib.parse.quote(path, safe="")
        return self.request("GET", f"/files/contents?file={q}", raw_response=True)

    def write_file(self, path: str, content: bytes) -> None:
        q = urllib.parse.quote(path, safe="")
        self.request("POST", f"/files/write?file={q}", raw_body=content)

    def delete(self, names_: list[str]) -> None:
        self.request("POST", "/files/delete", {"root": "/", "files": names_})


def names(files: list[dict[str, Any]]) -> list[str]:
    return sorted(f["name"] + ("" if f.get("is_file", True) else "/") for f in files)


def show_vars(variables: dict[str, dict[str, Any]]) -> None:
    for env, v in sorted(variables.items()):
        value = v.get("server_value")
        shown = repr(value) if env in PRINTABLE_VARS else ("<задано>" if value else "<пусто>")
        editable = "" if v.get("is_editable") else "  (только админ)"
        print(f"  {env:18} = {shown}{editable}")


def inspect(p: Panel) -> dict[str, Any]:
    info = p.info()
    print(f"Сервер: {info.get('name')!r}, node={info.get('node')!r}, image={info.get('docker_image')!r}")
    print(f"Состояние: {p.state()}")
    variables, meta = p.startup()
    print(f"Startup: {meta.get('raw_startup_command')!r}")
    print("Переменные:")
    show_vars(variables)
    root = p.ls("/")
    print(f"Файлы в /: {names(root) or '(пусто)'}")
    has = {f["name"] for f in root}
    if "data" in has:
        print(f"Файлы в /data: {names(p.ls('/data'))}")
    return {"variables": variables, "root": root}


def wait_running(p: Panel, *, require_down: bool, timeout: int = 300) -> bool:
    """Дождаться запуска. running засчитывается после того, как сервер был остановлен/запускался."""
    seen_down = not require_down
    starting = 0
    deadline = time.monotonic() + timeout
    i = 0
    while time.monotonic() < deadline:
        time.sleep(5)
        i += 1
        state = p.state()
        print(f"  [{i}] state={state}")
        if state == "running" and (seen_down or i >= 12):
            return True
        if state == "starting":
            seen_down = True
            starting += 1
            if starting >= 12:
                print(
                    "::warning::Сервер больше минуты в starting. Если бот работает — "
                    "попросите админа задать в яйце строку готовности «Бот запущен»."
                )
                return True
        else:
            starting = 0
            if state in ("stopping", "offline"):
                seen_down = True
    return False


def deploy(p: Panel) -> int:
    has_git = any(f["name"] == ".git" for f in p.ls("/"))
    if not has_git:
        print(
            "::error::В контейнере нет .git — git pull не сработает. Запустите workflow в режиме bootstrap."
        )
        return 1
    try:
        p.power("restart")
        print("restart отправлен")
    except ApiError as exc:
        if exc.status not in (409, 412):
            raise
        print(f"restart -> {exc.status}, сервер выключен — запускаю")
        time.sleep(3)
        p.power("start")
    if wait_running(p, require_down=True):
        print(f"Готово: сервер запущен, ожидаемый коммит {SHA}")
        return 0
    print("::error::Сервер не запустился за 5 минут — смотрите консоль панели")
    return 1


def bootstrap(p: Panel) -> int:
    state = inspect(p)
    variables: dict[str, dict[str, Any]] = state["variables"]
    missing = [k for k in ("GIT_ADDRESS", "AUTO_UPDATE", "PY_FILE") if k not in variables]
    if missing:
        print(f"::error::У яйца нет переменных {missing} — это не Python Generic. Нужна смена яйца админом.")
        return 1
    print("Настраиваю переменные Startup:")
    for key, value in WANTED_VARS.items():
        var = variables.get(key)
        if var is None:
            print(f"  {key}: нет в яйце — пропуск")
            continue
        if str(var.get("server_value") or "") == value:
            print(f"  {key}: уже {value!r}")
            continue
        if not var.get("is_editable"):
            print(f"::error::{key} нельзя менять с клиентского ключа (не editable). Нужен админ панели.")
            return 1
        p.set_var(key, value)
        print(f"  {key}: -> {value!r}")

    root = p.ls("/")
    present = {f["name"] for f in root}
    if ".git" in present:
        print("Код уже получен через git — обычный деплой.")
        return deploy(p)
    # Пустой каталог (допускаем служебные файлы панели/pip): переустановка клонирует репозиторий.
    ignorable = {".local", ".cache", ".config", ".pip", ".bash_history", ".python_history"}
    leftover = sorted(present - ignorable)
    stashed_env: bytes | None = None
    if leftover == [".env"]:
        # Установщик клонирует только в пустой каталог: .env держим в памяти раннера и вернём после клона.
        stashed_env = p.read_file("/.env")
        print(f"Найден .env ({len(stashed_env)} байт) — временно убираю его на время клонирования")
        leftover = []
    if leftover:
        print(
            "::error::Каталог сервера не пуст и в нём нет .git: "
            f"{leftover}. Автоматически ничего не удаляю. Сохраните {list(PRECIOUS)} (если есть), "
            "очистите каталог в Files и запустите bootstrap снова."
        )
        return 1
    print("Каталог пуст — запускаю переустановку (git clone + pip install).")
    if p.state() != "offline":
        try:
            p.power("stop")
        except ApiError as exc:
            print(f"  stop: {exc}")
        for _ in range(30):
            time.sleep(3)
            if p.state() == "offline":
                break
    if stashed_env is not None:
        p.delete([".env"])
    try:
        p.reinstall()
    except ApiError:
        if stashed_env is not None:
            p.write_file("/.env", stashed_env)
            print(".env возвращён на место")
        raise
    # Во время установки API отвечает 409; ждём, пока появится .git.
    for i in range(1, 61):
        time.sleep(10)
        try:
            files = p.ls("/")
        except ApiError as exc:
            print(f"  [{i}] установка идёт ({exc.status})")
            continue
        if any(f["name"] == ".git" for f in files) and any(f["name"] == "bot.py" for f in files):
            try:
                p.request("GET", "/files/list?directory=%2Fradar")
            except ApiError:
                continue
            print(f"  [{i}] код загружен: {names(files)}")
            break
        print(f"  [{i}] ждём файлы… {names(files)}")
    else:
        if stashed_env is not None:
            p.write_file("/.env", stashed_env)
            print(".env возвращён на место")
        print("::error::Переустановка не завершилась за 10 минут — смотрите консоль панели")
        return 1
    if stashed_env is not None:
        p.write_file("/.env", stashed_env)
        print(".env возвращён на место")
    time.sleep(20)  # дать установщику закончить pip install
    for _ in range(30):
        try:
            p.power("start")
            break
        except ApiError as exc:
            if exc.status != 409:
                raise
            print("  установщик ещё работает, ждём…")
            time.sleep(10)
    if wait_running(p, require_down=False):
        print(f"Сервер запущен, коммит {SHA}")
        if stashed_env is None:
            print(
                "::warning::Создайте .env в Files (BOT_TOKEN, ADMIN_IDS, VK_ACCESS_TOKEN, …): "
                "без него бот завершится с ошибкой конфигурации."
            )
        return 0
    print("::error::Сервер не запустился — смотрите консоль панели (скорее всего нет .env)")
    return 1


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "deploy").strip()
    url = os.environ.get("PANEL_URL") or "https://panel.frienworld.space"
    server = os.environ.get("SERVER_ID") or "75511c5e"
    key = os.environ.get("API_KEY") or ""
    if not key:
        print("::error::Не задан секрет PTERODACTYL_API_KEY (Client API key панели)")
        return 1
    p = Panel(url, server, key)
    try:
        if mode == "inspect":
            inspect(p)
            return 0
        if mode == "bootstrap":
            return bootstrap(p)
        if mode == "deploy":
            return deploy(p)
    except ApiError as exc:
        hint = ""
        if exc.status in (401, 403):
            hint = " — ключ неверный, не клиентский (нужен ptlc_…) или у пользователя нет доступа к серверу"
        elif exc.status == 404:
            hint = " — проверьте SERVER_ID и PANEL_URL"
        print(f"::error::Ошибка API панели: {exc}{hint}")
        return 1
    print(f"::error::Неизвестный режим {mode!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
