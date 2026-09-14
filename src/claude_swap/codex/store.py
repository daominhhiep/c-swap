"""On-disk layout for managed Codex accounts.

::

    <backup root>/codex/
        sequence.json     roster: slot number -> {email, accountId, planType, added, alias?}
        auth/<n>.json     that slot's copy of the Codex CLI's auth.json (0600)
        cache/usage.json  UsageStore rows (same format as the Claude cache)
        .lock             serializes ccswap's own Codex writes

The roster never holds tokens; the slot files do. The live login stays
where the Codex CLI keeps it (``~/.codex/auth.json``); switching copies a
slot file over it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.codex.paths import get_codex_auth_path
from claude_swap.exceptions import ConfigError
from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_sequence() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "provider": "codex",
        "activeAccountNumber": None,
        "lastUpdated": _now_iso(),
        "sequence": [],
        "accounts": {},
    }


def _write_json_0600(path: Path, data: dict) -> None:
    """Atomic JSON write that leaves the parent directory's mode alone.

    ``settings.atomic_write_json`` hardens the parent to 0700, which is right
    for ccswap's own directories but not for ``~/.codex`` — that directory
    belongs to the Codex CLI. The file itself still ends up 0600.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
        if sys.platform != "win32":
            os.chmod(str(target), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: Path, what: str) -> dict | None:
    """A JSON object from ``path``; ``None`` when absent, ``ConfigError`` when
    unreadable — a corrupt file must never look like "no accounts"."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError(f"Cannot read {what} ({path}): {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{what} is not valid JSON ({path}): {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{what} is not a JSON object ({path})")
    return data


class CodexStore:
    """Roster, slot files and the live login file, with one lock over them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sequence_file = root / "sequence.json"
        self.slots_dir = root / "auth"
        self.cache_dir = root / "cache"
        self.lock_file = root / ".lock"

    # -- lock / roster --------------------------------------------------------

    def lock(self, timeout: float = 10.0) -> FileLock:
        return FileLock(self.lock_file, timeout=timeout)

    def exists(self) -> bool:
        return self.sequence_file.exists()

    def read_sequence(self) -> dict:
        data = _read_json(self.sequence_file, "Codex account list")
        if data is None:
            return empty_sequence()
        data.setdefault("accounts", {})
        data.setdefault("sequence", [])
        data.setdefault("activeAccountNumber", None)
        if not isinstance(data["accounts"], dict) or not isinstance(data["sequence"], list):
            raise ConfigError(f"Codex account list has an unexpected shape ({self.sequence_file})")
        return data

    def write_sequence(self, data: dict) -> None:
        data["schemaVersion"] = SCHEMA_VERSION
        data["provider"] = "codex"
        data["lastUpdated"] = _now_iso()
        atomic_write_json(self.sequence_file, data)

    # -- slot files -----------------------------------------------------------

    def slot_path(self, num: str) -> Path:
        return self.slots_dir / f"{int(num)}.json"

    def read_slot(self, num: str) -> dict | None:
        return _read_json(self.slot_path(num), f"Codex account {num} login")

    def write_slot(self, num: str, auth: dict) -> None:
        atomic_write_json(self.slot_path(num), auth)

    def delete_slot(self, num: str) -> None:
        try:
            self.slot_path(num).unlink()
        except FileNotFoundError:
            pass

    # -- the Codex CLI's own login file ---------------------------------------

    def live_path(self) -> Path:
        return get_codex_auth_path()

    def read_live(self) -> dict | None:
        return _read_json(self.live_path(), "Codex login")

    def write_live(self, auth: dict) -> None:
        _write_json_0600(self.live_path(), auth)

    def delete_live(self) -> bool:
        """Remove the Codex CLI's login file (the file only — nothing is
        revoked). ``True`` when a file was removed."""
        try:
            self.live_path().unlink()
        except FileNotFoundError:
            return False
        return True

    # -- roster queries -------------------------------------------------------

    def next_number(self) -> int:
        accounts = self.read_sequence().get("accounts", {})
        nums = [int(k) for k in accounts if str(k).isdigit()]
        return max(nums, default=0) + 1

    def find_slot(self, email: str, account_id: str) -> str | None:
        for num, info in self.read_sequence().get("accounts", {}).items():
            if not isinstance(info, dict):
                continue
            if info.get("accountId") == account_id or (
                not account_id and info.get("email") == email
            ):
                return str(num)
        return None

    def find_by_alias(self, alias: str) -> str | None:
        wanted = alias.strip().lower()
        for num, info in self.read_sequence().get("accounts", {}).items():
            if isinstance(info, dict) and info.get("alias") == wanted:
                return str(num)
        return None

    def alias_in_use(self, alias: str, *, exclude_num: str | None = None) -> str | None:
        owner = self.find_by_alias(alias)
        if owner is not None and owner != exclude_num:
            return owner
        return None

    def resolve_identifier(self, identifier: str) -> str | None:
        """Slot number for ``identifier`` — a number, an alias or an email.

        Returns ``None`` when nothing matches; raises ``ConfigError`` when an
        email matches several slots (name the slot number instead).
        """
        ident = identifier.strip()
        data = self.read_sequence()
        accounts = data.get("accounts", {})
        if ident.isdigit():
            return ident if ident in accounts else None
        by_alias = self.find_by_alias(ident)
        if by_alias is not None:
            return by_alias
        matches = [
            str(num)
            for num, info in accounts.items()
            if isinstance(info, dict)
            and str(info.get("email", "")).lower() == ident.lower()
        ]
        if len(matches) > 1:
            raise ConfigError(
                f"'{identifier}' matches Codex accounts {', '.join(matches)}; "
                "use the slot number"
            )
        return matches[0] if matches else None
