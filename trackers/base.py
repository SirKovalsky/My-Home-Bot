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
from urllib.parse import urlparse

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
# ВАЖНО: нельзя использовать просто «cloudflare» — на обычных страницах
# трекеров есть скрипт static.cloudflareinsights.com, из-за которого живая
# страница с результатами поиска принималась за челлендж.
CAPTCHA_MARKERS = (
    "just a moment",
    "checking your browser",
    "challenge-platform",
    "cf-chl",
    "cf_chl",
    "attention required",
    "ddos-guard",
    "are you human",
    "captcha",
    "капча",
    "подтвердите, что вы не робот",
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
# Размер раздачи в строке таблицы: «1.37 GB», «4,7 ГБ» и т.п.
_SIZE_RE = re.compile(
    r"(\d[\d\s\u00a0.,]*)\s*(TB|GB|MB|KB|ТБ|ГБ|МБ|КБ|байт|байта|байтов)\b",
    re.IGNORECASE,
)


def _format_size(match: "re.Match") -> str:
    """Приводим найденный размер к виду «293 MB» / «37.85 ГБ»."""
    number = match.group(1).replace("\u00a0", " ").strip().rstrip(".,")
    return f"{number} {match.group(2)}".strip()


# Ячейки таблицы: у обоих трекеров размер лежит в отдельном <td>.
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)


def row_size(row_html: str) -> str:
    """Размер раздачи из HTML-строки таблицы (best effort).

    Сначала ищем ячейку, которая начинается с размера («293 MB ↓» или
    «37.85 ГБ»), и лишь потом — любое вхождение единицы измерения в
    строке. Второй вариант нужен как запасной: без него число с «MB»
    находится в названии раздачи («MP3, 320 kbps») и это ложное
    срабатывание.
    """
    row_html = row_html or ""

    for cell in _TD_RE.findall(row_html):
        match = _SIZE_RE.match(clean_html(cell).strip())
        if match:
            return _format_size(match)

    match = _SIZE_RE.search(row_html)
    return _format_size(match) if match else ""


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


# Решатель Cloudflare. Импорт стоит здесь, а не в шапке файла, чтобы не
# переупорядочивать уже сложившийся блок импортов: solver.py от base.py не
# зависит, циклического импорта нет.
from .solver import FlareSolverr, SolverError


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
        solver=None,
    ) -> None:
        self._proxies = proxies
        self._cookies_path = Path(cookies_path)
        self._timeout = timeout
        self._user_agent = user_agent
        # Решатель Cloudflare (FlareSolverr). Может быть None — тогда челлендж
        # не обходим, а показываем пользователю обычную ошибку.
        self._solver = solver
        # Защита от повторного входа, пока идёт решение челленджа.
        self._solving = False
        # True, когда UA задан явно (например, взятый из браузера вместе с
        # cf_clearance). Тогда переопределяем UA поверх impersonate.
        self._custom_ua = False
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
            extra = {"User-Agent": user_agent} if self._custom_ua else {}
            session = curl_requests.Session(
                impersonate=IMPERSONATE,
                headers=extra,
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

    # ------------------------------------------------------------------ #
    #  Импорт cookies / UA из браузера (для обхода Cloudflare)
    # ------------------------------------------------------------------ #
    def _host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def set_user_agent(self, user_agent: str) -> None:
        """Задать UA вручную (должен совпадать с тем, где получен cf_clearance)."""
        user_agent = (user_agent or "").strip()
        if not user_agent or user_agent == self._user_agent:
            return
        old_session = self._session
        self._user_agent = user_agent
        self._custom_ua = True
        self._session = self._build_session(user_agent)
        try:
            self._session.cookies = old_session.cookies
        except Exception:  # noqa: BLE001
            pass
        try:
            old_session.close()
        except Exception:  # noqa: BLE001
            pass

    def import_cookies(self, raw: str) -> int:
        """Разобрать строку вида ``name=value; name2=value2`` и подставить в сессию."""
        parsed: Dict[str, str] = {}
        for chunk in re.split(r"[;\n]", raw or ""):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name, value = name.strip(), value.strip()
            if name:
                parsed[name] = value

        if not parsed:
            raise TrackerError("не нашёл ни одной пары name=value")

        host = self._host()
        for name, value in parsed.items():
            self._session.cookies.set(name, value, domain=host, path="/")
        return len(parsed)

    def verify_session(self) -> str:
        """Дёрнуть главную страницу и сказать, прошла ли проверка Cloudflare."""
        response = self.fetch(self.base_url)
        return f"HTTP {response.status_code}, {len(response.text)} байт"

    def _cookies_for_pickle(self) -> List[Dict[str, str]]:
        """Cookies в простом (сериализуемом) виде.

        Раньше сюда возвращался сам CookieJar — и pickle.dump падал с
        «cannot pickle '_thread.RLock' object»: внутри jar лежит блокировка
        (и у requests, и у обёртки curl_cffi). Поэтому сохраняем плоский
        список словарей, а восстанавливаем через session.cookies.set(...).
        """
        container = self._session.cookies
        jar = getattr(container, "jar", None)

        # У curl_cffi итерация по Cookies отдаёт ИМЕНА строк, а не объекты
        # Cookie: из-за этого список получался пустым и cookies не сохранялись
        # (каждый поиск заново решал Cloudflare). Поэтому сначала пробуем
        # внутренний CookieJar — в нём лежат Cookie с доменом и путём.
        raw_cookies: List[Any] = []
        for source in (jar, container):
            if source is None:
                continue
            try:
                found = list(source)
            except Exception:  # noqa: BLE001
                found = []
            if found and not isinstance(found[0], str):
                raw_cookies = found
                break
        items: List[Dict[str, str]] = []
        for cookie in raw_cookies:
            name = getattr(cookie, "name", "") or ""
            if not name:
                continue
            items.append(
                {
                    "name": name,
                    "value": getattr(cookie, "value", "") or "",
                    "domain": getattr(cookie, "domain", "") or "",
                    "path": getattr(cookie, "path", "") or "/",
                }
            )

        if not items:
            # Запасной путь: словарь name->value. Домен неизвестен — при
            # загрузке подставим host трекера.
            getter = getattr(container, "get_dict", None)
            if callable(getter):
                try:
                    data = getter() or {}
                except Exception:  # noqa: BLE001
                    data = {}
                if isinstance(data, dict):
                    for name, value in data.items():
                        items.append(
                            {
                                "name": str(name),
                                "value": str(value),
                                "domain": "",
                                "path": "/",
                            }
                        )
        return items

    def save_cookies(self) -> None:
        path = self._cookies_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as fh:
                pickle.dump(
                    {
                        "saved_at": time.time(),
                        "cookies": self._cookies_for_pickle(),
                        # UA важен: cf_clearance привязан к паре IP + User-Agent.
                        "user_agent": self._user_agent,
                        "proxy": self._proxies.get("https") or self._proxies.get("http") or "",
                    },
                    fh,
                )
            log.info("[%s] cookies сохранены: %s", self.name, path)
        except (OSError, TypeError, pickle.PickleError) as exc:
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
            saved_ua = (payload.get("user_agent") or "").strip()
            if saved_ua and saved_ua != self._user_agent:
                # Отпечаток cf_clearance привязан к UA — восстанавливаем его.
                self.set_user_agent(saved_ua)

            saved_proxy = (payload.get("proxy") or "").strip()
            current_proxy = self._proxies.get("https") or self._proxies.get("http") or ""
            if saved_proxy and saved_proxy != current_proxy:
                log.warning(
                    "[%s] cookies получены через другой прокси (%s), а сейчас %s — "
                    "cf_clearance может не подойти",
                    self.name, saved_proxy, current_proxy or "direct",
                )

            # CookieJar присваивать нельзя: в старых файлах лежал объект jar
            # (именно на нём и падала запись), в новых — список словарей.
            saved = payload.get("cookies")
            if isinstance(saved, dict):
                saved = [{"name": k, "value": v} for k, v in saved.items()]

            restored = 0
            if isinstance(saved, list):
                host = self._host()
                for item in saved:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "")
                    if not name:
                        continue
                    value = str(item.get("value") or "")
                    domain = str(item.get("domain") or "") or host
                    cookie_path = str(item.get("path") or "/") or "/"
                    try:
                        self._session.cookies.set(
                            name, value, domain=domain, path=cookie_path
                        )
                    except Exception:  # noqa: BLE001
                        try:
                            self._session.cookies.set(name, value)
                        except Exception:  # noqa: BLE001
                            continue
                    restored += 1

            if not restored:
                log.info(
                    "[%s] в %s нет пригодных cookies — нужен повторный логин",
                    self.name,
                    path,
                )
                return False

            self._loaded_at = payload.get("saved_at", 0.0)
            log.info(
                "[%s] cookies загружены (%d шт., возраст %.1f дн.)",
                self.name,
                restored,
                age_days,
            )
            return True
        except (OSError, TypeError, AttributeError, pickle.PickleError, KeyError, EOFError) as exc:
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

        response = self._send(method, url, **kwargs)
        try:
            self._check_captcha(response)
        except CaptchaError:
            # Похоже на челлендж Cloudflare. Если рядом поднят FlareSolverr,
            # просим его пройти проверку и повторяем запрос ровно один раз.
            if not self._solve_challenge(url):
                raise
            response = self._send(method, url, **kwargs)
            self._check_captcha(response)
        return response

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """Один сетевой вызов с переводом ошибок библиотеки в наши типы."""
        try:
            return self._session.request(method, url, **kwargs)
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

    def fetch(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._request("POST", url, **kwargs)

    # ------------------------------------------------------------------ #
    #  Обход Cloudflare через FlareSolverr
    # ------------------------------------------------------------------ #
    def apply_solver_session(
        self, cookies: Dict[str, str], user_agent: str = ""
    ) -> None:
        """Подставить cookies и User-Agent, полученные от FlareSolverr.

        Порядок важен: сначала меняем UA (это пересобирает сессию), потом
        кладём cookies — иначе они потеряются при пересборке.
        """
        user_agent = (user_agent or "").strip()
        if user_agent and user_agent != self._user_agent:
            self.set_user_agent(user_agent)

        host = self._host()
        for name, value in cookies.items():
            try:
                self._session.cookies.set(name, value, domain=host, path="/")
            except Exception:  # noqa: BLE001
                try:
                    self._session.cookies.set(name, value)
                except Exception:  # noqa: BLE001
                    log.debug("[%s] не смог поставить cookie %s", self.name, name)

        try:
            self.save_cookies()
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] cookies не сохранились: %s", self.name, exc)

    def _solve_challenge(self, url: str) -> bool:
        """Пройти челлендж Cloudflare и обновить сессию.

        Возвращает True, если cookies обновлены и запрос имеет смысл повторить.
        Исключений не бросает: любая проблема решателя — это просто «не
        получилось», и вызывающий код покажет обычную ошибку капита.
        """
        if self._solver is None or not getattr(self._solver, "enabled", False):
            return False
        if self._solving:  # защита от повторного входа
            return False

        if self._proxies:
            # cf_clearance привязан к внешнему IP. Трекеры ходят через прокси,
            # а FlareSolverr — напрямую, поэтому cookie поможет только если
            # внешний IP у них общий.
            log.warning(
                "[%s] трекеры ходят через прокси, а FlareSolverr — напрямую: "
                "cf_clearance совпадёт только при общем внешнем IP",
                self.name,
            )

        self._solving = True
        try:
            cookies, user_agent = self._solver.solve(url)
        except SolverError as exc:
            log.warning("[%s] FlareSolverr не смог пройти челлендж: %s", self.name, exc)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] FlareSolverr: неожиданная ошибка: %s", self.name, exc)
            return False
        finally:
            self._solving = False

        self.apply_solver_session(cookies, user_agent)
        log.info("[%s] Cloudflare пройден, повторяю запрос", self.name)
        return True

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

    def search_url(self, query: str) -> str:
        """Ссылка на страницу поиска этого трекера (для открытия в браузере)."""
        raise NotImplementedError

    def is_logged_in(self) -> bool:
        """Эвристика: есть ли хотя бы одна сессионная cookie."""
        return bool(self._session.cookies)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
