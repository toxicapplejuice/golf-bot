"""Tests for shared_state.py multi-account coordination."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import shared_state  # noqa: E402


class TestSharedState:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = shared_state.SHARED_STATE_FILE
        shared_state.SHARED_STATE_FILE = os.path.join(self._tmpdir, "shared_state.json")

    def teardown_method(self):
        import shutil
        shared_state.SHARED_STATE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_read_returns_empty_when_file_missing(self):
        state = shared_state.read_shared("4/25/2026 - 4/26/2026")
        assert state["saturday"]["booked_by"] is None
        assert state["sunday"]["booked_by"] is None
        assert state["weekend"] == "4/25/2026 - 4/26/2026"

    def test_claim_saturday_succeeds_first_time(self):
        claimed, state = shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "saturday", "8:00 AM at Lions", "michael"
        )
        assert claimed is True
        assert state["saturday"]["booked_by"] == "michael"
        assert state["saturday"]["details"] == "8:00 AM at Lions"

    def test_claim_fails_when_already_claimed(self):
        # First call claims
        shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "saturday", "8:00 AM at Lions", "michael"
        )
        # Second call should lose
        claimed, state = shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "saturday", "8:10 AM at Lions", "friend1"
        )
        assert claimed is False
        assert state["saturday"]["booked_by"] == "michael"

    def test_claim_different_days_both_succeed(self):
        c1, _ = shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "saturday", "8:00 AM at Lions", "michael"
        )
        c2, state = shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "sunday", "9:00 AM at Roy Kizer", "friend1"
        )
        assert c1 is True and c2 is True
        assert state["saturday"]["booked_by"] == "michael"
        assert state["sunday"]["booked_by"] == "friend1"

    def test_stale_weekend_is_ignored(self):
        # Claim for old weekend
        shared_state.claim_booking(
            "4/18/2026 - 4/19/2026", "saturday", "8:00 AM at Lions", "michael"
        )
        # Read for new weekend — should be empty
        state = shared_state.read_shared("4/25/2026 - 4/26/2026")
        assert state["saturday"]["booked_by"] is None

    def test_day_already_booked_returns_true_and_winner(self):
        shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "saturday", "8:00 AM at Lions", "michael"
        )
        booked, who = shared_state.day_already_booked("4/25/2026 - 4/26/2026", "saturday")
        assert booked is True
        assert who == "michael"

    def test_day_already_booked_returns_false_when_not(self):
        booked, who = shared_state.day_already_booked("4/25/2026 - 4/26/2026", "saturday")
        assert booked is False
        assert who is None

    def test_invalid_day_raises(self):
        try:
            shared_state.claim_booking("weekend", "monday", "x", "michael")
        except ValueError:
            return
        assert False, "Expected ValueError"

    def test_reset_creates_empty_state(self):
        shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "saturday", "8:00 AM at Lions", "michael"
        )
        shared_state.reset_for_weekend("4/25/2026 - 4/26/2026")
        state = shared_state.read_shared("4/25/2026 - 4/26/2026")
        assert state["saturday"]["booked_by"] is None

    def test_clear_removes_file(self):
        shared_state.claim_booking(
            "4/25/2026 - 4/26/2026", "saturday", "8:00 AM at Lions", "michael"
        )
        assert os.path.exists(shared_state.SHARED_STATE_FILE)
        shared_state.clear_shared_state()
        assert not os.path.exists(shared_state.SHARED_STATE_FILE)
