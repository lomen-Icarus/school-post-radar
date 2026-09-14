"""Классификация постов моделью Claude со структурированным JSON-ответом."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

CLASSIFIER_VERSION = "llm-v2"

KINDS = ("olympiad", "competition", "sport", "award", "admission", "trip", "other")

HEADLINE_MAX = 120
SUMMARY_MAX = 600
REASON_MAX = 400
MAX_TOKENS = 4096  # на Opus/Sonnet 5 лимит включает и размышления модели, поэтому запас нужен


class Verdict(BaseModel):
    """Ответ модели. Длинные строки обрезаются, а не отвергаются: схема API длину не ограничивает."""

    is_achievement: bool
    confidence: float = Field(ge=0.0, le=1.0)
    about_students: bool
    kind: str
    headline: str
    summary: str
    students: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("headline", mode="before")
    @classmethod
    def _cut_headline(cls, value: Any) -> str:
        return str(value or "")[:HEADLINE_MAX]

    @field_validator("summary", mode="before")
    @classmethod
    def _cut_summary(cls, value: Any) -> str:
        return str(value or "")[:SUMMARY_MAX]

    @field_validator("reason", mode="before")
    @classmethod
    def _cut_reason(cls, value: Any) -> str:
        return str(value or "")[:REASON_MAX]

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: Any) -> str:
        return value if value in KINDS else "other"

    @field_validator("students", mode="before")
    @classmethod
    def _students_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(s).strip()[:80] for s in value if str(s).strip()][:12]


# Структурированный вывод API не поддерживает числовые/строковые ограничения (minimum, maxLength и т.п.):
# диапазоны описаны словами, а проверка выполняется в Verdict.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_achievement": {
            "type": "boolean",
            "description": "true, если пост сообщает о достижении/успехе/участии учеников школы, о котором уместно поздравить",
        },
        "confidence": {"type": "number", "description": "Уверенность от 0 до 1"},
        "about_students": {
            "type": "boolean",
            "description": "Речь именно об учениках (а не только о педагогах или школе как учреждении)",
        },
        "kind": {"type": "string", "enum": list(KINDS)},
        "headline": {"type": "string", "description": "Заголовок до 80 символов, по-русски, без эмодзи"},
        "summary": {
            "type": "string",
            "description": "1-2 предложения (до 400 символов): кто, что, где, какой результат",
        },
        "students": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Имена/фамилии учеников, если названы",
        },
        "reason": {"type": "string", "description": "Кратко (до 200 символов), почему принято решение"},
    },
    "required": [
        "is_achievement",
        "confidence",
        "about_students",
        "kind",
        "headline",
        "summary",
        "students",
        "reason",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ты помогаешь факультету вуза находить в постах школьных сообществ ВКонтакте (Чувашия) новости о достижениях учеников, чтобы факультет мог их поздравить в комментариях.

Считай достижением (is_achievement=true) посты, где ученики школы:
- победили, стали призёрами, лауреатами, дипломантами, получили грамоты, медали, награды, стипендии, значки ГТО, сертификаты за результат;
- заняли места на олимпиадах, конкурсах, соревнованиях, конференциях, фестивалях, турнирах (любого уровня: школьного, муниципального, республиканского, всероссийского, международного);
- поступили в вузы/колледжи, прошли отбор в образовательные центры («Сириус», «Эткер» и т.п.), сборные, профильные смены;
- достойно представили школу: участвовали в финале, защитили проект, выступили на форуме, съездили на олимпиаду или в вуз по приглашению (участие без призового места тоже подходит, но с меньшей уверенностью 0.5–0.7).

НЕ считай достижением: анонсы и приглашения, расписания, объявления о наборе, классные часы, обычные уроки и экскурсии без результата, достижения только учителей или школы как организации (about_students=false), поздравления с праздниками, посты не о людях из этой школы (например, репост общегородской новости без упоминания учеников школы). Если пост — репост новости о других школах, где ученики этой школы не упомянуты, ставь is_achievement=false.

Отвечай строго JSON по схеме. headline — короткий заголовок по-русски (например: «Победа на республиканской олимпиаде по физике»). summary — 1–2 предложения с сутью: кто (класс/имена, если есть), что, где, результат. students — только явно названные имена/фамилии учеников; не придумывай. confidence — число от 0 до 1.

Примеры решений:
1) «Поздравляем Иванову Анну, ученицу 10 класса, с победой в республиканском этапе Всероссийской олимпиады школьников по биологии!» → is_achievement=true, about_students=true, kind=olympiad, confidence≈0.95, students=["Иванова Анна"], headline «Победа на республиканском этапе ВсОШ по биологии».
2) «Команда школы заняла 2 место в первенстве округа по волейболу среди юношей. Молодцы!» → true, kind=sport, confidence≈0.9, students=[] (имена не названы).
3) «Выпускник Петров Илья поступил в МФТИ на бюджет. Гордимся!» → true, kind=admission, confidence≈0.9.
4) «Ученики 8а посетили ЧГУ им. Ульянова: побывали в лабораториях и на дне открытых дверей» → true, kind=trip, confidence≈0.55 (поездка без результата, но повод отметить).
5) «Уважаемые родители! Родительское собрание состоится 15 сентября в 18:00» → false, confidence≈0.98, reason «объявление».
6) «Учитель математики Смирнова О.В. стала победителем конкурса «Учитель года»» → is_achievement=true по смыслу, но about_students=false, kind=award, confidence≈0.9 — поздравлять надо педагога, не учеников.
7) «Управление образования подвело итоги муниципального этапа: победители из школ №1, №5 и гимназии №2» (репост общей новости без упоминания учеников этой школы) → false, reason «нет учеников этой школы».
8) «Наши ребята приняли участие в муниципальном этапе конкурса чтецов «Живая классика». Спасибо за подготовку!» → true, kind=competition, confidence≈0.6 (участие без места)."""


def _supports_effort(model: str) -> bool:
    """`output_config.effort` поддерживают Opus 4.5+ и Sonnet 4.6+/5; Haiku 4.5 и старые модели его отвергают."""
    name = model.lower()
    if "haiku" in name or "claude-3" in name:
        return False
    return "sonnet-4-5" not in name


class ClaudeClassifier:
    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        effort: str = "low",
        use_fallbacks: bool = True,
        api_key: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.model = model
        self.effort = effort
        self._use_fallbacks = use_fallbacks
        self._client = anthropic.AsyncAnthropic(api_key=api_key or None, timeout=timeout, max_retries=3)

    @property
    def version(self) -> str:
        return f"{CLASSIFIER_VERSION}:{self.model}:{self.effort}"

    async def close(self) -> None:
        await self._client.close()

    def _build_user_message(self, text: str, school_names: list[str], municipality: str | None) -> str:
        school = "; ".join(school_names) if school_names else "неизвестна"
        where = f" ({municipality})" if municipality else ""
        return f'Школа: {school}{where}\n\nТекст поста:\n"""\n{text.strip()[:6000]}\n"""'

    def build_request(
        self, text: str, school_names: list[str], municipality: str | None = None
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}}
        if _supports_effort(self.model):
            output_config["effort"] = self.effort
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": self._build_user_message(text, school_names, municipality)}
            ],
            "output_config": output_config,
        }

    async def classify(self, text: str, school_names: list[str], municipality: str | None = None) -> Verdict:
        response = await self._create(self.build_request(text, school_names, municipality))
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            reason = getattr(details, "category", None) if details is not None else None
            log.warning("Модель отказалась классифицировать пост (категория: %s)", reason)
            return Verdict(
                is_achievement=False,
                confidence=0.0,
                about_students=False,
                kind="other",
                headline="",
                summary="",
                students=[],
                reason=f"refusal:{reason}",
            )
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Ответ модели обрезан по max_tokens={MAX_TOKENS}; уменьшите CLASSIFIER_EFFORT или увеличьте лимит"
            )
        text_block = next((b for b in response.content if getattr(b, "type", "") == "text"), None)
        if text_block is None:
            raise RuntimeError("Пустой ответ модели")
        try:
            data = json.loads(text_block.text)
        except ValueError as exc:
            raise RuntimeError(f"Некорректный JSON от модели: {exc}") from exc
        try:
            return Verdict.model_validate(data)
        except ValidationError as exc:
            raise RuntimeError(f"JSON модели не соответствует схеме: {exc}") from exc

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        if self._use_fallbacks:
            try:
                return await self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            except anthropic.BadRequestError as exc:
                if not _is_fallback_error(exc):
                    raise  # ошибка в самом запросе — повтор без fallbacks её не исправит
                log.warning("Серверные fallbacks недоступны (%s); отключаю", exc.message)
                self._use_fallbacks = False
        return await self._client.messages.create(**kwargs)


def _is_fallback_error(exc: anthropic.BadRequestError) -> bool:
    """Ошибка 400 относится именно к параметру fallbacks/бета-заголовку, а не к запросу в целом."""
    message = (getattr(exc, "message", "") or str(exc)).lower()
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    param = ""
    if isinstance(body.get("error"), dict):
        param = str(body["error"].get("param") or "")
        message += " " + str(body["error"].get("message") or "").lower()
    return "fallback" in message or "fallback" in param.lower() or "server-side-fallback" in message
