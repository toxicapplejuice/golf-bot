#!/usr/bin/env python3
"""
Austin Municipal Golf Tee Time Booking Bot

Automatically books tee times at Lions, Roy Kizer, or Jimmy Clay
for Saturday/Sunday mornings when they release on Monday at 8pm CT.
"""

import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Retry configuration
MAX_LOGIN_RETRIES = 10  # Keep trying login for a while
LOGIN_RETRY_DELAY = 5   # Seconds between login attempts
MAX_BOOKING_RETRIES = 3  # Retries per booking attempt
MAX_TOTAL_TIME = 600    # 10 minutes max total runtime

# Booking release time (8:00 PM CT)
RELEASE_HOUR = 20  # 8 PM in 24-hour format
RELEASE_MINUTE = 0

# Load .env from script directory (important for cron jobs)
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

from config import (
    BASE_URL,
    SEARCH_URL,
    TARGET_COURSES,
    COURSE_CODES,
    TIME_PRIORITY,
    NUM_PLAYERS,
    MIN_HOUR,
    MAX_HOUR,
)

# Credentials from environment
USERNAME = os.getenv("GOLF_USERNAME")
PASSWORD = os.getenv("GOLF_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL")


def send_email(subject: str, body: str) -> None:
    """Send email notification."""
    if not all([SMTP_SERVER, SMTP_USERNAME, SMTP_PASSWORD, NOTIFICATION_EMAIL]):
        print("Email not configured, skipping notification")
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


def get_next_weekend_dates() -> tuple[str, str]:
    """Get the dates for the upcoming Saturday and Sunday."""
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.weekday() == 5:
        days_until_saturday = 7  # Next Saturday if today is Saturday

    # When running Monday 8pm, we want the coming weekend (5-6 days away)
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)

    return saturday.strftime("%-m/%d/%Y"), sunday.strftime("%-m/%d/%Y")


def parse_time(time_str: str) -> int:
    """Convert time string like '9:00 AM' to minutes since midnight for comparison."""
    match = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.IGNORECASE)
    if not match:
        return 9999  # Invalid time, sort to end

    hour, minute, period = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if period == "PM" and hour != 12:
        hour += 12
    elif period == "AM" and hour == 12:
        hour = 0

    return hour * 60 + minute


def is_time_in_range(time_str: str) -> bool:
    """Check if time is between 8am and 11am (inclusive of 11:xx)."""
    minutes = parse_time(time_str)
    min_minutes = MIN_HOUR * 60  # 8:00 AM = 480
    max_minutes = (MAX_HOUR + 1) * 60  # 12:00 PM = 720 (to include 11:xx)
    return min_minutes <= minutes < max_minutes


def get_time_priority(time_str: str) -> int:
    """Get priority index for a time (lower is better)."""
    # Check exact match first
    if time_str in TIME_PRIORITY:
        return TIME_PRIORITY.index(time_str)

    # Fall back to hour/half-hour-based priority
    # 9am > 9:30am > 10am > 10:30am > 8am > 11am
    minutes = parse_time(time_str)
    hour = minutes // 60
    minute = minutes % 60

    if hour == 9 and minute < 30:
        return 5   # 9:00-9:29
    elif hour == 9:
        return 10  # 9:30-9:59
    elif hour == 10 and minute < 30:
        return 15  # 10:00-10:29
    elif hour == 10:
        return 20  # 10:30-10:59
    elif hour == 8:
        return 30  # 8:00-8:59
    elif hour == 11:
        return 40  # 11:00-11:59
    else:
        return 100  # Outside preferred range


def wait_until_release_time():
    """Wait until 8:00 PM CT if we're early."""
    now = datetime.now()
    release_time = now.replace(hour=RELEASE_HOUR, minute=RELEASE_MINUTE, second=0, microsecond=0)

    if now >= release_time:
        print(f"Already past release time ({RELEASE_HOUR}:{RELEASE_MINUTE:02d}), proceeding immediately")
        return

    wait_seconds = (release_time - now).total_seconds()

    if wait_seconds > 0:
        print(f"\n*** Waiting until {RELEASE_HOUR}:{RELEASE_MINUTE:02d} PM for tee times to release ***")
        print(f"    Current time: {now.strftime('%H:%M:%S')}")
        print(f"    Release time: {release_time.strftime('%H:%M:%S')}")
        print(f"    Waiting {int(wait_seconds)} seconds...")

        # Wait in chunks so we can show progress
        while True:
            now = datetime.now()
            if now >= release_time:
                break
            remaining = (release_time - now).total_seconds()
            if remaining > 10:
                print(f"    {int(remaining)} seconds until release...")
                time.sleep(10)
            elif remaining > 0:
                time.sleep(remaining)
                break
            else:
                break

        print(f"*** Release time reached! Starting booking... ***\n")


def login_once(page) -> bool:
    """Single login attempt to Vermont Systems."""
    try:
        # Go to base URL first with longer timeout for busy periods
        page.goto(BASE_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(500)

        # Click sign in link
        print("  Clicking Sign In...")
        page.click("a:has-text('Sign In')", timeout=15000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(500)

        # Fill login form using exact field IDs
        print("  Entering credentials...")
        page.fill("#weblogin_username", USERNAME, timeout=10000)
        page.fill("#weblogin_password", PASSWORD, timeout=10000)

        # Submit - look for submit button
        submit_btn = page.locator("input[type='submit'], button[type='submit']").first
        submit_btn.click(timeout=10000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(500)

        # Click "Continue with Login" if present
        continue_btn = page.locator("button:has-text('Continue'), a:has-text('Continue with Login'), button:has-text('Continue with Login')")
        if continue_btn.count() > 0:
            print("  Clicking 'Continue with Login'...")
            continue_btn.first.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(500)

        # Verify login succeeded - check if we're still on login page
        if "login.html" in page.url.lower():
            error_msg = page.locator(".error, .alert, [class*='error']")
            if error_msg.count() > 0:
                print(f"  Login failed: {error_msg.first.text_content()}")
            else:
                print("  Login failed - still on login page")
            return False

        print("  Login successful!")
        return True

    except PlaywrightTimeout:
        print("  Login timed out (site may be busy)")
        return False
    except Exception as e:
        print(f"  Login error: {e}")
        return False


def login_with_retry(page, max_retries: int = MAX_LOGIN_RETRIES) -> bool:
    """Log in to Vermont Systems with retry logic for busy periods."""
    print("Logging in...")

    for attempt in range(1, max_retries + 1):
        print(f"\n  Login attempt {attempt}/{max_retries}...")

        if login_once(page):
            return True

        if attempt < max_retries:
            print(f"  Waiting {LOGIN_RETRY_DELAY}s before retry...")
            time.sleep(LOGIN_RETRY_DELAY)

            # Try refreshing the page or going back to base URL
            try:
                page.goto(BASE_URL, timeout=60000)
            except:
                pass

    print(f"  Login failed after {max_retries} attempts")
    return False


def search_tee_times(page, date: str, exclude_course: str = None) -> list[dict]:
    """Search for available tee times on a specific date across all target courses."""
    print(f"Searching for tee times on {date}...")
    if exclude_course:
        print(f"  (excluding {exclude_course})")

    all_tee_times = []

    for course_code, course_name in COURSE_CODES.items():
        # Skip excluded course (to avoid booking same course both days)
        if exclude_course and course_name == exclude_course:
            print(f"  Skipping {course_name} (already booked)")
            continue
        try:
            print(f"  Checking {course_name}...")

            # Navigate to search page with parameters via URL
            search_url = (
                f"{SEARCH_URL}"
                f"&secondarycode={course_code}"
                f"&begindate={date}"
                f"&begintime=07:00 am"
                f"&numberofplayers={NUM_PLAYERS}"
                f"&numberofholes=18"
                f"&Action=Start"
            )
            page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(
                    "tr:has-text('Available'), #grwebsearch_noresultsheader, :text('did not return')",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(300)

            # Click search button to refresh results
            search_btn = page.locator("#grwebsearch_buttonsearch")
            if search_btn.count() > 0:
                search_btn.click(timeout=10000)
                try:
                    page.wait_for_selector(
                        "tr:has-text('Available'), #grwebsearch_noresultsheader, :text('did not return')",
                        timeout=10000,
                    )
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(300)

            # Check if no results message is shown
            no_results = page.locator("#grwebsearch_noresultsheader")
            if no_results.count() > 0 and no_results.is_visible():
                print(f"    No tee times available at {course_name}")
                continue

            # Also check for "did not return" text
            page_content = page.content().lower()
            if "did not return any matching" in page_content:
                print(f"    No tee times available at {course_name}")
                continue

            # Only look at rows that have "Available" - skip "Booked" rows entirely
            available_rows = page.locator("tr:has-text('Available')").all()
            print(f"    Found {len(available_rows)} rows with 'Available' at {course_name}")

            for row in available_rows:
                try:
                    row_text = row.text_content()

                    # Count how many player slots show "Available" in this row
                    # Each player slot is either "Booked" or "Available"
                    available_count = row_text.lower().count("available")

                    if available_count < NUM_PLAYERS:
                        continue

                    # Extract time from this row
                    time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', row_text, re.IGNORECASE)
                    if not time_match:
                        continue

                    # Normalize time format: "09:00 am" -> "9:00 AM"
                    time_str = time_match.group(1).strip().upper()
                    # Remove leading zero from hour
                    if time_str.startswith("0"):
                        time_str = time_str[1:]

                    # Check if time is in our desired range
                    if not is_time_in_range(time_str):
                        continue

                    # Check for duplicates
                    exists = any(
                        t["time"] == time_str and t["course"] == course_name
                        for t in all_tee_times
                    )
                    if exists:
                        continue

                    all_tee_times.append({
                        "time": time_str,
                        "course": course_name,
                        "course_code": course_code,
                        "date": date,
                        "priority": get_time_priority(time_str),
                    })
                    print(f"    Available: {time_str} ({available_count} spots)")

                except Exception as e:
                    continue

        except Exception as e:
            print(f"    Error searching {course_name}: {e}")
            continue

    # Remove duplicates first
    seen = set()
    unique_times = []
    for tt in all_tee_times:
        key = (tt["time"], tt["course"])
        if key not in seen:
            seen.add(key)
            unique_times.append(tt)

    # Sort by: 1) time priority (9am > 10am > 8am > 11am), 2) course as tiebreaker (Lions > Roy Kizer > Jimmy Clay)
    course_priority = {"Lions": 0, "Roy Kizer": 1, "Jimmy Clay": 2}
    unique_times.sort(key=lambda x: (x["priority"], course_priority.get(x["course"], 99)))

    print(f"  Found {len(unique_times)} available tee times for {date}")
    for tt in unique_times[:5]:
        print(f"    - {tt['time']} at {tt['course']}")

    return unique_times


def book_tee_time(page, tee_time: dict) -> bool:
    """Book a specific tee time."""
    print(f"  Attempting to book {tee_time['time']} at {tee_time['course']} on {tee_time['date']}...")

    try:
        # Navigate to search page for this specific course and date
        course_code = tee_time.get("course_code", "")
        search_url = (
            f"{SEARCH_URL}"
            f"&secondarycode={course_code}"
            f"&begindate={tee_time['date']}"
            f"&begintime=06:00 am"
            f"&numberofplayers={NUM_PLAYERS}"
            f"&numberofholes=18"
            f"&Action=Start"
        )
        page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(
                "tr:has-text('Available'), #grwebsearch_noresultsheader, :text('did not return')",
                timeout=10000,
            )
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(300)

        # Click search button
        search_btn = page.locator("#grwebsearch_buttonsearch")
        if search_btn.count() > 0:
            search_btn.click(timeout=10000)
            try:
                page.wait_for_selector(
                    "tr:has-text('Available'), #grwebsearch_noresultsheader, :text('did not return')",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(300)

        # Find the row containing our target time and click its cart button
        clicked = False

        # Find rows with "Available" status
        rows = page.locator("tr:has-text('Available')").all()

        for row in rows:
            try:
                row_text = row.text_content()
                # Check if this row contains our time
                time_variations = [
                    tee_time['time'],
                    tee_time['time'].lower(),
                    tee_time['time'].replace(' ', ''),
                ]
                if any(t.lower() in row_text.lower() for t in time_variations):
                    # Found the row, now find the Add To Cart button
                    buttons = row.locator("button, a").all()

                    cart_btn = None
                    for btn in buttons:
                        btn_text = (btn.text_content() or "").lower()
                        if "add" in btn_text or "cart" in btn_text:
                            cart_btn = btn
                            break

                    # Fallback to last button
                    if not cart_btn and len(buttons) > 0:
                        cart_btn = buttons[-1]

                    if cart_btn:
                        try:
                            cart_btn.click(timeout=10000)
                        except:
                            # Try JavaScript click as fallback
                            cart_btn.evaluate("el => el.click()")

                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                        page.wait_for_timeout(500)
                        clicked = True
                        print(f"    Clicked add to cart for {tee_time['time']}")
                        break
            except:
                continue

        if not clicked:
            # Fallback: try clicking first available Add To Cart button
            cart_btn = page.locator("button:has-text('Add To Cart')").first
            if cart_btn.count() > 0:
                try:
                    cart_btn.click(timeout=10000)
                except:
                    cart_btn.evaluate("el => el.click()")
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.wait_for_timeout(500)
                clicked = True
                print(f"    Clicked first available Add To Cart button")

        if not clicked:
            print(f"    Could not find add to cart button for {tee_time['time']}")
            return False

        # Look for "One Click to Finish" button
        finish_selectors = [
            "button:has-text('One Click')",
            "button:has-text('one click')",
            "a:has-text('One Click')",
            "input[value*='One Click']",
            "#oneclickfinish",
            ".one-click-finish",
        ]

        for sel in finish_selectors:
            try:
                finish_btn = page.locator(sel).first
                if finish_btn.count() > 0 and finish_btn.is_visible():
                    finish_btn.click(timeout=15000)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_timeout(500)
                    print(f"    Clicked 'One Click to Finish'")
                    break
            except:
                continue

        # Check for confirmation
        page.screenshot(path=os.path.join(SCRIPT_DIR, f"booking_result_{tee_time['course']}_{tee_time['date'].replace('/', '-')}.png"))

        current_url = page.url.lower()
        page_text = page.content().lower()

        # FIRST: Check if we're still on search page - this means booking did NOT happen
        if "search.html" in current_url:
            # Check for "In Use" which means someone else took the slot
            if "in use" in page_text or "unavailable" in page_text:
                print(f"    Tee time is In Use/Unavailable - someone else got it, trying next slot...")
            else:
                print(f"    Still on search page - booking didn't complete, trying next slot...")
            return False

        # Check if we're on addtocart page but didn't proceed
        if "addtocart" in current_url:
            print(f"    Add to cart didn't complete - trying next slot...")
            return False

        # Check for "already taken" or similar errors (someone else booked it)
        taken_keywords = ["already taken", "no longer available", "not available", "already booked", "sold out", "taken by another", "in use"]
        for keyword in taken_keywords:
            if keyword in page_text:
                print(f"    Tee time was taken by someone else - trying next slot...")
                return False

        # Check for error messages
        if "the date is invalid" in page_text:
            print(f"    Cannot book - date is invalid (too early to book)")
            return False

        if "unavailable" in page_text and "individual allowance" in page_text:
            print(f"    Cannot book - booking rules prevent this reservation")
            return False

        error_elem = page.locator(".error-message, .alert-danger, .alert-error, .alert-warning")
        if error_elem.count() > 0:
            error_text = error_elem.first.text_content()
            print(f"    Booking error: {error_text[:100]} - trying next slot...")
            return False

        # NOW check for actual success indicators (only if we've left the search page)

        # Check if we're on a confirmation/receipt page (URL-based check is most reliable)
        if "confirmation" in current_url or "receipt" in current_url or "complete" in current_url:
            print(f"    Booking confirmed! (on confirmation page)")
            return True

        # Check if we're on cart page with items
        if "cart" in current_url:
            # Check if cart has items (not empty)
            cart_items = page.locator(".cart-item, [class*='cart-item'], tr[class*='item']")
            if cart_items.count() > 0:
                print(f"    Added to cart - items found")
                return True
            else:
                print(f"    On cart page but no items found")
                return False

        # Check for confirmation text on page (look for specific receipt/confirmation content)
        # Be more specific - look for "Receipt" or "Confirmation Number" text
        if "receipt number" in page_text or "confirmation number" in page_text or "booking confirmed" in page_text:
            print(f"    Booking confirmed! (found confirmation text)")
            return True

        # Check page title for confirmation
        page_title = page.title().lower()
        if "confirmation" in page_title or "receipt" in page_title:
            print(f"    Booking confirmed! (confirmation page title)")
            return True

        print(f"    Booking status unclear (URL: {current_url[:50]}...) - check screenshot")
        return False

    except Exception as e:
        print(f"    Booking error: {e}")
        return False


def search_and_book_immediately(page, course_code: str, course_name: str, date: str) -> dict:
    """Search ONE course and try to book directly from the results page (no reload between attempts)."""
    result = {"success": False, "details": None, "course": None}

    try:
        print(f"  [FAST] Searching {course_name}...")
        search_url = (
            f"{SEARCH_URL}"
            f"&secondarycode={course_code}"
            f"&begindate={date}"
            f"&begintime=07:00 am"
            f"&numberofplayers={NUM_PLAYERS}"
            f"&numberofholes=18"
            f"&Action=Start"
        )
        page.goto(search_url, timeout=30000, wait_until="domcontentloaded")

        # Wait for results table or no-results message to appear
        try:
            page.wait_for_selector(
                "tr:has-text('Available'), #grwebsearch_noresultsheader, :text('did not return')",
                timeout=10000,
            )
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(300)

        # Click search button if present
        search_btn = page.locator("#grwebsearch_buttonsearch")
        if search_btn.count() > 0:
            search_btn.click(timeout=10000)
            try:
                page.wait_for_selector(
                    "tr:has-text('Available'), #grwebsearch_noresultsheader, :text('did not return')",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(300)

        # Check for no results
        no_results = page.locator("#grwebsearch_noresultsheader")
        if no_results.count() > 0 and no_results.is_visible():
            print(f"  [FAST] No tee times at {course_name}")
            return result

        page_content = page.content().lower()
        if "did not return any matching" in page_content:
            print(f"  [FAST] No tee times at {course_name}")
            return result

        # Collect available rows and sort by time priority
        available_rows = page.locator("tr:has-text('Available')").all()
        print(f"  [FAST] Found {len(available_rows)} rows at {course_name}")

        # Build list of (priority, index, time_str) for sorting
        row_info = []
        for idx, row in enumerate(available_rows):
            try:
                row_text = row.text_content()
                available_count = row_text.lower().count("available")
                if available_count < NUM_PLAYERS:
                    continue

                time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', row_text, re.IGNORECASE)
                if not time_match:
                    continue

                time_str = time_match.group(1).strip().upper()
                if time_str.startswith("0"):
                    time_str = time_str[1:]

                if not is_time_in_range(time_str):
                    continue

                row_info.append((get_time_priority(time_str), idx, time_str))
            except Exception:
                continue

        # Sort by priority (best times first)
        row_info.sort(key=lambda x: x[0])

        if not row_info:
            print(f"  [FAST] No suitable times at {course_name}")
            return result

        print(f"  [FAST] Trying {len(row_info)} times: {', '.join(r[2] for r in row_info[:5])}")

        # Rapid-fire: click cart button for each row in priority order
        for priority, row_idx, time_str in row_info:
            try:
                # Re-fetch rows (DOM may have changed from tooltips/etc)
                current_rows = page.locator("tr:has-text('Available')").all()
                if row_idx >= len(current_rows):
                    continue

                row = current_rows[row_idx]
                buttons = row.locator("button, a").all()

                cart_btn = None
                for btn in buttons:
                    btn_text = (btn.text_content() or "").lower()
                    if "add" in btn_text or "cart" in btn_text:
                        cart_btn = btn
                        break
                if not cart_btn and len(buttons) > 0:
                    cart_btn = buttons[-1]

                if not cart_btn:
                    continue

                print(f"  [FAST] Clicking {time_str}...", end=" ", flush=True)
                pre_click_url = page.url

                try:
                    cart_btn.click(timeout=5000)
                except Exception:
                    try:
                        cart_btn.evaluate("el => el.click()")
                    except Exception:
                        print("click failed, next...")
                        continue

                # Wait briefly for navigation or tooltip
                page.wait_for_timeout(700)

                # Check if URL changed (navigated away from search = booking progress!)
                current_url = page.url
                if current_url != pre_click_url and "search.html" not in current_url.lower():
                    print("GOT THROUGH!")

                    # Wait for the new page to load
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(500)

                    # Look for "One Click to Finish" button
                    finish_selectors = [
                        "button:has-text('One Click')",
                        "a:has-text('One Click')",
                        "input[value*='One Click']",
                        "#oneclickfinish",
                    ]
                    for sel in finish_selectors:
                        try:
                            finish_btn = page.locator(sel).first
                            if finish_btn.count() > 0 and finish_btn.is_visible():
                                finish_btn.click(timeout=10000)
                                page.wait_for_load_state("domcontentloaded", timeout=15000)
                                page.wait_for_timeout(500)
                                print(f"  [FAST] Clicked 'One Click to Finish'")
                                break
                        except Exception:
                            continue

                    # Take screenshot
                    page.screenshot(path=os.path.join(SCRIPT_DIR, f"booking_result_{course_name}_{date.replace('/', '-')}.png"))

                    # Check for success
                    final_url = page.url.lower()
                    page_text = page.content().lower()

                    taken_keywords = ["already taken", "no longer available", "not available", "already booked", "sold out", "in use"]
                    was_taken = any(kw in page_text for kw in taken_keywords)

                    if was_taken:
                        print(f"  [FAST] {time_str} was taken, navigating back...")
                        page.goto(pre_click_url, timeout=15000, wait_until="domcontentloaded")
                        page.wait_for_timeout(500)
                        continue

                    if "confirmation" in final_url or "receipt" in final_url or "complete" in final_url:
                        print(f"  [FAST] *** BOOKED: {time_str} at {course_name} ***")
                        result = {"success": True, "details": f"{time_str} at {course_name}", "course": course_name}
                        return result

                    if "receipt number" in page_text or "confirmation number" in page_text or "booking confirmed" in page_text:
                        print(f"  [FAST] *** BOOKED: {time_str} at {course_name} ***")
                        result = {"success": True, "details": f"{time_str} at {course_name}", "course": course_name}
                        return result

                    # If we got past search page and no error, assume success
                    if "search.html" not in final_url:
                        print(f"  [FAST] *** Likely BOOKED: {time_str} at {course_name} (URL: {final_url[:60]}) ***")
                        result = {"success": True, "details": f"{time_str} at {course_name}", "course": course_name}
                        return result

                else:
                    # Still on search page - time was taken or tooltip appeared
                    print("taken, next...")
                    page.wait_for_timeout(200)
                    continue

            except Exception as e:
                print(f"error ({e}), next...")
                continue

        print(f"  [FAST] All times exhausted at {course_name}")
        return result

    except Exception as e:
        print(f"  [FAST] Error on {course_name}: {e}")
        return result


def try_book_day_fast(page, date: str, day_name: str, exclude_course: str = None) -> dict:
    """Phase 1: Aggressive booking - search one course at a time and book immediately."""
    print(f"\n  [FAST] === Phase 1: Fast booking for {day_name} {date} ===")

    for course_code, course_name in COURSE_CODES.items():
        if exclude_course and course_name == exclude_course:
            print(f"  [FAST] Skipping {course_name} (already booked)")
            continue

        result = search_and_book_immediately(page, course_code, course_name, date)
        if result["success"]:
            print(f"\n  [FAST] *** {day_name.upper()} BOOKED: {result['details']} ***")
            return result

    print(f"  [FAST] Phase 1 failed for {day_name} - no bookings made")
    return {"success": False, "details": None, "course": None}


def try_book_day(page, date: str, day_name: str, exclude_course: str = None, max_search_rounds: int = 5) -> dict:
    """Try to book a tee time for a single day with search retries and refreshes."""
    result = {"success": False, "details": None}

    for search_round in range(1, max_search_rounds + 1):
        print(f"\n  Search round {search_round}/{max_search_rounds} for {day_name}...")

        tee_times = search_tee_times(page, date, exclude_course=exclude_course)

        # If no times found, refresh and retry
        if len(tee_times) == 0:
            print(f"  No tee times found - refreshing in 3s...")
            time.sleep(3)
            continue

        # Try each available time
        print(f"  Will try up to {len(tee_times)} tee times...")
        for i, tee_time in enumerate(tee_times):
            print(f"\n  Attempt {i+1}/{len(tee_times)}:")
            if book_tee_time(page, tee_time):
                result = {
                    "success": True,
                    "details": f"{tee_time['time']} at {tee_time['course']}",
                    "course": tee_time['course']
                }
                print(f"\n  *** {day_name.upper()} BOOKED: {tee_time['time']} at {tee_time['course']} ***")
                return result

        # All times were taken - re-search with fresh results
        print(f"\n  All times taken or failed - refreshing to find new availability...")
        time.sleep(2)

    print(f"\n  Could not book any {day_name} tee times after {max_search_rounds} search rounds")
    return result


def run_booking_session(p, results: dict, saturday_date: str, sunday_date: str, skip_wait: bool = False) -> bool:
    """Run a single booking session. Returns True if we should stop retrying."""
    browser = None
    try:
        # Use Firefox to bypass Cloudflare protection
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        # Set longer default timeout for busy periods
        page.set_default_timeout(30000)

        # Login with retry
        if not login_with_retry(page):
            print("Login failed - will restart browser and retry...")
            return False  # Signal to retry with new browser

        # Wait until 8:00 PM if we logged in early (skip with --now flag)
        if not skip_wait:
            wait_until_release_time()

        # ===== PHASE 1: Aggressive fast booking =====
        phase1_start = time.time()
        print("\n" + "=" * 50)
        print("[FAST] PHASE 1: Aggressive fast booking")
        print("=" * 50)

        # Fast book Saturday
        if not results["saturday"]["success"]:
            print("\n=== [FAST] BOOKING SATURDAY (PRIORITY) ===")
            results["saturday"] = try_book_day_fast(page, saturday_date, "saturday")

        # Get Saturday course for exclusion
        saturday_course = None
        if isinstance(results["saturday"].get("course"), str):
            saturday_course = results["saturday"]["course"]
        elif results["saturday"].get("success") and results["saturday"].get("details"):
            saturday_course = results["saturday"]["details"].split(" at ")[-1] if " at " in results["saturday"]["details"] else None

        # Fast book Sunday
        if not results["sunday"]["success"]:
            print("\n=== [FAST] BOOKING SUNDAY ===")
            results["sunday"] = try_book_day_fast(page, sunday_date, "sunday", exclude_course=saturday_course)

        phase1_elapsed = time.time() - phase1_start
        print(f"\n[FAST] Phase 1 completed in {phase1_elapsed:.1f}s")

        # If both booked, we're done
        if results["saturday"]["success"] and results["sunday"]["success"]:
            return True

        # ===== PHASE 2: Fallback to existing search-all-then-book approach =====
        print("\n" + "=" * 50)
        print("PHASE 2: Fallback to standard booking")
        print("=" * 50)

        # Re-read saturday_course in case Phase 1 booked it
        saturday_course = None
        if isinstance(results["saturday"].get("course"), str):
            saturday_course = results["saturday"]["course"]
        elif results["saturday"].get("success") and results["saturday"].get("details"):
            saturday_course = results["saturday"]["details"].split(" at ")[-1] if " at " in results["saturday"]["details"] else None

        if not results["saturday"]["success"]:
            print("\n=== BOOKING SATURDAY (FALLBACK) ===")
            results["saturday"] = try_book_day(page, saturday_date, "saturday")
            # Update saturday_course
            if isinstance(results["saturday"].get("course"), str):
                saturday_course = results["saturday"]["course"]
            elif results["saturday"].get("success") and results["saturday"].get("details"):
                saturday_course = results["saturday"]["details"].split(" at ")[-1] if " at " in results["saturday"]["details"] else None

        if not results["sunday"]["success"]:
            print("\n=== BOOKING SUNDAY (FALLBACK) ===")
            results["sunday"] = try_book_day(page, sunday_date, "sunday", exclude_course=saturday_course)

        # Return True if both days are booked (success) or if we should stop
        return results["saturday"]["success"] and results["sunday"]["success"]

    except Exception as e:
        print(f"Session error: {e}")
        return False  # Signal to retry
    finally:
        if browser:
            try:
                browser.close()
            except:
                pass


def run_booking(skip_wait: bool = False) -> dict:
    """Main booking routine with full retry logic."""
    results = {
        "saturday": {"success": False, "details": None},
        "sunday": {"success": False, "details": None},
    }

    if not USERNAME or not PASSWORD:
        print("ERROR: Missing credentials. Set GOLF_USERNAME and GOLF_PASSWORD in .env")
        return results

    saturday_date, sunday_date = get_next_weekend_dates()
    print(f"Target dates: Saturday {saturday_date}, Sunday {sunday_date}")

    start_time = time.time()
    session_count = 0

    with sync_playwright() as p:
        while True:
            session_count += 1
            elapsed = time.time() - start_time

            # Check if we've exceeded max time
            if elapsed > MAX_TOTAL_TIME:
                print(f"\n*** MAX TIME ({MAX_TOTAL_TIME}s) EXCEEDED - stopping ***")
                break

            # Check if both days are already booked
            if results["saturday"]["success"] and results["sunday"]["success"]:
                print("\n*** Both days booked! ***")
                break

            print(f"\n{'='*50}")
            print(f"BOOKING SESSION {session_count} (elapsed: {int(elapsed)}s)")
            print(f"{'='*50}")

            # Run booking session (only wait for release time on first session)
            done = run_booking_session(p, results, saturday_date, sunday_date, skip_wait=(skip_wait or session_count > 1))

            if done:
                break

            # If not done, wait a bit before retrying
            if not (results["saturday"]["success"] and results["sunday"]["success"]):
                remaining_time = MAX_TOTAL_TIME - elapsed
                if remaining_time > 10:
                    wait_time = min(10, remaining_time)
                    print(f"\nWaiting {int(wait_time)}s before retry (remaining: {int(remaining_time)}s)...")
                    time.sleep(wait_time)
                else:
                    break

    # Send notification
    subject_parts = []
    body_lines = [f"Golf Booking Results for {saturday_date} and {sunday_date}\n"]

    if results["saturday"]["success"]:
        subject_parts.append(f"Sat: {results['saturday']['details']}")
        body_lines.append(f"Saturday: BOOKED - {results['saturday']['details']}")
    else:
        subject_parts.append("Sat: FAILED")
        body_lines.append("Saturday: No booking made")

    if results["sunday"]["success"]:
        subject_parts.append(f"Sun: {results['sunday']['details']}")
        body_lines.append(f"Sunday: BOOKED - {results['sunday']['details']}")
    else:
        subject_parts.append("Sun: FAILED")
        body_lines.append("Sunday: No booking made")

    send_email(
        f"Golf Bot: {' | '.join(subject_parts)}",
        "\n".join(body_lines)
    )

    return results


if __name__ == "__main__":
    skip_wait = "--now" in sys.argv

    print("=" * 50)
    print("Austin Golf Tee Time Booking Bot")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if skip_wait:
        print("Mode: IMMEDIATE (--now flag, skipping wait for 8pm)")
    else:
        print("Mode: SCHEDULED (will wait until 8:00 PM to book)")
    print("=" * 50)

    results = run_booking(skip_wait=skip_wait)

    print("\n" + "=" * 50)
    print("FINAL RESULTS:")
    print(f"  Saturday: {'SUCCESS - ' + results['saturday']['details'] if results['saturday']['success'] else 'No booking'}")
    print(f"  Sunday: {'SUCCESS - ' + results['sunday']['details'] if results['sunday']['success'] else 'No booking'}")
    if results["saturday"]["success"] or results["sunday"]["success"]:
        print("\n  - Fric the green frog")
    print("=" * 50)
