"""Settings from environment variables.

The container's init step validates the same variables before the app
starts, so a typo stops the container with a clear message. The checks here
cover the native start and the tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """A setting is missing or malformed."""


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    log_level: str
    session_days: int
    off_lookup: bool
    off_language: str
    trusted_proxies: str
    max_upload_mb: int
    currency: str

    @property
    def database(self) -> Path:
        return self.data_dir / "buybutler.sqlite3"

    @property
    def images(self) -> Path:
        return self.data_dir / "images"

    @property
    def backups(self) -> Path:
        return self.data_dir / "backups"


def load() -> Settings:
    level = os.environ.get("BUYBUTLER_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"BUYBUTLER_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {level!r}")

    language = os.environ.get("BUYBUTLER_OFF_LANGUAGE", "en").strip().lower() or "en"
    if not (len(language) == 2 and language.isascii() and language.isalpha()):
        raise ConfigError(f"BUYBUTLER_OFF_LANGUAGE must be a two-letter language code, got {language!r}")

    currency = os.environ.get("BUYBUTLER_CURRENCY", "EUR").strip().upper() or "EUR"
    if not (len(currency) == 3 and currency.isascii() and currency.isalpha()):
        raise ConfigError(f"BUYBUTLER_CURRENCY must be a three-letter currency code, got {currency!r}")

    return Settings(
        data_dir=Path(os.environ.get("BUYBUTLER_DATA_DIR", "/config")),
        host=os.environ.get("BUYBUTLER_HOST", "0.0.0.0"),
        port=_int("BUYBUTLER_PORT", 8080, 1, 65535),
        log_level=level,
        session_days=_int("BUYBUTLER_SESSION_DAYS", 90, 1, 3650),
        off_lookup=_bool("BUYBUTLER_OFF_LOOKUP", True),
        off_language=language,
        trusted_proxies=os.environ.get("BUYBUTLER_TRUSTED_PROXIES", "*").strip() or "*",
        max_upload_mb=_int("BUYBUTLER_MAX_UPLOAD_MB", 10, 1, 100),
        currency=currency,
    )
