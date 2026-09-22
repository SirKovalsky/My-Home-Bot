"""rutracker.org: логин, парсинг раздачи, скачивание .torrent ЧЕРЕЗ ПРОКСИ.

Все запросы идут через ``self._request`` из базового класса, который явно
подставляет ``proxies={'http': socks5h://..., 'https': socks5h://...}``.
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

# rutracker: уникальный id темы в URL (viewtopic.php?t=NNNNN).
TOPIC_ID_RE = re.compile(r"[?&]t=(\d+)", re.IGNORECASE)

# Прямая ссылка на скачивание .torrent на rutracker.
DOWNLOAD_LINK_RE = re.compile(r'href="(?:\./)?dl\.php\?t=(\d+)[^"]*"', re.IGNORECASE)
# Иногда встречается download.php.
DOWNLOAD_LINK_ALT_RE = re.compile(r'(?:dl|download)\.php\?t=(\d+)', re.IGNORECASE)

MAGNET_RE = re.compile(r"(magnet:\?[^\"'<\s]+)", re.IGNORECASE)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Ссылки на темы в результатах поиска (tracker.php).
SEARCH_LINK_RE = re.compile(
    r'<a[^>]*\bhref="[^"]*?viewtopic\.php\?[^"]*?\bt=(\d+)[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


class RuTrackerTracker(BaseTracker):
    name = "rutracker"
    base_url = "https://rutracker.org/forum/"

    # ------------------------------------------------------------------ #
    #  Авторизация
    # ------------------------------------------------------------------ #
    def login(self, login: str, password: str) -> bool:
        if not login or not password:
            raise NotLoggedInError(
                "Не заданы RUTRACKER_LOGIN / RUTRACKER_PASSWORD в .env"
            )

        url = f"{self.base_url}login.php"
        log.info("[rutracker] логин через прокси ...")
        # POST тоже идёт через прокси (см. _request -> kwargs['proxies']).
        response = self.post(
            url,
            data={
                "login_username": login,
                "login_password": password,
                "redirect": "index.php",
            },
            headers={"Referer": f"{self.base_url}index.php"},
        )

        if "bb_session" not in self._session.cookies.get_dict():
            # Логина нет — либо неверные данные, либо капча (её поймает _check_captcha).
            raise NotLoggedInError(
                "rutracker не выдал cookie bb_session — неверный логин/пароль или капча."
            )

        self.save_cookies()
        log.info("[rutracker] логин успешен")
        return True

    # ------------------------------------------------------------------ #
    #  Разбор страницы раздачи
    # ------------------------------------------------------------------ #
    def resolve(self, url: str) -> TorrentResult:
        topic_id = self._topic_id(url)
        if not topic_id:
            raise TrackerError(f"Не удалось вытащить id темы из ссылки: {url}")

        topic_url = f"{self.base_url}viewtopic.php?t={topic_id}"
        log.info("[rutracker] читаю тему %s через прокси", topic_id)
        html = self.fetch(topic_url).text

        if self._looks_logged_out(html):
            raise NotLoggedInError(
                "rutracker требует авторизацию. Выполните /login_rutracker."
            )

        name = self._extract_title(html) or f"rutracker_{topic_id}"

        # 1) Пытаемся найти magnet на странице.
        magnet_match = MAGNET_RE.search(html)
        if magnet_match:
            return TorrentResult(
                name=name,
                source=self.name,
                page_url=topic_url,
                magnet=magnet_match.group(1).replace("&amp;", "&"),
            )

        # 2) Пытаемся скачать .torrent по прямой ссылке (тоже через прокси!).
        torrent_id = self._extract_download_id(html) or topic_id
        raw = self._download_torrent(torrent_id)
        return TorrentResult(
            name=name,
            source=self.name,
            page_url=topic_url,
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

        log.info("[rutracker] поиск через прокси: %r", query)
        html = self.fetch(f"{self.base_url}tracker.php", params={"nm": query}).text

        if self._looks_logged_out(html):
            raise NotLoggedInError(
                "rutracker требует авторизацию. Выполните /login_rutracker."
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
                    url=f"{self.base_url}viewtopic.php?t={topic_id}",
                    topic_id=topic_id,
                )
            )
            if len(results) >= limit:
                break
        return results

    def _download_torrent(self, torrent_id: str) -> bytes:
        download_url = f"{self.base_url}dl.php?t={torrent_id}"
        log.info("[rutracker] скачиваю .torrent %s через прокси", torrent_id)
        response = self.fetch(download_url, headers={"Referer": self.base_url})

        raw = response.content
        if not raw or raw.lstrip()[:1] != b"d":
            raise TrackerError(
                "rutracker вернул не .torrent (возможно, страница логина/ошибки)."
            )
        return raw

    # ------------------------------------------------------------------ #
    #  Вспомогательное
    # ------------------------------------------------------------------ #
    @staticmethod
    def _topic_id(url: str) -> Optional[str]:
        match = TOPIC_ID_RE.search(url)
        return match.group(1) if match else None

    @staticmethod
    def _extract_download_id(html: str) -> Optional[str]:
        for pattern in (DOWNLOAD_LINK_RE, DOWNLOAD_LINK_ALT_RE):
            match = pattern.search(html)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_title(html: str) -> str:
        match = TITLE_RE.search(html)
        if not match:
            return ""
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        # rutracker: "<название> :: rutracker.org" / "... » ..."
        return title.split("::")[0].split("»")[0].strip()

    @staticmethod
    def _looks_logged_out(html: str) -> bool:
        lowered = html.lower()
        return "login.php" in lowered and "bb_session" not in lowered and "logout" not in lowered
