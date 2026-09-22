"""Разбор торрентов и определение категории / папки загрузки.

Здесь нет никакой сети — только работа с текстом и `.torrent`-байтами.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import config

MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)

# --- Очень компактный bencode-парсер (достаточно для 'name'/'announce') --- #


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
    """Парсит .torrent и возвращает info-dict (best effort, без падений)."""
    try:
        decoded, _ = _bdecode(raw)
    except Exception:
        return {}

    info = decoded.get(b"info", {}) if isinstance(decoded, dict) else {}
    result = {
        "announce": _to_str(decoded.get(b"announce", b"")) if isinstance(decoded, dict) else "",
        "name": _to_str(info.get(b"name", b"")) if isinstance(info, dict) else "",
        "comment": _to_str(decoded.get(b"comment", b"")) if isinstance(decoded, dict) else "",
    }
    return result


def _to_str(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value else ""


def is_torrent_bytes(raw: bytes) -> bool:
    """Похоже ли содержимое на .torrent (bencode-dict)?"""
    return bool(raw) and raw.lstrip()[:1] == b"d" and b"info" in raw


def is_magnet(text: str) -> bool:
    return bool(MAGNET_RE.match(text.strip()))


# --------------------------------------------------------------------------- #
#  Категории
# --------------------------------------------------------------------------- #
def detect_category(text: str) -> str:
    """Определяет категорию по ключевым словам в имени/описании.

    Возвращает config.CATEGORY_SERIES / CATEGORY_FILMS / config.CATEGORY_OTHER.
    Сначала проверяются сериалы (более специфичные маркеры), потом фильмы.
    """
    haystack = (text or "").lower()

    if _match_keywords(haystack, config.SERIES_KEYWORDS):
        return config.CATEGORY_SERIES
    if _match_keywords(haystack, config.FILMS_KEYWORDS):
        return config.CATEGORY_FILMS
    return config.CATEGORY_OTHER


def _match_keywords(haystack: str, keywords: List[str]) -> bool:
    """Ищет ключевые слова как отдельные токены/подстроки.

    Границы слова для латиницы (s0 -> "s01e02") проверяются вручную: короткие
    маркеры вроде ``s0`` должны совпадать с ``s01``, а не внутри случайного слова.
    """
    for keyword in keywords:
        if not keyword:
            continue
        # Короткие технические маркеры (s0, s0e) матчим как префикс сезона.
        if re.fullmatch(r"s\d+e?", keyword):
            if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}", haystack):
                return True
            continue
        if keyword in haystack:
            return True
    return False


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


def resolve_download_dir(category: str, override: Optional[str] = None) -> str:
    """Папка загрузки для категории (с возможностью явного переопределения)."""
    if override:
        return override
    return config.CONFIG.download_dir_for(category)


def human_size(num_bytes: float) -> str:
    """Человекочитаемый размер."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def human_speed(bytes_per_sec: float) -> str:
    return f"{human_size(bytes_per_sec)}/s"


def split_categories(text: str) -> Tuple[str, str]:
    """Совместимость: возвращает (категория, папка)."""
    category = detect_category(text)
    return category, resolve_download_dir(category)
