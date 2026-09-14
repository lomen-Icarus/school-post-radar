"""Прогон обновлений через настоящий Dispatcher aiogram с фиктивной сессией Telegram."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from radar.bot.app import create_dispatcher
from radar.classify.service import Classifier
from radar.config import Settings
from radar.db import repo
from radar.pipeline.notifier import Notifier
from radar.pipeline.scanner import Scanner


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        pass

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Radar", username="radar_bot")
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, SendMessage)]


def _update(update_id: int, chat: Chat, user: User, text: str) -> Update:
    message = Message(message_id=update_id, date=datetime.now(), chat=chat, from_user=user, text=text)
    return Update(update_id=update_id, message=message)


def _callback(update_id: int, chat: Chat, user: User, data: str, message_id: int = 1) -> Update:
    message = Message(message_id=message_id, date=datetime.now(), chat=chat, from_user=user, text="menu")
    query = CallbackQuery(id=str(update_id), from_user=user, chat_instance="ci", data=data, message=message)
    return Update(update_id=update_id, callback_query=query)


async def _make(db, settings: Settings):
    session = RecordingSession()
    bot = Bot(token="1:test", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    scanner = Scanner(db, None, Classifier(None), settings)
    notifier = Notifier(bot, db, settings)
    dp = create_dispatcher(settings, db, scanner=scanner, notifier=notifier, classifier=scanner.classifier)
    return dp, bot, session


async def test_admin_commands_rejected_for_non_admin(db, tmp_path):
    settings = Settings(bot_token="1:test", ADMIN_IDS="111", DB_PATH=str(tmp_path / "x.sqlite"))
    dp, bot, session = await _make(db, settings)
    stranger = User(id=999, is_bot=False, first_name="Некто")
    chat = Chat(id=999, type="private")
    await dp.feed_update(bot, _update(1, chat, stranger, "/stats"))
    assert session.sent_texts() == []
    admin = User(id=111, is_bot=False, first_name="Админ")
    await dp.feed_update(bot, _update(2, Chat(id=111, type="private"), admin, "/stats"))
    assert any("Статистика" in t for t in session.sent_texts())


async def test_private_start_creates_subscriber_and_menu(db, tmp_path):
    settings = Settings(bot_token="1:test", DB_PATH=str(tmp_path / "x.sqlite"))
    dp, bot, session = await _make(db, settings)
    user = User(id=5, is_bot=False, first_name="Оля", username="olya")
    await dp.feed_update(bot, _update(1, Chat(id=5, type="private"), user, "/start"))
    sub = await repo.get_subscriber(db, 5)
    assert sub is not None and sub.username == "olya" and sub.next_digest_at is not None
    texts = session.sent_texts()
    assert texts and "Радар школьных достижений" in texts[0] and "13:00, 21:00" in texts[0]


async def test_group_chat_becomes_subscriber(db, tmp_path):
    settings = Settings(bot_token="1:test", DB_PATH=str(tmp_path / "x.sqlite"))
    dp, bot, session = await _make(db, settings)
    user = User(id=7, is_bot=False, first_name="Пётр")
    group = Chat(id=-1001234567890, type="supergroup", title="Факультет")
    await dp.feed_update(bot, _update(1, group, user, "/start@radar_bot"))
    await dp.feed_update(bot, _update(2, group, user, "/regions"))
    sub = await repo.get_subscriber(db, -1001234567890)
    assert sub is not None and sub.first_name == "Факультет" and sub.username is None
    assert len(session.sent_texts()) == 2
    assert await repo.get_subscriber(db, 7) is None  # сам пользователь подписчиком не стал


async def test_commands_not_swallowed_by_fsm_and_tz_flow(db, tmp_path):
    settings = Settings(bot_token="1:test", DB_PATH=str(tmp_path / "x.sqlite"))
    dp, bot, session = await _make(db, settings)
    user = User(id=8, is_bot=False, first_name="Ира")
    chat = Chat(id=8, type="private")
    await dp.feed_update(bot, _update(1, chat, user, "/tz"))  # без аргумента — ждём ввод
    await dp.feed_update(bot, _update(2, chat, user, "/regions"))  # команда не должна проглатываться
    assert any("Территории" in t for t in session.sent_texts())
    await dp.feed_update(bot, _update(3, chat, user, "Asia/Yekaterinburg"))
    sub = await repo.get_subscriber(db, 8)
    assert sub is not None and sub.timezone == "Asia/Yekaterinburg"
    # Ввод с HTML-символами в «своё время» не ломает ответ
    await dp.feed_update(bot, _callback(4, chat, user, "sc:custom:"))
    await dp.feed_update(bot, _update(5, chat, user, "12:00 <b"))
    last = session.sent_texts()[-1]
    assert "&lt;b" in last and "<b" not in last.replace("<b>", "").replace("</b>", "").replace("<code>", "")


async def test_region_toggle_via_callback(db, tmp_path):
    settings = Settings(bot_token="1:test", DB_PATH=str(tmp_path / "x.sqlite"))
    dp, bot, session = await _make(db, settings)
    user = User(id=9, is_bot=False, first_name="Юра")
    chat = Chat(id=9, type="private")
    await dp.feed_update(bot, _update(1, chat, user, "/start"))
    await dp.feed_update(bot, _callback(2, chat, user, "rg:toggle:ru21-city-cheboksary"))
    sub = await repo.get_subscriber(db, 9)
    enabled = await repo.enabled_region_ids(db, sub.profile_id)
    assert "ru21-city-cheboksary" not in enabled and len(enabled) == 22
    assert any(isinstance(m, AnswerCallbackQuery) for m in session.requests)
    assert any(isinstance(m, EditMessageText) for m in session.requests)


async def test_access_whitelist_blocks_strangers(db, tmp_path):
    settings = Settings(bot_token="1:test", ALLOWED_USER_IDS="1", DB_PATH=str(tmp_path / "x.sqlite"))
    dp, bot, session = await _make(db, settings)
    stranger = User(id=2, is_bot=False, first_name="X")
    await dp.feed_update(bot, _update(1, Chat(id=2, type="private"), stranger, "/start"))
    assert session.sent_texts() == ["⛔️ Доступ к боту ограничен. Обратитесь к администратору."]
    assert await repo.get_subscriber(db, 2) is None
    await dp.feed_update(bot, _update(2, Chat(id=-5, type="group", title="g"), stranger, "просто текст"))
    assert len(session.sent_texts()) == 1  # в группе молчим
