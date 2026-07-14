"""Tests for standby_bot.py pure helpers."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import standby_bot  # noqa: E402


def _watch(players, min_players=None):
    return {"players": players, "min_players": min_players}


class TestPlayerCounts:
    def test_floor_walks_down_to_floor(self):
        assert standby_bot._player_counts(_watch(4, min_players=3)) == [4, 3]

    def test_floor_never_goes_below(self):
        counts = standby_bot._player_counts(_watch(4, min_players=3))
        assert 2 not in counts
        assert min(counts) == 3

    def test_floor_equal_to_players_single_count(self):
        assert standby_bot._player_counts(_watch(3, min_players=3)) == [3]

    def test_floor_of_one_tries_everything(self):
        assert standby_bot._player_counts(_watch(4, min_players=1)) == [4, 3, 2, 1]

    def test_legacy_no_floor_uses_global_fallback(self):
        with patch.object(standby_bot, "FALLBACK_NUM_PLAYERS", 2):
            assert standby_bot._player_counts(_watch(4)) == [4, 2]

    def test_legacy_missing_key_uses_global_fallback(self):
        # Watches created before min_players existed have no key at all.
        with patch.object(standby_bot, "FALLBACK_NUM_PLAYERS", 2):
            assert standby_bot._player_counts({"players": 4}) == [4, 2]

    def test_legacy_no_fallback_when_disabled(self):
        with patch.object(standby_bot, "FALLBACK_NUM_PLAYERS", None):
            assert standby_bot._player_counts(_watch(4)) == [4]

    def test_legacy_no_fallback_when_not_smaller(self):
        with patch.object(standby_bot, "FALLBACK_NUM_PLAYERS", 2):
            assert standby_bot._player_counts(_watch(2)) == [2]


class TestSearchWindow:
    # Windows are minutes past midnight: morning 420-660 (7:00-11:00),
    # afternoon 780-1020 (1:00-5:00), all 480-1020 (8:00-5:00).

    def test_no_cap_uses_pref_window(self):
        w = {"time_pref": "morning", "max_hour": None}
        assert standby_bot._search_window(w) == (420, 660)

    def test_missing_key_uses_pref_window(self):
        # Watches created before max_hour existed have no key at all.
        assert standby_bot._search_window({"time_pref": "all"}) == (480, 1020)

    def test_cap_tightens_window(self):
        # max_hour=9 is inclusive: allows through 9:59 AM (minute 599).
        w = {"time_pref": "morning", "max_hour": 9}
        assert standby_bot._search_window(w) == (420, 599)

    def test_cap_at_window_end_hour_is_noop(self):
        # morning ends at 11:00 exactly (660); a cap of 11 reaches 11:59
        # (719) and must not widen the window.
        w = {"time_pref": "morning", "max_hour": 11}
        assert standby_bot._search_window(w) == (420, 660)

    def test_cap_wider_than_pref_is_ignored(self):
        w = {"time_pref": "morning", "max_hour": 16}
        assert standby_bot._search_window(w) == (420, 660)


class TestFmtMinutes:
    def test_on_the_hour(self):
        assert standby_bot._fmt_minutes(420) == "7:00"

    def test_padded_minutes(self):
        assert standby_bot._fmt_minutes(599) == "9:59"

    def test_afternoon_24h(self):
        assert standby_bot._fmt_minutes(1020) == "17:00"
