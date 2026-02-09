#!/usr/bin/env python3
"""
Test script to book a single tee time for Friday to verify the full flow works.
"""

import os
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

USERNAME = os.getenv("GOLF_USERNAME")
PASSWORD = os.getenv("GOLF_PASSWORD")
BASE_URL = "https://txaustinweb.myvscloud.com/webtrac/web"
SEARCH_URL = f"{BASE_URL}/search.html?display=detail&module=GR"

# Test booking - EDIT THESE VALUES
TEST_DATE = "2/06/2026"  # Friday
TEST_COURSE_CODE = "4"  # Lions
TEST_COURSE_NAME = "Lions"
TEST_TIME = "12:00 PM"  # Afternoon time
NUM_PLAYERS = 4

print("=" * 50)
print("Golf Booking Test - Single Tee Time")
print(f"Date: {TEST_DATE}")
print(f"Course: {TEST_COURSE_NAME}")
print(f"Target time: {TEST_TIME}")
print(f"Players: {NUM_PLAYERS}")
print("=" * 50)

with sync_playwright() as p:
    browser = p.firefox.launch(headless=False)  # Visible so you can watch
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()

    # Step 1: Login
    print("\n1. Logging in...")
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    page.click("a:has-text('Sign In')")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    page.fill("#weblogin_username", USERNAME)
    page.fill("#weblogin_password", PASSWORD)
    page.locator("input[type='submit'], button[type='submit']").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Click Continue with Login if present
    continue_btn = page.locator("button:has-text('Continue'), button:has-text('Continue with Login')")
    if continue_btn.count() > 0:
        continue_btn.first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

    print("   Login successful!")

    # Step 2: Go to search page
    print("\n2. Searching for tee times...")
    search_url = (
        f"{SEARCH_URL}"
        f"&secondarycode={TEST_COURSE_CODE}"
        f"&begindate={TEST_DATE}"
        f"&begintime=12:00 pm"
        f"&numberofplayers={NUM_PLAYERS}"
        f"&numberofholes=18"
        f"&Action=Start"
    )
    page.goto(search_url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Click search
    search_btn = page.locator("#grwebsearch_buttonsearch")
    if search_btn.count() > 0:
        search_btn.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

    page.screenshot(path="test_search_results.png")
    print("   Screenshot saved: test_search_results.png")

    # Check for "no results" message specifically in the results area
    no_results_elem = page.locator("#grwebsearch_noresultsheader")
    if no_results_elem.count() > 0 and no_results_elem.is_visible():
        print("   No tee times available for this date")
        print("   Check test_search_results.png to see the page")
        input("\nPress Enter to close browser...")
        browser.close()
        exit()

    # Check if there are any tee time rows with "Available"
    available_rows = page.locator("tr:has-text('Available')")
    print(f"   Found {available_rows.count()} rows with 'Available'")

    if available_rows.count() == 0:
        print("   No available tee times found on page")
        print("   Check test_search_results.png to see what's shown")
        input("\nPress Enter to close browser...")
        browser.close()
        exit()

    # Step 3: Find and click the tee time
    print(f"\n3. Looking for {TEST_TIME} tee time...")

    # Find rows with available tee times (any time after 4pm is fine for this test)
    rows = page.locator("tr:has-text('Available')").all()
    target_row = None

    print(f"   Checking {len(rows)} available rows...")

    for row in rows:
        row_text = row.text_content()
        # Look for any PM time (afternoon)
        if "pm" in row_text.lower() and "available" in row_text.lower():
            target_row = row
            print(f"   Found afternoon tee time: {row_text[:100]}...")
            break

    if not target_row and len(rows) > 0:
        # Just use first available row
        target_row = rows[0]
        print(f"   Using first available: {target_row.text_content()[:100]}...")

    if not target_row:
        print("   No available tee times found!")
        input("\nPress Enter to close browser...")
        browser.close()
        exit()

    # Click the cart button
    print("\n4. Clicking add to cart...")

    # List all buttons/links in the row for debugging
    buttons = target_row.locator("button, a").all()
    print(f"   Found {len(buttons)} clickable elements in row")

    for i, btn in enumerate(buttons):
        try:
            btn_text = btn.text_content().strip()[:30] if btn.text_content() else ""
            btn_class = btn.get_attribute("class") or ""
            print(f"   [{i}] text='{btn_text}' class='{btn_class[:40]}'")
        except:
            pass

    # Try to find and click the Add To Cart button
    cart_btn = None

    # Method 1: Look for button with "Add" or "Cart" text
    for btn in buttons:
        try:
            btn_text = (btn.text_content() or "").lower()
            if "add" in btn_text or "cart" in btn_text:
                cart_btn = btn
                print(f"   Found cart button by text")
                break
        except:
            continue

    # Method 2: Use the last button (usually the action button)
    if not cart_btn and len(buttons) > 0:
        cart_btn = buttons[-1]
        print(f"   Using last button in row")

    if not cart_btn:
        print("   Could not find cart button!")
        input("\nPress Enter to close browser...")
        browser.close()
        exit()

    # Try clicking with JavaScript if normal click fails
    try:
        cart_btn.click(timeout=5000)
    except:
        print("   Normal click failed, trying JavaScript click...")
        cart_btn.evaluate("el => el.click()")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    page.screenshot(path="test_after_cart_click.png")
    print("   Screenshot saved: test_after_cart_click.png")
    print(f"   Current URL: {page.url}")

    # Step 4: Check if we need to click "One Click to Finish"
    print("\n5. Looking for 'One Click to Finish'...")
    one_click = page.locator("button:has-text('One Click'), a:has-text('One Click')")
    if one_click.count() > 0 and one_click.first.is_visible():
        print("   Found 'One Click to Finish' - clicking...")
        one_click.first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        page.screenshot(path="test_after_one_click.png")
        print("   Screenshot saved: test_after_one_click.png")
    else:
        print("   'One Click to Finish' not found - may need manual checkout")

    # Step 5: Check result
    print("\n6. Checking result...")
    print(f"   Final URL: {page.url}")

    if "confirmation" in page.url.lower() or "receipt" in page.url.lower():
        print("\n   *** BOOKING SUCCESSFUL! ***")
    elif "cart" in page.url.lower():
        print("\n   Added to cart - may need to complete checkout manually")
    else:
        print("\n   Check the browser window to see what happened")

    page.screenshot(path="test_final_result.png")
    print("   Final screenshot saved: test_final_result.png")

    input("\nPress Enter to close browser...")
    browser.close()

print("\nTest complete!")
