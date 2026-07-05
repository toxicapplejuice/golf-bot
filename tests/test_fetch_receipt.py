"""Tests for fetch_receipt.py's pure PDF extraction helper."""

import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_receipt import extract_confirmation_numbers  # noqa: E402


def _pdf_with_stream(payload: bytes, compress: bool = True) -> bytes:
    body = zlib.compress(payload) if compress else payload
    return b"%PDF-1.4\nstream\n" + body + b"endstream\ntrailer"


CONF_LINE = b"Confirmation Numbers 324180014,324180016,324180018,324180020"


class TestExtractConfirmationNumbers:
    def test_compressed_stream(self):
        pdf = _pdf_with_stream(CONF_LINE)
        assert extract_confirmation_numbers(pdf) == [
            "324180014,324180016,324180018,324180020"
        ]

    def test_uncompressed_stream(self):
        pdf = _pdf_with_stream(CONF_LINE, compress=False)
        assert extract_confirmation_numbers(pdf) == [
            "324180014,324180016,324180018,324180020"
        ]

    def test_crlf_stream_delimiter(self):
        pdf = b"%PDF-1.4\nstream\r\n" + zlib.compress(CONF_LINE) + b"endstream"
        assert extract_confirmation_numbers(pdf) == [
            "324180014,324180016,324180018,324180020"
        ]

    def test_single_number_needs_confirmation_label(self):
        # A lone 9-digit value only counts when the stream mentions
        # Confirmation — otherwise it could be any id in the document.
        labeled = _pdf_with_stream(b"Confirmation Numbers 324180014")
        assert extract_confirmation_numbers(labeled) == ["324180014"]
        unlabeled = _pdf_with_stream(b"Household 123456789 balance")
        assert extract_confirmation_numbers(unlabeled) == []

    def test_no_streams_returns_empty(self):
        assert extract_confirmation_numbers(b"%PDF-1.4 nothing here") == []

    def test_garbage_stream_is_skipped(self):
        pdf = b"stream\n\x00\x01\x02endstream"
        assert extract_confirmation_numbers(pdf) == []

    def test_two_tee_times_two_groups_in_order(self):
        payload = (b"Confirmation Numbers 324180014,324180016 more text "
                   b"Confirmation Numbers 324190001,324190002")
        pdf = _pdf_with_stream(payload)
        assert extract_confirmation_numbers(pdf) == [
            "324180014,324180016",
            "324190001,324190002",
        ]

    def test_duplicate_groups_deduped(self):
        payload = CONF_LINE + b" " + CONF_LINE
        pdf = _pdf_with_stream(payload)
        assert extract_confirmation_numbers(pdf) == [
            "324180014,324180016,324180018,324180020"
        ]

    def test_longer_digit_runs_ignored(self):
        # A 10-digit phone number must not yield a bogus 9-digit match.
        pdf = _pdf_with_stream(b"Confirmation Numbers phone 5129742000")
        assert extract_confirmation_numbers(pdf) == []

    def test_shorter_digit_runs_ignored(self):
        pdf = _pdf_with_stream(b"Confirmation Numbers household 342308")
        assert extract_confirmation_numbers(pdf) == []
