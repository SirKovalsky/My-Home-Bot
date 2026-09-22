"""Общий интерфейс трекеров.

КЛЮЧЕВОЕ АРХИТЕКТУРНОЕ ПРАВИЛО:
    Вся сеть к трекерам (rutracker.org / kinozal.me) идёт ТОЛЬКО через прокси
    на OpenWrt. Прокси задаётся явным аргументом ``proxies=...`` у
    ``requests.Session`` ниже — это единственный корректный способ, потому что:

    * никакие HTTP_PROXY/HTTPS_PROXY в окружении не используются (trust_env=False);
    * Transmission и локальные адреса этот сеанс не трогает вообще;
    * схема ``socks5h`` резолвит DNS на стороне OpenWrt.

    Сессии трекеров и клиент Transmission полностью изолированы друг от друга.
"""

from __future__ import annotations

import logging
import pickle
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)

# Маркеры капчи/антибота, встречающиеся на rutracker/kinozal.
CAPTCHA_MARKERS = (
    "captcha",
    "капча",
    "подтвердите, что вы не робот",
    "are you human",
    "cloudflare",
    "ddos-guard",
    "attention required",
)

# Маркеры разлогиненной сессии.
LOGGED_OUT_MARKERS = (
    "login.php",
    "takelogin.php",
    "вы не вошли",
    "not logged in",
)


class TrackerError(Exception):
    """Базовая ошибка трекера."""


class ProxyUnavailableError(TrackerError):
    """Прокси на OpenWrt недоступен (или неверные данные)."""


class CaptchaError(TrackerError):
    """Трекер отдал капчу/антибот."""


class NotLoggedInError(TrackerError):
    """Сессия недействительна, требуется /login_*."""


@dataclass
class TorrentResult:
    """Результат разбора страницы трекера."""

    name: str = ""
    source: str = ""
    page_url: str = ""
    # Ровно одно из двух полей заполнено:
    torrent_bytes: Optional[bytes] = None
    magnet: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_magnet(self) -> bool:
        return bool(self.magnet) and not self.torrent_bytes


class BaseTracker(ABC):
    """Базовый класс трекера с изолированной (проксированной) HTTP-сессией."""

    name: str = "tracker"
    base_url: str = ""

    def __init__(
        self,
        proxies: Dict[str, str],
        cookies_path: Path,
        user_agent: str,
        timeout: int = 30,
    ) -> None:
        self._proxies = proxies
        self._cookies_path = Path(cookies_path)
        self._timeout = timeout
        self._session = self._build_session(user_agent)
        self._loaded_at: float = 0.0

    # ------------------------------------------------------------------ #
    #  Сессия
    # ------------------------------------------------------------------ #
    def _build_session(self, user_agent: str) -> requests.Session:
        session = requests.Session()

        # >>> ЗДЕСЬ ЗАДАЁТСЯ ПРОКСИ <<<
        # Явный словарь {'http': ..., 'https': ...}. Без него запросы пойдут
        # напрямую и упадут/будут заблокированы. socks5h = DNS через прокси.
        session.proxies.update(self._proxies)

        # Игнорируем системные HTTP_PROXY/HTTPS_PROXY/NO_PROXY, если они вдруг
        # появятся в окружении: маршрутизация должна быть только явной.
        session.trust_env = False

        session.headers.update(
            {
                # Реальный браузерный UA — иначе бан.
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Connection": "keep-alive",
            }
        )
        return session

    @property
    def session(self) -> requests.Session:
        return self._session

    @property
    def headers(self) -> Dict[str, str]:
        return self._session.headers

    # ------------------------------------------------------------------ #
    #  Cookies (персистентность между запусками)
    # ------------------------------------------------------------------ #
    def _cookies_file(self) -> Path:
        return self._cookies_path.with_suffix(f".{self.name}.pickle")

    def save_cookies(self) -> None:
        path = self._cookies_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as fh:
                pickle.dump(
                    {
                        "saved_at": time.time(),
                        "cookies": self._session.cookies,
                    },
                    fh,
                )
            log.info("[%s] cookies сохранены: %s", self.name, path)
        except OSError as exc:
            log.warning("[%s] не удалось сохранить cookies: %s", self.name, exc)

    def load_cookies(self, ttl_days: int = 14) -> bool:
        """Загружает cookies, если файл свежий. Возвращает True при успехе."""
        path = self._cookies_file()
        if not path.exists():
            return False
        try:
            with path.open("rb") as fh:
                payload = pickle.load(fh)
            age_days = (time.time() - payload.get("saved_at", 0)) / 86400
            if ttl_days and age_days > ttl_days:
                log.info("[%s] cookies устарели (%.1f дн.), нужен повторный логин", self.name, age_days)
                return False
            self._session.cookies = payload["cookies"]
            self._loaded_at = payload.get("saved_at", 0.0)
            log.info("[%s] cookies загружены (возраст %.1f дн.)", self.name, age_days)
            return True
        except (OSError, pickle.PickleError, KeyError, EOFError) as exc:
            log.warning("[%s] cookies повреждены, игнорируем: %s", self.name, exc)
            return False

    # ------------------------------------------------------------------ #
    #  HTTP-хелперы (все — через прокси)
    # ------------------------------------------------------------------ #
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)

        # Явно дублируем прокси на уровне запроса. Это защита от случайной
        # подмены session.proxies где-то в коде: даже если кто-то отредактирует
        # сессию, конкретный запрос всё равно уйдёт через OpenWrt.
        kwargs["proxies"] = self._proxies

        try:
            response = self._session.request(method, url, **kwargs)
        except requests.exceptions.ProxyError as exc:
            raise ProxyUnavailableError(
                f"Прокси {self._proxies.get('https')} недоступен: {exc}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProxyUnavailableError(
                f"Ошибка соединения (возможно, прокси недоступен или трекер не резолвится): {exc}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise TrackerError(f"Таймаут запроса к {url} через прокси") from exc
        except requests.exceptions.RequestException as exc:
            raise TrackerError(f"Ошибка HTTP-запроса к {url}: {exc}") from exc

        self._check_captcha(response)
        return response

    def fetch(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._request("POST", url, **kwargs)

    @staticmethod
    def _check_captcha(response: requests.Response) -> None:
        if response.status_code in (403, 429):
            raise CaptchaError(
                f"Трекер отдал {response.status_code} (вероятно, антибот/капча или бан UA)"
            )
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            return
        body = response.text[:50_000].lower()
        for marker in CAPTCHA_MARKERS:
            if marker in body:
                raise CaptchaError(
                    "Обнаружена капча/антибот-защита. "
                    "Попробуйте позже, смените прокси или UA."
                )

    # ------------------------------------------------------------------ #
    #  Интерфейс
    # ------------------------------------------------------------------ #
    @abstractmethod
    def login(self, login: str, password: str) -> bool:
        """Выполняет логин и сохраняет cookies. True при успехе."""

    @abstractmethod
    def resolve(self, url: str) -> TorrentResult:
        """Разбирает страницу раздачи и возвращает .torrent/magnet."""

    def is_logged_in(self) -> bool:
        """Эвристика: есть ли хотя бы одна сессионная cookie."""
        return bool(self._session.cookies)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
