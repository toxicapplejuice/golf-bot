"""Tests for tournament_bot.py pure helpers — course parsing, consecutive /
tightest block finding, cross-course block choice, and the auto-fill nearest-
slot picker. No browser involved."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tournament_bot import (
    choose_block,
    find_consecutive_block,
    find_tightest_block,
    parse_clock,
    parse_courses,
    parse_window,
    pick_nearest_slot,
    slots_in_window,
)


def _slot(time_str, course="Roy Kizer", date="6/20/2026"):
    return {"time": time_str, "course": course, "date": date}


def _times(block):
    return [s["time"] for s in (block or [])]


# ----------------------------------------------------------------------
# window helpers (inlined copies, kept test-covered)
# ----------------------------------------------------------------------

class TestWindowHelpers:
    def test_parse_window_morning(self):
        assert parse_window("8:00 AM", "1:00 PM") == (480, 780)

    def test_parse_window_inverted_raises(self):
        with pytest.raises(ValueError, match="before"):
            parse_window("1:00 PM", "8:00 AM")

    def test_parse_clock_unparseable_raises(self):
        with pytest.raises(ValueError, match="--start"):
            parse_clock("--start", "noon")

    def test_slots_in_window_inclusive_bounds(self):
        slots = [_slot("7:59 AM"), _slot("8:00 AM"), _slot("1:00 PM"), _slot("1:01 PM")]
        assert _times(slots_in_window(slots, 480, 780)) == ["8:00 AM", "1:00 PM"]

    def test_slots_in_window_drops_unparseable(self):
        slots = [_slot("8:30 AM"), _slot("garbage")]
        assert _times(slots_in_window(slots, 480, 780)) == ["8:30 AM"]


# ----------------------------------------------------------------------
# parse_courses
# ----------------------------------------------------------------------

class TestParseCourses:
    def test_two_courses_to_codes_in_order(self):
        assert parse_courses("Roy Kizer,Jimmy Clay") == [
            ("2", "Roy Kizer"), ("1", "Jimmy Clay")]

    def test_case_insensitive(self):
        assert parse_courses("roy kizer") == [("2", "Roy Kizer")]

    def test_whitespace_tolerated(self):
        assert parse_courses("  Roy Kizer ,  Jimmy Clay ") == [
            ("2", "Roy Kizer"), ("1", "Jimmy Clay")]

    def test_order_preserved(self):
        assert parse_courses("Jimmy Clay,Roy Kizer") == [
            ("1", "Jimmy Clay"), ("2", "Roy Kizer")]

    def test_dedupes(self):
        assert parse_courses("Roy Kizer,Roy Kizer") == [("2", "Roy Kizer")]

    def test_unknown_course_raises(self):
        with pytest.raises(ValueError, match="Unknown course"):
            parse_courses("Pebble Beach")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            parse_courses(" , ")


# ----------------------------------------------------------------------
# find_consecutive_block
# ----------------------------------------------------------------------

class TestFindConsecutiveBlock:
    def test_clean_8_minute_run(self):
        slots = [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:16 AM")]
        assert _times(find_consecutive_block(slots, n=3, max_gap_min=10)) == [
            "8:00 AM", "8:08 AM", "8:16 AM"]

    def test_10_minute_run_within_gap(self):
        slots = [_slot("9:00 AM"), _slot("9:10 AM"), _slot("9:20 AM")]
        assert _times(find_consecutive_block(slots, n=3, max_gap_min=10)) == [
            "9:00 AM", "9:10 AM", "9:20 AM"]

    def test_gap_too_big_breaks_run(self):
        # 8:08 -> 8:20 is 12 min > max_gap 10
        slots = [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:20 AM")]
        assert find_consecutive_block(slots, n=3, max_gap_min=10) is None

    def test_picks_earliest_run(self):
        slots = [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:16 AM"),
                 _slot("9:00 AM"), _slot("9:08 AM"), _slot("9:16 AM")]
        assert _times(find_consecutive_block(slots, n=3)) == [
            "8:00 AM", "8:08 AM", "8:16 AM"]

    def test_slides_past_a_broken_prefix(self):
        # First two are far apart; the clean run starts at 8:30.
        slots = [_slot("8:00 AM"), _slot("8:30 AM"),
                 _slot("8:38 AM"), _slot("8:46 AM")]
        assert _times(find_consecutive_block(slots, n=3, max_gap_min=10)) == [
            "8:30 AM", "8:38 AM", "8:46 AM"]

    def test_unordered_input_is_sorted(self):
        slots = [_slot("8:16 AM"), _slot("8:00 AM"), _slot("8:08 AM")]
        assert _times(find_consecutive_block(slots, n=3)) == [
            "8:00 AM", "8:08 AM", "8:16 AM"]

    def test_too_few_slots_returns_none(self):
        assert find_consecutive_block([_slot("8:00 AM"), _slot("8:08 AM")], n=3) is None

    def test_n_two(self):
        slots = [_slot("8:00 AM"), _slot("8:40 AM"), _slot("8:48 AM")]
        assert _times(find_consecutive_block(slots, n=2, max_gap_min=10)) == [
            "8:40 AM", "8:48 AM"]


# ----------------------------------------------------------------------
# find_tightest_block
# ----------------------------------------------------------------------

class TestFindTightestBlock:
    def test_chooses_minimum_span_window(self):
        slots = [_slot("8:00 AM"), _slot("8:30 AM"),
                 _slot("9:00 AM"), _slot("9:05 AM"), _slot("9:10 AM")]
        assert _times(find_tightest_block(slots, n=3, max_spread_min=30)) == [
            "9:00 AM", "9:05 AM", "9:10 AM"]

    def test_respects_max_spread(self):
        # Every 3-window spans >= 60 min; none within 30.
        slots = [_slot("8:00 AM"), _slot("9:00 AM"), _slot("10:00 AM")]
        assert find_tightest_block(slots, n=3, max_spread_min=30) is None

    def test_ties_go_to_earlier(self):
        slots = [_slot("8:00 AM"), _slot("8:10 AM"), _slot("8:20 AM"),
                 _slot("9:00 AM"), _slot("9:10 AM"), _slot("9:20 AM")]
        # Both windows span 20 min; earlier wins.
        assert _times(find_tightest_block(slots, n=3, max_spread_min=30)) == [
            "8:00 AM", "8:10 AM", "8:20 AM"]

    def test_too_few_slots_returns_none(self):
        assert find_tightest_block([_slot("8:00 AM")], n=3) is None


# ----------------------------------------------------------------------
# choose_block
# ----------------------------------------------------------------------

class TestChooseBlock:
    def test_consecutive_beats_tight(self):
        course_slots = {
            ("2", "Roy Kizer"): [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:16 AM")],
            # gaps of 12 -> not consecutive, but spans 24 <= 30 -> tight
            ("1", "Jimmy Clay"): [_slot("9:00 AM"), _slot("9:12 AM"), _slot("9:24 AM")],
        }
        chosen = choose_block(course_slots, n=3, max_gap_min=10, max_spread_min=30)
        assert chosen["course_name"] == "Roy Kizer"
        assert chosen["kind"] == "consecutive"

    def test_course_priority_dominates_time(self):
        # Jimmy Clay is listed first (higher priority). Even though Roy Kizer's
        # block is earlier, the preferred course wins by default.
        course_slots = {
            ("1", "Jimmy Clay"): [_slot("9:00 AM"), _slot("9:08 AM"), _slot("9:16 AM")],
            ("2", "Roy Kizer"): [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:16 AM")],
        }
        chosen = choose_block(course_slots, n=3)
        assert chosen["course_name"] == "Jimmy Clay"
        assert _times(chosen["block"]) == ["9:00 AM", "9:08 AM", "9:16 AM"]

    def test_consecutive_beats_higher_priority_tight(self):
        # Jimmy Clay (higher priority) only has a scattered/tight block; Roy
        # Kizer has a clean consecutive one. Consecutiveness wins over course.
        course_slots = {
            ("1", "Jimmy Clay"): [_slot("8:00 AM"), _slot("8:14 AM"), _slot("8:28 AM")],
            ("2", "Roy Kizer"): [_slot("9:00 AM"), _slot("9:08 AM"), _slot("9:16 AM")],
        }
        chosen = choose_block(course_slots, n=3, max_gap_min=10, max_spread_min=30)
        assert chosen["course_name"] == "Roy Kizer"
        assert chosen["kind"] == "consecutive"

    def test_prefer_overrides_course_priority(self):
        # With an explicit --prefer target, closeness to it beats course order:
        # Jimmy Clay is higher priority but Roy Kizer sits on the target.
        course_slots = {
            ("1", "Jimmy Clay"): [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:16 AM")],
            ("2", "Roy Kizer"): [_slot("10:00 AM"), _slot("10:08 AM"), _slot("10:16 AM")],
        }
        chosen = choose_block(course_slots, n=3, prefer_min=10 * 60)
        assert chosen["course_name"] == "Roy Kizer"

    def test_prefer_min_pulls_toward_target(self):
        course_slots = {
            ("2", "Roy Kizer"): [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:16 AM")],
            ("1", "Jimmy Clay"): [_slot("10:00 AM"), _slot("10:08 AM"), _slot("10:16 AM")],
        }
        prefer = 10 * 60  # 10:00 AM
        chosen = choose_block(course_slots, n=3, prefer_min=prefer)
        assert chosen["course_name"] == "Jimmy Clay"

    def test_partial_fallback_when_no_full_block(self):
        course_slots = {
            ("2", "Roy Kizer"): [_slot("8:00 AM"), _slot("8:08 AM")],  # only 2
            ("1", "Jimmy Clay"): [_slot("9:00 AM")],                    # only 1
        }
        chosen = choose_block(course_slots, n=3)
        assert chosen["kind"] == "partial"
        assert chosen["course_name"] == "Roy Kizer"  # more available
        assert _times(chosen["block"]) == ["8:00 AM", "8:08 AM"]

    def test_no_slots_returns_none(self):
        assert choose_block({("2", "Roy Kizer"): [], ("1", "Jimmy Clay"): []}, n=3) is None


# ----------------------------------------------------------------------
# pick_nearest_slot
# ----------------------------------------------------------------------

class TestPickNearestSlot:
    def test_nearest_to_anchor(self):
        slots = [_slot("8:08 AM"), _slot("8:24 AM")]
        got = pick_nearest_slot(slots, anchor_min=480, used_times=set())  # 8:00
        assert got["time"] == "8:08 AM"

    def test_ties_go_to_earlier(self):
        slots = [_slot("8:00 AM"), _slot("8:16 AM")]
        got = pick_nearest_slot(slots, anchor_min=488, used_times=set())  # 8:08
        assert got["time"] == "8:00 AM"  # both 8 min away -> earlier

    def test_excludes_used_times(self):
        slots = [_slot("8:00 AM"), _slot("8:08 AM")]
        got = pick_nearest_slot(slots, anchor_min=480, used_times={"8:00 AM"})
        assert got["time"] == "8:08 AM"

    def test_none_when_all_used(self):
        slots = [_slot("8:00 AM")]
        assert pick_nearest_slot(slots, 480, used_times={"8:00 AM"}) is None

    def test_simulated_autofill_picks_three_distinct_consecutive(self):
        """Drive the booking loop's selection: anchor on the first win, exclude
        used times, and confirm three distinct adjacent slots get chosen."""
        slots = [_slot("8:00 AM"), _slot("8:08 AM"), _slot("8:16 AM"), _slot("8:24 AM")]
        used: set = set()
        anchor = 480  # block start 8:00
        chosen = []
        for _ in range(3):
            pick = pick_nearest_slot(slots, anchor, used)
            chosen.append(pick["time"])
            used.add(pick["time"])
            if len(chosen) == 1:
                anchor = 480  # first win stays the anchor (8:00)
        assert chosen == ["8:00 AM", "8:08 AM", "8:16 AM"]
