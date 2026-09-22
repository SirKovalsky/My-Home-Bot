"""Конфигурация бота: чтение переменных из .env.

ВАЖНО (архитектурное требование):
    Здесь НЕ настраиваются и НЕ экспортируются HTTP_PROXY / HTTPS_PROXY / ALL_PROXY
    в окружение процесса. Никакого «системного» прокси быть не должно.
    Прокси используется ТОЛЬКО явным аргументом ``proxies=...`` в HTTP-сессиях
    трекеров (см. ``trackers/base.py``), а клиент Transmission работает вообще
    без прокси (см. ``transmission_client.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

BASE_DIR: Path = Path(__file__).resolve().parent

# Загружаем .env один раз при импорте модуля.
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------- #
#  Хелперы чтения
# --------------------------------------------------------------------------- #
def _get(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _get_list(name: str, default: str = "") -> List[str]:
    raw = _get(name, default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (BASE_DIR / p)


# --------------------------------------------------------------------------- #
#  Категории и роутинг по папкам
# --------------------------------------------------------------------------- #
CATEGORY_SERIES = "series"
CATEGORY_FILMS = "films"
CATEGORY_OTHER = "other"

# Ключевые слова для автоопределения категории (по имени торрента / magnet dn=).
SERIES_KEYWORDS: List[str] = _get_list(
    "SERIES_KEYWORDS",
    "сериал,сериалы,сезон,season,s0,s0e,serial,tv-shows,tvshow",
)
FILMS_KEYWORDS: List[str] = _get_list(
    "FILMS_KEYWORDS",
    "фильм,фильмы,кино,movie,movies,film,films",
)


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    telegram_token: str = _get("TELEGRAM_BOT_TOKEN")
    allowed_user_ids: List[int] = field(
        default_factory=lambda: [
            int(x) for x in _get_list("ALLOWED_USER_IDS") if x.isdigit()
        ]
    )

    # --- Прокси для трекеров (используется ТОЛЬКО в trackers/*) ---
    proxy_url: str = _get("PROXY_URL", "socks5h://127.0.0.1:1080")
    proxy_timeout: int = _get_int("PROXY_TIMEOUT", 30)

    # --- Transmission RPC (БЕЗ прокси) ---
    transmission_host: str = _get("TRANSMISSION_HOST", "127.0.0.1")
    transmission_port: int = _get_int("TRANSMISSION_PORT", 9091)
    transmission_user: str = _get("TRANSMISSION_USER")
    transmission_password: str = _get("TRANSMISSION_PASSWORD")
    transmission_path: str = _get("TRANSMISSION_RPC_PATH", "/transmission/rpc")
    transmission_protocol: str = _get("TRANSMISSION_PROTOCOL", "http")
    transmission_timeout: int = _get_int("TRANSMISSION_TIMEOUT", 30)

    # --- Папки загрузки ---
    download_dir_default: str = _get("DOWNLOAD_DIR_DEFAULT", "/volume1/Downloads/Other")
    download_dir_series: str = _get("DOWNLOAD_DIR_SERIES", "/volume1/Downloads/Series")
    download_dir_films: str = _get("DOWNLOAD_DIR_FILMS", "/volume1/Downloads/Films")

    # --- Трекеры ---
    rutracker_login: str = _get("RUTRACKER_LOGIN")
    rutracker_password: str = _get("RUTRACKER_PASSWORD")
    kinozal_login: str = _get("KINOZAL_LOGIN")
    kinozal_password: str = _get("KINOZAL_PASSWORD")

    # --- Файлы состояния ---
    cookies_file: str = _get("COOKIES_FILE", "cookies.pickle")
    cookies_ttl_days: int = _get_int("COOKIES_TTL_DAYS", 14)
    log_file: str = _get("LOG_FILE", "torrent-bot.log")
    log_level: str = _get("LOG_LEVEL", "INFO").upper()

    # --- Уведомления ---
    poll_interval: int = _get_int("POLL_INTERVAL", 15)
    notify_on_complete: bool = _get_bool("NOTIFY_ON_COMPLETE", True)

    user_agent: str = _get(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )

    # ------------------------------------------------------------------ #
    #  Производные свойства
    # ------------------------------------------------------------------ #
    @property
    def download_dirs(self) -> Dict[str, str]:
        return {
            CATEGORY_OTHER: self.download_dir_default,
            CATEGORY_SERIES: self.download_dir_series,
            CATEGORY_FILMS: self.download_dir_films,
        }

    def download_dir_for(self, category: str) -> str:
        return self.download_dirs.get(category, self.download_dir_default)

    @property
    def cookies_path(self) -> Path:
        return _abs(self.cookies_file)

    @property
    def log_path(self) -> Path:
        return _abs(self.log_file)

    # ------------------------------------------------------------------ #
    #  Прокси-словарь для requests
    # ------------------------------------------------------------------ #
    @property
    def proxies(self) -> Dict[str, str]:
        """Словарь для параметра ``proxies=`` в requests.

        Единственное место в проекте, где прокси превращается в готовую
        структуру. ``socks5h`` означает: DNS-резолвинг выполняет прокси
        (OpenWrt), а не Xpenology — это важно и для приватности, и потому что
        rutracker/kinozal могут быть недоступны по DNS с Xpenology.
        """
        return {"http": self.proxy_url, "https": self.proxy_url}

    def validate(self) -> Optional[str]:
        """Возвращает текст ошибки конфигурации или None, если всё ок."""
        if not self.telegram_token or self.telegram_token.startswith("123456:"):
            return "TELEGRAM_BOT_TOKEN не задан (см. .env.example)."
        if not self.proxy_url:
            return "PROXY_URL не задан — трекеры будут недоступны."
        if not self.proxy_url.startswith(("socks5h://", "socks5://", "http://", "https://")):
            return (
                "PROXY_URL должен начинаться с socks5h:// (рекомендуется), "
                "socks5://, http:// или https://"
            )
        return None


CONFIG = Config()
