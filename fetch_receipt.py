#!/usr/bin/env python3
"""Recover tee-time confirmation numbers from WebTrac receipt PDFs.

WebTrac's cancel form (teetimecancel.html) requires the confirmation
number(s), which are NOT shown in the reservation history and NOT the same
as the receipt number. They ARE printed on the receipt PDF available from
My Account -> Reprint A Receipt. This tool logs in, downloads that PDF for
a given receipt number, and prints the comma-separated confirmation numbers
in exactly the form the cancel form accepts (proven live 2026-07-04).

Usage:
    # List recent receipts (numbers + dates) to find the one you need
    python3 fetch_receipt.py list --account michael

    # Fetch a receipt's PDF and print its confirmation numbers
    python3 fetch_receipt.py get --account michael --receipt 7419958

The PDF is saved to debug_screenshots/receipt_<number>.pdf for reference.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import bot

try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth

    stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

from playwright.sync_api import sync_playwright

REPRINT_URL = f"{bot.BASE_URL}/reprint.html?option=receipt"
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_screenshots")


def extract_confirmation_numbers(pdf_bytes: bytes) -> list[str]:
    """Pull confirmation-number lists out of a WebTrac receipt PDF.

    The numbers appear in the page's content stream as a comma-separated
    run of 9-digit values (one per player), e.g.
    "324180014,324180016,324180018,324180020". Streams are usually
    FlateDecode-compressed, so try zlib on each stream and fall back to
    the raw bytes. Returns each distinct comma-separated group found, in
    document order — one group per tee time on the receipt.
    """
    groups: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.S):
        raw = m.group(1)
        try:
            text = zlib.decompress(raw)
        except Exception:
            text = raw
        for g in re.findall(rb"(?<!\d)\d{9}(?:,\d{9})*(?!\d)", text):
            s = g.decode("ascii")
            # A lone 9-digit number could be anything; require either a
            # comma group or the Confirmation label nearby to count it.
            if "," in s or b"Confirmation" in text:
                if s not in groups:
                    groups.append(s)
    return groups


def _login(page) -> bool:
    if not bot.login_with_retry(page, queue_mode="timeout"):
        print("LOGIN FAILED")
        return False
    return True


def _goto_receipt_list(page) -> None:
    page.goto(REPRINT_URL, timeout=30000)
    page.wait_for_timeout(2500)


def cmd_list(args) -> None:
    """Print recent receipts (receipt number + date)."""
    bot.configure_account_context(args.account)
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        stealth_sync(page)
        try:
            if not _login(page):
                sys.exit(1)
            _goto_receipt_list(page)
            rows = page.eval_on_selector_all(
                "tr", "els => els.map(e => e.innerText.replace(/\\s+/g, ' ').trim())"
            )
            shown = 0
            for r in rows:
                m = re.match(r"^(\d{7})\s+(\d{2}/\d{2}/\d{4})", r)
                if m:
                    print(f"{m.group(1)}  {m.group(2)}")
                    shown += 1
                if shown >= args.limit:
                    break
            if not shown:
                print("No receipts found")
        finally:
            browser.close()


def cmd_get(args) -> None:
    """Download one receipt PDF and print its confirmation numbers."""
    bot.configure_account_context(args.account)
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900},
                                      accept_downloads=True)
        page = context.new_page()
        stealth_sync(page)
        try:
            if not _login(page):
                sys.exit(1)
            _goto_receipt_list(page)
            link = page.locator(f"tr:has-text('{args.receipt}') a").first
            try:
                href = link.get_attribute("href", timeout=10000)
            except Exception:
                href = None
            if not href:
                print(f"Receipt {args.receipt} not found in the reprint list")
                sys.exit(1)
            os.makedirs(DEBUG_DIR, exist_ok=True)
            path = os.path.join(DEBUG_DIR, f"receipt_{args.receipt}.pdf")
            # Navigating to the reprint URL triggers a download; goto aborts
            # when that happens, which is expected.
            with page.expect_download(timeout=30000) as dinfo:
                try:
                    page.goto(href, timeout=30000)
                except Exception:
                    pass
            dinfo.value.save_as(path)
            print(f"PDF saved: {path}")
            with open(path, "rb") as f:
                groups = extract_confirmation_numbers(f.read())
            if not groups:
                print("No confirmation numbers found in the PDF — open it "
                      "manually and check the 'Confirmation Numbers' line")
                sys.exit(1)
            for g in groups:
                print(f"Confirmation numbers: {g}")
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(
        description="Recover confirmation numbers from WebTrac receipts")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List recent receipts")
    p_list.add_argument("--account", required=True)
    p_list.add_argument("--limit", type=int, default=15)

    p_get = sub.add_parser("get", help="Fetch a receipt PDF + numbers")
    p_get.add_argument("--account", required=True)
    p_get.add_argument("--receipt", required=True,
                       help="Receipt number (from the booking screenshot, "
                            "notification, or `list`)")

    args = parser.parse_args()
    if args.command == "list":
        cmd_list(args)
    elif args.command == "get":
        cmd_get(args)


if __name__ == "__main__":
    main()
