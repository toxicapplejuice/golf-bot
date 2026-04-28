"""Tests for shared_state.py multi-account coordination."""

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
        # Force MAX=2 for predictable assertions
        self._orig_max = shared_state.MAX_BOOKINGS_PER_DAY
        shared_state.MAX_BOOKINGS_PER_DAY = 2

    def teardown_method(self):
        import shutil
        shared_state.SHARED_STATE_FILE = self._orig_file
        shared_state.MAX_BOOKINGS_PER_DAY = self._orig_max
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_read_returns_empty_when_file_missing(self):
        state = shared_state.read_shared("4/25/2026 - 4/26/2026")
        assert state["saturday"]["bookings"] == []
        assert state["sunday"]["bookings"] == []
        assert state["weekend"] == "4/25/2026 - 4/26/2026"

    def test_first_claim_succeeds(self):
        claimed, state = shared_state.claim_booking(
            "weekend1", "saturday", "8:00 AM at Lions", "michael"
        )
        assert claimed is True
        assert len(state["saturday"]["bookings"]) == 1
        assert state["saturday"]["bookings"][0]["booked_by"] == "michael"

    def test_second_claim_by_different_account_succeeds(self):
        shared_state.claim_booking("w", "saturday", "8:00 AM at Lions", "michael")
        claimed, state = shared_state.claim_booking(
            "w", "saturday", "8:08 AM at Lions", "grant"
        )
        assert claimed is True
        bookings = state["saturday"]["bookings"]
        assert len(bookings) == 2
        bookers = [b["booked_by"] for b in bookings]
        assert bookers == ["michael", "grant"]

    def test_third_claim_rejected_when_max_reached(self):
        shared_state.claim_booking("w", "saturday", "8:00 AM at Lions", "michael")
        shared_state.claim_booking("w", "saturday", "8:08 AM at Lions", "grant")
        claimed, state = shared_state.claim_booking(
            "w", "saturday", "8:16 AM at Lions", "christian"
        )
        assert claimed is False
        # state still has only 2 bookings
        assert len(state["saturday"]["bookings"]) == 2
        bookers = [b["booked_by"] for b in state["saturday"]["bookings"]]
        assert "christian" not in bookers

    def test_same_account_cannot_claim_twice(self):
        shared_state.claim_booking("w", "saturday", "8:00 AM at Lions", "michael")
        claimed, state = shared_state.claim_booking(
            "w", "saturday", "9:00 AM at Roy Kizer", "michael"
        )
        assert claimed is False
        # Still only one booking from michael
        assert len(state["saturday"]["bookings"]) == 1

    def test_different_days_independent(self):
        c1, _ = shared_state.claim_booking("w", "saturday", "8:00 AM Lions", "michael")
        c2, _ = shared_state.claim_booking("w", "saturday", "8:08 AM Lions", "grant")
        c3, _ = shared_state.claim_booking("w", "sunday", "9:00 AM Lions", "christian")
        c4, _ = shared_state.claim_booking("w", "sunday", "9:08 AM Lions", "michael")
        assert all([c1, c2, c3, c4])

    def test_stale_weekend_is_ignored(self):
        shared_state.claim_booking("old_weekend", "saturday", "8:00", "michael")
        state = shared_state.read_shared("new_weekend")
        assert state["saturday"]["bookings"] == []

    def test_day_already_booked_returns_false_until_full(self):
        full, who = shared_state.day_already_booked("w", "saturday")
        assert full is False
        assert who == []

        shared_state.claim_booking("w", "saturday", "8:00", "michael")
        full, who = shared_state.day_already_booked("w", "saturday")
        assert full is False  # only 1/2
        assert who == ["michael"]

        shared_state.claim_booking("w", "saturday", "8:08", "grant")
        full, who = shared_state.day_already_booked("w", "saturday")
        assert full is True  # 2/2
        assert who == ["michael", "grant"]

    def test_invalid_day_raises(self):
        try:
            shared_state.claim_booking("w", "monday", "x", "michael")
        except ValueError:
            return
        assert False, "Expected ValueError"

    def test_reset_creates_empty_state(self):
        shared_state.claim_booking("w", "saturday", "8:00", "michael")
        shared_state.reset_for_weekend("w")
        state = shared_state.read_shared("w")
        assert state["saturday"]["bookings"] == []

    def test_clear_removes_file(self):
        shared_state.claim_booking("w", "saturday", "8:00", "michael")
        assert os.path.exists(shared_state.SHARED_STATE_FILE)
        shared_state.clear_shared_state()
        assert not os.path.exists(shared_state.SHARED_STATE_FILE)
