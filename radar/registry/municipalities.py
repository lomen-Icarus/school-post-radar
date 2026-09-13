"""Справочник 23 муниципальных образований Чувашии и сопоставление area_source -> municipality_id.

Состав: 21 муниципальный округ + 2 городских округа (Чебоксары, Новочебоксарск).
Города Алатырь, Канаш, Шумерля, Козловка, Мариинский Посад, Цивильск входят в одноимённые
муниципальные округа. Идентификаторы — внутренние, не ОКТМО.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Municipality:
    municipality_id: str
    name: str
    short_name: str
    municipality_type: str  # 'city' | 'okrug'
    stem: str  # основа прилагательного для сопоставления area_source
    sort_order: int


_RAW: list[tuple[str, str, str, str, str]] = [
    # id, полное название, короткая подпись для кнопки, тип, основа
    ("ru21-city-cheboksary", "Городской округ Чебоксары", "Чебоксары (город)", "city", "чебоксары"),
    (
        "ru21-city-novocheboksarsk",
        "Городской округ Новочебоксарск",
        "Новочебоксарск",
        "city",
        "новочебоксарск",
    ),
    ("ru21-mo-alatyr", "Алатырский муниципальный округ", "Алатырский", "okrug", "алатыр"),
    ("ru21-mo-alikovo", "Аликовский муниципальный округ", "Аликовский", "okrug", "аликов"),
    ("ru21-mo-batyrevo", "Батыревский муниципальный округ", "Батыревский", "okrug", "батырев"),
    ("ru21-mo-vurnary", "Вурнарский муниципальный округ", "Вурнарский", "okrug", "вурнар"),
    ("ru21-mo-ibresi", "Ибресинский муниципальный округ", "Ибресинский", "okrug", "ибресин"),
    ("ru21-mo-kanash", "Канашский муниципальный округ", "Канашский", "okrug", "канаш"),
    ("ru21-mo-kozlovka", "Козловский муниципальный округ", "Козловский", "okrug", "козлов"),
    ("ru21-mo-komsomolskoe", "Комсомольский муниципальный округ", "Комсомольский", "okrug", "комсомольск"),
    (
        "ru21-mo-krasnoarmeyskoe",
        "Красноармейский муниципальный округ",
        "Красноармейский",
        "okrug",
        "красноармейск",
    ),
    (
        "ru21-mo-krasnye-chetai",
        "Красночетайский муниципальный округ",
        "Красночетайский",
        "okrug",
        "красночетайск",
    ),
    (
        "ru21-mo-mariinsky-posad",
        "Мариинско-Посадский муниципальный округ",
        "Мариинско-Посадский",
        "okrug",
        "мариинск",
    ),
    ("ru21-mo-morgaushi", "Моргаушский муниципальный округ", "Моргаушский", "okrug", "моргауш"),
    ("ru21-mo-poretskoe", "Порецкий муниципальный округ", "Порецкий", "okrug", "порецк"),
    ("ru21-mo-urmary", "Урмарский муниципальный округ", "Урмарский", "okrug", "урмар"),
    ("ru21-mo-tsivilsk", "Цивильский муниципальный округ", "Цивильский", "okrug", "цивил"),
    (
        "ru21-mo-cheboksary-okrug",
        "Чебоксарский муниципальный округ",
        "Чебоксарский округ",
        "okrug",
        "чебоксарск",
    ),
    ("ru21-mo-shemursha", "Шемуршинский муниципальный округ", "Шемуршинский", "okrug", "шемурш"),
    ("ru21-mo-shumerlya", "Шумерлинский муниципальный округ", "Шумерлинский", "okrug", "шумерл"),
    ("ru21-mo-yadrin", "Ядринский муниципальный округ", "Ядринский", "okrug", "ядрин"),
    ("ru21-mo-yalchiki", "Яльчикский муниципальный округ", "Яльчикский", "okrug", "яльчик"),
    ("ru21-mo-yantikovo", "Янтиковский муниципальный округ", "Янтиковский", "okrug", "янтиков"),
]

MUNICIPALITIES: list[Municipality] = [
    Municipality(mid, name, short, mtype, stem, i) for i, (mid, name, short, mtype, stem) in enumerate(_RAW)
]
BY_ID: dict[str, Municipality] = {m.municipality_id: m for m in MUNICIPALITIES}

# Точные формулировки из area_source (нижний регистр, схлопнутые пробелы) -> id.
_EXACT: dict[str, str] = {
    "г. чебоксары": "ru21-city-cheboksary",
    "город чебоксары": "ru21-city-cheboksary",
    "городской округ чебоксары": "ru21-city-cheboksary",
    "г. новочебоксарск": "ru21-city-novocheboksarsk",
    "город новочебоксарск": "ru21-city-novocheboksarsk",
    "городской округ новочебоксарск": "ru21-city-novocheboksarsk",
    "г. алатырь": "ru21-mo-alatyr",
    "г. канаш": "ru21-mo-kanash",
    "г. козловка": "ru21-mo-kozlovka",
    "г. мариинский посад": "ru21-mo-mariinsky-posad",
    "г. цивильск": "ru21-mo-tsivilsk",
    "г. шумерля": "ru21-mo-shumerlya",
    "г. ядрин": "ru21-mo-yadrin",
}

_ADJ_RE = re.compile(r"^([а-яё\-]+?)(ий|ой)\b", re.IGNORECASE)


def _normalize(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip().lower().replace("ё", "е"))


def municipality_for_area_source(area_source: str | None) -> str | None:
    """Определить municipality_id по строке района/округа из реестра. None, если не распознано."""
    if not area_source:
        return None
    text = _normalize(area_source)
    if text in _EXACT:
        return _EXACT[text]
    # "Чебоксарский район / округ", "Ядринский муниципальный округ" -> основа прилагательного
    match = _ADJ_RE.match(text)
    if not match:
        return None
    stem = match.group(1).replace("е", "е")
    # Ищем самое длинное совпадение основы, чтобы "чебоксарск" не перепутать с "чебоксары".
    best: Municipality | None = None
    for m in MUNICIPALITIES:
        m_stem = m.stem.replace("ё", "е")
        if stem.startswith(m_stem) and (best is None or len(m_stem) > len(best.stem)):
            best = m
    return best.municipality_id if best else None
