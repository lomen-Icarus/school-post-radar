"""Фабрики callback_data (лимит Telegram — 64 байта, поэтому значения короткие)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCb(CallbackData, prefix="m"):
    section: str  # main | mode | sched | regions | status | help | pause | resume | format | empty


class ModeCb(CallbackData, prefix="mode"):
    value: str  # digest | instant


class SchedCb(CallbackData, prefix="sc"):
    action: str  # preset | interval | custom | tz
    value: str = ""


class RegionCb(CallbackData, prefix="rg"):
    action: str  # toggle | all | none | page
    value: str = ""


class FormatCb(CallbackData, prefix="fmt"):
    value: str  # cards | list


class FeedbackCb(CallbackData, prefix="fb"):
    action: str  # reject | done | undo
    cid: int


class NoopCb(CallbackData, prefix="noop"):
    pass
