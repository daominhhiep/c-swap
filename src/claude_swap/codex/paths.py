"""Path resolution for the Codex CLI's login and ccswap's Codex slots.

The Codex CLI keeps its ChatGPT login in ``$CODEX_HOME/auth.json``
(``~/.codex/auth.json`` by default), a 0600 JSON file. ccswap's own Codex
data — the slot roster, the per-slot copies of ``auth.json`` and the usage
cache — lives under the regular backup root in a ``codex/`` subdirectory, so
``ccswap purge`` and the test suite's real-store guard cover it for free and
the upstream ``cswap`` simply ignores it.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from claude_swap.paths import get_backup_root

CODEX_DIRNAME = ".codex"
AUTH_FILENAME = "auth.json"
CONFIG_FILENAME = "config.toml"


def get_codex_home() -> Path:
    """Return the Codex CLI's home (``CODEX_HOME`` or ``~/.codex``)."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / CODEX_DIRNAME


def get_codex_auth_path() -> Path:
    """Return the live Codex login file (``<codex home>/auth.json``)."""
    return get_codex_home() / AUTH_FILENAME


def get_codex_config_path() -> Path:
    """Return the Codex CLI's ``config.toml``."""
    return get_codex_home() / CONFIG_FILENAME


def get_codex_backup_root() -> Path:
    """Return ccswap's Codex data directory (``<backup root>/codex``)."""
    return get_backup_root() / "codex"


def credentials_store_kind() -> str:
    """Where the Codex CLI keeps its login: ``"file"``, ``"keyring"`` or ``"auto"``.

    Read from ``cli_auth_credentials_store`` in ``config.toml``. A missing or
    unreadable config means the default file store. Only the file store is
    supported: with the keyring store there is no ``auth.json`` to copy.
    """
    try:
        with get_codex_config_path().open("rb") as fh:
            config = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return "file"
    kind = config.get("cli_auth_credentials_store") if isinstance(config, dict) else None
    return kind if isinstance(kind, str) and kind else "file"
