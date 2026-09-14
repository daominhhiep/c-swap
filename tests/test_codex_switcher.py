"""``CodexAccountSwitcher``: add, switch, remove, alias, usage and payloads."""

from __future__ import annotations

import json
import os
import stat
import sys
from unittest.mock import patch

import pytest

from claude_swap.codex import auth as codex_auth
from claude_swap.codex.auth import CodexAuthError, CodexRefreshError
from claude_swap.codex.switcher import CodexAccountSwitcher
from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    SwitchError,
    ValidationError,
)
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED, USAGE_TOKEN_EXPIRED
from claude_swap.claude.oauth import UsageOutcome
from claude_swap.usage_store import AUTH_DEAD_STRIKES, SERVE_TTL_S, UsageStore
from tests.conftest import make_codex_auth, write_codex_auth

USAGE = {"five_hour": {"pct": 12.0}, "seven_day": {"pct": 40.0}}


@pytest.fixture
def switcher(temp_home):
    return CodexAccountSwitcher()


def _live(temp_home) -> dict:
    return json.loads((temp_home / ".codex" / "auth.json").read_text())


def _roster(switcher) -> dict:
    return switcher._store.read_sequence()


class TestAdd:
    def test_first_add_creates_slot_1(self, switcher, codex_home, capsys):
        switcher.add_account()
        data = _roster(switcher)
        assert data["accounts"]["1"]["email"] == "me@example.com"
        assert data["accounts"]["1"]["accountId"] == "acct_123"
        assert data["accounts"]["1"]["planType"] == "plus"
        assert data["sequence"] == [1] and data["activeAccountNumber"] == 1
        assert switcher._store.read_slot("1")["tokens"]["refresh_token"] == "rt-1"
        assert "Added" in capsys.readouterr().out
        assert switcher.current_account_number() == "1"

    def test_second_account_takes_slot_2(self, switcher, temp_home, capsys):
        write_codex_auth(temp_home, make_codex_auth("a@x.y", "acct_a"))
        switcher.add_account()
        write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b"))
        switcher.add_account(alias="Work")
        data = _roster(switcher)
        assert data["sequence"] == [1, 2]
        assert data["accounts"]["2"]["alias"] == "work"
        assert data["activeAccountNumber"] == 2

    def test_same_account_again_updates_in_place(self, switcher, temp_home, capsys):
        write_codex_auth(temp_home, make_codex_auth(refresh_token="rt-old"))
        switcher.add_account(alias="main")
        write_codex_auth(temp_home, make_codex_auth(refresh_token="rt-rotated"))
        switcher.add_account()
        data = _roster(switcher)
        assert list(data["accounts"]) == ["1"]
        assert data["accounts"]["1"]["alias"] == "main"
        assert switcher._store.read_slot("1")["tokens"]["refresh_token"] == "rt-rotated"
        assert "Updated" in capsys.readouterr().out

    def test_slot_occupied_prompts(self, switcher, temp_home, capsys):
        write_codex_auth(temp_home, make_codex_auth("a@x.y", "acct_a"))
        switcher.add_account()
        write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b"))
        with patch("builtins.input", return_value="n"):
            switcher.add_account(slot=1)
        assert _roster(switcher)["accounts"]["1"]["accountId"] == "acct_a"
        assert "Cancelled" in capsys.readouterr().out
        with patch("builtins.input", return_value="y"):
            switcher.add_account(slot=1)
        assert _roster(switcher)["accounts"]["1"]["accountId"] == "acct_b"
        assert switcher._store.read_slot("1")["tokens"]["account_id"] == "acct_b"

    def test_assume_yes_skips_prompt(self, switcher, temp_home):
        write_codex_auth(temp_home, make_codex_auth("a@x.y", "acct_a"))
        switcher.add_account()
        write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b"))
        switcher.add_account(slot=1, assume_yes=True)
        assert _roster(switcher)["accounts"]["1"]["accountId"] == "acct_b"

    def test_explicit_slot_moves_existing_account(self, switcher, temp_home):
        switcher_home = temp_home
        write_codex_auth(switcher_home, make_codex_auth("a@x.y", "acct_a"))
        switcher.add_account()
        switcher.add_account(slot=5)
        data = _roster(switcher)
        assert list(data["accounts"]) == ["5"] and data["sequence"] == [5]
        assert switcher._store.read_slot("1") is None

    def test_duplicate_alias_rejected(self, switcher, temp_home):
        write_codex_auth(temp_home, make_codex_auth("a@x.y", "acct_a"))
        switcher.add_account(alias="dev")
        write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b"))
        with pytest.raises(ValidationError, match="already used"):
            switcher.add_account(alias="dev")

    def test_invalid_alias_rejected(self, switcher, codex_home):
        with pytest.raises(ValidationError):
            switcher.add_account(alias="12")

    def test_no_login(self, switcher, temp_home):
        with pytest.raises(CodexAuthError, match="codex login"):
            switcher.add_account()

    def test_api_key_login_refused(self, switcher, temp_home):
        write_codex_auth(temp_home, make_codex_auth(mode="apikey"))
        with pytest.raises(CodexAuthError, match="API key"):
            switcher.add_account()

    def test_keyring_store_refused(self, switcher, codex_home):
        (codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        with pytest.raises(CodexAuthError, match="keyring"):
            switcher.add_account()

    def test_file_store_setting_is_fine(self, switcher, codex_home):
        (codex_home / "config.toml").write_text('model = "gpt-5"\ncli_auth_credentials_store = "file"\n')
        switcher.add_account()
        assert switcher.current_account_number() == "1"


@pytest.fixture
def two_accounts(switcher, temp_home):
    write_codex_auth(temp_home, make_codex_auth("a@x.y", "acct_a", refresh_token="rt-a"))
    switcher.add_account()
    write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b", refresh_token="rt-b"))
    switcher.add_account()
    return switcher


class TestSwitch:
    def test_switch_to_writes_live_and_syncs_back(self, two_accounts, temp_home, capsys):
        # The live login (b) rotated its refresh token since it was stored.
        write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b", refresh_token="rt-b2"))
        two_accounts.switch_to("1")
        assert _live(temp_home)["tokens"]["account_id"] == "acct_a"
        assert two_accounts._store.read_slot("2")["tokens"]["refresh_token"] == "rt-b2"
        assert _roster(two_accounts)["activeAccountNumber"] == 1
        assert two_accounts.current_account_number() == "1"
        out = capsys.readouterr().out
        assert "Switched to" in out and "Codex account 1" in out
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(temp_home / ".codex" / "auth.json").st_mode) == 0o600

    def test_switch_to_by_email_and_alias(self, two_accounts, temp_home):
        two_accounts.set_alias("1", "first")
        two_accounts.switch_to("first")
        assert _live(temp_home)["tokens"]["account_id"] == "acct_a"
        two_accounts.switch_to("b@x.y")
        assert _live(temp_home)["tokens"]["account_id"] == "acct_b"

    def test_already_active_is_a_noop(self, two_accounts, temp_home, capsys):
        payload = two_accounts.switch_to("2", json_output=True)
        assert payload["switched"] is False and payload["reason"] == "already-active"
        assert payload["provider"] == "codex"
        two_accounts.switch_to("2")
        assert "Already on" in capsys.readouterr().out

    def test_force_rewrites_active(self, two_accounts, temp_home):
        write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b", refresh_token="rt-stale"))
        two_accounts._store.write_slot("2", make_codex_auth("b@x.y", "acct_b", refresh_token="rt-good"))
        payload = two_accounts.switch_to("2", json_output=True, force=True)
        assert payload["switched"] is True and payload["reason"] == "forced"
        # force activates the stored copy; the live copy is synced back first,
        # then overwritten by the slot's copy that was just refreshed from it.
        assert _live(temp_home)["tokens"]["account_id"] == "acct_b"

    def test_unknown_live_login_refused_without_force(self, two_accounts, temp_home):
        write_codex_auth(temp_home, make_codex_auth("stranger@x.y", "acct_s"))
        with pytest.raises(SwitchError, match="ccswap codex add"):
            two_accounts.switch_to("1")
        assert _live(temp_home)["tokens"]["account_id"] == "acct_s"

    def test_unknown_live_login_replaced_with_force(self, two_accounts, temp_home):
        write_codex_auth(temp_home, make_codex_auth("stranger@x.y", "acct_s"))
        payload = two_accounts.switch_to("1", json_output=True, force=True)
        assert payload["switched"] is True
        assert payload["from"] == {"number": None, "email": "stranger@x.y"}
        assert any("not saved" in w for w in payload["warnings"])
        assert _live(temp_home)["tokens"]["account_id"] == "acct_a"

    def test_api_key_live_login_refused_without_force(self, two_accounts, temp_home):
        write_codex_auth(temp_home, make_codex_auth(mode="apikey"))
        with pytest.raises(SwitchError, match="API key"):
            two_accounts.switch_to("1")
        two_accounts.switch_to("1", force=True)
        assert _live(temp_home)["auth_mode"] == "chatgpt"

    def test_no_live_login_still_switches(self, two_accounts, temp_home):
        (temp_home / ".codex" / "auth.json").unlink()
        payload = two_accounts.switch_to("1", json_output=True)
        assert payload["switched"] is True
        assert payload["from"] == {"number": None, "email": ""}
        assert _live(temp_home)["tokens"]["account_id"] == "acct_a"

    def test_missing_slot_file(self, two_accounts):
        two_accounts._store.delete_slot("1")
        with pytest.raises(CredentialReadError, match="ccswap codex login"):
            two_accounts.switch_to("1")

    def test_unknown_target(self, two_accounts):
        with pytest.raises(AccountNotFoundError):
            two_accounts.switch_to("7")

    def test_rotate_wraps_around(self, two_accounts, temp_home):
        assert two_accounts.current_account_number() == "2"
        two_accounts.switch(json_output=True)
        assert two_accounts.current_account_number() == "1"
        two_accounts.switch(json_output=True)
        assert two_accounts.current_account_number() == "2"

    def test_rotate_single_account_is_noop(self, switcher, codex_home):
        switcher.add_account()
        payload = switcher.switch(json_output=True)
        assert payload["switched"] is False

    def test_rotate_from_unmanaged_login_refuses(self, two_accounts, temp_home):
        write_codex_auth(temp_home, make_codex_auth("stranger@x.y", "acct_s"))
        with pytest.raises(SwitchError):
            two_accounts.switch()

    def test_nothing_managed(self, switcher, codex_home):
        with pytest.raises(ConfigError, match="ccswap codex add"):
            switcher.switch_to("1")


class TestLogin:
    """`ccswap codex login`: stash the current login so `codex login` cannot
    revoke it, run the login, save the result."""

    @pytest.fixture
    def fake_codex(self, temp_home):
        """Patch `codex` on PATH and `subprocess.call`; the fake login writes
        whatever `next_auth` holds (or nothing) and returns `rc`."""
        state = {"next_auth": None, "rc": 0, "calls": [], "live_at_call": "unset"}

        def call(argv, **_kw):
            state["calls"].append(argv)
            state["live_at_call"] = (temp_home / ".codex" / "auth.json").exists()
            if state["next_auth"] is not None:
                write_codex_auth(temp_home, state["next_auth"])
            return state["rc"]

        with patch("claude_swap.codex.switcher.shutil.which", return_value="/usr/bin/codex"), \
             patch("claude_swap.codex.switcher.subprocess.call", side_effect=call):
            yield state

    def test_saves_current_login_then_adds_the_new_one(self, switcher, codex_home, fake_codex, capsys):
        fake_codex["next_auth"] = make_codex_auth("b@x.y", "acct_b", "team")
        switcher.login(["--device-auth"])
        out = capsys.readouterr().out
        data = _roster(switcher)
        assert data["accounts"]["1"]["email"] == "me@example.com"
        assert data["accounts"]["2"]["email"] == "b@x.y"
        assert data["activeAccountNumber"] == 2
        assert switcher._store.read_slot("1")["tokens"]["refresh_token"] == "rt-1"
        assert fake_codex["calls"] == [["/usr/bin/codex", "login", "--device-auth"]]
        assert fake_codex["live_at_call"] is False  # nothing left for codex to revoke
        assert "Saved current login as Codex account 1" in out
        assert "Added Codex account 2" in out

    def test_managed_login_is_synced_back_not_duplicated(self, switcher, codex_home, fake_codex):
        switcher.add_account()
        rotated = make_codex_auth(refresh_token="rt-rotated")
        write_codex_auth(codex_home.parent, rotated)
        fake_codex["next_auth"] = make_codex_auth("b@x.y", "acct_b")
        switcher.login()
        data = _roster(switcher)
        assert sorted(data["accounts"]) == ["1", "2"]
        assert switcher._store.read_slot("1")["tokens"]["refresh_token"] == "rt-rotated"

    def test_failed_login_restores_previous_file(self, switcher, codex_home, fake_codex, capsys):
        fake_codex["rc"] = 1
        with pytest.raises(CodexAuthError, match="status 1"):
            switcher.login()
        assert _live(codex_home.parent)["tokens"]["refresh_token"] == "rt-1"
        data = _roster(switcher)
        assert data["accounts"]["1"]["email"] == "me@example.com"
        assert data["activeAccountNumber"] == 1
        assert "restored" in capsys.readouterr().out

    def test_login_that_writes_nothing_restores_previous_file(self, switcher, codex_home, fake_codex):
        with pytest.raises(CodexAuthError, match="without a ChatGPT login"):
            switcher.login()
        assert switcher.current_account_number() == "1"

    def test_ctrl_c_restores_and_reraises(self, switcher, codex_home, fake_codex):
        with patch("claude_swap.codex.switcher.subprocess.call", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                switcher.login()
        assert switcher.current_account_number() == "1"

    def test_no_current_login_just_runs_codex_login(self, switcher, temp_home, fake_codex):
        fake_codex["next_auth"] = make_codex_auth("b@x.y", "acct_b")
        switcher.login()
        assert _roster(switcher)["accounts"]["1"]["email"] == "b@x.y"

    def test_api_key_login_is_left_alone(self, switcher, temp_home, fake_codex, capsys):
        write_codex_auth(temp_home, make_codex_auth(mode="apikey"))
        fake_codex["next_auth"] = make_codex_auth("b@x.y", "acct_b")
        switcher.login()
        assert fake_codex["live_at_call"] is True
        assert "API key" in capsys.readouterr().out
        assert _roster(switcher)["accounts"]["1"]["email"] == "b@x.y"

    def test_codex_missing_from_path(self, switcher, codex_home):
        with patch("claude_swap.codex.switcher.shutil.which", return_value=None):
            with pytest.raises(CodexAuthError, match="not found"):
                switcher.login()
        assert _live(codex_home.parent)["tokens"]["refresh_token"] == "rt-1"  # untouched


class TestRemoveAndAlias:
    def test_remove_prompts_and_deletes(self, two_accounts, temp_home, capsys):
        with patch("builtins.input", return_value="n"):
            two_accounts.remove_account("1")
        assert "1" in _roster(two_accounts)["accounts"]
        with patch("builtins.input", return_value="y"):
            two_accounts.remove_account("1")
        data = _roster(two_accounts)
        assert "1" not in data["accounts"] and data["sequence"] == [2]
        assert two_accounts._store.read_slot("1") is None
        assert "Removed" in capsys.readouterr().out

    def test_remove_active_warns_and_clears_active(self, two_accounts, capsys):
        two_accounts.remove_account("2", assume_yes=True)
        assert _roster(two_accounts)["activeAccountNumber"] is None
        assert "current Codex login" in capsys.readouterr().out

    def test_remove_unknown(self, two_accounts):
        with pytest.raises(AccountNotFoundError):
            two_accounts.remove_account("9", assume_yes=True)

    def test_alias_set_list_unset(self, two_accounts):
        assert two_accounts.set_alias("1", "Dev") == ("1", "dev")
        assert two_accounts.list_aliases() == [("1", "dev", "a@x.y")]
        with pytest.raises(ValidationError, match="already used"):
            two_accounts.set_alias("2", "dev")
        with pytest.raises(ValidationError):
            two_accounts.set_alias("2", "42")
        assert two_accounts.unset_alias("dev") == "1"
        assert two_accounts.list_aliases() == []


class TestUsage:
    @pytest.fixture
    def clock(self):
        return {"now": 1_000_000.0}

    @pytest.fixture
    def usage_switcher(self, two_accounts, clock):
        two_accounts._usage_store = UsageStore(
            two_accounts._store.cache_dir, clock=lambda: clock["now"]
        )
        return two_accounts

    def test_fetches_both_accounts_and_records(self, usage_switcher):
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)) as fetch:
            entries = usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        assert fetch.call_count == 2
        assert entries["1"].last_good == USAGE and entries["2"].last_good == USAGE
        assert entries["1"].sentinel is None

    def test_served_from_cache_inside_ttl(self, usage_switcher, clock):
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)) as fetch:
            usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
            clock["now"] += SERVE_TTL_S / 2
            usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        assert fetch.call_count == 2

    def test_active_expired_is_sentinel_without_refresh(self, usage_switcher, temp_home):
        write_codex_auth(temp_home, make_codex_auth("b@x.y", "acct_b", expired=True))
        with patch.object(codex_auth, "refresh_tokens") as refresh, patch.object(
            codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)
        ):
            entries = usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        refresh.assert_not_called()
        assert entries["2"].sentinel == USAGE_TOKEN_EXPIRED
        assert entries["1"].last_good == USAGE

    def test_active_401_is_sentinel(self, usage_switcher):
        with patch.object(
            codex_auth, "fetch_usage", return_value=UsageOutcome(None, error="http-401")
        ):
            entries = usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        assert entries["2"].sentinel == USAGE_TOKEN_EXPIRED

    def test_inactive_expired_refreshes_and_persists(self, usage_switcher):
        usage_switcher._store.write_slot(
            "1", make_codex_auth("a@x.y", "acct_a", expired=True, refresh_token="rt-a")
        )
        new_tokens = {"access_token": make_codex_auth()["tokens"]["access_token"],
                      "refresh_token": "rt-a2", "id_token": None}
        with patch.object(codex_auth, "refresh_tokens", return_value=new_tokens) as refresh, patch.object(
            codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)
        ):
            entries = usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        refresh.assert_called_once_with("rt-a")
        assert usage_switcher._store.read_slot("1")["tokens"]["refresh_token"] == "rt-a2"
        assert entries["1"].last_good == USAGE

    def test_inactive_401_triggers_refresh(self, usage_switcher):
        outcomes = iter([UsageOutcome(None, error="http-401"), UsageOutcome(USAGE)])
        new_tokens = {"access_token": "at", "refresh_token": "rt-a2", "id_token": None}
        with patch.object(codex_auth, "refresh_tokens", return_value=new_tokens), patch.object(
            codex_auth, "fetch_usage", side_effect=lambda a: next(outcomes)
        ):
            infos = [i for i in usage_switcher._build_accounts_info() if i.number == "1"]
            entries = usage_switcher._collect_usage_entries(infos)
        assert entries["1"].last_good == USAGE

    def test_refresh_cas_does_not_clobber_concurrent_add(self, usage_switcher):
        usage_switcher._store.write_slot(
            "1", make_codex_auth("a@x.y", "acct_a", expired=True, refresh_token="rt-a")
        )

        def refresh(rt):
            # someone re-added the account while the POST was in flight
            usage_switcher._store.write_slot(
                "1", make_codex_auth("a@x.y", "acct_a", refresh_token="rt-fresh")
            )
            return {"access_token": "at", "refresh_token": "rt-a2", "id_token": None}

        with patch.object(codex_auth, "refresh_tokens", side_effect=refresh), patch.object(
            codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)
        ):
            infos = [i for i in usage_switcher._build_accounts_info() if i.number == "1"]
            usage_switcher._collect_usage_entries(infos)
        assert usage_switcher._store.read_slot("1")["tokens"]["refresh_token"] == "rt-fresh"

    def test_invalid_grant_quarantines_until_readded(self, usage_switcher, clock, temp_home):
        usage_switcher._store.write_slot(
            "1", make_codex_auth("a@x.y", "acct_a", expired=True, refresh_token="rt-dead")
        )
        with patch.object(
            codex_auth, "refresh_tokens", side_effect=CodexRefreshError("invalid_grant")
        ) as refresh, patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            for _ in range(AUTH_DEAD_STRIKES):
                infos = [i for i in usage_switcher._build_accounts_info() if i.number == "1"]
                entries = usage_switcher._collect_usage_entries(infos)
                clock["now"] += 24 * 3600  # past any failure backoff
            strikes = refresh.call_count
            assert entries["1"].sentinel == USAGE_RELOGIN_REQUIRED
            # quarantined: no further refresh attempts
            infos = [i for i in usage_switcher._build_accounts_info() if i.number == "1"]
            entries = usage_switcher._collect_usage_entries(infos)
            assert refresh.call_count == strikes
            assert entries["1"].sentinel == USAGE_RELOGIN_REQUIRED
        # re-adding with a fresh login heals the strike
        write_codex_auth(temp_home, make_codex_auth("a@x.y", "acct_a", refresh_token="rt-new"))
        usage_switcher.add_account()
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            entries = usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        assert entries["1"].sentinel is None and entries["1"].last_good == USAGE

    def test_fetch_failure_keeps_last_good(self, usage_switcher, clock):
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        clock["now"] += SERVE_TTL_S + 1
        with patch.object(
            codex_auth, "fetch_usage", return_value=UsageOutcome(None, error="network")
        ):
            entries = usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        assert entries["2"].last_good == USAGE
        assert entries["2"].last_error == "network"

    def test_missing_slot_file_is_no_credentials(self, usage_switcher):
        usage_switcher._store.delete_slot("1")
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            entries = usage_switcher._collect_usage_entries(usage_switcher._build_accounts_info())
        assert entries["1"].sentinel == "no credentials"


class TestSnapshotAndPayloads:
    def test_accounts_snapshot(self, two_accounts):
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            snap = two_accounts.accounts_snapshot()
        assert snap.active_number == "2"
        rows = {a.number: a for a in snap.accounts}
        assert rows["1"].provider == "codex" and rows["1"].kind == "chatgpt"
        assert rows["1"].key == "codex:1"
        assert rows["1"].display_tag == "plus"
        assert rows["1"].org_uuid == "acct_a"
        assert rows["2"].is_active and rows["2"].switchable
        assert rows["1"].usage.last_good == USAGE

    def test_list_json(self, two_accounts):
        two_accounts.set_alias("1", "dev")
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            payload = two_accounts.list_accounts(json_output=True)
        assert payload["schemaVersion"] == 1 and payload["provider"] == "codex"
        assert payload["activeAccountNumber"] == 2
        row = payload["accounts"][0]
        assert row["number"] == 1 and row["alias"] == "dev"
        assert row["planType"] == "plus" and row["accountId"] == "acct_a"
        assert row["usageStatus"] == "ok" and row["usage"]["fiveHour"]["pct"] == 12.0
        assert payload["accounts"][1]["active"] is True

    def test_list_human(self, two_accounts, capsys):
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            two_accounts.list_accounts()
        out = capsys.readouterr().out
        assert "Codex accounts:" in out
        assert "1: a@x.y" in out and "(active)" in out and "[plus]" in out
        assert "12%" in out

    def test_list_empty(self, switcher, codex_home, capsys):
        assert switcher.list_accounts(json_output=True) == {
            "schemaVersion": 1, "provider": "codex", "activeAccountNumber": None, "accounts": [],
        }
        switcher.list_accounts()
        assert "ccswap codex add" in capsys.readouterr().out

    def test_status_json_managed(self, two_accounts):
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            payload = two_accounts.status(json_output=True)
        assert payload["provider"] == "codex"
        assert payload["active"]["number"] == 2 and payload["active"]["managed"] is True
        assert payload["active"]["planType"] == "plus"
        assert payload["totalManagedAccounts"] == 2

    def test_status_unmanaged_and_missing(self, two_accounts, temp_home, capsys):
        write_codex_auth(temp_home, make_codex_auth("stranger@x.y", "acct_s"))
        assert two_accounts.status(json_output=True)["active"] == {
            "email": "stranger@x.y", "managed": False,
        }
        two_accounts.status()
        assert "not managed" in capsys.readouterr().out
        (temp_home / ".codex" / "auth.json").unlink()
        assert two_accounts.status(json_output=True)["active"] is None
        two_accounts.status()
        assert "codex login" in capsys.readouterr().out

    def test_status_human(self, two_accounts, capsys):
        with patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            two_accounts.status()
        out = capsys.readouterr().out
        assert "Codex account 2" in out and "b@x.y" in out and "12%" in out

    def test_relogin_note_points_at_codex(self, two_accounts, capsys):
        two_accounts._store.write_slot(
            "1", make_codex_auth("a@x.y", "acct_a", expired=True, refresh_token="rt-dead")
        )
        with patch.object(
            codex_auth, "refresh_tokens", side_effect=CodexRefreshError("invalid_grant")
        ), patch.object(codex_auth, "fetch_usage", return_value=UsageOutcome(USAGE)):
            store = UsageStore(two_accounts._store.cache_dir, clock=lambda: 1e9)
            two_accounts._usage_store = store
            for i in range(AUTH_DEAD_STRIKES):
                store.clock = (lambda i=i: 1e9 + i * 86400)
                two_accounts.list_accounts()
        out = capsys.readouterr().out
        assert "ccswap codex login" in out
