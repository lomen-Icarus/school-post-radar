"""Расчёт расписания дайджестов подписчика и окон сканирования."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from radar.utils.timeutil import parse_hhmm, parse_times


def next_digest_slot(
    after: datetime,
    times: list[time],
    interval_days: int,
    tz: ZoneInfo,
    base_date: date | None = None,
) -> datetime:
    """Первый слот строго после `after` в сетке: дни base_date + k*interval_days, времена `times`.

    base_date — дата, от которой отсчитываются интервалы (обычно дата последнего дайджеста).
    Если None — берётся локальная дата момента `after`.
    Возвращает datetime в UTC.
    """
    if not times:
        raise ValueError("Пустой список времён")
    interval_days = max(1, interval_days)
    local_after = after.astimezone(tz)
    start = base_date or local_after.date()
    # Если базовая дата в будущем относительно after, всё равно двигаемся по сетке от неё.
    for k in range(0, interval_days * 2 + 60):
        day = start + timedelta(days=k * interval_days)
        for t in sorted(times):
            candidate = datetime.combine(day, t, tzinfo=tz)
            if candidate > local_after:
                return candidate.astimezone(ZoneInfo("UTC"))
    raise RuntimeError("Не удалось вычислить следующий слот")  # практически недостижимо


def next_digest_after_settings_change(
    now: datetime, times_raw: str, interval_days: int, tz_name: str
) -> datetime:
    """При изменении настроек: ближайший слот от текущего момента (интервал начинает действовать после него)."""
    return next_digest_slot(now, parse_times(times_raw), 1, ZoneInfo(tz_name))


def next_digest_after_send(sent_at: datetime, times_raw: str, interval_days: int, tz_name: str) -> datetime:
    """После отправки дайджеста: следующий слот того же дня, иначе первый слот через interval_days."""
    tz = ZoneInfo(tz_name)
    times = parse_times(times_raw)
    local_sent = sent_at.astimezone(tz)
    # Слоты сегодня после отправки
    for t in times:
        candidate = datetime.combine(local_sent.date(), t, tzinfo=tz)
        if candidate > local_sent:
            return candidate.astimezone(ZoneInfo("UTC"))
    day = local_sent.date() + timedelta(days=max(1, interval_days))
    return datetime.combine(day, times[0], tzinfo=tz).astimezone(ZoneInfo("UTC"))


@dataclass(frozen=True)
class ScanWindow:
    index: int
    start: time
    end: time

    def bounds(self, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
        start = datetime.combine(day, self.start, tzinfo=tz)
        end = datetime.combine(day, self.end, tzinfo=tz)
        if end <= start:  # окно через полночь
            end += timedelta(days=1)
        return start, end


def parse_scan_windows(raw: str) -> list[ScanWindow]:
    windows: list[ScanWindow] = []
    for idx, chunk in enumerate(p.strip() for p in raw.split(",") if p.strip()):
        if "-" not in chunk:
            raise ValueError(f"Окно сканирования должно быть вида ЧЧ:ММ-ЧЧ:ММ: {chunk!r}")
        a, b = chunk.split("-", 1)
        windows.append(ScanWindow(idx, parse_hhmm(a), parse_hhmm(b)))
    if not windows:
        raise ValueError("Не задано ни одного окна сканирования")
    return windows


def active_window(
    now: datetime, windows: list[ScanWindow], tz: ZoneInfo
) -> tuple[ScanWindow, datetime, datetime] | None:
    """Окно, в котором находится момент `now` (учитывая окна, начавшиеся вчера и перешедшие через полночь)."""
    local = now.astimezone(tz)
    for day in (local.date() - timedelta(days=1), local.date()):
        for w in windows:
            start, end = w.bounds(day, tz)
            if start <= local < end:
                return w, start, end
    return None


def run_key_for(window: ScanWindow, start: datetime) -> str:
    return f"{start.date().isoformat()}:{window.index}"


def target_index(total: int, start: datetime, end: datetime, now: datetime) -> int:
    """Сколько сообществ должно быть обработано к моменту now при равномерном распределении по окну."""
    if total <= 0:
        return 0
    duration = (end - start).total_seconds()
    if duration <= 0 or now >= end:
        return total
    elapsed = max(0.0, (now - start).total_seconds())
    # +1 шаг вперёд, чтобы к концу окна гарантированно всё обработать, а первый тик не был пустым.
    fraction = min(1.0, elapsed / duration)
    return min(total, int(fraction * total + 0.999) + 0)


def next_window_start(now: datetime, windows: list[ScanWindow], tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    candidates: list[datetime] = []
    for day_offset in range(0, 3):
        day = local.date() + timedelta(days=day_offset)
        for w in windows:
            start, _ = w.bounds(day, tz)
            if start > local:
                candidates.append(start)
    return min(candidates).astimezone(ZoneInfo("UTC"))
