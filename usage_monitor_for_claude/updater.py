"""
In-App Updater
===============

Checks this fork's GitHub releases and installs updates on request.

Trust model: an update is only ever applied after its SHA256 matches the
``SHA256SUMS.txt`` published by the same release - both produced by the
public CI workflow (see SECURITY.md), so a tampered download can never be
installed. The swap uses the Windows rename trick: a running EXE's file can
be renamed, so the current binary is moved aside, the verified new one takes
its path, and the app restarts through the existing restart machinery
(``sys.executable`` still points at the original path, which is now the new
version). The leftover ``.old`` file is cleaned up on the next start.

Only active in frozen (packaged) builds; source checkouts update via git.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import requests

__all__ = ['FORK_VERSION', 'check_for_update', 'download_and_apply', 'cleanup_old_exe', 'watch_updates']

# The fork's own version track (upstream keeps __version__). The release
# workflow refuses to build a tag that does not match this constant, so it
# cannot silently drift.
FORK_VERSION = '1.7.1'

RELEASES_API = 'https://api.github.com/repos/yitzy240-spec/usage-monitor/releases/latest'
_EXE_NAME = 'UsageMonitorForClaude.exe'
_SUMS_NAME = 'SHA256SUMS.txt'
_CHECK_INTERVAL = 6 * 3600
_FIRST_CHECK_DELAY = 90
_HEADERS = {'Accept': 'application/vnd.github+json', 'User-Agent': f'usage-monitor-fork/{FORK_VERSION}'}


def _parse_tag(tag: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r'fork-v(\d+(?:\.\d+)*)', tag or '')
    return tuple(int(part) for part in match.group(1).split('.')) if match else None


def _is_newer(tag: str, current: str = FORK_VERSION) -> bool:
    remote = _parse_tag(tag)
    local = tuple(int(part) for part in current.split('.'))
    return remote is not None and remote > local


def check_for_update() -> dict[str, Any] | None:
    """Return {'version', 'tag', 'exe_url', 'sums_url'} when a newer release exists."""
    try:
        resp = requests.get(RELEASES_API, headers=_HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        release = resp.json()
    except Exception:
        return None

    tag = release.get('tag_name') or ''
    if not _is_newer(tag):
        return None

    urls = {asset.get('name'): asset.get('browser_download_url') for asset in release.get('assets') or []}
    if not urls.get(_EXE_NAME) or not urls.get(_SUMS_NAME):
        return None

    return {
        'version': tag.removeprefix('fork-v'),
        'tag': tag,
        'exe_url': urls[_EXE_NAME],
        'sums_url': urls[_SUMS_NAME],
    }


def _expected_hash(sums_text: str, name: str = _EXE_NAME) -> str | None:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].strip('*') == name:
            return parts[0].lower()
    return None


def download_and_apply(release: dict[str, Any]) -> str | None:
    """Download, verify, and swap in the release. Returns an error string or None.

    On success the new binary sits at the current executable's path; the
    caller restarts the app to run it.
    """
    if not getattr(sys, 'frozen', False):
        return 'updates apply to the packaged EXE only (source checkouts use git)'

    try:
        sums = requests.get(release['sums_url'], headers=_HEADERS, timeout=30).text
        expected = _expected_hash(sums)
        if not expected:
            return 'release checksums are malformed'

        with tempfile.NamedTemporaryFile(delete=False, suffix='.exe') as tmp:
            tmp_path = Path(tmp.name)
            digest = hashlib.sha256()
            with requests.get(release['exe_url'], headers=_HEADERS, timeout=300, stream=True) as resp:
                if resp.status_code != 200:
                    return f'download failed (HTTP {resp.status_code})'
                for chunk in resp.iter_content(1024 * 256):
                    tmp.write(chunk)
                    digest.update(chunk)

        if digest.hexdigest().lower() != expected:
            tmp_path.unlink(missing_ok=True)
            return 'checksum mismatch - download rejected'
    except requests.RequestException as exc:
        return f'network error: {exc.__class__.__name__}'
    except OSError as exc:
        return f'could not stage the download: {exc}'

    current = Path(sys.executable)
    old = current.with_name(current.name + '.old')
    try:
        old.unlink(missing_ok=True)
        current.rename(old)          # a running EXE's file may be renamed
        shutil.move(str(tmp_path), str(current))
    except OSError as exc:
        # Roll back so the app keeps working from its original path.
        try:
            if not current.exists() and old.exists():
                old.rename(current)
        except OSError:
            pass
        return f'could not swap the executable: {exc}'

    return None


def cleanup_old_exe() -> None:
    """Best-effort removal of the previous version left by an update."""
    if not getattr(sys, 'frozen', False):
        return
    try:
        Path(sys.executable).with_name(Path(sys.executable).name + '.old').unlink(missing_ok=True)
    except OSError:
        pass  # still locked by the exiting old process; next start gets it


def watch_updates(is_running: Callable[[], bool], on_available: Callable[[dict[str, Any]], None]) -> None:
    """Daemon loop: periodic update checks while the app runs (frozen only)."""
    if not getattr(sys, 'frozen', False):
        return

    waited = 0.0
    while is_running() and waited < _FIRST_CHECK_DELAY:
        time.sleep(1)
        waited += 1

    while is_running():
        release = check_for_update()
        if release is not None:
            on_available(release)
            return  # one notification per app run is enough
        waited = 0.0
        while is_running() and waited < _CHECK_INTERVAL:
            time.sleep(1)
            waited += 1
