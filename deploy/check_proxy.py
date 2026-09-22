#!/usr/bin/env python3
"""Диагностика окружения бота БЕЗ nc и curl (их на DSM нет).

Запускать питоном из venv проекта:

    cd /volume1/torrent-bot
    sudo .venv/bin/python deploy/check_proxy.py

Скрипт проверяет по очереди:

  1) TCP-доступность SOCKS5-порта на OpenWrt;
  2) выход в интернет ЧЕРЕЗ прокси   -> должен быть IP удалённого сервера;
  3) выход в интернет НАПРЯМУЮ       -> должен быть IP вашего провайдера;
  4) доступность rutracker.org через прокси;
  5) что Transmission RPC отвечает напрямую, без прокси.

Вывод — только ASCII, чтобы не спотыкаться о локаль на DSM.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

# Чтобы запускать и из корня проекта, и из deploy/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import requests
except ImportError:
    print("[FAIL] requests не установлен. Активируйте venv проекта: .venv/bin/python")
    sys.exit(2)

try:
    import config as config_module
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] не удалось прочитать config.py: {exc}")
    sys.exit(2)


def _reconfigure_stdout() -> None:
    """Не падать на юникоде, если локаль на DSM кривая."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def parse_proxy(url: str):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def make_proxies(url: str) -> dict:
    return {"http": url, "https": url}


def direct_session() -> requests.Session:
    """Сессия строго без прокси — как клиент Transmission в боте."""
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}
    return session


def line(name: str, ok: bool, detail: str = "") -> bool:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {name}")
    if detail:
        for chunk in str(detail).splitlines():
            print(f"        {chunk}")
    return ok


def check_tcp(host: str, port: int, timeout: float = 5.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} принимает TCP-соединения"
    except OSError as exc:
        return False, f"{host}:{port} недоступен: {exc}"


def check_exit_ip_via_proxy(proxy_url: str, user_agent: str):
    try:
        response = requests.get(
            "https://api.ipify.org",
            proxies=make_proxies(proxy_url),
            timeout=20,
            headers={"User-Agent": user_agent},
        )
        return True, f"HTTP {response.status_code}, внешний IP через прокси: {response.text.strip()}"
    except Exception as exc:  # noqa: BLE001
        return False, f"запрос через прокси не удался: {type(exc).__name__}: {exc}"


def check_exit_ip_direct(user_agent: str):
    try:
        response = direct_session().get(
            "https://api.ipify.org", timeout=20, headers={"User-Agent": user_agent}
        )
        return True, f"HTTP {response.status_code}, прямой внешний IP: {response.text.strip()}"
    except Exception as exc:  # noqa: BLE001
        return False, f"прямой запрос не удался: {type(exc).__name__}: {exc}"


def check_tracker(proxy_url: str, user_agent: str):
    results = []
    ok_any = False
    for url in ("https://rutracker.org/forum/index.php", "https://kinozal.me/"):
        try:
            response = requests.get(
                url,
                proxies=make_proxies(proxy_url),
                timeout=25,
                headers={"User-Agent": user_agent},
                allow_redirects=True,
            )
            # Любой HTTP-ответ означает, что путь через прокси рабочий.
            results.append(f"{url} -> HTTP {response.status_code}")
            ok_any = True
        except Exception as exc:  # noqa: BLE001
            results.append(f"{url} -> {type(exc).__name__}: {exc}")
    return ok_any, "\n".join(results)


def check_transmission():
    try:
        from transmission_client import TransmissionClient, TransmissionUnavailable
    except Exception as exc:  # noqa: BLE001
        return False, f"не удалось импортировать transmission_client: {exc}"

    client = TransmissionClient(config_module.CONFIG)
    try:
        torrents = client.get_status()
        return True, f"RPC отвечает, торрентов в списке: {len(torrents)} (прокси не используется)"
    except TransmissionUnavailable as exc:
        return False, f"Transmission недоступен: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"ошибка обращения к Transmission: {type(exc).__name__}: {exc}"


def main() -> int:
    _reconfigure_stdout()

    cfg = config_module.CONFIG
    scheme, host, port = parse_proxy(cfg.proxy_url)
    ua = cfg.user_agent

    print("=" * 68)
    print("  Диагностика torrent-bot")
    print("=" * 68)
    print(f"PROXY_URL        : {cfg.proxy_url}")
    print(f"Transmission RPC : {cfg.transmission_host}:{cfg.transmission_port}")
    print(f"Схема прокси     : {scheme or '(не разобрана)'}")
    print("-" * 68)

    if not host or not port:
        line("Разбор PROXY_URL", False, "не удалось выделить хост/порт из PROXY_URL")
        return 1

    if host in ("127.0.0.1", "localhost", "::1"):
        print(
            "[WARN] PROXY_URL указывает на localhost. На Xpenology это сам Xpenology,\n"
            "       а не OpenWrt! Укажите LAN-адрес OpenWrt, например socks5h://192.168.1.2:20170"
        )
        print("-" * 68)

    results = []

    ok, detail = check_tcp(host, port)
    results.append(line(f"1) TCP до прокси {host}:{port}", ok, detail))
    if not ok:
        print("-" * 68)
        print("Дальнейшие проверки через прокси бессмысленны.")
        print("Смотрите на OpenWrt:")
        print("  netstat -lnpt | grep %s" % port)
        print("Должно быть 0.0.0.0:%s, а не 127.0.0.1:%s." % (port, port))
        print("Если там 127.0.0.1 -> включите Port sharing в панели v2rayA,")
        print("либо поднимите проброс: socat TCP-LISTEN:%s,fork,reuseaddr \\" % port)
        print("                            TCP:127.0.0.1:%s" % port)
        print("-" * 68)
        return 1

    if scheme in ("socks5h", "socks5", "http", "https"):
        results.append(line("2) Выход через прокси", *check_exit_ip_via_proxy(cfg.proxy_url, ua)))
    else:
        results.append(line("2) Выход через прокси", False, f"неизвестная схема: {scheme}"))

    results.append(line("3) Выход напрямую (для сравнения)", *check_exit_ip_direct(ua)))

    tracker_ok, tracker_detail = check_tracker(cfg.proxy_url, ua)
    results.append(line("4) Трекеры через прокси", tracker_ok, tracker_detail))

    results.append(line("5) Transmission напрямую", *check_transmission()))

    print("=" * 68)
    if all(results):
        print("ИТОГ: всё в порядке.")
        return 0

    print("ИТОГ: есть проблемы (см. FAIL выше).")
    if not results[1] and results[0]:
        print("Подсказка: TCP есть, а через SOCKS не ходит — проверьте, что в v2rayA")
        print("выбран рабочий узел/группа и режим правил пускает трафик через прокси.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
