"""Разбор торрентов, определение категории и предложение папки загрузки.

Здесь нет никакой сети — только работа с текстом и `.torrent`-байтами.

ВАЖНО про категории: функция :func:`detect_category` — это лишь *догадка*.
Бот показывает её пользователю как подсказку, но окончательный выбор папки
всегда подтверждает человек (см. ``bot.py``, inline-клавиатура).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import config

MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)

# --------------------------------------------------------------------------- #
#  Маркеры «сериальности»
# --------------------------------------------------------------------------- #
# S01, S01E02, s1e2
SEASON_EP_RE = re.compile(r"\b[sS]\d{1,2}(\s?[eE]\d{1,3})?\b")
# отдельный E01 / E12
EP_ONLY_RE = re.compile(r"\b[eE]\d{1,3}\b")
# диапазон серий 01-12 / 1-24 (годы 2020-2021 под шаблон не попадают)
EPISODE_RANGE_RE = re.compile(r"\b\d{1,2}\s?-\s?\d{1,3}\b")


def has_episode_markers(text: str) -> bool:
    """Есть ли в имени торрента признаки многосерийности."""
    haystack = (text or "").lower()
    if not haystack:
        return False
    if SEASON_EP_RE.search(haystack):
        return True
    if EP_ONLY_RE.search(haystack):
        return True
    if EPISODE_RANGE_RE.search(haystack):
        return True
    return _match_keywords(haystack, config.EPISODE_MARKERS)


# --------------------------------------------------------------------------- #
#  Категории
# --------------------------------------------------------------------------- #
def detect_category(text: str) -> str:
    """Определяет наиболее вероятную категорию по имени/описанию торрента.

    Приоритет: аудиокниги → музыка → софт → (аниме) → сериалы → фильмы → прочее.

    Логика для разных типов аниме (из требований):
      * аниме + признаки сериальности  -> Anime  (многосерийное аниме);
      * аниме без признаков сериальности -> Movies (полнометражное аниме);
      * сериалы (в т.ч. мультипликационные и 3D) -> Series.
    """
    haystack = (text or "").lower()
    if not haystack:
        return config.CATEGORY_OTHER

    if _match_keywords(haystack, config.AUDIOBOOKS_KEYWORDS):
        return config.CATEGORY_AUDIOBOOKS

    if _match_keywords(haystack, config.MUSIC_KEYWORDS):
        return config.CATEGORY_MUSIC

    if _match_keywords(haystack, config.SOFT_KEYWORDS):
        return config.CATEGORY_SOFT

    is_anime = _match_keywords(haystack, config.ANIME_KEYWORDS)
    episodic = has_episode_markers(haystack)

    if is_anime:
        # Многосерийное аниме -> Anime, полнометражное -> Movies.
        return config.CATEGORY_ANIME if episodic else config.CATEGORY_MOVIES

    if episodic or _match_keywords(haystack, config.SERIES_KEYWORDS):
        return config.CATEGORY_SERIES

    if _match_keywords(haystack, config.MOVIES_KEYWORDS):
        return config.CATEGORY_MOVIES

    return config.CATEGORY_OTHER


def _match_keywords(haystack: str, keywords: List[str]) -> bool:
    """Ищет ключевые слова.

    Для коротких ASCII-токенов (mp3, ost, flac, film...) требуем границы слова,
    чтобы «ost» не срабатывал внутри «most»/«poster». Для длинных и для
    кириллицы достаточно подстроки («кино» -> «кинофильм»).
    """
    for keyword in keywords:
        if not keyword:
            continue
        if keyword.isascii() and keyword.isalnum() and len(keyword) <= 5:
            if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", haystack):
                return True
            continue
        if keyword in haystack:
            return True
    return False


# --------------------------------------------------------------------------- #
#  Пути загрузки
# --------------------------------------------------------------------------- #
def category_label(category: str) -> str:
    return config.CATEGORY_LABELS.get(category, category)


def resolve_download_dir(category: str, override: Optional[str] = None) -> str:
    """Папка загрузки для категории (с возможностью явного переопределения)."""
    if override:
        return override
    return config.CONFIG.download_dir_for(category)


def category_choices() -> List[Tuple[str, str, str]]:
    """Список (category, label, path) для клавиатуры выбора."""
    return [
        (cat, category_label(cat), config.CONFIG.download_dir_for(cat))
        for cat in config.CATEGORY_ORDER
    ]


# --------------------------------------------------------------------------- #
#  Работа с .torrent (bencode)
# --------------------------------------------------------------------------- #
def _bdecode(data: bytes, index: int = 0):
    """Минимальный bencode-декодер.

    Возвращает (значение, новый_индекс). Поддерживает строки, int, list, dict —
    ровно то, что нужно для чтения метаданных .torrent.
    """
    if index >= len(data):
        raise ValueError("bencode: неожиданный конец данных")

    char = data[index : index + 1]

    if char == b"i":  # integer
        end = data.index(b"e", index)
        return int(data[index + 1 : end]), end + 1

    if char == b"l":  # list
        result = []
        index += 1
        while data[index : index + 1] != b"e":
            value, index = _bdecode(data, index)
            result.append(value)
        return result, index + 1

    if char == b"d":  # dict
        result = {}
        index += 1
        while data[index : index + 1] != b"e":
            key, index = _bdecode(data, index)
            value, index = _bdecode(data, index)
            result[key] = value
        return result, index + 1

    if char.isdigit():  # byte string
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        return data[start : start + length], start + length

    raise ValueError(f"bencode: неизвестный токен {char!r} на позиции {index}")


def parse_torrent_bytes(raw: bytes) -> Dict:
    """Парсит .torrent и возвращает метаданные (best effort, без падений)."""
    try:
        decoded, _ = _bdecode(raw)
    except Exception:
        return {}

    if not isinstance(decoded, dict):
        return {}

    info = decoded.get(b"info", {})
    return {
        "announce": _to_str(decoded.get(b"announce", b"")),
        "name": _to_str(info.get(b"name", b"")) if isinstance(info, dict) else "",
        "comment": _to_str(decoded.get(b"comment", b"")),
    }


def _to_str(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value else ""


def is_torrent_bytes(raw: bytes) -> bool:
    """Похоже ли содержимое на .torrent (bencode-dict)?"""
    return bool(raw) and raw.lstrip()[:1] == b"d" and b"info" in raw


def is_magnet(text: str) -> bool:
    return bool(MAGNET_RE.match(text.strip()))


def magnet_name(magnet: str) -> str:
    """Вытаскивает display name (dn=) из magnet-ссылки."""
    match = re.search(r"[?&]dn=([^&]+)", magnet or "")
    if not match:
        return ""
    from urllib.parse import unquote_plus

    return unquote_plus(match.group(1))


def guess_title(raw: bytes, fallback: str = "") -> str:
    """Достаёт человекочитаемое имя торрента из .torrent-файла."""
    meta = parse_torrent_bytes(raw)
    return meta.get("name") or fallback


# --------------------------------------------------------------------------- #
#  Форматирование
# --------------------------------------------------------------------------- #
def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def human_speed(bytes_per_sec: float) -> str:
    return f"{human_size(bytes_per_sec)}/s"
