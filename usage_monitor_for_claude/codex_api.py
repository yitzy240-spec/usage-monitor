"""
Codex API Client
=================

Reads OpenAI Codex CLI OAuth credentials and communicates with the
ChatGPT backend usage API.  This is the only module that handles Codex
credentials.

Network communication exclusively with ``chatgpt.com`` (usage) and
``auth.openai.com`` (token refresh).  Credentials used only in HTTP
Authorization headers, except the atomic write-back of rotated tokens
to ``auth.json`` — skipping that write-back would burn the single-use
refresh token and break the user's ``codex`` CLI login.

The fetched windows are mapped onto the same quota-dict shape the
Claude client emits (``{'five_hour': {'utilization', 'resets_at'}}``),
so all field-name-driven formatting and rendering works unchanged.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .api import _model_slug, _parse_retry_after
from .i18n import T

__all__ = ['API_URL_USAGE', 'API_URL_USAGE_FALLBACK', 'API_URL_TOKEN', 'CODEX_HOME', 'CODEX_AUTH', 'read_codex_tokens', 'fetch_codex_usage']

# API endpoints & credentials
API_URL_USAGE = 'https://chatgpt.com/backend-api/codex/usage'
API_URL_USAGE_FALLBACK = 'https://chatgpt.com/backend-api/wham/usage'
API_URL_TOKEN = 'https://auth.openai.com/oauth/token'
CODEX_HOME = Path(os.environ.get('CODEX_HOME', '')) if os.environ.get('CODEX_HOME') else Path.home() / '.codex'
CODEX_AUTH = CODEX_HOME / 'auth.json'

# OAuth client id the Codex CLI itself uses for refresh-token grants.
_REFRESH_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
# The usage endpoint is Cloudflare-fronted and intermittently answers HTML
# 403s to non-browser clients; retry on this ladder before giving up.
_RETRY_DELAYS = (0, 0.25, 0.5, 0.75, 1, 1.5, 2)
_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0'
# limit_window_seconds -> upstream quota field name (±10% tolerance)
_WINDOW_FIELDS = {18000: 'five_hour', 86400: 'one_day', 604800: 'seven_day', 2592000: 'thirty_day'}


def read_codex_tokens() -> dict[str, Any] | None:
    """Read the current token set from the Codex CLI auth file."""
    if not CODEX_AUTH.exists():
        return None

    try:
        auth = json.loads(CODEX_AUTH.read_text(encoding='utf-8'))
        tokens = auth.get('tokens') if isinstance(auth, dict) else None
        if isinstance(tokens, dict) and tokens.get('access_token'):
            return tokens
        return None
    except (OSError, ValueError):
        # OSError also covers a read racing the Codex CLI rewriting the file
        # on its own token rotation; treat it as "no token right now".
        return None


def fetch_codex_usage() -> dict[str, Any]:
    """Fetch usage data from the ChatGPT backend usage API.

    On an auth failure the file is re-read first — the Codex CLI may have
    rotated the tokens itself — and only then is a refresh-token grant
    attempted (with atomic write-back, since refresh tokens are single-use).
    """
    tokens = read_codex_tokens()
    if not tokens:
        return {'error': T['no_token'], 'auth_error': True}

    result = _request_usage(tokens)
    if not result.get('auth_error'):
        return result

    fresh = read_codex_tokens()
    if fresh and fresh.get('access_token') != tokens.get('access_token'):
        retry = _request_usage(fresh)
        if not retry.get('auth_error'):
            return retry
        tokens = fresh

    refreshed = _refresh_tokens(tokens)
    if refreshed:
        retry = _request_usage(refreshed)
        if not retry.get('auth_error'):
            return retry

    return {'error': T['auth_expired'], 'auth_error': True}


# Helpers


def _codex_headers(tokens: dict[str, Any]) -> dict[str, str]:
    """Return auth headers for the ChatGPT backend usage API."""
    headers = {
        'Authorization': f'Bearer {tokens["access_token"]}',
        'Accept': 'application/json',
        'User-Agent': _USER_AGENT,
    }
    if tokens.get('account_id'):
        headers['chatgpt-account-id'] = tokens['account_id']
    return headers


def _request_usage(tokens: dict[str, Any]) -> dict[str, Any]:
    """Perform the usage GET with the 403 retry ladder and URL fallback."""
    headers = _codex_headers(tokens)

    try:
        resp = None
        for delay in _RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            resp = requests.get(API_URL_USAGE, headers=headers, timeout=10)
            if resp.status_code == 404:
                resp = requests.get(API_URL_USAGE_FALLBACK, headers=headers, timeout=10)
            if not (resp.status_code == 403 and not _is_json(resp)):
                break

        if resp is None:
            return {'error': T['codex_connection_error']}
        if resp.status_code == 200:
            return _map_usage(resp.json())
        if resp.status_code == 401:
            return {'error': T['auth_expired'], 'auth_error': True}
        if resp.status_code == 429:
            retry = _parse_retry_after(resp)
            extra: dict[str, Any] = {'retry_after': retry} if retry is not None else {}
            return {**extra, 'error': T['http_error'].format(code=429), 'rate_limited': True}
        if 500 <= resp.status_code < 600:
            # Codex-specific wording: the shared string blames the Anthropic
            # API, which reads as OUR bug when chatgpt.com has an outage.
            return {'error': T['codex_server_error'].format(code=resp.status_code)}
        return {'error': T['http_error'].format(code=resp.status_code)}
    except requests.ConnectionError:
        return {'error': T['codex_connection_error']}
    except Exception:
        return {'error': T['codex_connection_error']}


def _is_json(resp: Any) -> bool:
    """True when the response body parses as JSON (vs. a Cloudflare HTML page)."""
    try:
        resp.json()
        return True
    except Exception:
        return False


def _classify_window(seconds: Any) -> str | None:
    """Map a rate-limit window duration onto an upstream quota field name."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None

    for base, field in _WINDOW_FIELDS.items():
        if abs(seconds - base) <= base * 0.1:
            return field
    return None


def _window_to_quota(window: dict[str, Any]) -> dict[str, Any]:
    """Convert one rate-limit window into the upstream quota-field shape."""
    reset_at = window.get('reset_at')
    resets_at = datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat() if isinstance(reset_at, (int, float)) else None
    return {'utilization': float(window.get('used_percent') or 0), 'resets_at': resets_at}


def _map_usage(data: dict[str, Any]) -> dict[str, Any]:
    """Map a usage response onto the upstream quota-dict shape.

    ``primary_window``/``secondary_window`` are classified by duration
    (5h → ``five_hour``, 7d → ``seven_day``, …) rather than position, and
    ``additional_rate_limits`` become variant fields (``five_hour_spark``)
    mirroring the Claude client's synthetic scoped-limit fields.
    """
    result: dict[str, Any] = {}

    rate_limit = data.get('rate_limit') or {}
    for key in ('primary_window', 'secondary_window'):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        field = _classify_window(window.get('limit_window_seconds'))
        if field and field not in result:
            result[field] = _window_to_quota(window)

    for extra in data.get('additional_rate_limits') or []:
        if not isinstance(extra, dict):
            continue
        window = extra.get('rate_limit')
        name = extra.get('limit_name')
        if not isinstance(window, dict) or not name:
            continue
        field = _classify_window(window.get('limit_window_seconds'))
        if field:
            result[f'{field}_{_model_slug(str(name))}'] = _window_to_quota(window)

    if data.get('plan_type'):
        result['plan_type'] = data['plan_type']

    return result


def _refresh_tokens(tokens: dict[str, Any]) -> dict[str, Any] | None:
    """Exchange the refresh token for new tokens and write them back.

    Returns the new token set, or None on failure (auth.json untouched).
    """
    if not tokens.get('refresh_token'):
        return None

    try:
        resp = requests.post(
            API_URL_TOKEN,
            json={
                'client_id': _REFRESH_CLIENT_ID,
                'grant_type': 'refresh_token',
                'refresh_token': tokens['refresh_token'],
                'scope': 'openid profile email',
            },
            headers={'User-Agent': _USER_AGENT},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None

    access_token = payload.get('access_token')
    if not access_token:
        return None

    new_tokens = dict(tokens)
    new_tokens['access_token'] = access_token
    for key in ('refresh_token', 'id_token'):
        if payload.get(key):
            new_tokens[key] = payload[key]

    _write_back_tokens(new_tokens)
    return new_tokens


def _write_back_tokens(tokens: dict[str, Any]) -> None:
    """Atomically persist rotated tokens to auth.json, preserving its schema."""
    try:
        data = json.loads(CODEX_AUTH.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}

    stored = dict(data.get('tokens') or {})
    for key in ('id_token', 'access_token', 'refresh_token', 'account_id'):
        if tokens.get(key) is not None:
            stored[key] = tokens[key]
    data['tokens'] = stored
    data['last_refresh'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    try:
        tmp_path = CODEX_AUTH.with_name(CODEX_AUTH.name + '.tmp')
        tmp_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        os.replace(tmp_path, CODEX_AUTH)
    except OSError:
        pass
