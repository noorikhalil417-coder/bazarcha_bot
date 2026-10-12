"""Environment-based configuration for Bazaarche."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _integer(name: str, value: str | None, default: int, minimum: int = 1) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} باید عدد صحیح باشد.") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{name} باید حداقل {minimum} باشد.")
    return parsed


def _chat_id(name: str, value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} باید شناسهٔ عددی تلگرام باشد.") from exc


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset[int]
    group_id: int | None
    publication_chat_id: int | None
    database_path: Path
    log_level: str
    default_ad_expiry_days: int
    max_active_ads_per_user: int
    moderation_review_mode: str
    backup_directory: Path
    automatic_backup_interval_hours: int
    backup_retention_days: int
    cleanup_retention_days: int
    welcome_message: str
    group_rules: str

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Config:
        values = os.environ if environ is None else environ
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or ":" not in token:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN تنظیم نشده یا قالب آن درست نیست.")
        try:
            admin_ids = frozenset(int(item.strip()) for item in values.get("ADMIN_IDS", "").split(",") if item.strip())
        except ValueError as exc:
            raise ConfigurationError("ADMIN_IDS باید فهرستی از شناسه‌های عددی باشد.") from exc
        if not admin_ids or any(user_id <= 0 for user_id in admin_ids):
            raise ConfigurationError("حداقل یک شناسهٔ مثبت مدیر در ADMIN_IDS لازم است.")
        review_mode = values.get("MODERATION_REVIEW_MODE", "required").strip().lower()
        if review_mode not in {"required", "auto"}:
            raise ConfigurationError("MODERATION_REVIEW_MODE فقط required یا auto است.")
        log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL معتبر نیست.")
        return cls(
            token=token,
            admin_ids=admin_ids,
            group_id=_chat_id("GROUP_ID", values.get("GROUP_ID")),
            publication_chat_id=_chat_id("AD_PUBLICATION_CHAT_ID", values.get("AD_PUBLICATION_CHAT_ID")),
            database_path=Path(values.get("DATABASE_PATH", "data/bazaarche.sqlite3")),
            log_level=log_level,
            default_ad_expiry_days=_integer("DEFAULT_AD_EXPIRY_DAYS", values.get("DEFAULT_AD_EXPIRY_DAYS"), 30),
            max_active_ads_per_user=_integer("MAX_ACTIVE_ADS_PER_USER", values.get("MAX_ACTIVE_ADS_PER_USER"), 5),
            moderation_review_mode=review_mode,
            backup_directory=Path(values.get("BACKUP_DIRECTORY", "backups")),
            automatic_backup_interval_hours=_integer("AUTOMATIC_BACKUP_INTERVAL_HOURS", values.get("AUTOMATIC_BACKUP_INTERVAL_HOURS"), 24, minimum=0),
            backup_retention_days=_integer("BACKUP_RETENTION_DAYS", values.get("BACKUP_RETENTION_DAYS"), 14),
            cleanup_retention_days=_integer("CLEANUP_RETENTION_DAYS", values.get("CLEANUP_RETENTION_DAYS"), 30),
            welcome_message=values.get("WELCOME_MESSAGE", "به بازارچه خوش آمدید! برای ثبت یا جست‌وجوی آگهی از منوی ربات استفاده کنید."),
            group_rules=values.get("GROUP_RULES", "با احترام رفتار کنید. آگهی نادرست، تکراری و محتوای خلاف قانون مجاز نیست."),
        )