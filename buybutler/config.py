"""Settings from environment variables.

The container's init step validates the same variables before the app
starts, so a typo stops the container with a clear message. The checks here
cover the native start and the tests.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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


def _public_url() -> str:
    """The address users open Buybutler at; passkeys are bound to it."""
    raw = os.environ.get("BUYBUTLER_PUBLIC_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query
            or parts.fragment or parts.username or parts.password):
        raise ConfigError(
            f"BUYBUTLER_PUBLIC_URL must be the address in the browser, such as https://buybutler.example.com,"
            f" got {raw!r}"
        )
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise ConfigError(f"BUYBUTLER_PUBLIC_URL needs a host name, passkeys don't work with an IP address: {raw!r}")
    if parts.scheme == "http" and parts.hostname != "localhost":
        raise ConfigError(f"BUYBUTLER_PUBLIC_URL must start with https:// (only localhost may use http), got {raw!r}")
    try:
        parts.port
    except ValueError:
        raise ConfigError(f"BUYBUTLER_PUBLIC_URL has an invalid port: {raw!r}") from None
    return f"{parts.scheme}://{parts.netloc.lower()}"


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
    # Empty when unset; passkeys are then off, TOTP works without it.
    public_url: str = ""

    @property
    def passkey_rp_id(self) -> str:
        """The WebAuthn relying party ID: the public host name."""
        return urlsplit(self.public_url).hostname or ""

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
        public_url=_public_url(),
    )
