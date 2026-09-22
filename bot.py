"""Точка входа Telegram-бота автозакачки торрентов в Transmission.

Архитектура (см. README.md):
    Telegram  ->  бот на Xpenology  ->  [прокси OpenWrt]  ->  rutracker/kinozal
                                     ->  [localhost, БЕЗ прокси] ->  Transmission

    Прокси используется ИСКЛЮЧИТЕЛЬНО внутри ``trackers/*`` (явный параметр
    ``proxies=``). Клиент Transmission прокси не использует никогда.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from io import BytesIO
from typing import Dict, List, Optional, Set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

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

log = logging.getLogger("torrent-bot")

router = Router()

# Клиенты создаются лениво/один раз.
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
def is_allowed(message: Message) -> bool:
    allowed: List[int] = CONFIG.allowed_user_ids
    if not allowed:
        return True
    return bool(message.from_user and message.from_user.id in allowed)


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
        "• magnet-ссылку — сразу добавлю в Transmission;\n"
        "• <code>.torrent</code> файл — сразу добавлю;\n"
        "• ссылку на раздачу rutracker.org / kinozal.me — скачаю .torrent "
        "через прокси и добавлю.\n\n"
        "<b>Команды:</b>\n"
        "/login_rutracker — обновить сессию rutracker\n"
        "/login_kinozal — обновить сессию kinozal\n"
        "/status — активные загрузки\n"
        "/stats — суммарная статистика\n"
        "/help — эта справка\n\n"
        f"<i>Прокси для трекеров:</i> <code>{CONFIG.proxy_url}</code>\n"
        f"<i>Transmission:</i> <code>{CONFIG.transmission_host}:"
        f"{CONFIG.transmission_port}</code> (без прокси)"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


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
        await asyncio.to_thread(tracker.login, login, password)
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
        torrents = await asyncio.to_thread(transmission.get_status)
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
        stats = await asyncio.to_thread(transmission.get_stats)
        session = await asyncio.to_thread(transmission.session_stats)
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
#  Приём magnet / ссылок
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
        await add_to_transmission(message, name=torrent_parser.magnet_name(text), magnet=text)
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
    await status.edit_text("⏳ Отправляю в Transmission...")
    await add_to_transmission(message, name=name, torrent_bytes=raw, status_message=status)


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
        result = await asyncio.to_thread(tracker.resolve, url)
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

    await status.edit_text("⏳ Передаю в Transmission (локально, без прокси)...")
    await add_to_transmission(
        message,
        name=result.name or f"{tracker_name}_download",
        magnet=result.magnet,
        torrent_bytes=result.torrent_bytes,
        source=f"{tracker_name}: {result.page_url}",
        status_message=status,
    )


# --------------------------------------------------------------------------- #
#  Добавление в Transmission
# --------------------------------------------------------------------------- #
async def add_to_transmission(
    message: Message,
    *,
    name: str,
    magnet: Optional[str] = None,
    torrent_bytes: Optional[bytes] = None,
    source: str = "",
    status_message: Optional[Message] = None,
) -> None:
    category = torrent_parser.detect_category(name)
    download_dir = torrent_parser.resolve_download_dir(category)

    try:
        info = await asyncio.to_thread(
            transmission.add_torrent,
            torrent_bytes=torrent_bytes,
            magnet=magnet,
            download_dir=download_dir,
        )
    except TransmissionUnavailable as exc:
        text = f"⚠️ Transmission недоступен: {exc}"
        return await _respond(message, status_message, text)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка добавления торрента")
        return await _respond(message, status_message, f"💥 Ошибка добавления: {exc}")

    category_label = {
        config_module.CATEGORY_SERIES: "сериалы",
        config_module.CATEGORY_FILMS: "фильмы",
        config_module.CATEGORY_OTHER: "прочее",
    }.get(category, category)

    lines = [
        "✅ <b>Добавлено в Transmission</b>",
        f"Название: <b>{_escape(info.name or name)}</b>",
        f"Категория: <b>{category_label}</b>",
        f"Папка: <code>{download_dir}</code>",
    ]
    if source:
        lines.append(f"Источник: {_escape(source)}")
    await _respond(message, status_message, "\n".join(lines))


async def _respond(message: Message, status_message: Optional[Message], text: str) -> None:
    if status_message is not None:
        try:
            await status_message.edit_text(text)
            return
        except Exception:  # noqa: BLE001
            pass
    await message.answer(text)


# --------------------------------------------------------------------------- #
#  Уведомления о завершении загрузки
# --------------------------------------------------------------------------- #
class CompletionNotifier:
    """Опрашивает Transmission и уведомляет о завершённых загрузках."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._seen: Dict[int, str] = {}       # id -> name
        self._notified: Set[int] = set()      # id уже уведомлённых

    async def run(self) -> None:
        # Первичный снимок: не уведомляем о том, что уже было завершено.
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
            torrents = await asyncio.to_thread(transmission.get_status)
        except Exception:  # noqa: BLE001
            return
        for t in torrents:
            self._seen[t.id] = t.name
            if t.is_complete:
                self._notified.add(t.id)

    async def _tick(self) -> None:
        try:
            torrents = await asyncio.to_thread(transmission.get_status)
        except TransmissionUnavailable as exc:
            log.warning("Transmission недоступен в цикле уведомлений: %s", exc)
            return

        current_ids = {t.id for t in torrents}
        # Забываем удалённые торренты, чтобы не уведомлять повторно при новом id.
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

    bot = Bot(
        token=CONFIG.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    log.info("Проверяю Transmission...")
    try:
        await asyncio.to_thread(transmission.get_status)
        log.info("Transmission доступен (без прокси).")
    except TransmissionUnavailable as exc:
        log.warning("Transmission сейчас недоступен: %s", exc)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено пользователем")
