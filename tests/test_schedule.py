from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from radar.pipeline import schedule
from radar.utils.timeutil import parse_times

MSK = ZoneInfo("Europe/Moscow")


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC"))


def test_next_slot_same_day_then_interval():
    times = parse_times("13:00, 21:00")
    now = datetime(2026, 9, 13, 14, 0, tzinfo=MSK)
    nxt = schedule.next_digest_slot(now, times, 1, MSK)
    assert nxt.astimezone(MSK) == datetime(2026, 9, 13, 21, 0, tzinfo=MSK)
    after_send = schedule.next_digest_after_send(nxt, "13:00,21:00", 3, "Europe/Moscow")
    assert after_send.astimezone(MSK) == datetime(2026, 9, 16, 13, 0, tzinfo=MSK)
    # после утреннего слота — вечерний того же дня, интервал не трогает
    after_morning = schedule.next_digest_after_send(
        datetime(2026, 9, 16, 13, 0, tzinfo=MSK), "13:00,21:00", 3, "Europe/Moscow"
    )
    assert after_morning.astimezone(MSK) == datetime(2026, 9, 16, 21, 0, tzinfo=MSK)


def test_settings_change_uses_nearest_slot():
    nxt = schedule.next_digest_after_settings_change(
        datetime(2026, 9, 13, 22, 30, tzinfo=MSK), "13:00,21:00", 7, "Europe/Moscow"
    )
    assert nxt.astimezone(MSK) == datetime(2026, 9, 14, 13, 0, tzinfo=MSK)


def test_parse_times_validation():
    assert parse_times("21:00, 13:00") == [time(13, 0), time(21, 0)]
    with pytest.raises(ValueError):
        parse_times("25:00")
    with pytest.raises(ValueError):
        parse_times("")


def test_windows_and_target_index():
    windows = schedule.parse_scan_windows("08:00-12:45,17:00-20:45")
    assert len(windows) == 2
    now = datetime(2026, 9, 13, 9, 0, tzinfo=MSK)
    active = schedule.active_window(now, windows, MSK)
    assert active is not None
    w, start, end = active
    assert w.index == 0 and start.hour == 8 and end.hour == 12
    assert schedule.active_window(datetime(2026, 9, 13, 13, 0, tzinfo=MSK), windows, MSK) is None
    assert schedule.target_index(365, start, end, start) == 0
    assert schedule.target_index(365, start, end, end) == 365
    mid = schedule.target_index(365, start, end, start + (end - start) / 2)
    assert 180 <= mid <= 185
    assert schedule.target_index(0, start, end, now) == 0
    nxt = schedule.next_window_start(datetime(2026, 9, 13, 13, 0, tzinfo=MSK), windows, MSK)
    assert nxt.astimezone(MSK) == datetime(2026, 9, 13, 17, 0, tzinfo=MSK)
    assert schedule.run_key_for(w, start) == "2026-09-13:0"


def test_window_across_midnight():
    windows = schedule.parse_scan_windows("23:00-01:00")
    active = schedule.active_window(datetime(2026, 9, 14, 0, 30, tzinfo=MSK), windows, MSK)
    assert active is not None
    _, start, end = active
    assert start.day == 13 and end.day == 14


def test_bad_windows():
    with pytest.raises(ValueError):
        schedule.parse_scan_windows("08:00")
