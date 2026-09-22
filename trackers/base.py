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

import html as html_module
import logging
import os
import pickle
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# curl_cffi подделывает TLS/HTTP-фингерпринт реального браузера — именно это
# проходит проверку Cloudflare («Just a moment...»). Обычный requests она
# распознаёт и отдаёт челлендж. Если curl_cffi не установился/не импортируется,
# спокойно работаем на requests, как раньше.
try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests import exceptions as curl_exceptions

    HAVE_CURL_CFFI = True
except Exception:  # noqa: BLE001
    curl_requests = None
    curl_exceptions = None
    HAVE_CURL_CFFI = False

try:
    import certifi

    _CA_BUNDLE = certifi.where()
except Exception:  # noqa: BLE001
    _CA_BUNDLE = None

if HAVE_CURL_CFFI:
    _ProxyError = curl_exceptions.ProxyError
    _ConnectionError = curl_exceptions.ConnectionError
    _TimeoutError = curl_exceptions.Timeout
    _RequestError = curl_exceptions.RequestException
else:
    _ProxyError = requests.exceptions.ProxyError
    _ConnectionError = requests.exceptions.ConnectionError
    _TimeoutError = requests.exceptions.Timeout
    _RequestError = requests.exceptions.RequestException

log = logging.getLogger(__name__)

# Какой браузер подделывать. Можно переопределить: CURL_IMPERSONATE=chrome124
# (в curl_cffi 0.9.0 доступны chrome99..chrome131, safari18_0, firefox133).
IMPERSONATE = os.getenv("CURL_IMPERSONATE", "chrome").strip() or "chrome"

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


@dataclass
class SearchResult:
    """Одна найденная раздача."""

    tracker: str
    title: str
    url: str
    topic_id: str = ""
    size: str = ""
    seeds: int = 0
    leeches: int = 0


# --------------------------------------------------------------------------- #
#  Разбор HTML (best effort, без внешних зависимостей)
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_html(text: str) -> str:
    """Убирает теги и лишние пробелы из фрагмента HTML."""
    text = _TAG_RE.sub("", text or "")
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


# Целые числа в отдельных ячейках таблицы (например «1 234» как разряды).
_CELL_NUM_RE = re.compile(r">\s*([0-9][0-9\s\u00a0]*?)\s*<")


def row_numbers(row_html: str, max_n: int = 4) -> List[int]:
    """Первые числа из ячеек строки таблицы — эвристика для «сидов/личов».

    Разметка трекеров меняется, поэтому это best-effort: если числа не
    найдутся, вернётся пустой список, а сортировка просто не изменит порядок.
    """
    numbers: List[int] = []
    for raw in _CELL_NUM_RE.findall(row_html or ""):
        try:
            numbers.append(int(raw.replace(" ", "").replace("\u00a0", "")))
        except ValueError:
            continue
        if len(numbers) >= max_n:
            break
    return numbers


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
    def _build_session(self, user_agent: str):
        headers = {
            # Реальный браузерный UA — иначе бан.
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        if HAVE_CURL_CFFI:
            # >>> ЗДЕСЬ ЗАДАЁТСЯ ПРОКСИ <<< (та же явная структура, что и ниже)
            #
            # ВАЖНО: не подставляем свой User-Agent/Accept. Impersonate сам
            # выставляет согласованный набор заголовков под выбранный браузер;
            # если переопределить хотя бы UA, Cloudflare увидит расхождение
            # «TLS от Chrome 131 — заголовки от Chrome 124» и снова даст челлендж.
            session = curl_requests.Session(
                impersonate=IMPERSONATE,
                proxies=dict(self._proxies),
                timeout=self._timeout,
                trust_env=False,
                verify=_CA_BUNDLE if _CA_BUNDLE else True,
            )
            session.proxies = dict(self._proxies)
            session.trust_env = False
            return session

        session = requests.Session()

        # >>> ЗДЕСЬ ЗАДАЁТСЯ ПРОКСИ <<<
        # Явный словарь {'http': ..., 'https': ...}. Без него запросы пойдут
        # напрямую и упадут/будут заблокированы. socks5h = DNS через прокси.
        session.proxies.update(self._proxies)

        # Игнорируем системные HTTP_PROXY/HTTPS_PROXY/NO_PROXY, если они вдруг
        # появятся в окружении: маршрутизация должна быть только явной.
        session.trust_env = False
        session.headers.update(headers)
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

    def _cookies_for_pickle(self):
        """CookieJar для сериализации.

        У curl_cffi cookies — обёртка с .jar внутри; у requests это уже jar.
        """
        cookies = self._session.cookies
        return getattr(cookies, "jar", cookies)

    def save_cookies(self) -> None:
        path = self._cookies_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as fh:
                pickle.dump(
                    {
                        "saved_at": time.time(),
                        "cookies": self._cookies_for_pickle(),
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
        except _ProxyError as exc:
            raise ProxyUnavailableError(
                f"Прокси {self._proxies.get('https')} недоступен: {exc}"
            ) from exc
        except _TimeoutError as exc:
            raise TrackerError(f"Таймаут запроса к {url} через прокси") from exc
        except _ConnectionError as exc:
            raise ProxyUnavailableError(
                f"Ошибка соединения (прокси недоступен или трекер не резолвится): {exc}"
            ) from exc
        except _RequestError as exc:
            raise TrackerError(f"Ошибка HTTP-запроса к {url}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise TrackerError(f"Неожиданная ошибка запроса к {url}: {exc}") from exc

        self._check_captcha(response)
        return response

    def fetch(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._request("POST", url, **kwargs)

    @staticmethod
    def _check_captcha(response: requests.Response) -> None:
        if response.status_code in (403, 429):
            # Показываем начало ответа: по нему видно, это капча, бан IP,
            # требование авторизации или бан User-Agent.
            try:
                snippet = clean_html((response.text or "")[:4000])[:200]
            except Exception:  # noqa: BLE001
                snippet = ""
            detail = f" Ответ сервера: {snippet!r}" if snippet else ""
            raise CaptchaError(
                f"Трекер отдал {response.status_code}.{detail}"
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

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[SearchResult]:
        """Ищет раздачи по названию (запрос идёт через прокси)."""

    def search_html(self, query: str) -> str:
        """Сырой HTML страницы поиска — для диагностики парсера."""
        raise NotImplementedError

    def is_logged_in(self) -> bool:
        """Эвристика: есть ли хотя бы одна сессионная cookie."""
        return bool(self._session.cookies)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
