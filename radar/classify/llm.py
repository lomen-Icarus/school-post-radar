"""Классификация постов моделью Claude со структурированным JSON-ответом."""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger(__name__)

CLASSIFIER_VERSION = "llm-v1"

KINDS = ("olympiad", "competition", "sport", "award", "admission", "trip", "other")


class Verdict(BaseModel):
    is_achievement: bool
    confidence: float = Field(ge=0.0, le=1.0)
    about_students: bool
    kind: str
    headline: str = Field(max_length=120)
    summary: str = Field(max_length=600)
    students: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=400)


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_achievement": {
            "type": "boolean",
            "description": "true, если пост сообщает о достижении/успехе/участии учеников школы, о котором уместно поздравить",
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "about_students": {
            "type": "boolean",
            "description": "Речь именно об учениках (а не только о педагогах или школе как учреждении)",
        },
        "kind": {"type": "string", "enum": list(KINDS)},
        "headline": {"type": "string", "description": "Заголовок до 80 символов, по-русски, без эмодзи"},
        "summary": {"type": "string", "description": "1-2 предложения: кто, что, где, какой результат"},
        "students": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Имена/фамилии учеников, если названы",
        },
        "reason": {"type": "string", "description": "Кратко, почему принято решение"},
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

Отвечай строго JSON по схеме. headline — короткий заголовок по-русски (например: «Победа на республиканской олимпиаде по физике»). summary — 1–2 предложения с сутью: кто (класс/имена, если есть), что, где, результат. students — только явно названные имена/фамилии учеников; не придумывай."""


class ClaudeClassifier:
    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        effort: str = "low",
        use_fallbacks: bool = True,
        api_key: str | None = None,
        timeout: float = 60.0,
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

    async def classify(self, text: str, school_names: list[str], municipality: str | None = None) -> Verdict:
        user_message = self._build_user_message(text, school_names, municipality)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_message}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
        }
        response = await self._create(kwargs)
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
        text_block = next((b for b in response.content if getattr(b, "type", "") == "text"), None)
        if text_block is None:
            raise RuntimeError("Пустой ответ модели")
        try:
            data = json.loads(text_block.text)
            verdict = Verdict.model_validate(data)
        except (ValueError, ValidationError) as exc:
            raise RuntimeError(f"Некорректный JSON от модели: {exc}") from exc
        if verdict.kind not in KINDS:
            verdict.kind = "other"
        return verdict

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        if self._use_fallbacks:
            try:
                return await self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            except anthropic.BadRequestError as exc:
                # Аккаунт/регион без поддержки бета-фолбэков — работаем без них дальше.
                log.warning("Серверные fallbacks недоступны (%s); отключаю", exc.message)
                self._use_fallbacks = False
        return await self._client.messages.create(**kwargs)
