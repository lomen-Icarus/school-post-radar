from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from radar.bot import keyboards, texts
from radar.bot.callbacks import FormatCb, MenuCb, ModeCb, SchedCb
from radar.bot.handlers.common import edit_or_send, render_main
from radar.db import repo
from radar.db.connection import Database
from radar.pipeline.schedule import next_digest_after_settings_change
from radar.utils.timeutil import format_times, now_utc, parse_times, to_iso

router = Router(name="settings")


class ScheduleForm(StatesGroup):
    custom_times = State()
    timezone = State()


async def _reschedule(db: Database, chat_id: int, **fields: Any) -> repo.Subscriber:
    """Применить поля расписания и пересчитать next_digest_at от текущего момента."""
    current = await repo.get_subscriber(db, chat_id)
    assert current is not None
    times = fields.get("digest_times", current.digest_times)
    interval = int(fields.get("interval_days", current.interval_days))
    tz_name = fields.get("timezone", current.timezone)
    nxt = next_digest_after_settings_change(now_utc(), times, interval, tz_name)
    await repo.update_subscriber(db, chat_id, next_digest_at=to_iso(nxt), **fields)
    sub = await repo.get_subscriber(db, chat_id)
    assert sub is not None
    return sub


# ------------------------------------------------------------------ режим
@router.callback_query(MenuCb.filter(F.section == "mode"))
async def cb_mode(query: CallbackQuery, subscriber: repo.Subscriber) -> None:
    await edit_or_send(query, texts.mode_screen(subscriber), keyboards.mode_menu(subscriber))
    await query.answer()


@router.callback_query(ModeCb.filter())
async def cb_mode_set(
    query: CallbackQuery, callback_data: ModeCb, db: Database, subscriber: repo.Subscriber
) -> None:
    if callback_data.value not in ("digest", "instant"):
        await query.answer()
        return
    if callback_data.value == "digest":
        sub = await _reschedule(db, subscriber.chat_id, mode="digest")
    else:
        await repo.update_subscriber(db, subscriber.chat_id, mode="instant")
        sub = await repo.get_subscriber(db, subscriber.chat_id) or subscriber
    await edit_or_send(query, texts.mode_screen(sub), keyboards.mode_menu(sub))
    await query.answer("Режим сохранён")


# ------------------------------------------------------------- расписание
@router.callback_query(MenuCb.filter(F.section == "sched"))
async def cb_sched(query: CallbackQuery, subscriber: repo.Subscriber) -> None:
    await edit_or_send(query, texts.schedule_screen(subscriber), keyboards.schedule_menu(subscriber))
    await query.answer()


@router.callback_query(SchedCb.filter(F.action == "preset"))
async def cb_sched_preset(
    query: CallbackQuery, callback_data: SchedCb, db: Database, subscriber: repo.Subscriber
) -> None:
    preset = keyboards.SCHEDULE_PRESETS.get(callback_data.value)
    if preset is None:
        await query.answer()
        return
    sub = await _reschedule(db, subscriber.chat_id, digest_times=preset[1], mode="digest")
    await edit_or_send(query, texts.schedule_screen(sub), keyboards.schedule_menu(sub))
    await query.answer("Расписание сохранено")


@router.callback_query(SchedCb.filter(F.action == "interval"))
async def cb_sched_interval(
    query: CallbackQuery, callback_data: SchedCb, db: Database, subscriber: repo.Subscriber
) -> None:
    try:
        days = int(callback_data.value)
    except ValueError:
        await query.answer()
        return
    days = max(1, min(30, days))
    sub = await _reschedule(db, subscriber.chat_id, interval_days=days)
    await edit_or_send(query, texts.schedule_screen(sub), keyboards.schedule_menu(sub))
    await query.answer("Частота сохранена")


@router.callback_query(SchedCb.filter(F.action == "custom"))
async def cb_sched_custom(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScheduleForm.custom_times)
    if isinstance(query.message, Message):
        await query.message.answer(texts.ask_custom_times())
    await query.answer()


@router.message(ScheduleForm.custom_times, F.text, ~F.text.startswith("/"))
async def on_custom_times(
    message: Message, state: FSMContext, db: Database, subscriber: repo.Subscriber
) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "отмена":
        await state.clear()
        await render_main(db, message, subscriber, intro="Отменено.")
        return
    try:
        times = parse_times(raw)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}. Пример: <code>09:30, 21:00</code>")
        return
    if len(times) > 6:
        await message.answer("⚠️ Не больше 6 отправок в день.")
        return
    await state.clear()
    sub = await _reschedule(
        db, subscriber.chat_id, digest_times=format_times(times).replace(" ", ""), mode="digest"
    )
    await message.answer(texts.schedule_screen(sub), reply_markup=keyboards.schedule_menu(sub))


@router.callback_query(SchedCb.filter(F.action == "tz"))
async def cb_sched_tz(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScheduleForm.timezone)
    if isinstance(query.message, Message):
        await query.message.answer(texts.ask_timezone())
    await query.answer()


@router.message(ScheduleForm.timezone, F.text, ~F.text.startswith("/"))
async def on_timezone(message: Message, state: FSMContext, db: Database, subscriber: repo.Subscriber) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "отмена":
        await state.clear()
        await render_main(db, message, subscriber, intro="Отменено.")
        return
    await state.clear()
    await _apply_timezone(message, raw, db, subscriber)


@router.message(Command("tz"))
async def cmd_tz(message: Message, db: Database, subscriber: repo.Subscriber) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(texts.ask_timezone())
        return
    await _apply_timezone(message, parts[1].strip(), db, subscriber)


async def _apply_timezone(message: Message, raw: str, db: Database, subscriber: repo.Subscriber) -> None:
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        await message.answer("⚠️ Неизвестный часовой пояс. Пример: <code>Europe/Moscow</code>")
        return
    sub = await _reschedule(db, subscriber.chat_id, timezone=raw)
    await message.answer(texts.schedule_screen(sub), reply_markup=keyboards.schedule_menu(sub))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, db: Database, subscriber: repo.Subscriber) -> None:
    await state.clear()
    await render_main(db, message, subscriber, intro="Отменено.")


# ---------------------------------------------------------------- формат
@router.callback_query(MenuCb.filter(F.section == "format"))
async def cb_format(query: CallbackQuery, subscriber: repo.Subscriber) -> None:
    await edit_or_send(
        query,
        "<b>📇 Формат дайджеста</b>\n\nКак присылать найденные достижения?",
        keyboards.format_menu(subscriber),
    )
    await query.answer()


@router.callback_query(FormatCb.filter())
async def cb_format_set(
    query: CallbackQuery, callback_data: FormatCb, db: Database, subscriber: repo.Subscriber
) -> None:
    if callback_data.value in ("cards", "list"):
        await repo.update_subscriber(db, subscriber.chat_id, digest_format=callback_data.value)
    sub = await repo.get_subscriber(db, subscriber.chat_id) or subscriber
    await edit_or_send(
        query, "<b>📇 Формат дайджеста</b>\n\nКак присылать найденные достижения?", keyboards.format_menu(sub)
    )
    await query.answer("Сохранено")


@router.callback_query(MenuCb.filter(F.section == "empty"))
async def cb_toggle_empty(query: CallbackQuery, db: Database, subscriber: repo.Subscriber) -> None:
    await repo.update_subscriber(db, subscriber.chat_id, notify_empty=0 if subscriber.notify_empty else 1)
    sub = await repo.get_subscriber(db, subscriber.chat_id) or subscriber
    await edit_or_send(
        query, "<b>📇 Формат дайджеста</b>\n\nКак присылать найденные достижения?", keyboards.format_menu(sub)
    )
    await query.answer("Сохранено")
