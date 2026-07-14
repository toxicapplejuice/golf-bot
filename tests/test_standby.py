"""Tests for standby_queue.py standby watch functionality."""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import standby_queue  # noqa: E402


class TestTimePrefRanges:
    def test_morning_range(self):
        assert standby_queue.TIME_PREF_RANGES["morning"] == (420, 660)

    def test_afternoon_range(self):
        assert standby_queue.TIME_PREF_RANGES["afternoon"] == (780, 1020)

    def test_all_range(self):
        assert standby_queue.TIME_PREF_RANGES["all"] == (480, 1020)

    def test_valid_time_prefs_match_keys(self):
        assert set(standby_queue.VALID_TIME_PREFS) == set(standby_queue.TIME_PREF_RANGES.keys())

    def test_morning_excludes_after_11(self):
        min_m, max_m = standby_queue.TIME_PREF_RANGES["morning"]
        assert min_m == 7 * 60
        assert max_m == 11 * 60


class TestComputeExpiry:
    def test_sunday_end_of_day(self):
        result = standby_queue._compute_expiry("5/18/2026")
        assert result == "2026-05-18T23:59:59"

    def test_handles_padded_month(self):
        result = standby_queue._compute_expiry("12/07/2026")
        assert result == "2026-12-07T23:59:59"

    def test_handles_single_digit_month(self):
        result = standby_queue._compute_expiry("1/04/2026")
        assert result == "2026-01-04T23:59:59"


class TestGetUpcomingWeekendDates:
    def test_weekday_returns_this_weekend(self):
        # Wednesday May 14 2026 -> Fri May 15, Sat May 16, Sun May 17
        with patch("standby_queue.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 14, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            fri, sat, sun = standby_queue.get_upcoming_weekend_dates()
        assert "15" in fri
        assert "16" in sat
        assert "17" in sun

    def test_friday_returns_today(self):
        # Friday May 15 2026
        with patch("standby_queue.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 15, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            fri, sat, sun = standby_queue.get_upcoming_weekend_dates()
        assert "15" in fri
        assert "16" in sat
        assert "17" in sun

    def test_saturday_returns_yesterday_friday(self):
        with patch("standby_queue.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 16, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            fri, sat, sun = standby_queue.get_upcoming_weekend_dates()
        assert "15" in fri
        assert "16" in sat
        assert "17" in sun

    def test_sunday_returns_this_weekend(self):
        with patch("standby_queue.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 17, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            fri, sat, sun = standby_queue.get_upcoming_weekend_dates()
        assert "15" in fri
        assert "16" in sat
        assert "17" in sun

    def test_monday_returns_next_weekend(self):
        # Monday May 18 2026 -> Fri May 22, Sat May 23, Sun May 24
        with patch("standby_queue.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 18, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            fri, sat, sun = standby_queue.get_upcoming_weekend_dates()
        assert "22" in fri
        assert "23" in sat
        assert "24" in sun


class TestAddWatch:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = standby_queue.QUEUE_FILE
        standby_queue.QUEUE_FILE = os.path.join(self._tmpdir, "standby_queue.json")

    def teardown_method(self):
        standby_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_add_basic_watch(self):
        watch = standby_queue.add_watch(["saturday", "sunday"], "morning", 4)
        assert watch["status"] == "watching"
        assert watch["days"] == ["saturday", "sunday"]
        assert watch["time_pref"] == "morning"
        assert watch["players"] == 4
        assert watch["check_count"] == 0
        assert watch["last_checked_at"] is None
        assert "saturday" in watch["target_dates"]
        assert "sunday" in watch["target_dates"]
        assert watch["results"]["saturday"] is None
        assert watch["results"]["sunday"] is None
        assert len(watch["id"]) == 8

    def test_add_single_day_watch(self):
        watch = standby_queue.add_watch(["saturday"], "afternoon", 2)
        assert watch["days"] == ["saturday"]
        assert watch["time_pref"] == "afternoon"
        assert watch["players"] == 2
        assert "sunday" not in watch["target_dates"]

    def test_add_friday_watch(self):
        watch = standby_queue.add_watch(["friday"], "morning", 4)
        assert watch["days"] == ["friday"]
        assert "friday" in watch["target_dates"]
        assert watch["results"]["friday"] is None

    def test_add_friday_saturday_watch(self):
        watch = standby_queue.add_watch(["friday", "saturday"], "morning", 4)
        assert watch["days"] == ["friday", "saturday"]
        assert "friday" in watch["target_dates"]
        assert "saturday" in watch["target_dates"]

    def test_add_all_three_days(self):
        watch = standby_queue.add_watch(["friday", "saturday", "sunday"], "all", 2)
        assert watch["days"] == ["friday", "saturday", "sunday"]
        assert len(watch["target_dates"]) == 3
        assert all(d in watch["results"] for d in ["friday", "saturday", "sunday"])

    def test_add_persists_to_file(self):
        standby_queue.add_watch(["saturday"], "morning", 4)
        standby_queue.add_watch(["sunday"], "all", 2)
        watches = standby_queue.list_watches()
        assert len(watches) == 2

    def test_invalid_day_raises(self):
        try:
            standby_queue.add_watch(["monday"], "morning", 4)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "monday" in str(e)

    def test_thursday_invalid(self):
        try:
            standby_queue.add_watch(["thursday"], "morning", 4)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "thursday" in str(e)

    def test_invalid_time_pref_raises(self):
        try:
            standby_queue.add_watch(["saturday"], "evening", 4)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "evening" in str(e)

    def test_invalid_players_raises(self):
        try:
            standby_queue.add_watch(["saturday"], "morning", 0)
            assert False, "Expected ValueError"
        except ValueError:
            pass
        try:
            standby_queue.add_watch(["saturday"], "morning", 5)
            assert False, "Expected ValueError"
        except ValueError:
            pass

    def test_min_players_defaults_to_none(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        assert watch["min_players"] is None

    def test_min_players_stored(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4,
                                        min_players=3)
        assert watch["min_players"] == 3

    def test_min_players_equal_to_players_ok(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 3,
                                        min_players=3)
        assert watch["min_players"] == 3

    def test_min_players_above_players_raises(self):
        try:
            standby_queue.add_watch(["saturday"], "morning", 2, min_players=3)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "min_players" in str(e)

    def test_min_players_below_one_raises(self):
        try:
            standby_queue.add_watch(["saturday"], "morning", 4, min_players=0)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "min_players" in str(e)

    def test_max_hour_defaults_to_none(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        assert watch["max_hour"] is None

    def test_max_hour_stored(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4,
                                        max_hour=11)
        assert watch["max_hour"] == 11

    def test_max_hour_below_pref_min_raises(self):
        # afternoon starts at 13; an 11 cap would make the window empty
        try:
            standby_queue.add_watch(["saturday"], "afternoon", 4, max_hour=11)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "max_hour" in str(e)

    def test_max_hour_above_23_raises(self):
        try:
            standby_queue.add_watch(["saturday"], "morning", 4, max_hour=24)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "max_hour" in str(e)


class TestCancelWatch:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = standby_queue.QUEUE_FILE
        standby_queue.QUEUE_FILE = os.path.join(self._tmpdir, "standby_queue.json")

    def teardown_method(self):
        standby_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_cancel_existing_watch(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        assert standby_queue.cancel_watch(watch["id"]) is True
        watches = standby_queue.list_watches()
        assert watches[0]["status"] == "cancelled"

    def test_cancel_nonexistent_returns_false(self):
        assert standby_queue.cancel_watch("nonexistent") is False

    def test_cancel_already_cancelled_returns_false(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        standby_queue.cancel_watch(watch["id"])
        assert standby_queue.cancel_watch(watch["id"]) is False


class TestMarkDayBooked:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = standby_queue.QUEUE_FILE
        standby_queue.QUEUE_FILE = os.path.join(self._tmpdir, "standby_queue.json")

    def teardown_method(self):
        standby_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_mark_single_day_completes_watch(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        standby_queue.mark_day_booked(watch["id"], "saturday", "8:32 AM at Lions")
        watches = standby_queue.list_watches()
        assert watches[0]["status"] == "booked"
        assert watches[0]["results"]["saturday"]["booked"] is True
        assert watches[0]["results"]["saturday"]["details"] == "8:32 AM at Lions"

    def test_mark_one_of_two_days_stays_watching(self):
        watch = standby_queue.add_watch(["saturday", "sunday"], "morning", 4)
        standby_queue.mark_day_booked(watch["id"], "saturday", "8:32 AM at Lions")
        watches = standby_queue.list_watches()
        assert watches[0]["status"] == "watching"
        assert watches[0]["results"]["saturday"]["booked"] is True
        assert watches[0]["results"]["sunday"] is None

    def test_mark_both_days_completes_watch(self):
        watch = standby_queue.add_watch(["saturday", "sunday"], "morning", 4)
        standby_queue.mark_day_booked(watch["id"], "saturday", "8:32 AM at Lions")
        standby_queue.mark_day_booked(watch["id"], "sunday", "9:00 AM at Roy Kizer")
        watches = standby_queue.list_watches()
        assert watches[0]["status"] == "booked"

    def test_mark_friday_booked(self):
        watch = standby_queue.add_watch(["friday"], "morning", 4)
        standby_queue.mark_day_booked(watch["id"], "friday", "9:00 AM at Lions")
        watches = standby_queue.list_watches()
        assert watches[0]["status"] == "booked"
        assert watches[0]["results"]["friday"]["booked"] is True


class TestExpireStaleWatches:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = standby_queue.QUEUE_FILE
        standby_queue.QUEUE_FILE = os.path.join(self._tmpdir, "standby_queue.json")

    def teardown_method(self):
        standby_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_future_watch_not_expired(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        count = standby_queue.expire_stale_watches()
        assert count == 0
        watches = standby_queue.list_watches()
        assert watches[0]["status"] == "watching"

    def test_past_watch_gets_expired(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        # Manually set expires_at to the past
        with standby_queue._locked_file(standby_queue.QUEUE_FILE, "r+") as f:
            data = standby_queue._load(f)
            data["watches"][0]["expires_at"] = "2020-01-01T23:59:59"
            standby_queue._save(f, data)
        count = standby_queue.expire_stale_watches()
        assert count == 1
        watches = standby_queue.list_watches()
        assert watches[0]["status"] == "expired"

    def test_already_booked_not_expired(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        standby_queue.mark_day_booked(watch["id"], "saturday", "8:00 AM at Lions")
        count = standby_queue.expire_stale_watches()
        assert count == 0


class TestUpdateWatchCheck:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = standby_queue.QUEUE_FILE
        standby_queue.QUEUE_FILE = os.path.join(self._tmpdir, "standby_queue.json")

    def teardown_method(self):
        standby_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_increments_check_count(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        standby_queue.update_watch_check(watch["id"])
        standby_queue.update_watch_check(watch["id"])
        watches = standby_queue.list_watches()
        assert watches[0]["check_count"] == 2
        assert watches[0]["last_checked_at"] is not None


class TestGetActiveWatches:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = standby_queue.QUEUE_FILE
        standby_queue.QUEUE_FILE = os.path.join(self._tmpdir, "standby_queue.json")

    def teardown_method(self):
        standby_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_returns_only_watching(self):
        w1 = standby_queue.add_watch(["saturday"], "morning", 4)
        w2 = standby_queue.add_watch(["sunday"], "morning", 4)
        standby_queue.cancel_watch(w2["id"])
        active = standby_queue.get_active_watches()
        assert len(active) == 1
        assert active[0]["id"] == w1["id"]

    def test_excludes_expired(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        with standby_queue._locked_file(standby_queue.QUEUE_FILE, "r+") as f:
            data = standby_queue._load(f)
            data["watches"][0]["expires_at"] = "2020-01-01T23:59:59"
            standby_queue._save(f, data)
        active = standby_queue.get_active_watches()
        assert len(active) == 0


class TestClearOldWatches:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = standby_queue.QUEUE_FILE
        standby_queue.QUEUE_FILE = os.path.join(self._tmpdir, "standby_queue.json")

    def teardown_method(self):
        standby_queue.QUEUE_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_removes_old_terminal_watches(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        standby_queue.cancel_watch(watch["id"])
        # Set created_at to 30 days ago
        with standby_queue._locked_file(standby_queue.QUEUE_FILE, "r+") as f:
            data = standby_queue._load(f)
            data["watches"][0]["created_at"] = "2020-01-01T00:00:00"
            standby_queue._save(f, data)
        removed = standby_queue.clear_old_watches(keep_days=14)
        assert removed == 1
        assert len(standby_queue.list_watches()) == 0

    def test_keeps_recent_terminal_watches(self):
        watch = standby_queue.add_watch(["saturday"], "morning", 4)
        standby_queue.cancel_watch(watch["id"])
        removed = standby_queue.clear_old_watches(keep_days=14)
        assert removed == 0
        assert len(standby_queue.list_watches()) == 1

    def test_keeps_active_watches_regardless_of_age(self):
        standby_queue.add_watch(["saturday"], "morning", 4)
        with standby_queue._locked_file(standby_queue.QUEUE_FILE, "r+") as f:
            data = standby_queue._load(f)
            data["watches"][0]["created_at"] = "2020-01-01T00:00:00"
            standby_queue._save(f, data)
        removed = standby_queue.clear_old_watches(keep_days=14)
        assert removed == 0
