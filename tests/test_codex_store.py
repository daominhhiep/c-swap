"""``CodexStore``: roster, slot files and the live login file."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from claude_swap.codex.store import CodexStore, empty_sequence
from claude_swap.exceptions import ConfigError
from tests.conftest import make_codex_auth, write_codex_auth

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


@pytest.fixture
def store(temp_home):
    return CodexStore(temp_home / ".claude-swap-backup" / "codex")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestSequence:
    def test_fresh_store_reads_empty_roster(self, store):
        data = store.read_sequence()
        assert data["accounts"] == {} and data["sequence"] == []
        assert data["activeAccountNumber"] is None
        assert data["provider"] == "codex"
        assert not store.exists()

    def test_write_stamps_metadata(self, store):
        data = empty_sequence()
        data["accounts"]["1"] = {"email": "a@b.c", "accountId": "acct_1"}
        data["sequence"] = [1]
        store.write_sequence(data)
        assert store.exists()
        back = json.loads(store.sequence_file.read_text())
        assert back["schemaVersion"] == 1 and back["provider"] == "codex"
        assert back["accounts"]["1"]["accountId"] == "acct_1"

    def test_corrupt_roster_is_an_error_not_empty(self, store):
        store.sequence_file.parent.mkdir(parents=True)
        store.sequence_file.write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            store.read_sequence()

    def test_next_number_skips_to_max_plus_one(self, store):
        assert store.next_number() == 1
        data = empty_sequence()
        data["accounts"] = {"1": {}, "3": {}}
        store.write_sequence(data)
        assert store.next_number() == 4


class TestSlots:
    @posix_only
    def test_slot_file_is_private(self, store):
        store.write_slot("2", make_codex_auth())
        assert _mode(store.slot_path("2")) == 0o600
        assert _mode(store.slots_dir) == 0o700

    def test_round_trip_and_delete(self, store):
        auth = make_codex_auth(email="x@y.z")
        store.write_slot("1", auth)
        assert store.read_slot("1")["tokens"]["refresh_token"] == "rt-1"
        store.delete_slot("1")
        assert store.read_slot("1") is None
        store.delete_slot("1")  # idempotent


class TestLive:
    def test_reads_codex_home_auth(self, store, codex_home):
        assert store.read_live()["auth_mode"] == "chatgpt"

    def test_missing_live_is_none(self, store, temp_home):
        assert store.read_live() is None

    def test_corrupt_live_is_an_error(self, store, temp_home):
        (temp_home / ".codex").mkdir()
        (temp_home / ".codex" / "auth.json").write_text("nope")
        with pytest.raises(ConfigError, match="Codex login"):
            store.read_live()

    @posix_only
    def test_write_live_keeps_codex_dir_mode(self, store, temp_home):
        codex_dir = temp_home / ".codex"
        codex_dir.mkdir()
        os.chmod(codex_dir, 0o755)
        store.write_live(make_codex_auth(email="new@x.y"))
        path = codex_dir / "auth.json"
        assert json.loads(path.read_text())["tokens"]["account_id"] == "acct_123"
        assert _mode(path) == 0o600
        assert _mode(codex_dir) == 0o755  # not hardened: the directory is codex's

    def test_write_live_replaces_atomically(self, store, temp_home):
        write_codex_auth(temp_home, make_codex_auth(email="old@x.y"))
        store.write_live(make_codex_auth(email="new@x.y", account_id="acct_new"))
        assert store.read_live()["tokens"]["account_id"] == "acct_new"
        assert not list((temp_home / ".codex").glob("*.tmp"))

    def test_respects_codex_home_env(self, store, temp_home, monkeypatch):
        other = temp_home / "elsewhere"
        monkeypatch.setenv("CODEX_HOME", str(other))
        store.write_live(make_codex_auth())
        assert (other / "auth.json").exists()


class TestResolve:
    @pytest.fixture
    def roster(self, store):
        data = empty_sequence()
        data["accounts"] = {
            "1": {"email": "a@b.c", "accountId": "acct_1", "alias": "work"},
            "2": {"email": "d@e.f", "accountId": "acct_2"},
            "3": {"email": "d@e.f", "accountId": "acct_3"},
        }
        data["sequence"] = [1, 2, 3]
        store.write_sequence(data)
        return store

    def test_by_number(self, roster):
        assert roster.resolve_identifier("1") == "1"
        assert roster.resolve_identifier("9") is None

    def test_by_alias_case_insensitive(self, roster):
        assert roster.resolve_identifier("WORK") == "1"

    def test_by_unique_email(self, roster):
        assert roster.resolve_identifier("A@B.C") == "1"

    def test_ambiguous_email_is_an_error(self, roster):
        with pytest.raises(ConfigError, match="2, 3"):
            roster.resolve_identifier("d@e.f")

    def test_unknown_is_none(self, roster):
        assert roster.resolve_identifier("nobody") is None

    def test_find_slot_and_alias_helpers(self, roster):
        assert roster.find_slot("a@b.c", "acct_1") == "1"
        assert roster.find_slot("zzz", "acct_3") == "3"
        assert roster.find_slot("nobody", "acct_x") is None
        assert roster.find_by_alias("work") == "1"
        assert roster.alias_in_use("work") == "1"
        assert roster.alias_in_use("work", exclude_num="1") is None
