"""Standby watch queue for cancellation monitoring.

Manages a queue of tee-time watches that the standby bot polls periodically.
Each watch specifies target days, time preference, and player count. The
standby bot checks for openings every 30 minutes and books if found.

Uses fcntl.flock() for atomic read-modify-write, consistent with shared_state.py.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(SCRIPT_DIR, "standby_queue.json")

TIME_PREF_RANGES = {
    "morning": (8, 13),
    "afternoon": (13, 17),
    "all": (8, 17),
}

VALID_DAYS = ("saturday", "sunday")
VALID_TIME_PREFS = tuple(TIME_PREF_RANGES.keys())


def _generate_id() -> str:
    return secrets.token_hex(4)


@contextmanager
def _locked_file(path: str, mode: str):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({"watches": []}, f)

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


def _load(f) -> dict:
    raw = f.read().strip()
    if not raw:
        return {"watches": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"watches": []}
    if "watches" not in data:
        data["watches"] = []
    return data


def _save(f, data: dict) -> None:
    f.seek(0)
    f.truncate()
    json.dump(data, f, indent=2)


def get_upcoming_weekend_dates() -> tuple[str, str]:
    """Return (saturday_date, sunday_date) for the upcoming weekend.

    Unlike bot.get_next_weekend_dates(), this includes today if it IS
    Saturday or Sunday — standby watches should target the current weekend.
    """
    today = datetime.now()
    weekday = today.weekday()
    if weekday == 5:  # Saturday
        saturday = today
    elif weekday == 6:  # Sunday
        saturday = today - timedelta(days=1)
    else:
        days_until = (5 - weekday) % 7
        if days_until == 0:
            days_until = 7
        saturday = today + timedelta(days=days_until)
    sunday = saturday + timedelta(days=1)
    return saturday.strftime("%-m/%d/%Y"), sunday.strftime("%-m/%d/%Y")


def _compute_expiry(sunday_date_str: str) -> str:
    """Expire at end of Sunday (the weekend is over)."""
    sunday = datetime.strptime(sunday_date_str, "%m/%d/%Y")
    return sunday.replace(hour=23, minute=59, second=59).isoformat(timespec="seconds")


def add_watch(days: list[str], time_pref: str, players: int) -> dict:
    """Add a new standby watch. Returns the created watch dict."""
    for d in days:
        if d not in VALID_DAYS:
            raise ValueError(f"Invalid day: {d!r} (must be one of {VALID_DAYS})")
    if time_pref not in VALID_TIME_PREFS:
        raise ValueError(f"Invalid time_pref: {time_pref!r} (must be one of {VALID_TIME_PREFS})")
    if not 1 <= players <= 4:
        raise ValueError(f"Invalid players: {players} (must be 1-4)")

    sat_date, sun_date = get_upcoming_weekend_dates()
    target_dates = {}
    results = {}
    for d in days:
        target_dates[d] = sat_date if d == "saturday" else sun_date
        results[d] = None

    watch = {
        "id": _generate_id(),
        "days": sorted(days),
        "time_pref": time_pref,
        "players": players,
        "status": "watching",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_checked_at": None,
        "check_count": 0,
        "target_dates": target_dates,
        "expires_at": _compute_expiry(sun_date),
        "results": results,
    }

    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        data["watches"].append(watch)
        _save(f, data)

    return watch


def cancel_watch(watch_id: str) -> bool:
    """Cancel a watch by ID. Returns True if found and cancelled."""
    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        for w in data["watches"]:
            if w["id"] == watch_id and w["status"] == "watching":
                w["status"] = "cancelled"
                _save(f, data)
                return True
        _save(f, data)
    return False


def list_watches() -> list[dict]:
    """Return all watches (all statuses)."""
    try:
        with _locked_file(QUEUE_FILE, "r+") as f:
            data = _load(f)
            return data.get("watches", [])
    except Exception:
        return []


def get_active_watches() -> list[dict]:
    """Return watches with status='watching' that haven't expired."""
    now = datetime.now()
    active = []
    for w in list_watches():
        if w["status"] != "watching":
            continue
        try:
            expires = datetime.fromisoformat(w["expires_at"])
            if now > expires:
                continue
        except (KeyError, ValueError):
            continue
        active.append(w)
    return active


def update_watch_check(watch_id: str) -> None:
    """Mark a watch as just-checked (update timestamp, increment count)."""
    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        for w in data["watches"]:
            if w["id"] == watch_id:
                w["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
                w["check_count"] = w.get("check_count", 0) + 1
                break
        _save(f, data)


def mark_day_booked(watch_id: str, day: str, details: str) -> None:
    """Record a successful booking for one day of a watch."""
    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        for w in data["watches"]:
            if w["id"] == watch_id:
                w["results"][day] = {"booked": True, "details": details}
                all_booked = all(
                    isinstance(w["results"].get(d), dict)
                    and w["results"][d].get("booked")
                    for d in w["days"]
                )
                if all_booked:
                    w["status"] = "booked"
                break
        _save(f, data)


def expire_stale_watches() -> int:
    """Mark expired watches. Returns count expired."""
    now = datetime.now()
    count = 0
    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        for w in data["watches"]:
            if w["status"] != "watching":
                continue
            try:
                expires = datetime.fromisoformat(w["expires_at"])
                if now > expires:
                    w["status"] = "expired"
                    count += 1
            except (KeyError, ValueError):
                w["status"] = "expired"
                count += 1
        if count:
            _save(f, data)
    return count


def clear_old_watches(keep_days: int = 14) -> int:
    """Remove terminal watches older than keep_days."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        original = len(data["watches"])
        kept = []
        for w in data["watches"]:
            if w["status"] == "watching":
                kept.append(w)
                continue
            try:
                created = datetime.fromisoformat(w.get("created_at", "2000-01-01"))
                if created > cutoff:
                    kept.append(w)
            except ValueError:
                pass
        data["watches"] = kept
        removed = original - len(kept)
        if removed:
            _save(f, data)
    return removed
