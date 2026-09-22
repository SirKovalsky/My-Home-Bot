"""Точка входа Telegram-бота автозакачки торрентов в Transmission.

Архитектура (см. README.md):
    Telegram  ->  бот на Xpenology  ->  [прокси OpenWrt]  ->  rutracker/kinozal
                                     ->  [localhost, БЕЗ прокси] ->  Transmission

    Прокси используется ИСКЛЮЧИТЕЛЬНО внутри ``trackers/*`` (явный параметр
    ``proxies=``). Клиент Transmission прокси не использует никогда.

Выбор папки: бот определяет категорию автоматически, но НЕ применяет её молча —
он предлагает вариант кнопкой и ждёт подтверждения пользователя.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import secrets
import socket
import sys
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, List, Optional, Set, Tuple, cast
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiohttp import ClientError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config as config_module
from config import CONFIG
from trackers import (
    BaseTracker,
    CaptchaError,
    KinozalTracker,
    NotLoggedInError,
    ProxyUnavailableError,
    RuTrackerTracker,
    SearchResult,
    TrackerError,
)
from transmission_client import TransmissionClient, TransmissionUnavailable
from utils import torrent_parser

# asyncio.to_thread() exists only since Python 3.9, while the Synology DSM 6.2
# Python package is 3.8. Emulate it with the default thread pool.
if hasattr(asyncio, "to_thread"):
    to_thread = asyncio.to_thread
else:  # Python 3.8 fallback
    async def to_thread(func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


class ProxiedAiohttpSession(AiohttpSession):
    """AiohttpSession, отправляющая запросы через HTTP-прокси.

    Зачем свой класс: aiogram-овский ``AiohttpSession(proxy=...)`` опирается на
    пакет ``aiohttp-socks`` и без него не работает вовсе. А aiohttp прекрасно
    умеет HTTP-прокси сам (параметр ``proxy=`` у запроса), поэтому лишняя
    зависимость на Python 3.8 нам не нужна.

    Подходит именно HTTP-прокси. Для SOCKS нужен aiohttp-socks.
    """

    def __init__(self, proxy: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._proxy_url = proxy

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: Optional[int] = None,
    ) -> TelegramType:
        session = await self.create_session()
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        form = self.build_form_data(bot=bot, method=method)

        try:
            async with session.post(
                url,
                data=form,
                timeout=self.timeout if timeout is None else timeout,
                proxy=self._proxy_url,
            ) as resp:
                raw_result = await resp.text()
        except asyncio.TimeoutError:
            raise TelegramNetworkError(method=method, message="Request timeout error")
        except ClientError as exc:
            raise TelegramNetworkError(
                method=method, message=f"{type(exc).__name__}: {exc}"
            )

        response = self.check_response(
            bot=bot, method=method, status_code=resp.status, content=raw_result
        )
        return cast(TelegramType, response.result)

    async def stream_content(
        self,
        url: str,
        headers: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ):
        session = await self.create_session()
        async with session.get(
            url,
            timeout=timeout,
            headers=headers or {},
            raise_for_status=raise_for_status,
            proxy=self._proxy_url,
        ) as resp:
            async for chunk in resp.content.iter_chunked(chunk_size):
                yield chunk


log = logging.getLogger("torrent-bot")

router = Router()

# Клиенты создаются один раз.
transmission = TransmissionClient(CONFIG)

trackers: Dict[str, BaseTracker] = {
    "rutracker": RuTrackerTracker(
        proxies=CONFIG.proxies,
        cookies_path=CONFIG.cookies_path,
        user_agent=CONFIG.user_agent,
        timeout=CONFIG.proxy_timeout,
    ),
    "kinozal": KinozalTracker(
        proxies=CONFIG.proxies,
        cookies_path=CONFIG.cookies_path,
        user_agent=CONFIG.user_agent,
        timeout=CONFIG.proxy_timeout,
    ),
}

# Чаты для уведомлений о завершении загрузки.
known_chats: Set[int] = set()


# --------------------------------------------------------------------------- #
#  Ожидающие подтверждения торренты
# --------------------------------------------------------------------------- #
@dataclass
class PendingTorrent:
    """Торрент, который ждёт, пока пользователь выберет папку."""

    token: str
    user_id: int
    name: str
    suggested: str
    magnet: Optional[str] = None
    torrent_bytes: Optional[bytes] = None
    source: str = ""
    created: float = 0.0


PENDING: Dict[str, PendingTorrent] = {}


@dataclass
class SearchSession:
    """Результаты поиска, ожидающие выбора пользователя."""

    token: str
    user_id: int
    query: str
    results: List[SearchResult]
    created: float


SEARCHES: Dict[str, SearchSession] = {}


def _prune_pending() -> None:
    ttl = CONFIG.confirm_ttl
    now = time.time()
    expired = [tok for tok, item in PENDING.items() if now - item.created > ttl]
    for tok in expired:
        PENDING.pop(tok, None)
    expired = [tok for tok, item in SEARCHES.items() if now - item.created > ttl]
    for tok in expired:
        SEARCHES.pop(tok, None)


# --------------------------------------------------------------------------- #
#  Логирование: файл + stdout
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    level = getattr(logging, CONFIG.log_level, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(CONFIG.log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
#  Доступ
# --------------------------------------------------------------------------- #
def _user_allowed(user_id: Optional[int]) -> bool:
    allowed: List[int] = CONFIG.allowed_user_ids
    if not allowed:
        return True
    return user_id is not None and user_id in allowed


def is_allowed(message: Message) -> bool:
    return _user_allowed(message.from_user.id if message.from_user else None)


async def deny(message: Message) -> None:
    await message.answer("⛔ У вас нет доступа к этому боту.")


def track_chat(message: Message) -> None:
    if message.chat:
        known_chats.add(message.chat.id)


class SearchFlow(StatesGroup):
    """Состояние «жду текст запроса после нажатия 🔎 Поиск»."""

    waiting_query = State()


class CookieFlow(StatesGroup):
    """Импорт cookies/User-Agent из браузера (обход Cloudflare)."""

    waiting_cookies = State()
    waiting_ua = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="menu|status"),
                InlineKeyboardButton(text="📈 Статистика", callback_data="menu|stats"),
            ],
            [
                InlineKeyboardButton(text="📁 Папки", callback_data="menu|dirs"),
                InlineKeyboardButton(text="🔎 Поиск", callback_data="menu|search"),
            ],
        ]
    )


# --------------------------------------------------------------------------- #
#  Команды
# --------------------------------------------------------------------------- #
@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    await message.answer(
        "🤖 <b>Torrent Bot</b>\n\n"
        "Отправьте мне:\n"
        "• magnet-ссылку — сразу предложу папку;\n"
        "• <code>.torrent</code> файл — сразу предложу папку;\n"
        "• ссылку на раздачу rutracker.org / kinozal.me — скачаю .torrent "
        "через прокси.\n\n"
        "<b>Папку загрузки подтверждаете вы</b> — бот только предлагает вариант.\n\n"
        "<b>Команды:</b>\n"
        "/search запрос — поиск раздачи на трекерах\n"
        "/login_rutracker — обновить сессию rutracker\n"
        "/login_kinozal — обновить сессию kinozal\n"
        "/cookies rutracker|kinozal — импорт cookies из браузера (Cloudflare)\n"
        "/status — активные загрузки\n"
        "/stats — суммарная статистика\n"
        "/dirs — показать список папок\n"
        "/help — эта справка\n\n"
        "<i>Если /search ругается на Cloudflare — это ограничение серверного IP. "
        "Тогда просто пришлите .torrent или magnet из браузера телефона, "
        "остальное бот сделает сам.</i>\n\n"
        f"<i>Прокси для трекеров:</i> <code>{CONFIG.proxy_url}</code>\n"
        f"<i>Transmission:</i> <code>{CONFIG.transmission_host}:"
        f"{CONFIG.transmission_port}</code> (без прокси)",
        reply_markup=main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


def _dirs_text() -> str:
    lines = ["📂 <b>Папки загрузки</b>", ""]
    for _cat, label, path in torrent_parser.category_choices():
        lines.append(f"{label}: <code>{path}</code>")
    return "\n".join(lines)


@router.message(Command("dirs"))
async def cmd_dirs(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    await message.answer(_dirs_text())


@router.message(Command("login_rutracker"))
async def cmd_login_rutracker(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    await message.answer("🔐 Логинюсь в rutracker через прокси...")
    await _do_login(message, "rutracker")


@router.message(Command("login_kinozal"))
async def cmd_login_kinozal(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    await message.answer("🔐 Логинюсь в kinozal через прокси...")
    await _do_login(message, "kinozal")


async def _do_login(message: Message, tracker_name: str) -> None:
    tracker = trackers[tracker_name]
    if tracker_name == "rutracker":
        login, password = CONFIG.rutracker_login, CONFIG.rutracker_password
    else:
        login, password = CONFIG.kinozal_login, CONFIG.kinozal_password

    try:
        # Логин — блокирующий вызов с сетью через прокси: уводим в поток.
        await to_thread(tracker.login, login, password)
    except NotLoggedInError as exc:
        await message.answer(f"❌ Не удалось войти: {exc}")
    except CaptchaError as exc:
        await message.answer(f"🤖 Трекер отдал капчу: {exc}")
    except ProxyUnavailableError as exc:
        await message.answer(f"🔌 Прокси недоступен: {exc}")
    except TrackerError as exc:
        await message.answer(f"❌ Ошибка трекера: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Необработанная ошибка логина")
        await message.answer(f"💥 Внутренняя ошибка: {exc}")
    else:
        await message.answer(f"✅ Сессия {tracker_name} сохранена.")


async def _status_text() -> str:
    torrents = await to_thread(transmission.get_status)
    if not torrents:
        return "📭 Список загрузок пуст."
    active = [t for t in torrents if t.status in ("downloading", "seeding")]
    header = f"📊 <b>Загрузки</b> ({len(torrents)} всего, {len(active)} активных)\n\n"
    body = "\n".join(TransmissionClient.format_torrent(t) for t in torrents[:20])
    note = "\n\n<i>Показаны первые 20.</i>" if len(torrents) > 20 else ""
    return header + body + note


async def _stats_text() -> str:
    stats = await to_thread(transmission.get_stats)
    session = await to_thread(transmission.session_stats)
    return (
        "📈 <b>Статистика Transmission</b>\n\n"
        f"Торрентов всего: <b>{stats['total']}</b>\n"
        f"• качается: {stats['downloading']}\n"
        f"• раздаётся: {stats['seeding']}\n"
        f"• на паузе: {stats['paused']}\n\n"
        f"Текущая скорость ↓: <b>{torrent_parser.human_speed(stats['download_speed'])}</b>\n"
        f"Текущая скорость ↑: <b>{torrent_parser.human_speed(stats['upload_speed'])}</b>\n\n"
        f"За сессию скачано: {torrent_parser.human_size(session['downloaded_bytes'])}\n"
        f"За сессию отдано: {torrent_parser.human_size(session['uploaded_bytes'])}"
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    try:
        text = await _status_text()
    except TransmissionUnavailable as exc:
        text = f"⚠️ Transmission недоступен: {exc}"
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    try:
        text = await _stats_text()
    except TransmissionUnavailable as exc:
        text = f"⚠️ Transmission недоступен: {exc}"
    await message.answer(text, reply_markup=main_menu())


# --------------------------------------------------------------------------- #
#  Поиск по трекерам
# --------------------------------------------------------------------------- #
async def _search_all(query: str, per_tracker: int = 6) -> Tuple[List[SearchResult], List[str]]:
    results: List[SearchResult] = []
    errors: List[str] = []
    for name in ("rutracker", "kinozal"):
        tracker = trackers[name]
        try:
            results.extend(await to_thread(tracker.search, query, per_tracker))
        except NotLoggedInError as exc:
            errors.append(f"🔑 {name}: {exc}")
        except CaptchaError as exc:
            errors.append(f"🤖 {name}: {exc}")
        except ProxyUnavailableError as exc:
            errors.append(f"🔌 {name}: {exc}")
        except TrackerError as exc:
            errors.append(f"❌ {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка поиска на %s", name)
            errors.append(f"💥 {name}: {exc}")
    return results, errors


def _search_keyboard(token: str, results: List[SearchResult]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for idx, item in enumerate(results):
        seeds = f"{item.seeds}↑ " if item.seeds else ""
        label = f"{idx + 1}. [{item.tracker}] {seeds}{item.title}"
        if len(label) > 60:
            label = label[:59] + "…"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"pick|{token}|{idx}")]
        )
    rows.append(
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancelsearch|{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _run_search(message: Message, query: str) -> None:
    status = await message.answer(f"🔎 Ищу «{_escape(query)}» через прокси...")
    results, errors = await _search_all(query)

    if not results:
        cloudflare = any("just a moment" in err.lower() for err in errors)
        text = CLOUDFLARE_HINT if cloudflare else "🤷 Ничего не нашлось."
        if errors:
            text += "\n\n" + "\n".join(errors)
        return await status.edit_text(
            text, reply_markup=_tracker_search_keyboard(query)
        )

    _prune_pending()
    token = secrets.token_urlsafe(6)
    SEARCHES[token] = SearchSession(
        token=token,
        user_id=message.from_user.id if message.from_user else 0,
        query=query,
        results=results,
        created=time.time(),
    )
    await status.edit_text(
        f"🔎 По запросу «{_escape(query)}» найдено {len(results)} "
        "(отсортировано по раздающим). Выберите раздачу:",
        reply_markup=_search_keyboard(token, results),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if not is_allowed(message):
        return await deny(message)
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


@router.message(Command("search"))
async def cmd_search(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)

    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        return await message.answer(
            "🔎 <b>Поиск по трекерам</b>\n\n"
            "Использование: <code>/search название</code>\n"
            "Например: <code>/search Интерстеллар</code>\n\n"
            "Или нажмите кнопку «🔎 Поиск» в меню и просто пришлите название.\n"
            "Ищет на rutracker и kinozal через прокси, сортирует по сидам."
        )
    await _run_search(message, query)


@router.message(StateFilter(SearchFlow.waiting_query))
async def on_search_query(message: Message, state: FSMContext) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    await state.clear()

    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return await message.answer("Поиск отменён.", reply_markup=main_menu())
    await _run_search(message, query)


# --------------------------------------------------------------------------- #
#  Импорт cookies из браузера (Cloudflare)
# --------------------------------------------------------------------------- #
COOKIE_HELP = (
    "Использование: <code>/cookies rutracker</code> или "
    "<code>/cookies kinozal</code>\n\n"
    "Зачем: трекеры закрыты Cloudflare-челленджем, который в состоянии решить "
    "только браузер. Один раз проходишь проверку в браузере (через тот же "
    "прокси, что у бота) и присылаешь сюда cookies, включая "
    "<code>cf_clearance</code>, плюс User-Agent того же браузера."
)


@router.message(Command("cookies"))
async def cmd_cookies(message: Message, state: FSMContext) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)

    arg = (message.text or "").partition(" ")[2].strip().lower()
    if arg not in trackers:
        return await message.answer(f"🍪 {COOKIE_HELP}")

    await state.set_state(CookieFlow.waiting_cookies)
    await state.update_data(tracker=arg)
    await message.answer(
        f"🍪 Пришли строку cookies для <b>{arg}</b> в формате "
        "<code>name=value; name2=value2</code>.\n\n"
        "Где взять: браузер → F12 → <b>Application</b> → Cookies → выбрать домен "
        f"<code>{tracker_host(arg)}</code> → скопировать значения "
        "(обязательно нужен <code>cf_clearance</code>).\n\n"
        "Отмена — /cancel"
    )


def tracker_host(name: str) -> str:
    try:
        return urlparse(trackers[name].base_url).hostname or name
    except Exception:  # noqa: BLE001
        return name


@router.message(StateFilter(CookieFlow.waiting_cookies))
async def on_cookies_input(message: Message, state: FSMContext) -> None:
    if not is_allowed(message):
        return await deny(message)
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        await state.clear()
        return await message.answer("Отменено.", reply_markup=main_menu())

    data = await state.get_data()
    name = data.get("tracker", "rutracker")
    tracker = trackers.get(name)
    if tracker is None:
        await state.clear()
        return await message.answer("Неизвестный трекер.")

    try:
        count = await to_thread(tracker.import_cookies, raw)
    except Exception as exc:  # noqa: BLE001
        return await message.answer(f"❌ Не разобрал cookies: {exc}")

    await state.update_data(cookies=raw)
    await state.set_state(CookieFlow.waiting_ua)
    await message.answer(
        f"✅ Принял cookies: <b>{count}</b>.\n\n"
        "Теперь пришли <b>User-Agent</b> того же браузера "
        "(F12 → Network → любой запрос → Request Headers → User-Agent).\n"
        "Это критично: <code>cf_clearance</code> привязан к паре IP + User-Agent.\n\n"
        "Если не знаешь — пришли <code>-</code>, оставим текущий."
    )


@router.message(StateFilter(CookieFlow.waiting_ua))
async def on_ua_input(message: Message, state: FSMContext) -> None:
    if not is_allowed(message):
        return await deny(message)

    text = (message.text or "").strip()
    data = await state.get_data()
    name = data.get("tracker", "rutracker")
    tracker = trackers.get(name)
    await state.clear()
    if tracker is None:
        return await message.answer("Неизвестный трекер.")

    if text and text not in ("-", "none", "нет"):
        await to_thread(tracker.set_user_agent, text)
    await to_thread(tracker.save_cookies)

    status = await message.answer("🔎 Проверяю сессию через прокси...")
    try:
        result = await to_thread(tracker.verify_session)
    except Exception as exc:  # noqa: BLE001
        return await status.edit_text(
            f"⚠️ Cookies сохранены, но проверка не прошла:\n{exc}\n\n"
            "Скорее всего User-Agent не совпал или cookies уже истекли."
        )
    await status.edit_text(
        f"✅ Cookies сохранены, проверка пройдена: {result}\n"
        "Пробуйте /search."
    )


@router.message(Command("searchraw"))
async def cmd_searchraw(message: Message) -> None:
    """Диагностика: сохраняет HTML страницы поиска, чтобы подстроить парсер."""
    if not is_allowed(message):
        return await deny(message)
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        return await message.answer("Использование: <code>/searchraw запрос</code>")

    status = await message.answer("🧪 Забираю сырой HTML страниц поиска...")
    lines: List[str] = []
    for name, tracker in trackers.items():
        try:
            html = await to_thread(tracker.search_html, query)
            path = config_module.BASE_DIR / f"debug-search-{name}.html"
            path.write_text(html, encoding="utf-8", errors="replace")
            lines.append(f"{name}: {len(html)} байт → <code>{path.name}</code>")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{name}: {type(exc).__name__}: {exc}")
    await status.edit_text("🧪 Готово:\n" + "\n".join(lines))


@router.callback_query(F.data.startswith("pick|"))
async def on_pick_result(callback: CallbackQuery) -> None:
    if not _user_allowed(callback.from_user.id):
        return await callback.answer("⛔ Нет доступа", show_alert=True)

    try:
        _, token, idx_raw = (callback.data or "").split("|", 2)
        idx = int(idx_raw)
    except ValueError:
        return await callback.answer("Некорректные данные", show_alert=True)

    session = SEARCHES.get(token)
    if not session:
        return await callback.answer("⌛️ Поиск устарел, повторите /search.", show_alert=True)
    if session.user_id != callback.from_user.id:
        return await callback.answer("Это не ваш поиск.", show_alert=True)
    if not 0 <= idx < len(session.results):
        return await callback.answer("Некорректный выбор", show_alert=True)

    result = session.results[idx]
    SEARCHES.pop(token, None)
    await callback.answer("Открываю раздачу...")

    tracker = trackers[result.tracker]
    try:
        resolved = await to_thread(tracker.resolve, result.url)
    except NotLoggedInError as exc:
        return await _edit(callback, f"🔑 {exc}")
    except CaptchaError as exc:
        if _is_cloudflare(exc):
            return await _edit(
                callback,
                CLOUDFLARE_HINT,
                reply_markup=_open_in_browser_keyboard(result.url, "🌐 Открыть раздачу"),
            )
        return await _edit(callback, f"🤖 {exc}")
    except ProxyUnavailableError as exc:
        return await _edit(callback, f"🔌 Прокси недоступен: {exc}")
    except TrackerError as exc:
        return await _edit(callback, f"❌ Ошибка трекера: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка разбора раздачи из поиска")
        return await _edit(callback, f"💥 Внутренняя ошибка: {exc}")

    if callback.message:
        await propose_folder(
            callback.message,
            name=resolved.name or result.title,
            magnet=resolved.magnet,
            torrent_bytes=resolved.torrent_bytes,
            source=f"{result.tracker}: {result.url}",
            user_id=callback.from_user.id,
        )


@router.callback_query(F.data.startswith("cancelsearch|"))
async def on_cancel_search(callback: CallbackQuery) -> None:
    if not _user_allowed(callback.from_user.id):
        return await callback.answer("⛔ Нет доступа", show_alert=True)
    parts = (callback.data or "").split("|", 1)
    SEARCHES.pop(parts[1] if len(parts) > 1 else "", None)
    await callback.answer("Отменено.")
    await _edit(callback, "🚫 Поиск отменён.")


@router.callback_query(F.data.startswith("menu|"))
async def on_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not _user_allowed(callback.from_user.id):
        return await callback.answer("⛔ Нет доступа", show_alert=True)

    _, _, action = (callback.data or "menu|").partition("|")
    await callback.answer()

    if action == "search":
        await state.set_state(SearchFlow.waiting_query)
        if callback.message:
            await callback.message.answer(
                "🔎 Что искать? Пришлите название следующим сообщением.\n"
                "Отмена — /cancel"
            )
        return

    try:
        if action == "status":
            text = await _status_text()
        elif action == "stats":
            text = await _stats_text()
        elif action == "dirs":
            text = _dirs_text()
        else:
            text = "Неизвестное действие."
    except TransmissionUnavailable as exc:
        text = f"⚠️ Transmission недоступен: {exc}"

    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=main_menu())
    except Exception:  # noqa: BLE001
        await callback.message.answer(text, reply_markup=main_menu())


# --------------------------------------------------------------------------- #
#  Приём magnet / ссылок / файлов
# --------------------------------------------------------------------------- #
@router.message(F.text)
async def on_text(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)

    text = (message.text or "").strip()
    if not text:
        return

    if torrent_parser.is_magnet(text):
        await propose_folder(
            message, name=torrent_parser.magnet_name(text), magnet=text
        )
        return

    tracker_name = detect_tracker(text)
    if tracker_name:
        await process_tracker_url(message, tracker_name, text)
        return

    if text.startswith("/"):
        return  # неизвестная команда — молчим
    await message.answer(
        "🤔 Не понял. Пришлите magnet, .torrent файл или ссылку на "
        "rutracker.org / kinozal.me."
    )


@router.message(F.document)
async def on_document(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)

    document = message.document
    filename = document.file_name or ""
    if not filename.lower().endswith(".torrent"):
        return await message.answer("🤔 Это не .torrent файл.")

    status = await message.answer("📥 Скачиваю файл из Telegram...")
    try:
        buffer: BytesIO = await message.bot.download(document)
        raw = buffer.read()
    except Exception as exc:  # noqa: BLE001
        log.exception("Не удалось скачать документ")
        return await status.edit_text(f"❌ Не удалось получить файл: {exc}")

    name = torrent_parser.guess_title(raw, fallback=filename)
    await propose_folder(message, name=name, torrent_bytes=raw, status_message=status)


def detect_tracker(url: str) -> Optional[str]:
    lowered = url.lower()
    if "rutracker.org" in lowered:
        return "rutracker"
    if "kinozal" in lowered:
        return "kinozal"
    return None


async def process_tracker_url(message: Message, tracker_name: str, url: str) -> None:
    tracker = trackers[tracker_name]
    status = await message.answer(
        f"🔎 Разбираю раздачу {tracker_name} через прокси {CONFIG.proxy_url}..."
    )
    try:
        result = await to_thread(tracker.resolve, url)
    except NotLoggedInError as exc:
        return await status.edit_text(
            f"🔑 {exc}\nВыполните /login_{tracker_name} и повторите."
        )
    except CaptchaError as exc:
        if _is_cloudflare(exc):
            return await status.edit_text(
                CLOUDFLARE_HINT,
                reply_markup=_open_in_browser_keyboard(url, "🌐 Открыть раздачу"),
            )
        return await status.edit_text(f"🤖 {exc}")
    except ProxyUnavailableError as exc:
        return await status.edit_text(
            f"🔌 Прокси {CONFIG.proxy_url} недоступен: {exc}"
        )
    except TrackerError as exc:
        return await status.edit_text(f"❌ Ошибка трекера: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка разбора страницы трекера")
        return await status.edit_text(f"💥 Внутренняя ошибка: {exc}")

    await propose_folder(
        message,
        name=result.name or f"{tracker_name}_download",
        magnet=result.magnet,
        torrent_bytes=result.torrent_bytes,
        source=f"{tracker_name}: {result.page_url}",
        status_message=status,
    )


# --------------------------------------------------------------------------- #
#  Предложение папки (вместо автоматического выбора)
# --------------------------------------------------------------------------- #
async def propose_folder(
    message: Message,
    *,
    name: str,
    magnet: Optional[str] = None,
    torrent_bytes: Optional[bytes] = None,
    source: str = "",
    status_message: Optional[Message] = None,
    user_id: Optional[int] = None,
) -> None:
    _prune_pending()

    if torrent_bytes:
        suggested = torrent_parser.detect_category_for_bytes(name, torrent_bytes)
    else:
        suggested = torrent_parser.detect_category(name)
    token = secrets.token_urlsafe(8)
    # user_id must be passed explicitly when called from a callback: there
    # message.from_user is the bot itself.
    owner_id = user_id if user_id is not None else (
        message.from_user.id if message.from_user else 0
    )
    PENDING[token] = PendingTorrent(
        token=token,
        user_id=owner_id,
        name=name,
        suggested=suggested,
        magnet=magnet,
        torrent_bytes=torrent_bytes,
        source=source,
        created=time.time(),
    )

    suggested_dir = torrent_parser.resolve_download_dir(suggested)
    if suggested == config_module.CATEGORY_OTHER:
        hint = "🤔 <i>Категорию определить не удалось — выберите папку вручную.</i>"
    else:
        hint = (
            f"Предлагаю: <b>{torrent_parser.category_label(suggested)}</b> "
            f"(⭐)\n<code>{suggested_dir}</code>"
        )

    # Что внутри раздачи — помогает понять папку, когда имя ни о чём не говорит.
    content_line = ""
    if torrent_bytes:
        summary = torrent_parser.summarize_content(torrent_bytes)
        if summary:
            content_line = f"📦 {summary}\n"

    text = (
        "🎯 <b>Куда сохранить?</b>\n\n"
        f"<b>{_escape(name)}</b>\n"
        f"{content_line}\n"
        f"{hint}\n\n"
        "Подтвердите кнопкой ниже или выберите другую папку."
    )
    markup = _build_folder_keyboard(token, suggested)
    await _respond(message, status_message, text, reply_markup=markup)


def _build_folder_keyboard(token: str, suggested: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for cat, label, _path in torrent_parser.category_choices():
        prefix = "⭐ " if cat == suggested else ""
        row.append(
            InlineKeyboardButton(
                text=f"{prefix}{label}", callback_data=f"dl|{cat}|{token}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel|{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("dl|"))
async def on_folder_chosen(callback: CallbackQuery) -> None:
    if not _user_allowed(callback.from_user.id):
        return await callback.answer("⛔ Нет доступа", show_alert=True)

    try:
        _, category, token = (callback.data or "").split("|", 2)
    except ValueError:
        return await callback.answer("Некорректные данные", show_alert=True)

    pending = PENDING.get(token)
    if not pending:
        return await callback.answer(
            "⌛️ Запрос устарел, пришлите торрент заново.", show_alert=True
        )
    if pending.user_id != callback.from_user.id:
        return await callback.answer("Это не ваш торрент.", show_alert=True)

    # Забираем из очереди сразу, чтобы двойной клик не добавил дважды.
    PENDING.pop(token, None)
    await callback.answer("Добавляю...")

    download_dir = torrent_parser.resolve_download_dir(category)
    try:
        info = await to_thread(
            transmission.add_torrent,
            torrent_bytes=pending.torrent_bytes,
            magnet=pending.magnet,
            download_dir=download_dir,
        )
    except TransmissionUnavailable as exc:
        return await _edit(callback, f"⚠️ Transmission недоступен: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка добавления торрента")
        return await _edit(callback, f"💥 Ошибка добавления: {exc}")

    lines = [
        "✅ <b>Добавлено в Transmission</b>",
        f"Название: <b>{_escape(info.name or pending.name)}</b>",
        f"Категория: <b>{torrent_parser.category_label(category)}</b>",
        f"Папка: <code>{download_dir}</code>",
    ]
    if pending.source:
        lines.append(f"Источник: {_escape(pending.source)}")
    await _edit(callback, "\n".join(lines))


@router.callback_query(F.data.startswith("cancel|"))
async def on_cancel(callback: CallbackQuery) -> None:
    if not _user_allowed(callback.from_user.id):
        return await callback.answer("⛔ Нет доступа", show_alert=True)

    parts = (callback.data or "cancel|").split("|", 1)
    token = parts[1] if len(parts) > 1 else ""
    pending = PENDING.pop(token, None)
    if pending is None:
        return await callback.answer("Уже отменено.")
    await callback.answer("Отменено.")
    await _edit(callback, "🚫 Отменено — торрент не добавлен.")


# --------------------------------------------------------------------------- #
#  Вспомогательное
# --------------------------------------------------------------------------- #
async def _respond(
    message: Message,
    status_message: Optional[Message],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    if status_message is not None:
        try:
            await status_message.edit_text(text, reply_markup=reply_markup)
            return
        except Exception:  # noqa: BLE001
            pass
    await message.answer(text, reply_markup=reply_markup)


async def _edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:  # noqa: BLE001
        try:
            await callback.message.answer(text, reply_markup=reply_markup)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось отредактировать сообщение")


# --------------------------------------------------------------------------- #
#  Уведомления о завершении загрузки
# --------------------------------------------------------------------------- #
class CompletionNotifier:
    """Опрашивает Transmission и уведомляет о завершённых загрузках."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._seen: Dict[int, str] = {}
        self._notified: Set[int] = set()

    async def run(self) -> None:
        await self._prime()
        while True:
            await asyncio.sleep(CONFIG.poll_interval)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в цикле уведомлений")

    async def _prime(self) -> None:
        try:
            torrents = await to_thread(transmission.get_status)
        except Exception:  # noqa: BLE001
            return
        for t in torrents:
            self._seen[t.id] = t.name
            if t.is_complete:
                self._notified.add(t.id)

    async def _tick(self) -> None:
        try:
            torrents = await to_thread(transmission.get_status)
        except TransmissionUnavailable as exc:
            log.warning("Transmission недоступен в цикле уведомлений: %s", exc)
            return

        current_ids = {t.id for t in torrents}
        self._notified &= current_ids
        self._seen = {t.id: t.name for t in torrents}

        if not CONFIG.notify_on_complete:
            return

        for t in torrents:
            if t.is_complete and t.id not in self._notified:
                self._notified.add(t.id)
                await self._notify(t)

    async def _notify(self, torrent) -> None:
        targets = set(CONFIG.allowed_user_ids) | known_chats
        if not targets:
            log.info("Некуда отправлять уведомление (нет известных чатов)")
            return
        text = (
            "🎉 <b>Загрузка завершена</b>\n"
            f"<b>{_escape(torrent.name)}</b>\n"
            f"Размер: {torrent_parser.human_size(torrent.total_size)}\n"
            f"Папка: <code>{torrent.download_dir}</code>"
        )
        for chat_id in targets:
            try:
                await self._bot.send_message(chat_id, text)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось отправить уведомление в %s: %s", chat_id, exc)


# --------------------------------------------------------------------------- #
#  Запуск
# --------------------------------------------------------------------------- #
async def on_startup(bot: Bot) -> None:
    log.info("Бот запускается. Прокси трекеров: %s", CONFIG.proxy_url)
    for tracker_name, tracker in trackers.items():
        loaded = tracker.load_cookies(CONFIG.cookies_ttl_days)
        log.info("[%s] cookies из файла: %s", tracker_name, "да" if loaded else "нет")
    notifier = CompletionNotifier(bot)
    asyncio.create_task(notifier.run())


async def on_shutdown(bot: Bot) -> None:
    log.info("Останавливаюсь, сохраняю cookies...")
    for tracker in trackers.values():
        try:
            tracker.save_cookies()
            tracker.close()
        except Exception:  # noqa: BLE001
            pass
    await bot.session.close()


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CLOUDFLARE_HINT = (
    "☁️ Трекер отдал Cloudflare-проверку, которую проходит только браузер: "
    "бот выходит с серверного IP.\n\n"
    "Что делать прямо сейчас:\n"
    "1) открой раздачу в браузере на телефоне (там проверка проходится сама);\n"
    "2) пришли боту <b>.torrent</b> файлом или <b>magnet</b>-ссылку — "
    "категорию, папку и Transmission бот сделает сам."
)


def _is_cloudflare(exc: Exception) -> bool:
    text = str(exc).lower()
    return "just a moment" in text or "cloudflare" in text


def _open_in_browser_keyboard(
    url: str, label: str = "🌐 Открыть в браузере"
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]]
    )


def _tracker_search_keyboard(query: str) -> Optional[InlineKeyboardMarkup]:
    """Кнопки «искать в браузере» — работают, когда бот упёрся в Cloudflare."""
    rows: List[List[InlineKeyboardButton]] = []
    for name in ("rutracker", "kinozal"):
        tracker = trackers.get(name)
        if tracker is None:
            continue
        try:
            url = tracker.search_url(query)
        except Exception:  # noqa: BLE001
            continue
        rows.append([InlineKeyboardButton(text=f"🔎 Искать на {name}", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --------------------------------------------------------------------------- #
#  Проверка связности перед запуском
# --------------------------------------------------------------------------- #
def _tcp_check(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def preflight() -> None:
    """Быстрые подсказки при старте: доступны ли прокси.

    Не фатально: бот продолжает работу, но в логе сразу видно, что адрес/порт
    прокси указаны неверно - а не только по таймауту через минуту.
    """
    if not CONFIG.use_proxy:
        log.info("Трекеры ходят НАПРЯМУЮ (PROXY_URL=%s)", CONFIG.proxy_url or "пусто")
        parsed = None
    else:
        parsed = urlparse(CONFIG.proxy_url)

    if parsed is not None and parsed.hostname and parsed.port:
        if await to_thread(_tcp_check, parsed.hostname, parsed.port):
            log.info("Прокси трекеров доступен: %s", CONFIG.proxy_url)
        else:
            log.warning(
                "Прокси трекеров НЕДОСТУПЕН (TCP %s:%s). Проверьте адрес/порт "
                "в PROXY_URL и правило firewall на OpenWrt.",
                parsed.hostname, parsed.port,
            )

    if CONFIG.telegram_proxy:
        tp = urlparse(CONFIG.telegram_proxy)
        if tp.hostname and tp.port and not await to_thread(_tcp_check, tp.hostname, tp.port):
            log.warning(
                "Telegram-прокси НЕДОСТУПЕН (TCP %s:%s). Проверьте "
                "TELEGRAM_PROXY_URL и firewall на OpenWrt.",
                tp.hostname, tp.port,
            )


async def main() -> None:
    setup_logging()

    problem = CONFIG.validate()
    if problem:
        log.error("Ошибка конфигурации: %s", problem)
        sys.exit(1)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Telegram Bot API access. If api.telegram.org is blocked on the ISP, set
    # TELEGRAM_PROXY_URL (HTTP proxy) - only this control channel is proxied,
    # P2P and Transmission stay direct.
    session = None
    if CONFIG.telegram_proxy:
        log.info("Telegram Bot API через прокси: %s", CONFIG.telegram_proxy)
        session = ProxiedAiohttpSession(proxy=CONFIG.telegram_proxy)

    bot = Bot(
        token=CONFIG.telegram_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    log.info("Проверяю Transmission...")
    try:
        await to_thread(transmission.get_status)
        log.info("Transmission доступен (без прокси).")
    except TransmissionUnavailable as exc:
        log.warning("Transmission сейчас недоступен: %s", exc)

    await preflight()

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramNetworkError as exc:
        log.error("Нет связи с Telegram Bot API: %s", exc)
        log.error("Что проверить:")
        log.error("  1) открывается ли api.telegram.org с этого NAS напрямую;")
        log.error("  2) если провайдер его блокирует - задайте TELEGRAM_PROXY_URL")
        log.error("     (HTTP-прокси, напр. http://<IP_OpenWrt>:11081) и перезапустите;")
        log.error("  3) диагностика: .venv/bin/python deploy/check_proxy.py")
        raise SystemExit(1)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено пользователем")
