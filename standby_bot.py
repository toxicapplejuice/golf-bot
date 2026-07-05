#!/usr/bin/env python3
"""Standby booking bot — polls for cancellations between Monday rush runs.

Checks the standby watch queue every invocation, searching for available
tee times matching each active watch's preferences. Designed to run via
crontab every 15 minutes (Sun + Tue-Sat).

Usage:
    # Add a watch
    python3 standby_bot.py add --day saturday --day sunday --time morning
    python3 standby_bot.py add --day saturday --time afternoon --players 2

    # Run one check cycle
    python3 standby_bot.py check
    python3 standby_bot.py check --dry-run --headful

    # List watches
    python3 standby_bot.py list

    # Cancel a watch
    python3 standby_bot.py cancel abc123

Crontab (every 15 min, Sun + Tue-Sat):
    */15 * * * 0,2-6 /usr/bin/python3 -u /path/to/standby_bot.py check >> /path/to/standby.log 2>&1
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import standby_queue
from config import COURSE_CODES, FALLBACK_NUM_PLAYERS

from bot import (
    build_search_url,
    clear_live_screenshot,
    configure_account_context,
    extract_available_slots,
    attempt_booking_click,
    load_accounts,
    login_with_retry,
    navigate_to_search,
    notify,
    parse_time,
    save_debug_screenshot,
    update_live_screenshot,
    verify_booking_via_history,
)

try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth

    stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

from playwright.sync_api import sync_playwright


# ======================================================================
# Account selection
# ======================================================================


def pick_account(account_id: str | None) -> dict | None:
    """Pick the account to use for standby checks.

    Default: first enabled account in accounts.json, falling back to .env.
    """
    accounts = load_accounts()
    if account_id:
        for acc in accounts:
            if acc["id"] == account_id:
                return acc
        print(f"ERROR: account {account_id!r} not found in accounts.json")
        sys.exit(1)
    if accounts:
        return accounts[0]
    return None


# ======================================================================
# Search helpers
# ======================================================================


def _search_course(
    page,
    course_code: str,
    course_name: str,
    target_date: str,
    num_players: int,
    min_hour: int,
    max_hour: int,
    watch: dict,
    day: str,
    players_label: str,
    dry_run: bool,
) -> str:
    """Search one course for a slot. Returns 'booked', 'abort', or 'continue'."""
    url = build_search_url(course_code, target_date, num_players)
    if not navigate_to_search(page, url):
        print(f"    {course_name}: nav failed")
        return "continue"

    slots = extract_available_slots(
        page, course_code, course_name, target_date,
        num_players, max_hour, blacklist=set(),
    )
    if min_hour > 8:
        slots = [s for s in slots if parse_time(s["time"]) >= min_hour * 60]

    if not slots:
        print(f"    {course_name}: no slots")
        return "continue"

    print(f"    {course_name}: {len(slots)} slot(s) — "
          f"{', '.join(s['time'] for s in slots[:5])}")

    for slot in slots:
        print(f"    Trying {slot['time']} at {course_name}...", end=" ", flush=True)
        update_live_screenshot(page, f"standby: {slot['time']} at {course_name}")
        status = attempt_booking_click(page, slot, dry_run=dry_run)

        if status == "booked":
            print("BOOKED! — verifying...", end=" ", flush=True)
            if verify_booking_via_history(page, slot):
                print("VERIFIED")
                details = f"{slot['time']} at {course_name}{players_label}"
                standby_queue.mark_day_booked(watch["id"], day, details)
                notify(
                    f"Standby: {day.capitalize()} booked!",
                    f"{details} on {target_date}",
                    priority="high", tags="golf,white_check_mark",
                )
                return "booked"
            else:
                print("VERIFY FAILED — stopping for safety")
                save_debug_screenshot(page, f"standby_unverified_{day}")
                standby_queue.mark_day_booked(
                    watch["id"], day,
                    f"UNVERIFIED — {slot['time']} at {course_name}",
                )
                notify(
                    "Standby: possible booking needs manual check",
                    f"{day.capitalize()} {target_date}: {slot['time']} at "
                    f"{course_name} — reached confirmation but history "
                    f"didn't show it",
                    priority="urgent", tags="warning",
                )
                return "booked"

        if status == "dry_run":
            print("DRY-RUN OK")
            standby_queue.mark_day_booked(
                watch["id"], day,
                f"[DRY-RUN] {slot['time']} at {course_name}{players_label}",
            )
            return "booked"

        if status == "session_expired":
            print("session expired")
            if not login_with_retry(page, queue_mode="timeout"):
                return "abort"
            return "continue"

        print(status)
        if not navigate_to_search(page, url):
            return "continue"

    return "continue"


def _search_window(watch: dict) -> tuple[int, int]:
    """(min_hour, max_hour) for a watch: the time_pref window, optionally
    capped by the watch's max_hour (inclusive 24h hour)."""
    min_hour, max_hour = standby_queue.TIME_PREF_RANGES[watch["time_pref"]]
    cap = watch.get("max_hour")
    if cap is not None:
        max_hour = min(max_hour, cap)
    return min_hour, max_hour


def _player_counts(watch: dict) -> list[int]:
    """Player counts to try for a watch, in descending priority order.

    A watch with a min_players floor tries every count from players down
    to that floor and never below it (so "at least 3 people" can't fall
    back to a 2-person booking). Without a floor, legacy behavior: the
    requested count, then FALLBACK_NUM_PLAYERS if smaller.
    """
    players = watch["players"]
    floor = watch.get("min_players")
    if floor is not None:
        return list(range(players, floor - 1, -1))
    counts = [players]
    if FALLBACK_NUM_PLAYERS and FALLBACK_NUM_PLAYERS < players:
        counts.append(FALLBACK_NUM_PLAYERS)
    return counts


def _search_day(page, day: str, watch: dict, dry_run: bool) -> bool:
    """Search all courses for openings on one day. Returns True if booked."""
    target_date = watch["target_dates"][day]

    today = datetime.now()
    try:
        target_dt = datetime.strptime(target_date, "%m/%d/%Y")
        if target_dt.date() < today.date():
            print(f"  [{day}] {target_date} is in the past — skipping")
            return False
    except ValueError:
        return False

    min_hour, max_hour = _search_window(watch)
    print(f"  [{day}] Searching {watch['time_pref']} slots on {target_date} "
          f"({watch['players']}p, {min_hour}:00-{max_hour}:59)...")

    for num_players in _player_counts(watch):
        label = f" ({num_players}p)" if num_players != watch["players"] else ""
        if label:
            print(f"  [{day}] Retrying with {num_players} players...")

        for course_code, course_name in COURSE_CODES.items():
            result = _search_course(
                page, course_code, course_name, target_date,
                num_players, min_hour, max_hour, watch, day, label, dry_run,
            )
            if result == "booked":
                return True
            if result == "abort":
                return False

    return False


def check_watch(page, watch: dict, dry_run: bool = False) -> None:
    """Run one check cycle for a single watch."""
    for day in watch["days"]:
        result = watch.get("results", {}).get(day)
        if isinstance(result, dict) and result.get("booked"):
            continue
        _search_day(page, day, watch, dry_run)


# ======================================================================
# CLI commands
# ======================================================================


def run_check(args) -> None:
    """Run one check cycle across all active watches."""
    print(f"\n{'=' * 50}")
    print(f"Standby Check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 50}")

    expired = standby_queue.expire_stale_watches()
    if expired:
        print(f"Expired {expired} stale watch(es)")

    standby_queue.clear_old_watches()

    watches = standby_queue.get_active_watches()
    if not watches:
        print("No active standby watches — nothing to check")
        return

    print(f"Active watches: {len(watches)}")
    for w in watches:
        days = ", ".join(w["days"])
        print(f"  [{w['id']}] {days} {w['time_pref']} ({w['players']}p) "
              f"— checked {w.get('check_count', 0)}x")

    account = pick_account(getattr(args, "account_id", None))
    configure_account_context(account["id"] if account else None)

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=not args.headful)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        stealth_sync(page)

        try:
            if not login_with_retry(page, queue_mode="timeout"):
                print("Login failed — aborting check")
                return

            for watch in watches:
                print(f"\n--- Watch [{watch['id']}] ---")
                standby_queue.update_watch_check(watch["id"])
                check_watch(page, watch, dry_run=args.dry_run)
        finally:
            try:
                browser.close()
            except Exception:
                pass
            clear_live_screenshot()

    print(f"\nCheck complete at {datetime.now().strftime('%H:%M:%S')}")


def cmd_add(args) -> None:
    """Add a new standby watch."""
    watch = standby_queue.add_watch(
        days=args.day,
        time_pref=args.time,
        players=args.players,
        min_players=args.min_players,
        max_hour=args.max_hour,
    )
    min_hour, max_hour = _search_window(watch)
    print(f"Watch added: {watch['id']}")
    print(f"  Days: {', '.join(watch['days'])}")
    print(f"  Time: {watch['time_pref']} ({min_hour}:00-{max_hour}:59)")
    floor = f" (min {watch['min_players']})" if watch.get("min_players") else ""
    print(f"  Players: {watch['players']}{floor}")
    print(f"  Dates: {', '.join(f'{d}={v}' for d, v in watch['target_dates'].items())}")
    print(f"  Expires: {watch['expires_at']}")


def cmd_list(args) -> None:
    """List all watches."""
    standby_queue.expire_stale_watches()
    watches = standby_queue.list_watches()
    if not watches:
        print("No watches")
        return
    for w in watches:
        status = w["status"].upper()
        days = ", ".join(w["days"])
        checked = w.get("check_count", 0)
        last = w.get("last_checked_at") or "never"
        print(f"[{w['id']}] {status} — {days} {w['time_pref']} ({w['players']}p)")
        print(f"  Checked {checked}x | Last: {last} | Expires: {w['expires_at']}")
        for d in w["days"]:
            r = w.get("results", {}).get(d)
            if isinstance(r, dict) and r.get("booked"):
                print(f"  {d}: {r['details']}")
            else:
                print(f"  {d}: pending")


def cmd_cancel(args) -> None:
    """Cancel a watch."""
    if standby_queue.cancel_watch(args.id):
        print(f"Watch {args.id} cancelled")
    else:
        print(f"Watch {args.id} not found or not active")


def main():
    parser = argparse.ArgumentParser(description="Standby Tee Time Watch Bot")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Run one check cycle")
    p_check.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_check.add_argument("--headful", action="store_true")
    p_check.add_argument("--account-id", dest="account_id", default=None)

    p_add = sub.add_parser("add", help="Add a standby watch")
    p_add.add_argument("--day", action="append", required=True,
                       choices=["saturday", "sunday"])
    p_add.add_argument("--time", default="morning",
                       choices=["morning", "afternoon", "all"])
    p_add.add_argument("--players", type=int, default=4)
    p_add.add_argument("--min-players", dest="min_players", type=int,
                       default=None,
                       help="Floor for the player-count fallback: try every "
                            "count from --players down to this, never below")
    p_add.add_argument("--max-hour", dest="max_hour", type=int, default=None,
                       help="Cap the end of the time window (24h inclusive "
                            "hour): morning + --max-hour 11 = 8am-11:59am")

    sub.add_parser("list", help="List all watches")

    p_cancel = sub.add_parser("cancel", help="Cancel a watch")
    p_cancel.add_argument("id", help="Watch ID to cancel")

    args = parser.parse_args()

    if args.command == "check":
        run_check(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "cancel":
        cmd_cancel(args)


if __name__ == "__main__":
    main()
