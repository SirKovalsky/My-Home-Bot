"""Трекеры: авторизация и получение .torrent/magnet строго через прокси OpenWrt."""

from .base import (
    KIND_FILTER_LABELS,
    KIND_LABELS,
    BaseTracker,
    CaptchaError,
    NotLoggedInError,
    ProxyUnavailableError,
    SearchResult,
    TorrentResult,
    TrackerError,
    detect_kind,
    extract_release_year,
    parse_query_filters,
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
    "KIND_LABELS",
    "KIND_FILTER_LABELS",
    "parse_query_filters",
    "detect_kind",
    "extract_release_year",
    "RuTrackerTracker",
    "KinozalTracker",
    "FlareSolverr",
    "SolverError",
]
