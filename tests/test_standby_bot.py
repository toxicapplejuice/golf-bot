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


class TestGroupByAccount:
    def test_legacy_watches_group_under_default_none(self):
        w1, w2 = {"id": "a"}, {"id": "b"}
        assert standby_bot._group_by_account([w1, w2]) == {None: [w1, w2]}

    def test_explicit_account_gets_own_group(self):
        w1 = {"id": "a"}
        w2 = {"id": "b", "account_id": "grant"}
        groups = standby_bot._group_by_account([w1, w2])
        assert groups == {None: [w1], "grant": [w2]}

    def test_cli_default_applies_only_to_unpinned(self):
        w1 = {"id": "a"}
        w2 = {"id": "b", "account_id": "grant"}
        groups = standby_bot._group_by_account([w1, w2], default="christian")
        assert groups == {"christian": [w1], "grant": [w2]}

    def test_null_account_id_key_treated_as_default(self):
        # A stored watch may have account_id: null (JSON round-trip).
        w1 = {"id": "a", "account_id": None}
        w2 = {"id": "b", "account_id": "grant"}
        groups = standby_bot._group_by_account([w1, w2])
        assert groups == {None: [w1], "grant": [w2]}

    def test_same_account_watches_share_group(self):
        w1 = {"id": "a", "account_id": "grant"}
        w2 = {"id": "b", "account_id": "grant"}
        assert standby_bot._group_by_account([w1, w2]) == {"grant": [w1, w2]}


class TestSearchCourseCartBounce:
    """The session-expired (cart-bounce) recovery in _search_course.

    2026-07-24 postmortem: WebTrac bounced every checkout click to
    login.html while the browsing session still looked authenticated, so
    the old plain re-login was a no-op and every open slot was lost. The
    fix: force a REAL login (cookies cleared) and retry the same slot
    once; if it still bounces, alert the user to book manually.
    """

    SLOT = {"time": "10:51 AM", "course": "Roy Kizer", "date": "7/26/2026"}
    WATCH = {"id": "w1", "players": 4}

    def _run(self, statuses, fresh_login_ok=True, nav_ok=True, clear=True):
        """Drive _search_course with attempt_booking_click returning the
        given statuses in order. Returns (result, calls dict). clear=False
        preserves the module-level alert-dedupe set from a prior _run, to
        simulate the same slot re-appearing later in one check cycle."""
        calls = {"attempts": 0, "fresh_logins": 0, "notifies": []}

        def fake_attempt(page, slot, dry_run):
            calls["attempts"] += 1
            return statuses[calls["attempts"] - 1]

        def fake_fresh_login(page, queue_mode="timeout"):
            calls["fresh_logins"] += 1
            return fresh_login_ok

        def fake_notify(title, body, **kw):
            calls["notifies"].append(title)

        if clear:
            standby_bot._checkout_refused_alerted.clear()
        with patch.object(standby_bot, "navigate_to_search", lambda p, u: nav_ok), \
             patch.object(standby_bot, "extract_available_slots",
                          lambda *a, **k: [dict(self.SLOT)]), \
             patch.object(standby_bot, "parse_time", lambda t: 651), \
             patch.object(standby_bot, "attempt_booking_click", fake_attempt), \
             patch.object(standby_bot, "force_fresh_login", fake_fresh_login), \
             patch.object(standby_bot, "update_live_screenshot", lambda *a, **k: None), \
             patch.object(standby_bot, "verify_booking_via_history", lambda p, s: True), \
             patch.object(standby_bot.standby_queue, "mark_day_booked",
                          lambda *a, **k: None), \
             patch.object(standby_bot, "notify", fake_notify):
            result = standby_bot._search_course(
                page=None, course_code="GL", course_name="Roy Kizer",
                target_date="7/26/2026", num_players=4,
                min_minutes=420, max_minutes=660,
                watch=dict(self.WATCH), day="sunday",
                players_label="", dry_run=False,
            )
        return result, calls

    def test_bounce_then_success_books_after_fresh_login(self):
        result, calls = self._run(["session_expired", "booked"])
        assert result == "booked"
        assert calls["fresh_logins"] == 1
        assert calls["attempts"] == 2
        assert any("booked" in t.lower() for t in calls["notifies"])

    def test_double_bounce_alerts_user_to_book_manually(self):
        result, calls = self._run(["session_expired", "session_expired"])
        assert result == "continue"
        assert calls["fresh_logins"] == 1  # exactly one forced login per slot
        assert calls["attempts"] == 2      # never loops beyond the retry
        assert any("checkout refused" in t.lower() for t in calls["notifies"])

    def test_double_bounce_alert_deduped_within_cycle(self):
        _, calls1 = self._run(["session_expired", "session_expired"])
        assert len(calls1["notifies"]) == 1
        # Same slot seen again in the same process (e.g. lower player count):
        _, calls2 = self._run(["session_expired", "session_expired"], clear=False)
        assert calls2["notifies"] == []

    def test_fresh_login_failure_aborts(self):
        result, calls = self._run(["session_expired"], fresh_login_ok=False)
        assert result == "abort"
        assert calls["attempts"] == 1

    def test_no_bounce_books_without_fresh_login(self):
        result, calls = self._run(["booked"])
        assert result == "booked"
        assert calls["fresh_logins"] == 0
        assert calls["attempts"] == 1

    def test_taken_slot_does_not_trigger_fresh_login(self):
        result, calls = self._run(["taken"])
        assert result == "continue"
        assert calls["fresh_logins"] == 0
