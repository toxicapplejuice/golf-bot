"""Tests for cancel_bot.py — form-driving cancellation helpers.

Covers the stable, pure-ish logic: parsing a tee time into the three
teetimecancel.html selects, building the slot dict, the not-found heuristic,
and the form-fill orchestration (via a fake page).

The post-Search confirm step (_confirm_cancel) is intentionally NOT unit-tested
here: its selectors are provisional pending the step-2 live DOM mapping, and the
real arbiter of cancel success is the status-aware verify-gone check in bot.py
(already covered by tests/test_cancel.py::TestActiveSlotInContent).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cancel_bot  # noqa: E402


# ----------------------------------------------------------------------
# _parse_time_to_slots (pure: "8:01 AM" -> ("08", "01", "AM"))
# ----------------------------------------------------------------------

class TestParseTimeToSlots:
    def test_morning_zero_pads_hour(self):
        assert cancel_bot._parse_time_to_slots("8:01 AM") == ("08", "01", "AM")

    def test_afternoon_pm(self):
        assert cancel_bot._parse_time_to_slots("12:40 PM") == ("12", "40", "PM")

    def test_single_digit_hour_pm(self):
        assert cancel_bot._parse_time_to_slots("1:05 PM") == ("01", "05", "PM")

    def test_two_digit_hour(self):
        assert cancel_bot._parse_time_to_slots("10:00 AM") == ("10", "00", "AM")

    def test_noon(self):
        assert cancel_bot._parse_time_to_slots("12:00 PM") == ("12", "00", "PM")

    def test_midnight_hour_twelve_am(self):
        assert cancel_bot._parse_time_to_slots("12:30 AM") == ("12", "30", "AM")

    def test_lowercase_meridian_uppercased(self):
        assert cancel_bot._parse_time_to_slots("8:01 am") == ("08", "01", "AM")

    def test_mixed_case_meridian(self):
        assert cancel_bot._parse_time_to_slots("8:01 pM") == ("08", "01", "PM")

    def test_surrounding_whitespace_stripped(self):
        assert cancel_bot._parse_time_to_slots("  8:40 AM  ") == ("08", "40", "AM")

    def test_returns_strings(self):
        h, m, ap = cancel_bot._parse_time_to_slots("9:09 AM")
        assert (h, m, ap) == ("09", "09", "AM")
        assert all(isinstance(x, str) for x in (h, m, ap))

    # ---- invalid inputs ----

    def _raises(self, value):
        try:
            cancel_bot._parse_time_to_slots(value)
        except ValueError:
            return
        assert False, f"expected ValueError for {value!r}"

    def test_no_meridian_raises(self):
        self._raises("8:01")

    def test_empty_raises(self):
        self._raises("")

    def test_word_raises(self):
        self._raises("morning")

    def test_hour_too_large_raises(self):
        self._raises("25:00 AM")

    def test_hour_zero_raises(self):
        self._raises("0:30 AM")

    def test_hour_thirteen_raises(self):
        self._raises("13:00 PM")

    def test_minute_too_large_raises(self):
        self._raises("8:99 AM")

    def test_missing_colon_raises(self):
        self._raises("8 AM")

    def test_extra_colon_raises(self):
        self._raises("8:01:02 AM")

    def test_non_digit_hour_raises(self):
        self._raises("ab:01 AM")

    def test_non_digit_minute_raises(self):
        self._raises("8:cd AM")

    def test_bad_meridian_raises(self):
        self._raises("8:01 XM")

    def test_negative_hour_raises(self):
        self._raises("-1:00 AM")

    def test_empty_minute_raises(self):
        self._raises("8: AM")


# ----------------------------------------------------------------------
# _slot_for (build {date,time,course} for text matching)
# ----------------------------------------------------------------------

class TestSlotFor:
    def _job(self, **o):
        j = {"date": "6/13/2026", "time": "8:40 AM", "course": "Jimmy Clay"}
        j.update(o)
        return j

    def test_passes_through_fields(self):
        assert cancel_bot._slot_for(self._job()) == {
            "date": "6/13/2026", "time": "8:40 AM", "course": "Jimmy Clay"
        }

    def test_none_course_becomes_empty_string(self):
        # Empty course makes bot._slot_in_content's course check a no-op.
        assert cancel_bot._slot_for(self._job(course=None))["course"] == ""

    def test_missing_course_key_becomes_empty_string(self):
        job = {"date": "6/13/2026", "time": "8:40 AM"}
        assert cancel_bot._slot_for(job)["course"] == ""


# ----------------------------------------------------------------------
# Fake Playwright page for form-driving tests
# ----------------------------------------------------------------------

class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def input_value(self):
        # Echo what select_option/eval stored, simulating a working <select>,
        # unless the test forces a specific read-back value.
        if self._page._readback_value is not None:
            return self._page._readback_value
        return self._page.selected.get(self._selector, "")


class FakePage:
    def __init__(self, content="", fill_raises=False, click_raises=False,
                 select_raises=False, eval_raises=False, readback_value=None):
        self._content = content
        self._fill_raises = fill_raises
        self._click_raises = click_raises
        self._select_raises = select_raises
        self._eval_raises = eval_raises
        self._readback_value = readback_value  # None = echo what was stored
        self.filled = {}
        self.selected = {}
        self.clicked = []
        self.evaled = []

    def fill(self, selector, value, **kw):
        if self._fill_raises:
            raise RuntimeError("fill boom")
        self.filled[selector] = value

    def select_option(self, selector, value, **kw):
        if self._select_raises:
            raise RuntimeError("element is not visible")
        self.selected[selector] = value

    def eval_on_selector(self, selector, script, value, **kw):
        if self._eval_raises:
            raise RuntimeError("eval boom")
        self.evaled.append(selector)
        self.selected[selector] = value

    def locator(self, selector):
        return FakeLocator(self, selector)

    def click(self, selector, **kw):
        if self._click_raises:
            raise RuntimeError("click boom")
        self.clicked.append(selector)

    def content(self):
        return self._content

    def wait_for_timeout(self, ms):
        pass


# ----------------------------------------------------------------------
# _fill_cancel_form (orchestration: confirmation # + 3 selects + Search)
# ----------------------------------------------------------------------

class TestFillCancelForm:
    def _job(self, **o):
        j = {
            "date": "6/13/2026", "time": "8:40 AM", "course": "Jimmy Clay",
            "confirmation_number": "R1234567",
        }
        j.update(o)
        return j

    def test_happy_path_fills_all_fields_and_searches(self):
        page = FakePage()
        assert cancel_bot._fill_cancel_form(page, self._job()) is True
        assert page.filled["#webteetimecancel_confirmationnumber"] == "R1234567"
        assert page.selected["#webteetimecancel_teetimeslot1"] == "08"
        assert page.selected["#webteetimecancel_teetimeslot2"] == "40"
        assert page.selected["#webteetimecancel_teetimeslot3"] == "AM"
        assert "#webteetimecancel_buttonsearch" in page.clicked

    def test_missing_confirmation_number_returns_false(self):
        page = FakePage()
        assert cancel_bot._fill_cancel_form(page, self._job(confirmation_number="")) is False
        assert page.filled == {}  # bailed before touching the form

    def test_whitespace_confirmation_number_returns_false(self):
        page = FakePage()
        assert cancel_bot._fill_cancel_form(page, self._job(confirmation_number="   ")) is False

    def test_confirmation_number_stripped_before_fill(self):
        page = FakePage()
        cancel_bot._fill_cancel_form(page, self._job(confirmation_number="  R987  "))
        assert page.filled["#webteetimecancel_confirmationnumber"] == "R987"

    def test_unparseable_time_returns_false_before_filling(self):
        page = FakePage()
        assert cancel_bot._fill_cancel_form(page, self._job(time="whenever")) is False
        assert page.filled == {}  # bailed before touching the form

    def test_fill_failure_returns_false(self):
        page = FakePage(fill_raises=True)
        assert cancel_bot._fill_cancel_form(page, self._job()) is False

    def test_search_click_failure_returns_false(self):
        page = FakePage(click_raises=True)
        assert cancel_bot._fill_cancel_form(page, self._job()) is False

    def test_hidden_select_falls_back_to_js_and_submits(self):
        # Vue hides the native select -> select_option raises -> JS fallback
        # sets the value and the form still submits.
        page = FakePage(select_raises=True)
        assert cancel_bot._fill_cancel_form(page, self._job()) is True
        assert page.selected["#webteetimecancel_teetimeslot3"] == "AM"
        assert "#webteetimecancel_teetimeslot3" in page.evaled
        assert "#webteetimecancel_buttonsearch" in page.clicked

    def test_unsettable_select_aborts_before_search(self):
        # Both select_option and the JS fallback fail -> never click Search
        # (searching with a wrong AM/PM targets the wrong reservation).
        page = FakePage(select_raises=True, eval_raises=True)
        assert cancel_bot._fill_cancel_form(page, self._job()) is False
        assert page.clicked == []

    def test_readback_mismatch_aborts_before_search(self):
        # select_option "succeeds" but the select doesn't hold the value.
        page = FakePage(readback_value="AM")  # hour reads back "AM" != "08"
        assert cancel_bot._fill_cancel_form(page, self._job()) is False
        assert page.clicked == []


class TestSetCombobox:
    def test_native_select_path(self):
        page = FakePage()
        assert cancel_bot._set_combobox(page, "x_slot", "PM") is True
        assert page.selected["#x_slot"] == "PM"
        assert page.evaled == []

    def test_js_fallback_fires_when_select_hidden(self):
        page = FakePage(select_raises=True)
        assert cancel_bot._set_combobox(page, "x_slot", "PM") is True
        assert page.selected["#x_slot"] == "PM"
        assert page.evaled == ["#x_slot"]

    def test_returns_false_when_value_does_not_stick(self):
        page = FakePage(readback_value="AM")
        assert cancel_bot._set_combobox(page, "x_slot", "PM") is False


# ----------------------------------------------------------------------
# _search_found_reservation (not-found heuristic)
# ----------------------------------------------------------------------

class TestSearchFoundReservation:
    def test_reservation_present_is_true(self):
        page = FakePage(content="Your reservation: 8:40 AM at Jimmy Clay on 6/13/2026")
        assert cancel_bot._search_found_reservation(page) is True

    def test_no_reservation_message_is_false(self):
        page = FakePage(content="No reservation matches that confirmation number.")
        assert cancel_bot._search_found_reservation(page) is False

    def test_not_found_marker_case_insensitive(self):
        page = FakePage(content="INVALID CONFIRMATION number provided")
        assert cancel_bot._search_found_reservation(page) is False

    def test_live_no_tee_times_message_is_false(self):
        # Exact message observed live on 2026-07-03.
        page = FakePage(content="No tee times available for Confirmation "
                                "Number and Time selected. Please try new "
                                "criteria.")
        assert cancel_bot._search_found_reservation(page) is False

    def test_content_read_error_assumes_present(self):
        # Verify-gone is the real arbiter; on a read error we must not
        # short-circuit to "not found" and skip the cancel.
        class Boom(FakePage):
            def content(self):
                raise RuntimeError("detached")

        assert cancel_bot._search_found_reservation(Boom()) is True
