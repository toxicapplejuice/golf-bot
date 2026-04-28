"""Tests for pure (no-browser) functions in bot.py and config.py."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot  # noqa: E402
import config  # noqa: E402


class TestParseTime:
    def test_morning(self):
        assert bot.parse_time("9:00 AM") == 9 * 60

    def test_noon(self):
        assert bot.parse_time("12:00 PM") == 12 * 60

    def test_midnight(self):
        assert bot.parse_time("12:00 AM") == 0

    def test_pm(self):
        assert bot.parse_time("1:30 PM") == 13 * 60 + 30

    def test_single_digit_hour(self):
        assert bot.parse_time("8:48 AM") == 8 * 60 + 48

    def test_invalid(self):
        assert bot.parse_time("not a time") == 9999
        assert bot.parse_time("") == 9999


class TestIsTimeInRange:
    def test_morning_window_includes_8am(self):
        assert bot.is_time_in_range("8:00 AM")

    def test_morning_window_includes_late_1pm(self):
        assert bot.is_time_in_range("1:48 PM")

    def test_morning_window_excludes_2pm(self):
        assert not bot.is_time_in_range("2:00 PM")

    def test_morning_window_excludes_7am(self):
        assert not bot.is_time_in_range("7:00 AM")

    def test_fallback_window_includes_5pm(self):
        assert bot.is_time_in_range("5:00 PM", max_hour=17)

    def test_fallback_window_excludes_6pm(self):
        assert not bot.is_time_in_range("6:00 PM", max_hour=17)


class TestGetTimePriority:
    def test_830am_is_top_priority(self):
        """8:30am block has highest priority."""
        eight_thirty = bot.get_time_priority("8:32 AM")
        assert eight_thirty < bot.get_time_priority("8:00 AM")
        assert eight_thirty < bot.get_time_priority("9:00 AM")
        assert eight_thirty < bot.get_time_priority("10:00 AM")

    def test_ordering_matches_time_priority_list(self):
        # 8:30am > 8am > 9am > 10am > 11am > 12pm in the config
        assert bot.get_time_priority("8:32 AM") < bot.get_time_priority("8:00 AM")
        assert bot.get_time_priority("8:00 AM") < bot.get_time_priority("9:00 AM")
        assert bot.get_time_priority("9:00 AM") < bot.get_time_priority("10:00 AM")
        assert bot.get_time_priority("10:00 AM") < bot.get_time_priority("11:00 AM")
        assert bot.get_time_priority("11:00 AM") < bot.get_time_priority("12:00 PM")

    def test_unknown_time_falls_back(self):
        # Off-the-8 time not in TIME_PRIORITY list — should still get a bucket
        prio = bot.get_time_priority("9:07 AM")
        assert 0 <= prio <= 100


class TestNextWeekendDates:
    def test_returns_two_strings(self):
        sat, sun = bot.get_next_weekend_dates()
        assert isinstance(sat, str) and isinstance(sun, str)

    def test_dates_are_consecutive(self):
        sat_str, sun_str = bot.get_next_weekend_dates()
        sat = datetime.strptime(sat_str, "%m/%d/%Y")
        sun = datetime.strptime(sun_str, "%m/%d/%Y")
        assert (sun - sat).days == 1

    def test_saturday_is_actually_saturday(self):
        sat_str, _ = bot.get_next_weekend_dates()
        sat = datetime.strptime(sat_str, "%m/%d/%Y")
        assert sat.weekday() == 5  # Monday=0, Saturday=5


class TestPhantomBlacklistShape:
    """Blacklist is a plain set of (date, course, time) tuples. These tests
    lock in the tuple shape so search_and_book_course and extract_available_slots
    stay in sync."""

    def test_tuple_key(self):
        blacklist = set()
        key = ("4/18/2026", "Lions", "9:00 AM")
        blacklist.add(key)
        assert key in blacklist

    def test_different_dates_dont_collide(self):
        blacklist = set()
        blacklist.add(("4/18/2026", "Lions", "9:00 AM"))
        assert ("4/19/2026", "Lions", "9:00 AM") not in blacklist

    def test_different_courses_dont_collide(self):
        blacklist = set()
        blacklist.add(("4/18/2026", "Lions", "9:00 AM"))
        assert ("4/18/2026", "Roy Kizer", "9:00 AM") not in blacklist


class TestCourseConfig:
    """Verify course configuration integrity."""

    def test_course_codes_order(self):
        """Courses should be searched in priority order: Lions > Roy Kizer > Jimmy Clay > Morris Williams."""
        names = list(config.COURSE_CODES.values())
        assert names == ["Lions", "Roy Kizer", "Jimmy Clay", "Morris Williams"]

    def test_all_course_codes_are_strings(self):
        for code, name in config.COURSE_CODES.items():
            assert isinstance(code, str), f"Code for {name} should be string"
            assert isinstance(name, str), f"Name for code {code} should be string"

    def test_no_duplicate_course_codes(self):
        codes = list(config.COURSE_CODES.keys())
        assert len(codes) == len(set(codes))

    def test_no_duplicate_course_names(self):
        names = list(config.COURSE_CODES.values())
        assert len(names) == len(set(names))


class TestPlayerFallback:
    def test_fallback_is_less_than_default(self):
        assert config.FALLBACK_NUM_PLAYERS < config.NUM_PLAYERS

    def test_fallback_is_at_least_1(self):
        assert config.FALLBACK_NUM_PLAYERS is None or config.FALLBACK_NUM_PLAYERS >= 1

    def test_default_is_4(self):
        assert config.NUM_PLAYERS == 4

    def test_fallback_is_2(self):
        assert config.FALLBACK_NUM_PLAYERS == 2


class TestNavRecoveryConstant:
    def test_max_nav_recovery_attempts_is_positive(self):
        assert bot.MAX_NAV_RECOVERY_ATTEMPTS >= 2


class TestStatePersistence:
    """State file should only be used for the current weekend, and should
    round-trip cleanly through save/load."""

    def setup_method(self):
        # Use a temp state file for isolation
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._orig_state = bot.STATE_FILE
        bot.STATE_FILE = f"{self._tmpdir}/state.json"

    def teardown_method(self):
        import shutil
        bot.STATE_FILE = self._orig_state
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_missing_file_returns_empty(self):
        state = bot.load_state("4/25/2026", "4/26/2026")
        assert state["saturday"]["success"] is False
        assert state["sunday"]["success"] is False

    def test_save_and_load_roundtrip(self):
        results = {
            "saturday": {"success": True, "details": "8:00 AM at Lions", "course": "Lions"},
            "sunday": {"success": False, "details": None, "course": None},
        }
        bot.save_state("4/25/2026", "4/26/2026", results)
        loaded = bot.load_state("4/25/2026", "4/26/2026")
        assert loaded["saturday"]["success"] is True
        assert loaded["saturday"]["details"] == "8:00 AM at Lions"
        assert loaded["sunday"]["success"] is False

    def test_stale_state_ignored(self):
        """State from a different weekend should not be loaded."""
        results = {
            "saturday": {"success": True, "details": "8:00 AM at Lions", "course": "Lions"},
            "sunday": {"success": True, "details": "9:00 AM at Roy Kizer", "course": "Roy Kizer"},
        }
        bot.save_state("4/11/2026", "4/12/2026", results)
        loaded = bot.load_state("4/25/2026", "4/26/2026")
        assert loaded["saturday"]["success"] is False
        assert loaded["sunday"]["success"] is False

    def test_clear_state_removes_file(self):
        results = {"saturday": {"success": True, "details": "x", "course": "x"},
                   "sunday": {"success": False, "details": None, "course": None}}
        bot.save_state("4/25/2026", "4/26/2026", results)
        import os
        assert os.path.exists(bot.STATE_FILE)
        bot.clear_state()
        assert not os.path.exists(bot.STATE_FILE)


class TestNotificationConfig:
    def test_notify_no_ops_without_channels(self, monkeypatch):
        """notify() should silently no-op when no channels are configured."""
        monkeypatch.setattr(bot, "NTFY_TOPIC", None)
        monkeypatch.setattr(bot, "SMTP_SERVER", None)
        # Should not raise
        bot.notify("test", "body")


class TestVerifyBookingOnPage:
    """Receipt-page verification must match every time format Vermont Systems
    can render. A mismatch here previously caused the bot to mark a successful
    booking as 'failed' and immediately re-book the next slot — producing
    ~20 real bookings before max_time expired.

    The slot's stored time is "8:32 AM"; the receipt may show:
      - "8:32 AM"  (exact)
      - "8:32AM"   (no space)
      - "8:32A"    (condensed, no M)
    All three must verify True.
    """

    SLOT = {"time": "8:32 AM", "course": "Lions", "date": "4/25/2026"}

    def _check(self, receipt_html: str) -> bool:
        # page arg is unused inside the function — None is fine here.
        return bot.verify_booking_on_page(None, self.SLOT, receipt_html.lower())

    def test_full_format_with_space(self):
        assert self._check(
            "<html>Receipt #12345 — Lions Municipal — 8:32 AM — 4 players</html>"
        )

    def test_full_format_no_space(self):
        assert self._check(
            "<html>Receipt #12345 — Lions Municipal — 8:32AM — 4 players</html>"
        )

    def test_condensed_format_no_m(self):
        # The bug case: receipt renders "8:32A" instead of "8:32 AM"
        assert self._check(
            "<html>Receipt #12345 — Lions Municipal — 8:32A — 4 players</html>"
        )

    def test_pm_condensed_format(self):
        slot = {"time": "1:32 PM", "course": "Lions", "date": "4/25/2026"}
        assert bot.verify_booking_on_page(
            None, slot,
            "<html>receipt #12345 — lions — 1:32p — 4 players</html>",
        )

    def test_wrong_time_returns_false(self):
        assert not self._check(
            "<html>Receipt #12345 — Lions Municipal — 9:00 AM — 4 players</html>"
        )

    def test_missing_course_returns_false(self):
        assert not self._check(
            "<html>Receipt #12345 — 8:32 AM — 4 players</html>"
        )


class TestHaltDayStatePersistence:
    """halt_day must round-trip through save/load so session retries (and
    crash restarts) don't re-attempt a day where we may have already booked
    but couldn't verify."""

    def setup_method(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._orig_state = bot.STATE_FILE
        bot.STATE_FILE = f"{self._tmpdir}/state.json"

    def teardown_method(self):
        import shutil
        bot.STATE_FILE = self._orig_state
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_halt_day_only_state_round_trips(self):
        """If a day was halted (no success), load_state must still resume it
        — otherwise the next session-loop iteration would re-attempt it."""
        results = {
            "saturday": {
                "success": False, "details": "UNVERIFIED — possible booking",
                "course": None, "halt_day": True,
            },
            "sunday": {"success": False, "details": None, "course": None},
        }
        bot.save_state("4/25/2026", "4/26/2026", results)
        loaded = bot.load_state("4/25/2026", "4/26/2026")
        assert loaded["saturday"].get("halt_day") is True
        assert loaded["sunday"].get("halt_day") in (False, None)

    def test_mixed_success_and_halt_round_trips(self):
        results = {
            "saturday": {"success": True, "details": "8:00 AM at Lions",
                         "course": "Lions"},
            "sunday": {"success": False, "details": "UNVERIFIED",
                       "course": None, "halt_day": True},
        }
        bot.save_state("4/25/2026", "4/26/2026", results)
        loaded = bot.load_state("4/25/2026", "4/26/2026")
        assert loaded["saturday"]["success"] is True
        assert loaded["sunday"].get("halt_day") is True
