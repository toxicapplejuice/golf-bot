#!/usr/bin/env python3
"""Pre-flight smoke test for the golf booking bot.

Runs 15 minutes before the actual bot (at 7:30 PM Monday) to verify:
- Required env vars are present
- Python deps import cleanly
- Playwright can launch Firefox
- WebTrac site is reachable and login flow works
- Notification channels actually deliver

If anything's broken, sends a high-priority notification so there's time
to fix it before 8:00 PM. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    print("=" * 50)
    print("Golf Bot Pre-Flight Check")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 1. Env vars
    print("\n[1/5] Checking environment...")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    except Exception as e:
        failures.append(f"Failed to load .env: {e}")
        print(f"  FAIL: {e}")
    else:
        for key in ("GOLF_USERNAME", "GOLF_PASSWORD"):
            if not os.getenv(key):
                failures.append(f"Missing required env var: {key}")
                print(f"  FAIL: missing {key}")
            else:
                print(f"  OK: {key} present")
        if not os.getenv("NTFY_TOPIC") and not os.getenv("SMTP_SERVER"):
            warnings.append("No notification channel configured (no NTFY_TOPIC or SMTP)")
            print(f"  WARN: no notifications configured")

    # 2. Imports
    print("\n[2/5] Checking Python deps...")
    try:
        import bot  # noqa: F401
        import config  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        try:
            from playwright_stealth import stealth_sync  # noqa: F401
        except ImportError:
            from playwright_stealth import Stealth  # noqa: F401
        print("  OK: all imports succeeded")
    except Exception as e:
        failures.append(f"Import failure: {e}")
        print(f"  FAIL: {e}")
        traceback.print_exc()
        # If imports fail, nothing else will work
        _notify_and_exit(failures, warnings)
        return 1

    # 3. Playwright browser launch
    print("\n[3/5] Checking Playwright browser launch...")
    page = None
    browser = None
    playwright_ctx = None
    try:
        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth import stealth_sync
        except ImportError:
            from playwright_stealth import Stealth
            stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

        playwright_ctx = sync_playwright().start()
        browser = playwright_ctx.firefox.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        stealth_sync(page)
        print("  OK: Firefox launched")
    except Exception as e:
        failures.append(f"Playwright launch failed: {e}")
        print(f"  FAIL: {e}")
        traceback.print_exc()
        _notify_and_exit(failures, warnings)
        return 1

    # 4. Site reachability + login
    print("\n[4/5] Checking WebTrac site + login...")
    try:
        start = time.time()
        page.goto("https://txaustinweb.myvscloud.com/webtrac/web",
                  timeout=30000, wait_until="domcontentloaded")
        load_time = time.time() - start
        print(f"  OK: site loaded in {load_time:.1f}s")

        # Check for Queue-it (unusual this early; worth warning)
        content_lower = ""
        try:
            content_lower = page.content().lower()
        except Exception:
            pass
        if "queue-it.net" in page.url.lower() or "virtual waiting room" in content_lower:
            warnings.append("Site is in Queue-it waiting room during preflight (unusual)")
            print("  WARN: hit Queue-it early")

        # Try actual login
        from dotenv import load_dotenv
        load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
        import bot as bot_mod
        if bot_mod.login_once(page, queue_mode="timeout"):
            print("  OK: login succeeded")
        else:
            failures.append("Login failed during preflight")
            print("  FAIL: login failed")
    except Exception as e:
        failures.append(f"Site/login check failed: {e}")
        print(f"  FAIL: {e}")
        traceback.print_exc()
    finally:
        try:
            if page: page.close()
            if browser: browser.close()
            if playwright_ctx: playwright_ctx.stop()
        except Exception:
            pass

    # 5. Notification channel test
    print("\n[5/5] Testing notification delivery...")
    try:
        import bot as bot_mod
        if bot_mod.NTFY_TOPIC:
            bot_mod.send_ntfy(
                "✈️ Golf Bot preflight",
                f"Preflight running at {datetime.now().strftime('%H:%M')} — "
                "you should get a booking notification ~15 min from now.",
                priority="low",
                tags="airplane",
            )
            print("  OK: ntfy test sent")
        elif bot_mod.SMTP_SERVER:
            bot_mod.send_email(
                "Golf Bot preflight check",
                f"Preflight passed at {datetime.now().strftime('%H:%M')}",
            )
            print("  OK: email test sent")
        else:
            print("  SKIP: no notification channel configured")
    except Exception as e:
        warnings.append(f"Notification test failed: {e}")
        print(f"  WARN: {e}")

    _notify_and_exit(failures, warnings)
    return 1 if failures else 0


def _notify_and_exit(failures: list, warnings: list) -> None:
    """Send summary notification of preflight results."""
    print("\n" + "=" * 50)
    if failures:
        print(f"PREFLIGHT FAILED — {len(failures)} issue(s):")
        for f in failures:
            print(f"  ❌ {f}")
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  ⚠️  {w}")
    if not failures and not warnings:
        print("PREFLIGHT OK — ready for 8:00 PM")
    print("=" * 50)

    # Notify on failure (high priority) — warnings alone don't need to wake user
    if failures:
        try:
            import bot as bot_mod
            body = "FAILURES:\n" + "\n".join(f"- {f}" for f in failures)
            if warnings:
                body += "\n\nWARNINGS:\n" + "\n".join(f"- {w}" for w in warnings)
            body += "\n\nFix before 8:00 PM or the bot won't book anything tonight."
            bot_mod.notify(
                "🚨 Golf Bot PREFLIGHT FAILED",
                body,
                priority="urgent",
                tags="rotating_light",
            )
        except Exception as e:
            print(f"Failed to send failure notification: {e}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Catch-all — we still want to notify on catastrophic failure
        print(f"Preflight crashed: {e}")
        traceback.print_exc()
        try:
            import bot as bot_mod
            bot_mod.notify(
                "🚨 Golf Bot PREFLIGHT CRASHED",
                f"Preflight script itself crashed:\n{e}\n\n{traceback.format_exc()[:500]}",
                priority="urgent",
                tags="rotating_light",
            )
        except Exception:
            pass
        sys.exit(1)
