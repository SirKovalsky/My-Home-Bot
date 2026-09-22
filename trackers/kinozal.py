"""kinozal.me: логин, парсинг раздачи, скачивание .torrent ЧЕРЕЗ ПРОКСИ.

Как и rutracker, весь сетевой доступ идёт через ``self._request`` базового
класса с явно заданным ``proxies``. Исключений нет.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from .base import (
    BaseTracker,
    NotLoggedInError,
    SearchResult,
    TorrentResult,
    TrackerError,
    clean_html,
)

log = logging.getLogger(__name__)

# kinozal: id раздачи, например details.php?id=123456.
DETAILS_ID_RE = re.compile(r"[?&]id=(\d+)", re.IGNORECASE)
# Ссылка на скачивание .torrent (у kinozal бывает get_sid.php / download.php).
DOWNLOAD_LINK_RE = re.compile(
    r'href="([^"]*(?:get_sid|download|dload)\.php\?[^"]*?id=(\d+)[^"]*)"',
    re.IGNORECASE,
)
DOWNLOAD_ID_FALLBACK_RE = re.compile(
    r'(?:get_sid|download|dload)\.php\?[^"\'\s]*?id=(\d+)', re.IGNORECASE
)

MAGNET_RE = re.compile(r"(magnet:\?[^\"'<\s]+)", re.IGNORECASE)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Ссылки на раздачи в результатах поиска.
SEARCH_LINK_RE = re.compile(
    r'<a[^>]*\bhref="[^"]*?details\.php\?id=(\d+)[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


class KinozalTracker(BaseTracker):
    name = "kinozal"
    base_url = "https://kinozal.me/"

    # ------------------------------------------------------------------ #
    #  Авторизация
    # ------------------------------------------------------------------ #
    def login(self, login: str, password: str) -> bool:
        if not login or not password:
            raise NotLoggedInError(
                "Не заданы KINOZAL_LOGIN / KINOZAL_PASSWORD в .env"
            )

        url = f"{self.base_url}takelogin.php"
        log.info("[kinozal] логин через прокси ...")
        response = self.post(
            url,
            data={"username": login, "password": password, "submit": "Вход"},
            headers={"Referer": f"{self.base_url}login.php"},
        )

        cookies = self._session.cookies.get_dict()
        if "uid" not in cookies and "pass" not in cookies:
            raise NotLoggedInError(
                "kinozal не выдал сессионные cookies (uid/pass) — "
                "неверные данные или капча."
            )

        self.save_cookies()
        log.info("[kinozal] логин успешен")
        return True

    # ------------------------------------------------------------------ #
    #  Разбор страницы раздачи
    # ------------------------------------------------------------------ #
    def resolve(self, url: str) -> TorrentResult:
        topic_id = self._topic_id(url)
        if not topic_id:
            raise TrackerError(f"Не удалось вытащить id раздачи из ссылки: {url}")

        page_url = f"{self.base_url}details.php?id={topic_id}"
        log.info("[kinozal] читаю раздачу %s через прокси", topic_id)
        html = self.fetch(page_url).text

        if self._looks_logged_out(html):
            raise NotLoggedInError(
                "kinozal требует авторизацию. Выполните /login_kinozal."
            )

        name = self._extract_title(html) or f"kinozal_{topic_id}"

        # 1) magnet, если есть.
        magnet_match = MAGNET_RE.search(html)
        if magnet_match:
            return TorrentResult(
                name=name,
                source=self.name,
                page_url=page_url,
                magnet=magnet_match.group(1).replace("&amp;", "&"),
            )

        # 2) Прямая ссылка на .torrent (через прокси).
        download_url = self._extract_download_url(html, topic_id)
        raw = self._download_torrent(download_url)
        return TorrentResult(
            name=name,
            source=self.name,
            page_url=page_url,
            torrent_bytes=raw,
            extra={"topic_id": topic_id},
        )

    # ------------------------------------------------------------------ #
    #  Поиск
    # ------------------------------------------------------------------ #
    def search(self, query: str, limit: int = 10) -> List[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []

        log.info("[kinozal] поиск через прокси: %r", query)
        html = self.fetch(f"{self.base_url}browse.php", params={"s": query}).text

        if self._looks_logged_out(html):
            raise NotLoggedInError(
                "kinozal требует авторизацию. Выполните /login_kinozal."
            )

        results: List[SearchResult] = []
        seen = set()
        for match in SEARCH_LINK_RE.finditer(html):
            topic_id = match.group(1)
            if topic_id in seen:
                continue
            title = clean_html(match.group(2))
            if not title:
                continue
            seen.add(topic_id)
            results.append(
                SearchResult(
                    tracker=self.name,
                    title=title,
                    url=f"{self.base_url}details.php?id={topic_id}",
                    topic_id=topic_id,
                )
            )
            if len(results) >= limit:
                break
        return results

    def _download_torrent(self, download_url: str) -> bytes:
        log.info("[kinozal] скачиваю .torrent через прокси: %s", download_url)
        response = self.fetch(download_url, headers={"Referer": self.base_url})
        raw = response.content

        if not raw or raw.lstrip()[:1] != b"d":
            raise TrackerError(
                "kinozal вернул не .torrent (возможно, требуется логин или кончились "
                "раздачи/лимит скачиваний)."
            )
        return raw

    # ------------------------------------------------------------------ #
    #  Вспомогательное
    # ------------------------------------------------------------------ #
    @staticmethod
    def _topic_id(url: str) -> Optional[str]:
        match = DETAILS_ID_RE.search(url)
        return match.group(1) if match else None

    def _extract_download_url(self, html: str, topic_id: str) -> str:
        match = DOWNLOAD_LINK_RE.search(html)
        if match:
            href = match.group(1).replace("&amp;", "&")
            return self._absolute(href)

        fallback = DOWNLOAD_ID_FALLBACK_RE.search(html)
        if fallback:
            return f"{self.base_url}get_sid.php?id={fallback.group(1)}"

        # Последний шанс — стандартный эндпоинт kinozal.
        return f"{self.base_url}get_sid.php?id={topic_id}"

    def _absolute(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return f"{self.base_url}{href.lstrip('/')}"

    @staticmethod
    def _extract_title(html: str) -> str:
        match = TITLE_RE.search(html)
        if not match:
            return ""
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        return title.split("::")[0].split("»")[0].split("(")[0].strip()

    @staticmethod
    def _looks_logged_out(html: str) -> bool:
        lowered = html.lower()
        return "takelogin.php" in lowered and "logout" not in lowered
