"""Codex login helpers: JWT identity, expiry, refresh and usage parsing."""

from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.codex import auth
from claude_swap.codex.auth import (
    CodexAuthError,
    CodexRefreshError,
    access_token_expired,
    auth_mode,
    build_usage_result,
    decode_jwt_payload,
    fetch_usage,
    identity_from_auth,
    merged_after_refresh,
    refresh_fingerprint,
    refresh_tokens,
)
from tests.conftest import make_codex_auth, make_id_token


def _resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int, body: dict | None = None, headers: dict | None = None):
    return urllib.error.HTTPError(
        "https://x", code, "err", headers or {}, io.BytesIO(json.dumps(body or {}).encode())
    )


class TestJwt:
    def test_decodes_claims(self):
        claims = decode_jwt_payload(make_id_token("a@b.c", "acct_9", "pro"))
        assert claims["email"] == "a@b.c"
        assert claims["https://api.openai.com/auth"]["chatgpt_account_id"] == "acct_9"

    @pytest.mark.parametrize("token", ["", "abc", "a.b", "a..c", "a.!!!.c", "a.e30.c.d"])
    def test_rejects_malformed(self, token):
        with pytest.raises(ValueError):
            decode_jwt_payload(token)

    def test_rejects_non_object_payload(self):
        import base64

        seg = base64.urlsafe_b64encode(b"[1]").decode().rstrip("=")
        with pytest.raises(ValueError):
            decode_jwt_payload(f"h.{seg}.s")


class TestIdentity:
    def test_from_claims(self):
        ident = identity_from_auth(make_codex_auth("a@b.c", "acct_9", "pro"))
        assert (ident.email, ident.account_id, ident.plan_type) == ("a@b.c", "acct_9", "pro")

    def test_account_id_falls_back_to_tokens(self):
        a = make_codex_auth()
        a["tokens"]["id_token"] = make_id_token(email="x@y.z", account_id="")
        a["tokens"]["account_id"] = "acct_fallback"
        assert identity_from_auth(a).account_id == "acct_fallback"

    def test_missing_email_is_an_error(self):
        a = make_codex_auth()
        a["tokens"]["id_token"] = make_id_token(email="")
        with pytest.raises(CodexAuthError, match="email"):
            identity_from_auth(a)

    def test_unreadable_id_token_is_an_error(self):
        a = make_codex_auth()
        a["tokens"]["id_token"] = "garbage"
        with pytest.raises(CodexAuthError, match="id_token"):
            identity_from_auth(a)

    def test_auth_mode(self):
        assert auth_mode(make_codex_auth()) == "chatgpt"
        assert auth_mode(make_codex_auth(mode="apikey")) == "apikey"
        legacy = make_codex_auth()
        del legacy["auth_mode"]
        assert auth_mode(legacy) == "chatgpt"
        assert auth_mode({}) == ""


class TestExpiry:
    def test_future_token_is_live(self):
        assert access_token_expired(make_codex_auth()) is False

    def test_past_token_is_expired(self):
        assert access_token_expired(make_codex_auth(expired=True)) is True

    def test_buffer(self):
        a = make_codex_auth()
        a["tokens"]["access_token"] = make_id_token(exp=time.time() + 100)
        assert access_token_expired(a, buffer_s=300) is True
        assert access_token_expired(a, buffer_s=10) is False

    def test_unparseable_token_is_treated_as_live(self):
        a = make_codex_auth()
        a["tokens"]["access_token"] = "opaque-token"
        assert access_token_expired(a) is False

    def test_missing_token_is_expired(self):
        a = make_codex_auth()
        a["tokens"]["access_token"] = ""
        assert access_token_expired(a) is True

    def test_fingerprint_tracks_refresh_token(self):
        a = make_codex_auth(refresh_token="rt-a")
        b = make_codex_auth(refresh_token="rt-b")
        assert refresh_fingerprint(a) != refresh_fingerprint(b)
        assert refresh_fingerprint({"tokens": {}}) is None


@pytest.mark.no_codex_network_fake
class TestRefresh:
    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_posts_refresh_grant_and_returns_tokens(self, mock_urlopen):
        mock_urlopen.return_value = _resp(
            {"access_token": "at-2", "refresh_token": "rt-2", "id_token": "id-2"}
        )
        tokens = refresh_tokens("rt-1")
        assert tokens == {"access_token": "at-2", "refresh_token": "rt-2", "id_token": "id-2"}
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == auth.OPENAI_TOKEN_URL
        body = json.loads(req.data.decode())
        assert body == {
            "client_id": auth.CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "rt-1",
        }

    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_keeps_old_refresh_token_when_not_rotated(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"access_token": "at-2"})
        tokens = refresh_tokens("rt-1")
        assert tokens["refresh_token"] == "rt-1"
        assert tokens["id_token"] is None

    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_invalid_grant_kind(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(400, {"error": "invalid_grant"})
        with pytest.raises(CodexRefreshError) as exc:
            refresh_tokens("rt-dead")
        assert exc.value.kind == "invalid_grant"

    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_http_error_without_code_uses_status(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(503, {})
        with pytest.raises(CodexRefreshError) as exc:
            refresh_tokens("rt-1")
        assert exc.value.kind == "http-503"

    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_network_error_kind(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("down")
        with pytest.raises(CodexRefreshError) as exc:
            refresh_tokens("rt-1")
        assert exc.value.kind == "network"

    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_missing_access_token_is_bad_response(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"refresh_token": "rt-2"})
        with pytest.raises(CodexRefreshError) as exc:
            refresh_tokens("rt-1")
        assert exc.value.kind == "bad-response"

    def test_merged_after_refresh(self):
        a = make_codex_auth(refresh_token="rt-1")
        merged = merged_after_refresh(
            a, {"access_token": "at-2", "refresh_token": "rt-2", "id_token": None}
        )
        assert merged["tokens"]["access_token"] == "at-2"
        assert merged["tokens"]["refresh_token"] == "rt-2"
        assert merged["tokens"]["id_token"] == a["tokens"]["id_token"]
        assert merged["tokens"]["account_id"] == "acct_123"
        assert merged["last_refresh"].endswith("Z")
        assert a["tokens"]["access_token"] != "at-2"  # input untouched


SNAKE = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {"used_percent": 34, "limit_window_seconds": 18000, "reset_at": 1778091218},
        "secondary_window": {"used_percent": 37, "limit_window_seconds": 604800, "reset_at": 1778605571},
    },
    "additional_rate_limits": [],
}


class TestBuildUsageResult:
    def test_snake_case_payload(self):
        result = build_usage_result(SNAKE)
        assert result["five_hour"]["pct"] == 34
        assert result["seven_day"]["pct"] == 37
        assert result["plan_type"] == "plus"
        assert result["five_hour"]["resets_at"] == datetime.fromtimestamp(
            1778091218, tz=timezone.utc
        ).isoformat()
        assert "countdown" in result["five_hour"] and "clock" in result["seven_day"]

    def test_camel_case_payload(self):
        data = {"rateLimit": {"primaryWindow": {"usedPercent": 10, "resetsAt": 1778103354},
                              "secondaryWindow": {"usedPercent": 20}}}
        result = build_usage_result(data)
        assert result["five_hour"]["pct"] == 10
        assert result["seven_day"]["pct"] == 20
        assert "resets_at" not in result["seven_day"]

    def test_weekly_only_primary_is_classified_by_length(self):
        data = {"rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": 604800}}}
        result = build_usage_result(data)
        assert "five_hour" not in result
        assert result["seven_day"]["pct"] == 5

    def test_missing_windows_yield_none(self):
        assert build_usage_result({"rate_limit": {}}) is None
        assert build_usage_result({}) is None
        assert build_usage_result({"rate_limit": {"primary_window": {"limit_window_seconds": 1}}}) is None

    def test_reset_after_seconds(self):
        data = {"rate_limit": {"primary_window": {"used_percent": 1, "reset_after_seconds": 60}}}
        result = build_usage_result(data)
        assert "resets_at" in result["five_hour"]

    def test_nested_data_envelope(self):
        assert build_usage_result({"data": SNAKE})["five_hour"]["pct"] == 34


@pytest.mark.no_codex_network_fake
class TestFetchUsage:
    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_sends_bearer_and_account_headers(self, mock_urlopen):
        mock_urlopen.return_value = _resp(SNAKE)
        outcome = fetch_usage(make_codex_auth(account_id="acct_77"))
        assert outcome.error is None
        assert outcome.usage["five_hour"]["pct"] == 34
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == auth.CODEX_USAGE_URL
        assert req.get_header("Chatgpt-account-id") == "acct_77"
        assert req.get_header("Authorization").startswith("Bearer ")

    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_401(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401)
        assert fetch_usage(make_codex_auth()).error == "http-401"

    @patch("claude_swap.codex.auth.urllib.request.urlopen")
    def test_429_retry_after(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(429, headers={"Retry-After": "30"})
        outcome = fetch_usage(make_codex_auth())
        assert outcome.error == "http-429"
        assert outcome.retry_after_s == 30.0

    def test_no_access_token(self):
        a = make_codex_auth()
        a["tokens"]["access_token"] = ""
        assert fetch_usage(a).error == "no-access-token"
