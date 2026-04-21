"""Tests for accounts.json loader and per-account path derivation."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import bot  # noqa: E402


class TestLoadAccounts:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = bot.ACCOUNTS_FILE
        bot.ACCOUNTS_FILE = os.path.join(self._tmpdir, "accounts.json")

    def teardown_method(self):
        import shutil
        bot.ACCOUNTS_FILE = self._orig_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, data):
        with open(bot.ACCOUNTS_FILE, "w") as f:
            json.dump(data, f)

    def test_missing_file_returns_empty(self):
        assert bot.load_accounts() == []

    def test_empty_list_returns_empty(self):
        self._write([])
        assert bot.load_accounts() == []

    def test_loads_valid_accounts(self):
        self._write([
            {"id": "michael", "display_name": "Michael", "username": "u1", "password": "p1"},
            {"id": "friend1", "display_name": "Friend", "username": "u2", "password": "p2"},
        ])
        accounts = bot.load_accounts()
        assert len(accounts) == 2
        assert accounts[0]["id"] == "michael"
        assert accounts[1]["id"] == "friend1"

    def test_filters_disabled_accounts(self):
        self._write([
            {"id": "a", "username": "u1", "password": "p1"},
            {"id": "b", "username": "u2", "password": "p2", "disabled": True},
        ])
        accounts = bot.load_accounts()
        ids = [a["id"] for a in accounts]
        assert "a" in ids
        assert "b" not in ids

    def test_filters_replace_me_placeholders(self):
        self._write([
            {"id": "real", "username": "user1", "password": "pass1"},
            {"id": "placeholder1", "username": "REPLACE_ME", "password": "pass"},
            {"id": "placeholder2", "username": "user", "password": "REPLACE_ME"},
        ])
        accounts = bot.load_accounts()
        ids = [a["id"] for a in accounts]
        assert ids == ["real"]

    def test_display_name_defaults_to_id(self):
        self._write([
            {"id": "michael", "username": "u", "password": "p"},
        ])
        accounts = bot.load_accounts()
        assert accounts[0]["display_name"] == "michael"

    def test_get_account_by_id(self):
        self._write([
            {"id": "michael", "display_name": "Michael", "username": "u1", "password": "p1"},
        ])
        found = bot.get_account_by_id("michael")
        assert found is not None
        assert found["username"] == "u1"
        assert bot.get_account_by_id("nonexistent") is None

    def test_entry_missing_required_fields_is_filtered(self):
        self._write([
            {"id": "ok", "username": "u", "password": "p"},
            {"id": "no_pwd", "username": "u"},
            {"no_id": "x", "username": "u", "password": "p"},
        ])
        accounts = bot.load_accounts()
        assert [a["id"] for a in accounts] == ["ok"]


class TestConfigureAccountContext:
    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_file = bot.ACCOUNTS_FILE
        bot.ACCOUNTS_FILE = os.path.join(self._tmpdir, "accounts.json")
        # Snapshot state we mutate
        self._snap = (bot.USERNAME, bot.PASSWORD, bot.ACCOUNT_ID,
                      bot.ACCOUNT_DISPLAY_NAME, bot.STATE_FILE,
                      bot.LIVE_SCREENSHOT, bot.BOOKING_LOG_PATH)
        with open(bot.ACCOUNTS_FILE, "w") as f:
            json.dump([
                {"id": "michael", "display_name": "Michael",
                 "username": "m_user", "password": "m_pass"},
                {"id": "friend1", "display_name": "Friend One",
                 "username": "f_user", "password": "f_pass"},
            ], f)

    def teardown_method(self):
        import shutil
        bot.ACCOUNTS_FILE = self._orig_file
        (bot.USERNAME, bot.PASSWORD, bot.ACCOUNT_ID,
         bot.ACCOUNT_DISPLAY_NAME, bot.STATE_FILE,
         bot.LIVE_SCREENSHOT, bot.BOOKING_LOG_PATH) = self._snap
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_none_account_id_uses_defaults(self):
        """With --account-id not passed, fall back to legacy (single-account) paths."""
        bot.USERNAME = "env_user"
        bot.PASSWORD = "env_pass"
        account = bot.configure_account_context(None)
        assert account["id"] == "default"
        assert bot.USERNAME == "env_user"

    def test_account_id_switches_credentials_and_paths(self):
        bot.configure_account_context("michael")
        assert bot.USERNAME == "m_user"
        assert bot.PASSWORD == "m_pass"
        assert bot.ACCOUNT_ID == "michael"
        assert bot.ACCOUNT_DISPLAY_NAME == "Michael"
        assert "state_michael.json" in bot.STATE_FILE
        assert "live_michael.png" in bot.LIVE_SCREENSHOT
        assert "booking_michael.log" in bot.BOOKING_LOG_PATH

    def test_different_accounts_get_different_paths(self):
        bot.configure_account_context("michael")
        michael_state = bot.STATE_FILE
        bot.configure_account_context("friend1")
        friend_state = bot.STATE_FILE
        assert michael_state != friend_state

    def test_unknown_account_id_raises_systemexit(self):
        try:
            bot.configure_account_context("nonexistent")
        except SystemExit:
            return
        assert False, "Expected SystemExit for unknown account"
