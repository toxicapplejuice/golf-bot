"""Cross-process coordination for multi-account booking.

When N accounts race through Queue-it in parallel subprocesses, they need
to coordinate so we don't double-book a day. `shared_state.json` tracks
which account has claimed Saturday/Sunday; each subprocess consults + updates
it through this module's functions.

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


def _empty_state(weekend: str) -> dict:
    return {
        "weekend": weekend,
        "saturday": {"booked_by": None, "details": None, "booked_at": None},
        "sunday": {"booked_by": None, "details": None, "booked_at": None},
    }


@contextmanager
def _locked_file(path: str, mode: str):
    """Open a file with an exclusive advisory lock. Releases on close."""
    # Ensure the file exists so we can lock it even on first read
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


def read_shared(weekend: str) -> dict:
    """Read the current shared state. Returns fresh empty state if file is
    missing, empty, or references a different weekend."""
    try:
        with _locked_file(SHARED_STATE_FILE, "r+") as f:
            raw = f.read().strip()
            if not raw:
                return _empty_state(weekend)
            try:
                state = json.loads(raw)
            except json.JSONDecodeError:
                return _empty_state(weekend)
            if state.get("weekend") != weekend:
                # Stale — belongs to a prior weekend
                return _empty_state(weekend)
            return state
    except Exception:
        return _empty_state(weekend)


def claim_booking(weekend: str, day: str, details: str,
                  account_id: str) -> tuple[bool, dict]:
    """Atomically claim `day` for this account.

    Returns (claimed, current_state):
        claimed=True  -> this call successfully set booked_by to account_id
        claimed=False -> another account got there first (current_state shows who)

    `day` must be "saturday" or "sunday".
    """
    if day not in ("saturday", "sunday"):
        raise ValueError(f"day must be 'saturday' or 'sunday', got {day!r}")

    # Atomic RMW under the flock
    with _locked_file(SHARED_STATE_FILE, "r+") as f:
        raw = f.read().strip()
        if not raw:
            state = _empty_state(weekend)
        else:
            try:
                state = json.loads(raw)
            except json.JSONDecodeError:
                state = _empty_state(weekend)
            if state.get("weekend") != weekend:
                state = _empty_state(weekend)

        current = state.get(day) or {}
        if current.get("booked_by"):
            # Already claimed — we lost the race
            return False, state

        state[day] = {
            "booked_by": account_id,
            "details": details,
            "booked_at": datetime.now().isoformat(timespec="seconds"),
        }

        f.seek(0)
        f.truncate()
        json.dump(state, f, indent=2)
        return True, state


def day_already_booked(weekend: str, day: str) -> tuple[bool, str | None]:
    """Cheap read-only check. Returns (booked, booked_by_account_id)."""
    state = read_shared(weekend)
    entry = state.get(day) or {}
    return bool(entry.get("booked_by")), entry.get("booked_by")


def clear_shared_state() -> None:
    """Remove the shared state file. Called by orchestrator when both days
    are booked (fresh start next week) or when starting a new weekend."""
    try:
        if os.path.exists(SHARED_STATE_FILE):
            os.remove(SHARED_STATE_FILE)
    except Exception:
        pass


def reset_for_weekend(weekend: str) -> None:
    """Overwrite shared state with a fresh empty structure for `weekend`.
    Called by the multi_bot orchestrator at the start of each run."""
    try:
        with _locked_file(SHARED_STATE_FILE, "w") as f:
            json.dump(_empty_state(weekend), f, indent=2)
    except Exception:
        pass
