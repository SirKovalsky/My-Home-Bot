"""Решение Cloudflare-челленджа («Just a moment...») через FlareSolverr.

Зачем это нужно
---------------
curl_cffi подделывает TLS/HTTP-отпечаток браузера, но НЕ исполняет
JavaScript. Cloudflare это распознаёт и отдаёт челлендж-страницу, поэтому
логин, поиск и скачивание .torrent с rutracker/kinozal упираются в 403.

Рядом с ботом (на том же хосте и, главное, через тот же внешний IP) крутится
FlareSolverr — настоящий Chromium, который челлендж проходит и возвращает
готовые cookies (в первую очередь ``cf_clearance``) и свой User-Agent.

Архитектурное правило:
    * FlareSolverr опрашивается по ЛОКАЛЬНОМУ адресу и БЕЗ прокси
      (``trust_env=False``, ``proxies={}``) — ровно как клиент Transmission;
    * полученные cookies и UA подставляются в сессию трекера, а она уже
      ходит к трекеру через прокси OpenWrt;
    * ``cf_clearance`` привязан к паре «внешний IP + User-Agent», поэтому UA
      из ответа FlareSolverr обязан стать UA сессии трекера, иначе cookie
      мгновенно перестанет работать.

Если FlareSolverr не поднят — поведение прежнее: трекер просто отдаст
``CaptchaError``, а бот предложит открыть раздачу в браузере телефона.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

import requests

log = logging.getLogger("torrent-bot")


class SolverError(Exception):
    """FlareSolverr недоступен или не смог решить челлендж."""


# Значения, которыми можно выключить решатель в .env
_OFF = ("", "none", "off", "no", "disabled", "0")


class FlareSolverr:
    """Клиент локального FlareSolverr (HTTP API ``/v1``)."""

    def __init__(self, url: str, timeout: int = 180) -> None:
        self._url = (url or "").strip()
        self._timeout = max(30, int(timeout))
        # Локальный вызов: никакого прокси и никакого окружения.
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.proxies = {}

    @property
    def url(self) -> str:
        return self._url

    @property
    def enabled(self) -> bool:
        return self._url.strip().lower() not in _OFF

    def solve(self, url: str) -> Tuple[Dict[str, str], str]:
        """Пройти челлендж на ``url`` и вернуть ``(cookies, user_agent)``.

        Бросает :class:`SolverError`, если решатель недоступен или не отдал
        cookies. Сам по себе факт ``status: ok`` без cookie смысла не имеет —
        так бывает, когда страница челленджем не защищена (например,
        ``index.php``), поэтому пустой набор cookie считаем ошибкой.
        """
        if not self.enabled:
            raise SolverError("FlareSolverr не настроен (FLARESOLVERR_URL пуст)")

        payload = {"cmd": "request.get", "url": url, "maxTimeout": 120000}
        try:
            response = self._session.post(
                self._url, json=payload, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise SolverError(f"FlareSolverr недоступен ({self._url}): {exc}") from exc

        if response.status_code != 200:
            raise SolverError(
                f"FlareSolverr вернул HTTP {response.status_code} "
                f"(ожидался 200)"
            )

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise SolverError(f"FlareSolverr вернул не JSON: {exc}") from exc

        status = data.get("status")
        if status != "ok":
            message = str(data.get("message") or "")[:200]
            raise SolverError(f"FlareSolverr: {status or 'без статуса'} — {message}")

        solution = data.get("solution") or {}
        cookies = {
            str(item.get("name")): str(item.get("value") or "")
            for item in (solution.get("cookies") or [])
            if item.get("name")
        }
        user_agent = str(solution.get("userAgent") or "").strip()

        if not cookies:
            raise SolverError(
                "FlareSolverr ответил ok, но cookies не вернул "
                "(возможно, страница не защищена челленджем)"
            )

        log.info(
            "Cloudflare пройден: cookies=%s, cf_clearance=%d символов, UA=%s",
            ", ".join(sorted(cookies)) or "-",
            len(cookies.get("cf_clearance", "")),
            user_agent[:60] or "-",
        )
        return cookies, user_agent

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
