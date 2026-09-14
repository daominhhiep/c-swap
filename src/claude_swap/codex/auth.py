"""Codex CLI login: identity, token refresh and usage.

This is the only module that knows OpenAI's endpoints. The rest of the
Codex path deals in the ``auth.json`` dict the Codex CLI writes::

    {"auth_mode": "chatgpt", "OPENAI_API_KEY": null,
     "tokens": {"id_token": ..., "access_token": ..., "refresh_token": ...,
                "account_id": ...},
     "last_refresh": "2026-09-14T10:00:00Z"}

Identity comes from the ``id_token`` JWT (``email`` plus the ChatGPT account
id and plan under the ``https://api.openai.com/auth`` claim). Refresh uses
the Codex CLI's own public client id; refresh tokens rotate, so a refreshed
slot must be written back. Usage is read from ChatGPT's rate-limit endpoint,
which is not a documented API — it is isolated here so a change only costs
the usage column, never account switching.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.oauth import UsageOutcome, _classify_usage_error, format_reset

_logger = logging.getLogger("claude-swap")

OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT = f"ccswap/{__version__}"
AUTH_CLAIM = "https://api.openai.com/auth"

# Windows shorter than this are the session ("5-hour") lane; anything longer
# is the weekly lane. Classifying by length rather than by primary/secondary
# position matters because some plans only carry a weekly window, and the
# server then reports it as the primary one.
_SESSION_WINDOW_MAX_S = 6 * 3600


class CodexAuthError(ClaudeSwitchError):
    """The Codex login cannot be used (missing, API-key mode, malformed)."""


class CodexRefreshError(CodexAuthError):
    """A token refresh failed; ``kind`` is a short stable token for the cause
    (``invalid_grant``, ``http-4xx``, ``network``, ``timeout``, ``bad-response``)."""

    def __init__(self, kind: str, message: str | None = None) -> None:
        super().__init__(message or f"Codex token refresh failed ({kind})")
        self.kind = kind


@dataclass(frozen=True)
class CodexIdentity:
    email: str
    account_id: str
    plan_type: str = ""


# -- auth.json helpers --------------------------------------------------------


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT's payload without verifying its signature.

    The token was issued to this machine by OpenAI's auth server and only
    identifies the login; nothing here grants access based on its claims.
    Raises ``ValueError`` for anything that is not a three-part JWT with a
    JSON object payload.
    """
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3 or not parts[1]:
        raise ValueError("not a JWT")
    segment = parts[1].replace("-", "+").replace("_", "/")
    segment += "=" * (-len(segment) % 4)
    try:
        claims = json.loads(base64.b64decode(segment, validate=True))
    except (binascii.Error, UnicodeError, json.JSONDecodeError) as e:
        raise ValueError(f"malformed JWT payload: {e}") from e
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


def _tokens(auth: dict) -> dict:
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    return tokens if isinstance(tokens, dict) else {}


def auth_mode(auth: dict) -> str:
    """``"chatgpt"``, ``"apikey"`` or ``""`` when the file carries no mode.

    Older Codex builds wrote no ``auth_mode``; treat a file with tokens as a
    ChatGPT login and one with only an API key as API-key mode.
    """
    mode = auth.get("auth_mode") if isinstance(auth, dict) else None
    if isinstance(mode, str) and mode:
        return mode.lower()
    if _tokens(auth).get("access_token"):
        return "chatgpt"
    if isinstance(auth, dict) and auth.get("OPENAI_API_KEY"):
        return "apikey"
    return ""


def identity_from_auth(auth: dict) -> CodexIdentity:
    """Who the login belongs to, from the ``id_token`` claims.

    ``account_id`` prefers the JWT's ``chatgpt_account_id`` and falls back to
    ``tokens.account_id`` (the same value, as written by the Codex CLI).
    """
    tokens = _tokens(auth)
    id_token = tokens.get("id_token")
    claims: dict = {}
    if isinstance(id_token, str) and id_token:
        try:
            claims = decode_jwt_payload(id_token)
        except ValueError as e:
            raise CodexAuthError(f"Codex login has an unreadable id_token: {e}") from e
    auth_claims = claims.get(AUTH_CLAIM)
    auth_claims = auth_claims if isinstance(auth_claims, dict) else {}

    email = claims.get("email") or auth_claims.get("chatgpt_user_email")
    account_id = auth_claims.get("chatgpt_account_id") or tokens.get("account_id")
    plan = auth_claims.get("chatgpt_plan_type")
    if not isinstance(email, str) or not email:
        raise CodexAuthError(
            "Codex login carries no email address; run `codex login` and try again."
        )
    if not isinstance(account_id, str) or not account_id:
        raise CodexAuthError(
            "Codex login carries no ChatGPT account id; run `codex login` and try again."
        )
    return CodexIdentity(
        email=email,
        account_id=account_id,
        plan_type=plan if isinstance(plan, str) else "",
    )


def access_token_expired(auth: dict, buffer_s: float = 300) -> bool:
    """True when the access token's ``exp`` claim is within ``buffer_s`` of now.

    A token whose expiry cannot be read is treated as live: the server's 401
    is the authoritative answer, and a refresh can follow that.
    """
    token = _tokens(auth).get("access_token")
    if not isinstance(token, str) or not token:
        return True
    try:
        exp = decode_jwt_payload(token).get("exp")
    except ValueError:
        return False
    if not isinstance(exp, (int, float)):
        return False
    return exp <= datetime.now(timezone.utc).timestamp() + buffer_s


def refresh_fingerprint(auth: dict) -> str | None:
    """Short hash of the refresh token — identifies a token generation without
    holding the token itself (bound to usage-store auth strikes)."""
    rt = _tokens(auth).get("refresh_token")
    if not isinstance(rt, str) or not rt:
        return None
    return hashlib.sha256(rt.encode("utf-8")).hexdigest()[:16]


def access_token_fingerprint(auth: dict) -> str | None:
    at = _tokens(auth).get("access_token")
    if not isinstance(at, str) or not at:
        return None
    return hashlib.sha256(at.encode("utf-8")).hexdigest()[:16]


# -- network ------------------------------------------------------------------


def refresh_tokens(refresh_token: str, *, timeout: float = 10) -> dict:
    """Exchange a refresh token for a new token set.

    Returns ``{"access_token", "refresh_token", "id_token"}`` (the server may
    omit ``refresh_token``, in which case the old one stays valid and is
    returned in its place). Raises :class:`CodexRefreshError`.
    """
    body = json.dumps(
        {
            "client_id": CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        kind = f"http-{e.code}"
        try:
            err = json.loads(e.read().decode("utf-8"))
            code = err.get("error") if isinstance(err, dict) else None
            if isinstance(code, dict):
                code = code.get("code") or code.get("type")
            if isinstance(code, str) and code:
                kind = code
        except Exception:  # noqa: BLE001 - body is optional diagnostics
            pass
        _logger.warning("Codex token refresh failed: %s", kind)
        raise CodexRefreshError(kind) from e
    except Exception as e:  # noqa: BLE001
        kind, _ = _classify_usage_error(e)
        _logger.warning("Codex token refresh failed: %s", kind)
        raise CodexRefreshError(kind) from e

    access = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(access, str) or not access:
        raise CodexRefreshError("bad-response", "Codex token refresh returned no access token")
    new_rt = data.get("refresh_token")
    return {
        "access_token": access,
        "refresh_token": new_rt if isinstance(new_rt, str) and new_rt else refresh_token,
        "id_token": data.get("id_token") if isinstance(data.get("id_token"), str) else None,
    }


def merged_after_refresh(auth: dict, tokens: dict) -> dict:
    """A copy of ``auth`` carrying the refreshed tokens (``id_token`` and
    ``account_id`` are kept when the response did not send new ones)."""
    merged = dict(auth)
    old = dict(_tokens(auth))
    old["access_token"] = tokens["access_token"]
    old["refresh_token"] = tokens["refresh_token"]
    if tokens.get("id_token"):
        old["id_token"] = tokens["id_token"]
    merged["tokens"] = old
    merged["last_refresh"] = (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    return merged


def request_usage(access_token: str, account_id: str, *, timeout: float = 5) -> dict:
    """Raw rate-limit payload for one ChatGPT account."""
    req = urllib.request.Request(
        CODEX_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# -- usage normalization ------------------------------------------------------


def _first(record: dict, *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _window_entry(window: object) -> tuple[dict, float | None] | None:
    """Normalize one rate-limit window into the ``{pct, resets_at, ...}``
    shape the Claude path uses, plus its length in seconds (when known)."""
    if not isinstance(window, dict):
        return None
    pct = _number(
        _first(window, "used_percent", "usedPercent", "usage_percent", "usagePercent")
    )
    if pct is None:
        return None
    entry: dict = {"pct": pct}
    reset = _first(window, "reset_at", "resets_at", "resetAt", "resetsAt")
    reset_ts = _number(reset)
    if reset_ts is None:
        after = _number(_first(window, "reset_after_seconds", "resetAfterSeconds"))
        if after is not None:
            reset_ts = datetime.now(timezone.utc).timestamp() + after
    if reset_ts is None and isinstance(reset, str):
        try:
            reset_ts = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
        except ValueError:
            reset_ts = None
    if reset_ts is not None:
        iso = datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
        entry["resets_at"] = iso
        entry["countdown"], entry["clock"] = format_reset(iso)
    length = _number(_first(window, "limit_window_seconds", "limitWindowSeconds"))
    if length is None:
        minutes = _number(
            _first(window, "window_duration_mins", "windowDurationMins", "window_minutes")
        )
        length = minutes * 60 if minutes is not None else None
    return entry, length


def build_usage_result(data: dict) -> dict | None:
    """Normalize the rate-limit payload into ``{"five_hour", "seven_day"}``.

    Windows are assigned to a lane by their length; when the length is not
    reported the primary window is the session lane and the secondary the
    weekly one. ``plan_type`` rides along when present. Returns ``None`` when
    no window carried a percentage.
    """
    _logger.debug("Codex usage response: %s", json.dumps(data, indent=2))
    if not isinstance(data, dict):
        return None
    payload = data
    if not any(k in payload for k in ("rate_limit", "rateLimit")) and isinstance(
        payload.get("data"), dict
    ):
        payload = payload["data"]
    rate_limit = _first(payload, "rate_limit", "rateLimit")
    if not isinstance(rate_limit, dict):
        return None

    result: dict = {}
    for keys, fallback in (
        (("primary_window", "primaryWindow", "primary"), "five_hour"),
        (("secondary_window", "secondaryWindow", "secondary"), "seven_day"),
    ):
        normalized = _window_entry(_first(rate_limit, *keys))
        if normalized is None:
            continue
        entry, length = normalized
        lane = fallback
        if length is not None:
            lane = "five_hour" if length <= _SESSION_WINDOW_MAX_S else "seven_day"
        if lane not in result:
            result[lane] = entry
    if not result:
        return None
    plan = _first(payload, "plan_type", "planType")
    if isinstance(plan, str) and plan:
        result["plan_type"] = plan
    return result


def fetch_usage(auth: dict) -> UsageOutcome:
    """Usage for the login in ``auth``; never raises."""
    tokens = _tokens(auth)
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return UsageOutcome(None, error="no-access-token")
    try:
        identity = identity_from_auth(auth)
    except CodexAuthError:
        return UsageOutcome(None, error="no-account-id")
    try:
        data = request_usage(access_token, identity.account_id)
    except Exception as e:  # noqa: BLE001
        kind, retry_after = _classify_usage_error(e)
        _logger.warning("Codex usage fetch failed: %s", kind)
        _logger.debug("Codex usage fetch failure detail: %r", e)
        return UsageOutcome(None, error=kind, retry_after_s=retry_after)
    return UsageOutcome(build_usage_result(data))
