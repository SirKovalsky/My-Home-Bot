#!/usr/bin/env python3
"""Test the trackers directly - no Telegram, faster and more detailed.

Run:

    cd /volume1/My-Home-Bot
    sudo .venv/bin/python deploy/test_trackers.py [query]

Shows:
  * whether curl_cffi is in use (without it Cloudflare answers
    'Just a moment...' with HTTP 403);
  * which proxy is used;
  * how many results were found and which seed counts were parsed;
  * whether the first release opens (magnet or .torrent).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

import config as config_module  # noqa: E402
from trackers import KinozalTracker, RuTrackerTracker  # noqa: E402
from trackers.base import HAVE_CURL_CFFI, IMPERSONATE  # noqa: E402


def main() -> int:
    query = " ".join(sys.argv[1:]).strip() or "interstellar"
    cfg = config_module.CONFIG

    print("=" * 68)
    if HAVE_CURL_CFFI:
        print(f"curl_cffi : YES (impersonate={IMPERSONATE})")
    else:
        print("curl_cffi : NO -> Cloudflare will very likely answer 'Just a moment...'")
    print(f"PROXY_URL : {cfg.proxy_url}   (use_proxy={cfg.use_proxy})")
    print(f"query     : {query!r}")
    print("=" * 68)

    # Optional: test browser cookies without touching Telegram, e.g.
    #   sudo env TRACKER_COOKIES='cf_clearance=...; bb_session=...' \
    #            TRACKER_UA='Mozilla/5.0 ...' \
    #       .venv/bin/python deploy/test_trackers.py interstellar
    raw_cookies = os.getenv("TRACKER_COOKIES", "").strip()
    raw_ua = os.getenv("TRACKER_UA", "").strip()
    if raw_cookies:
        print("browser cookies : provided via TRACKER_COOKIES")
    if raw_ua:
        print("user agent      : provided via TRACKER_UA")

    for name, cls in (("rutracker", RuTrackerTracker), ("kinozal", KinozalTracker)):
        tracker = cls(
            proxies=cfg.proxies,
            cookies_path=cfg.cookies_path,
            user_agent=cfg.user_agent,
            timeout=cfg.proxy_timeout,
        )
        try:
            tracker.load_cookies(cfg.cookies_ttl_days)
            if raw_ua:
                tracker.set_user_agent(raw_ua)
            if raw_cookies:
                count = tracker.import_cookies(raw_cookies)
                print(f"[{name}] imported {count} cookies; verify -> ", end="")
                try:
                    print(tracker.verify_session())
                except Exception as exc:  # noqa: BLE001
                    print(f"{type(exc).__name__}: {exc}")
            results = tracker.search(query, 8)
            print(f"\n[{name}] found: {len(results)}")
            for item in results:
                print(f"   {item.seeds:>6}u {item.leeches:>5}d  {item.title[:60]}")
                print(f"            {item.url}")

            if results:
                print(f"\n[{name}] opening the first release...")
                resolved = tracker.resolve(results[0].url)
                if resolved.is_magnet:
                    kind = "magnet"
                else:
                    kind = f"{len(resolved.torrent_bytes or b'')} bytes .torrent"
                print(f"   -> {resolved.name[:60]!r}  ({kind})")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{name}] ERROR: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
