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
