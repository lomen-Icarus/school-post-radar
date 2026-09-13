from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from radar.bot.callbacks import FeedbackCb, FormatCb, MenuCb, ModeCb, NoopCb, RegionCb, SchedCb
from radar.db.repo import RegionState, Subscriber


def main_menu(sub: Subscriber, enabled_regions: int, total_regions: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    mode_label = "⚡ Режим: сразу" if sub.mode == "instant" else "📦 Режим: дайджест"
    kb.button(text=mode_label, callback_data=MenuCb(section="mode"))
    kb.button(text="🕐 Расписание", callback_data=MenuCb(section="sched"))
    kb.button(text=f"🗺 Территории {enabled_regions}/{total_regions}", callback_data=MenuCb(section="regions"))
    kb.button(text="📇 Формат", callback_data=MenuCb(section="format"))
    kb.button(text="📊 Статус", callback_data=MenuCb(section="status"))
    if sub.is_active:
        kb.button(text="⏸ Пауза", callback_data=MenuCb(section="pause"), style="danger")
    else:
        kb.button(text="▶️ Возобновить", callback_data=MenuCb(section="resume"), style="success")
    kb.button(text="❓ Помощь", callback_data=MenuCb(section="help"))
    kb.adjust(1, 2, 2, 2)
    return kb.as_markup()


def back_row(kb: InlineKeyboardBuilder, section: str = "main") -> None:
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=MenuCb(section=section).pack()))


def mode_menu(sub: Subscriber) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for value, label in (("digest", "📦 Дайджест по расписанию"), ("instant", "⚡ Сразу, как найдём")):
        mark = "✅ " if sub.mode == value else ""
        kb.button(
            text=f"{mark}{label}",
            callback_data=ModeCb(value=value),
            style="primary" if sub.mode == value else None,
        )
    kb.adjust(1)
    back_row(kb)
    return kb.as_markup()


SCHEDULE_PRESETS: dict[str, tuple[str, str]] = {
    "1x13": ("1 раз в день · 13:00", "13:00"),
    "1x21": ("1 раз в день · 21:00", "21:00"),
    "2x": ("2 раза в день · 13:00 и 21:00", "13:00,21:00"),
    "3x": ("3 раза в день · 09:00, 13:00, 21:00", "09:00,13:00,21:00"),
}


def schedule_menu(sub: Subscriber) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (label, times) in SCHEDULE_PRESETS.items():
        active = sub.digest_times == times
        kb.button(text=("✅ " if active else "") + label, callback_data=SchedCb(action="preset", value=key))
    kb.adjust(1)
    interval_row = []
    for days, label in ((1, "Каждый день"), (2, "Раз в 2 дня"), (3, "Раз в 3 дня"), (7, "Раз в неделю")):
        mark = "✅ " if sub.interval_days == days else ""
        interval_row.append(
            InlineKeyboardButton(
                text=f"{mark}{label}", callback_data=SchedCb(action="interval", value=str(days)).pack()
            )
        )
    kb.row(*interval_row[:2])
    kb.row(*interval_row[2:])
    kb.row(
        InlineKeyboardButton(text="✏️ Своё время", callback_data=SchedCb(action="custom").pack()),
        InlineKeyboardButton(text="🌍 Часовой пояс", callback_data=SchedCb(action="tz").pack()),
    )
    back_row(kb)
    return kb.as_markup()


def regions_menu(regions: list[RegionState]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    cities = [r for r in regions if r.municipality_type == "city"]
    okrugs = [r for r in regions if r.municipality_type == "okrug"]
    for r in cities:
        kb.row(
            InlineKeyboardButton(
                text=f"{'✅' if r.enabled else '⬜'} {r.short_name}",
                callback_data=RegionCb(action="toggle", value=r.municipality_id).pack(),
            )
        )
    buttons = [
        InlineKeyboardButton(
            text=f"{'✅' if r.enabled else '⬜'} {r.short_name}",
            callback_data=RegionCb(action="toggle", value=r.municipality_id).pack(),
        )
        for r in okrugs
    ]
    for i in range(0, len(buttons), 2):
        kb.row(*buttons[i : i + 2])
    kb.row(
        InlineKeyboardButton(text="✅ Включить все", callback_data=RegionCb(action="all").pack()),
        InlineKeyboardButton(text="⬜ Выключить все", callback_data=RegionCb(action="none").pack()),
    )
    back_row(kb)
    return kb.as_markup()


def format_menu(sub: Subscriber) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for value, label in (
        ("cards", "📇 Карточки — по одной на достижение, с кнопками"),
        ("list", "📋 Список — компактно, одним сообщением"),
    ):
        mark = "✅ " if sub.digest_format == value else ""
        kb.button(text=f"{mark}{label}", callback_data=FormatCb(value=value))
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(
            text=("🔕 Не присылать пустые" if sub.notify_empty else "🔔 Сообщать, если пусто"),
            callback_data=MenuCb(section="empty").pack(),
        )
    )
    back_row(kb)
    return kb.as_markup()


def status_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=MenuCb(section="status"))
    back_row(kb)
    return kb.as_markup()


def help_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    back_row(kb)
    return kb.as_markup()


def card_keyboard(
    candidate_id: int, url: str, done: bool = False, rejected: bool = False
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔗 Открыть пост", url=url))
    if rejected:
        kb.row(
            InlineKeyboardButton(
                text="↩️ Вернуть", callback_data=FeedbackCb(action="undo", cid=candidate_id).pack()
            )
        )
    elif done:
        kb.row(InlineKeyboardButton(text="✅ Поздравили", callback_data=NoopCb().pack(), style="success"))
    else:
        kb.row(
            InlineKeyboardButton(
                text="🙈 Не достижение", callback_data=FeedbackCb(action="reject", cid=candidate_id).pack()
            ),
            InlineKeyboardButton(
                text="🎉 Поздравили",
                callback_data=FeedbackCb(action="done", cid=candidate_id).pack(),
                style="success",
            ),
        )
    return kb.as_markup()
