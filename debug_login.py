#!/usr/bin/env python3
"""Debug script - captures screenshots and page info."""

import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
load_dotenv()

USERNAME = os.getenv("GOLF_USERNAME")
PASSWORD = os.getenv("GOLF_PASSWORD")
BASE_URL = "https://txaustinweb.myvscloud.com/webtrac/web"

print("Starting browser (stealth mode to bypass Cloudflare)...\n")

with sync_playwright() as p:
    browser = p.firefox.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()

    print(f"1. Loading {BASE_URL}...")
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Take screenshot
    page.screenshot(path="debug_1_homepage.png", full_page=True)
    print("   Screenshot saved: debug_1_homepage.png")

    # Print what's on the page
    print(f"   Page title: {page.title()}")
    print(f"   Page URL: {page.url}")

    # Look for any login-related links
    print("\n2. Looking for links on page...")
    links = page.locator("a").all()
    print(f"   Found {len(links)} links")

    for link in links[:30]:
        try:
            text = link.text_content().strip()
            href = link.get_attribute("href") or ""
            if text and len(text) < 50:
                print(f"   - '{text}' -> {href[:50]}")
        except:
            pass

    # Try clicking sign in
    print("\n3. Trying to find Sign In link...")

    # Try multiple selectors
    selectors = [
        "a:has-text('Sign In')",
        "a:has-text('Login')",
        "a:has-text('Log In')",
        "text=Sign In",
        "text=Login",
        "[href*='login']",
        ".login",
        "#login",
    ]

    clicked = False
    for sel in selectors:
        try:
            elem = page.locator(sel).first
            if elem.is_visible():
                print(f"   Found with selector: {sel}")
                elem.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2000)
                clicked = True
                break
        except:
            pass

    if clicked:
        page.screenshot(path="debug_2_after_click.png", full_page=True)
        print("   Screenshot saved: debug_2_after_click.png")
    else:
        print("   Could not find sign in link")

    print(f"   Current URL: {page.url}")

    # Look for input fields
    print("\n4. Input fields on current page:")
    inputs = page.locator("input").all()
    for inp in inputs:
        try:
            inp_type = inp.get_attribute("type") or "text"
            inp_name = inp.get_attribute("name") or ""
            inp_id = inp.get_attribute("id") or ""
            inp_placeholder = inp.get_attribute("placeholder") or ""
            print(f"   - type='{inp_type}' name='{inp_name}' id='{inp_id}' placeholder='{inp_placeholder}'")
        except:
            pass

    # Save page HTML for inspection
    html = page.content()
    with open("debug_page.html", "w") as f:
        f.write(html)
    print("\n5. Page HTML saved: debug_page.html")

    browser.close()

print("\nDone! Open debug_1_homepage.png to see what the page looks like.")
