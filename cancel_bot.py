#!/usr/bin/env python3
"""Cancellation bot — cancels a single booked tee time via the logged-in session.

WebTrac's teetimecancel.html is a search-by-confirmation-number form, not a
clickable list of reservations, and the number is NOT shown in the
reservation history — get it from the WebTrac booking email or from the
My Account -> Reprint A Receipt PDF, which lists the per-player confirmation
numbers (the form accepts the full comma-separated list; the receipt number
itself is NOT a confirmation number).
The bot logs into the holding account, opens the bare `teetimecancel.html`
page (which mints a session-specific _csrf_token), fills in the confirmation
number + tee-time selects, submits the search, confirms the cancel, and
verifies the slot is no longer active in the reservation history.

Usage:
    # Run a queued job (spawned by the dashboard on a Cancel click)
    python3 cancel_bot.py run --job <id>
    python3 cancel_bot.py run --job <id> --dry-run --headful

    # One-off manual cancel (no queue entry)
    python3 cancel_bot.py cancel --account christian --date 6/13/2026 \
        --time "8:40 AM" --course "Jimmy Clay" --confirmation R1234567
    python3 cancel_bot.py cancel --account christian --date 6/13/2026 \
        --time "8:40 AM" --course "Jimmy Clay" --confirmation R1234567 \
        --dry-run --headful

Safety:
    --dry-run navigates to the cancel page, fills + submits the search, dumps
    the result, and STOPS before confirming the cancel. A real cancel is
    irreversible, so the dashboard always gates it behind a confirm() dialog
    and the runner verifies the slot is actually gone before reporting success.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

import bot
import cancel_queue

try:
    from playwright_stealth import stealth_sync
except ImportError:
    from playwright_stealth import Stealth

    stealth_sync = lambda page: Stealth().apply_stealth_sync(page)

from playwright.sync_api import sync_playwright

CANCEL_URL = f"{bot.BASE_URL}/teetimecancel.html"
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_screenshots")


# ======================================================================
# teetimecancel.html — form-driving cancellation
#
# Live DOM mapping (2026-06-08) showed teetimecancel.html is NOT a list of
# reservations with cancel buttons. It is a search form:
#   - #webteetimecancel_confirmationnumber   (required text input)
#   - #webteetimecancel_teetimeslot1/2/3      (hour / minute / AM-PM selects)
#   - #webteetimecancel_buttonsearch          (Search submit)
# The confirmation number is required and is NOT recoverable from the
# reservation history — the user supplies it from their WebTrac booking email.
# After Search the site shows the matched reservation + a confirm control;
# that post-Search step is the one piece still mapped provisionally — finalize
# it from the *_aftersearch.html dump that a --dry-run produces.
# ======================================================================


def _dump_cancel_page(page, tag: str) -> None:
    """Save the cancel page's HTML + screenshot so selectors can be mapped."""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = os.path.join(DEBUG_DIR, f"cancel_page_{tag}_{ts}.html")
    try:
        with open(html_path, "w") as f:
            f.write(page.content())
        print(f"  [cancel] page HTML dumped: {html_path}")
    except Exception as e:
        print(f"  [cancel] HTML dump failed: {e}")
    bot.save_debug_screenshot(page, f"cancel_page_{tag}")


def _slot_for(job: dict) -> dict:
    """Build the {date,time,course} slot dict used for text matching.

    course may be None (legacy/manual); an empty string makes the course
    check in bot._slot_in_content a no-op, degrading to date+time matching.
    """
    return {
        "date": job["date"],
        "time": job["time"],
        "course": job.get("course") or "",
    }


def _parse_time_to_slots(time_str: str) -> tuple[str, str, str]:
    """Parse "8:01 AM" -> ("08", "01", "AM") for the three teetimeslot selects.

    Pure + strict: the form's options are hour 01-12, minute 00-59, AM/PM, so
    we zero-pad to two digits and validate the ranges. A wrong pad here would
    target the wrong slot, so this is unit-tested.
    """
    parts = time_str.strip().split()
    if len(parts) != 2:
        raise ValueError(f"expected 'H:MM AM/PM', got {time_str!r}")
    hm, ampm = parts[0], parts[1].upper()
    if ampm not in ("AM", "PM"):
        raise ValueError(f"expected AM/PM, got {parts[1]!r}")
    hmp = hm.split(":")
    if len(hmp) != 2 or not hmp[0].isdigit() or not hmp[1].isdigit():
        raise ValueError(f"bad clock time {hm!r}")
    hour, minute = int(hmp[0]), int(hmp[1])
    if not (1 <= hour <= 12):
        raise ValueError(f"hour out of range: {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"minute out of range: {minute}")
    return f"{hour:02d}", f"{minute:02d}", ampm


def _set_combobox(page, select_id: str, value: str) -> bool:
    """Set a Vermont Systems Vue combobox via its underlying <select> value.

    The Vue widget hides the native <select> (display:none), so select_option
    can time out waiting for visibility. But the POST serializes the native
    select's value, not the widget — so the fallback sets the value directly
    via JS and fires the input/change events Vue listens for. Returns True
    only if the select reads back the requested value; callers must NOT
    submit the form otherwise (a wrong AM/PM silently searches the wrong
    reservation).
    """
    sel = f"#{select_id}"
    try:
        page.select_option(sel, value, timeout=2000)
    except Exception:
        try:
            page.eval_on_selector(
                sel,
                """(el, v) => {
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
        except Exception as e:
            print(f"  [cancel] could not set {select_id}={value!r}: {e}")
            return False
    try:
        got = page.locator(sel).input_value()
    except Exception as e:
        print(f"  [cancel] could not read back {select_id}: {e}")
        return False
    if got != value:
        print(f"  [cancel] {select_id} reads {got!r}, expected {value!r}")
        return False
    return True


def _fill_cancel_form(page, job: dict) -> bool:
    """Fill confirmation number + tee-time selects, then click Search.

    Returns False if a required field can't be set; True once Search is clicked.
    """
    conf = (job.get("confirmation_number") or "").strip()
    if not conf:
        print("  [cancel] job has no confirmation_number — cannot cancel")
        return False
    try:
        hour, minute, ampm = _parse_time_to_slots(job["time"])
    except ValueError as e:
        print(f"  [cancel] cannot parse tee time {job.get('time')!r}: {e}")
        return False
    try:
        page.fill("#webteetimecancel_confirmationnumber", conf, timeout=8000)
    except Exception as e:
        print(f"  [cancel] could not fill confirmation number: {e}")
        return False
    # Abort rather than search with wrong criteria — a mis-set select means
    # the search targets a different (or nonexistent) reservation.
    if not (_set_combobox(page, "webteetimecancel_teetimeslot1", hour)
            and _set_combobox(page, "webteetimecancel_teetimeslot2", minute)
            and _set_combobox(page, "webteetimecancel_teetimeslot3", ampm)):
        return False
    try:
        page.click("#webteetimecancel_buttonsearch", timeout=8000)
    except Exception as e:
        print(f"  [cancel] could not click Search: {e}")
        return False
    page.wait_for_timeout(2000)
    return True


# "no tee times available" is the live message (mapped 2026-07-03):
# "No tee times available for Confirmation Number and Time selected."
# The rest are defensive variants.
_NOT_FOUND_MARKERS = (
    "no tee times available", "no reservation", "not found", "no matching",
    "invalid confirmation", "could not be found", "no records",
)


def _search_found_reservation(page) -> bool:
    """Best-effort: did Search surface a reservation (vs. a not-found message)?

    Provisional until the post-Search page is mapped; verify-gone is the real
    arbiter, so on any read error we assume present and let verify decide.
    """
    try:
        text = (page.content() or "").lower()
    except Exception:
        return True
    return not any(m in text for m in _NOT_FOUND_MARKERS)


# Full cancellation checkout, mapped live 2026-07-03: the Search result page
# asks "Are you sure you want to cancel the tee time slot(s)..." with a
# Continue anchor that routes through addtocart.html?action=cancellation —
# the same cart checkout as booking — then the cart and checkout pages each
# need one more click before confirmation.html is reached.
_CANCEL_FLOW_STEPS = (
    ("confirm", ("#webteetimecancel_buttonaddtocart",
                 "a:has-text('Continue')")),
    ("cart-checkout", ("#webcart_buttoncheckout",
                       "a:has-text('Proceed To Checkout')")),
    ("checkout-submit", ("#webcheckout_buttoncontinue",
                         "button:has-text('Continue')",
                         "a:has-text('Continue')",
                         "input[type='submit']")),
)


def _click_flow_step(page, label: str, selectors: tuple) -> bool:
    """Click the first matching control for one checkout step, with logging.

    No is_visible() pre-check — that raced a still-rendering cart on
    2026-07-03 and silently skipped the click; page.click() already waits
    for actionability.
    """
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() == 0:
                continue
            el.click(timeout=8000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            print(f"  [cancel] {label}: clicked {sel}")
            return True
        except Exception as e:
            print(f"  [cancel] {label}: {sel} failed: {type(e).__name__}")
    print(f"  [cancel] {label}: no control matched")
    return False


def _confirm_cancel(page) -> bool:
    """Walk the cancellation checkout to completion.

    Returns True only if a confirmation/receipt URL is reached. Anything
    less (e.g. cancellation items stranded in the cart) is a failure —
    verify-gone alone can't tell "processed" from "pending in cart", both
    hide the Reserved row, so stopping short here would report a false
    success while the cart could still expire and revive the reservation.
    """
    page.on("dialog", lambda d: d.accept())
    for label, selectors in _CANCEL_FLOW_STEPS:
        if any(m in page.url.lower() for m in bot.BOOKED_URL_MARKERS):
            break  # already at confirmation — flow was shorter than mapped
        if not _click_flow_step(page, label, selectors):
            break
        _dump_cancel_page(page, f"cancel_{label}")
    if any(m in page.url.lower() for m in bot.BOOKED_URL_MARKERS):
        return True
    print(f"  [cancel] checkout did not reach a confirmation page "
          f"(ended on {page.url[:80]})")
    return False


# ======================================================================
# Cancellation flow
# ======================================================================


def cancel_reservation(page, job: dict, dry_run: bool = False) -> str:
    """Cancel one reservation via the teetimecancel.html form. Returns:

      cancelled        — confirmed no longer active in history
      dry_run          — filled the form + searched, stopped before confirming
      failed: <reason> — could not drive the form, or not confirmed gone
    """
    tag = job.get("id", "manual")

    if not bot.navigate_to_search(page, CANCEL_URL):
        return "failed: could not load cancel page"

    # Provisional-DOM safety net: always capture the page so selectors can be
    # validated/finalized from real output.
    _dump_cancel_page(page, f"{tag}_form")

    if not _fill_cancel_form(page, job):
        return "failed: could not drive cancel form"

    _dump_cancel_page(page, f"{tag}_aftersearch")

    if dry_run:
        bot.save_debug_screenshot(page, f"cancel_dryrun_{tag}")
        print("  [cancel] dry-run: searched by confirmation number, "
              "stopping before confirm")
        return "dry_run"

    if not _search_found_reservation(page):
        print("  [cancel] search surfaced no reservation")
        return "failed: no matching reservation (check confirmation number)"

    if not _confirm_cancel(page):
        return "failed: cancellation checkout did not complete"

    # Verify the reservation is actually gone. A cancelled reservation still
    # appears in history with Status flipped to "Cancelled", so a flat text
    # match would falsely report "still present" — key on the live Status via
    # _active_slot_in_content. Distinguish "no longer active" (success) from
    # "couldn't load history" (cannot confirm); never claim success on an
    # unreachable history page.
    content = bot._fetch_history_content(page)
    if content is None:
        return "failed: could not verify (history unreachable)"
    if bot._active_slot_in_content(content, _slot_for(job)):
        return "failed: reservation still active after cancel"
    return "cancelled"


def _run(page, job: dict, dry_run: bool) -> str:
    """Drive one cancellation against an already-logged-in page."""
    print(f"\n=== CANCELLING {job['details']} "
          f"({job.get('account_name') or job['account_id']}) ===")
    bot.update_live_screenshot(page, f"cancel: {job['details']}")
    return cancel_reservation(page, job, dry_run=dry_run)


def _execute(job: dict, dry_run: bool, headful: bool) -> str:
    """Configure account, launch browser, log in, and run the cancellation."""
    bot.configure_account_context(job["account_id"])

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=not headful)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        stealth_sync(page)
        try:
            if not bot.login_with_retry(page, queue_mode="timeout"):
                return "failed: login failed"
            return _run(page, job, dry_run)
        finally:
            try:
                browser.close()
            except Exception:
                pass
            bot.clear_live_screenshot()


# ======================================================================
# CLI commands
# ======================================================================


def cmd_run(args) -> None:
    """Execute a queued cancellation job by ID."""
    job = cancel_queue.get_job(args.job)
    if not job:
        print(f"ERROR: job {args.job!r} not found")
        sys.exit(1)
    if job["status"] not in ("pending", "running"):
        print(f"Job {args.job} is already {job['status']} — nothing to do")
        return

    dry_run = bool(job.get("dry_run")) or args.dry_run
    cancel_queue.update_job(
        job["id"], status="running",
        started_at=datetime.now().isoformat(timespec="seconds"),
    )

    try:
        result = _execute(job, dry_run, args.headful)
    except Exception as e:
        result = f"failed: {type(e).__name__}: {e}"

    _finalize(job, result, dry_run)


def cmd_cancel(args) -> None:
    """One-off manual cancel (not persisted to the queue)."""
    acc = bot.get_account_by_id(args.account)
    job = {
        "id": "manual",
        "account_id": args.account,
        "account_name": acc.get("display_name") if acc else args.account,
        "date": args.date,
        "day": args.day,
        "time": args.time,
        "course": args.course,
        "details": f"{args.time} at {args.course}" if args.course else args.time,
        "confirmation_number": args.confirmation,
        "dry_run": args.dry_run,
    }
    try:
        result = _execute(job, args.dry_run, args.headful)
    except Exception as e:
        result = f"failed: {type(e).__name__}: {e}"
    print(f"\nResult: {result}")
    if result == "cancelled":
        bot.mark_booking_cancelled(
            job["account_id"], job["date"], job["time"], job.get("course")
        )


def _finalize(job: dict, result: str, dry_run: bool) -> None:
    """Record the outcome: update history, the job, and notify."""
    now = datetime.now().isoformat(timespec="seconds")
    status = "failed" if result.startswith("failed") else "done"

    if result == "cancelled":
        bot.mark_booking_cancelled(
            job["account_id"], job["date"], job["time"], job.get("course")
        )
        bot.notify(
            "Tee time cancelled",
            f"{job['details']} on {job['date']} ({job.get('account_name') or job['account_id']})",
            priority="default", tags="golf,wastebasket",
        )
    elif result.startswith("failed"):
        bot.notify(
            "Cancellation needs attention",
            f"{job['details']} on {job['date']}: {result}",
            priority="high", tags="warning",
        )

    cancel_queue.update_job(job["id"], status=status, finished_at=now, result=result)
    print(f"\nJob {job['id']} -> {status} ({result})")


def main():
    parser = argparse.ArgumentParser(description="Tee Time Cancellation Bot")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Execute a queued cancellation job")
    p_run.add_argument("--job", required=True, help="Job ID from cancel_queue")
    p_run.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_run.add_argument("--headful", action="store_true")

    p_cancel = sub.add_parser("cancel", help="One-off manual cancel")
    p_cancel.add_argument("--account", required=True, help="Holding account id")
    p_cancel.add_argument("--date", required=True, help='e.g. "6/13/2026"')
    p_cancel.add_argument("--day", default="saturday",
                          choices=["saturday", "sunday"])
    p_cancel.add_argument("--time", required=True, help='e.g. "8:40 AM"')
    p_cancel.add_argument("--course", default=None, help='e.g. "Jimmy Clay"')
    p_cancel.add_argument("--confirmation", required=True,
                          help="Confirmation number from the WebTrac booking email")
    p_cancel.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_cancel.add_argument("--headful", action="store_true")

    args = parser.parse_args()
    if args.command == "run":
        cmd_run(args)
    elif args.command == "cancel":
        cmd_cancel(args)


if __name__ == "__main__":
    main()
