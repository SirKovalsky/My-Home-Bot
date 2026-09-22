"""Обёртка над Transmission RPC — РАБОТАЕТ БЕЗ ПРОКСИ.

Архитектурное требование: P2P-трафик и обращения к локальному RPC не должны
проходить через прокси OpenWrt. Поэтому здесь:

    * сессия requests создаётся отдельно от сессий трекеров;
    * ``trust_env = False`` — игнорируем HTTP_PROXY/HTTPS_PROXY/NO_PROXY;
    * ``proxies = {}`` — явно пустой, «нет прокси»;
    * очищаются cookies, чтобы запросы были предсказуемыми.

Эти настройки применяются и к той сессии, которую создаёт сама библиотека
transmission-rpc, — на случай, если она создаёт её сама.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from transmission_rpc import Client
from transmission_rpc.error import TransmissionError

import config as config_module
from utils.torrent_parser import human_size, human_speed

log = logging.getLogger(__name__)


class TransmissionUnavailable(Exception):
    """Transmission RPC недоступен."""


@dataclass
class TorrentInfo:
    """Нормализованная информация о торренте."""

    id: int
    name: str
    status: str
    progress: float
    percent_done: float
    rate_download: float
    rate_upload: float
    total_size: int
    downloaded: int
    eta: Optional[int]
    download_dir: str

    @property
    def is_complete(self) -> bool:
        return self.percent_done >= 100.0


class TransmissionClient:
    """Тонкая синхронная обёртка. Вызывать из бота через asyncio.to_thread."""

    def __init__(self, cfg: config_module.Config | None = None) -> None:
        self._cfg = cfg or config_module.CONFIG
        self._client: Optional[Client] = None

        # >>> ЯВНО БЕЗ ПРОКСИ <<<
        # Собственная сессия requests: пустые proxies + отключённый trust_env.
        # Используется, если установленная версия transmission-rpc поддерживает
        # параметр session= ; в противном случае мы всё равно «починим» её
        # сессию в _harden_session().
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.proxies = {}
        self._session.cookies.clear()

    # ------------------------------------------------------------------ #
    #  Подключение
    # ------------------------------------------------------------------ #
    def _connect(self) -> Client:
        if self._client is not None:
            return self._client

        kwargs: Dict[str, Any] = dict(
            host=self._cfg.transmission_host,
            port=self._cfg.transmission_port,
            username=self._cfg.transmission_user or None,
            password=self._cfg.transmission_password or None,
            path=self._cfg.transmission_path,
            protocol=self._cfg.transmission_protocol,
            timeout=self._cfg.transmission_timeout,
        )

        try:
            try:
                client = Client(session=self._session, **kwargs)
            except TypeError:
                # Старые версии transmission-rpc не принимают session=.
                client = Client(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise TransmissionUnavailable(
                f"Не удалось создать клиент Transmission: {exc}"
            ) from exc

        self._harden_session(client)
        self._client = client
        return client

    def _harden_session(self, client: Client) -> None:
        """Гарантирует отсутствие прокси в HTTP-сессии клиента."""
        candidates = ("session", "_session", "http_session", "_http_session")
        for attr in candidates:
            session = getattr(client, attr, None)
            if isinstance(session, requests.Session):
                session.trust_env = False   # игнорировать *_PROXY из окружения
                session.proxies = {}        # прокси нет
                session.cookies.clear()
                log.debug("Transmission: %s переведена в режим direct (без прокси)", attr)

    # ------------------------------------------------------------------ #
    #  Операции
    # ------------------------------------------------------------------ #
    def add_torrent(
        self,
        *,
        torrent_bytes: Optional[bytes] = None,
        magnet: Optional[str] = None,
        download_dir: Optional[str] = None,
        paused: bool = False,
    ) -> TorrentInfo:
        """Добавляет .torrent (байты) или magnet. Возвращает инфо о торренте."""
        if not torrent_bytes and not magnet:
            raise ValueError("Нужен либо torrent_bytes, либо magnet")

        client = self._connect()
        target = torrent_bytes if torrent_bytes else magnet
        try:
            added = client.add_torrent(
                target,
                download_dir=download_dir,
                paused=paused,
            )
        except TransmissionError as exc:
            raise TransmissionUnavailable(f"Transmission отклонил торрент: {exc}") from exc

        log.info("Добавлен торрент: %s -> %s", added.name, download_dir or "default")
        return self._to_info(added)

    def get_status(self) -> List[TorrentInfo]:
        """Список активных загрузок."""
        client = self._connect()
        try:
            torrents = client.get_torrents()
        except TransmissionError as exc:
            raise TransmissionUnavailable(f"Не удалось получить статус: {exc}") from exc
        return [self._to_info(t) for t in torrents]

    def get_stats(self) -> Dict[str, float]:
        """Суммарная скорость и количество торрентов."""
        torrents = self.get_status()
        active = [t for t in torrents if t.status in ("downloading", "seeding")]
        return {
            "total": len(torrents),
            "downloading": sum(1 for t in torrents if t.status == "downloading"),
            "seeding": sum(1 for t in torrents if t.status == "seeding"),
            "paused": sum(1 for t in torrents if t.status.startswith("stopped")),
            "active": len(active),
            "download_speed": sum(t.rate_download for t in torrents),
            "upload_speed": sum(t.rate_upload for t in torrents),
            "downloaded_total": sum(t.downloaded for t in torrents),
        }

    def session_stats(self) -> Dict[str, Any]:
        """Статистика сессии Transmission (всего скачано/отдано и т.п.)."""
        client = self._connect()
        try:
            stats = client.session_stats()
        except TransmissionError as exc:
            raise TransmissionUnavailable(str(exc)) from exc
        return {
            "download_speed": getattr(stats, "download_speed", 0),
            "upload_speed": getattr(stats, "upload_speed", 0),
            "downloaded_bytes": getattr(stats, "downloaded_bytes", 0),
            "uploaded_bytes": getattr(stats, "uploaded_bytes", 0),
        }

    def stop_torrent(self, torrent_id: int) -> None:
        self._connect().stop_torrent(torrent_id)

    def start_torrent(self, torrent_id: int) -> None:
        self._connect().start_torrent(torrent_id)

    # ------------------------------------------------------------------ #
    #  Нормализация
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_info(torrent) -> TorrentInfo:
        status = getattr(torrent, "status", "unknown")
        if hasattr(status, "value"):  # Enum
            status = status.value
        eta = getattr(torrent, "eta", None)
        if eta is not None and getattr(eta, "total_seconds", None):
            try:
                eta = int(eta.total_seconds())
            except (TypeError, ValueError):
                eta = None

        # В Transmission RPC поле percentDone — доля 0..1, в некоторых версиях
        # библиотека уже отдаёт проценты. Нормализуем к 0..100.
        raw_percent = float(getattr(torrent, "percent_done", 0.0) or 0.0)
        if raw_percent <= 1.0:
            raw_percent *= 100.0

        return TorrentInfo(
            id=getattr(torrent, "id", 0),
            name=getattr(torrent, "name", ""),
            status=str(status),
            progress=float(getattr(torrent, "progress", 0.0) or 0.0),
            percent_done=raw_percent,
            rate_download=float(getattr(torrent, "rate_download", 0) or 0),
            rate_upload=float(getattr(torrent, "rate_upload", 0) or 0),
            total_size=int(getattr(torrent, "total_size", 0) or 0),
            downloaded=int(getattr(torrent, "downloaded_ever", 0) or 0),
            eta=eta,
            download_dir=getattr(torrent, "download_dir", "") or "",
        )

    # ------------------------------------------------------------------ #
    #  Форматирование для Telegram
    # ------------------------------------------------------------------ #
    @staticmethod
    def format_torrent(t: TorrentInfo) -> str:
        bar = _progress_bar(t.percent_done)
        eta = f"{t.eta // 60} мин" if t.eta else "—"
        return (
            f"• <b>{_escape(t.name)}</b>\n"
            f"  {bar} {t.percent_done:.1f}%  [{t.status}]\n"
            f"  ↓ {human_speed(t.rate_download)}  ↑ {human_speed(t.rate_upload)}  ETA {eta}"
        )


def _progress_bar(percent: float, width: int = 12) -> str:
    filled = int(round(width * max(0.0, min(percent, 100.0)) / 100.0))
    return "▰" * filled + "▱" * (width - filled)


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
