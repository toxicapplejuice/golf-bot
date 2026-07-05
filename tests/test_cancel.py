"""Tests for bot.py cancellation helpers: history matching + cancel marking."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot  # noqa: E402


# ----------------------------------------------------------------------
# _slot_in_content (pure content matcher)
# ----------------------------------------------------------------------

class TestSlotInContent:
    def _slot(self, **o):
        s = {"date": "6/13/2026", "time": "8:40 AM", "course": "Jimmy Clay"}
        s.update(o)
        return s

    def test_present_full_form(self):
        content = "Reservation 8:40 AM at Jimmy Clay on 6/13/2026"
        assert bot._slot_in_content(content, self._slot()) is True

    def test_absent_when_nothing_matches(self):
        assert bot._slot_in_content("totally unrelated page", self._slot()) is False

    def test_condensed_time_form(self):
        # Receipt shows "8:40A" instead of "8:40 AM"
        content = "8:40A Jimmy Clay 6/13/2026"
        assert bot._slot_in_content(content, self._slot()) is True

    def test_padded_date_form(self):
        # Site renders "04/25/2026" but slot stored "4/25/2026"
        content = "8:01 AM Roy Kizer 04/25/2026"
        slot = self._slot(date="4/25/2026", time="8:01 AM", course="Roy Kizer")
        assert bot._slot_in_content(content, slot) is True

    def test_missing_course_fails(self):
        content = "8:40 AM at Roy Kizer on 6/13/2026"  # wrong course
        assert bot._slot_in_content(content, self._slot()) is False

    def test_missing_time_fails(self):
        content = "9:00 AM at Jimmy Clay on 6/13/2026"  # wrong time
        assert bot._slot_in_content(content, self._slot()) is False

    def test_missing_date_fails(self):
        content = "8:40 AM at Jimmy Clay on 6/20/2026"  # wrong date
        assert bot._slot_in_content(content, self._slot()) is False

    def test_case_insensitive(self):
        content = "8:40 am AT JIMMY CLAY 6/13/2026"
        assert bot._slot_in_content(content, self._slot()) is True


# ----------------------------------------------------------------------
# _active_slot_in_content (status-aware: cancelled rows persist in history)
# ----------------------------------------------------------------------

class TestActiveSlotInContent:
    SLOT = {"date": "6/13/2026", "time": "8:40 AM", "course": "Jimmy Clay"}

    def _row(self, time_txt, date, course_txt, status):
        return (
            '<tr><td data-title="Description">Tee Time at ' + time_txt + ', '
            + date + ' on ' + course_txt + ' Golf Course</td>'
            '<td data-title="Name">Michael</td>'
            '<td data-title="Date Range">' + date + ' -' + date + '</td>'
            '<td data-title="Status">' + status + '</td>'
            '<td data-title="Location">Clay-Kizer Golf</td></tr>'
        )

    def test_reserved_row_is_active(self):
        html = "<table>" + self._row("8:40 am", "06/13/2026", "Jimmy Clay", "Reserved") + "</table>"
        assert bot._active_slot_in_content(html, self.SLOT) is True

    def test_cancelled_row_is_not_active(self):
        # The core regression: a cancelled reservation keeps its slot text in
        # the history table, but verify-gone must read it as no-longer-active.
        html = "<table>" + self._row("8:40 am", "06/13/2026", "Jimmy Clay", "Cancelled") + "</table>"
        assert bot._active_slot_in_content(html, self.SLOT) is False
        # Flat matcher still (intentionally) sees the text — that's exactly why
        # a status-aware check is needed for cancellation.
        assert bot._slot_in_content(html, self.SLOT) is True

    def test_other_slot_reserved_not_matched(self):
        html = "<table>" + self._row("9:00 am", "06/13/2026", "Jimmy Clay", "Reserved") + "</table>"
        assert bot._active_slot_in_content(html, self.SLOT) is False

    def test_mixed_rows_live_reserved_matches(self):
        # Old cancelled row (different date) + the live reserved row.
        html = (
            "<table>"
            + self._row("8:40 am", "04/25/2026", "Jimmy Clay", "Cancelled")
            + self._row("8:40 am", "06/13/2026", "Jimmy Clay", "Reserved")
            + "</table>"
        )
        assert bot._active_slot_in_content(html, self.SLOT) is True

    def test_condensed_time_reserved(self):
        html = "<table>" + self._row("8:40a", "06/13/2026", "Jimmy Clay", "Reserved") + "</table>"
        assert bot._active_slot_in_content(html, self.SLOT) is True

    def test_empty_content_false(self):
        assert bot._active_slot_in_content("", self.SLOT) is False


# ----------------------------------------------------------------------
# _fetch_history_content / slot_in_history / verify_booking_via_history
# ----------------------------------------------------------------------

class FakePage:
    def __init__(self, content="", goto_raises=None):
        self._content = content
        self._goto_raises = goto_raises
        self.goto_called_with = None

    def goto(self, url, **kw):
        self.goto_called_with = url
        if self._goto_raises is not None:
            raise self._goto_raises

    def content(self):
        return self._content


class TestHistoryFetch:
    def setup_method(self):
        self._orig_login = bot.is_on_login_page
        self._orig_queue = bot.is_in_queue
        bot.is_on_login_page = lambda p: False
        bot.is_in_queue = lambda p: False

    def teardown_method(self):
        bot.is_on_login_page = self._orig_login
        bot.is_in_queue = self._orig_queue

    def _slot(self):
        return {"date": "6/13/2026", "time": "8:40 AM", "course": "Jimmy Clay"}

    def test_fetch_returns_lowercased_content(self):
        page = FakePage(content="8:40 AM at Jimmy Clay 6/13/2026")
        out = bot._fetch_history_content(page)
        assert out == "8:40 am at jimmy clay 6/13/2026"

    def test_fetch_returns_none_on_timeout(self):
        page = FakePage(goto_raises=bot.PlaywrightTimeout("boom"))
        assert bot._fetch_history_content(page) is None

    def test_fetch_returns_none_when_on_login(self):
        bot.is_on_login_page = lambda p: True
        page = FakePage(content="login form")
        assert bot._fetch_history_content(page) is None

    def test_fetch_returns_none_when_in_queue(self):
        bot.is_in_queue = lambda p: True
        page = FakePage(content="you're in line")
        assert bot._fetch_history_content(page) is None

    def test_slot_in_history_true_when_present(self):
        page = FakePage(content="8:40 AM at Jimmy Clay on 6/13/2026")
        assert bot.slot_in_history(page, self._slot()) is True

    def test_slot_in_history_false_when_absent(self):
        page = FakePage(content="no reservations found")
        assert bot.slot_in_history(page, self._slot()) is False

    def test_slot_in_history_false_on_fetch_failure(self):
        page = FakePage(goto_raises=bot.PlaywrightTimeout("boom"))
        assert bot.slot_in_history(page, self._slot()) is False

    def test_verify_booking_delegates(self):
        present = FakePage(content="8:40 AM at Jimmy Clay 6/13/2026")
        absent = FakePage(content="nothing here")
        assert bot.verify_booking_via_history(present, self._slot()) is True
        assert bot.verify_booking_via_history(absent, self._slot()) is False


# ----------------------------------------------------------------------
# mark_booking_cancelled
# ----------------------------------------------------------------------

class TestMarkBookingCancelled:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "history.json")

    def teardown_method(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, history):
        with open(self._path, "w") as f:
            json.dump(history, f)

    def _read(self):
        with open(self._path) as f:
            return json.load(f)

    def _multi_entry(self):
        return {
            "saturday_date": "6/13/2026",
            "sunday_date": "6/14/2026",
            "account_id": "multi",
            "results": {
                "saturday": {
                    "bookings": [
                        {"booked_by": "christian", "details": "8:40 AM at Jimmy Clay", "course": "Jimmy Clay"},
                        {"booked_by": "michael", "details": "8:01 AM at Roy Kizer", "course": "Roy Kizer"},
                    ]
                },
                "sunday": {
                    "bookings": [
                        {"booked_by": "christian", "details": "8:01 AM at Roy Kizer", "course": "Roy Kizer"},
                    ]
                },
            },
        }

    def _single_entry(self):
        return {
            "saturday_date": "4/25/2026",
            "sunday_date": "4/26/2026",
            "account_id": "default",
            "results": {
                "saturday": {"success": True, "details": "8:01 AM at Roy Kizer", "course": "Roy Kizer", "booked_by": "michael"},
                "sunday": {"success": True, "details": "8:40 AM at Lions", "course": "Lions", "booked_by": "michael"},
            },
        }

    def test_multi_marks_only_matching_subbooking(self):
        self._write([self._multi_entry()])
        ok = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay", history_file=self._path
        )
        assert ok is True
        sat = self._read()[0]["results"]["saturday"]["bookings"]
        christian = next(b for b in sat if b["booked_by"] == "christian")
        michael = next(b for b in sat if b["booked_by"] == "michael")
        assert christian["cancelled"] is True
        assert "cancelled_at" in christian
        assert michael.get("cancelled") is None  # untouched
        # christian's *sunday* booking is a different day — untouched
        sun = self._read()[0]["results"]["sunday"]["bookings"][0]
        assert sun.get("cancelled") is None

    def test_single_account_marks_day_level(self):
        self._write([self._single_entry()])
        ok = bot.mark_booking_cancelled(
            "michael", "4/25/2026", "8:01 AM", "Roy Kizer", history_file=self._path
        )
        assert ok is True
        results = self._read()[0]["results"]
        assert results["saturday"]["cancelled"] is True
        assert results["sunday"].get("cancelled") is None

    def test_legacy_subbooking_without_course_field_matches(self):
        entry = self._multi_entry()
        # Strip the explicit course field (legacy multi entries had none)
        for b in entry["results"]["saturday"]["bookings"]:
            b.pop("course", None)
        self._write([entry])
        ok = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay", history_file=self._path
        )
        assert ok is True

    def test_course_none_matches_on_time_only(self):
        self._write([self._multi_entry()])
        ok = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", None, history_file=self._path
        )
        assert ok is True

    def test_wrong_account_no_match(self):
        self._write([self._multi_entry()])
        ok = bot.mark_booking_cancelled(
            "grant", "6/13/2026", "8:40 AM", "Jimmy Clay", history_file=self._path
        )
        assert ok is False

    def test_wrong_time_no_match(self):
        self._write([self._multi_entry()])
        ok = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "9:00 AM", "Jimmy Clay", history_file=self._path
        )
        assert ok is False

    def test_wrong_date_no_match(self):
        self._write([self._multi_entry()])
        ok = bot.mark_booking_cancelled(
            "christian", "1/01/2026", "8:40 AM", "Jimmy Clay", history_file=self._path
        )
        assert ok is False

    def test_course_mismatch_no_match(self):
        self._write([self._multi_entry()])
        ok = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", "Lions", history_file=self._path
        )
        assert ok is False

    def test_idempotent_second_call_returns_false(self):
        self._write([self._multi_entry()])
        first = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay", history_file=self._path
        )
        second = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay", history_file=self._path
        )
        assert first is True
        assert second is False  # already cancelled

    def test_missing_file_returns_false(self):
        ok = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay",
            history_file=os.path.join(self._tmpdir, "nope.json"),
        )
        assert ok is False

    def test_marks_across_duplicate_entries_same_date(self):
        # Two history entries for the same weekend (e.g. a re-run) — both flagged.
        self._write([self._multi_entry(), self._multi_entry()])
        ok = bot.mark_booking_cancelled(
            "christian", "6/13/2026", "8:40 AM", "Jimmy Clay", history_file=self._path
        )
        assert ok is True
        for entry in self._read():
            sat = entry["results"]["saturday"]["bookings"]
            christian = next(b for b in sat if b["booked_by"] == "christian")
            assert christian["cancelled"] is True
