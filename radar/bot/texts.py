"""Тексты интерфейса (HTML-разметка Telegram)."""

from __future__ import annotations

from html import escape

from radar.config import Settings
from radar.db.repo import Subscriber
from radar.utils.timeutil import from_iso, humanize_local

MODE_LABELS = {"digest": "📦 Дайджест по расписанию", "instant": "⚡ Сразу, как найдём"}
FORMAT_LABELS = {"cards": "📇 Карточки", "list": "📋 Список"}
KIND_LABELS = {
    "olympiad": "🧠 Олимпиада",
    "competition": "🎭 Конкурс",
    "sport": "🏅 Спорт",
    "award": "🏆 Награда",
    "admission": "🎓 Поступление",
    "trip": "🚌 Поездка",
    "other": "✨ Достижение",
}


def _tz_label(tz_name: str) -> str:
    return "МСК" if tz_name == "Europe/Moscow" else tz_name


def welcome(first_name: str | None, created: bool, sub: Subscriber) -> str:
    name = escape(first_name or "коллега")
    intro = (
        f"Привет, {name}! Я <b>Радар школьных достижений</b>.\n\n"
        "Дважды в день просматриваю ленты ВК всех школ Чувашии и отбираю посты, где ученики "
        "победили, стали призёрами, поступили, съездили на олимпиаду или вуз — чтобы факультет "
        "успел поздравить их под постом.\n\n"
    )
    if created:
        times = escape(sub.digest_times.replace(",", ", "))
        intro += (
            f"По умолчанию: дайджест в <b>{times}</b> ({escape(_tz_label(sub.timezone))}), "
            "все 23 территории включены.\n\n"
        )
    return intro + "Ниже — настройки. Всё меняется кнопками, ничего вводить не нужно."


def help_text(settings: Settings, communities_total: int) -> str:
    windows = escape(settings.scan_windows.replace(",", " и ").replace("-", "–"))
    scope = (
        "все найденные сообщества" if settings.monitor_scope == "all" else "только подтверждённые сообщества"
    )
    return (
        "<b>Как это работает</b>\n"
        f"• В окна {windows} ({escape(_tz_label(settings.timezone))}) бот по очереди обходит "
        f"{communities_total} сообществ школ ({scope}), чтобы нагрузка была ровной.\n"
        f"• Новые посты за последние {settings.lookback_hours} ч проверяются на «достижение ученика» "
        "(ключевые слова + модель Claude).\n"
        "• Найденное приходит пачкой в выбранное время или сразу — как настроите.\n\n"
        "<b>Команды</b>\n"
        "/start — главное меню\n"
        "/settings — настройки\n"
        "/regions — территории\n"
        "/status — что происходит сейчас\n"
        "/digest — прислать накопленное сейчас\n"
        "/pause и /resume — пауза и возобновление\n"
        "/tz Europe/Moscow — часовой пояс расписания\n\n"
        "В каждой карточке есть кнопки «Не достижение» и «Поздравили» — так вы отмечаете обработанное, "
        "а ошибочные срабатывания больше никому не показываются."
    )


def settings_overview(sub: Subscriber, enabled_regions: int, total_regions: int) -> str:
    mode = MODE_LABELS.get(sub.mode, sub.mode)
    times = escape(sub.digest_times.replace(",", ", "))
    interval = "каждый день" if sub.interval_days == 1 else f"раз в {sub.interval_days} дн."
    nxt = humanize_local(from_iso(sub.next_digest_at), sub.timezone) if sub.mode == "digest" else "—"
    state = "▶️ активен" if sub.is_active else "⏸ на паузе"
    return (
        "<b>⚙️ Настройки</b>\n\n"
        f"Состояние: {state}\n"
        f"Режим: {mode}\n"
        f"Расписание: {times} · {interval}\n"
        f"Следующий дайджест: {nxt}\n"
        f"Формат: {FORMAT_LABELS.get(sub.digest_format, sub.digest_format)}\n"
        f"Территории: {enabled_regions} из {total_regions}\n"
        f"Часовой пояс: {escape(sub.timezone)}\n"
        f"Пустые дайджесты: {'сообщать' if sub.notify_empty else 'не присылать'}"
    )


def mode_screen(sub: Subscriber) -> str:
    return (
        "<b>🔔 Режим доставки</b>\n\n"
        "📦 <b>Дайджест</b> — всё найденное приходит пачкой в назначенное время.\n"
        "⚡ <b>Сразу</b> — каждое достижение приходит отдельной карточкой, как только бот его нашёл "
        "(в часы сканирования).\n\n"
        f"Сейчас: {MODE_LABELS.get(sub.mode, sub.mode)}"
    )


def schedule_screen(sub: Subscriber) -> str:
    interval = "каждый день" if sub.interval_days == 1 else f"раз в {sub.interval_days} дн."
    nxt = humanize_local(from_iso(sub.next_digest_at), sub.timezone)
    return (
        "<b>🕐 Расписание дайджестов</b>\n\n"
        f"Время: <b>{escape(sub.digest_times.replace(',', ', '))}</b> ({escape(sub.timezone)})\n"
        f"Частота: <b>{interval}</b>\n"
        f"Следующий: {nxt}\n\n"
        "Выберите готовый вариант или задайте своё время."
    )


def regions_screen(enabled: int, total: int) -> str:
    return (
        "<b>🗺 Территории</b>\n\n"
        f"Включено: <b>{enabled} из {total}</b>. Нажмите на территорию, чтобы включить или выключить её.\n"
        "Отключённая территория не попадёт в ваши уведомления, но опрос сообществ продолжается для остальных."
    )


def ask_custom_times() -> str:
    return (
        "Введите время дайджестов через запятую, например: <code>09:30, 13:00, 21:00</code>\nОтмена — /cancel"
    )


def ask_timezone() -> str:
    return (
        "Введите часовой пояс в формате IANA, например <code>Europe/Moscow</code> или "
        "<code>Asia/Yekaterinburg</code>.\nОтмена — /cancel"
    )
