"""Трекеры: авторизация и получение .torrent/magnet строго через прокси OpenWrt."""

from .base import (
    BaseTracker,
    CaptchaError,
    NotLoggedInError,
    ProxyUnavailableError,
    TorrentResult,
    TrackerError,
)
from .kinozal import KinozalTracker
from .rutracker import RuTrackerTracker

__all__ = [
    "BaseTracker",
    "TrackerError",
    "ProxyUnavailableError",
    "CaptchaError",
    "NotLoggedInError",
    "TorrentResult",
    "RuTrackerTracker",
    "KinozalTracker",
]
