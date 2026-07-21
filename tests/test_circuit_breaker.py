"""Tests for the cart-bounce circuit breaker (2026-07-20 postmortem).

On 2026-07-20 the Add-to-cart click kept bouncing to login.html while the
browsing session was still valid, so the bot re-logged in (a no-op) and retried
the same slot 192 times without ever booking. These tests cover the breaker
that detects that pathological loop and aborts the run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot  # noqa: E402


class TestCircuitBreaker:
    def setup_method(self):
        # Each test starts from a clean, un-tripped circuit.
        bot.reset_session_circuit()

    def test_reset_clears_state(self):
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY):
            bot.note_cart_bounce_reexpiry()
        assert bot.session_circuit_tripped() is True
        bot.reset_session_circuit()
        assert bot.session_circuit_tripped() is False

    def test_does_not_trip_below_threshold(self):
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY - 1):
            tripped = bot.note_cart_bounce_reexpiry()
            assert tripped is False
        assert bot.session_circuit_tripped() is False

    def test_trips_at_threshold(self):
        tripped = False
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY):
            tripped = bot.note_cart_bounce_reexpiry()
        assert tripped is True
        assert bot.session_circuit_tripped() is True

    def test_return_value_matches_tripped_state(self):
        for i in range(1, bot.MAX_CART_BOUNCE_REEXPIRY + 1):
            ret = bot.note_cart_bounce_reexpiry()
            expected = i >= bot.MAX_CART_BOUNCE_REEXPIRY
            assert ret is expected

    def test_progress_resets_streak(self):
        # A cart action that reaches the site (taken/failed/booked) between
        # bounces breaks the consecutive streak, so the breaker never trips.
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY * 3):
            bot.note_cart_bounce_reexpiry()
            assert bot.session_circuit_tripped() is False
            bot.note_cart_progress()
        assert bot.session_circuit_tripped() is False

    def test_progress_partway_then_trips(self):
        # Bounce a few times, make progress (reset), then a full run of bounces
        # still trips — the streak is what matters, not the lifetime total.
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY - 1):
            bot.note_cart_bounce_reexpiry()
        bot.note_cart_progress()
        assert bot.session_circuit_tripped() is False
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY):
            bot.note_cart_bounce_reexpiry()
        assert bot.session_circuit_tripped() is True

    def test_once_tripped_stays_tripped_until_reset(self):
        for _ in range(bot.MAX_CART_BOUNCE_REEXPIRY):
            bot.note_cart_bounce_reexpiry()
        assert bot.session_circuit_tripped() is True
        # Progress after a trip does NOT un-trip it (abort is terminal for the run).
        bot.note_cart_progress()
        assert bot.session_circuit_tripped() is True
