"""Tests for navigate_to_search's goto-retry budget (2026-09-07 postmortem).

WebTrac stopped serving pages for ~6 minutes after the 8:00 PM release. With
3 attempts x 12s, each dead course cost 36s and taught the bot nothing; 89
timeouts across three accounts consumed the entire morning/4p pass. Rush mode
must now take ONE short look and rotate to the next course, while Queue-it and
session-expiry recovery keep their full budget.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


class FakePage:
    """Minimal page double: a scripted list of goto outcomes + page states."""

    def __init__(self, outcomes, states=None):
        self.outcomes = list(outcomes)
        self.states = list(states or [])
        self.goto_calls = []

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls.append({"url": url, "timeout": timeout})
        if not self.outcomes:
            return None
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def next_state(self):
        return self.states.pop(0) if self.states else "target"


@pytest.fixture(autouse=True)
def _reset_rush():
    bot.set_rush_mode(False)
    yield
    bot.set_rush_mode(False)


@pytest.fixture
def stub_page_state(monkeypatch):
    """Route is_in_queue / is_on_login_page through FakePage.next_state()."""
    def apply(page):
        holder = {"state": None}

        def _advance():
            holder["state"] = page.next_state()

        def is_in_queue(p):
            _advance()
            return holder["state"] == "queue"

        def is_on_login_page(p):
            return holder["state"] == "login"

        monkeypatch.setattr(bot, "is_in_queue", is_in_queue)
        monkeypatch.setattr(bot, "is_on_login_page", is_on_login_page)
        monkeypatch.setattr(bot, "save_debug_screenshot", lambda *a, **k: None)
    return apply


def timeout_exc():
    return bot.PlaywrightTimeout("nav timeout")


class TestRushFailsFast:
    def test_rush_gives_up_after_one_timeout(self, stub_page_state):
        """The core fix: one look, then rotate to the next course."""
        bot.set_rush_mode(True)
        page = FakePage([timeout_exc(), timeout_exc(), timeout_exc()])
        stub_page_state(page)
        assert bot.navigate_to_search(page, "http://x") is False
        assert len(page.goto_calls) == 1

    def test_rush_uses_the_short_timeout(self, stub_page_state):
        bot.set_rush_mode(True)
        page = FakePage([timeout_exc()])
        stub_page_state(page)
        bot.navigate_to_search(page, "http://x")
        assert page.goto_calls[0]["timeout"] == bot.NAV_TIMEOUT_RUSH

    def test_rush_dead_course_costs_one_timeout_not_three(self):
        """Budget math the fix is meant to deliver."""
        old = 3 * 12000
        new = bot.NAV_GOTO_ATTEMPTS_RUSH * bot.NAV_TIMEOUT_RUSH
        assert new < old
        assert new * 4 <= 30000, "a 4-course sweep must cost <= ~30s"

    def test_rush_still_succeeds_when_page_loads(self, stub_page_state):
        bot.set_rush_mode(True)
        page = FakePage([None], states=["target"])
        stub_page_state(page)
        assert bot.navigate_to_search(page, "http://x") is True


class TestNormalModeUnchanged:
    def test_normal_mode_retries_three_times(self, stub_page_state):
        page = FakePage([timeout_exc(), timeout_exc(), timeout_exc()])
        stub_page_state(page)
        assert bot.navigate_to_search(page, "http://x") is False
        assert len(page.goto_calls) == bot.NAV_GOTO_ATTEMPTS_NORMAL == 3

    def test_normal_mode_uses_long_timeout(self, stub_page_state):
        page = FakePage([timeout_exc()])
        stub_page_state(page)
        bot.navigate_to_search(page, "http://x")
        assert page.goto_calls[0]["timeout"] == bot.NAV_TIMEOUT_NORMAL

    def test_normal_mode_recovers_on_second_attempt(self, stub_page_state):
        page = FakePage([timeout_exc(), None], states=["target"])
        stub_page_state(page)
        assert bot.navigate_to_search(page, "http://x") is True
        assert len(page.goto_calls) == 2


class TestRecoveryBudgetIsSeparate:
    """The Queue-it / session-expiry funnel must NOT be shortened by rush mode.

    That funnel is the entire reason navigate_to_search exists (CLAUDE.md,
    2026-04-13 Queue-it incident). Failing fast on a dead socket is fine;
    failing fast on a queue release would re-introduce the old bug.
    """

    def test_rush_still_renavigates_after_queue_release(self, monkeypatch, stub_page_state):
        bot.set_rush_mode(True)
        page = FakePage([None, None], states=["queue", "target"])
        stub_page_state(page)
        monkeypatch.setattr(bot, "wait_for_queue", lambda p, mode=None: True)
        assert bot.navigate_to_search(page, "http://x") is True
        assert len(page.goto_calls) == 2, "must re-navigate after the queue"

    def test_rush_still_renavigates_after_relogin(self, monkeypatch, stub_page_state):
        bot.set_rush_mode(True)
        page = FakePage([None, None], states=["login", "target"])
        stub_page_state(page)
        monkeypatch.setattr(bot, "login_with_retry", lambda p, queue_mode=None: True)
        assert bot.navigate_to_search(page, "http://x") is True
        assert len(page.goto_calls) == 2

    def test_rush_handles_chained_queue_then_login(self, monkeypatch, stub_page_state):
        """Chained recovery still gets its full MAX_NAV_RECOVERY_ATTEMPTS."""
        bot.set_rush_mode(True)
        page = FakePage([None, None, None], states=["queue", "login", "target"])
        stub_page_state(page)
        monkeypatch.setattr(bot, "wait_for_queue", lambda p, mode=None: True)
        monkeypatch.setattr(bot, "login_with_retry", lambda p, queue_mode=None: True)
        assert bot.navigate_to_search(page, "http://x") is True
        assert len(page.goto_calls) == 3

    def test_queue_timeout_still_fails_closed(self, monkeypatch, stub_page_state):
        bot.set_rush_mode(True)
        page = FakePage([None], states=["queue"])
        stub_page_state(page)
        monkeypatch.setattr(bot, "wait_for_queue", lambda p, mode=None: False)
        assert bot.navigate_to_search(page, "http://x") is False

    def test_relogin_failure_still_fails_closed(self, monkeypatch, stub_page_state):
        bot.set_rush_mode(True)
        page = FakePage([None], states=["login"])
        stub_page_state(page)
        monkeypatch.setattr(bot, "login_with_retry", lambda p, queue_mode=None: False)
        assert bot.navigate_to_search(page, "http://x") is False


class TestNonTimeoutErrors:
    def test_playwright_error_counts_against_goto_budget(self, stub_page_state):
        bot.set_rush_mode(True)
        page = FakePage([bot.PlaywrightError("boom")])
        stub_page_state(page)
        assert bot.navigate_to_search(page, "http://x") is False
        assert len(page.goto_calls) == 1
