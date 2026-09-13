"""Оркестратор классификации: ключевые слова -> модель (если есть ключ)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from radar.classify import keywords
from radar.classify.llm import ClaudeClassifier, Verdict

log = logging.getLogger(__name__)

KEYWORD_VERSION = "keywords-v1"


@dataclass
class ClassificationResult:
    is_achievement: bool
    confidence: float
    kind: str
    headline: str
    summary: str
    students: list[str] = field(default_factory=list)
    keyword_hits: list[str] = field(default_factory=list)
    classifier_version: str = KEYWORD_VERSION
    about_students: bool = True
    reason: str = ""
    skipped_llm: bool = False


class Classifier:
    def __init__(
        self, llm: ClaudeClassifier | None, concurrency: int = 3, min_confidence: float = 0.6
    ) -> None:
        self._llm = llm
        self._sem = asyncio.Semaphore(concurrency)
        self.min_confidence = min_confidence

    @property
    def llm_enabled(self) -> bool:
        return self._llm is not None

    @property
    def version(self) -> str:
        return self._llm.version if self._llm else KEYWORD_VERSION

    async def classify(
        self, text: str, school_names: list[str], municipality: str | None = None, *, force_llm: bool = False
    ) -> ClassificationResult:
        consider, hits = keywords.should_consider(text)
        if not consider and not force_llm:
            return ClassificationResult(
                is_achievement=False,
                confidence=0.0,
                kind="other",
                headline="",
                summary="",
                keyword_hits=hits,
                classifier_version=KEYWORD_VERSION,
                skipped_llm=True,
                reason="нет ключевых слов",
            )
        if self._llm is None:
            is_ach, score, kind = keywords.keyword_only_verdict(text)
            snippet = keywords.normalize_text(text)[:200]
            return ClassificationResult(
                is_achievement=is_ach,
                confidence=score,
                kind=kind,
                headline=_headline_from_text(text),
                summary=snippet,
                keyword_hits=hits,
                classifier_version=KEYWORD_VERSION,
                reason="только ключевые слова",
            )
        async with self._sem:
            verdict: Verdict = await self._llm.classify(text, school_names, municipality)
        is_ach = verdict.is_achievement and verdict.about_students
        return ClassificationResult(
            is_achievement=is_ach,
            confidence=verdict.confidence
            if is_ach
            else min(verdict.confidence, 0.49)
            if verdict.is_achievement
            else verdict.confidence,
            kind=verdict.kind,
            headline=verdict.headline.strip() or _headline_from_text(text),
            summary=verdict.summary.strip(),
            students=[s.strip() for s in verdict.students if s.strip()],
            keyword_hits=hits,
            classifier_version=self._llm.version,
            about_students=verdict.about_students,
            reason=verdict.reason,
        )


def _headline_from_text(text: str) -> str:
    first = keywords.normalize_text(text).split(". ")[0]
    return (first[:77] + "…") if len(first) > 80 else first
