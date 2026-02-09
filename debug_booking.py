#!/usr/bin/env python3
"""Debug script - test full login and navigate to tee time search."""

import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

USERNAME = os.getenv("GOLF_USERNAME")
PASSWORD = os.getenv("GOLF_PASSWORD")
BASE_URL = "https://txaustinweb.myvscloud.com/webtrac/web"
SEARCH_URL = "https://txaustinweb.myvscloud.com/webtrac/web/search.html?display=detail&module=GR&secondarycode=2"

print("=== Golf Booking Debug ===\n")

with sync_playwright() as p:
    browser = p.firefox.launch(headless=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()

    # Step 1: Load homepage
    print("1. Loading homepage...")
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    print(f"   URL: {page.url}")

    # Step 2: Click Sign In
    print("\n2. Clicking Sign In...")
    sign_in = page.locator("a:has-text('Sign In')").first
    sign_in.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    page.screenshot(path="debug_login_page.png")
    print(f"   URL: {page.url}")
    print("   Screenshot: debug_login_page.png")

    # Step 3: Login
    print("\n3. Logging in...")
    page.fill("#weblogin_username", USERNAME)
    page.fill("#weblogin_password", PASSWORD)
    page.locator("input[type='submit'], button[type='submit']").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)
    page.screenshot(path="debug_after_login.png")
    print(f"   URL: {page.url}")
    print("   Screenshot: debug_after_login.png")

    if "login" in page.url.lower():
        print("   ERROR: Still on login page - login may have failed")
    else:
        print("   SUCCESS: Login completed!")

    # Step 4: Go to Tee Time Search
    print("\n4. Navigating to Tee Time Search...")
    page.goto(SEARCH_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)
    page.screenshot(path="debug_tee_time_search.png", full_page=True)
    print(f"   URL: {page.url}")
    print("   Screenshot: debug_tee_time_search.png")

    # Step 5: Analyze the search page
    print("\n5. Analyzing search page elements...")

    # Find all select dropdowns
    print("\n   SELECT dropdowns:")
    selects = page.locator("select").all()
    for sel in selects:
        try:
            name = sel.get_attribute("name") or ""
            sel_id = sel.get_attribute("id") or ""
            # Get first few options
            options = sel.locator("option").all()[:5]
            opt_texts = [o.text_content().strip()[:30] for o in options]
            print(f"   - name='{name}' id='{sel_id}'")
            print(f"     options: {opt_texts}")
        except:
            pass

    # Find buttons
    print("\n   BUTTONS:")
    buttons = page.locator("button, input[type='submit'], input[type='button']").all()
    for btn in buttons[:10]:
        try:
            text = btn.text_content().strip() or btn.get_attribute("value") or ""
            if text:
                print(f"   - '{text[:40]}'")
        except:
            pass

    # Save HTML
    html = page.content()
    with open("debug_search_page.html", "w") as f:
        f.write(html)
    print("\n   HTML saved: debug_search_page.html")

    browser.close()

print("\n=== Done! Check the screenshots ===")
