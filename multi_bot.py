#!/usr/bin/env python3
"""Multi-account orchestrator.

Spawns one subprocess per enabled account in accounts.json. Each subprocess
runs bot.py with --account-id=X and behaves exactly like a single-account
bot — but coordinates with siblings via shared_state.json so only one
account successfully books each day.

Usage:
    python3 multi_bot.py                       # scheduled run (waits for 8pm)
    python3 multi_bot.py --now --dry-run       # immediate test walk-through
    python3 multi_bot.py --only michael        # just run one account

Each subprocess's stdout/stderr goes to booking_<id>.log; the orchestrator
itself writes to multi_bot.log. At the end, one aggregated history entry
is appended via bot.append_to_history with an account_id of "multi".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import bot  # noqa: E402
import shared_state  # noqa: E402

DEFAULT_MAX_TIME = 1800  # 30 min — same as single-account bot


def _open_log(account_id: str):
    """Open the per-account log file for writing. Truncate at start of run."""
    path = os.path.join(SCRIPT_DIR, f"booking_{account_id}.log")
    return open(path, "w", buffering=1)  # line-buffered


def spawn_account(account: dict, args: argparse.Namespace) -> tuple[subprocess.Popen, any]:
    """Launch a bot.py subprocess for one account. Returns (Popen, log_handle)."""
    cmd = [
        sys.executable, "-u", os.path.join(SCRIPT_DIR, "bot.py"),
        "--account-id", account["id"],
        "--players", str(args.players),
        "--max-time", str(args.max_time),
    ]
    if args.now:
        cmd.append("--now")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.headful:
        cmd.append("--headful")

    log_handle = _open_log(account["id"])
    log_handle.write(f"=== multi_bot spawning {account['id']} at "
                     f"{datetime.now().isoformat(timespec='seconds')} ===\n")
    log_handle.flush()

    # Env var signals to bot.py that it's running under the orchestrator,
    # so it skips writing its own history entry (we write one aggregate).
    env = os.environ.copy()
    env["MULTI_BOT_ACTIVE"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=SCRIPT_DIR,
        env=env,
    )
    return proc, log_handle


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-account golf booking orchestrator")
    parser.add_argument("--now", action="store_true",
                        help="Skip 8pm wait (testing)")
    parser.add_argument("--players", type=int, default=4,
                        help="Number of players per booking")
    parser.add_argument("--max-time", dest="max_time", type=int,
                        default=DEFAULT_MAX_TIME,
                        help="Max total runtime seconds (default: 1800)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Walk through flow but abort before final checkout")
    parser.add_argument("--headful", action="store_true",
                        help="Show browser windows (debugging)")
    parser.add_argument("--only", default=None,
                        help="Only run the specified account id (skip others)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    accounts = bot.load_accounts()
    if args.only:
        accounts = [a for a in accounts if a["id"] == args.only]

    if not accounts:
        print("ERROR: no enabled accounts in accounts.json")
        bot.send_ntfy(
            "Multi-bot: no accounts configured",
            "accounts.json is missing or all entries are disabled/REPLACE_ME.",
            priority="urgent", tags="rotating_light",
        )
        return 1

    print("=" * 60)
    print("Austin Municipal Tee Time — Multi-Account Orchestrator")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Accounts: {', '.join(a['display_name'] for a in accounts)}")
    print(f"Mode: {'IMMEDIATE' if args.now else 'SCHEDULED'}"
          f"{' [DRY-RUN]' if args.dry_run else ''}")
    print("=" * 60)

    # Fresh shared state for this weekend
    saturday_date, sunday_date = bot.get_next_weekend_dates()
    weekend = f"{saturday_date} - {sunday_date}"
    shared_state.reset_for_weekend(weekend)

    # Notify that the orchestrator is alive
    if not args.now:
        bot.send_ntfy(
            "Multi-bot launched",
            f"{len(accounts)} accounts racing for Sat {saturday_date} / Sun {sunday_date}: "
            f"{', '.join(a['display_name'] for a in accounts)}",
            priority="low", tags="rocket",
        )

    # Spawn subprocesses
    procs = []
    for acc in accounts:
        proc, log_handle = spawn_account(acc, args)
        procs.append({"account": acc, "proc": proc, "log": log_handle})
        print(f"  Spawned {acc['display_name']} (pid {proc.pid})")

    # Wait for all to finish, with overall timeout
    deadline = time.time() + args.max_time + 60  # a little grace
    try:
        while any(p["proc"].poll() is None for p in procs):
            if time.time() > deadline:
                print(f"\n!!! Overall deadline exceeded — terminating stragglers")
                for p in procs:
                    if p["proc"].poll() is None:
                        print(f"  Killing {p['account']['id']}")
                        try:
                            p["proc"].terminate()
                        except Exception:
                            pass
                time.sleep(5)
                for p in procs:
                    if p["proc"].poll() is None:
                        try:
                            p["proc"].kill()
                        except Exception:
                            pass
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n!!! Interrupted — terminating subprocesses")
        for p in procs:
            try:
                p["proc"].terminate()
            except Exception:
                pass
    finally:
        for p in procs:
            try:
                p["log"].close()
            except Exception:
                pass

    # Aggregate results from shared state
    final_state = shared_state.read_shared(weekend)
    sat = final_state.get("saturday") or {}
    sun = final_state.get("sunday") or {}
    sat_bookings = sat.get("bookings", []) or []
    sun_bookings = sun.get("bookings", []) or []
    MAX = shared_state.MAX_BOOKINGS_PER_DAY

    def fmt_bookings(bs):
        if not bs:
            return "NO BOOKINGS"
        return "; ".join(f"{b['details']} ({b['booked_by']})" for b in bs)

    print("\n" + "=" * 60)
    print("MULTI-BOT FINAL RESULTS:")
    print(f"  Saturday ({len(sat_bookings)}/{MAX}): {fmt_bookings(sat_bookings)}")
    print(f"  Sunday ({len(sun_bookings)}/{MAX}): {fmt_bookings(sun_bookings)}")
    print("=" * 60)

    # Build a unified history entry (one per orchestrator run, not per account).
    # Keep "success" semantically = "we got at least one booking that day", and
    # surface the full bookings list for richer dashboards.
    aggregate_results = {
        "saturday": {
            "success": len(sat_bookings) > 0,
            "details": fmt_bookings(sat_bookings) if sat_bookings else None,
            "course": None,
            "booked_by": sat_bookings[0]["booked_by"] if sat_bookings else None,
            "bookings": sat_bookings,
            "count": len(sat_bookings),
            "max": MAX,
        },
        "sunday": {
            "success": len(sun_bookings) > 0,
            "details": fmt_bookings(sun_bookings) if sun_bookings else None,
            "course": None,
            "booked_by": sun_bookings[0]["booked_by"] if sun_bookings else None,
            "bookings": sun_bookings,
            "count": len(sun_bookings),
            "max": MAX,
        },
    }
    bot.ACCOUNT_ID = "multi"
    bot.ACCOUNT_DISPLAY_NAME = "Multi-bot"
    bot.append_to_history(
        saturday_date, sunday_date, aggregate_results,
        run_started=datetime.now().isoformat(timespec="seconds"),
        run_ended=datetime.now().isoformat(timespec="seconds"),
        notes=f"Accounts: {', '.join(a['display_name'] for a in accounts)}",
    )

    total_booked = len(sat_bookings) + len(sun_bookings)
    target = 2 * MAX
    body = [
        f"Saturday ({len(sat_bookings)}/{MAX}): {fmt_bookings(sat_bookings)}",
        f"Sunday ({len(sun_bookings)}/{MAX}): {fmt_bookings(sun_bookings)}",
    ]

    if total_booked >= target:
        title = f"All {target} tee times booked!"
        priority = "default"
        tags = "golf,white_check_mark"
    elif total_booked > 0:
        title = f"Partial: {total_booked}/{target} booked"
        priority = "high"
        tags = "golf,warning"
    else:
        title = "No bookings across any account"
        priority = "high"
        tags = "golf,x"

    bot.notify(f"Multi-bot: {title}", "\n".join(body), priority=priority, tags=tags)

    return 0


if __name__ == "__main__":
    sys.exit(main())
