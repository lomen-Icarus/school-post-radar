from radar.classify import keywords
from radar.classify.service import Classifier


def test_should_consider_strong_and_weak():
    ok, hits = keywords.should_consider(
        "Поздравляем Иванову Анну с победой в республиканской олимпиаде по биологии!"
    )
    assert ok and "победа" in hits
    ok, _ = keywords.should_consider("Родительское собрание состоится 15 сентября в 18:00.")
    assert not ok
    ok, hits = keywords.should_consider(
        "Наши ребята побывали в ЧГУ на дне открытых дверей и посетили лаборатории."
    )
    assert ok and "вуз" in hits and "поездка" in hits
    ok, _ = keywords.should_consider("коротко")
    assert not ok


def test_keyword_only_verdict_kinds():
    assert (
        keywords.keyword_only_verdict("Команда школы заняла 2 место в первенстве района по волейболу")[2]
        == "sport"
    )
    assert keywords.keyword_only_verdict("Выпускник Петров Илья поступил в МФТИ на бюджет!")[2] == "admission"
    assert (
        keywords.keyword_only_verdict("Ученица стала призёром регионального этапа олимпиады")[2] == "olympiad"
    )
    assert keywords.keyword_only_verdict("С Днём знаний!")[0] is False


async def test_classifier_without_llm():
    clf = Classifier(None)
    res = await clf.classify(
        "Поздравляем призёров муниципального этапа олимпиады по физике: Сидоров Иван, 10 класс!", ["Школа"]
    )
    assert res.is_achievement and res.confidence >= 0.5 and res.classifier_version == "keywords-v1"
    res2 = await clf.classify("Уважаемые родители, напоминаем о графике работы столовой.", ["Школа"])
    assert not res2.is_achievement and res2.skipped_llm
