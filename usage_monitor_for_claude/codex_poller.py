"""
Codex Poller
=============

Background polling thread for the Codex (OpenAI) usage provider.

Deliberately independent of the Claude ``UsageCache``/``poll_loop`` pair:
Codex errors can never perturb Claude's backoff or reset alignment, and
vice versa.  The poller exposes the last snapshot (same quota-dict shape
as the Claude response) for the popup and HUD to render.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .codex_api import fetch_codex_usage, read_codex_tokens
from .settings import CODEX_POLL_INTERVAL, MAX_BACKOFF, POLL_ERROR

__all__ = ['CodexPoller']


class CodexPoller:
    """Polls the Codex usage API on a fixed cadence with error backoff."""

    def __init__(self, on_update: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.usage: dict[str, Any] = {}
        self.last_success_time: float | None = None
        self.running = False
        self._on_update = on_update
        self._thread: threading.Thread | None = None
        self._error_streak = 0
        # Access token that failed auth (incl. a failed refresh).  While the
        # file still carries it, fetches are skipped so the refresh endpoint
        # is not hammered; any new login (token change) clears the latch.
        self._failed_token: str | None = None

    def start(self) -> None:
        """Start the polling thread (idempotent)."""
        if self._thread is not None:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the polling thread to exit."""
        self.running = False

    def _poll_once(self) -> float:
        """Fetch one snapshot and return the seconds until the next poll."""
        tokens = read_codex_tokens()
        current_token = tokens.get('access_token') if tokens else None
        if self._failed_token is not None and current_token == self._failed_token:
            return float(CODEX_POLL_INTERVAL)

        result = fetch_codex_usage()
        self.usage = result
        self._failed_token = current_token if result.get('auth_error') else None

        if 'error' not in result:
            self.last_success_time = time.time()
            self._error_streak = 0
            interval = float(CODEX_POLL_INTERVAL)
        else:
            self._error_streak += 1
            backoff = POLL_ERROR * (2 ** (self._error_streak - 1))
            if result.get('rate_limited') and result.get('retry_after'):
                backoff = max(backoff, result['retry_after'])
            interval = float(min(backoff, MAX_BACKOFF))

        if self._on_update is not None:
            try:
                self._on_update(dict(result))
            except Exception:
                pass

        return interval

    def _loop(self) -> None:
        while self.running:
            interval = self._poll_once()
            target = time.time() + interval
            while self.running and time.time() < target:
                time.sleep(1)
