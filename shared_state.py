"""Cross-process coordination for multi-account booking.

When N accounts race through Queue-it in parallel subprocesses, they need
to coordinate so we don't book more than MAX_BOOKINGS_PER_DAY slots per
day. `shared_state.json` tracks per-day booking lists; each subprocess
consults + appends through this module's functions.

Schema:
{
  "weekend": "5/2/2026 - 5/3/2026",
  "saturday": {
    "bookings": [
      {"booked_by": "michael", "details": "8:00 AM at Lions", "booked_at": "..."},
      {"booked_by": "grant",   "details": "8:08 AM at Lions", "booked_at": "..."}
    ]
  },
  "sunday": {"bookings": []}
}

Uses fcntl.flock() for atomic read-modify-write — safe across processes,
safe against crashed writers (OS releases the lock on process exit).
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_STATE_FILE = os.path.join(SCRIPT_DIR, "shared_state.json")

# Pull MAX from config so the orchestrator and shared_state agree.
try:
    from config import MAX_BOOKINGS_PER_DAY
except Exception:
    MAX_BOOKINGS_PER_DAY = 2


def _empty_state(weekend: str) -> dict:
    return {
        "weekend": weekend,
        "saturday": {"bookings": []},
        "sunday": {"bookings": []},
    }


def _normalize_day(state: dict, day: str) -> dict:
    """Ensure state[day] is the new {bookings: [...]} shape; migrate if old."""
    entry = state.get(day) or {}
    if "bookings" not in entry:
        entry = {"bookings": []}
    state[day] = entry
    return entry


@contextmanager
def _locked_file(path: str, mode: str):
    """Open a file with an exclusive advisory lock. Releases on close."""
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("")

    f = open(path, mode)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def _load_or_empty(f, weekend: str) -> dict:
    raw = f.read().strip()
    if not raw:
        return _empty_state(weekend)
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_state(weekend)
    if state.get("weekend") != weekend:
        return _empty_state(weekend)
    _normalize_day(state, "saturday")
    _normalize_day(state, "sunday")
    return state


def read_shared(weekend: str) -> dict:
    """Read the current shared state. Returns fresh empty state if file is
    missing, empty, or references a different weekend."""
    try:
        with _locked_file(SHARED_STATE_FILE, "r+") as f:
            return _load_or_empty(f, weekend)
    except Exception:
        return _empty_state(weekend)


def claim_booking(weekend: str, day: str, details: str,
                  account_id: str) -> tuple[bool, dict]:
    """Atomically append a booking for `account_id` to `day`'s list, IF there's
    capacity (len(bookings) < MAX_BOOKINGS_PER_DAY) and the account hasn't
    already booked this day.

    Returns (claimed, current_state):
        claimed=True  -> booking was recorded
        claimed=False -> day is full OR this account already booked

    `day` must be "saturday" or "sunday".
    """
    if day not in ("saturday", "sunday"):
        raise ValueError(f"day must be 'saturday' or 'sunday', got {day!r}")

    with _locked_file(SHARED_STATE_FILE, "r+") as f:
        state = _load_or_empty(f, weekend)
        entry = _normalize_day(state, day)
        bookings = entry["bookings"]

        # An account shouldn't double-book the same day
        if any(b.get("booked_by") == account_id for b in bookings):
            return False, state

        # Day full?
        if len(bookings) >= MAX_BOOKINGS_PER_DAY:
            return False, state

        bookings.append({
            "booked_by": account_id,
            "details": details,
            "booked_at": datetime.now().isoformat(timespec="seconds"),
        })

        f.seek(0)
        f.truncate()
        json.dump(state, f, indent=2)
        return True, state


def day_already_booked(weekend: str, day: str) -> tuple[bool, list]:
    """Returns (is_full, list_of_account_ids_who_booked).

    is_full=True only when bookings count has reached MAX_BOOKINGS_PER_DAY.
    """
    state = read_shared(weekend)
    entry = state.get(day) or {}
    bookings = entry.get("bookings", []) or []
    is_full = len(bookings) >= MAX_BOOKINGS_PER_DAY
    return is_full, [b.get("booked_by") for b in bookings if b.get("booked_by")]


def clear_shared_state() -> None:
    """Remove the shared state file."""
    try:
        if os.path.exists(SHARED_STATE_FILE):
            os.remove(SHARED_STATE_FILE)
    except Exception:
        pass


def reset_for_weekend(weekend: str) -> None:
    """Overwrite shared state with a fresh empty structure for `weekend`."""
    try:
        with _locked_file(SHARED_STATE_FILE, "w") as f:
            json.dump(_empty_state(weekend), f, indent=2)
    except Exception:
        pass
