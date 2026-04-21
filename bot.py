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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth
    stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_screenshots")
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")
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
        urllib.request.urlopen(req, timeout=10)
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

    def __init__(self, log_path: str = BOOKING_LOG_PATH,
                 stall_seconds: int = WATCHDOG_STALL_SECONDS):
        self.log_path = log_path
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
        with open(os.path.join(DEBUG_DIR, "live_label.txt"), "w") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} — {label}")
    except Exception:
        pass  # screenshots must never block booking


def clear_live_screenshot() -> None:
    """Remove the live snapshot when the bot finishes so the dashboard shows 'idle'."""
    for path in (LIVE_SCREENSHOT, os.path.join(DEBUG_DIR, "live_label.txt")):
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
        if sat.get("success") or sun.get("success"):
            print(f"  [state] Resuming: Sat={sat.get('details') or 'pending'}, "
                  f"Sun={sun.get('details') or 'pending'}")
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
    """True if the page shows logged-in chrome (Sign Out / My Account link)."""
    if is_in_queue(page) or is_on_login_page(page):
        return False
    try:
        if page.locator("a:has-text('Sign Out'), a:has-text('Logout'), a:has-text('Log Out')").count() > 0:
            return True
        if page.locator("a:has-text('My Account')").count() > 0:
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


def navigate_to_search(page, url: str) -> bool:
    """Navigate to a search URL, handling Queue-it interception and session expiry.

    Returns True if we ended up on the target page authenticated. Any caller
    that uses page.goto() directly risks silently parsing a Queue-it waiting
    room page and seeing zero rows — always route through this helper.

    Loops up to MAX_NAV_RECOVERY_ATTEMPTS times to handle chained failures
    (e.g. Queue-it wait -> session expired -> Queue-it again).
    """
    for attempt in range(1, MAX_NAV_RECOVERY_ATTEMPTS + 1):
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            print(f"  [nav] goto timed out (attempt {attempt})")
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

def build_search_url(course_code: str, date: str, num_players: int) -> str:
    return (
        f"{SEARCH_URL}"
        f"&secondarycode={course_code}"
        f"&begindate={date}"
        f"&begintime=07:00 am"
        f"&numberofplayers={num_players}"
        f"&numberofholes=18"
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

# Reservation history page — used for post-booking verification
HISTORY_URL = f"{BASE_URL}/history.html?historyoption=inquiry"


def verify_booking_on_page(page, slot: dict, page_text: str) -> bool:
    """Quick first-pass verification: does the current page reference our slot?"""
    time_lower = slot["time"].lower().replace(" ", "")
    course_lower = slot["course"].lower()
    # The receipt page shows time like "8:01A" or "8:01 AM" — normalize both
    page_normalized = page_text.replace(" ", "").replace(":", ":")
    time_found = time_lower in page_normalized or slot["time"].lower() in page_text
    course_found = course_lower in page_text
    return time_found and course_found


def verify_booking_via_history(page, slot: dict) -> bool:
    """Definitive verification: navigate to reservation history page and look for slot.

    This is the ground-truth check — if the booking doesn't appear here, it didn't happen.
    """
    try:
        page.goto(HISTORY_URL, timeout=15000, wait_until="domcontentloaded")
    except PlaywrightTimeout:
        print(f"    [verify] History page timeout — assuming booking failed")
        return False

    # If we got redirected to login or queue, something's wrong
    if is_on_login_page(page) or is_in_queue(page):
        print(f"    [verify] Lost session during verification — assuming booking failed")
        return False

    try:
        content = page.content().lower()
    except Exception:
        return False

    # Look for slot time, course, and date
    time_lower = slot["time"].lower()
    course_lower = slot["course"].lower()
    date_str = slot["date"]  # e.g. "4/25/2026"

    # Normalize date variations: site might show "04/25/2026" or "4/25/2026"
    date_parts = date_str.split("/")
    date_padded = f"{date_parts[0].zfill(2)}/{date_parts[1].zfill(2)}/{date_parts[2]}"

    date_found = date_str in content or date_padded in content
    time_found = time_lower in content

    # Time on receipt can be "8:01A" instead of "8:01 AM" — also check condensed form
    time_condensed = slot["time"].replace(" AM", "A").replace(" PM", "P").lower()
    time_found = time_found or time_condensed in content

    course_found = course_lower in content

    print(f"    [verify] history check — date:{date_found} time:{time_found} course:{course_found}")
    return date_found and time_found and course_found


def attempt_booking_click(page, slot: dict, dry_run: bool = False) -> str:
    """Click cart button for a slot and determine result.

    Returns one of: 'booked', 'taken', 'dry_run', 'session_expired', 'failed'.

    IMPORTANT: only returns 'booked' if a positive confirmation signal is found
    (receipt/confirmation URL or confirmation text on page). Never assumes
    "not on search page = booked" — that caused false positives when the slot
    was already taken and the site showed an error page we didn't recognize.
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
    for sel in (
        "button:has-text('One Click')",
        "a:has-text('One Click')",
        "input[value*='One Click']",
        "#oneclickfinish",
    ):
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
        # VERIFY: the confirmation page should reference our specific slot.
        # Receipt pages show course name and time — check they match.
        if verify_booking_on_page(page, slot, page_text):
            return "booked"
        else:
            # Confirmation URL/text but slot info missing — suspicious
            print(f"    [book] Confirmation page shown but slot not verified")
            save_debug_screenshot(page, f"unverified_{slot['time'].replace(' ', '_')}")
            return "failed"

    # If we're still on addtocart.html, the checkout didn't complete
    if "addtocart" in final_url:
        if not clicked_finish:
            print(f"    [book] On cart page but couldn't find 'One Click Finish' button")
        else:
            print(f"    [book] Clicked finish but still on cart page — slot likely taken")
        save_debug_screenshot(page, f"cart_stuck_{slot['time'].replace(' ', '_')}")
        return "failed"

    # Unknown page — do NOT assume booked. Screenshot for debugging.
    print(f"    [book] Ambiguous outcome (URL: {page.url[:60]})")
    save_debug_screenshot(page, f"ambiguous_{slot['time'].replace(' ', '_')}")
    return "failed"


# ======================================================================
# Per-course search-and-book
# ======================================================================

def search_and_book_course(page, course_code: str, course_name: str, date: str,
                            num_players: int, max_hour: int, blacklist: set,
                            dry_run: bool = False) -> dict:
    """Search one course and try to book the best available slot."""
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

        print(f"  [book] {slot['time']} at {course_name}...", end=" ", flush=True)
        update_live_screenshot(page, f"attempting {slot['time']} at {course_name}")
        status = attempt_booking_click(page, slot, dry_run=dry_run)
        update_live_screenshot(page, f"{slot['time']} @ {course_name}: {status}")

        if status == "booked":
            print("BOOKED! — verifying...", end=" ", flush=True)
            # Ground-truth check: navigate to reservation history and confirm
            if verify_booking_via_history(page, slot):
                print("VERIFIED ✓")
                return {
                    "success": True,
                    "details": f"{slot['time']} at {course_name}",
                    "course": course_name,
                }
            else:
                print("VERIFICATION FAILED — treating as not booked")
                save_debug_screenshot(page, f"verify_failed_{slot['time'].replace(' ', '_')}")
                blacklist.add(key)
                # Re-navigate to search page to continue trying other slots
                if not navigate_to_search(page, url):
                    break
                continue

        if status == "dry_run":
            print("DRY-RUN OK")
            return {
                "success": True,
                "details": f"[DRY-RUN] {slot['time']} at {course_name}",
                "course": course_name,
            }

        if status == "session_expired":
            print("session expired")
            if not login_with_retry(page, queue_mode="timeout"):
                return result
            if not navigate_to_search(page, url):
                return result
            # DOM state is stale now — bail to outer loop
            break

        # taken / failed: blacklist and try next slot (requires re-nav to refresh DOM)
        print("taken" if status == "taken" else "failed")
        blacklist.add(key)
        if not navigate_to_search(page, url):
            break

    return result


# ======================================================================
# Day-level orchestration (two-pass: morning then fallback window)
# ======================================================================

def try_book_day(page, date: str, day_name: str, num_players: int,
                 blacklist: set, exclude_course: str = None,
                 dry_run: bool = False) -> dict:
    """Two-pass search: morning window first, then widen to FALLBACK_MAX_HOUR."""
    passes = [("morning", MAX_HOUR), ("fallback", FALLBACK_MAX_HOUR)]

    for pass_label, max_hour in passes:
        print(f"\n  === {day_name.upper()} / {pass_label} pass (until {max_hour}:00) ===")
        for round_num in range(1, MAX_SEARCH_ROUNDS_PER_PASS + 1):
            print(f"  Round {round_num}/{MAX_SEARCH_ROUNDS_PER_PASS}")
            for course_code, course_name in COURSE_CODES.items():
                if exclude_course and course_name == exclude_course:
                    continue
                result = search_and_book_course(
                    page, course_code, course_name, date, num_players,
                    max_hour, blacklist, dry_run=dry_run,
                )
                if result["success"]:
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
            send_ntfy("Golf Bot: login failing",
                      "Repeated login failures — session will retry. Check the logs.",
                      priority="high", tags="warning")
        return False

    if is_first_session:
        send_ntfy("Golf Bot: logged in",
                  "Through login + Queue-it. Now waiting for 8:00 PM release.",
                  priority="low", tags="white_check_mark")

    if not skip_wait:
        wait_until_release_time()

    # After waking up at 8:00 PM, verify we're still logged in before searching
    if not is_authenticated(page):
        print("Session no longer authenticated after release-wait — re-authenticating")
        if not login_with_retry(page, queue_mode="timeout"):
            send_ntfy("Golf Bot: re-auth failed after 8 PM",
                      "Session expired during wait and re-login failed. Bot will retry.",
                      priority="high", tags="warning")
            return False

    blacklist: set = set()

    def course_of(result):
        course = result.get("course")
        return course if isinstance(course, str) else None

    def book_day(day_key, date, day_name, exclude_course=None):
        if results[day_key]["success"]:
            print(f"\n=== {day_name.upper()} already booked from prior session — skipping ===")
            return
        print(f"\n=== BOOKING {day_name.upper()} ===")
        results[day_key] = try_book_day(
            page, date, day_name, num_players, blacklist,
            exclude_course=exclude_course, dry_run=dry_run,
        )
        # Player-count fallback: if no slots for num_players, retry with fewer
        if (not results[day_key]["success"]
                and FALLBACK_NUM_PLAYERS is not None
                and FALLBACK_NUM_PLAYERS < num_players):
            print(f"\n  === {day_name.upper()} / retrying with {FALLBACK_NUM_PLAYERS} players ===")
            results[day_key] = try_book_day(
                page, date, day_name, FALLBACK_NUM_PLAYERS, blacklist,
                exclude_course=exclude_course, dry_run=dry_run,
            )
        # Persist state after each day — survives a crash mid-run
        if results[day_key]["success"]:
            save_state(saturday_date, sunday_date, results)
            send_ntfy(
                f"Golf Bot: {day_name.capitalize()} booked",
                f"{results[day_key]['details']}",
                priority="default", tags="golf,white_check_mark",
            )

    book_day("saturday", saturday_date, "saturday")
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
            "Golf Bot: launched",
            f"Running for Sat {saturday_date} / Sun {sunday_date}. "
            "Will book 4 players, falling back to 2 if needed.",
            priority="low", tags="rocket",
        )
    print(f"Players: {args.players} | Max time: {args.max_time}s | "
          f"Dry run: {args.dry_run} | Headful: {args.headful}")

    start_time = time.time()
    session_count = 0

    # Watchdog — urgent notification if the bot appears stuck
    watchdog = Watchdog()
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
        title = "Golf Bot: Both days booked!"
        tags = "golf,white_check_mark"
        priority = "default"
    elif any_booked:
        title = "Golf Bot: Partial success"
        tags = "golf,warning"
        priority = "high"
    else:
        title = "Golf Bot: No bookings made"
        tags = "golf,x"
        priority = "high"

    notify(title, "\n".join(body_lines), priority=priority, tags=tags)

    # Clear state if fully successful — fresh start next week
    if both_booked:
        clear_state()

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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    print("=" * 50)
    print("Austin Golf Tee Time Booking Bot")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
