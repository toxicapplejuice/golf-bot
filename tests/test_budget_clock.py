"""Tests for the --max-time budget clock (2026-09-07 postmortem).

The budget must measure BOOKING time, not wall-clock since launch. On
2026-09-07 the 7:40 PM launch with a 1800s budget killed every account at
20:11 — mid Sunday's morning pass — because 20 of the 30 minutes were spent
in the pre-release sleep. Sunday finished 0/2 with its afternoon pass never
having run.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


def ts(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s).timestamp()


class TestComputeBudgetStart:
    def test_immediate_mode_starts_now(self):
        """--now has no pre-release wait to exclude."""
        now = ts(2026, 9, 7, 19, 40)
        assert bot.compute_budget_start(now, immediate=True) == now

    def test_immediate_mode_ignores_release_time(self):
        """Even well before release, --now budgets from this moment."""
        now = ts(2026, 9, 7, 12, 0)
        assert bot.compute_budget_start(now, immediate=True) == now

    def test_scheduled_before_release_starts_at_release(self):
        """The 7:40 PM launch budgets from 8:00 PM, not 7:40."""
        launch = ts(2026, 9, 7, 19, 40)
        assert bot.compute_budget_start(launch, immediate=False) == ts(2026, 9, 7, 20, 0)

    def test_the_actual_2026_09_07_regression(self):
        """1800s from a 7:40 launch must now expire at 8:30, not 8:10."""
        launch = ts(2026, 9, 7, 19, 40, 1)
        start = bot.compute_budget_start(launch, immediate=False)
        assert datetime.fromtimestamp(start + 1800) == datetime(2026, 9, 7, 20, 30)

    def test_earlier_launch_does_not_shrink_budget(self):
        """The whole point: moving the launch earlier costs no booking time."""
        early = bot.compute_budget_start(ts(2026, 9, 7, 19, 20), immediate=False)
        late = bot.compute_budget_start(ts(2026, 9, 7, 19, 55), immediate=False)
        assert early == late == ts(2026, 9, 7, 20, 0)

    def test_scheduled_after_release_starts_now(self):
        """A late launch cannot retroactively claim time it missed."""
        now = ts(2026, 9, 7, 20, 5)
        assert bot.compute_budget_start(now, immediate=False) == now

    def test_scheduled_exactly_at_release_starts_now(self):
        now = ts(2026, 9, 7, 20, 0)
        assert bot.compute_budget_start(now, immediate=False) == now

    def test_one_second_before_release(self):
        now = ts(2026, 9, 7, 19, 59, 59)
        assert bot.compute_budget_start(now, immediate=False) == ts(2026, 9, 7, 20, 0)

    def test_budget_start_may_be_in_the_future(self):
        """elapsed goes negative during the wait — that must not trip the cap."""
        launch = ts(2026, 9, 7, 19, 40)
        start = bot.compute_budget_start(launch, immediate=False)
        elapsed = launch - start
        assert elapsed < 0
        assert not (elapsed > 1800)

    def test_uses_release_constants_not_hardcoded_8pm(self):
        """Track RELEASE_HOUR/MINUTE so moving release moves the budget."""
        launch = ts(2026, 9, 7, 19, 40)
        start = bot.compute_budget_start(launch, immediate=False)
        expected = datetime(2026, 9, 7, bot.RELEASE_HOUR, bot.RELEASE_MINUTE)
        assert datetime.fromtimestamp(start) == expected

    def test_pre_dawn_launch_same_day_release(self):
        """A same-day morning launch still waits for that evening's release."""
        now = ts(2026, 9, 7, 3, 0)
        assert bot.compute_budget_start(now, immediate=False) == ts(2026, 9, 7, 20, 0)
