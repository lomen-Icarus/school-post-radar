"""CLI: `radar run` (бот), `radar init-db`, `radar resolve`, `radar scan`, `radar classify`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from radar.config import Settings, get_settings


def _settings() -> Settings:
    try:
        return get_settings()
    except Exception as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(2)


async def _init_db(settings: Settings) -> None:
    from radar.db.bootstrap import initialize_database

    db = await initialize_database(settings.db_path, settings.registry_seed_path)
    total = await db.scalar("SELECT COUNT(*) FROM communities")
    unresolved = await db.scalar(
        "SELECT COUNT(*) FROM communities WHERE group_id IS NULL AND resolved_group_id IS NULL"
    )
    print(f"База готова: {settings.db_path} · сообществ {total}, без числового id {unresolved}")
    await db.close()


async def _resolve(settings: Settings) -> None:
    from radar.app import build_classifier, setup_logging
    from radar.db.bootstrap import initialize_database
    from radar.pipeline.scanner import Scanner
    from radar.vk.client import VkClient

    setup_logging(settings.log_level)
    db = await initialize_database(settings.db_path, settings.registry_seed_path)
    vk = VkClient(
        settings.vk_access_token,
        version=settings.vk_api_version,
        rps=settings.vk_rps,
        api_base=settings.vk_api_base,
        use_execute=settings.vk_use_execute,
    )
    scanner = Scanner(db, vk, build_classifier(settings), settings)
    n = await scanner.resolve_pending(limit=500)
    print(f"Разрешено: {n}")
    await vk.close()
    await db.close()


async def _scan(settings: Settings) -> None:
    from radar.app import build_classifier, setup_logging
    from radar.db.bootstrap import initialize_database
    from radar.pipeline.scanner import Scanner, TickStats
    from radar.vk.client import VkClient

    setup_logging(settings.log_level)
    db = await initialize_database(settings.db_path, settings.registry_seed_path)
    vk = VkClient(
        settings.vk_access_token,
        version=settings.vk_api_version,
        rps=settings.vk_rps,
        api_base=settings.vk_api_base,
        use_execute=settings.vk_use_execute,
    )
    scanner = Scanner(db, vk, build_classifier(settings), settings)

    async def progress(done: int, total: int, stats: TickStats) -> None:
        print(
            f"\r{done}/{total} · постов {stats.posts_fetched} · новых {stats.posts_new} · достижений {stats.achievements} · ошибок {stats.errors}",
            end="",
        )

    stats = await scanner.full_scan(progress)
    print()
    print(json.dumps(stats.__dict__, ensure_ascii=False))
    await vk.close()
    await db.close()


async def _check_token(settings: Settings) -> None:
    from radar.vk.client import VkClient

    if not settings.vk_enabled:
        print("VK_ACCESS_TOKEN не задан")
        sys.exit(1)
    vk = VkClient(
        settings.vk_access_token,
        version=settings.vk_api_version,
        rps=settings.vk_rps,
        api_base=settings.vk_api_base,
        use_execute=True,
    )
    try:
        # Гимназия №6 (Новочебоксарск), СОШ №9 (Алатырь), Гимназия №5 (Чебоксары)
        report = await vk.check_token(probe_owner_ids=[-10812563, -150859146, -180216915])
    finally:
        await vk.close()
    for method, status in report.items():
        print(f"{method:16} {status}")
    if not report.get("wall.get", "").startswith("ok"):
        print(
            "\nwall.get недоступен этому ключу — бот не сможет читать ленты. "
            "Нужен сервисный ключ приложения или пользовательский токен."
        )
        sys.exit(1)
    if report.get("execute") == "ok":
        print("\nexecute доступен: можно включить VK_USE_EXECUTE=1 (пакетирование запросов).")
    else:
        print(
            "\nexecute недоступен (ожидаемо для сервисного ключа) — бот будет делать одиночные вызовы wall.get."
        )


async def _classify(settings: Settings, text: str) -> None:
    from radar.app import build_classifier

    classifier = build_classifier(settings)
    result = await classifier.classify(text, [], None, force_llm=True)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="radar", description="School Post Radar")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="запустить бота (по умолчанию)")
    sub.add_parser("init-db", help="создать/обновить рабочую базу")
    sub.add_parser("resolve", help="разрешить короткие адреса ВК в числовые id")
    sub.add_parser("scan", help="полный обход сообществ без Telegram")
    sub.add_parser("check-token", help="проверить, какие методы VK доступны ключу")
    p_cls = sub.add_parser("classify", help="проверить классификатор на тексте")
    p_cls.add_argument("text", nargs="+")
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"
    if cmd == "run":
        from radar.app import run

        asyncio.run(run(_settings()))
    elif cmd == "init-db":
        asyncio.run(_init_db(_settings()))
    elif cmd == "resolve":
        asyncio.run(_resolve(_settings()))
    elif cmd == "scan":
        asyncio.run(_scan(_settings()))
    elif cmd == "check-token":
        asyncio.run(_check_token(_settings()))
    elif cmd == "classify":
        asyncio.run(_classify(_settings(), " ".join(args.text)))


if __name__ == "__main__":
    main()
