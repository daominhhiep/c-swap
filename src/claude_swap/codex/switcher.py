"""``CodexAccountSwitcher`` — the Codex CLI twin of ``ClaudeAccountSwitcher``.

Only the surface the CLI and the TUI call is implemented: add, remove,
alias, switch, switch_to, list, status and ``accounts_snapshot``. Slots are
whole copies of the Codex CLI's ``auth.json``; switching copies one over the
live file after saving the (possibly rotated) live tokens back into their
own slot. Usage is read through the shared :class:`UsageStore`, so pacing,
backoff and dead-token quarantine behave exactly as they do for Claude
accounts.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.codex import auth as codex_auth
from claude_swap.codex.auth import CodexAuthError, CodexIdentity, CodexRefreshError
from claude_swap.codex.paths import credentials_store_kind, get_codex_backup_root
from claude_swap.codex.store import CodexStore
from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    SwitchError,
    ValidationError,
)
from claude_swap.json_output import (
    SCHEMA_VERSION,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
    account_ref,
    account_row,
    last_good_usage_fields,
    usage_failure_fields,
    usage_fields,
    usage_freshness_fields,
)
from claude_swap.models import AccountSnapshot, AccountsSnapshot, normalize_alias
from claude_swap.printer import (
    accent,
    bold_accent,
    bolded,
    dimmed,
    muted,
    warning,
)
from claude_swap.claude.switcher import (
    _FETCH_STAGGER_S,
    SENTINEL_NOTES,
    _usage_entry_lines,
)
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore, with_sentinel

PROVIDER = "codex"
KIND = "chatgpt"

# The Claude wording for a dead refresh token points at Claude Code; the
# Codex twin points at `codex login`.
_CODEX_SENTINEL_NOTES = {
    **SENTINEL_NOTES,
    USAGE_RELOGIN_REQUIRED: (
        "re-login needed — refresh token dead; run: ccswap codex login"
    ),
}

# `codex login` revokes whatever login it finds before starting a new one, so
# the only safe way to add a second account is through `ccswap codex login`.
_LOGIN_HINT = "To add another account run `ccswap codex login` (a plain `codex login` would revoke this one)"


def _usage_lines(entry: UsageEntry) -> list[str]:
    """``_usage_entry_lines`` with the Codex re-login wording."""
    if entry.sentinel == USAGE_RELOGIN_REQUIRED:
        lines = _usage_entry_lines(entry)
        lines[0] = dimmed(_CODEX_SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED])
        return lines
    return _usage_entry_lines(entry)


@dataclass(frozen=True)
class CodexAccountInfo:
    number: str
    email: str
    account_id: str
    plan_type: str
    alias: str
    is_active: bool
    auth: dict | None  # live file for the active slot, the slot copy otherwise


class CodexAccountSwitcher:
    """Manage ChatGPT logins for the Codex CLI. See the module docstring."""

    provider = PROVIDER

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.backup_dir: Path = get_codex_backup_root()
        self._store = CodexStore(self.backup_dir)
        self._usage_store = UsageStore(self._store.cache_dir)
        # The Claude switcher installs the file handler on this logger; when
        # constructed alone (tests) records simply go to the root logger.
        self._logger = logging.getLogger("claude-swap")

    # -- live login -----------------------------------------------------------

    def _live_auth(self) -> dict | None:
        """The Codex CLI's current login, or ``None`` when nobody is logged in.

        Refuses the two shapes ccswap cannot manage: an API-key login (there
        is no ChatGPT account behind it) and the keyring credential store
        (there is no ``auth.json`` to copy).
        """
        if credentials_store_kind() == "keyring":
            raise CodexAuthError(
                "The Codex CLI keeps its login in the system keyring "
                "(cli_auth_credentials_store = \"keyring\" in ~/.codex/config.toml); "
                "ccswap can only manage the file store. Set it to \"file\", run "
                "`codex login` again, then retry."
            )
        auth = self._store.read_live()
        if auth is None:
            return None
        mode = codex_auth.auth_mode(auth)
        if mode == "apikey":
            raise CodexAuthError(
                "The Codex CLI is logged in with an API key; only ChatGPT logins "
                "can be managed. Run `codex login` and sign in with ChatGPT."
            )
        if mode != "chatgpt":
            return None
        return auth

    def _live_identity(self) -> tuple[dict, CodexIdentity] | None:
        auth = self._live_auth()
        if auth is None:
            return None
        return auth, codex_auth.identity_from_auth(auth)

    def current_account_number(self) -> str | None:
        """The slot whose account the Codex CLI is logged in as, if managed."""
        try:
            live = self._live_identity()
        except CodexAuthError:
            return None
        if live is None:
            return None
        return self._store.find_slot(live[1].email, live[1].account_id)

    # -- roster helpers -------------------------------------------------------

    def _require_roster(self) -> dict:
        if not self._store.exists():
            raise ConfigError("No Codex accounts are managed yet — run `ccswap codex add`")
        return self._store.read_sequence()

    def _resolve(self, identifier: str) -> str:
        num = self._store.resolve_identifier(identifier)
        if num is None:
            raise AccountNotFoundError(f"Codex account '{identifier}' does not exist")
        return num

    @staticmethod
    def _tag(info: dict) -> str:
        plan = info.get("planType") if isinstance(info, dict) else ""
        return plan if isinstance(plan, str) and plan else "chatgpt"

    def _label(self, num: str, info: dict) -> str:
        return f"Codex account {num} ({info.get('email', '')} {muted('[' + self._tag(info) + ']')})"

    # -- mutations ------------------------------------------------------------

    def add_account(
        self, slot: int | None = None, alias: str | None = None, assume_yes: bool = False
    ) -> None:
        """Back up the Codex CLI's current login as a managed account."""
        try:
            normalized_alias = normalize_alias(alias) if alias else ""
        except ValueError as e:
            raise ValidationError(str(e)) from e
        live = self._live_identity()
        if live is None:
            raise CodexAuthError(
                "No Codex login found. Run `codex login`, sign in with ChatGPT, then retry."
            )
        auth, identity = live

        with self._store.lock():
            data = self._store.read_sequence()
            accounts: dict = data["accounts"]
            existing = self._store.find_slot(identity.email, identity.account_id)
            if normalized_alias:
                # The alias may only be held by the slot this add ends up in.
                keep = existing if slot is None else str(slot)
                owner = self._store.alias_in_use(normalized_alias, exclude_num=keep)
                if owner is not None:
                    raise ValidationError(
                        f"alias '{normalized_alias}' is already used by Codex account {owner}"
                    )

            if existing is not None and slot is None:
                # Same account again: refresh the stored copy in place.
                num = existing
                if normalized_alias:
                    accounts[num]["alias"] = normalized_alias
                accounts[num]["planType"] = identity.plan_type
                self._store.write_slot(num, auth)
                data["activeAccountNumber"] = int(num)
                self._store.write_sequence(data)
                self._usage_store.clear_dead_token(
                    [num], {num: (identity.email, identity.account_id)}
                )
                self._logger.info("Updated Codex account %s", num)
                print(f"{accent('Updated')} {self._label(num, accounts[num])}")
                print(dimmed(_LOGIN_HINT))
                return

            if slot is not None:
                if slot < 1:
                    raise ValidationError("Slot numbers start at 1")
                num = str(slot)
                occupant = accounts.get(num)
                if occupant and (
                    occupant.get("accountId") != identity.account_id
                ) and not assume_yes:
                    answer = input(
                        f"Codex slot {num} holds {occupant.get('email')}. Overwrite? [y/N] "
                    )
                    if answer.strip().lower() != "y":
                        print(dimmed("Cancelled"))
                        return
                if existing is not None and existing != num:
                    # Moving the account to a new slot: drop the old one.
                    self._store.delete_slot(existing)
                    del accounts[existing]
                    data["sequence"] = [n for n in data["sequence"] if str(n) != existing]
            else:
                num = str(self._store.next_number())

            record = {
                "email": identity.email,
                "accountId": identity.account_id,
                "planType": identity.plan_type,
                "added": datetime.now(timezone.utc).isoformat(),
            }
            previous = accounts.get(num) or {}
            if normalized_alias:
                record["alias"] = normalized_alias
            elif previous.get("accountId") == identity.account_id and previous.get("alias"):
                record["alias"] = previous["alias"]
            accounts[num] = record
            if int(num) not in data["sequence"]:
                data["sequence"].append(int(num))
                data["sequence"].sort()
            self._store.write_slot(num, auth)
            data["activeAccountNumber"] = int(num)
            self._store.write_sequence(data)
            self._usage_store.clear_dead_token(
                [num], {num: (identity.email, identity.account_id)}
            )
        self._logger.info("Added Codex account %s", num)
        print(f"{accent('Added')} {self._label(num, record)}")

    def login(self, codex_args: list[str] | None = None) -> None:
        """Sign in to another ChatGPT account without losing the current one.

        ``codex login`` revokes the login it finds in ``auth.json`` before it
        starts a new one (``clear_existing_auth_before_login`` in the Codex
        CLI), which kills the saved copy too. So: save the current login to
        its slot, move the file out of the way, run ``codex login``, then
        save the new login as an account. If the login fails or is
        cancelled, the previous file is put back.
        """
        codex = shutil.which("codex")
        if codex is None:
            raise CodexAuthError("The `codex` command was not found on PATH")

        stashed: dict | None = None
        raw = self._store.read_live()
        if raw is not None and codex_auth.auth_mode(raw) == "apikey":
            # An API-key login: nothing ccswap manages, and `codex login`
            # does not revoke it. Leave it to the Codex CLI.
            print(dimmed("Current Codex login is an API key; leaving it to `codex login`"))
            live = None
        else:
            live = self._live_identity()
        if live is not None:
            auth, identity = live
            with self._store.lock():
                data = self._store.read_sequence()
                num = self._store.find_slot(identity.email, identity.account_id)
                if num is None:
                    num = str(self._store.next_number())
                    data["accounts"][num] = {
                        "email": identity.email,
                        "accountId": identity.account_id,
                        "planType": identity.plan_type,
                        "added": datetime.now(timezone.utc).isoformat(),
                    }
                    data["sequence"].append(int(num))
                    data["sequence"].sort()
                    print(f"{accent('Saved')} current login as {self._label(num, data['accounts'][num])}")
                else:
                    data["accounts"][num]["planType"] = identity.plan_type
                    print(f"{accent('Saved')} current login to {self._label(num, data['accounts'][num])}")
                self._store.write_slot(num, auth)
                data["activeAccountNumber"] = None
                self._store.write_sequence(data)
                self._store.delete_live()
                stashed = auth

        print(dimmed(f"Running: codex login {' '.join(codex_args or [])}".rstrip()))
        sys.stdout.flush()
        try:
            rc = subprocess.call([codex, "login", *(codex_args or [])])
        except KeyboardInterrupt:
            rc = 130

        new_live = self._store.read_live()
        if rc != 0 or new_live is None or codex_auth.auth_mode(new_live) != "chatgpt":
            if stashed is not None:
                with self._store.lock():
                    if self._store.read_live() is None:
                        self._store.write_live(stashed)
                        data = self._store.read_sequence()
                        data["activeAccountNumber"] = int(
                            self._store.find_slot(*self._identity_pair(stashed))
                        )
                        self._store.write_sequence(data)
                print(dimmed("Previous Codex login restored"))
            if rc == 130:
                raise KeyboardInterrupt
            raise CodexAuthError(
                f"`codex login` exited with status {rc}" if rc != 0
                else "`codex login` finished without a ChatGPT login"
            )
        self.add_account()

    @staticmethod
    def _identity_pair(auth: dict) -> tuple[str, str]:
        ident = codex_auth.identity_from_auth(auth)
        return ident.email, ident.account_id

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        data = self._require_roster()
        num = self._resolve(identifier)
        info = data["accounts"][num]
        email = info.get("email", "")
        if self.current_account_number() == num:
            warning(f"Warning: Codex account {num} ({email}) is the current Codex login")
        if not assume_yes:
            confirm = input(
                f"Are you sure you want to permanently remove Codex account {num} ({email})? [y/N] "
            )
            if confirm.strip().lower() != "y":
                print(dimmed("Cancelled"))
                return
        with self._store.lock():
            data = self._store.read_sequence()
            self._store.delete_slot(num)
            data["accounts"].pop(num, None)
            data["sequence"] = [n for n in data["sequence"] if str(n) != num]
            if str(data.get("activeAccountNumber")) == num:
                data["activeAccountNumber"] = None
            self._store.write_sequence(data)
        self._logger.info("Removed Codex account %s", num)
        print(f"{accent('Removed')} Codex account {num} ({email})")

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        try:
            normalized = normalize_alias(alias)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        self._require_roster()
        num = self._resolve(identifier)
        owner = self._store.alias_in_use(normalized, exclude_num=num)
        if owner is not None:
            raise ValidationError(
                f"alias '{normalized}' is already used by Codex account {owner}"
            )
        with self._store.lock():
            data = self._store.read_sequence()
            data["accounts"][num]["alias"] = normalized
            self._store.write_sequence(data)
        return num, normalized

    def unset_alias(self, identifier: str) -> str:
        self._require_roster()
        num = self._resolve(identifier)
        with self._store.lock():
            data = self._store.read_sequence()
            data["accounts"][num].pop("alias", None)
            self._store.write_sequence(data)
        return num

    def list_aliases(self) -> list[tuple[str, str, str]]:
        accounts = self._store.read_sequence().get("accounts", {})
        return [
            (num, info["alias"], info.get("email", ""))
            for num, info in sorted(accounts.items(), key=lambda kv: int(kv[0]))
            if isinstance(info, dict) and info.get("alias")
        ]

    # -- switching ------------------------------------------------------------

    def _perform_switch(self, target: str, *, force: bool) -> dict:
        """Copy slot ``target`` over the live login. See the module docstring."""
        warnings_out: list[str] = []
        with self._store.lock():
            data = self._store.read_sequence()
            target_info = data["accounts"].get(target)
            if target_info is None:
                raise AccountNotFoundError(f"Codex account {target} does not exist")
            target_auth = self._store.read_slot(target)
            if target_auth is None:
                raise CredentialReadError(
                    f"Codex account {target} has no stored login — run "
                    f"`ccswap codex login` and sign in as that account"
                )

            from_ref = account_ref(None, "")
            live = self._store.read_live()
            if live is not None:
                mode = codex_auth.auth_mode(live)
                if mode == "chatgpt":
                    try:
                        identity = codex_auth.identity_from_auth(live)
                    except CodexAuthError:
                        identity = None
                    current = (
                        self._store.find_slot(identity.email, identity.account_id)
                        if identity
                        else None
                    )
                    if current is not None:
                        # Save the (possibly rotated) live tokens into their slot
                        # so the copy we activate later is the freshest one.
                        self._store.write_slot(current, live)
                        from_ref = account_ref(int(current), identity.email)
                    else:
                        email = identity.email if identity else "unknown"
                        if not force:
                            raise SwitchError(
                                f"The current Codex login ({email}) is not a managed "
                                "account — run `ccswap codex add` to keep it, or pass "
                                "--force to replace it"
                            )
                        from_ref = account_ref(None, email)
                        warnings_out.append(
                            f"replaced an unmanaged Codex login ({email}); it was not saved"
                        )
                elif mode == "apikey":
                    if not force:
                        raise SwitchError(
                            "The Codex CLI is logged in with an API key; pass --force "
                            "to replace that login with the stored ChatGPT account"
                        )
                    warnings_out.append("replaced an API-key Codex login; it was not saved")

            self._store.write_live(target_auth)
            data["activeAccountNumber"] = int(target)
            self._store.write_sequence(data)
        self._logger.info("Switched Codex login to account %s", target)
        return {
            "from": from_ref,
            "to": account_ref(int(target), target_info.get("email", "")),
            "warnings": warnings_out,
        }

    def _result(self, op: dict, *, strategy: str) -> dict:
        switched = op["from"] != op["to"]
        to_ref = op["to"]
        if switched:
            reason, message = "switched", f"Switched to Codex account {to_ref['number']} ({to_ref['email']})"
        else:
            reason, message = "already-active", f"Already on Codex account {to_ref['number']} ({to_ref['email']})"
        return {
            "schemaVersion": SCHEMA_VERSION,
            "provider": PROVIDER,
            "switched": switched,
            "from": op["from"],
            "to": to_ref,
            "strategy": strategy,
            "reason": reason,
            "message": message,
            "warnings": op["warnings"],
        }

    def _emit(self, result: dict, json_output: bool) -> dict | None:
        if json_output:
            return result
        for w in result["warnings"]:
            warning(f"Warning: {w}")
        if result["switched"]:
            print(f"{accent('Switched to')} Codex account {result['to']['number']} ({result['to']['email']})")
            print(dimmed("Running codex sessions keep their old login; restart them to pick this up."))
        else:
            print(f"{dimmed(result['message'])}")
        return None

    def switch(self, json_output: bool = False) -> dict | None:
        """Rotate to the next managed Codex account."""
        data = self._require_roster()
        order = [str(n) for n in data.get("sequence", []) if str(n) in data["accounts"]]
        if not order:
            raise ConfigError("No Codex accounts are managed yet — run `ccswap codex add`")
        current = self.current_account_number()
        if current in order:
            target = order[(order.index(current) + 1) % len(order)]
        else:
            recorded = str(data.get("activeAccountNumber"))
            target = (
                order[(order.index(recorded) + 1) % len(order)]
                if recorded in order
                else order[0]
            )
        if len(order) == 1 and current == target:
            result = self._result(
                {"from": account_ref(int(target), data["accounts"][target].get("email", "")),
                 "to": account_ref(int(target), data["accounts"][target].get("email", "")),
                 "warnings": []},
                strategy="rotate",
            )
            return self._emit(result, json_output)
        op = self._perform_switch(target, force=False)
        return self._emit(self._result(op, strategy="rotate"), json_output)

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict | None:
        """Switch to one Codex account by number, alias or email."""
        data = self._require_roster()
        target = self._resolve(identifier)
        if not force and self.current_account_number() == target:
            info = data["accounts"][target]
            ref = account_ref(int(target), info.get("email", ""))
            result = self._result({"from": ref, "to": ref, "warnings": []}, strategy="direct")
            return self._emit(result, json_output)
        op = self._perform_switch(target, force=force)
        if force and op["from"] == op["to"]:
            # A forced re-activation onto the current account is a real write.
            result = self._result(op, strategy="direct")
            result.update(switched=True, reason="forced", message=f"Re-activated Codex account {target}")
            return self._emit(result, json_output)
        return self._emit(self._result(op, strategy="direct"), json_output)

    # -- read model -----------------------------------------------------------

    def _build_accounts_info(self) -> list[CodexAccountInfo]:
        data = self._store.read_sequence()
        accounts = data.get("accounts", {})
        live_auth: dict | None = None
        live_id: CodexIdentity | None = None
        try:
            live = self._live_identity()
        except CodexAuthError:
            live = None
        if live is not None:
            live_auth, live_id = live
        infos: list[CodexAccountInfo] = []
        for num, info in sorted(accounts.items(), key=lambda kv: int(kv[0])):
            if not isinstance(info, dict):
                continue
            is_active = live_id is not None and info.get("accountId") == live_id.account_id
            auth = live_auth if is_active else self._store.read_slot(num)
            infos.append(
                CodexAccountInfo(
                    number=str(num),
                    email=info.get("email", ""),
                    account_id=info.get("accountId", ""),
                    plan_type=info.get("planType", "") or "",
                    alias=info.get("alias", "") or "",
                    is_active=is_active,
                    auth=auth,
                )
            )
        return infos

    def _fetch_account_usage(self, info: CodexAccountInfo) -> FetchRecord:
        """One usage fetch, refreshing a non-active slot's token when needed."""
        auth = info.auth
        if auth is None:
            return FetchRecord(sentinel=USAGE_NO_CREDENTIALS)
        expired = codex_auth.access_token_expired(auth)
        if info.is_active:
            # The Codex CLI owns the live file and refreshes it itself.
            if expired:
                return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
            outcome = codex_auth.fetch_usage(auth)
            if outcome.error == "http-401":
                return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
            return FetchRecord(
                usage=outcome.usage, error=outcome.error, retry_after_s=outcome.retry_after_s
            )

        if not expired:
            outcome = codex_auth.fetch_usage(auth)
            if outcome.error != "http-401":
                return FetchRecord(
                    usage=outcome.usage, error=outcome.error, retry_after_s=outcome.retry_after_s
                )
        refreshed = self._refresh_slot(info.number, auth)
        if isinstance(refreshed, FetchRecord):
            return refreshed
        outcome = codex_auth.fetch_usage(refreshed)
        return FetchRecord(
            usage=outcome.usage, error=outcome.error, retry_after_s=outcome.retry_after_s
        )

    def _refresh_slot(self, num: str, auth: dict) -> dict | FetchRecord:
        """Refresh a stored slot's tokens and persist them (fingerprint CAS).

        The POST happens outside the lock; the write only lands if the slot
        still holds the generation that was refreshed, so a concurrent
        ``add`` is never clobbered. Returns the refreshed auth dict, or a
        failure ``FetchRecord``.
        """
        fp = codex_auth.refresh_fingerprint(auth)
        rt = auth.get("tokens", {}).get("refresh_token") if isinstance(auth.get("tokens"), dict) else None
        if not rt:
            return FetchRecord(error="no-refresh-token")
        try:
            tokens = codex_auth.refresh_tokens(rt)
        except CodexRefreshError as e:
            struck = fp if e.kind == "invalid_grant" else None
            return FetchRecord(error=e.kind, struck_fp=struck)
        merged = codex_auth.merged_after_refresh(auth, tokens)
        with self._store.lock():
            current = self._store.read_slot(num)
            if current is not None and codex_auth.refresh_fingerprint(current) == fp:
                self._store.write_slot(num, merged)
                return merged
        # Someone replaced the slot meanwhile: use what is stored now.
        return current if current is not None else FetchRecord(error="slot-replaced")

    def _collect_usage_entries(
        self, infos: list[CodexAccountInfo], fetch: set[str] | None = None
    ) -> dict[str, UsageEntry]:
        store = self._usage_store
        identities = {i.number: (i.email, i.account_id) for i in infos}
        by_num = {i.number: i for i in infos}
        entries = store.entries(identities)
        sentinels: dict[str, str] = {}
        for num, info in by_num.items():
            if info.auth is None:
                sentinels[num] = USAGE_NO_CREDENTIALS
            elif entries[num].token_dead(stored_fp=codex_auth.refresh_fingerprint(info.auth)):
                sentinels[num] = USAGE_RELOGIN_REQUIRED
            elif entries[num].auth_dead_strikes and entries[num].token_dead():
                store.clear_dead_token([num], {num: identities[num]})
                entries = store.entries(identities)
        requested = [
            num for num in by_num
            if num not in sentinels and (fetch is None or num in fetch)
        ]
        claims = store.reserve(
            requested, identities, respect_plans=True, repair_overslept=True
        )
        for num, info in by_num.items():
            if num in sentinels or num in claims or not info.is_active:
                continue
            if info.auth is not None and codex_auth.access_token_expired(info.auth):
                sentinels[num] = USAGE_TOKEN_EXPIRED
        if claims:
            records: dict[str, FetchRecord] = {}
            for i, num in enumerate(claims):
                if i:
                    time.sleep(_FETCH_STAGGER_S)
                records[num] = self._fetch_account_usage(by_num[num])
            accepted = store.record(records, identities, claims)
            for num in accepted:
                if records[num].sentinel is not None:
                    sentinels[num] = records[num].sentinel
            entries = store.entries(identities)
            for num in by_num:
                if num not in sentinels and entries[num].token_dead(
                    stored_fp=codex_auth.refresh_fingerprint(by_num[num].auth or {})
                ):
                    sentinels[num] = USAGE_RELOGIN_REQUIRED
        return {
            num: with_sentinel(entries[num], sentinels.get(num)) for num in by_num
        }

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        infos = self._build_accounts_info()
        entries = self._collect_usage_entries(infos, fetch=fetch)
        active: str | None = None
        rows: list[AccountSnapshot] = []
        for info in infos:
            if info.is_active:
                active = info.number
            rows.append(
                AccountSnapshot(
                    number=info.number,
                    email=info.email,
                    org_name=info.plan_type,
                    org_uuid=info.account_id,
                    is_active=info.is_active,
                    kind=KIND,
                    switchable=self._store.slot_path(info.number).exists(),
                    usage=entries[info.number],
                    alias=info.alias,
                    disabled=False,
                    provider=PROVIDER,
                )
            )
        return AccountsSnapshot(
            active_number=active, accounts=tuple(rows), taken_at=self._usage_store.clock()
        )

    def usage_fetch_stamps(self) -> dict[str, float | None]:
        accounts = self._store.read_sequence().get("accounts", {})
        identities = {
            str(num): (info.get("email", ""), info.get("accountId", ""))
            for num, info in accounts.items()
            if isinstance(info, dict)
        }
        return {
            num: entry.fetched_at
            for num, entry in self._usage_store.entries(identities).items()
        }

    def set_poll_policy_inputs(self, *_args) -> None:  # TUI parity; no auto engine
        return None

    def clear_poll_policy_inputs(self) -> None:
        return None

    # -- list / status --------------------------------------------------------

    def _row(self, info: CodexAccountInfo, entry: UsageEntry) -> dict:
        now = self._usage_store.clock()
        row = account_row(
            int(info.number), info.email, info.plan_type, info.account_id, info.is_active,
            entry.decision_value(),
            usage_fetched_at=entry.fetched_at,
            usage_age_s=entry.age_s,
            last_good_usage=entry.last_good,
            last_error=entry.last_error,
            backoff_until=entry.backoff_until if entry.in_backoff(now) else None,
            alias=info.alias,
        )
        row["planType"] = info.plan_type
        row["accountId"] = info.account_id
        return row

    def list_accounts(
        self,
        show_token_status: bool = False,
        json_output: bool = False,
        fetch: set[str] | None = None,
    ) -> dict | None:
        if not self._store.exists():
            if json_output:
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "provider": PROVIDER,
                    "activeAccountNumber": None,
                    "accounts": [],
                }
            print(dimmed("No Codex accounts are managed yet — run `ccswap codex add`."))
            return None
        infos = self._build_accounts_info()
        entries = self._collect_usage_entries(infos, fetch=fetch)
        if json_output:
            active = next((int(i.number) for i in infos if i.is_active), None)
            return {
                "schemaVersion": SCHEMA_VERSION,
                "provider": PROVIDER,
                "activeAccountNumber": active,
                "accounts": [self._row(i, entries[i.number]) for i in infos],
            }
        print(bolded("Codex accounts:"))
        for info in infos:
            label = f"{accent(info.alias)} ({info.email})" if info.alias else info.email
            tag = info.plan_type or KIND
            marker = f" {bold_accent('(active)')}" if info.is_active else ""
            print(f"  {info.number}: {label} {muted(f'[{tag}]')}{marker}")
            for line in _usage_lines(entries[info.number]):
                print(f"     {line}")
        return None

    def status(self, json_output: bool = False) -> dict | None:
        try:
            live = self._live_identity()
        except CodexAuthError as e:
            if json_output:
                return {"schemaVersion": SCHEMA_VERSION, "provider": PROVIDER, "active": None, "error": str(e)}
            print(f"{bolded('Codex status:')} {dimmed(str(e))}")
            return None
        if live is None:
            if json_output:
                return {"schemaVersion": SCHEMA_VERSION, "provider": PROVIDER, "active": None}
            print(f"{bolded('Codex status:')} {dimmed('No Codex login (run `codex login`)')}")
            return None
        _auth, identity = live
        num = self._store.find_slot(identity.email, identity.account_id)
        total = len(self._store.read_sequence().get("accounts", {}))
        if num is None:
            if json_output:
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "provider": PROVIDER,
                    "active": {"email": identity.email, "managed": False},
                    "totalManagedAccounts": total,
                }
            print(f"{bolded('Codex status:')} {identity.email} {dimmed('(not managed)')}")
            return None
        infos = [i for i in self._build_accounts_info() if i.number == num]
        info = infos[0]
        entry = self._collect_usage_entries(infos)[num]
        if json_output:
            status, usage = usage_fields(entry.decision_value(), entry.fetched_at)
            active: dict = {
                "number": int(num),
                "email": info.email,
                "planType": info.plan_type,
                "accountId": info.account_id,
                "managed": True,
                "usageStatus": status,
                "usage": usage,
            }
            if info.alias:
                active["alias"] = info.alias
            if usage is not None:
                active.update(usage_freshness_fields(entry.fetched_at, entry.age_s))
            else:
                active.update(last_good_usage_fields(entry.last_good, entry.fetched_at, entry.age_s))
                now = self._usage_store.clock()
                active.update(usage_failure_fields(
                    status, entry.last_error,
                    entry.backoff_until if entry.in_backoff(now) else None,
                ))
            return {
                "schemaVersion": SCHEMA_VERSION,
                "provider": PROVIDER,
                "active": active,
                "totalManagedAccounts": total,
            }
        tag = info.plan_type or KIND
        print(
            f"{bolded('Codex status:')} {accent(f'Codex account {num}')} "
            f"({info.email} {muted(f'[{tag}]')})"
        )
        print(f"  {dimmed(f'Total managed Codex accounts: {total}')}")
        for line in _usage_lines(entry):
            print(f"  {line}")
        return None
