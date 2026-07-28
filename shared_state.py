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
      {"booked_by": "michael", "details": "8:00 AM at Jimmy Clay", "course": "Jimmy Clay", "booked_at": "..."},
      {"booked_by": "grant",   "details": "8:08 AM at Roy Kizer",  "course": "Roy Kizer",  "booked_at": "..."}
    ]
  },
  "sunday": {"bookings": []}
}

The `course` field powers best-effort cross-account diversity: a sibling
account reads courses_booked() and skips courses already taken that day.

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

try:
    from config import NUM_PLAYERS as FULL_PARTY
except Exception:
    FULL_PARTY = 4

try:
    from config import MAX_REDUCED_BOOKINGS_PER_DAY
except Exception:
    MAX_REDUCED_BOOKINGS_PER_DAY = 1


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


def is_reduced_booking(booking: dict) -> bool:
    """True if this booking is for a smaller party than the full group.

    "Reduced" is measured against config.NUM_PLAYERS so every account agrees
    on the definition regardless of its own --players flag. Legacy bookings
    with no recorded player count are treated as full, which is the safe
    reading: it can only make the guard more permissive, never less.
    """
    players = booking.get("players")
    return players is not None and players < FULL_PARTY


def reduced_bookings(weekend: str, day: str) -> list:
    """Bookings on `day` that are for a smaller-than-full party."""
    state = read_shared(weekend)
    entry = state.get(day) or {}
    return [b for b in (entry.get("bookings", []) or []) if is_reduced_booking(b)]


def day_reduced_slots_left(weekend: str, day: str) -> int:
    """How many more reduced-party bookings `day` may still take.

    Caps sub-full bookings at MAX_REDUCED_BOOKINGS_PER_DAY so the group can't
    end up split across two courses at two times — the failure mode that the
    2026-07-27 morning-ladder change would otherwise open up, since
    cross-account course diversity actively pushes siblings apart.
    """
    return max(0, MAX_REDUCED_BOOKINGS_PER_DAY - len(reduced_bookings(weekend, day)))


def read_shared(weekend: str) -> dict:
    """Read the current shared state. Returns fresh empty state if file is
    missing, empty, or references a different weekend."""
    try:
        with _locked_file(SHARED_STATE_FILE, "r+") as f:
            return _load_or_empty(f, weekend)
    except Exception:
        return _empty_state(weekend)


def claim_booking(weekend: str, day: str, details: str,
                  account_id: str, course: str = None,
                  players: int = None) -> tuple[bool, dict]:
    """Atomically append a booking for `account_id` to `day`'s list, IF there's
    capacity (len(bookings) < MAX_BOOKINGS_PER_DAY), the account hasn't
    already booked this day, and — for a smaller-than-full party — the day
    hasn't already used up its MAX_REDUCED_BOOKINGS_PER_DAY allowance.

    `course` is recorded so sibling accounts can read courses_booked() and
    avoid double-booking the same course on the same day (best-effort
    diversity). `players` is recorded so the reduced-party cap can be
    enforced. Both are optional for backward compatibility.

    The reduced-party check lives HERE, inside the lock, because accounts race
    in parallel subprocesses: two of them can both observe "no reduced booking
    yet" and both go on to book. Callers still pre-check to avoid wasted work,
    but this is the decision that actually holds.

    Returns (claimed, current_state):
        claimed=True  -> booking was recorded
        claimed=False -> day is full, this account already booked, or the
                         reduced-party allowance is spent

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

        # Reduced-party allowance spent? (keeps the group from splitting
        # across two courses at two times)
        candidate = {"players": players}
        if is_reduced_booking(candidate):
            already = sum(1 for b in bookings if is_reduced_booking(b))
            if already >= MAX_REDUCED_BOOKINGS_PER_DAY:
                return False, state

        bookings.append({
            "booked_by": account_id,
            "details": details,
            "course": course,
            "players": players,
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


def courses_booked(weekend: str, day: str) -> set:
    """Return the set of course names already booked for `day` this weekend.

    Powers best-effort cross-account course diversity: a sibling account skips
    courses that are already taken today. Bookings without a recorded course
    (legacy entries / None) are ignored, degrading safely to no exclusion.
    """
    state = read_shared(weekend)
    entry = state.get(day) or {}
    bookings = entry.get("bookings", []) or []
    return {b["course"] for b in bookings if b.get("course")}


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
