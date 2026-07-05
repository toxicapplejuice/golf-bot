"""Tests for cancel_queue.py — the dashboard cancellation job store."""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cancel_queue  # noqa: E402


class TestCancelQueue:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = cancel_queue.QUEUE_FILE
        cancel_queue.QUEUE_FILE = os.path.join(self._tmpdir, "cancel_queue.json")

    def teardown_method(self):
        import shutil

        cancel_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add(self, **overrides):
        kwargs = dict(
            account_id="christian",
            account_name="Christian",
            date="6/13/2026",
            day="saturday",
            time="8:40 AM",
            course="Jimmy Clay",
            details="8:40 AM at Jimmy Clay",
            confirmation_number="R1234567",
        )
        kwargs.update(overrides)
        return cancel_queue.add_job(**kwargs)

    # ---- add_job ----

    def test_add_job_creates_pending_job_with_all_fields(self):
        job = self._add()
        assert job["status"] == "pending"
        assert job["account_id"] == "christian"
        assert job["account_name"] == "Christian"
        assert job["date"] == "6/13/2026"
        assert job["day"] == "saturday"
        assert job["time"] == "8:40 AM"
        assert job["course"] == "Jimmy Clay"
        assert job["details"] == "8:40 AM at Jimmy Clay"
        assert job["confirmation_number"] == "R1234567"
        assert job["dry_run"] is False
        assert job["created_at"]
        assert job["started_at"] is None
        assert job["finished_at"] is None
        assert job["result"] is None
        assert len(job["id"]) == 8  # token_hex(4)

    def test_add_job_persists_to_disk(self):
        job = self._add()
        assert os.path.exists(cancel_queue.QUEUE_FILE)
        again = cancel_queue.get_job(job["id"])
        assert again is not None
        assert again["id"] == job["id"]

    def test_add_job_dry_run_flag_stored(self):
        job = self._add(dry_run=True)
        assert job["dry_run"] is True

    def test_add_job_dry_run_coerced_to_bool(self):
        job = self._add(dry_run=1)
        assert job["dry_run"] is True

    def test_add_job_ids_unique(self):
        a = self._add()
        b = self._add(time="9:00 AM")
        assert a["id"] != b["id"]

    def test_add_job_allows_none_course(self):
        job = self._add(course=None)
        assert job["course"] is None

    def test_add_job_invalid_day_raises(self):
        try:
            self._add(day="monday")
        except ValueError:
            return
        assert False, "Expected ValueError for invalid day"

    def test_add_job_missing_account_id_raises(self):
        try:
            self._add(account_id="")
        except ValueError:
            return
        assert False, "Expected ValueError for empty account_id"

    def test_add_job_missing_date_raises(self):
        try:
            self._add(date="")
        except ValueError:
            return
        assert False, "Expected ValueError for empty date"

    def test_add_job_missing_time_raises(self):
        try:
            self._add(time="")
        except ValueError:
            return
        assert False, "Expected ValueError for empty time"

    def test_add_job_missing_confirmation_number_raises(self):
        try:
            self._add(confirmation_number="")
        except ValueError:
            return
        assert False, "Expected ValueError for empty confirmation_number"

    def test_add_job_whitespace_confirmation_number_raises(self):
        try:
            self._add(confirmation_number="   ")
        except ValueError:
            return
        assert False, "Expected ValueError for whitespace-only confirmation_number"

    def test_add_job_strips_confirmation_number(self):
        job = self._add(confirmation_number="  R987  ")
        assert job["confirmation_number"] == "R987"

    # ---- get_job / list_jobs ----

    def test_get_job_returns_none_when_missing(self):
        assert cancel_queue.get_job("deadbeef") is None

    def test_list_jobs_empty_when_no_file(self):
        assert cancel_queue.list_jobs() == []

    def test_list_jobs_returns_all_statuses(self):
        a = self._add()
        b = self._add(time="9:00 AM")
        cancel_queue.update_job(b["id"], status="done", result="cancelled")
        ids = {j["id"] for j in cancel_queue.list_jobs()}
        assert ids == {a["id"], b["id"]}

    # ---- update_job ----

    def test_update_job_changes_status_and_result(self):
        job = self._add()
        updated = cancel_queue.update_job(
            job["id"],
            status="done",
            finished_at="2026-06-09T10:00:00",
            result="cancelled",
        )
        assert updated["status"] == "done"
        assert updated["result"] == "cancelled"
        assert updated["finished_at"] == "2026-06-09T10:00:00"
        # round-trips through disk
        again = cancel_queue.get_job(job["id"])
        assert again["status"] == "done"
        assert again["result"] == "cancelled"

    def test_update_job_running_sets_started_at(self):
        job = self._add()
        cancel_queue.update_job(job["id"], status="running", started_at="2026-06-09T09:59:00")
        again = cancel_queue.get_job(job["id"])
        assert again["status"] == "running"
        assert again["started_at"] == "2026-06-09T09:59:00"

    def test_update_job_unknown_field_raises(self):
        job = self._add()
        try:
            cancel_queue.update_job(job["id"], course="Lions")  # identity field, frozen
        except ValueError:
            return
        assert False, "Expected ValueError updating a frozen field"

    def test_update_job_invalid_status_raises(self):
        job = self._add()
        try:
            cancel_queue.update_job(job["id"], status="bogus")
        except ValueError:
            return
        assert False, "Expected ValueError for invalid status"

    def test_update_job_nonexistent_returns_none(self):
        assert cancel_queue.update_job("deadbeef", status="done") is None

    # ---- find_active_for (dup-spawn guard) ----

    def test_find_active_for_matches_pending_job(self):
        job = self._add()
        found = cancel_queue.find_active_for(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay"
        )
        assert found is not None
        assert found["id"] == job["id"]

    def test_find_active_for_matches_running_job(self):
        job = self._add()
        cancel_queue.update_job(job["id"], status="running")
        found = cancel_queue.find_active_for(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay"
        )
        assert found is not None

    def test_find_active_for_ignores_done_job(self):
        job = self._add()
        cancel_queue.update_job(job["id"], status="done", result="cancelled")
        found = cancel_queue.find_active_for(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay"
        )
        assert found is None

    def test_find_active_for_ignores_failed_job(self):
        job = self._add()
        cancel_queue.update_job(job["id"], status="failed", result="failed: boom")
        found = cancel_queue.find_active_for(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay"
        )
        assert found is None

    def test_find_active_for_distinguishes_account(self):
        self._add()
        found = cancel_queue.find_active_for(
            "michael", "6/13/2026", "8:40 AM", "Jimmy Clay"
        )
        assert found is None

    def test_find_active_for_distinguishes_time(self):
        self._add()
        found = cancel_queue.find_active_for(
            "christian", "6/13/2026", "9:00 AM", "Jimmy Clay"
        )
        assert found is None

    def test_find_active_for_distinguishes_date(self):
        self._add()
        found = cancel_queue.find_active_for(
            "christian", "6/14/2026", "8:40 AM", "Jimmy Clay"
        )
        assert found is None

    def test_find_active_for_distinguishes_course(self):
        self._add()
        found = cancel_queue.find_active_for(
            "christian", "6/13/2026", "8:40 AM", "Roy Kizer"
        )
        assert found is None

    def test_find_active_for_matches_none_course(self):
        self._add(course=None)
        found = cancel_queue.find_active_for(
            "christian", "6/13/2026", "8:40 AM", None
        )
        assert found is not None

    # ---- clear_old_jobs ----

    def _set_created(self, job_id, dt):
        # Reach past the frozen-field guard to backdate created_at for testing.
        import json

        with open(cancel_queue.QUEUE_FILE) as f:
            data = json.load(f)
        for j in data["jobs"]:
            if j["id"] == job_id:
                j["created_at"] = dt.isoformat(timespec="seconds")
        with open(cancel_queue.QUEUE_FILE, "w") as f:
            json.dump(data, f)

    def test_clear_old_jobs_removes_old_terminal(self):
        job = self._add()
        cancel_queue.update_job(job["id"], status="done", result="cancelled")
        self._set_created(job["id"], datetime.now() - timedelta(days=20))
        removed = cancel_queue.clear_old_jobs(keep_days=14)
        assert removed == 1
        assert cancel_queue.get_job(job["id"]) is None

    def test_clear_old_jobs_keeps_recent_terminal(self):
        job = self._add()
        cancel_queue.update_job(job["id"], status="done", result="cancelled")
        self._set_created(job["id"], datetime.now() - timedelta(days=3))
        removed = cancel_queue.clear_old_jobs(keep_days=14)
        assert removed == 0
        assert cancel_queue.get_job(job["id"]) is not None

    def test_clear_old_jobs_keeps_active_regardless_of_age(self):
        job = self._add()  # stays pending
        self._set_created(job["id"], datetime.now() - timedelta(days=90))
        removed = cancel_queue.clear_old_jobs(keep_days=14)
        assert removed == 0
        assert cancel_queue.get_job(job["id"]) is not None
