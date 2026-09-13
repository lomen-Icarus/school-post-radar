from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_int_list(raw: str) -> list[int]:
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(int(chunk))
    return out


class Settings(BaseSettings):
    """Все настройки читаются из окружения / .env (см. .env.example)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # Telegram
    bot_token: str = Field(min_length=1)
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")
    allowed_user_ids_raw: str = Field(default="", alias="ALLOWED_USER_IDS")

    # VK
    vk_access_token: str = ""
    vk_api_version: str = "5.199"
    vk_api_base: str = "https://api.vk.ru/method/"
    vk_rps: float = Field(default=3.0, gt=0, le=20)
    # execute (пакет до 25 вызовов) доступен только пользовательскому/групповому токену, но не сервисному ключу.
    vk_use_execute: bool = False
    vk_execute_batch: int = Field(default=10, ge=1, le=25)

    # Claude
    anthropic_api_key: str = ""
    # Имена CLASSIFIER_* выбраны, чтобы не пересекаться с переменными окружения CLAUDE_* других инструментов.
    claude_model: str = Field(default="claude-opus-5", alias="CLASSIFIER_MODEL")
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(
        default="low", alias="CLASSIFIER_EFFORT"
    )
    claude_fallbacks: bool = Field(default=True, alias="CLASSIFIER_FALLBACKS")
    classifier_concurrency: int = Field(default=3, ge=1, le=10)

    # Расписание
    timezone: str = "Europe/Moscow"
    scan_windows: str = "08:00-12:45,17:00-20:45"
    scan_tick_seconds: int = Field(default=300, ge=30, le=3600)
    lookback_hours: int = Field(default=48, ge=1, le=24 * 14)
    monitor_scope: Literal["all", "official"] = "all"
    wall_count: int = Field(default=20, ge=5, le=100)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    default_digest_times: str = "13:00,21:00"
    catchup_max_per_tick: int = Field(default=60, ge=1, le=500)

    # Хранилище
    db_path: Path = Path("data/radar.sqlite")
    registry_seed_path: Path = Path("data/registry_seed.sqlite")
    log_level: str = "INFO"

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)  # бросит исключение, если зона неизвестна
        return value

    @property
    def admin_ids(self) -> list[int]:
        return _parse_int_list(self.admin_ids_raw)

    @property
    def allowed_user_ids(self) -> list[int]:
        return _parse_int_list(self.allowed_user_ids_raw)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def vk_enabled(self) -> bool:
        return bool(self.vk_access_token.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
