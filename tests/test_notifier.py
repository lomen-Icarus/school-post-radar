from datetime import timedelta

from radar.bot import formatting
from radar.config import Settings
from radar.db import repo
from radar.pipeline.notifier import Notifier
from radar.utils.timeutil import from_iso, now_utc, to_iso


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

        class Msg:
            message_id = len(self.sent)

        return Msg()


async def _candidate(db, owner_id: int, post_id: int, mids: list[str], conf: float = 0.9) -> int:
    now = now_utc()
    await repo.insert_post(
        db,
        owner_id=owner_id,
        post_id=post_id,
        community_key="vk:10812563",
        published_at=to_iso(now),
        url=f"https://vk.com/wall{owner_id}_{post_id}",
        text="t",
        content_hash="h",
        is_repost=False,
        attachments=[],
    )
    cid = await repo.upsert_candidate(
        db,
        owner_id=owner_id,
        post_id=post_id,
        community_key="vk:10812563",
        kind="olympiad",
        headline="Победа",
        summary="Ученики победили",
        students=["Иванова А."],
        confidence=conf,
        municipality_ids=mids,
        unit_ids=["u"],
        school_names=["Школа №1"],
        published_at=to_iso(now),
        url=f"https://vk.com/wall{owner_id}_{post_id}",
        classifier_version="test",
    )
    await db.commit()
    return cid


async def test_digest_respects_regions_and_dedup(db, settings: Settings):
    bot = FakeBot()
    notifier = Notifier(bot, db, settings)  # type: ignore[arg-type]
    sub, _ = await repo.upsert_subscriber(db, 1, None, None, "13:00,21:00", "Europe/Moscow")
    await repo.set_region_enabled(db, sub.profile_id, "ru21-city-cheboksary", False)
    a = await _candidate(db, -1, 1, ["ru21-city-cheboksary"])
    b = await _candidate(db, -2, 1, ["ru21-mo-kanash"])
    await _candidate(db, -3, 1, ["ru21-mo-kanash"], conf=0.3)  # ниже порога
    sub = await repo.get_subscriber(db, 1)
    n = await notifier.deliver_digest(sub)
    assert n == 1
    assert len(bot.sent) == 2  # заголовок + карточка
    assert "Победа" in bot.sent[1][1] and "reply_markup" in bot.sent[1][2]
    sub = await repo.get_subscriber(db, 1)
    assert sub.next_digest_at is not None and sub.last_digest_at is not None
    assert from_iso(sub.next_digest_at) > now_utc()
    # второй раз — пусто, ничего не шлём (notify_empty=0)
    assert await notifier.deliver_digest(sub) == 0
    assert len(bot.sent) == 2
    # включаем Чебоксары — доедет карточка a
    await repo.set_region_enabled(db, sub.profile_id, "ru21-city-cheboksary", True)
    assert await notifier.deliver_digest(await repo.get_subscriber(db, 1)) == 1
    delivered = await db.fetchall("SELECT owner_id FROM deliveries WHERE chat_id = 1 ORDER BY owner_id")
    assert [d["owner_id"] for d in delivered] == [-2, -1]
    assert a != b


async def test_instant_delivery_only_instant_mode(db, settings: Settings):
    bot = FakeBot()
    notifier = Notifier(bot, db, settings)  # type: ignore[arg-type]
    await repo.upsert_subscriber(db, 1, None, None, "13:00", "Europe/Moscow")
    await repo.upsert_subscriber(db, 2, None, None, "13:00", "Europe/Moscow")
    await repo.update_subscriber(db, 2, mode="instant")
    cid = await _candidate(db, -5, 1, ["ru21-mo-kanash"])
    sent = await notifier.deliver_instant([cid])
    assert sent == 1 and bot.sent[0][0] == 2
    assert await notifier.deliver_instant([cid]) == 0  # без дублей


async def test_list_format_splits_long_digest(db, settings: Settings):
    items = []
    for i in range(120):
        cid = await _candidate(db, -100 - i, 1, ["ru21-mo-kanash" if i % 2 else "ru21-city-cheboksary"])
        items.append(await repo.get_candidate(db, cid))
    msgs = formatting.format_digest_list(
        items, "Europe/Moscow", formatting.digest_header(120, "Europe/Moscow")
    )
    assert len(msgs) >= 2 and all(len(m) <= 4000 for m in msgs)
    assert msgs[0].startswith("<b>🏆 Дайджест: 120 достижений</b>")
    assert formatting.digest_header(1, "Europe/Moscow").startswith("<b>🏆 Дайджест: 1 достижение</b>")
    assert "3 достижения" in formatting.digest_header(3, "Europe/Moscow")
    assert "11 достижений" in formatting.digest_header(11, "Europe/Moscow")


async def test_card_escapes_html(db, settings: Settings):
    cid = await _candidate(db, -7, 1, ["ru21-mo-kanash"])
    c = await repo.get_candidate(db, cid)
    c.headline = "<script>alert(1)</script>"
    text = formatting.format_card(c, "Europe/Moscow")
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "Канашский" in text and "Школа №1" in text
    assert (now_utc() - timedelta(minutes=1)) < now_utc()
