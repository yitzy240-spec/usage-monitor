"""
Claude App Login (OAuth)
=========================

Lets claude.ai chat / Desktop / Cowork subscribers use the monitor without
installing Claude Code: the same OAuth authorization-code + PKCE flow the
Claude Code CLI performs, against Anthropic's public client id. The browser
opens Anthropic's login page; the user pastes back the short code shown
after approving; the app exchanges it for tokens.

Tokens are stored ONLY on this machine, encrypted with Windows DPAPI
(per-user), at ``%APPDATA%/UsageMonitorForClaude/claude-oauth.dat``. They
are sent exclusively as Authorization headers to ``api.anthropic.com`` /
``console.anthropic.com``. When a Claude Code login exists it is preferred
and this store is not consulted (see ``api.read_access_token``).

The flow is the community-established one (identical parameters to the
CLI's own login); Anthropic may change it - failures degrade to the
signed-out state, never to a crash.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

__all__ = ['generate_login', 'exchange_code', 'get_access_token', 'has_app_login', 'sign_out']

# Anthropic's public OAuth client (the one Claude Code ships with).
CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
AUTHORIZE_URL = 'https://claude.ai/oauth/authorize'
TOKEN_URL = 'https://console.anthropic.com/v1/oauth/token'
REDIRECT_URI = 'https://console.anthropic.com/oauth/code/callback'
# The client's standard scope set (deviating from it is rejected). This app
# only ever calls the usage/profile read endpoints - it never creates API
# keys and never runs inference.
SCOPES = 'org:create_api_key user:profile user:inference'

STORE_DIR = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming')) / 'UsageMonitorForClaude'
STORE_PATH = STORE_DIR / 'claude-oauth.dat'

# Refresh this many seconds before nominal expiry.
_EXPIRY_MARGIN = 120

_lock = threading.Lock()
# get_access_token is polled every second (token-change watch); cache the
# decrypted tokens keyed by file mtime so DPAPI runs only on actual change.
_cache_mtime: float | None = None
_cache_tokens: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

def generate_login() -> tuple[str, str]:
    """Return (authorize_url, pkce_verifier) for a fresh login attempt."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    params = {
        'code': 'true',
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPES,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': verifier,
    }
    return f'{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}', verifier


def exchange_code(pasted: str, verifier: str) -> str | None:
    """Exchange the pasted ``code#state`` for tokens. Returns an error string or None."""
    code, _, state = pasted.strip().partition('#')
    if not code:
        return 'empty code'

    try:
        resp = requests.post(
            TOKEN_URL,
            json={
                'grant_type': 'authorization_code',
                'code': code,
                'state': state or verifier,
                'client_id': CLIENT_ID,
                'redirect_uri': REDIRECT_URI,
                'code_verifier': verifier,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return f'network error: {exc.__class__.__name__}'

    if resp.status_code != 200:
        return f'login rejected (HTTP {resp.status_code})'

    try:
        payload = resp.json()
    except ValueError:
        return 'unexpected response'

    access_token = payload.get('access_token')
    if not access_token:
        return 'no token in response'

    with _lock:
        _save({
            'access_token': access_token,
            'refresh_token': payload.get('refresh_token'),
            'expires_at': time.time() + float(payload.get('expires_in') or 3600),
        })
    return None


def get_access_token() -> str | None:
    """Current app-login access token, refreshed when near expiry, or None."""
    with _lock:
        tokens = _load()
        if not tokens:
            return None
        if time.time() > float(tokens.get('expires_at') or 0) - _EXPIRY_MARGIN:
            tokens = _refresh(tokens)
        return tokens.get('access_token') if tokens else None


def has_app_login() -> bool:
    return STORE_PATH.exists()


def sign_out() -> None:
    with _lock:
        try:
            STORE_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def _refresh(tokens: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh-token grant; persists and returns the new set, or None."""
    refresh_token = tokens.get('refresh_token')
    if not refresh_token:
        return None

    try:
        resp = requests.post(
            TOKEN_URL,
            json={'grant_type': 'refresh_token', 'refresh_token': refresh_token, 'client_id': CLIENT_ID},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None

    access_token = payload.get('access_token')
    if not access_token:
        return None

    new_tokens = {
        'access_token': access_token,
        'refresh_token': payload.get('refresh_token') or refresh_token,
        'expires_at': time.time() + float(payload.get('expires_in') or 3600),
    }
    _save(new_tokens)
    return new_tokens


# ---------------------------------------------------------------------------
# DPAPI-encrypted storage (ctypes, fully typed - 64-bit handle lessons apply)
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', ctypes.wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt32() -> tuple[Any, Any]:
    crypt32 = ctypes.WinDLL('crypt32')
    kernel32 = ctypes.WinDLL('kernel32')
    for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
        fn.argtypes = [
            ctypes.POINTER(_DATA_BLOB), ctypes.wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
        ]
        fn.restype = ctypes.wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    crypt32, kernel32 = _crypt32()
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data) if data else (ctypes.c_ubyte * 1)()
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    blob_out = _DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _save(tokens: dict[str, Any]) -> None:
    global _cache_mtime, _cache_tokens
    encrypted = _dpapi(json.dumps(tokens).encode('utf-8'), protect=True)
    if encrypted is None:
        return
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_name(STORE_PATH.name + '.tmp')
        tmp.write_bytes(encrypted)
        os.replace(tmp, STORE_PATH)
        _cache_mtime = STORE_PATH.stat().st_mtime
        _cache_tokens = tokens
    except OSError:
        pass


def _load() -> dict[str, Any] | None:
    global _cache_mtime, _cache_tokens
    try:
        mtime = STORE_PATH.stat().st_mtime
    except OSError:
        _cache_mtime = _cache_tokens = None
        return None

    if _cache_mtime == mtime and _cache_tokens is not None:
        return _cache_tokens

    try:
        encrypted = STORE_PATH.read_bytes()
    except OSError:
        return None
    decrypted = _dpapi(encrypted, protect=False)
    if decrypted is None:
        return None
    try:
        tokens = json.loads(decrypted)
    except ValueError:
        return None
    if not (isinstance(tokens, dict) and tokens.get('access_token')):
        return None
    _cache_mtime, _cache_tokens = mtime, tokens
    return tokens
