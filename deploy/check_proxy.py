#!/usr/bin/env python3
"""Diagnose the bot environment WITHOUT nc or curl (absent on DSM).

Run it with the project venv:

    cd /volume1/torrent-bot
    sudo .venv/bin/python deploy/check_proxy.py

Checks, in order:

  1) TCP reachability of the SOCKS5 port on OpenWrt;
  2) internet access THROUGH the proxy  -> must be the remote server IP;
  3) internet access DIRECTLY           -> must be the ISP IP (for comparison);
  4) rutracker.org / kinozal.me reachability through the proxy;
  5) that Transmission RPC answers directly, without a proxy.

Output is ASCII-only on purpose (safe on any DSM locale).
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

# Allow running both from the project root and from deploy/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import requests
except ImportError:
    print("[FAIL] requests is not installed. Use the project venv: .venv/bin/python")
    sys.exit(2)

try:
    import config as config_module
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] cannot read config.py: {exc}")
    sys.exit(2)


def _reconfigure_stdout() -> None:
    """Never crash on unicode if the DSM locale is broken."""
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
    """A session strictly without a proxy - same as the Transmission client."""
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
            return True, f"{host}:{port} accepts TCP connections"
    except OSError as exc:
        return False, f"{host}:{port} unreachable: {exc}"


def check_exit_ip_via_proxy(proxy_url: str, user_agent: str):
    try:
        response = requests.get(
            "https://api.ipify.org",
            proxies=make_proxies(proxy_url),
            timeout=20,
            headers={"User-Agent": user_agent},
        )
        return True, f"HTTP {response.status_code}, exit IP via proxy: {response.text.strip()}"
    except Exception as exc:  # noqa: BLE001
        return False, f"request through the proxy failed: {type(exc).__name__}: {exc}"


def check_exit_ip_direct(user_agent: str):
    try:
        response = direct_session().get(
            "https://api.ipify.org", timeout=20, headers={"User-Agent": user_agent}
        )
        return True, f"HTTP {response.status_code}, direct exit IP: {response.text.strip()}"
    except Exception as exc:  # noqa: BLE001
        return False, f"direct request failed: {type(exc).__name__}: {exc}"


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
            # Any HTTP response means the path through the proxy works.
            results.append(f"{url} -> HTTP {response.status_code}")
            ok_any = True
        except Exception as exc:  # noqa: BLE001
            results.append(f"{url} -> {type(exc).__name__}: {exc}")
    return ok_any, "\n".join(results)


def check_transmission():
    try:
        from transmission_client import TransmissionClient, TransmissionUnavailable
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot import transmission_client: {exc}"

    client = TransmissionClient(config_module.CONFIG)
    try:
        torrents = client.get_status()
        return True, f"RPC is alive, torrents: {len(torrents)} (no proxy involved)"
    except TransmissionUnavailable as exc:
        return False, f"Transmission unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Transmission request error: {type(exc).__name__}: {exc}"


def main() -> int:
    _reconfigure_stdout()

    cfg = config_module.CONFIG
    scheme, host, port = parse_proxy(cfg.proxy_url)
    ua = cfg.user_agent

    print("=" * 68)
    print("  torrent-bot diagnostics")
    print("=" * 68)
    print(f"PROXY_URL        : {cfg.proxy_url}")
    print(f"Transmission RPC : {cfg.transmission_host}:{cfg.transmission_port}")
    print(f"Proxy scheme     : {scheme or '(not parsed)'}")
    print("-" * 68)

    if not host or not port:
        line("Parse PROXY_URL", False, "cannot extract host/port from PROXY_URL")
        return 1

    if host in ("127.0.0.1", "localhost", "::1"):
        print(
            "[WARN] PROXY_URL points to localhost. On Xpenology that is Xpenology\n"
            "       itself, not OpenWrt! Use the OpenWrt LAN address, e.g.\n"
            "       socks5h://192.168.1.2:20170"
        )
        print("-" * 68)

    results = []

    ok, detail = check_tcp(host, port)
    results.append(line(f"1) TCP to proxy {host}:{port}", ok, detail))
    if not ok:
        print("-" * 68)
        print("Testing through the proxy is pointless.")
        print("On OpenWrt check:")
        print("  netstat -lnpt | grep %s" % port)
        print("Expect 0.0.0.0:%s (or :::%s), not 127.0.0.1:%s." % (port, port, port))
        print("If it is 127.0.0.1, expose the port (see README section 4).")
        print("-" * 68)
        return 1

    if scheme in ("socks5h", "socks5", "http", "https"):
        results.append(line("2) Exit via proxy", *check_exit_ip_via_proxy(cfg.proxy_url, ua)))
    else:
        results.append(line("2) Exit via proxy", False, f"unknown scheme: {scheme}"))

    results.append(line("3) Exit directly (for comparison)", *check_exit_ip_direct(ua)))

    tracker_ok, tracker_detail = check_tracker(cfg.proxy_url, ua)
    results.append(line("4) Trackers via proxy", tracker_ok, tracker_detail))

    results.append(line("5) Transmission directly", *check_transmission()))

    print("=" * 68)
    if all(results):
        print("RESULT: everything is fine.")
        return 0

    print("RESULT: problems found (see FAIL above).")
    if not results[1] and results[0]:
        print("Hint: TCP works but SOCKS does not - make sure v2rayA has a working")
        print("node/outbound selected and its rules route this traffic via the proxy.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
