#!/usr/bin/env python3
"""Pre-flight smoke test for the golf booking bot.

Runs 15 minutes before the actual bot (at 7:30 PM Monday) to verify:
- Required env vars are present
- Python deps import cleanly
- Playwright can launch Firefox
- accounts.json loads with at least one enabled account
- WebTrac site is reachable + login works for EACH enabled account
- Notification channels deliver

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

    print("=" * 60)
    print("Austin Tee Time Bot — Pre-Flight Check")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. Env vars
    print("\n[1/5] Checking environment...")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    except Exception as e:
        failures.append(f"Failed to load .env: {e}")
        print(f"  FAIL: {e}")
    else:
        if not os.getenv("NTFY_TOPIC") and not os.getenv("SMTP_SERVER"):
            warnings.append("No notification channel configured (no NTFY_TOPIC or SMTP)")
            print(f"  WARN: no notifications configured")
        else:
            print(f"  OK: notifications configured")

    # 2. Imports + accounts.json
    print("\n[2/5] Checking Python deps + accounts.json...")
    try:
        import bot  # noqa: F401
        import config  # noqa: F401
        import shared_state  # noqa: F401
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
        _notify_and_exit(failures, warnings)
        return 1

    accounts = bot.load_accounts()
    if not accounts:
        failures.append("No enabled accounts in accounts.json (or file missing)")
        print("  FAIL: no enabled accounts")
        _notify_and_exit(failures, warnings)
        return 1
    print(f"  OK: {len(accounts)} enabled account(s): {', '.join(a['display_name'] for a in accounts)}")

    # 3. Playwright browser launch
    print("\n[3/5] Checking Playwright browser launch...")
    try:
        from playwright.sync_api import sync_playwright
        playwright_ctx = sync_playwright().start()
        browser = playwright_ctx.firefox.launch(headless=True)
        print("  OK: Firefox launched")
    except Exception as e:
        failures.append(f"Playwright launch failed: {e}")
        print(f"  FAIL: {e}")
        traceback.print_exc()
        _notify_and_exit(failures, warnings)
        return 1

    # 4. Site reachability + login for each account
    print(f"\n[4/5] Checking WebTrac login for {len(accounts)} account(s)...")
    try:
        from playwright_stealth import stealth_sync
    except ImportError:
        from playwright_stealth import Stealth
        stealth_sync = lambda p: Stealth().apply_stealth_sync(p)

    try:
        for acc in accounts:
            print(f"\n  → {acc['display_name']} (id={acc['id']})")
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            stealth_sync(page)
            try:
                start = time.time()
                page.goto("https://txaustinweb.myvscloud.com/webtrac/web",
                          timeout=30000, wait_until="domcontentloaded")
                print(f"    site loaded in {time.time() - start:.1f}s")

                # Override bot credentials temporarily for this login test
                bot.USERNAME = acc["username"]
                bot.PASSWORD = acc["password"]
                if bot.login_once(page, queue_mode="timeout"):
                    print(f"    OK: login succeeded")
                else:
                    msg = f"Login failed for {acc['display_name']}"
                    failures.append(msg)
                    print(f"    FAIL: {msg}")
            except Exception as e:
                msg = f"Preflight error for {acc['display_name']}: {e}"
                failures.append(msg)
                print(f"    FAIL: {msg}")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    finally:
        try:
            browser.close()
            playwright_ctx.stop()
        except Exception:
            pass

    # 5. Notification channel test
    print("\n[5/5] Testing notification delivery...")
    try:
        if bot.NTFY_TOPIC:
            bot.send_ntfy(
                "Preflight check",
                f"{len(accounts)} account(s) verified at {datetime.now().strftime('%H:%M')} — "
                "you should see booking notifications ~15 min from now.",
                priority="low",
                tags="airplane",
            )
            print("  OK: ntfy test sent")
        elif bot.SMTP_SERVER:
            bot.send_email(
                "Preflight check",
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
    print("\n" + "=" * 60)
    if failures:
        print(f"PREFLIGHT FAILED — {len(failures)} issue(s):")
        for f in failures:
            print(f"  FAIL: {f}")
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  WARN: {w}")
    if not failures and not warnings:
        print("PREFLIGHT OK — ready for 8:00 PM")
    print("=" * 60)

    if failures:
        try:
            import bot as bot_mod
            body = "FAILURES:\n" + "\n".join(f"- {f}" for f in failures)
            if warnings:
                body += "\n\nWARNINGS:\n" + "\n".join(f"- {w}" for w in warnings)
            body += "\n\nFix before 8:00 PM or the bot won't book anything tonight."
            bot_mod.notify(
                "PREFLIGHT FAILED",
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
        print(f"Preflight crashed: {e}")
        traceback.print_exc()
        try:
            import bot as bot_mod
            bot_mod.notify(
                "PREFLIGHT CRASHED",
                f"Preflight script itself crashed:\n{e}\n\n{traceback.format_exc()[:500]}",
                priority="urgent",
                tags="rotating_light",
            )
        except Exception:
            pass
        sys.exit(1)
