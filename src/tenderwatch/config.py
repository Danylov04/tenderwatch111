"""Налаштування з оточення (.env) + правила з TOML."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/tenderwatch.sqlite3"))
    rules_path: str = field(default_factory=lambda: _env("RULES_PATH", ""))
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    dashboard_user: str = field(default_factory=lambda: _env("DASHBOARD_USER"))
    dashboard_password: str = field(default_factory=lambda: _env("DASHBOARD_PASSWORD"))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL"))
    user_agent: str = field(
        default_factory=lambda: _env("USER_AGENT", "tenderwatch/0.1 (+monitoring public tenders; contact: see repo)")
    )
    request_interval: float = field(default_factory=lambda: float(_env("REQUEST_INTERVAL", "0.35")))
    incremental_minutes: int = field(default_factory=lambda: int(_env("INCREMENTAL_MINUTES", "60")))
    lookback_days: int = field(default_factory=lambda: int(_env("LOOKBACK_DAYS", "3")))
    sweep_hour_warsaw: int = field(default_factory=lambda: int(_env("SWEEP_HOUR_WARSAW", "2")))
    digest_hour_kyiv: int = field(default_factory=lambda: int(_env("DIGEST_HOUR_KYIV", "7")))
    body_scan: bool = field(default_factory=lambda: _env("BODY_SCAN", "1") not in ("0", "false", "no"))
    enabled_sources: list[str] = field(
        default_factory=lambda: [s for s in _env("SOURCES", "ezam,ted,pz,bk").split(",") if s]
    )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def load_settings() -> Settings:
    return Settings()


@dataclass
class Rules:
    raw: dict

    @property
    def cpv_strong(self) -> list[str]:
        return list(self.raw["cpv"]["strong"])

    @property
    def cpv_medium(self) -> list[str]:
        return list(self.raw["cpv"]["medium"])

    @property
    def construction_prefixes(self) -> list[str]:
        return list(self.raw["cpv"]["construction_prefixes"])

    @property
    def title_keywords(self) -> list[str]:
        return list(self.raw["search"]["title_keywords"])

    @property
    def pz_keywords(self) -> list[str]:
        return list(self.raw["search"]["pz_keywords"])

    @property
    def cpv_codes(self) -> list[str]:
        return list(self.raw["search"]["cpv_codes"])

    @property
    def canaries(self) -> list[str]:
        return list(self.raw.get("canaries", {}).get("ids", []))

    @property
    def reminder_days(self) -> list[int]:
        return list(self.raw.get("notify", {}).get("reminder_days", [3, 1]))


def _read_default() -> dict:
    with resources.files("tenderwatch").joinpath("default_rules.toml").open("rb") as fh:
        return tomllib.load(fh)


@lru_cache(maxsize=8)
def load_rules(path: str = "") -> Rules:
    if path and Path(path).exists():
        with open(path, "rb") as fh:
            return Rules(tomllib.load(fh))
    return Rules(_read_default())
