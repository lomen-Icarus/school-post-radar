import sqlite3

from radar.registry.municipalities import BY_ID, MUNICIPALITIES, municipality_for_area_source
from tests.conftest import SEED


def test_23_municipalities_two_cities():
    assert len(MUNICIPALITIES) == 23
    assert sum(1 for m in MUNICIPALITIES if m.municipality_type == "city") == 2
    assert len(BY_ID) == 23


def test_every_registry_area_source_is_mapped():
    db = sqlite3.connect(SEED)
    unmapped = []
    for (area,) in db.execute("SELECT DISTINCT area_source FROM units"):
        if municipality_for_area_source(area) is None:
            unmapped.append(area)
    assert unmapped == []


def test_specific_mappings():
    assert municipality_for_area_source("г. Чебоксары") == "ru21-city-cheboksary"
    assert municipality_for_area_source("городской округ Чебоксары") == "ru21-city-cheboksary"
    assert municipality_for_area_source("Чебоксарский район / округ") == "ru21-mo-cheboksary-okrug"
    assert municipality_for_area_source("г. Канаш") == "ru21-mo-kanash"
    assert municipality_for_area_source("Канашский район / округ") == "ru21-mo-kanash"
    assert municipality_for_area_source("Ядринский муниципальный округ") == "ru21-mo-yadrin"
    assert municipality_for_area_source("г. Шумерля") == "ru21-mo-shumerlya"
    assert municipality_for_area_source("") is None
    assert municipality_for_area_source("Марс") is None
