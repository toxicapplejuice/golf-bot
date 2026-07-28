"""Tests for multi_bot.py run-log archiving and Queue-it wait extraction.

Added 2026-07-27. Slot quality is a function of how fast the first booking
lands after the 8:00 PM release, and the thing that decides that is how long
Queue-it holds us. Every run used to truncate the only record of that wait,
so the archive + extractor below exist to make release nights measurable.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import multi_bot  # noqa: E402


QUEUE_LOG = """Logging in...
  [login] Queue-it detected — waiting (timeout mode)
  [queue] Waiting up to 3600s (timeout mode)
  [queue] Still waiting... (0s elapsed)
  [queue] Still waiting... (11s elapsed)
  [queue] Still waiting... (863s elapsed)
  [queue] Released! URL: https://txaustinweb.myvscloud.com/webtrac/web/login
  [login] Success!
"""


class TestQueueWaitSeconds:
    def test_returns_longest_elapsed(self, tmp_path):
        p = tmp_path / "booking_grant.log"
        p.write_text(QUEUE_LOG)
        assert multi_bot.queue_wait_seconds(str(p)) == 863

    def test_none_when_no_queue_hit(self, tmp_path):
        p = tmp_path / "booking_x.log"
        p.write_text("Logging in...\n  [login] Success!\n")
        assert multi_bot.queue_wait_seconds(str(p)) is None

    def test_none_when_file_missing(self, tmp_path):
        assert multi_bot.queue_wait_seconds(str(tmp_path / "nope.log")) is None

    def test_none_on_empty_file(self, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("")
        assert multi_bot.queue_wait_seconds(str(p)) is None

    def test_unordered_lines_still_return_max(self, tmp_path):
        # Defensive: don't assume the last match is the largest.
        p = tmp_path / "b.log"
        p.write_text("(900s elapsed)\n(120s elapsed)\n")
        assert multi_bot.queue_wait_seconds(str(p)) == 900

    def test_survives_undecodable_bytes(self, tmp_path):
        p = tmp_path / "b.log"
        p.write_bytes(b"\xff\xfe garbage (450s elapsed)\n")
        assert multi_bot.queue_wait_seconds(str(p)) == 450


class TestArchiveLogs:
    def _setup(self, tmp_path, accounts=("michael", "grant")):
        for a in accounts:
            (tmp_path / f"booking_{a}.log").write_text(QUEUE_LOG)
        (tmp_path / "multi_bot.log").write_text("orchestrator output\n")
        return tmp_path

    def test_copies_every_log_into_timestamped_dir(self, tmp_path):
        self._setup(tmp_path)
        with patch.object(multi_bot, "SCRIPT_DIR", str(tmp_path)), \
             patch.object(multi_bot, "LOG_ARCHIVE_DIR",
                          str(tmp_path / "logs" / "runs")):
            dest = multi_bot.archive_logs(["michael", "grant"])
        assert dest is not None
        assert sorted(os.listdir(dest)) == [
            "booking_grant.log", "booking_michael.log", "multi_bot.log"]

    def test_originals_are_left_in_place(self, tmp_path):
        # The dashboard reads booking_<id>.log by fixed name — archiving must
        # copy, never move.
        self._setup(tmp_path)
        with patch.object(multi_bot, "SCRIPT_DIR", str(tmp_path)), \
             patch.object(multi_bot, "LOG_ARCHIVE_DIR",
                          str(tmp_path / "logs" / "runs")):
            multi_bot.archive_logs(["michael", "grant"])
        assert (tmp_path / "booking_michael.log").exists()
        assert (tmp_path / "multi_bot.log").exists()

    def test_archived_content_matches_original(self, tmp_path):
        self._setup(tmp_path)
        with patch.object(multi_bot, "SCRIPT_DIR", str(tmp_path)), \
             patch.object(multi_bot, "LOG_ARCHIVE_DIR",
                          str(tmp_path / "logs" / "runs")):
            dest = multi_bot.archive_logs(["michael"])
        archived = Path(dest) / "booking_michael.log"
        assert archived.read_text() == QUEUE_LOG
        # The whole point: the wait is still recoverable from the archive.
        assert multi_bot.queue_wait_seconds(str(archived)) == 863

    def test_missing_account_log_is_skipped_not_fatal(self, tmp_path):
        self._setup(tmp_path, accounts=("michael",))
        with patch.object(multi_bot, "SCRIPT_DIR", str(tmp_path)), \
             patch.object(multi_bot, "LOG_ARCHIVE_DIR",
                          str(tmp_path / "logs" / "runs")):
            dest = multi_bot.archive_logs(["michael", "never_ran"])
        assert sorted(os.listdir(dest)) == [
            "booking_michael.log", "multi_bot.log"]

    def test_no_accounts_still_archives_orchestrator_log(self, tmp_path):
        self._setup(tmp_path, accounts=())
        with patch.object(multi_bot, "SCRIPT_DIR", str(tmp_path)), \
             patch.object(multi_bot, "LOG_ARCHIVE_DIR",
                          str(tmp_path / "logs" / "runs")):
            dest = multi_bot.archive_logs([])
        assert os.listdir(dest) == ["multi_bot.log"]

    def test_unwritable_archive_root_returns_none(self, tmp_path):
        # A failed archive must never take the booking run down with it.
        self._setup(tmp_path)
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        with patch.object(multi_bot, "SCRIPT_DIR", str(tmp_path)), \
             patch.object(multi_bot, "LOG_ARCHIVE_DIR", str(blocker / "runs")):
            assert multi_bot.archive_logs(["michael"]) is None


class TestReportQueueWaits:
    def test_reports_each_account(self, tmp_path, capsys):
        (tmp_path / "booking_grant.log").write_text(QUEUE_LOG)
        (tmp_path / "booking_quiet.log").write_text("no queue here\n")
        with patch.object(multi_bot, "SCRIPT_DIR", str(tmp_path)):
            multi_bot.report_queue_waits(["grant", "quiet"])
        out = capsys.readouterr().out
        assert "863s" in out and "14m23s" in out
        assert "no queue hit" in out
