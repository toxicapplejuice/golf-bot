"""Cancellation job queue for dashboard-triggered tee-time cancellations.

Manages a queue of one-shot cancellation jobs. The dashboard enqueues a job
when the user clicks "Cancel" on a booking, then spawns `cancel_bot.py run
--job <id>` to execute it. The dashboard polls job status to reflect progress.

Each job targets exactly one reservation (one account, one date, one slot).
Unlike standby watches (which recur), a cancel job runs once and terminates.

Uses fcntl.flock() for atomic read-modify-write, consistent with
shared_state.py and standby_queue.py.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(SCRIPT_DIR, "cancel_queue.json")

VALID_DAYS = ("saturday", "sunday")
VALID_STATUSES = ("pending", "running", "done", "failed")
ACTIVE_STATUSES = ("pending", "running")

# Fields a caller may mutate after creation. Identity/target fields are frozen.
_MUTABLE_FIELDS = ("status", "started_at", "finished_at", "result")


def _generate_id() -> str:
    return secrets.token_hex(4)


@contextmanager
def _locked_file(path: str, mode: str):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({"jobs": []}, f)

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
        return {"jobs": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"jobs": []}
    if "jobs" not in data:
        data["jobs"] = []
    return data


def _save(f, data: dict) -> None:
    f.seek(0)
    f.truncate()
    json.dump(data, f, indent=2)


def add_job(
    account_id: str,
    account_name: str,
    date: str,
    day: str,
    time: str,
    course: str | None,
    details: str,
    confirmation_number: str,
    dry_run: bool = False,
) -> dict:
    """Enqueue a cancellation job. Returns the created job dict.

    confirmation_number is required: WebTrac's teetimecancel.html is a
    search-by-confirmation-number form, and the number is not recoverable from
    the reservation history — the user supplies it from their booking email.
    """
    if day not in VALID_DAYS:
        raise ValueError(f"Invalid day: {day!r} (must be one of {VALID_DAYS})")
    if not account_id:
        raise ValueError("account_id is required")
    if not date:
        raise ValueError("date is required")
    if not time:
        raise ValueError("time is required")
    if not confirmation_number or not str(confirmation_number).strip():
        raise ValueError("confirmation_number is required")

    job = {
        "id": _generate_id(),
        "status": "pending",
        "account_id": account_id,
        "account_name": account_name,
        "date": date,
        "day": day,
        "time": time,
        "course": course,
        "details": details,
        "confirmation_number": str(confirmation_number).strip(),
        "dry_run": bool(dry_run),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": None,
        "finished_at": None,
        "result": None,
    }

    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        data["jobs"].append(job)
        _save(f, data)

    return job


def get_job(job_id: str) -> dict | None:
    """Return a single job by ID, or None if not found."""
    for j in list_jobs():
        if j["id"] == job_id:
            return j
    return None


def list_jobs() -> list[dict]:
    """Return all jobs (all statuses)."""
    try:
        with _locked_file(QUEUE_FILE, "r+") as f:
            data = _load(f)
            return data.get("jobs", [])
    except Exception:
        return []


def update_job(job_id: str, **fields) -> dict | None:
    """Update mutable fields on a job. Returns the updated job, or None.

    Only status/started_at/finished_at/result may be changed; attempting to
    mutate an identity field (id, account, date, slot) raises ValueError so a
    typo can't silently corrupt the target of a cancellation.
    """
    unknown = set(fields) - set(_MUTABLE_FIELDS)
    if unknown:
        raise ValueError(
            f"Cannot update field(s) {sorted(unknown)}; "
            f"mutable fields are {list(_MUTABLE_FIELDS)}"
        )
    if "status" in fields and fields["status"] not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status: {fields['status']!r} (must be one of {VALID_STATUSES})"
        )

    updated = None
    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        for j in data["jobs"]:
            if j["id"] == job_id:
                j.update(fields)
                updated = j
                break
        if updated is not None:
            _save(f, data)
    return updated


def find_active_for(
    account_id: str, date: str, time: str, course: str | None
) -> dict | None:
    """Return an active (pending/running) job for the same slot, or None.

    Used as a dup-spawn guard: a double-click on the dashboard Cancel button
    must not enqueue and spawn two cancellations for the same reservation.
    """
    for j in list_jobs():
        if j.get("status") not in ACTIVE_STATUSES:
            continue
        if (
            j.get("account_id") == account_id
            and j.get("date") == date
            and j.get("time") == time
            and j.get("course") == course
        ):
            return j
    return None


def clear_old_jobs(keep_days: int = 14) -> int:
    """Remove terminal (done/failed) jobs older than keep_days. Returns count removed."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    with _locked_file(QUEUE_FILE, "r+") as f:
        data = _load(f)
        original = len(data["jobs"])
        kept = []
        for j in data["jobs"]:
            if j.get("status") in ACTIVE_STATUSES:
                kept.append(j)
                continue
            try:
                created = datetime.fromisoformat(j.get("created_at", "2000-01-01"))
                if created > cutoff:
                    kept.append(j)
            except ValueError:
                pass
        data["jobs"] = kept
        removed = original - len(kept)
        if removed:
            _save(f, data)
    return removed
