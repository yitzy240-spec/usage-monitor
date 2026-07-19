# Security & Privacy

This fork handles OAuth credentials for two AI subscriptions, so its security
posture is documented explicitly. The upstream project's audit-friendly
structure is preserved: small modules, no obfuscation, ~1,000 unit tests.

## What the app touches

**Credentials (read):**

| File | Owner | Used for |
|---|---|---|
| `~/.claude/.credentials.json` | Claude Code CLI | Bearer token for the Anthropic usage/profile API |
| `~/.codex/auth.json` | Codex CLI | Bearer token + account id for the ChatGPT usage API |

**Credentials (write):** only `~/.codex/auth.json`, and only when a Codex
token refresh succeeds. OpenAI refresh tokens are single-use (rotating);
writing the rotated pair back atomically (`codex_api._write_back_tokens`,
temp file + `os.replace`) is what keeps the user's own `codex` CLI login
working. The file's schema and unrelated keys are preserved. Claude
credentials are never written.

**Local data (read-only, never leaves the machine):** Claude Code session
transcripts under `~/.claude/projects/` are tail-read to compute
context-window fill (`claude_sessions.py`). Only token *counts* and the
project folder name are used; message content is never parsed further,
stored, or transmitted.

## Network endpoints — the complete list

| Endpoint | Purpose |
|---|---|
| `api.anthropic.com/api/oauth/usage`, `/api/oauth/profile` | Claude usage + account info |
| `chatgpt.com/backend-api/codex/usage` (fallback `/backend-api/wham/usage`) | Codex usage |
| `auth.openai.com/oauth/token` | Codex token refresh (only after a 401) |
| `github.com` (release page, opened in the system browser) | manual update check via the tray menu |

There is no telemetry, no analytics, no crash reporting, and no other
network traffic. Tokens are sent only as `Authorization` headers to the
provider that issued them. The HUD/setup WebView pages carry a
`Content-Security-Policy` that forbids loading any remote content.

## Releases: unsigned, but verifiable

Release EXEs are **not code-signed** (no certificate), so SmartScreen shows
its generic warning on first run. Instead of a signature, every release from
`fork-v1.0.3` on is built **from this public source by GitHub Actions** and
ships with cryptographic build provenance:

```
# proves the EXE was built by this repo's public workflow, unmodified:
gh attestation verify UsageMonitorForClaude.exe --owner yitzy240-spec

# or just compare hashes with the attached SHA256SUMS.txt:
certutil -hashfile UsageMonitorForClaude.exe SHA256
```

Dependencies are pinned (`requirements.txt` + full `requirements-lock.txt`
used by CI) and watched by Dependabot.

## Reporting

Open a GitHub issue, or use GitHub's private vulnerability reporting on this
repository for anything sensitive.
