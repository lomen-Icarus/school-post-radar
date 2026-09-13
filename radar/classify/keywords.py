"""Предфильтр по ключевым словам: отсеивает явно нерелевантные посты до обращения к модели."""

from __future__ import annotations

import re

# Сильные признаки: почти наверняка речь о результате/награде.
STRONG_PATTERNS: dict[str, str] = {
    "победа": r"\bпобед\w*",
    "призёр": r"\bприз[её]р\w*",
    "лауреат": r"\bлауреат\w*",
    "диплом": r"\bдиплом\w*",
    "грамота": r"\bграмот\w*",
    "награда": r"\bнагра(д|жд)\w*",
    "медаль": r"\bмедал\w*",
    "кубок": r"\bкуб(ок|ка|ки)\b",
    "чемпион": r"\bчемпион\w*",
    "финалист": r"\bфиналист\w*",
    "место": r"\b(1|2|3|i|ii|iii|перв|втор|трет|призов)\w*[\s\-]*(мест[оа]|места)\b",
    "занял место": r"\bзаня(л|ла|ли|ть)\b[^.!?\n]{0,60}\bмест",
    "стипендия": r"\bстипенд\w*",
    "поступил": r"\bпоступил\w*",
    "зачислен": r"\bзачислен\w*",
    "сертификат": r"\bсертификат\w*",
    "номинация": r"\bноминац\w*",
    "гордимся": r"\bгордимся\b|\bгордость\b",
    "поздравляем": r"\bпоздравля\w*",
    "золото": r"\b(золот|серебр|бронз)\w*",
    "рекорд": r"\bрекорд\w*",
    "значок гто": r"\bгто\b",
}

# Слабые признаки: контекст мероприятия/поездки, где может быть достижение или участие.
WEAK_PATTERNS: dict[str, str] = {
    "олимпиада": r"\bолимпиад\w*",
    "конкурс": r"\bконкурс\w*",
    "соревнования": r"\bсоревнован\w*",
    "турнир": r"\bтурнир\w*",
    "первенство": r"\bпервенств\w*",
    "спартакиада": r"\bспартакиад\w*",
    "фестиваль": r"\bфестивал\w*",
    "конференция": r"\bконференц\w*",
    "форум": r"\bфорум\w*",
    "чемпионат": r"\bчемпионат\w*",
    "этап": r"\b(муниципальн|региональн|республиканск|всероссийск|межрегиональн|международн)\w*\s+(этап|уровн|тур)",
    "участие": r"\bучаст(ие|вовал|ник|ница|ники)\w*",
    "вуз": r"\b(вуз|университет|институт|академи|чгу|чгпу|чгсха|мгу|мфти|итмо|вшэ|спбгу|лицей|колледж)\w*",
    "поездка": r"\b(поездк|съездил|побывал|посетил|экскурси|делегац)\w*",
    "результат": r"\bрезультат\w*|\bитог(и|ам|ов)\b",
    "достижение": r"\bдостижен\w*|\bуспех\w*",
    "выступление": r"\bвыступ(ил|ила|или|ление)\w*",
    "защитил": r"\bзащит(ил|ила|или)\b|\bпредстав(ил|ила|или)\b",
    "проект": r"\bпроект\w*",
    "ученик": r"\b(ученик|ученица|ученики|учащ|обучающ|школьник|выпускник|команда|сборная)\w*",
}

_STRONG = {k: re.compile(v, re.IGNORECASE) for k, v in STRONG_PATTERNS.items()}
_WEAK = {k: re.compile(v, re.IGNORECASE) for k, v in WEAK_PATTERNS.items()}

# Явные признаки объявлений/расписаний/административных постов, которые почти никогда не про достижения.
NEGATIVE_PATTERNS: dict[str, str] = {
    "объявление": r"\bприглаша\w*\b|\bприём документов\b|\bзапис(ь|ывайтесь)\b|\bрасписани\w*",
    "родительское": r"\bродительск\w+ собран",
    "вакансия": r"\bвакансия\w*",
}
_NEGATIVE = {k: re.compile(v, re.IGNORECASE) for k, v in NEGATIVE_PATTERNS.items()}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("ё", "е").replace("Ё", "Е")).strip()


def keyword_hits(text: str) -> tuple[list[str], list[str], list[str]]:
    """Возвращает (сильные, слабые, негативные) совпадения по названиям паттернов."""
    norm = normalize_text(text)
    strong = [name for name, rx in _STRONG.items() if rx.search(norm)]
    weak = [name for name, rx in _WEAK.items() if rx.search(norm)]
    negative = [name for name, rx in _NEGATIVE.items() if rx.search(norm)]
    return strong, weak, negative


def should_consider(text: str) -> tuple[bool, list[str]]:
    """Стоит ли отправлять пост в модель. True при любом сильном признаке или >= 2 слабых."""
    if not text or len(text.strip()) < 25:
        return False, []
    strong, weak, _negative = keyword_hits(text)
    hits = strong + weak
    if strong:
        return True, hits
    if len(weak) >= 2:
        return True, hits
    return False, hits


def keyword_only_verdict(text: str) -> tuple[bool, float, str]:
    """Оценка без модели: (достижение?, уверенность, категория). Используется, если LLM недоступна."""
    strong, weak, negative = keyword_hits(text)
    if not strong:
        return False, 0.15 if weak else 0.0, "other"
    score = 0.45 + 0.08 * len(strong) + 0.03 * len(weak) - 0.15 * len(negative)
    score = max(0.0, min(0.85, score))
    kind = "award"
    if "поступил" in strong or "зачислен" in strong:
        kind = "admission"
    elif "олимпиада" in weak:
        kind = "olympiad"
    elif "соревнования" in weak or "турнир" in weak or "первенство" in weak or "чемпион" in strong:
        kind = "sport"
    elif "конкурс" in weak or "фестиваль" in weak:
        kind = "competition"
    return score >= 0.5, score, kind
