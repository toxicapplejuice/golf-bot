"""Tests for the morning player ladder and the reduced-party day cap.

2026-07-27: the Monday run booked four afternoon slots because the player
count was the OUTER loop — try_book_day ran morning AND afternoon at full
size, and only then would the caller retry smaller. So a full-party
afternoon slot always beat a smaller morning party, the opposite of the
preference. Player count is now the inner loop of build_attempt_plan.

The cap on smaller-party bookings exists because accounts book in parallel
and cross-account course diversity actively pushes siblings onto DIFFERENT
courses — so two reduced bookings would split the group across two courses
at two times, which is worse than one full-party afternoon slot.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot  # noqa: E402
import shared_state  # noqa: E402

WEEKEND = "8/01/2026 - 8/02/2026"


class TestBuildAttemptPlan:
    def test_morning_walks_down_before_afternoon(self):
        plan = bot.build_attempt_plan(4)
        assert plan == [
            ("morning/4p", bot.MAX_HOUR, 4),
            ("morning/3p", bot.MAX_HOUR, 3),
            ("morning/2p", bot.MAX_HOUR, 2),
            ("afternoon/4p", bot.FALLBACK_MAX_HOUR, 4),
        ]

    def test_every_morning_attempt_precedes_the_afternoon(self):
        # The core regression: no afternoon attempt may outrank any morning one.
        plan = bot.build_attempt_plan(4)
        first_afternoon = next(i for i, p in enumerate(plan)
                               if p[0].startswith("afternoon"))
        assert all(p[0].startswith("morning") for p in plan[:first_afternoon])

    def test_afternoon_is_full_size_only(self):
        # A reduced party in the afternoon is the worst of both worlds.
        plan = bot.build_attempt_plan(4)
        afternoons = [p for p in plan if p[0].startswith("afternoon")]
        assert afternoons == [("afternoon/4p", bot.FALLBACK_MAX_HOUR, 4)]

    def test_morning_attempts_use_the_morning_ceiling(self):
        for label, max_hour, _players in bot.build_attempt_plan(4):
            if label.startswith("morning"):
                assert max_hour == bot.MAX_HOUR

    def test_ladder_disabled_when_floor_equals_party(self):
        with patch.object(bot, "MIN_PLAYERS_MORNING", 4):
            assert bot.build_attempt_plan(4) == [
                ("morning/4p", bot.MAX_HOUR, 4),
                ("afternoon/4p", bot.FALLBACK_MAX_HOUR, 4),
            ]

    def test_floor_above_party_is_clamped_not_empty(self):
        with patch.object(bot, "MIN_PLAYERS_MORNING", 4):
            plan = bot.build_attempt_plan(2)
        assert plan == [
            ("morning/2p", bot.MAX_HOUR, 2),
            ("afternoon/2p", bot.FALLBACK_MAX_HOUR, 2),
        ]

    def test_floor_of_three_stops_at_three(self):
        with patch.object(bot, "MIN_PLAYERS_MORNING", 3):
            counts = [p[2] for p in bot.build_attempt_plan(4)
                      if p[0].startswith("morning")]
        assert counts == [4, 3]
        assert 2 not in counts

    def test_plan_is_never_empty(self):
        for n in (1, 2, 3, 4):
            assert bot.build_attempt_plan(n)


class TestTryBookDayFollowsThePlan:
    """The plan is only worth anything if try_book_day actually searches in
    that order. These assert the wiring, which build_attempt_plan alone
    cannot prove."""

    def _run(self, found_at=None, reduced_slots_left=1, weekend=None):
        """Drive try_book_day with search_and_book_course stubbed. `found_at`
        is the (players, max_hour) pair that should 'succeed'; None = nothing
        is ever found. Returns the ordered list of attempts made."""
        attempts = []

        def fake_search(page, code, name, date, num_players, max_hour,
                        blacklist, dry_run=False, weekend=None, day_name=None):
            attempts.append((num_players, max_hour))
            if found_at and (num_players, max_hour) == found_at:
                return {"success": True, "details": "8:40 AM at Lions",
                        "course": "Lions"}
            return {"success": False, "details": None, "course": None}

        class FakePage:
            def wait_for_timeout(self, _ms):
                pass

        with patch.object(bot, "search_and_book_course", fake_search), \
             patch.object(bot, "MAX_SEARCH_ROUNDS_PER_PASS", 1), \
             patch.object(bot, "COURSE_CODES", {"GL01": "Lions"}), \
             patch.object(bot.shared_state, "day_already_booked",
                          lambda w, d: (False, [])), \
             patch.object(bot.shared_state, "courses_booked",
                          lambda w, d: set()), \
             patch.object(bot.shared_state, "day_reduced_slots_left",
                          lambda w, d: reduced_slots_left):
            result = bot.try_book_day(
                page=FakePage(), date="8/01/2026", day_name="saturday",
                num_players=4, blacklist=set(), weekend=weekend,
            )
        return attempts, result

    def test_searches_morning_at_every_size_before_afternoon(self):
        attempts, _ = self._run()
        assert attempts == [
            (4, bot.MAX_HOUR), (3, bot.MAX_HOUR), (2, bot.MAX_HOUR),
            (4, bot.FALLBACK_MAX_HOUR),
        ]

    def test_stops_as_soon_as_full_morning_party_succeeds(self):
        attempts, result = self._run(found_at=(4, bot.MAX_HOUR))
        assert result["success"] is True
        assert attempts == [(4, bot.MAX_HOUR)]

    def test_reduced_morning_beats_full_afternoon(self):
        # The whole point: a 2-player morning slot must be taken and the
        # afternoon never reached.
        attempts, result = self._run(found_at=(2, bot.MAX_HOUR))
        assert result["success"] is True
        assert (4, bot.FALLBACK_MAX_HOUR) not in attempts

    def test_never_searches_a_reduced_afternoon(self):
        attempts, _ = self._run()
        afternoon = [a for a in attempts if a[1] == bot.FALLBACK_MAX_HOUR]
        assert afternoon == [(4, bot.FALLBACK_MAX_HOUR)]

    def test_reduced_passes_skipped_when_allowance_is_spent(self):
        # A sibling account already took today's one smaller-party booking,
        # so this account must go straight from morning/4p to the afternoon.
        attempts, _ = self._run(weekend=WEEKEND, reduced_slots_left=0)
        assert attempts == [(4, bot.MAX_HOUR), (4, bot.FALLBACK_MAX_HOUR)]

    def test_reduced_passes_run_when_allowance_remains(self):
        attempts, _ = self._run(weekend=WEEKEND, reduced_slots_left=1)
        assert (3, bot.MAX_HOUR) in attempts
        assert (2, bot.MAX_HOUR) in attempts


class TestReducedBookingCap:
    """NOTE: every test here MUST redirect SHARED_STATE_FILE to a temp path.
    These helpers write and delete the file at module scope, so pointing them
    at the default location destroys the live weekend's booking record."""

    @pytest.fixture(autouse=True)
    def _isolate_state_file(self, tmp_path):
        with patch.object(shared_state, "SHARED_STATE_FILE",
                          str(tmp_path / "shared_state.json")):
            shared_state.reset_for_weekend(WEEKEND)
            yield

    def _claim(self, account, players, course):
        return shared_state.claim_booking(
            WEEKEND, "saturday", f"8:00 AM at {course}", account,
            course=course, players=players,
        )[0]

    def test_first_reduced_booking_is_allowed(self):
        assert self._claim("grant", 2, "Lions") is True

    def test_second_reduced_booking_is_rejected(self):
        assert self._claim("grant", 2, "Lions") is True
        # This is the split-across-two-courses case the cap exists to stop.
        assert self._claim("christian", 2, "Jimmy Clay") is False

    def test_full_party_still_allowed_after_a_reduced_one(self):
        assert self._claim("grant", 2, "Lions") is True
        assert self._claim("christian", 4, "Jimmy Clay") is True

    def test_two_full_party_bookings_are_unaffected(self):
        assert self._claim("grant", 4, "Lions") is True
        assert self._claim("christian", 4, "Jimmy Clay") is True

    def test_reduced_rejected_even_when_day_has_capacity(self):
        # Capacity is 2/day; after one reduced + zero others there IS room,
        # but the reduced allowance alone must still block it.
        assert self._claim("grant", 3, "Lions") is True
        assert self._claim("christian", 3, "Jimmy Clay") is False
        is_full, _ = shared_state.day_already_booked(WEEKEND, "saturday")
        assert is_full is False

    def test_slots_left_reflects_the_cap(self):
        assert shared_state.day_reduced_slots_left(WEEKEND, "saturday") == 1
        self._claim("grant", 2, "Lions")
        assert shared_state.day_reduced_slots_left(WEEKEND, "saturday") == 0

    def test_full_party_booking_does_not_consume_the_allowance(self):
        self._claim("grant", 4, "Lions")
        assert shared_state.day_reduced_slots_left(WEEKEND, "saturday") == 1

    def test_players_is_persisted(self):
        self._claim("grant", 2, "Lions")
        state = shared_state.read_shared(WEEKEND)
        assert state["saturday"]["bookings"][0]["players"] == 2

    def test_legacy_booking_without_players_counts_as_full(self):
        # Entries written before `players` existed must not be read as
        # reduced — that would silently consume the allowance.
        shared_state.claim_booking(
            WEEKEND, "saturday", "8:00 AM at Lions", "grant", course="Lions")
        assert shared_state.day_reduced_slots_left(WEEKEND, "saturday") == 1
        assert self._claim("christian", 2, "Jimmy Clay") is True

    def test_sunday_has_its_own_allowance(self):
        assert self._claim("grant", 2, "Lions") is True
        claimed = shared_state.claim_booking(
            WEEKEND, "sunday", "8:00 AM at Lions", "grant",
            course="Lions", players=2)[0]
        assert claimed is True

    def test_is_reduced_booking_classification(self):
        assert shared_state.is_reduced_booking({"players": 2}) is True
        assert shared_state.is_reduced_booking({"players": 3}) is True
        assert shared_state.is_reduced_booking({"players": 4}) is False
        assert shared_state.is_reduced_booking({}) is False
