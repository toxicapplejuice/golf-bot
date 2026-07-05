#!/usr/bin/env python3
"""One-off same-day tee time watcher.

Polls WebTrac every N minutes for open tee times inside a minute-precise
window on a single date and, by default, books the first one found. Built
for ad-hoc "find me a slot TONIGHT 5:30-6:30" runs. For weekend
cancellation polling use standby_bot.py (cron-driven, hour-granular).

Usage:
    # Auto-book watch: today 5:30-6:30 PM, 2 players, check every 15 min.
    # caffeinate -i keeps the Mac from idle-sleeping while it runs.
    nohup caffeinate -i /usr/bin/python3 -u today_watch.py \
        --start "5:30 PM" --end "6:30 PM" --players 2 \
        >> today_watch.log 2>&1 &

    # One immediate check that only reports — safe smoke test
    python3 today_watch.py --once --mode notify

    # Walk the booking flow but abort at the cart
    python3 today_watch.py --once --dry-run --headful

Grep-able log markers: BOOKED / FOUND / DRY-RUN OK / NEEDS MANUAL CHECK /
LOGIN FAILED / CYCLE ERROR / WATCH ENDED / FATAL
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import date, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from config import COURSE_CODES

from bot import (
    attempt_booking_click,
    build_search_url,
    clear_live_screenshot,
    configure_account_context,
    extract_available_slots,
    login_with_retry,
    navigate_to_search,
    notify,
    parse_time,
    save_debug_screenshot,
    update_live_screenshot,
    verify_booking_via_history,
)
from standby_bot import pick_account

try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth

    stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

from playwright.sync_api import sync_playwright


# ======================================================================
# Pure helpers (covered by tests/test_today_watch.py)
# ======================================================================


def parse_clock(label: str, time_str: str) -> int:
    """Parse one 'H:MM AM/PM' value to minutes since midnight.

    Raises ValueError naming the offending flag so startup dies loudly.
    """
    minutes = parse_time(time_str)
    if minutes == 9999:
        raise ValueError(f"Unparseable {label} time: {time_str!r}")
    return minutes


def parse_window(start_str: str, end_str: str) -> tuple[int, int]:
    """Parse 'H:MM AM/PM' bounds into (start, end) minutes since midnight.

    Raises ValueError on unparseable times or an empty/inverted window.
    """
    start = parse_clock("--start", start_str)
    end = parse_clock("--end", end_str)
    if start >= end:
        raise ValueError(
            f"Window start {start_str!r} must be before end {end_str!r}"
        )
    return start, end


def slots_in_window(slots: list[dict], start_min: int, end_min: int) -> list[dict]:
    """Keep slots whose tee time is inside [start_min, end_min] inclusive.

    Slots with unparseable times (parse_time -> 9999) are dropped.
    """
    return [s for s in slots if start_min <= parse_time(s["time"]) <= end_min]


def preference_key(prefer_min: int):
    """Sort key: distance from prefer_min, then earlier time on ties."""
    return lambda s: (abs(parse_time(s["time"]) - prefer_min),
                      parse_time(s["time"]))


def sort_by_preference(slots: list[dict], prefer_min: int) -> list[dict]:
    """Order slots by closeness to prefer_min; ties go to the earlier time."""
    return sorted(slots, key=preference_key(prefer_min))


def date_relation(date_str: str, today: date | None = None) -> str:
    """Where M/D/YYYY date_str sits relative to today: 'past'|'today'|'future'.

    Padding-agnostic ('6/11/2026' == '06/11/2026'). Raises ValueError on a
    malformed date so the watch dies loudly at startup instead of polling
    for a day that can never match.
    """
    today = today or datetime.now().date()
    target = datetime.strptime(date_str, "%m/%d/%Y").date()
    if target < today:
        return "past"
    if target == today:
        return "today"
    return "future"


def parse_holes(holes_str: str) -> list[int]:
    """'18,9' -> [18, 9]. Entries must be 9 or 18; dedupes, keeps order."""
    out: list[int] = []
    for part in holes_str.split(","):
        part = part.strip()
        if not part:
            continue
        if part not in ("9", "18"):
            raise ValueError(f"--holes entries must be 9 or 18, got {part!r}")
        n = int(part)
        if n not in out:
            out.append(n)
    if not out:
        raise ValueError("--holes must contain at least one of 9, 18")
    return out


# ======================================================================
# One check cycle
# ======================================================================


def _try_slots(page, slots, url, args, tried) -> str | None:
    """Attempt slots in order. Returns a terminal status ('booked',
    'dry_run', 'needs_manual_check') or None to keep searching."""
    note = f"\n\n{args.success_note}" if args.success_note else ""
    for slot in slots:
        print(f"    Trying {slot['time']} at {slot['course']}...", flush=True)
        update_live_screenshot(page, f"today_watch: {slot['time']} at {slot['course']}")
        status = attempt_booking_click(page, slot, dry_run=args.dry_run)
        details = (f"{slot['time']} at {slot['course']} on {slot['date']} "
                   f"({args.players} players)")

        if status == "booked":
            print("    confirmation seen — verifying in history...", flush=True)
            if verify_booking_via_history(page, slot):
                print(f"BOOKED: {details}")
                notify("Tee time booked!", details + note,
                       priority="high", tags="golf,white_check_mark")
                return "booked"
            print(f"NEEDS MANUAL CHECK: {details} — reached confirmation "
                  "but reservation history doesn't show it")
            save_debug_screenshot(page, "today_watch_unverified")
            notify("Booking needs manual check",
                   f"{details}: reached confirmation but history didn't show "
                   "it. Check WebTrac email/history before assuming either "
                   f"way.{note}",
                   priority="urgent", tags="warning")
            return "needs_manual_check"

        if status == "dry_run":
            print(f"DRY-RUN OK: would have booked {details}")
            notify("Tee time watch dry-run",
                   f"Would have booked {details}.{note}")
            return "dry_run"

        if status == "unverified_post_click":
            # The booking MAY have committed at the site. Never try another
            # slot after this — stop the whole watch and flag for a human.
            print(f"NEEDS MANUAL CHECK: {details} — ambiguous post-checkout "
                  "state, stopping watch")
            notify("Booking needs manual check",
                   f"{details}: checkout ended in an ambiguous state and may "
                   f"have committed. Check WebTrac email/history.{note}",
                   priority="urgent", tags="warning")
            return "needs_manual_check"

        if status == "session_expired":
            print("    session expired — re-logging in", flush=True)
            if not login_with_retry(page, queue_mode="timeout"):
                return None  # cycle over; next cycle starts a fresh login
            if not navigate_to_search(page, url):
                return None
            continue

        # 'taken' / 'failed' — phantom or stale row; don't retry it this cycle
        print(f"    {status}")
        tried.add((slot["date"], slot["course"], slot["time"]))
        if not navigate_to_search(page, url):
            return None
    return None


def run_cycle(args, start_min, end_min, end_hour, notified: set) -> str:
    """One full check across all hole-counts and courses.

    Book mode is greedy by default: first in-window slot at the highest-
    priority course wins (fastest in a race). With --prefer, all courses
    are scanned first and slots are attempted globally ordered by
    closeness to the preferred time.

    Returns 'stop' (terminal outcome — booked/dry-run/manual-check),
    'continue', or 'login_failed'.
    """
    found_new: list[str] = []
    tried: set = set()  # per-cycle phantom blacklist: (date, course, time)
    candidates: list[tuple[dict, str]] = []  # (slot, search url) for --prefer

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=not args.headful)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            stealth_sync(page)

            if not login_with_retry(page, queue_mode="timeout"):
                print("LOGIN FAILED — will retry next cycle")
                return "login_failed"

            for holes in args.holes:
                for code, name in COURSE_CODES.items():
                    url = build_search_url(code, args.date, args.players,
                                           holes=holes)
                    if not navigate_to_search(page, url):
                        print(f"  {name} ({holes}h): nav failed")
                        continue
                    slots = extract_available_slots(
                        page, code, name, args.date,
                        args.players, end_hour, blacklist=tried,
                    )
                    slots = slots_in_window(slots, start_min, end_min)
                    if not slots:
                        print(f"  {name} ({holes}h): no slots in window")
                        continue

                    times = ", ".join(s["time"] for s in slots)
                    print(f"  {name} ({holes}h): {len(slots)} slot(s) in "
                          f"window — {times}")

                    if args.mode == "notify":
                        for s in slots:
                            key = (s["date"], s["course"], s["time"], holes)
                            if key not in notified:
                                notified.add(key)
                                found_new.append(
                                    f"{s['time']} at {s['course']} ({holes}h)")
                        continue

                    if args.prefer_min is not None:
                        candidates.extend((s, url) for s in slots)
                        continue

                    outcome = _try_slots(page, slots, url, args, tried)
                    if outcome:
                        return "stop"

            if candidates:
                candidates.sort(
                    key=lambda c: preference_key(args.prefer_min)(c[0]))
                order = ", ".join(f"{s['time']} {s['course']}"
                                  for s, _ in candidates)
                print(f"  attempt order (closest to {args.prefer} first): "
                      f"{order}")
                for slot, url in candidates:
                    if (slot["date"], slot["course"], slot["time"]) in tried:
                        continue  # already failed under the other hole count
                    if not navigate_to_search(page, url):
                        continue
                    outcome = _try_slots(page, [slot], url, args, tried)
                    if outcome:
                        return "stop"
        finally:
            try:
                browser.close()
            except Exception:
                pass
            clear_live_screenshot()

    if found_new:
        listing = "; ".join(found_new)
        print(f"FOUND: {listing}")
        notify("Tee times open in your window!",
               f"{args.date} {args.start}-{args.end}: {listing}. "
               "Book it yourself — this watch is notify-only.",
               priority="high", tags="golf")
    return "continue"


# ======================================================================
# Main loop
# ======================================================================


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Same-day tee time watcher (minute-precise window)")
    ap.add_argument("--date", default=datetime.now().strftime("%-m/%d/%Y"),
                    help="M/D/YYYY (default today). Watch ends once this "
                         "date is past, or past --end on the day itself.")
    ap.add_argument("--start", default="5:30 PM",
                    help="window start, e.g. '5:30 PM'")
    ap.add_argument("--end", default="6:30 PM",
                    help="window end (inclusive), e.g. '6:30 PM'")
    ap.add_argument("--players", type=int, default=2)
    ap.add_argument("--interval-min", type=float, default=15,
                    help="minutes between cycle starts (default 15)")
    ap.add_argument("--mode", choices=["book", "notify"], default="book",
                    help="book = auto-book first slot and stop; "
                         "notify = push alert and keep watching")
    ap.add_argument("--holes", default="18,9",
                    help="comma list of hole counts to search, in order "
                         "(default '18,9' — evening slots are often 9-hole)")
    ap.add_argument("--prefer", default=None,
                    help="optional 'H:MM AM/PM' target time; in book mode, "
                         "scans ALL courses each cycle and attempts slots "
                         "closest to this time first (ties go earlier). "
                         "Default: greedy course-priority, earliest first")
    ap.add_argument("--success-note", dest="success_note", default=None,
                    help="extra text appended to booked/dry-run/manual-check "
                         "notifications (e.g. a reminder to cancel a held "
                         "slot)")
    ap.add_argument("--account-id", default=None,
                    help="accounts.json id (default: first enabled account)")
    ap.add_argument("--once", action="store_true",
                    help="run a single cycle and exit")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="walk the booking flow but abort at the cart")
    ap.add_argument("--headful", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    start_min, end_min = parse_window(args.start, args.end)
    end_hour = end_min // 60
    args.holes = parse_holes(args.holes)
    args.prefer_min = parse_clock("--prefer", args.prefer) if args.prefer else None
    date_relation(args.date)  # validate format up front; dies loudly if bad

    account = pick_account(args.account_id)
    active = configure_account_context(account["id"] if account else None)

    print("=" * 56)
    print(f"Today Watch — started {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  date={args.date}  window={args.start}-{args.end}  "
          f"players={args.players}  holes={args.holes}")
    print(f"  mode={args.mode}{' (dry-run)' if args.dry_run else ''}  "
          f"prefer={args.prefer or '-'}  interval={args.interval_min:g}m  "
          f"account={active['id']}")
    print("=" * 56, flush=True)

    notified: set = set()
    fail_streak = 0
    cycle = 0

    while True:
        now = datetime.now()
        relation = date_relation(args.date)
        if relation == "past":
            print(f"WATCH ENDED — {args.date} has passed, nothing booked")
            notify("Tee time watch ended",
                   f"No booking made for {args.date} {args.start}-{args.end}.",
                   priority="low")
            return 0
        if relation == "today" and now.hour * 60 + now.minute > end_min:
            print(f"WATCH ENDED — past {args.end}, nothing booked")
            notify("Tee time watch ended",
                   f"No {args.start}-{args.end} slot opened up today "
                   f"({args.date}).",
                   priority="low")
            return 0

        cycle += 1
        print(f"\n--- Cycle {cycle} @ {now:%H:%M:%S} ---", flush=True)
        cycle_t0 = time.time()
        try:
            result = run_cycle(args, start_min, end_min, end_hour, notified)
        except Exception:
            print("CYCLE ERROR:")
            traceback.print_exc()
            result = "error"

        if result == "stop":
            return 0

        fail_streak = fail_streak + 1 if result in ("error", "login_failed") else 0
        if fail_streak == 3:
            notify("Tee time watch struggling",
                   "3 consecutive failed check cycles (see today_watch.log). "
                   f"Still retrying every {args.interval_min:g}m.",
                   priority="high", tags="warning")

        if args.once:
            print("Done (--once)")
            return 0

        sleep_s = max(30.0, args.interval_min * 60 - (time.time() - cycle_t0))
        nxt = datetime.fromtimestamp(time.time() + sleep_s)
        print(f"  next check ~{nxt:%H:%M:%S}", flush=True)
        time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        print("FATAL:")
        traceback.print_exc()
        sys.exit(1)
