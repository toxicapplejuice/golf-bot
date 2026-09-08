"""Tests for the taken-vs-confirmation ordering bug (2026-09-07 postmortem).

michael's 3:41 PM Roy Kizer checkout COMPLETED and landed on confirmation.html,
but the page also carried a loose TAKEN_KEYWORDS phrase. Because the taken-check
ran first, the bot recorded "taken", moved on, and booked 3:51 PM as well. Both
reservations were real; neither reached shared_state.json or history.json.

The contradiction must now resolve to 'unverified_post_click' (halt for human
review) rather than being guessed either way.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


CONFIRM_URL = "https://txaustinweb.myvscloud.com/webtrac/web/confirmation.html?_csrf_token=bo5y"
CART_URL = "https://txaustinweb.myvscloud.com/webtrac/web/addtocart.html"
LOGIN_URL = "https://txaustinweb.myvscloud.com/webtrac/web/login.html"

SLOT = {"time": "3:41 PM", "course": "Roy Kizer", "date": "9/12/2026"}


class FakePage:
    def __init__(self, url, html):
        self.url = url
        self._html = html

    def content(self):
        return self._html

    def locator(self, sel):
        raise AssertionError("finish-button lookup should be stubbed")

    def wait_for_load_state(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """Stub the finish-button click and screenshots; exercise only the verdict."""
    monkeypatch.setattr(bot, "save_debug_screenshot", lambda *a, **k: None)
    monkeypatch.setattr(bot, "ONE_CLICK_FINISH_SELECTORS", [])


def verdict(url, html, monkeypatch, slot_matches=False, clicked_finish=False):
    monkeypatch.setattr(bot, "verify_booking_on_page",
                        lambda p, s, t: slot_matches)
    monkeypatch.setattr(bot, "notify", lambda *a, **k: None)
    page = FakePage(url, html)
    return bot._evaluate_checkout_result(page, SLOT, clicked_finish)


class TestAmbiguousContradiction:
    """The actual regression."""

    @pytest.mark.parametrize("kw", [
        "maximum number", "limit one tee time", "duplicate",
        "not available", "in use", "tee time per fm",
    ])
    def test_taken_keyword_on_confirmation_url_is_ambiguous(self, kw, monkeypatch):
        html = f"<html>Reservation complete. Warning: {kw} of tee times.</html>"
        assert verdict(CONFIRM_URL, html, monkeypatch) == "unverified_post_click"

    def test_the_actual_3_41pm_page_shape(self, monkeypatch):
        """confirmation.html + an account-limit warning => halt, never 'taken'."""
        html = ("<html><h1>Confirmation</h1>Confirmation Number 333728302"
                "<div>You have reached the maximum number of tee times.</div></html>")
        assert verdict(CONFIRM_URL, html, monkeypatch) == "unverified_post_click"

    def test_ambiguous_wins_even_when_slot_text_matches(self, monkeypatch):
        """A contradiction is unresolved regardless of slot-text verification."""
        html = "<html>Confirmation Number 1 — duplicate detected</html>"
        assert verdict(CONFIRM_URL, html, monkeypatch,
                       slot_matches=True) == "unverified_post_click"

    @pytest.mark.parametrize("marker", ["confirmation", "receipt", "complete",
                                        "finishaddtocart"])
    def test_every_booked_url_marker_triggers_ambiguity(self, marker, monkeypatch):
        url = f"https://txaustinweb.myvscloud.com/webtrac/web/{marker}.html"
        html = "<html>no longer available</html>"
        assert verdict(url, html, monkeypatch) == "unverified_post_click"


class TestGenuineTakenStillWorks:
    """Failing fast on a real 'taken' is the common path — don't regress it."""

    def test_taken_on_cart_url_is_still_taken(self, monkeypatch):
        html = "<html>That tee time is no longer available.</html>"
        assert verdict(CART_URL, html, monkeypatch) == "taken"

    def test_taken_on_search_page_is_still_taken(self, monkeypatch):
        html = "<html>already booked by another user</html>"
        url = "https://txaustinweb.myvscloud.com/webtrac/web/search.html"
        assert verdict(url, html, monkeypatch) == "taken"


class TestUnrelatedPathsUnchanged:
    def test_clean_confirmation_still_books(self, monkeypatch):
        html = "<html>Confirmation Number 333728302 — reservation confirmed</html>"
        assert verdict(CONFIRM_URL, html, monkeypatch, slot_matches=True) == "booked"

    def test_session_expiry_still_detected(self, monkeypatch):
        html = "<html>Please sign in to continue</html>"
        assert verdict(LOGIN_URL, html, monkeypatch) == "session_expired"

    def test_login_url_with_taken_keyword_prefers_taken(self, monkeypatch):
        """login.html is not a BOOKED_URL_MARKER, so no contradiction arises."""
        html = "<html>no longer available</html>"
        assert verdict(LOGIN_URL, html, monkeypatch) == "taken"

    def test_stuck_on_cart_without_finish_click_is_retryable(self, monkeypatch):
        """Nothing committed, so the caller may safely try the next slot."""
        assert verdict(CART_URL, "<html></html>", monkeypatch,
                       clicked_finish=False) == "failed"

    def test_unknown_page_after_finish_click_halts_the_day(self, monkeypatch):
        """The pre-existing duplicate guard: a click we can't score halts."""
        url = "https://txaustinweb.myvscloud.com/webtrac/web/somewhere-new.html"
        assert verdict(url, "<html>hello</html>", monkeypatch,
                       clicked_finish=True) == "unverified_post_click"

    def test_unknown_page_without_finish_click_is_retryable(self, monkeypatch):
        url = "https://txaustinweb.myvscloud.com/webtrac/web/somewhere-new.html"
        assert verdict(url, "<html>hello</html>", monkeypatch,
                       clicked_finish=False) == "failed"


class TestKeywordListIntegrity:
    def test_loose_phrases_are_still_present(self):
        """These caused the bug; the fix tolerates them rather than deleting them."""
        for kw in ("not available", "in use", "maximum number", "duplicate"):
            assert kw in bot.TAKEN_KEYWORDS

    def test_confirmation_is_a_booked_url_marker(self):
        assert "confirmation" in bot.BOOKED_URL_MARKERS
