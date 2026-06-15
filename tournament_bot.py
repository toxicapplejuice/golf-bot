#!/usr/bin/env python3
"""Tournament booker — grab N consecutive tee times under N accounts.

A weekend tournament wants several groups going off back-to-back at the same
course. WebTrac caps each account at one tee time per day, so 3 consecutive
Saturday slots require 3 separate accounts — one slot each. This script drives
all of them from a single process (one Playwright browser, one isolated
context per account) and books them as a tight block.

It reuses bot.py's battle-tested primitives (login, Queue-it-safe navigation,
slot extraction, the add-to-cart + checkout click, history verification) — the
only new logic is finding a consecutive block and orchestrating the accounts.

Design notes:
  - Booking is SEQUENTIAL across accounts (michael -> grant -> christian),
    never concurrent, so two sessions are never open on one account at once.
  - On partial failure (a slot snatched mid-rush) it AUTO-FILLS: re-scans the
    course and books the open slot nearest the block for the next account. It
    NEVER cancels (cancellation is irreversible).
  - Sunday is independent (the per-day cap is per day), so each warm session
    also grabs a regular Sunday slot by reusing bot.try_book_day().

Usage:
    # Real run — launch ~7:45 PM; it warms up the sessions then waits for 8 PM.
    nohup caffeinate -i /usr/bin/python3 -u tournament_bot.py \
        >> tournament.log 2>&1 &

    # Plumbing smoke test (no 8 PM wait, aborts before checkout). Saturday isn't
    # open until 8 PM, so point --date at a currently-bookable date to also
    # exercise block-finding + the dry-run cart abort.
    python3 tournament_bot.py --now --dry-run --headful --date 6/17/2026

    # Only the 3 Saturday slots, no Sunday booking
    python3 tournament_bot.py --no-sunday

Grep-able log markers: BOOKED / DRY-RUN OK / NEEDS MANUAL CHECK / NO BLOCK /
LOGIN FAILED / TOURNAMENT RESULT / FATAL
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from config import COURSE_CODES, FALLBACK_NUM_PLAYERS

from bot import (
    attempt_booking_click,
    build_search_url,
    clear_live_screenshot,
    configure_account_context,
    extract_available_slots,
    get_next_weekend_dates,
    is_authenticated,
    load_accounts,
    login_with_retry,
    navigate_to_search,
    notify,
    parse_time,
    save_debug_screenshot,
    send_ntfy,
    try_book_day,
    update_live_screenshot,
    verify_booking_via_history,
    wait_until_release_time,
)

try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth

    stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

from playwright.sync_api import sync_playwright

# Tournament defaults: the two least-contested courses, biggest odds of a clean
# back-to-back block at the 8 PM rush.
DEFAULT_COURSES = "Roy Kizer,Jimmy Clay"
TOURNAMENT_SIZE = 3  # consecutive slots wanted (== number of accounts)

# Per account, how many open slots to try before giving up on that account.
# Realizes the 4A auto-fill: a "taken" slot just advances to the next-nearest.
MAX_FILL_ATTEMPTS = 4


# ======================================================================
# Pure helpers (covered by tests/test_tournament.py)
# ======================================================================

# Self-contained copies of the same-day watcher's window helpers, kept local so
# this module has no cross-script import dependency. Mirrors today_watch.py.
def parse_clock(label: str, time_str: str) -> int:
    """Parse one 'H:MM AM/PM' value to minutes since midnight. Raises ValueError
    naming the offending flag so a bad --start/--end/--prefer dies at startup."""
    minutes = parse_time(time_str)
    if minutes == 9999:
        raise ValueError(f"Unparseable {label} time: {time_str!r}")
    return minutes


def parse_window(start_str: str, end_str: str) -> tuple[int, int]:
    """Parse 'H:MM AM/PM' bounds into (start, end) minutes. Raises ValueError on
    unparseable times or an empty/inverted window."""
    start = parse_clock("--start", start_str)
    end = parse_clock("--end", end_str)
    if start >= end:
        raise ValueError(f"Window start {start_str!r} must be before end {end_str!r}")
    return start, end


def slots_in_window(slots: list[dict], start_min: int, end_min: int) -> list[dict]:
    """Keep slots whose tee time is inside [start_min, end_min] inclusive.
    Unparseable times (parse_time -> 9999) are dropped."""
    return [s for s in slots if start_min <= parse_time(s["time"]) <= end_min]


def parse_courses(courses_str: str) -> list[tuple[str, str]]:
    """'Roy Kizer,Jimmy Clay' -> [('2','Roy Kizer'), ('1','Jimmy Clay')].

    Matches names case-insensitively against COURSE_CODES, preserves the
    given order (= search priority), dedupes. Raises ValueError naming an
    unknown course so a typo dies at startup instead of silently searching
    nothing.
    """
    name_to_code = {name.lower(): code for code, name in COURSE_CODES.items()}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in courses_str.split(","):
        name = part.strip()
        if not name:
            continue
        code = name_to_code.get(name.lower())
        if code is None:
            valid = ", ".join(COURSE_CODES.values())
            raise ValueError(f"Unknown course {name!r}. Valid: {valid}")
        if code in seen:
            continue
        seen.add(code)
        out.append((code, COURSE_CODES[code]))
    if not out:
        raise ValueError("--courses must name at least one course")
    return out


def _sorted_by_time(slots: list[dict]) -> list[dict]:
    """Slots ascending by clock time; unparseable times (9999) sort last."""
    return sorted(slots, key=lambda s: parse_time(s["time"]))


def find_consecutive_block(slots: list[dict], n: int = TOURNAMENT_SIZE,
                           max_gap_min: int = 10) -> list[dict] | None:
    """Earliest run of n time-sorted slots where each adjacent pair is
    <= max_gap_min apart.

    WebTrac tee intervals are ~8-10 min, so two available slots within
    max_gap_min are almost certainly adjacent tee times — the right proxy for
    "in a row" given we only see *available* rows. Returns the block (sorted by
    time) or None if no clean run of length n exists.
    """
    if n <= 0:
        return None
    s = _sorted_by_time(slots)
    if len(s) < n:
        return None
    for i in range(len(s) - n + 1):
        window = s[i:i + n]
        if all(parse_time(window[j + 1]["time"]) - parse_time(window[j]["time"])
               <= max_gap_min for j in range(n - 1)):
            return window
    return None


def find_tightest_block(slots: list[dict], n: int = TOURNAMENT_SIZE,
                        max_spread_min: int = 30) -> list[dict] | None:
    """Fallback when no clean adjacency exists: the n time-sorted slots with the
    smallest total span, as long as that span is <= max_spread_min.

    Ties (equal span) go to the earlier block. Returns the block or None.
    """
    if n <= 0:
        return None
    s = _sorted_by_time(slots)
    if len(s) < n:
        return None
    best: list[dict] | None = None
    best_spread = None
    for i in range(len(s) - n + 1):
        window = s[i:i + n]
        spread = parse_time(window[-1]["time"]) - parse_time(window[0]["time"])
        if spread <= max_spread_min and (best_spread is None or spread < best_spread):
            best, best_spread = window, spread
    return best


def choose_block(course_slots: dict[tuple[str, str], list[dict]],
                 n: int = TOURNAMENT_SIZE, max_gap_min: int = 10,
                 max_spread_min: int = 30,
                 prefer_min: int | None = None) -> dict | None:
    """Pick the best block across courses.

    Preference order: a clean consecutive block beats a merely-tight one; within
    a tier, rank by closeness to prefer_min (or earliest start when prefer_min is
    None), then by earliest start. If no course yields n slots, fall back to the
    best PARTIAL (the course with the most available slots, earliest).

    Returns {course_code, course_name, block, kind} where kind is
    'consecutive' | 'tight' | 'partial', or None if nothing is bookable.
    """
    def start_min(block: list[dict]) -> int:
        return parse_time(block[0]["time"])

    def rank(block: list[dict], tier: int) -> tuple:
        start = start_min(block)
        closeness = abs(start - prefer_min) if prefer_min is not None else start
        return (tier, closeness, start)

    candidates: list[tuple[tuple, str, str, list[dict], str]] = []
    for (code, name), slots in course_slots.items():
        block = find_consecutive_block(slots, n, max_gap_min)
        if block:
            candidates.append((rank(block, 0), code, name, block, "consecutive"))
            continue
        block = find_tightest_block(slots, n, max_spread_min)
        if block:
            candidates.append((rank(block, 1), code, name, block, "tight"))
    if candidates:
        candidates.sort(key=lambda c: c[0])
        _, code, name, block, kind = candidates[0]
        return {"course_code": code, "course_name": name, "block": block, "kind": kind}

    # Partial fallback — book what little there is and flag it.
    best: dict | None = None
    for (code, name), slots in course_slots.items():
        block = _sorted_by_time(slots)[:n]
        if not block:
            continue
        if (best is None
                or len(block) > len(best["block"])
                or (len(block) == len(best["block"])
                    and parse_time(block[0]["time"]) < parse_time(best["block"][0]["time"]))):
            best = {"course_code": code, "course_name": name, "block": block, "kind": "partial"}
    return best


def pick_nearest_slot(slots: list[dict], anchor_min: int,
                      used_times: set[str]) -> dict | None:
    """The available slot closest to anchor_min (ties -> earlier time), skipping
    any time already used. Drives the auto-fill: each account clusters around the
    block even when its first-choice slot was taken. None if nothing is left.
    """
    pool = [s for s in slots if s["time"] not in used_times]
    if not pool:
        return None
    return min(pool, key=lambda s: (abs(parse_time(s["time"]) - anchor_min),
                                    parse_time(s["time"])))


# ======================================================================
# Account / session setup
# ======================================================================

def resolve_accounts(spec: str | None) -> list[dict]:
    """All enabled accounts, or just the ids in `spec` (comma list) in order."""
    accounts = load_accounts()
    if not spec:
        return accounts
    by_id = {a["id"]: a for a in accounts}
    chosen: list[dict] = []
    for raw in spec.split(","):
        aid = raw.strip()
        if not aid:
            continue
        if aid not in by_id:
            valid = ", ".join(by_id) or "(none enabled)"
            raise SystemExit(f"Account id {aid!r} not in accounts.json. Have: {valid}")
        chosen.append(by_id[aid])
    return chosen


def warm_up_sessions(browser, accounts: list[dict],
                     announce: bool = True) -> list[dict]:
    """Log each account into its own isolated context. Returns a session list
    [{acct, context, page, authed}]. Done before the 8 PM wait so every account
    is through Queue-it and ready to book the instant slots drop.

    `announce` pushes a per-account "ready" ntfy (useful for the real scheduled
    run; suppressed on immediate --now test runs to avoid noise).
    """
    sessions: list[dict] = []
    for acct in accounts:
        configure_account_context(acct["id"])
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        stealth_sync(page)
        print(f"  warming up {acct['display_name']}...", flush=True)
        authed = login_with_retry(page, queue_mode="timeout")
        if authed and announce:
            send_ntfy(f"[Tournament] {acct['display_name']} ready",
                      "Through login + Queue-it.", priority="low",
                      tags="white_check_mark")
        elif not authed:
            print(f"  LOGIN FAILED: {acct['display_name']}", flush=True)
        sessions.append({"acct": acct, "context": context, "page": page,
                         "authed": authed})
    return sessions


def reverify_sessions(sessions: list[dict]) -> None:
    """After the pre-release wait, re-auth any session that isn't logged in
    (dropped during the idle wait, or never made it through warm-up)."""
    for s in sessions:
        configure_account_context(s["acct"]["id"])
        if is_authenticated(s["page"]):
            s["authed"] = True
            continue
        print(f"  re-authenticating {s['acct']['display_name']}...", flush=True)
        s["authed"] = login_with_retry(s["page"], queue_mode="timeout")


# ======================================================================
# Saturday: scan + book the consecutive block
# ======================================================================

def scan_courses(page, courses: list[tuple[str, str]], date: str, players: int,
                 holes: int, max_hour: int, start_min: int,
                 end_min: int) -> dict[tuple[str, str], list[dict]]:
    """Extract in-window available slots for each course (one scout session)."""
    out: dict[tuple[str, str], list[dict]] = {}
    for code, name in courses:
        url = build_search_url(code, date, players, holes=holes)
        if not navigate_to_search(page, url):
            print(f"  scan {name}: nav failed", flush=True)
            out[(code, name)] = []
            continue
        slots = extract_available_slots(page, code, name, date, players,
                                        max_hour, blacklist=set())
        slots = slots_in_window(slots, start_min, end_min)
        out[(code, name)] = slots
        print(f"  scan {name}: {', '.join(s['time'] for s in slots) or 'none'}",
              flush=True)
    return out


def _book_one(page, slot: dict, acct: dict, dry_run: bool) -> str:
    """Click + checkout a single slot, returning a normalized status:
    'booked' | 'dry_run' | 'needs_manual_check' | 'taken' | 'failed' |
    'session_expired'. Mirrors today_watch._try_slots' interpretation.
    """
    details = f"{slot['time']} at {slot['course']} on {slot['date']}"
    update_live_screenshot(
        page, f"tournament: {acct['display_name']} -> {slot['time']} {slot['course']}")
    status = attempt_booking_click(page, slot, dry_run=dry_run)

    if status == "booked":
        if verify_booking_via_history(page, slot):
            print(f"BOOKED: [{acct['id']}] {details}", flush=True)
            return "booked"
        print(f"NEEDS MANUAL CHECK: [{acct['id']}] {details} — confirmation seen "
              "but not found in history", flush=True)
        save_debug_screenshot(page, f"tournament_unverified_{acct['id']}")
        return "needs_manual_check"
    if status == "dry_run":
        print(f"DRY-RUN OK: [{acct['id']}] would book {details}", flush=True)
        return "dry_run"
    if status == "unverified_post_click":
        print(f"NEEDS MANUAL CHECK: [{acct['id']}] {details} — ambiguous "
              "post-checkout state", flush=True)
        save_debug_screenshot(page, f"tournament_ambiguous_{acct['id']}")
        return "needs_manual_check"
    # taken / failed / session_expired
    print(f"  [{acct['id']}] {slot['time']}: {status}", flush=True)
    return status


def book_block(sessions: list[dict], chosen: dict, date: str, players: int,
               holes: int, max_hour: int, start_min: int, end_min: int,
               dry_run: bool) -> list[dict]:
    """Book one slot per authed account at the chosen course, clustered around
    the block. Auto-fills around taken slots; never cancels. Returns a result
    per session: {acct, slot, status}.
    """
    code = chosen["course_code"]
    name = chosen["course_name"]
    url = build_search_url(code, date, players, holes=holes)
    anchor = parse_time(chosen["block"][0]["time"])  # block start; tightens to
    #                                                  the first booked time
    used_times: set[str] = set()
    booked_any = False
    results: list[dict] = []

    for s in sessions:
        acct = s["acct"]
        if not s["authed"]:
            results.append({"acct": acct, "slot": None, "status": "login_failed"})
            continue

        page = s["page"]
        configure_account_context(acct["id"])
        outcome: dict | None = None
        tried_times: set[str] = set()

        for _ in range(MAX_FILL_ATTEMPTS):
            if not navigate_to_search(page, url):
                continue
            slots = extract_available_slots(page, code, name, date, players,
                                            max_hour, blacklist=set())
            slots = slots_in_window(slots, start_min, end_min)
            target = pick_nearest_slot(slots, anchor, used_times | tried_times)
            if target is None:
                outcome = {"acct": acct, "slot": None, "status": "no_slot"}
                break

            status = _book_one(page, target, acct, dry_run)
            if status in ("booked", "dry_run"):
                used_times.add(target["time"])
                if not booked_any:  # cluster the rest around the first win
                    anchor = parse_time(target["time"])
                    booked_any = True
                outcome = {"acct": acct, "slot": target, "status": status}
                break
            if status == "needs_manual_check":
                used_times.add(target["time"])  # may have committed — don't reuse
                outcome = {"acct": acct, "slot": target, "status": status}
                break
            # taken / failed / session_expired: skip this slot, try next-nearest
            tried_times.add(target["time"])
            if status == "session_expired":
                login_with_retry(page, queue_mode="timeout")

        results.append(outcome or {"acct": acct, "slot": None, "status": "exhausted"})

    return results


# ======================================================================
# Sunday: one regular slot per account (reuses the normal booking path)
# ======================================================================

def book_sunday(sessions: list[dict], sunday_date: str, players: int,
                dry_run: bool) -> list[dict]:
    """Grab each authed account a regular Sunday slot via try_book_day(). With
    weekend=None this skips all shared-state coordination — a plain single-day
    booking per session. Light course diversity by excluding the prior pick.
    """
    results: list[dict] = []
    exclude: str | None = None
    for s in sessions:
        acct = s["acct"]
        if not s["authed"]:
            results.append({"acct": acct,
                            "result": {"success": False, "details": None, "skipped": True}})
            continue
        page = s["page"]
        configure_account_context(acct["id"])
        blacklist: set = set()
        res = try_book_day(page, sunday_date, "sunday", players, blacklist,
                           exclude_course=exclude, dry_run=dry_run, weekend=None)
        if (not res.get("success") and not res.get("halt_day")
                and FALLBACK_NUM_PLAYERS and FALLBACK_NUM_PLAYERS < players):
            res = try_book_day(page, sunday_date, "sunday", FALLBACK_NUM_PLAYERS,
                               blacklist, exclude_course=exclude, dry_run=dry_run,
                               weekend=None)
        if res.get("success") and isinstance(res.get("course"), str):
            exclude = res["course"]
            send_ntfy(f"[{acct['display_name']}] Sunday booked",
                      res.get("details") or "", priority="default",
                      tags="golf,white_check_mark")
        results.append({"acct": acct, "result": res})
    return results


# ======================================================================
# Reporting
# ======================================================================

def report(chosen: dict | None, sat_results: list[dict], sun_results: list[dict],
           date: str, dry_run: bool) -> None:
    """Print + push one summary covering the Saturday block and Sunday."""
    lines: list[str] = []
    booked = [r for r in sat_results if r["status"] in ("booked", "dry_run")]
    manual = [r for r in sat_results if r["status"] == "needs_manual_check"]

    if chosen:
        lines.append(f"Saturday {date} — {chosen['course_name']} ({chosen['kind']}):")
    else:
        lines.append(f"Saturday {date} — NO BLOCK found")
    for r in sat_results:
        who = r["acct"]["display_name"]
        if r["slot"]:
            lines.append(f"  {who}: {r['slot']['time']} [{r['status']}]")
        else:
            lines.append(f"  {who}: — [{r['status']}]")

    if sun_results:
        lines.append("Sunday:")
        for r in sun_results:
            who = r["acct"]["display_name"]
            res = r["result"]
            if res.get("success"):
                lines.append(f"  {who}: {res.get('details')}")
            elif res.get("skipped"):
                lines.append(f"  {who}: skipped (not logged in)")
            else:
                lines.append(f"  {who}: no slot")

    body = "\n".join(lines)
    print("\nTOURNAMENT RESULT:\n" + body, flush=True)

    want = len(sat_results)
    got = len(booked)
    if manual:
        title = f"Tournament: {got}/{want} booked, {len(manual)} need manual check"
        priority, tags = "urgent", "golf,warning"
    elif got == want and want > 0:
        title = f"Tournament: all {got} Saturday slots {'(dry-run) ' if dry_run else ''}booked!"
        priority, tags = "high", "golf,white_check_mark"
    elif got > 0:
        title = f"Tournament: {got}/{want} Saturday slots booked"
        priority, tags = "high", "golf,warning"
    else:
        title = "Tournament: NO Saturday slots booked"
        priority, tags = "high", "golf,x"
    notify(title, body, priority=priority, tags=tags)


# ======================================================================
# CLI
# ======================================================================

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Book N consecutive tee times under N accounts (tournament)")
    ap.add_argument("--date", default=None,
                    help="Saturday M/D/YYYY (default: upcoming Saturday)")
    ap.add_argument("--courses", default=DEFAULT_COURSES,
                    help=f"comma list, in priority order (default '{DEFAULT_COURSES}')")
    ap.add_argument("--players", type=int, default=4,
                    help="players per slot (default 4)")
    ap.add_argument("--start", default="8:00 AM", help="window start (default 8:00 AM)")
    ap.add_argument("--end", default="1:00 PM", help="window end, inclusive (default 1:00 PM)")
    ap.add_argument("--prefer", default=None,
                    help="optional 'H:MM AM/PM' target; rank blocks by closeness "
                         "to it (default: earliest block)")
    ap.add_argument("--max-gap", dest="max_gap", type=int, default=10,
                    help="max minutes between adjacent slots to count as "
                         "consecutive (default 10)")
    ap.add_argument("--max-spread", dest="max_spread", type=int, default=30,
                    help="fallback: max total span of the 3-slot block (default 30)")
    ap.add_argument("--holes", type=int, default=18, choices=(9, 18),
                    help="9 or 18 (default 18)")
    ap.add_argument("--accounts", default=None,
                    help="comma list of accounts.json ids (default: all enabled)")
    ap.add_argument("--no-sunday", dest="no_sunday", action="store_true",
                    help="skip the per-account regular Sunday booking")
    ap.add_argument("--now", action="store_true",
                    help="skip the 8 PM wait; warm up and run immediately")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="walk the booking flow but abort at the cart")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    # Validate everything up front so a bad flag dies loudly (to the terminal,
    # not as a crash push) before we ever launch a browser at 8 PM.
    try:
        start_min, end_min = parse_window(args.start, args.end)
        courses = parse_courses(args.courses)
        prefer_min = parse_clock("--prefer", args.prefer) if args.prefer else None
    except ValueError as e:
        print(f"FATAL (bad arguments): {e}", flush=True)
        return 2
    max_hour = end_min // 60
    accounts = resolve_accounts(args.accounts)
    if not accounts:
        print("FATAL: no enabled accounts in accounts.json", flush=True)
        return 1

    saturday_date, sunday_date = get_next_weekend_dates()
    date = args.date or saturday_date

    print("=" * 60)
    print(f"Tournament Bot — started {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Saturday={date}  courses={[n for _, n in courses]}")
    print(f"  window={args.start}-{args.end}  players={args.players}  "
          f"holes={args.holes}  prefer={args.prefer or '-'}")
    print(f"  accounts={[a['id'] for a in accounts]}  "
          f"sunday={'off' if args.no_sunday else sunday_date}")
    print(f"  mode={'IMMEDIATE' if args.now else 'SCHEDULED'}"
          f"{' [DRY-RUN]' if args.dry_run else ''}")
    print("=" * 60, flush=True)

    if not args.now:
        send_ntfy("Tournament bot launched",
                  f"{len(accounts)} accounts chasing {TOURNAMENT_SIZE} consecutive "
                  f"slots on Sat {date} ({', '.join(n for _, n in courses)}).",
                  priority="low", tags="rocket")

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=not args.headful)
        try:
            sessions = warm_up_sessions(browser, accounts, announce=not args.now)

            if not args.now:
                wait_until_release_time()
                reverify_sessions(sessions)

            authed = [s for s in sessions if s["authed"]]
            if not authed:
                print("FATAL: no account authenticated — cannot book", flush=True)
                notify("Tournament: all logins failed",
                       "No account made it through login/Queue-it.",
                       priority="urgent", tags="x")
                return 1

            # Target a block sized to how many accounts actually got in.
            n = min(TOURNAMENT_SIZE, len(authed))

            print(f"\n=== SCANNING {len(courses)} course(s) for a {n}-slot block ===",
                  flush=True)
            course_slots = scan_courses(authed[0]["page"], courses, date,
                                        args.players, args.holes, max_hour,
                                        start_min, end_min)
            chosen = choose_block(course_slots, n=n, max_gap_min=args.max_gap,
                                  max_spread_min=args.max_spread, prefer_min=prefer_min)

            if chosen is None:
                print("NO BLOCK: no bookable slots in window on any course",
                      flush=True)
                sat_results = [{"acct": s["acct"], "slot": None, "status": "no_slot"}
                               for s in sessions]
            else:
                print(f"\n=== BOOKING {chosen['kind']} block at "
                      f"{chosen['course_name']}: "
                      f"{', '.join(s['time'] for s in chosen['block'])} ===",
                      flush=True)
                sat_results = book_block(sessions, chosen, date, args.players,
                                         args.holes, max_hour, start_min, end_min,
                                         args.dry_run)

            sun_results: list[dict] = []
            if not args.no_sunday:
                print("\n=== SUNDAY (one regular slot per account) ===", flush=True)
                sun_results = book_sunday(sessions, sunday_date, args.players,
                                          args.dry_run)

            report(chosen, sat_results, sun_results, date, args.dry_run)
        finally:
            try:
                browser.close()
            except Exception:
                pass
            clear_live_screenshot()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted", flush=True)
        sys.exit(130)
    except Exception:
        print("FATAL:", flush=True)
        traceback.print_exc()
        try:
            notify("Tournament bot crashed",
                   "Unhandled exception — see tournament.log.",
                   priority="urgent", tags="x")
        except Exception:
            pass
        sys.exit(1)
