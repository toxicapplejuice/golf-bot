#!/usr/bin/env python3
"""
Austin Municipal Golf Tee Time Booking Bot

Automatically books tee times at Lions, Roy Kizer, or Jimmy Clay
for Saturday/Sunday mornings when they release on Monday at 8pm CT.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import smtplib
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth
    stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

# Multi-account coordination. Safe to import unconditionally — if the bot is
# run single-account, the shared-state calls just claim against an empty file.
import shared_state

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_screenshots")
ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.json")

# Default per-account paths use the account id as suffix. The single-account
# legacy names (state.json, booking.log, live.png) remain the defaults for
# backward compatibility when no --account-id is passed.
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "history.json")
HISTORY_MAX_ENTRIES = 50

# Active account context. Populated at startup by configure_account_context()
# using --account-id (or the .env fallback for backward compat).
ACCOUNT_ID: str = "default"
ACCOUNT_DISPLAY_NAME: str = "Golf Bot"

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from config import (
    BASE_URL,
    SEARCH_URL,
    COURSE_CODES,
    TIME_PRIORITY,
    NUM_PLAYERS as DEFAULT_NUM_PLAYERS,
    FALLBACK_NUM_PLAYERS,
    MIN_HOUR,
    MAX_HOUR,
    FALLBACK_MAX_HOUR,
)

# Retry / timing constants
MAX_LOGIN_RETRIES = 10
LOGIN_RETRY_DELAY = 5
DEFAULT_MAX_TOTAL_TIME = 1800  # 30 min

RELEASE_HOUR = 20
RELEASE_MINUTE = 0
QUEUE_DEADLINE_HOUR = 20
QUEUE_DEADLINE_MINUTE = 5

# Queue-it fallback: long enough to ride out the 8pm rush without tossing progress
QUEUE_FALLBACK_TIMEOUT = 3600

# Tight refresh between empty search rounds (was 3s, hurts rush-minute throughput)
REFRESH_BETWEEN_ROUNDS_MS = 500

MAX_SEARCH_ROUNDS_PER_PASS = 3

# Credentials default to the .env values. configure_account_context() overrides
# these at startup when an --account-id flag selects a different accounts.json entry.
USERNAME = os.getenv("GOLF_USERNAME")
PASSWORD = os.getenv("GOLF_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")  # e.g. "golfbot-michael-xyz123"
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")


# ======================================================================
# Account loading + per-account path configuration
# ======================================================================

def load_accounts() -> list:
    """Load accounts.json if it exists. Returns a list of account dicts
    with enabled accounts only (filters out entries with disabled=True)."""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    # Validate shape and filter out disabled
    valid = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if not all(k in entry for k in ("id", "username", "password")):
            continue
        if entry.get("disabled"):
            continue
        if entry.get("username") == "REPLACE_ME" or entry.get("password") == "REPLACE_ME":
            continue
        entry.setdefault("display_name", entry["id"])
        valid.append(entry)
    return valid


def get_account_by_id(account_id: str) -> dict | None:
    """Return the account dict with matching id, or None."""
    for entry in load_accounts():
        if entry.get("id") == account_id:
            return entry
    return None


def configure_account_context(account_id: str | None) -> dict:
    """Set module-level credentials and per-account file paths.

    If account_id is None, use the legacy .env defaults (single-account mode).
    If account_id is given, look it up in accounts.json and switch credentials
    + file paths to that account's namespace.

    Returns the active account dict so callers can access display_name.
    """
    global USERNAME, PASSWORD, ACCOUNT_ID, ACCOUNT_DISPLAY_NAME
    global STATE_FILE, LIVE_SCREENSHOT, BOOKING_LOG_PATH

    if account_id is None:
        # Legacy single-account mode — keep current behavior
        account = {
            "id": "default",
            "display_name": "Golf Bot",
            "username": USERNAME,
            "password": PASSWORD,
        }
    else:
        found = get_account_by_id(account_id)
        if not found:
            raise SystemExit(
                f"Account id {account_id!r} not found in {ACCOUNTS_FILE}. "
                "Check the file for a matching entry (and ensure it isn't "
                "disabled or using REPLACE_ME credentials)."
            )
        account = found
        USERNAME = account["username"]
        PASSWORD = account["password"]
        ACCOUNT_ID = account["id"]
        ACCOUNT_DISPLAY_NAME = account.get("display_name", account["id"])
        # Per-account file paths
        STATE_FILE = os.path.join(SCRIPT_DIR, f"state_{account['id']}.json")
        LIVE_SCREENSHOT = os.path.join(DEBUG_DIR, f"live_{account['id']}.png")
        BOOKING_LOG_PATH = os.path.join(SCRIPT_DIR, f"booking_{account['id']}.log")

    return account


def live_label_path() -> str:
    """Per-account live-screenshot label path. Derived at call time so it
    works whether or not configure_account_context has run."""
    suffix = f"_{ACCOUNT_ID}" if ACCOUNT_ID != "default" else ""
    return os.path.join(DEBUG_DIR, f"live_label{suffix}.txt")


# ======================================================================
# Email
# ======================================================================

def send_email(subject: str, body: str) -> None:
    if not all([SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD, NOTIFICATION_EMAIL]):
        return

    msg = MIMEMultipart()
    msg["From"] = SMTP_USERNAME
    msg["To"] = NOTIFICATION_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Failed to send email: {e}")


def send_ntfy(title: str, message: str, priority: str = "default",
              tags: str = None) -> None:
    """Send a push notification via ntfy.sh.

    priority: "min", "low", "default", "high", "urgent"
    tags: comma-separated emoji shortcodes (ntfy renders these), e.g.
          "golf,white_check_mark" — use these for emoji instead of putting
          them in the title, because HTTP headers must be latin-1 (ASCII-safe).
    """
    if not NTFY_TOPIC:
        return
    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    # Title must be ASCII-safe (HTTP header limitation). Strip non-latin-1 chars
    # rather than failing — any emoji the caller passed here should come through
    # via the `tags` parameter instead.
    safe_title = title.encode("latin-1", errors="ignore").decode("latin-1")
    headers = {
        "Title": safe_title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags
    try:
        req = urllib.request.Request(
            url, data=message.encode("utf-8"), headers=headers, method="POST"
        )
        # The CLT Python's OpenSSL can't find the system CA bundle
        # (CERTIFICATE_VERIFY_FAILED) — use certifi's when available.
        ssl_ctx = None
        try:
            import ssl
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        urllib.request.urlopen(req, timeout=10, context=ssl_ctx)
        print(f"ntfy sent: {safe_title}")
    except Exception as e:
        print(f"Failed to send ntfy: {e}")


def notify(title: str, message: str, priority: str = "default",
           tags: str = None) -> None:
    """Send notifications via all configured channels (ntfy + email)."""
    send_ntfy(title, message, priority=priority, tags=tags)
    # Email as fallback/additional
    if SMTP_SERVER:
        send_email(title, message)
    # Always print to log
    print(f"\n=== NOTIFY: {title} ===\n{message}\n===")


# ======================================================================
# Debug screenshots
# ======================================================================

def save_debug_screenshot(page, label: str) -> None:
    """Save a screenshot for post-mortem debugging. Silently no-ops on failure."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(DEBUG_DIR, f"debug_{label}_{ts}.png")
        page.screenshot(path=path, full_page=False)
        print(f"  [debug] Screenshot saved: {path}")
    except Exception as e:
        print(f"  [debug] Screenshot failed ({label}): {e}")


# Rolling "what the bot sees right now" snapshot — overwritten on every call.
# The dashboard reads this file to render a live browser view.
LIVE_SCREENSHOT = os.path.join(DEBUG_DIR, "live.png")

# Watchdog: if the log hasn't been written to in this many seconds, alert.
WATCHDOG_STALL_SECONDS = 90
WATCHDOG_CHECK_INTERVAL_SECONDS = 30
BOOKING_LOG_PATH = os.path.join(SCRIPT_DIR, "booking.log")


class Watchdog:
    """Background thread that notifies if the booking.log stops being written.

    Uses the log file's mtime as the heartbeat — the bot writes via print()
    and log redirection, so if the file is stale the bot is stalled. This
    is thread-safe (we never touch the Playwright page from here).
    """

    def __init__(self, log_path: str = None,
                 stall_seconds: int = WATCHDOG_STALL_SECONDS):
        # log_path defaults to the module-level BOOKING_LOG_PATH at instantiation
        # time (not at class definition time) so per-account overrides work.
        self.log_path = log_path or BOOKING_LOG_PATH
        self.stall_seconds = stall_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread = None
        self._notified = False  # debounce — only notify once per stall

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name="watchdog")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(WATCHDOG_CHECK_INTERVAL_SECONDS):
            try:
                if not os.path.exists(self.log_path):
                    continue
                mtime = os.path.getmtime(self.log_path)
                age = time.time() - mtime
                if age > self.stall_seconds:
                    if not self._notified:
                        self._notified = True
                        send_ntfy(
                            "Golf Bot: possibly stuck",
                            f"No log activity for {int(age)}s. Check the bot — "
                            f"it may be hung. (Watchdog threshold: {self.stall_seconds}s)",
                            priority="urgent",
                            tags="rotating_light",
                        )
                else:
                    # Reset so we can re-alert if it stalls again
                    self._notified = False
            except Exception:
                pass  # watchdog must never take down the bot


def update_live_screenshot(page, label: str = "") -> None:
    """Overwrite the live snapshot the dashboard displays. Cheap + best-effort."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=LIVE_SCREENSHOT, full_page=False, timeout=3000)
        # Also write a label file for the dashboard to display
        with open(live_label_path(), "w") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} — {label}")
    except Exception:
        pass  # screenshots must never block booking


def clear_live_screenshot() -> None:
    """Remove the live snapshot when the bot finishes so the dashboard shows 'idle'."""
    for path in (LIVE_SCREENSHOT, live_label_path()):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


# ======================================================================
# State persistence (resume-on-crash)
# ======================================================================

def load_state(saturday_date: str, sunday_date: str) -> dict:
    """Load saved booking state for the current weekend. Returns empty if
    state is stale (different weekend) or missing."""
    empty = {
        "saturday": {"success": False, "details": None, "course": None},
        "sunday": {"success": False, "details": None, "course": None},
    }
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return empty

    # Stale check: only use state if it matches this weekend
    if state.get("saturday_date") != saturday_date or state.get("sunday_date") != sunday_date:
        print(f"  [state] Existing state is for a different weekend — ignoring")
        return empty

    if state.get("results"):
        sat = state["results"].get("saturday", {})
        sun = state["results"].get("sunday", {})
        # Resume if either day was booked OR halted for manual review (so we
        # don't re-attempt a day where we may have already committed a booking).
        has_progress = (sat.get("success") or sun.get("success")
                        or sat.get("halt_day") or sun.get("halt_day"))
        if has_progress:
            def _label(d):
                if d.get("success"):
                    return d.get("details") or "booked"
                if d.get("halt_day"):
                    return "halted (manual check)"
                return "pending"
            print(f"  [state] Resuming: Sat={_label(sat)}, Sun={_label(sun)}")
            return state["results"]
    return empty


def save_state(saturday_date: str, sunday_date: str, results: dict) -> None:
    """Save booking state so a crashed run can resume."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "saturday_date": saturday_date,
                "sunday_date": sunday_date,
                "saved_at": datetime.now().isoformat(),
                "results": results,
            }, f, indent=2)
    except Exception as e:
        print(f"  [state] Save failed: {e}")


def clear_state() -> None:
    """Clear state file after a fully successful run (both days booked)."""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass


# ======================================================================
# Historical run log (for dashboard history panel)
# ======================================================================

def _load_history() -> list:
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def append_to_history(saturday_date: str, sunday_date: str, results: dict,
                       run_started: str, run_ended: str,
                       notes: str = None) -> None:
    """Record a completed run in history.json. Newest entries first.

    Keeps last HISTORY_MAX_ENTRIES runs so the file doesn't grow forever.
    Dashboard displays this as a "past runs" panel.
    """
    history = _load_history()
    entry = {
        "run_started": run_started,
        "run_ended": run_ended,
        "weekend": f"{saturday_date} - {sunday_date}",
        "saturday_date": saturday_date,
        "sunday_date": sunday_date,
        "account_id": ACCOUNT_ID,
        "account_name": ACCOUNT_DISPLAY_NAME,
        "results": results,
    }
    if notes:
        entry["notes"] = notes
    history.insert(0, entry)
    history = history[:HISTORY_MAX_ENTRIES]
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"  [history] Save failed: {e}")


def _booking_matches(booking: dict, account_id: str, time: str, course) -> bool:
    """True if a history booking dict matches the holding account + slot.

    Matches on the stored ``details`` string (e.g. "8:40 AM at Jimmy Clay"),
    which carries both time and course, so this works for legacy multi-bot
    sub-bookings that predate the explicit ``course`` field.
    """
    if booking.get("booked_by") != account_id:
        return False
    details = booking.get("details") or ""
    if time not in details:
        return False
    if course and course not in details:
        return False
    return True


def mark_booking_cancelled(account_id: str, date: str, time: str, course,
                           history_file: str = None) -> bool:
    """Flag a booking as cancelled in history.json. Returns True if one matched.

    Handles both history shapes:
      - single-account: the day dict itself carries booked_by/details/course
      - multi-bot: the day dict has a 'bookings' list of per-account entries

    ``account_id`` is the holding account (the booking's ``booked_by``). All
    matching bookings across every history entry for that date are flagged, so
    a re-booked weekend with duplicate entries stays consistent.
    """
    path = history_file or HISTORY_FILE
    try:
        with open(path, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    cancelled_at = datetime.now().isoformat(timespec="seconds")
    changed = False

    for entry in history:
        results = entry.get("results", {})
        for day_key in ("saturday", "sunday"):
            day = results.get(day_key)
            if not isinstance(day, dict):
                continue
            if entry.get(f"{day_key}_date") != date:
                continue
            subs = day.get("bookings")
            targets = subs if isinstance(subs, list) and subs else [day]
            for booking in targets:
                if _booking_matches(booking, account_id, time, course) \
                        and not booking.get("cancelled"):
                    booking["cancelled"] = True
                    booking["cancelled_at"] = cancelled_at
                    changed = True

    if changed:
        try:
            with open(path, "w") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            print(f"  [history] cancel-mark save failed: {e}")
            return False
    return changed


# ======================================================================
# Pure helpers (dates, times, priorities)
# ======================================================================

def get_next_weekend_dates() -> tuple[str, str]:
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.weekday() == 5:
        days_until_saturday = 7
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)
    return saturday.strftime("%-m/%d/%Y"), sunday.strftime("%-m/%d/%Y")


def parse_time(time_str: str) -> int:
    """'9:00 AM' -> minutes since midnight. Invalid -> 9999."""
    match = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.IGNORECASE)
    if not match:
        return 9999
    hour, minute, period = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if period == "PM" and hour != 12:
        hour += 12
    elif period == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute


def is_time_in_range(time_str: str, max_hour: int = MAX_HOUR) -> bool:
    """True if time is within [MIN_HOUR, max_hour] inclusive of the max hour's minutes."""
    minutes = parse_time(time_str)
    min_minutes = MIN_HOUR * 60
    max_minutes = (max_hour + 1) * 60
    return min_minutes <= minutes < max_minutes


def get_time_priority(time_str: str) -> int:
    """Lower = better. Preferred order: 8:30am > 8am > 9am > 10am > ...

    Times in TIME_PRIORITY get their list index (lower = better).
    Unlisted times fall back to a bucket based on hour.
    """
    if time_str in TIME_PRIORITY:
        return TIME_PRIORITY.index(time_str)
    minutes = parse_time(time_str)
    hour = minutes // 60
    if hour == 8:
        return 5
    if hour == 9:
        return 10
    if hour == 10:
        return 20
    if hour == 11:
        return 30
    if hour == 12:
        return 40
    return 100


# ======================================================================
# Page state detection
# ======================================================================

def is_on_login_page(page) -> bool:
    return "login.html" in page.url.lower()


def is_in_queue(page) -> bool:
    """True if the page is currently showing the Queue-it waiting room."""
    current_url = page.url.lower()
    if "queue-it.net" in current_url:
        return True
    try:
        content = page.content().lower()
    except Exception:
        return False
    return (
        "you're in line" in content
        or "virtual waiting room" in content
        or "will be entering our site soon" in content
    )


def is_authenticated(page) -> bool:
    """True if the page shows logged-in chrome (Sign Out / My Account link).

    The 2026-07 site redesign renders Logout as a <button> on a
    "You are logged in" welcome page, so match buttons and that text
    too, not just anchor links.
    """
    if is_in_queue(page) or is_on_login_page(page):
        return False
    try:
        # A "Sign In" link only renders when logged OUT — its presence is
        # definitive. This gate MUST come first: the 2026-07 redesign put
        # "My Account" in the public (logged-out) nav too, so that link
        # alone proves nothing. Trusting it made every fresh browser look
        # "already authenticated", silently skipping login — searches still
        # worked (public), but every checkout bounced to login.html
        # (the 7/20 release wall and the 7/24 lost standby slots).
        if page.locator("a:has-text('Sign In')").count() > 0:
            return False
        if page.locator(
            "a:has-text('Sign Out'), a:has-text('Logout'), a:has-text('Log Out'), "
            "button:has-text('Sign Out'), button:has-text('Logout'), button:has-text('Log Out')"
        ).count() > 0:
            return True
        if "you are logged in" in page.content().lower():
            return True
        # "My Account" is only meaningful once the Sign In gate above has
        # ruled out the logged-out nav.
        if page.locator("a:has-text('My Account'), a:has-text('My Profile')").count() > 0:
            return True
    except Exception:
        pass
    return False


# ======================================================================
# Queue-it waiting
# ======================================================================

def wait_for_queue(page, mode: str = "timeout",
                    max_wait_seconds: int = QUEUE_FALLBACK_TIMEOUT) -> bool:
    """Wait for Queue-it to release us.

    mode='deadline' -> wait until 8:05 PM (for pre-release login).
    mode='timeout'  -> wait up to max_wait_seconds (for mid-session recovery).
    """
    check_interval_ms = 10000

    if mode == "deadline":
        deadline = datetime.now().replace(
            hour=QUEUE_DEADLINE_HOUR, minute=QUEUE_DEADLINE_MINUTE,
            second=0, microsecond=0,
        )
        print(f"  [queue] Waiting until {deadline.strftime('%H:%M:%S')} (deadline mode)")
        while True:
            now = datetime.now()
            if now >= deadline:
                print("  [queue] Deadline reached — still in queue")
                return False
            if not is_in_queue(page):
                print(f"  [queue] Released! URL: {page.url[:60]}")
                update_live_screenshot(page, "through Queue-it")
                return True
            remaining = int((deadline - now).total_seconds())
            print(f"  [queue] Still waiting... ({remaining}s until deadline)")
            update_live_screenshot(page, f"Queue-it: {remaining}s until deadline")
            page.wait_for_timeout(check_interval_ms)

    start = time.time()
    print(f"  [queue] Waiting up to {max_wait_seconds}s (timeout mode)")
    while time.time() - start < max_wait_seconds:
        if not is_in_queue(page):
            print(f"  [queue] Released! URL: {page.url[:60]}")
            update_live_screenshot(page, "through Queue-it")
            return True
        elapsed = int(time.time() - start)
        print(f"  [queue] Still waiting... ({elapsed}s elapsed)")
        update_live_screenshot(page, f"Queue-it: {elapsed}s waiting")
        page.wait_for_timeout(check_interval_ms)
    print(f"  [queue] Timeout after {max_wait_seconds}s")
    return False


# ======================================================================
# Release-time wait
# ======================================================================

def wait_until_release_time() -> None:
    """Sleep until 8:00 PM CT.

    Deliberately does NOT touch the page. The previous version periodically
    reloaded as "keepalive", which silently landed the session in Queue-it
    without any detection — that was the root cause of the failed run on
    2026-04-13. At 8:00 PM the booking code will navigate fresh and
    navigate_to_search() handles Queue-it interception properly.
    """
    now = datetime.now()
    release_time = now.replace(hour=RELEASE_HOUR, minute=RELEASE_MINUTE,
                                second=0, microsecond=0)
    if now >= release_time:
        print("Already past release time, proceeding immediately")
        return

    wait_seconds = (release_time - now).total_seconds()
    print(f"\n*** Waiting until {release_time.strftime('%H:%M:%S')} for tee time release ***")
    print(f"    Current: {now.strftime('%H:%M:%S')}, sleeping {int(wait_seconds)}s")

    while True:
        now = datetime.now()
        if now >= release_time:
            break
        remaining = (release_time - now).total_seconds()
        if remaining > 10:
            print(f"    {int(remaining)}s until release...")
            time.sleep(10)
        else:
            time.sleep(max(0, remaining))
            break
    print("*** Release time reached! ***\n")


# ======================================================================
# Login (per-step try/except so failures are identifiable in logs)
# ======================================================================

def login_once(page, queue_mode: str = "timeout") -> bool:
    def step(label: str, action) -> bool:
        try:
            action()
            return True
        except PlaywrightTimeout:
            print(f"  [login] TIMEOUT at step: {label}")
            return False
        except Exception as e:
            print(f"  [login] ERROR at step '{label}': {e}")
            return False

    def handle_queue_if_present() -> bool:
        if is_in_queue(page):
            print(f"  [login] Queue-it detected — waiting ({queue_mode} mode)")
            return wait_for_queue(page, mode=queue_mode)
        return True

    if not step("goto base",
                lambda: page.goto(BASE_URL, timeout=60000, wait_until="domcontentloaded")):
        return False
    print(f"  [login] Page: {page.title()[:60]} | URL: {page.url[:80]}")
    update_live_screenshot(page, f"login: {page.title()[:40]}")

    if not handle_queue_if_present():
        return False

    # A previous attempt on this same page may have logged in even though a
    # later step timed out (e.g. a slow Cloudflare interstitial after submit).
    # The logged-in home page has no Sign In link, so without this check every
    # retry would time out on "click Sign In" until attempts are exhausted.
    if is_authenticated(page):
        print("  [login] Already authenticated — skipping sign-in")
        update_live_screenshot(page, "logged in")
        return True

    if not step("click Sign In",
                lambda: page.click("a:has-text('Sign In')", timeout=60000)):
        return False
    step("wait load (post-signin click)",
         lambda: page.wait_for_load_state("domcontentloaded", timeout=30000))

    if not handle_queue_if_present():
        return False

    if not step("fill username",
                lambda: page.fill("#weblogin_username", USERNAME, timeout=10000)):
        return False
    if not step("fill password",
                lambda: page.fill("#weblogin_password", PASSWORD, timeout=10000)):
        return False

    if not step("click submit",
                lambda: page.locator("input[type='submit'], button[type='submit']").first.click(timeout=30000)):
        return False
    step("wait load (post-submit)",
         lambda: page.wait_for_load_state("domcontentloaded", timeout=30000))

    if not handle_queue_if_present():
        return False

    # Optional "Continue with Login" intercept page
    try:
        cont = page.locator(
            "button:has-text('Continue'), a:has-text('Continue with Login'), button:has-text('Continue with Login')"
        )
        if cont.count() > 0:
            print("  [login] Clicking Continue with Login...")
            cont.first.click(timeout=10000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PlaywrightTimeout:
                pass
    except Exception as e:
        print(f"  [login] Continue-button step: {e}")

    if is_on_login_page(page):
        err = page.locator(".error, .alert, [class*='error']")
        if err.count() > 0:
            try:
                print(f"  [login] FAILED: {err.first.text_content()[:120]}")
            except Exception:
                pass
        print("  [login] FAILED: still on login page")
        save_debug_screenshot(page, "login_failed")
        return False

    if not is_authenticated(page):
        print(f"  [login] Not clearly authenticated (URL: {page.url[:80]}) — continuing")

    print("  [login] Success!")
    update_live_screenshot(page, "logged in")
    return True


def force_fresh_login(page, queue_mode: str = "timeout") -> bool:
    """Clear session cookies, then perform a REAL form login.

    Cart-bounce recovery needs this: when a checkout click bounces to
    login.html, the *browsing* session still looks authenticated, so
    login_once short-circuits on "already authenticated" and nothing
    actually refreshes — the same slot then bounces again (the 2026-07-20
    failure mode). Clearing cookies makes is_authenticated() false, so the
    next login_once performs a genuine sign-in and mints a fresh session.

    NEVER call this during the pre-release wait (wait_until_release_time
    must stay pure time.sleep — see CLAUDE.md) or on navigation failures —
    dropping cookies there would forfeit Queue-it progress for nothing.
    Only call it AFTER a cart bounce (attempt_booking_click returned
    "session_expired"), where the choice is between risking a queue
    re-entry and never booking at all.
    """
    print("  [login] Forcing fresh login (clearing session cookies)")
    try:
        page.context.clear_cookies()
    except Exception as e:
        print(f"  [login] Cookie clear failed ({e}) — attempting login anyway")
    return login_with_retry(page, queue_mode=queue_mode)


def login_with_retry(page, queue_mode: str = "timeout") -> bool:
    """Retry login on the same page (preserves queue progress across attempts)."""
    print("Logging in...")
    for attempt in range(1, MAX_LOGIN_RETRIES + 1):
        print(f"\n  Login attempt {attempt}/{MAX_LOGIN_RETRIES}...")
        # Only first attempt uses deadline mode; retries use fallback timeout
        mode = queue_mode if attempt == 1 else "timeout"
        if login_once(page, queue_mode=mode):
            return True
        if attempt < MAX_LOGIN_RETRIES:
            print(f"  Waiting {LOGIN_RETRY_DELAY}s before retry...")
            time.sleep(LOGIN_RETRY_DELAY)
            try:
                page.goto(BASE_URL, timeout=60000)
            except Exception:
                pass
    print(f"  Login failed after {MAX_LOGIN_RETRIES} attempts")
    save_debug_screenshot(page, "login_exhausted")
    return False


# ======================================================================
# Navigation with Queue-it + session-expiry recovery
# ======================================================================

MAX_NAV_RECOVERY_ATTEMPTS = 3
NAV_TIMEOUT_NORMAL = 30000
NAV_TIMEOUT_RUSH = 12000

_rush_mode = False


def set_rush_mode(enabled: bool) -> None:
    global _rush_mode
    _rush_mode = enabled


# ======================================================================
# Cart-bounce circuit breaker (2026-07-20 postmortem)
# ======================================================================
# On the 2026-07-20 release the site repeatedly bounced the Add-to-cart click
# to login.html even though the *browsing* session was still valid. Each bounce
# was read as "session expired", so the bot re-logged in — but login was a no-op
# ("already authenticated"), so nothing changed and the same slot bounced again.
# The result was 192 futile attempts that burned the entire 30-minute budget
# (and hammered the site) without ever booking. Recovery is now two layers
# (2026-07-24, ported from the standby path): a bounced slot first gets ONE
# force_fresh_login (cookies cleared, REAL re-auth) plus a same-slot retry;
# only a bounce that survives that fresh session counts toward this breaker.
# Too many consecutive surviving bounces means the site is refusing checkout
# and spinning won't help — abort the run so it fails fast. Any cart action
# that actually reaches the site (booked/taken/failed) resets the streak, so
# only the pathological loop trips it.

MAX_CART_BOUNCE_REEXPIRY = 5

_cart_bounce_reexpiry = 0
_circuit_tripped = False


def reset_session_circuit() -> None:
    """Reset the cart-bounce circuit breaker. Called once at the start of a run."""
    global _cart_bounce_reexpiry, _circuit_tripped
    _cart_bounce_reexpiry = 0
    _circuit_tripped = False


def note_cart_bounce_reexpiry() -> bool:
    """Record a cart bounce that survived a fresh-login retry.

    Returns True if the circuit is now tripped (too many consecutive
    surviving bounces — the site is refusing checkout and retrying won't
    help).
    """
    global _cart_bounce_reexpiry, _circuit_tripped
    _cart_bounce_reexpiry += 1
    if _cart_bounce_reexpiry >= MAX_CART_BOUNCE_REEXPIRY:
        _circuit_tripped = True
    return _circuit_tripped


def note_cart_progress() -> None:
    """A cart action reached the site (booked / taken / failed — not a login
    bounce), so the session is healthy. Reset the consecutive no-op counter."""
    global _cart_bounce_reexpiry
    _cart_bounce_reexpiry = 0


def session_circuit_tripped() -> bool:
    return _circuit_tripped


def navigate_to_search(page, url: str) -> bool:
    """Navigate to a search URL, handling Queue-it interception and session expiry.

    Returns True if we ended up on the target page authenticated. Any caller
    that uses page.goto() directly risks silently parsing a Queue-it waiting
    room page and seeing zero rows — always route through this helper.

    Loops up to MAX_NAV_RECOVERY_ATTEMPTS times to handle chained failures
    (e.g. Queue-it wait -> session expired -> Queue-it again).
    """
    nav_timeout = NAV_TIMEOUT_RUSH if _rush_mode else NAV_TIMEOUT_NORMAL
    for attempt in range(1, MAX_NAV_RECOVERY_ATTEMPTS + 1):
        try:
            page.goto(url, timeout=nav_timeout, wait_until="domcontentloaded")
        except (PlaywrightTimeout, PlaywrightError) as e:
            label = "goto timed out" if isinstance(e, PlaywrightTimeout) else f"goto failed ({type(e).__name__})"
            print(f"  [nav] {label} (attempt {attempt})")
            if attempt < MAX_NAV_RECOVERY_ATTEMPTS:
                continue
            return False

        if is_in_queue(page):
            print("  [nav] Hit Queue-it — waiting through it")
            if not wait_for_queue(page, mode="timeout"):
                save_debug_screenshot(page, "nav_queue_timeout")
                return False
            continue  # re-navigate after queue release

        if is_on_login_page(page):
            print("  [nav] Session expired — re-authenticating")
            if not login_with_retry(page, queue_mode="timeout"):
                save_debug_screenshot(page, "nav_relogin_failed")
                return False
            continue  # re-navigate after login

        # Not in queue, not on login page — we're on the target page
        return True

    print(f"  [nav] Failed after {MAX_NAV_RECOVERY_ATTEMPTS} recovery attempts")
    save_debug_screenshot(page, "nav_exhausted")
    return False


# ======================================================================
# Search URL + slot extraction
# ======================================================================

def build_search_url(course_code: str, date: str, num_players: int,
                     holes: int = 18) -> str:
    return (
        f"{SEARCH_URL}"
        f"&secondarycode={course_code}"
        f"&begindate={date}"
        f"&begintime=07:00 am"
        f"&numberofplayers={num_players}"
        f"&numberofholes={holes}"
        f"&Action=Start"
    )


def extract_available_slots(page, course_code: str, course_name: str, date: str,
                             num_players: int, max_hour: int,
                             blacklist: set) -> list[dict]:
    """Parse search results into sorted bookable slots, skipping blacklisted ones."""
    try:
        page.wait_for_selector(
            "tr:has-text('Available'), #grwebsearch_noresultsheader, :text('did not return')",
            timeout=3000,
        )
    except PlaywrightTimeout:
        pass

    no_results = page.locator("#grwebsearch_noresultsheader")
    if no_results.count() > 0 and no_results.is_visible():
        return []

    try:
        content_lower = page.content().lower()
    except Exception:
        content_lower = ""
    if "did not return any matching" in content_lower:
        return []

    slots: list[dict] = []
    seen_times: set[str] = set()

    for idx, row in enumerate(page.locator("tr:has-text('Available')").all()):
        try:
            row_text = row.text_content() or ""
            if row_text.lower().count("available") < num_players:
                continue

            time_match = re.search(r"(\d{1,2}:\d{2}\s*[ap]m)", row_text, re.IGNORECASE)
            if not time_match:
                continue
            time_str = time_match.group(1).strip().upper()
            if time_str.startswith("0"):
                time_str = time_str[1:]

            if not is_time_in_range(time_str, max_hour=max_hour):
                continue
            if time_str in seen_times:
                continue
            seen_times.add(time_str)

            if (date, course_name, time_str) in blacklist:
                continue

            slots.append({
                "time": time_str,
                "course": course_name,
                "course_code": course_code,
                "date": date,
                "row_index": idx,
                "priority": get_time_priority(time_str),
            })
        except Exception:
            continue

    slots.sort(key=lambda s: (s["priority"], parse_time(s["time"])))
    return slots


# ======================================================================
# Booking click + confirmation check
# ======================================================================

TAKEN_KEYWORDS = [
    "already taken", "no longer available", "not available",
    "already booked", "sold out", "taken by another", "in use",
    "already reserved", "no longer open", "has been reserved",
    "time slot is full", "maximum number", "duplicate",
    "invalid selection", "encountered the following restrictions",
    "limit one tee time", "tee time per fm",
]

# Positive confirmation signals — at least one must be present to count as booked.
# Without a positive signal, we assume the booking did NOT go through.
BOOKED_URL_MARKERS = ("confirmation", "receipt", "complete", "finishaddtocart")
BOOKED_TEXT_MARKERS = (
    "receipt number", "confirmation number", "booking confirmed",
    "has been added", "successfully reserved", "reservation confirmed",
    "thank you for your reservation", "tee time confirmation",
)

# Cart-page finish control — shared by the booking flow and cancel_bot's
# cancellation cart (which routes through the same addtocart.html checkout).
ONE_CLICK_FINISH_SELECTORS = (
    "button:has-text('One Click')",
    "a:has-text('One Click')",
    "input[value*='One Click']",
    "#oneclickfinish",
)

# Reservation history page — used for post-booking verification
HISTORY_URL = f"{BASE_URL}/history.html?historyoption=inquiry"


def verify_booking_on_page(page, slot: dict, page_text: str) -> bool:
    """Quick first-pass verification: does the current page reference our slot?

    NOTE: As of 2026-04-27, Vermont Systems' Checkout Confirmation page does
    NOT include the slot time/course/date — it only shows a receipt number
    and emails the details. So this check returns False on real receipts in
    practice, and the URL-marker fallback in attempt_booking_click() (plus
    verify_booking_via_history) is what actually decides "booked." This
    function is kept as a defense in case the confirmation page ever starts
    showing slot details again.

    Time-format handling mirrors verify_booking_via_history() to handle
    "8:32 AM" / "8:32AM" / "8:32A" so we don't false-negative if the page
    does include the slot.
    """
    course_lower = slot["course"].lower()
    course_found = course_lower in page_text

    raw_time = slot["time"].lower()                         # "8:32 am"
    time_no_space = raw_time.replace(" ", "")                # "8:32am"
    time_condensed = (slot["time"]
                      .replace(" AM", "A")
                      .replace(" PM", "P")
                      .lower())                              # "8:32a"
    page_no_space = page_text.replace(" ", "")
    time_found = (
        raw_time in page_text
        or time_no_space in page_no_space
        or time_condensed in page_no_space
    )
    return time_found and course_found


def _slot_in_content(content: str, slot: dict) -> bool:
    """Pure: does reservation-history page content reference this slot?

    Matches date AND time AND course, tolerating the site's formatting quirks:
      - date may be zero-padded ("04/25/2026") or not ("4/25/2026")
      - time may be condensed ("8:01A") instead of full ("8:01 AM")

    Kept pure (no I/O) so it can be unit-tested and reused by both the
    booking-success check and the cancellation verify-gone check.
    """
    content = content.lower()

    date_str = slot["date"]  # e.g. "4/25/2026"
    date_parts = date_str.split("/")
    date_padded = f"{date_parts[0].zfill(2)}/{date_parts[1].zfill(2)}/{date_parts[2]}"
    date_found = date_str in content or date_padded in content

    time_lower = slot["time"].lower()
    time_condensed = slot["time"].replace(" AM", "A").replace(" PM", "P").lower()
    time_found = time_lower in content or time_condensed in content

    course_found = slot["course"].lower() in content

    return date_found and time_found and course_found


def _active_slot_in_content(content: str, slot: dict) -> bool:
    """Pure: is there an ACTIVE (Status 'Reserved') history row for this slot?

    A cancelled reservation is NOT removed from the WebTrac history table — its
    Status cell merely flips from 'Reserved' to 'Cancelled' (old cancelled rows
    keep showing the same date/time/course text). So the flat _slot_in_content
    check can't tell a live booking from a cancelled one, and a cancellation
    verify-gone built on it would forever report "still present". This splits
    the page into <tr> rows and requires a slot-matching row whose Status cell
    still reads 'Reserved'.
    """
    lowered = content.lower()
    for row in re.split(r"(?=<tr[\s>])", lowered):
        if _slot_in_content(row, slot) and re.search(
            r'data-title="status">\s*reserved', row
        ):
            return True
    return False


def _fetch_history_content(page):
    """Navigate to the reservation history page and return its lowercased HTML.

    Returns None if the page can't be loaded or the session was lost — callers
    that must distinguish "slot absent" from "couldn't check" (e.g. cancel
    verification) should branch on None explicitly rather than treating a fetch
    failure as "slot gone".
    """
    try:
        page.goto(HISTORY_URL, timeout=15000, wait_until="domcontentloaded")
    except PlaywrightTimeout:
        print("    [verify] History page timeout")
        return None
    if is_on_login_page(page) or is_in_queue(page):
        print("    [verify] Lost session while loading history")
        return None
    try:
        return page.content().lower()
    except Exception:
        return None


def slot_in_history(page, slot: dict) -> bool:
    """Navigate to reservation history and return True iff the slot appears.

    Returns False both when the slot is genuinely absent AND when the history
    page can't be loaded. Booking verification wants exactly that (a failed
    check must never read as "booked"). Cancellation needs the finer
    distinction and should call _fetch_history_content/_slot_in_content.
    """
    content = _fetch_history_content(page)
    if content is None:
        return False
    present = _slot_in_content(content, slot)
    print(f"    [verify] history check — slot {'present' if present else 'absent'}")
    return present


def verify_booking_via_history(page, slot: dict) -> bool:
    """Definitive booking verification — the slot must appear in history.

    Ground-truth check: if the booking doesn't appear here, it didn't happen.
    """
    return slot_in_history(page, slot)


def attempt_booking_click(page, slot: dict, dry_run: bool = False) -> str:
    """Click cart button for a slot and determine result.

    Returns one of: 'booked', 'taken', 'dry_run', 'session_expired', 'failed',
    'unverified_post_click'.

    Status semantics:
      booked                 — strong confirmation signal seen; safe to record.
      taken / failed         — booking definitively did NOT occur; safe to retry next slot.
      session_expired        — site bumped us to login; recover and retry.
      dry_run                — aborted before checkout per --dry-run.
      unverified_post_click  — we clicked One Click Finish and ended up somewhere
                                ambiguous. The booking MAY have committed at the
                                site. Caller MUST halt the day to avoid duplicates.

    The 'unverified_post_click' return is the safety net for the runaway-loop
    bug where verification false-negatives caused 20 real bookings: once any
    verification gap is hit after the cart click, we stop trying to book more
    slots that day rather than risk piling on duplicates.
    """
    target_row = None
    for row in page.locator("tr:has-text('Available')").all():
        try:
            row_text = (row.text_content() or "").lower()
            if slot["time"].lower() in row_text:
                target_row = row
                break
        except Exception:
            continue

    if not target_row:
        return "taken"

    cart_btn = None
    buttons = target_row.locator("button, a").all()
    for btn in buttons:
        btn_text = (btn.text_content() or "").lower()
        if "add" in btn_text or "cart" in btn_text:
            cart_btn = btn
            break
    if not cart_btn and buttons:
        cart_btn = buttons[-1]
    if not cart_btn:
        return "failed"

    pre_url = page.url
    try:
        cart_btn.click(timeout=5000)
    except Exception:
        try:
            cart_btn.evaluate("el => el.click()")
        except Exception:
            return "failed"

    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeout:
        pass

    current_url = page.url.lower()
    if current_url == pre_url or "search.html" in current_url:
        return "taken"

    if "login.html" in current_url:
        return "session_expired"

    if dry_run:
        print(f"    [dry-run] Reached {page.url[:60]} — aborting before checkout")
        return "dry_run"

    # We should be on addtocart.html now — click "One Click Finish" to complete
    clicked_finish = False
    for sel in ONE_CLICK_FINISH_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=10000)
                clicked_finish = True
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except PlaywrightTimeout:
                    pass
                break
        except Exception:
            continue

    final_url = page.url.lower()
    try:
        page_text = page.content().lower()
    except Exception:
        page_text = ""

    # Check for "taken" signals first — these are definitive
    if any(kw in page_text for kw in TAKEN_KEYWORDS):
        print(f"    [book] Slot taken (keyword match on page)")
        return "taken"

    if "login.html" in final_url:
        return "session_expired"

    # Check for POSITIVE confirmation signals — require at least one
    has_url_marker = any(marker in final_url for marker in BOOKED_URL_MARKERS)
    has_text_marker = any(marker in page_text for marker in BOOKED_TEXT_MARKERS)

    if has_url_marker or has_text_marker:
        # Receipt pages show course name and time — check they match the slot.
        if verify_booking_on_page(page, slot, page_text):
            return "booked"
        if has_url_marker:
            # 4A: confirmation-shaped URL is a very strong signal that the
            # booking committed (these substrings only appear on Vermont Systems
            # post-checkout pages). Trust it even when text-verify fails — past
            # behavior of returning "failed" here caused the bot to immediately
            # book another slot, generating duplicates. Flag for manual review.
            print(f"    [book] Confirmation URL ({final_url[:80]}) but on-page slot text mismatch — trusting URL")
            save_debug_screenshot(page, f"url_only_{slot['time'].replace(' ', '_')}")
            notify(
                f"[{ACCOUNT_DISPLAY_NAME}] Booking needs manual verification",
                f"Reached confirmation URL for {slot['time']} at {slot['course']} "
                f"on {slot['date']} but on-page slot text didn't match. Booking "
                f"likely succeeded — please verify in reservation history.",
                priority="high", tags="warning",
            )
            return "booked"
        # Text marker only, verification failed — booking may or may not have
        # committed. Halt the day to be safe.
        print(f"    [book] Confirmation text without URL marker, verify failed — halting day")
        save_debug_screenshot(page, f"text_only_unverified_{slot['time'].replace(' ', '_')}")
        return "unverified_post_click"

    # If we're still on addtocart.html, the checkout didn't complete.
    # No booking committed here, so it's safe to try the next slot.
    if "addtocart" in final_url:
        if not clicked_finish:
            print(f"    [book] On cart page but couldn't find 'One Click Finish' button")
        else:
            print(f"    [book] Clicked finish but still on cart page — slot likely taken")
        save_debug_screenshot(page, f"cart_stuck_{slot['time'].replace(' ', '_')}")
        return "failed"

    # Unknown page. If we successfully clicked One Click Finish, the booking
    # MIGHT have committed even though we don't recognize the destination —
    # halt the day rather than risk a duplicate. If we never clicked finish,
    # nothing committed, so retrying the next slot is safe.
    if clicked_finish:
        print(f"    [book] Ambiguous URL after One Click Finish ({page.url[:80]}) — halting day")
        save_debug_screenshot(page, f"ambiguous_post_finish_{slot['time'].replace(' ', '_')}")
        return "unverified_post_click"

    print(f"    [book] Ambiguous outcome pre-finish (URL: {page.url[:60]})")
    save_debug_screenshot(page, f"ambiguous_{slot['time'].replace(' ', '_')}")
    return "failed"


# ======================================================================
# Per-course search-and-book
# ======================================================================

def search_and_book_course(page, course_code: str, course_name: str, date: str,
                            num_players: int, max_hour: int, blacklist: set,
                            dry_run: bool = False,
                            weekend: str = None, day_name: str = None) -> dict:
    """Search one course and try to book the best available slot.

    `weekend` and `day_name` enable multi-account shared-state coordination.
    When a booking verifies successfully, we claim the slot in shared_state.json
    so other accounts stop trying the same day. If the claim fails (another
    account just won), we mark the booking as a duplicate and warn the user.
    """
    result = {"success": False, "details": None, "course": None}
    url = build_search_url(course_code, date, num_players)

    if not navigate_to_search(page, url):
        print(f"  [search] Nav failed for {course_name}")
        return result

    slots = extract_available_slots(page, course_code, course_name, date,
                                     num_players, max_hour, blacklist)
    if not slots:
        print(f"  [search] No suitable times at {course_name}")
        return result

    print(f"  [search] {course_name}: {len(slots)} slot(s) — "
          f"{', '.join(s['time'] for s in slots[:5])}")
    update_live_screenshot(page, f"{course_name}: {len(slots)} slots found")

    for slot in slots:
        key = (slot["date"], slot["course"], slot["time"])
        if key in blacklist:
            continue

        # Up to 2 attempts per slot: a checkout click that bounces to
        # login.html while the browsing session still looks valid makes a
        # plain re-login a no-op (the 2026-07-20 failure). Force a REAL
        # login (cookies cleared) and retry the same slot once; only a
        # bounce that survives the fresh session counts toward the
        # circuit breaker below.
        status = None
        for attempt in (1, 2):
            retry_label = " (fresh session)" if attempt == 2 else ""
            print(f"  [book] {slot['time']} at {course_name}{retry_label}...",
                  end=" ", flush=True)
            update_live_screenshot(page, f"attempting {slot['time']} at {course_name}")
            status = attempt_booking_click(page, slot, dry_run=dry_run)
            update_live_screenshot(page, f"{slot['time']} @ {course_name}: {status}")
            if status != "session_expired" or attempt == 2:
                break
            print("session expired — forcing fresh re-login")
            if not force_fresh_login(page):
                return result
            if not navigate_to_search(page, url):
                return result

        if status == "booked":
            print("BOOKED! — verifying...", end=" ", flush=True)
            # Ground-truth check: navigate to reservation history and confirm
            if verify_booking_via_history(page, slot):
                print("VERIFIED ✓")
                details = f"{slot['time']} at {course_name}"
                # Multi-account coordination: append to shared bookings list.
                # claim_booking allows up to MAX_BOOKINGS_PER_DAY total per day
                # (and rejects same-account duplicates).
                if weekend and day_name:
                    claimed, cur_state = shared_state.claim_booking(
                        weekend, day_name, details, ACCOUNT_ID,
                        course=course_name,
                    )
                    if not claimed:
                        # Day was already at MAX bookings when we tried to claim,
                        # OR this account already booked this day (shouldn't happen
                        # in normal flow). Either way our just-booked slot is a
                        # duplicate to cancel.
                        existing = (cur_state.get(day_name) or {}).get("bookings", [])
                        existing_names = ", ".join(b.get("booked_by", "?") for b in existing)
                        print(f"    [coord] WARNING: {day_name} already has "
                              f"{len(existing)} bookings ({existing_names}). "
                              f"This is a duplicate — cancel manually.")
                        send_ntfy(
                            f"[{ACCOUNT_DISPLAY_NAME}] DUPLICATE booking on {day_name}",
                            f"{day_name.capitalize()} already booked by: {existing_names}. "
                            f"Mine ({details}) is extra — cancel manually.",
                            priority="urgent", tags="warning",
                        )
                return {
                    "success": True,
                    "details": details,
                    "course": course_name,
                }
            else:
                # 2A: history check failed AFTER attempt_booking_click returned
                # "booked". The booking very likely committed at the site (we got
                # past the cart click and saw a confirmation signal); the history
                # page just didn't reflect it yet, or the slot text rendered in an
                # unexpected format. Previously we'd blacklist + retry the next
                # slot — that's exactly how 20 real bookings happened. Halt the
                # day instead and surface for manual review.
                print("VERIFICATION FAILED — halting day to prevent duplicate bookings")
                save_debug_screenshot(page, f"verify_failed_{slot['time'].replace(' ', '_')}")
                notify(
                    f"[{ACCOUNT_DISPLAY_NAME}] Booking unverified — manual check needed",
                    f"Click reached confirmation page for {slot['time']} at {course_name} "
                    f"on {date} but history page didn't show it. Halting further "
                    f"bookings for this day. Please verify reservation history manually.",
                    priority="urgent", tags="warning",
                )
                return {
                    "success": False,
                    "details": f"UNVERIFIED — possible booking at {slot['time']} at {course_name}",
                    "course": None,
                    "halt_day": True,
                }

        if status == "unverified_post_click":
            # 2A: attempt_booking_click reached an ambiguous post-cart state.
            # The booking MAY have committed. Halt the day to avoid duplicates.
            print("UNVERIFIED post-click — halting day to prevent duplicate bookings")
            notify(
                f"[{ACCOUNT_DISPLAY_NAME}] Possible booking — manual check needed",
                f"Cart click for {slot['time']} at {course_name} on {date} "
                f"reached an ambiguous state. Halting further bookings for this "
                f"day. Please verify reservation history manually.",
                priority="urgent", tags="warning",
            )
            return {
                "success": False,
                "details": f"UNVERIFIED — possible booking at {slot['time']} at {course_name}",
                "course": None,
                "halt_day": True,
            }

        if status == "dry_run":
            print("DRY-RUN OK")
            return {
                "success": True,
                "details": f"[DRY-RUN] {slot['time']} at {course_name}",
                "course": course_name,
            }

        if status == "session_expired":
            # Bounced AGAIN even after a genuine fresh login — the site is
            # refusing checkout for this session. Count it; if this keeps
            # recurring across slots, looping is futile.
            print("session expired AGAIN after fresh re-login")
            if note_cart_bounce_reexpiry():
                save_debug_screenshot(page, "cart_bounce_circuit_break")
                print(f"  [book] CIRCUIT BREAKER: {MAX_CART_BOUNCE_REEXPIRY} "
                      f"consecutive cart bounces that survived a fresh "
                      f"re-login — site is refusing checkout; aborting run")
                result["abort_run"] = True
                return result
            if not navigate_to_search(page, url):
                return result
            # DOM state is stale now — bail to outer loop
            break

        # taken / failed: blacklist and try next slot (requires re-nav to refresh DOM)
        # The cart action reached the site (it didn't bounce to login), so the
        # session is healthy — clear the cart-bounce streak.
        print("taken" if status == "taken" else "failed")
        note_cart_progress()
        blacklist.add(key)
        if not navigate_to_search(page, url):
            break

    return result


# ======================================================================
# Day-level orchestration (two-pass: morning then fallback window)
# ======================================================================

def try_book_day(page, date: str, day_name: str, num_players: int,
                 blacklist: set, exclude_course: str = None,
                 dry_run: bool = False, weekend: str = None) -> dict:
    """Two-pass search: morning window first, then widen to FALLBACK_MAX_HOUR.

    `weekend` is the combined weekend label used for multi-account coordination
    (e.g. "4/25/2026 - 4/26/2026"). If another account already booked this day
    via shared_state, we short-circuit with skipped=True.
    """
    # Multi-account coordination: skip if this day has already hit MAX bookings
    # across all accounts. Each account books at most one slot/day, so we check
    # both "did this account already book" and "is the day at capacity."
    if weekend:
        is_full, booked_by_list = shared_state.day_already_booked(weekend, day_name)
        if ACCOUNT_ID in booked_by_list:
            print(f"\n  === {day_name.upper()} already booked by THIS account — skipping ===")
            return {"success": False, "details": None, "course": None, "skipped": True}
        if is_full:
            print(f"\n  === {day_name.upper()} already at capacity ({', '.join(booked_by_list)}) — skipping ===")
            return {"success": False, "details": None, "course": None, "skipped": True}

    passes = [("morning", MAX_HOUR), ("fallback", FALLBACK_MAX_HOUR)]

    for pass_label, max_hour in passes:
        print(f"\n  === {day_name.upper()} / {pass_label} pass (until {max_hour}:00) ===")
        courses = list(COURSE_CODES.items())
        random.shuffle(courses)
        print(f"  Course order: {', '.join(name for _, name in courses)}")
        for round_num in range(1, MAX_SEARCH_ROUNDS_PER_PASS + 1):
            print(f"  Round {round_num}/{MAX_SEARCH_ROUNDS_PER_PASS}")
            # Courses to skip this round: the per-account Saturday->Sunday
            # exclusion, plus any course a sibling account has already booked
            # today (best-effort cross-account diversity). Re-read every round
            # so a sibling's just-claimed course is honored as soon as it lands.
            excluded_courses = set()
            if exclude_course:
                excluded_courses.add(exclude_course)
            # Poll shared state between rounds too — sibling accounts may have
            # filled the day to capacity while we were searching this course.
            if weekend:
                is_full, booked_by_list = shared_state.day_already_booked(weekend, day_name)
                if ACCOUNT_ID in booked_by_list:
                    return {"success": False, "details": None, "course": None, "skipped": True}
                if is_full:
                    print(f"  [coord] {day_name} hit capacity ({', '.join(booked_by_list)}) — stopping")
                    return {"success": False, "details": None, "course": None, "skipped": True}
                sibling_courses = shared_state.courses_booked(weekend, day_name)
                if sibling_courses:
                    excluded_courses |= sibling_courses
                    print(f"  [coord] {day_name}: avoiding course(s) already "
                          f"booked today — {', '.join(sorted(sibling_courses))}")
            for course_code, course_name in courses:
                if course_name in excluded_courses:
                    continue
                result = search_and_book_course(
                    page, course_code, course_name, date, num_players,
                    max_hour, blacklist, dry_run=dry_run,
                    weekend=weekend, day_name=day_name,
                )
                if result["success"]:
                    return result
                if result.get("halt_day"):
                    # 2A: a slot may have booked but we couldn't verify.
                    # Stop everything for this day to prevent duplicates.
                    print(f"  [halt] {day_name} halted after possible-but-unverified "
                          f"booking — see notification for manual check")
                    return result
                if result.get("abort_run"):
                    # Cart-bounce circuit breaker tripped — the site is refusing
                    # checkout. Stop; retrying more courses/days won't help.
                    return result
            page.wait_for_timeout(REFRESH_BETWEEN_ROUNDS_MS)
        print(f"  {pass_label} pass exhausted for {day_name}")

    save_debug_screenshot(page, f"no_slots_{day_name}")
    return {"success": False, "details": None, "course": None}


# ======================================================================
# Session + outer loop
# ======================================================================

def run_booking_session(page, results: dict, saturday_date: str, sunday_date: str,
                         num_players: int, dry_run: bool,
                         skip_wait: bool, is_first_session: bool) -> bool:
    """Single booking session. Browser/page is persisted by the caller.

    Lets page-death exceptions propagate so the outer loop can recreate the page.
    """
    queue_mode = "deadline" if (is_first_session and not skip_wait) else "timeout"
    if queue_mode == "deadline":
        print("\n*** QUEUE MODE: deadline (until 8:05 PM) ***\n")

    if not login_with_retry(page, queue_mode=queue_mode):
        print("Login failed — session will retry")
        # Only notify on persistent login failures, not every attempt
        if not is_first_session:
            send_ntfy(f"[{ACCOUNT_DISPLAY_NAME}] login failing",
                      "Repeated login failures — session will retry. Check the logs.",
                      priority="high", tags="warning")
        return False

    if is_first_session:
        send_ntfy(f"[{ACCOUNT_DISPLAY_NAME}] logged in",
                  "Through login + Queue-it. Now waiting for 8:00 PM release.",
                  priority="low", tags="white_check_mark")

    if not skip_wait:
        wait_until_release_time()

    set_rush_mode(True)
    print("  [rush] Rush mode ON — 12s nav timeout for initial burst")

    # After waking up at 8:00 PM, verify we're still logged in before searching
    if not is_authenticated(page):
        print("Session no longer authenticated after release-wait — re-authenticating")
        if not login_with_retry(page, queue_mode="timeout"):
            send_ntfy(f"[{ACCOUNT_DISPLAY_NAME}] re-auth failed after 8 PM",
                      "Session expired during wait and re-login failed. Bot will retry.",
                      priority="high", tags="warning")
            return False

    blacklist: set = set()
    weekend = f"{saturday_date} - {sunday_date}"

    def course_of(result):
        course = result.get("course")
        return course if isinstance(course, str) else None

    def book_day(day_key, date, day_name, exclude_course=None):
        if results[day_key]["success"]:
            print(f"\n=== {day_name.upper()} already booked from prior session — skipping ===")
            return
        if results[day_key].get("halt_day"):
            # 2A: a prior session halted this day after a possible-but-unverified
            # booking. Don't re-attempt across session retries — that's how
            # duplicates pile up.
            print(f"\n=== {day_name.upper()} halted in prior session (unverified booking) — skipping ===")
            return
        print(f"\n=== BOOKING {day_name.upper()} ===")
        results[day_key] = try_book_day(
            page, date, day_name, num_players, blacklist,
            exclude_course=exclude_course, dry_run=dry_run,
            weekend=weekend,
        )
        # Player-count fallback: if no slots for num_players, retry with fewer.
        # 2A: skip the fallback if try_book_day halted on an unverified booking
        # — we may have already committed and don't want to risk a second one.
        if (not results[day_key]["success"]
                and not results[day_key].get("skipped")
                and not results[day_key].get("halt_day")
                and not results[day_key].get("abort_run")
                and FALLBACK_NUM_PLAYERS is not None
                and FALLBACK_NUM_PLAYERS < num_players):
            print(f"\n  === {day_name.upper()} / retrying with {FALLBACK_NUM_PLAYERS} players ===")
            results[day_key] = try_book_day(
                page, date, day_name, FALLBACK_NUM_PLAYERS, blacklist,
                exclude_course=exclude_course, dry_run=dry_run,
                weekend=weekend,
            )
        # Persist state after each day — survives a crash mid-run.
        # Also persist halt_day so session retries don't re-attempt a day where
        # we may have already booked but couldn't verify.
        if results[day_key]["success"] or results[day_key].get("halt_day"):
            save_state(saturday_date, sunday_date, results)
        if results[day_key]["success"]:
            send_ntfy(
                f"[{ACCOUNT_DISPLAY_NAME}] {day_name.capitalize()} booked",
                f"{results[day_key]['details']}",
                priority="default", tags="golf,white_check_mark",
            )

    book_day("saturday", saturday_date, "saturday")

    if session_circuit_tripped():
        print("\n*** Cart-bounce circuit breaker tripped — aborting session "
              "(site refusing checkout); skipping Sunday ***")
        return False

    if _rush_mode:
        set_rush_mode(False)
        print("  [rush] Rush mode OFF — switching to normal 30s nav timeout")

    book_day("sunday", sunday_date, "sunday",
             exclude_course=course_of(results["saturday"]))

    return results["saturday"]["success"] and results["sunday"]["success"]


def run_booking(args) -> dict:
    """Main routine. Browser is launched once and persisted across session retries."""
    if not USERNAME or not PASSWORD:
        print("ERROR: Missing GOLF_USERNAME / GOLF_PASSWORD in .env")
        return {
            "saturday": {"success": False, "details": None, "course": None},
            "sunday": {"success": False, "details": None, "course": None},
        }

    saturday_date, sunday_date = get_next_weekend_dates()

    # Load prior state — lets a crashed run resume instead of redoing work
    results = load_state(saturday_date, sunday_date)

    print(f"Target dates: Saturday {saturday_date}, Sunday {sunday_date}")

    # Launch notification — only when skipping-wait is off (real scheduled runs)
    if not args.now:
        send_ntfy(
            f"[{ACCOUNT_DISPLAY_NAME}] launched",
            f"Running for Sat {saturday_date} / Sun {sunday_date}. "
            "Will book 4 players, falling back to 2 if needed.",
            priority="low", tags="rocket",
        )
    print(f"Players: {args.players} | Max time: {args.max_time}s | "
          f"Dry run: {args.dry_run} | Headful: {args.headful}")

    start_time = time.time()
    run_started_iso = datetime.now().isoformat(timespec="seconds")
    session_count = 0
    reset_session_circuit()

    # Watchdog — urgent notification if the bot appears stuck
    watchdog = Watchdog(log_path=BOOKING_LOG_PATH)
    watchdog.start()

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=not args.headful)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        stealth_sync(page)

        def new_page():
            """Create a fresh page in the same browser context."""
            nonlocal page
            try:
                page.close()
            except Exception:
                pass
            page = context.new_page()
            stealth_sync(page)
            print("  [recovery] Created fresh browser page")
            return page

        try:
            while True:
                session_count += 1
                elapsed = time.time() - start_time

                if elapsed > args.max_time:
                    print(f"\n*** Max time ({args.max_time}s) exceeded ***")
                    break
                if results["saturday"]["success"] and results["sunday"]["success"]:
                    print("\n*** Both days booked! ***")
                    break
                # 2A: stop retrying sessions if every day is resolved (either
                # booked or halted for manual review). Without this we'd burn
                # the remaining budget repeatedly logging in just to skip both
                # halted days each session.
                sat_done = (results["saturday"]["success"]
                            or results["saturday"].get("halt_day"))
                sun_done = (results["sunday"]["success"]
                            or results["sunday"].get("halt_day"))
                if sat_done and sun_done:
                    print("\n*** Both days resolved (booked or halted for review) ***")
                    break

                print(f"\n{'=' * 50}")
                print(f"SESSION {session_count} (elapsed: {int(elapsed)}s)")
                print(f"{'=' * 50}")

                try:
                    done = run_booking_session(
                        page, results, saturday_date, sunday_date,
                        num_players=args.players,
                        dry_run=args.dry_run,
                        skip_wait=args.now,
                        is_first_session=(session_count == 1),
                    )
                except Exception as e:
                    # Page or browser context died — recover with a fresh page
                    print(f"\n  [recovery] Session crashed: {e}")
                    page = new_page()
                    done = False

                if done:
                    break

                if session_circuit_tripped():
                    print("\n*** Cart-bounce circuit breaker tripped — the site is "
                          "refusing checkout; stopping run instead of looping ***")
                    send_ntfy(
                        f"[{ACCOUNT_DISPLAY_NAME}] aborted — site refusing checkout",
                        f"Add-to-cart kept bouncing to login with a healthy session "
                        f"({MAX_CART_BOUNCE_REEXPIRY}+ times). Stopped early to avoid a "
                        f"futile retry loop. Slots were visible but not bookable — "
                        f"likely site overload/anti-bot at release. Check manually.",
                        priority="high", tags="warning",
                    )
                    break

                remaining = args.max_time - (time.time() - start_time)
                if remaining <= 10:
                    break
                wait_time = min(10, remaining)
                print(f"\nRetrying in {int(wait_time)}s (budget: {int(remaining)}s left)...")
                time.sleep(wait_time)
        finally:
            try:
                browser.close()
            except Exception:
                pass
            watchdog.stop()

    subject_parts = []
    body_lines = [f"Golf Booking Results for {saturday_date} and {sunday_date}\n"]
    for day in ("saturday", "sunday"):
        name = day.capitalize()
        if results[day]["success"]:
            subject_parts.append(f"{name[:3]}: {results[day]['details']}")
            body_lines.append(f"{name}: BOOKED — {results[day]['details']}")
        else:
            subject_parts.append(f"{name[:3]}: FAILED")
            body_lines.append(f"{name}: No booking made")

    both_booked = results["saturday"]["success"] and results["sunday"]["success"]
    any_booked = results["saturday"]["success"] or results["sunday"]["success"]

    if both_booked:
        title = f"[{ACCOUNT_DISPLAY_NAME}] Both days booked!"
        tags = "golf,white_check_mark"
        priority = "default"
    elif any_booked:
        title = f"[{ACCOUNT_DISPLAY_NAME}] Partial success"
        tags = "golf,warning"
        priority = "high"
    else:
        title = f"[{ACCOUNT_DISPLAY_NAME}] No bookings made"
        tags = "golf,x"
        priority = "high"

    notify(title, "\n".join(body_lines), priority=priority, tags=tags)

    # Clear state if fully successful — fresh start next week
    if both_booked:
        clear_state()

    # Record the run in history for the dashboard's past-runs view.
    # Skip when running under multi_bot — the orchestrator writes one
    # aggregate entry instead, to avoid per-account noise.
    if not os.getenv("MULTI_BOT_ACTIVE"):
        append_to_history(
            saturday_date, sunday_date, results,
            run_started=run_started_iso,
            run_ended=datetime.now().isoformat(timespec="seconds"),
        )

    clear_live_screenshot()
    return results


# ======================================================================
# Startup / CLI
# ======================================================================

def check_env() -> bool:
    required = ["GOLF_USERNAME", "GOLF_PASSWORD"]
    email_keys = ["SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFICATION_EMAIL"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"  Required env: MISSING {', '.join(missing)}")
        return False
    print(f"  Required env: OK")
    email_on = all(os.getenv(k) for k in email_keys)
    ntfy_on = bool(NTFY_TOPIC)
    notifications = []
    if ntfy_on: notifications.append("ntfy")
    if email_on: notifications.append("email")
    print(f"  Notifications: {', '.join(notifications) if notifications else 'DISABLED'}")
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="Austin Golf Tee Time Booking Bot")
    parser.add_argument("--now", action="store_true",
                        help="Skip wait for 8pm release (for testing)")
    parser.add_argument("--players", type=int, default=DEFAULT_NUM_PLAYERS,
                        help=f"Number of players (default: {DEFAULT_NUM_PLAYERS})")
    parser.add_argument("--max-time", type=int, default=DEFAULT_MAX_TOTAL_TIME,
                        dest="max_time",
                        help=f"Max total runtime seconds (default: {DEFAULT_MAX_TOTAL_TIME})")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Walk through flow but abort before final checkout")
    parser.add_argument("--headful", action="store_true",
                        help="Show browser window (debugging)")
    parser.add_argument("--account-id", dest="account_id", default=None,
                        help="Account id from accounts.json (default: single-account mode, uses .env creds)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Select credentials + per-account paths before anything else runs.
    active_account = configure_account_context(args.account_id)

    print("=" * 50)
    print("Austin Golf Tee Time Booking Bot")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Account: {active_account['display_name']} (id={active_account['id']})")
    mode = "IMMEDIATE" if args.now else "SCHEDULED (wait for 8pm)"
    if args.dry_run:
        mode += " [DRY-RUN]"
    print(f"Mode: {mode}")
    if not check_env():
        print("FATAL: missing credentials")
        sys.exit(1)
    print("=" * 50)

    results = run_booking(args)

    print("\n" + "=" * 50)
    print("FINAL RESULTS:")
    for day in ("saturday", "sunday"):
        name = day.capitalize()
        if results[day]["success"]:
            print(f"  {name}: SUCCESS — {results[day]['details']}")
        else:
            print(f"  {name}: No booking")
    print("=" * 50)
