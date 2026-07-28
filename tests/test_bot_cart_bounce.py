"""Tests for cart-bounce (session-expired) recovery on the Monday-release
path (bot.search_and_book_course) and the same-day watcher
(today_watch._try_slots).

2026-07-20 postmortem: WebTrac bounced every checkout click to login.html
while the browsing session still looked authenticated, so the plain
re-login was a no-op and the same slot bounced forever. The fix (ported
from standby_bot._search_course, see TestSearchCourseCartBounce): force a
REAL login (cookies cleared) and retry the same slot ONCE; only a bounce
that survives the fresh session counts toward the circuit breaker.
"""

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot  # noqa: E402
import today_watch  # noqa: E402


SLOT = {"time": "9:00 AM", "course": "Lions", "date": "7/25/2026"}


class TestSearchAndBookCourseCartBounce:
    """Monday-release path: one force_fresh_login + same-slot retry per
    bounce; a second bounce counts toward MAX_CART_BOUNCE_REEXPIRY."""

    def setup_method(self):
        bot.reset_session_circuit()

    def _run(self, statuses, fresh_login_ok=True, recovery_nav_ok=True,
             blacklist=None):
        """Drive search_and_book_course with attempt_booking_click returning
        the given statuses in order. The FIRST navigate_to_search call (page
        load for the search) always succeeds; later ones (post-bounce /
        post-taken recovery) return recovery_nav_ok."""
        calls = {"attempts": 0, "fresh_logins": 0, "navs": 0}

        def fake_attempt(page, slot, dry_run):
            calls["attempts"] += 1
            return statuses[calls["attempts"] - 1]

        def fake_fresh_login(page, queue_mode="timeout"):
            calls["fresh_logins"] += 1
            return fresh_login_ok

        def fake_nav(page, url):
            calls["navs"] += 1
            return True if calls["navs"] == 1 else recovery_nav_ok

        blacklist = set() if blacklist is None else blacklist
        with patch.object(bot, "navigate_to_search", fake_nav), \
             patch.object(bot, "extract_available_slots",
                          lambda *a, **k: [dict(SLOT)]), \
             patch.object(bot, "attempt_booking_click", fake_attempt), \
             patch.object(bot, "force_fresh_login", fake_fresh_login), \
             patch.object(bot, "update_live_screenshot", lambda *a, **k: None), \
             patch.object(bot, "save_debug_screenshot", lambda *a, **k: None), \
             patch.object(bot, "verify_booking_via_history", lambda p, s: True), \
             patch.object(bot, "notify", lambda *a, **k: None), \
             patch.object(bot, "send_ntfy", lambda *a, **k: None):
            result = bot.search_and_book_course(
                page=None, course_code="GL01", course_name="Lions",
                date="7/25/2026", num_players=4, max_hour=13,
                blacklist=blacklist, dry_run=False,
                weekend=None, day_name=None,
            )
        return result, calls

    def test_bounce_then_success_books_after_fresh_login(self):
        result, calls = self._run(["session_expired", "booked"])
        assert result["success"] is True
        assert calls["fresh_logins"] == 1
        assert calls["attempts"] == 2
        # A successful booking is not a surviving bounce — breaker untouched.
        assert bot._cart_bounce_reexpiry == 0

    def test_double_bounce_counts_toward_breaker(self):
        result, calls = self._run(["session_expired", "session_expired"])
        assert result["success"] is False
        assert "abort_run" not in result
        assert calls["fresh_logins"] == 1  # exactly one forced login per slot
        assert calls["attempts"] == 2      # never loops beyond the retry
        assert bot._cart_bounce_reexpiry == 1
        assert bot.session_circuit_tripped() is False

    def test_double_bounce_at_threshold_trips_breaker(self):
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY - 1):
            bot.note_cart_bounce_reexpiry()
        result, calls = self._run(["session_expired", "session_expired"])
        assert result.get("abort_run") is True
        assert bot.session_circuit_tripped() is True
        assert calls["fresh_logins"] == 1

    def test_single_bounce_does_not_count_toward_breaker(self):
        # The whole point of the port: a bounce that a fresh login fixes
        # must NOT move the breaker (it used to, when re-login was a no-op).
        self._run(["session_expired", "booked"])
        assert bot._cart_bounce_reexpiry == 0

    def test_bounce_then_taken_resets_streak(self):
        bot.note_cart_bounce_reexpiry()
        bot.note_cart_bounce_reexpiry()
        result, calls = self._run(["session_expired", "taken"])
        assert calls["fresh_logins"] == 1
        assert calls["attempts"] == 2
        # "taken" reached the site — note_cart_progress clears the streak.
        assert bot._cart_bounce_reexpiry == 0

    def test_fresh_login_failure_gives_up_on_course(self):
        result, calls = self._run(["session_expired"], fresh_login_ok=False)
        assert result["success"] is False
        assert calls["attempts"] == 1

    def test_nav_failure_after_fresh_login_gives_up_on_course(self):
        result, calls = self._run(["session_expired"], recovery_nav_ok=False)
        assert result["success"] is False
        assert calls["fresh_logins"] == 1
        assert calls["attempts"] == 1

    def test_no_bounce_books_without_fresh_login(self):
        result, calls = self._run(["booked"])
        assert result["success"] is True
        assert calls["fresh_logins"] == 0
        assert calls["attempts"] == 1

    def test_taken_slot_does_not_trigger_fresh_login(self):
        blacklist = set()
        result, calls = self._run(["taken"], blacklist=blacklist)
        assert result["success"] is False
        assert calls["fresh_logins"] == 0
        assert (SLOT["date"], SLOT["course"], SLOT["time"]) in blacklist


class TestTrySlotsCartBounce:
    """today_watch._try_slots: same one-fresh-login-per-slot retry; a second
    bounce ends the cycle (next cycle gets a fresh browser + login)."""

    def _run(self, statuses, fresh_login_ok=True, nav_ok=True):
        calls = {"attempts": 0, "fresh_logins": 0}
        tried = set()

        def fake_attempt(page, slot, dry_run):
            calls["attempts"] += 1
            return statuses[calls["attempts"] - 1]

        def fake_fresh_login(page, queue_mode="timeout"):
            calls["fresh_logins"] += 1
            return fresh_login_ok

        args = argparse.Namespace(dry_run=False, players=2, success_note=None)
        with patch.object(today_watch, "navigate_to_search",
                          lambda p, u: nav_ok), \
             patch.object(today_watch, "attempt_booking_click", fake_attempt), \
             patch.object(today_watch, "force_fresh_login", fake_fresh_login), \
             patch.object(today_watch, "update_live_screenshot",
                          lambda *a, **k: None), \
             patch.object(today_watch, "save_debug_screenshot",
                          lambda *a, **k: None), \
             patch.object(today_watch, "verify_booking_via_history",
                          lambda p, s: True), \
             patch.object(today_watch, "notify", lambda *a, **k: None):
            outcome = today_watch._try_slots(
                page=None, slots=[dict(SLOT)], url="u", args=args, tried=tried)
        return outcome, calls, tried

    def test_bounce_then_success_books_after_fresh_login(self):
        outcome, calls, _ = self._run(["session_expired", "booked"])
        assert outcome == "booked"
        assert calls["fresh_logins"] == 1
        assert calls["attempts"] == 2

    def test_double_bounce_ends_cycle(self):
        outcome, calls, _ = self._run(["session_expired", "session_expired"])
        assert outcome is None
        assert calls["fresh_logins"] == 1  # never burns a second login
        assert calls["attempts"] == 2

    def test_fresh_login_failure_ends_cycle(self):
        outcome, calls, _ = self._run(["session_expired"], fresh_login_ok=False)
        assert outcome is None
        assert calls["attempts"] == 1

    def test_no_bounce_books_without_fresh_login(self):
        outcome, calls, _ = self._run(["booked"])
        assert outcome == "booked"
        assert calls["fresh_logins"] == 0
        assert calls["attempts"] == 1

    def test_taken_slot_does_not_trigger_fresh_login(self):
        outcome, calls, tried = self._run(["taken"])
        assert outcome is None
        assert calls["fresh_logins"] == 0
        assert (SLOT["date"], SLOT["course"], SLOT["time"]) in tried
