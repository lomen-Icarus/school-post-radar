"""Форматирование карточек и дайджестов."""

from __future__ import annotations

from html import escape

from radar.bot.texts import KIND_LABELS
from radar.db.repo import Candidate
from radar.registry.municipalities import BY_ID
from radar.utils.timeutil import from_iso, humanize_local

TG_LIMIT = 4000  # с запасом от 4096


def _municipality_label(c: Candidate) -> str:
    names = [BY_ID[m].short_name for m in c.municipality_ids if m in BY_ID]
    return ", ".join(names) if names else "Чувашия"


def _school_label(c: Candidate) -> str:
    if not c.school_names:
        return "Школа"
    label = c.school_names[0]
    if len(c.school_names) > 1:
        label += f" (+{len(c.school_names) - 1})"
    return label


def format_card(c: Candidate, tz_name: str) -> str:
    kind = KIND_LABELS.get(c.kind or "other", KIND_LABELS["other"])
    when = humanize_local(from_iso(c.published_at), tz_name)
    lines = [
        f"{kind} · <b>{escape(_municipality_label(c))}</b>",
        f"🏫 {escape(_school_label(c))}",
        "",
        f"<b>{escape(c.headline or 'Достижение учеников')}</b>",
    ]
    if c.summary:
        lines.append(escape(c.summary))
    if c.students:
        lines.append(f"👤 {escape(', '.join(c.students[:8]))}")
    lines.append("")
    conf = f" · уверенность {round(c.confidence * 100)}%" if c.confidence else ""
    repost = " · репост" if c.is_repost else ""
    lines.append(f"📅 {when}{conf}{repost}")
    return "\n".join(lines)


def format_digest_list(
    items: list[Candidate], tz_name: str, header: str
) -> list[tuple[str, list[Candidate]]]:
    """Компактный дайджест по территориям, разбитый на сообщения до 4000 символов.

    Возвращает пары (текст сообщения, элементы в нём), чтобы доставки учитывались по факту отправки.
    """
    groups: dict[str, list[Candidate]] = {}
    for c in items:
        groups.setdefault(_municipality_label(c), []).append(c)

    messages: list[tuple[str, list[Candidate]]] = []
    lines: list[str] = [header]
    current_items: list[Candidate] = []
    current_label: str | None = None

    def flush() -> None:
        nonlocal lines, current_items, current_label
        if current_items:
            messages.append(("\n".join(lines), current_items))
        lines, current_items, current_label = [], [], None

    for label in sorted(groups):
        group_header = f"<b>📍 {escape(label)}</b>"
        for c in groups[label]:
            kind_emoji = KIND_LABELS.get(c.kind or "other", "✨")[:1]
            title = escape(c.headline or "Достижение")
            school = escape(_school_label(c))
            when = humanize_local(from_iso(c.published_at), tz_name, with_date=False)
            line = f'{kind_emoji} <a href="{escape(c.url)}">{title}</a> — {school} · {when}'
            if current_label == label:
                prefix: list[str] = []
            else:
                prefix = ([""] if lines else []) + [group_header]
            projected = "\n".join([*lines, *prefix, line])
            if len(projected) > TG_LIMIT and current_items:
                flush()
                prefix = [group_header]
            lines.extend(prefix)
            lines.append(line)
            current_items.append(c)
            current_label = label
    flush()
    return messages


def digest_header(count: int, tz_name: str, when: str | None = None) -> str:
    word = (
        "достижение"
        if count % 10 == 1 and count % 100 != 11
        else "достижения"
        if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14
        else "достижений"
    )
    return f"<b>🏆 Дайджест: {count} {word}</b>" + (f" · {escape(when)}" if when else "")
