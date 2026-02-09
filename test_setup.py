#!/usr/bin/env python3
"""
Test script to verify bot setup and configuration.
Run this before the actual booking to make sure everything works.
"""

import os
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


def check_env():
    """Check environment variables are set."""
    print("Checking environment variables...")

    required = ["GOLF_USERNAME", "GOLF_PASSWORD"]
    optional = ["SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFICATION_EMAIL"]

    all_good = True

    for var in required:
        value = os.getenv(var)
        if value:
            print(f"  [OK] {var} is set")
        else:
            print(f"  [MISSING] {var} - REQUIRED")
            all_good = False

    for var in optional:
        value = os.getenv(var)
        if value:
            print(f"  [OK] {var} is set")
        else:
            print(f"  [WARN] {var} - not set (email notifications disabled)")

    return all_good


def check_playwright():
    """Check Playwright is installed and browsers available."""
    print("\nChecking Playwright installation...")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            page = browser.new_page()
            page.goto("https://example.com")
            title = page.title()
            browser.close()

        print(f"  [OK] Playwright working with Firefox (loaded example.com: '{title}')")
        return True

    except Exception as e:
        print(f"  [ERROR] Playwright failed: {e}")
        print("  Run: python3 -m playwright install firefox")
        return False


def check_dates():
    """Show upcoming weekend dates."""
    print("\nUpcoming weekend dates:")

    # Import the function from bot
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from bot import get_next_weekend_dates

    saturday, sunday = get_next_weekend_dates()
    print(f"  Saturday: {saturday}")
    print(f"  Sunday: {sunday}")


def test_login():
    """Test login to Vermont Systems."""
    print("\nTesting login to Vermont Systems...")

    username = os.getenv("GOLF_USERNAME")
    password = os.getenv("GOLF_PASSWORD")

    if not username or not password:
        print("  [SKIP] Credentials not set")
        return False

    try:
        from playwright.sync_api import sync_playwright
        from config import BASE_URL

        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()

            print(f"  Loading {BASE_URL}...")
            page.goto(BASE_URL)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            # Click sign in link
            print("  Clicking Sign In...")
            page.click("a:has-text('Sign In')")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            # Fill credentials using exact field IDs
            print("  Entering credentials...")
            page.fill("#weblogin_username", username)
            page.fill("#weblogin_password", password)

            # Submit
            print("  Clicking submit...")
            page.locator("input[type='submit'], button[type='submit']").first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            # Click "Continue with Login" if present
            continue_btn = page.locator("button:has-text('Continue'), a:has-text('Continue with Login'), button:has-text('Continue with Login')")
            if continue_btn.count() > 0:
                print("  Clicking 'Continue with Login'...")
                continue_btn.first.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(3000)

            # Check result
            current_url = page.url.lower()
            print(f"  Current URL: {current_url[:80]}...")

            # Check for any error messages on page
            page_text = page.content()
            if "invalid" in page_text.lower() or "incorrect" in page_text.lower():
                print("  [FAILED] Invalid credentials")
                browser.close()
                return False

            # Success if we're on splash page or any page that's not login
            if "login.html" in current_url:
                # Check for error message
                error = page.locator(".error, .alert, [class*='error'], .message")
                if error.count() > 0:
                    print(f"  [FAILED] Login error: {error.first.text_content()[:100]}")
                else:
                    print("  [FAILED] Login failed - still on login page (check debug_after_submit.png)")
                browser.close()
                return False

            print("  [OK] Login successful!")
            browser.close()
            return True

    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def main():
    print("=" * 50)
    print("Golf Booking Bot - Setup Test")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    env_ok = check_env()
    pw_ok = check_playwright()
    check_dates()

    if env_ok and pw_ok:
        test_login()

    print("\n" + "=" * 50)
    if env_ok and pw_ok:
        print("Setup looks good! Run with: python scheduler.py --now")
    else:
        print("Please fix the issues above before running the bot.")
    print("=" * 50)


if __name__ == "__main__":
    main()
