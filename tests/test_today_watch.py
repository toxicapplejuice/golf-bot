"""Tests for today_watch.py pure helpers (window parsing/filtering,
date relation, holes parsing) and the build_search_url holes param."""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import build_search_url
from today_watch import (
    date_relation,
    parse_clock,
    parse_holes,
    parse_window,
    slots_in_window,
    sort_by_preference,
)


def _slot(time_str):
    return {"time": time_str, "course": "Roy Kizer", "date": "6/11/2026"}


class TestParseWindow:
    def test_evening_window(self):
        assert parse_window("5:30 PM", "6:30 PM") == (17 * 60 + 30, 18 * 60 + 30)

    def test_morning_window(self):
        assert parse_window("8:00 AM", "1:00 PM") == (480, 780)

    def test_noon_is_720_not_zero(self):
        assert parse_window("11:30 AM", "12:30 PM") == (690, 750)

    def test_midnight_start(self):
        assert parse_window("12:15 AM", "1:00 AM") == (15, 60)

    def test_unparseable_start_raises(self):
        with pytest.raises(ValueError, match="--start"):
            parse_window("5:30", "6:30 PM")  # missing AM/PM

    def test_unparseable_end_raises(self):
        with pytest.raises(ValueError, match="--end"):
            parse_window("5:30 PM", "later")

    def test_inverted_window_raises(self):
        with pytest.raises(ValueError, match="before"):
            parse_window("6:30 PM", "5:30 PM")

    def test_empty_window_raises(self):
        with pytest.raises(ValueError, match="before"):
            parse_window("5:30 PM", "5:30 PM")


class TestSlotsInWindow:
    WINDOW = (17 * 60 + 30, 18 * 60 + 30)  # 5:30-6:30 PM

    def _times(self, slots):
        return [s["time"] for s in slots_in_window(slots, *self.WINDOW)]

    def test_start_boundary_inclusive(self):
        assert self._times([_slot("5:30 PM")]) == ["5:30 PM"]

    def test_end_boundary_inclusive(self):
        assert self._times([_slot("6:30 PM")]) == ["6:30 PM"]

    def test_just_before_start_excluded(self):
        assert self._times([_slot("5:29 PM")]) == []

    def test_just_after_end_excluded(self):
        assert self._times([_slot("6:31 PM")]) == []

    def test_morning_time_excluded(self):
        # Same clock digits, wrong half of day — AM must not leak in
        assert self._times([_slot("5:45 AM")]) == []

    def test_unparseable_time_excluded(self):
        assert self._times([_slot("TBD")]) == []

    def test_mixed_preserves_order_and_dicts(self):
        slots = [_slot("5:00 PM"), _slot("5:40 PM"), _slot("6:08 PM"),
                 _slot("6:48 PM")]
        kept = slots_in_window(slots, *self.WINDOW)
        assert kept == [slots[1], slots[2]]


class TestDateRelation:
    TODAY = date(2026, 6, 11)

    def test_today_unpadded(self):
        assert date_relation("6/11/2026", self.TODAY) == "today"

    def test_today_padded(self):
        assert date_relation("06/11/2026", self.TODAY) == "today"

    def test_past(self):
        assert date_relation("6/10/2026", self.TODAY) == "past"

    def test_future(self):
        assert date_relation("6/12/2026", self.TODAY) == "future"

    def test_year_boundary(self):
        assert date_relation("1/1/2027", self.TODAY) == "future"

    def test_malformed_raises(self):
        with pytest.raises(ValueError):
            date_relation("2026-06-11", self.TODAY)

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            date_relation("tonight", self.TODAY)


class TestParseHoles:
    def test_default_pair(self):
        assert parse_holes("18,9") == [18, 9]

    def test_single(self):
        assert parse_holes("18") == [18]

    def test_whitespace_and_order(self):
        assert parse_holes(" 9 , 18 ") == [9, 18]

    def test_dedupes(self):
        assert parse_holes("18,18,9") == [18, 9]

    def test_invalid_count_raises(self):
        with pytest.raises(ValueError, match="9 or 18"):
            parse_holes("12")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            parse_holes("")


class TestBuildSearchUrlHoles:
    def test_default_is_18(self):
        url = build_search_url("2", "6/11/2026", 2)
        assert "numberofholes=18" in url

    def test_explicit_9(self):
        url = build_search_url("2", "6/11/2026", 2, holes=9)
        assert "numberofholes=9" in url
        assert "numberofholes=18" not in url

    def test_other_params_unchanged(self):
        url = build_search_url("2", "6/11/2026", 2, holes=9)
        assert "secondarycode=2" in url
        assert "begindate=6/11/2026" in url
        assert "numberofplayers=2" in url
        assert "Action=Start" in url


class TestParseClock:
    def test_parses_pm(self):
        assert parse_clock("--prefer", "6:00 PM") == 18 * 60

    def test_error_names_the_flag(self):
        with pytest.raises(ValueError, match="--prefer"):
            parse_clock("--prefer", "sixish")


class TestSortByPreference:
    SIX_PM = 18 * 60

    def _times(self, slots):
        return [s["time"] for s in sort_by_preference(slots, self.SIX_PM)]

    def test_orders_by_distance_from_target(self):
        slots = [_slot("5:30 PM"), _slot("6:24 PM"), _slot("6:08 PM")]
        assert self._times(slots) == ["6:08 PM", "6:24 PM", "5:30 PM"]

    def test_exact_match_first(self):
        slots = [_slot("5:54 PM"), _slot("6:00 PM")]
        assert self._times(slots) == ["6:00 PM", "5:54 PM"]

    def test_tie_prefers_earlier_time(self):
        # 5:52 and 6:08 are both 8 minutes from 6:00 — earlier wins
        slots = [_slot("6:08 PM"), _slot("5:52 PM")]
        assert self._times(slots) == ["5:52 PM", "6:08 PM"]

    def test_does_not_mutate_input(self):
        slots = [_slot("6:24 PM"), _slot("5:52 PM")]
        snapshot = list(slots)
        sort_by_preference(slots, self.SIX_PM)
        assert slots == snapshot
