# Security & Privacy

This fork handles OAuth credentials for two AI subscriptions, so its security
posture is documented explicitly. The upstream project's audit-friendly
structure is preserved: small modules, no obfuscation, ~1,000 unit tests.

## What the app touches

**Credentials (read):**

| File | Owner | Used for |
|---|---|---|
| `~/.claude/.credentials.json` | Claude Code CLI | Bearer token for the Anthropic usage/profile API (preferred when present) |
| `~/.codex/auth.json` | Codex CLI | Bearer token + account id for the ChatGPT usage API |
| `%APPDATA%/UsageMonitorForClaude/claude-oauth.dat` | this app | Optional "Sign in with Claude" app login for users without the CLI - OAuth tokens encrypted with Windows DPAPI (per-user), created only if you use that flow |

**App login (OAuth):** the setup window's *Sign in with Claude* runs the same
authorization-code + PKCE flow the Claude Code CLI uses, against Anthropic's
public client id, in your own browser - the app never sees your password.
The granted token's scope set is the client's fixed one
(`org:create_api_key user:profile user:inference`); this app only ever calls
the usage/profile **read** endpoints - it never creates API keys and never
runs inference. *Sign out of app login* in the setup window deletes the
stored tokens.

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
| `claude.ai/oauth/authorize` (system browser), `console.anthropic.com/v1/oauth/token` | optional app login + its token refresh |
| `chatgpt.com/backend-api/codex/usage` (fallback `/backend-api/wham/usage`) | Codex usage |
| `auth.openai.com/oauth/token` | Codex token refresh (only after a 401) |
| `api.anthropic.com/v1/messages` | Sprite Builder only: user-initiated, one small request per drawing, on the user's own subscription |
| `api.github.com/repos/.../releases/latest` + the release asset URLs | in-app update check (every 6h) and user-initiated update download |

**In-app updates:** the tray menu's update action downloads the newest
release EXE and installs it **only after its SHA256 matches the release's
`SHA256SUMS.txt`** - both artifacts come from the public CI workflow, so a
tampered download can never be installed. Nothing installs without you
clicking the menu item.

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

## Art credits

The visitor critters in `hud/visitors/` are from the [Dungeon Crawl Stone
Soup tiles](https://github.com/crawl/tiles) (originally RLTiles), released
by their artists under **CC0** (no attribution required - given gladly
anyway). The Codex companion spritesheet is OpenAI's own published pet
asset; Clawd is the MIT-licensed ClawdMoji recreation of the Claude Code
logo. No other third-party characters are bundled; anything users drop
into their local `visitors/` folder is their own responsibility.

## Reporting

Open a GitHub issue, or use GitHub's private vulnerability reporting on this
repository for anything sensitive.
