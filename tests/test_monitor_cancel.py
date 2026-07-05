"""Integration tests for monitor.py cancellation HTTP routes.

Spins up the real Handler on an ephemeral port with the browser-spawn and
account lookup stubbed, so we exercise route dispatch + the dup-spawn guard
without launching Playwright or depending on accounts.json.
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cancel_queue  # noqa: E402
import monitor  # noqa: E402


class TestMonitorCancelRoutes:
    def setup_method(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_q = cancel_queue.QUEUE_FILE
        cancel_queue.QUEUE_FILE = os.path.join(self._tmp, "cancel_queue.json")

        self._orig_spawn = monitor._spawn_cancel_runner
        self.spawned = []
        monitor._spawn_cancel_runner = lambda job_id: self.spawned.append(job_id)

        self._orig_disp = monitor._account_display_name
        monitor._account_display_name = lambda aid: aid.capitalize()

        self._server = http.server.HTTPServer(("127.0.0.1", 0), monitor.Handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def teardown_method(self):
        import shutil

        self._server.shutdown()
        self._server.server_close()
        cancel_queue.QUEUE_FILE = self._orig_q
        monitor._spawn_cancel_runner = self._orig_spawn
        monitor._account_display_name = self._orig_disp
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self._port}{path}") as r:
            return r.status, json.loads(r.read())

    _SLOT = {
        "account_id": "christian", "date": "6/13/2026", "day": "saturday",
        "time": "8:40 AM", "course": "Jimmy Clay",
        "details": "8:40 AM at Jimmy Clay", "confirmation_number": "R1234567",
    }

    def test_jobs_empty_initially(self):
        status, jobs = self._get("/api/cancel/jobs")
        assert status == 200
        assert jobs == []

    def test_post_creates_job_and_spawns(self):
        status, job = self._post("/api/booking/cancel", dict(self._SLOT))
        assert status == 200
        assert job["id"]
        assert job["status"] == "pending"
        assert job["account_name"] == "Christian"  # resolved server-side
        assert job["course"] == "Jimmy Clay"
        assert job["confirmation_number"] == "R1234567"
        assert self.spawned == [job["id"]]
        _, jobs = self._get("/api/cancel/jobs")
        assert len(jobs) == 1
        assert jobs[0]["id"] == job["id"]

    def test_dup_guard_returns_existing_without_second_spawn(self):
        _, job1 = self._post("/api/booking/cancel", dict(self._SLOT))
        _, job2 = self._post("/api/booking/cancel", dict(self._SLOT))
        assert job1["id"] == job2["id"]
        assert self.spawned == [job1["id"]]  # spawned exactly once
        _, jobs = self._get("/api/cancel/jobs")
        assert len(jobs) == 1

    def test_different_slot_spawns_separately(self):
        _, job1 = self._post("/api/booking/cancel", dict(self._SLOT))
        other = dict(self._SLOT)
        other.update(time="9:00 AM", details="9:00 AM at Jimmy Clay")
        _, job2 = self._post("/api/booking/cancel", other)
        assert job1["id"] != job2["id"]
        assert self.spawned == [job1["id"], job2["id"]]

    def test_missing_fields_400(self):
        status, body = self._post("/api/booking/cancel", {"account_id": "christian"})
        assert status == 400
        assert "error" in body
        assert self.spawned == []

    def test_invalid_day_400(self):
        bad = dict(self._SLOT)
        bad["day"] = "monday"
        status, body = self._post("/api/booking/cancel", bad)
        assert status == 400
        assert self.spawned == []

    def test_missing_confirmation_number_400(self):
        bad = dict(self._SLOT)
        del bad["confirmation_number"]
        status, body = self._post("/api/booking/cancel", bad)
        assert status == 400
        assert "confirmation number" in body["error"].lower()
        assert self.spawned == []

    def test_blank_confirmation_number_400(self):
        bad = dict(self._SLOT)
        bad["confirmation_number"] = "   "
        status, body = self._post("/api/booking/cancel", bad)
        assert status == 400
        assert self.spawned == []

    def test_dashboard_html_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/") as r:
            assert r.status == 200
            html = r.read().decode()
        assert "cancelBooking" in html
        assert "cancelJobs" in html
