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
from typing import Any, Dict, List, Optional, Set, cast
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command
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


def _prune_pending() -> None:
    ttl = CONFIG.confirm_ttl
    now = time.time()
    expired = [tok for tok, item in PENDING.items() if now - item.created > ttl]
    for tok in expired:
        PENDING.pop(tok, None)


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
        "/login_rutracker — обновить сессию rutracker\n"
        "/login_kinozal — обновить сессию kinozal\n"
        "/status — активные загрузки\n"
        "/stats — суммарная статистика\n"
        "/dirs — показать список папок\n"
        "/help — эта справка\n\n"
        f"<i>Прокси для трекеров:</i> <code>{CONFIG.proxy_url}</code>\n"
        f"<i>Transmission:</i> <code>{CONFIG.transmission_host}:"
        f"{CONFIG.transmission_port}</code> (без прокси)"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@router.message(Command("dirs"))
async def cmd_dirs(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    lines = ["📂 <b>Папки загрузки</b>", ""]
    for cat, label, path in torrent_parser.category_choices():
        lines.append(f"{label}: <code>{path}</code>")
    await message.answer("\n".join(lines))


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


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    try:
        torrents = await to_thread(transmission.get_status)
    except TransmissionUnavailable as exc:
        return await message.answer(f"⚠️ Transmission недоступен: {exc}")

    if not torrents:
        return await message.answer("📭 Список загрузок пуст.")

    active = [t for t in torrents if t.status in ("downloading", "seeding")]
    header = f"📊 <b>Загрузки</b> ({len(torrents)} всего, {len(active)} активных)\n\n"
    body = "\n".join(TransmissionClient.format_torrent(t) for t in torrents[:20])
    note = "\n\n<i>Показаны первые 20.</i>" if len(torrents) > 20 else ""
    await message.answer(header + body + note)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_allowed(message):
        return await deny(message)
    track_chat(message)
    try:
        stats = await to_thread(transmission.get_stats)
        session = await to_thread(transmission.session_stats)
    except TransmissionUnavailable as exc:
        return await message.answer(f"⚠️ Transmission недоступен: {exc}")

    await message.answer(
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
) -> None:
    _prune_pending()

    suggested = torrent_parser.detect_category(name)
    token = secrets.token_urlsafe(8)
    PENDING[token] = PendingTorrent(
        token=token,
        user_id=message.from_user.id if message.from_user else 0,
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

    text = (
        "🎯 <b>Куда сохранить?</b>\n\n"
        f"<b>{_escape(name)}</b>\n\n"
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


async def _edit(callback: CallbackQuery, text: str) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text)
    except Exception:  # noqa: BLE001
        try:
            await callback.message.answer(text)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось отредактировать сообщение с выбором папки")


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
    parsed = urlparse(CONFIG.proxy_url)
    if parsed.hostname and parsed.port:
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

    dp = Dispatcher()
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
