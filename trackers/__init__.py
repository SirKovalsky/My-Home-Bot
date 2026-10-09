"""Трекеры: авторизация и получение .torrent/magnet строго через прокси OpenWrt."""

from .base import (
    BaseTracker,
    CaptchaError,
    NotLoggedInError,
    ProxyUnavailableError,
    SearchResult,
    TorrentResult,
    TrackerError,
)
from .kinozal import KinozalTracker
from .rutracker import RuTrackerTracker
from .solver import FlareSolverr, SolverError

__all__ = [
    "BaseTracker",
    "TrackerError",
    "ProxyUnavailableError",
    "CaptchaError",
    "NotLoggedInError",
    "TorrentResult",
    "SearchResult",
    "RuTrackerTracker",
    "KinozalTracker",
    "FlareSolverr",
    "SolverError",
]
