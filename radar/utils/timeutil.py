from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """ISO-8601 в UTC с секундной точностью — единый формат хранения в SQLite."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def from_unix(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def local_now(tz_name: str) -> datetime:
    return now_utc().astimezone(ZoneInfo(tz_name))


def parse_hhmm(raw: str) -> time:
    raw = raw.strip().replace(".", ":").replace("-", ":")
    shown = raw[:20] + ("…" if len(raw) > 20 else "")  # в сообщение об ошибке не тащим весь ввод
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"Неверное время: {shown!r} (ожидается ЧЧ:ММ)")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Неверное время: {shown!r} (ожидается ЧЧ:ММ)") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Неверное время: {shown!r}")
    return time(hour=hour, minute=minute)


def parse_times(raw: str) -> list[time]:
    """'13:00, 21:00' -> отсортированный список уникальных time."""
    items = {parse_hhmm(p) for p in raw.replace(";", ",").split(",") if p.strip()}
    if not items:
        raise ValueError("Список времён пуст")
    return sorted(items)


def format_times(times: list[time]) -> str:
    return ", ".join(t.strftime("%H:%M") for t in times)


def humanize_local(dt: datetime | None, tz_name: str, with_date: bool = True) -> str:
    if dt is None:
        return "—"
    local = dt.astimezone(ZoneInfo(tz_name))
    months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    if with_date:
        return f"{local.day} {months[local.month - 1]}, {local:%H:%M}"
    return f"{local:%H:%M}"
