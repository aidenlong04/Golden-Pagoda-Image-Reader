# Golden Pagoda — Project Instructions

Discord bot ("**Oda Helper**") that OCRs Warframe profile screenshots, verifies platform + clan, and assigns roles. The maintainer agent for this repo is **Oda Assistant** (see `.github/agents/oda-assistant.agent.md`).

## Stack

- **Runtime**: Python 3.12, `discord.py` 2.x, OCR.space (engine 3) with Tesseract fallback.
- **Entrypoint**: `bot.py` (single file, Discord client + slash commands + V2 component helpers). Pure logic in `logic.py`. Analytics in `analytics.py` (stdlib `sqlite3`, fail-soft). Tests in `tests/`.
- **Container**: `Dockerfile` produces `golden-pagoda:latest` image. Runs as non-root `bot` user (uid `10001`).
- **Hosting**: Hetzner CX22, Ubuntu 24.04, Docker + systemd.

## Deployment

- **Auto-deploy**: Push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) → rsync (excludes `data/`, `icons/`, `.env`) → `docker build` → `systemctl restart golden-pagoda`.
- **Server**: `5.78.211.130`, SSH user `nomekui` (NOPASSWD sudo, in `docker` group). Root login + password auth disabled.
- **SSH keys** (in codespace): `~/.ssh/hetzner` (interactive), `~/.ssh/gha_deploy` (workflow).
- **GitHub secrets**: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY` (base64-encoded private key — workflow `base64 -d`s it).
- **systemd unit**: `scripts/golden-pagoda.service` → `/etc/systemd/system/golden-pagoda.service`. Hardened: `--init`, `--memory=512m`, `--cpus=1`, `--pids-limit=256`, json-file log rotation (10m × 3 files), `SuccessExitStatus=0 137 143`.
- **Watchdog**: `scripts/golden-pagoda-watchdog.{sh,service}` is a long-running `simple` systemd service that blocks on `docker events --filter container=golden-pagoda --filter event=health_status --filter event=die` and restarts `golden-pagoda.service` on `health_status: unhealthy` or container death. Cooldown (60s) + sliding-window cap (5 restarts / 10 min) prevent thrash. Idle CPU/RAM ~zero.
- **Server `.env`**: `/opt/golden-pagoda/.env` (NOT in git — gitignored). The repo's local `.env` is excluded from the deploy rsync, so server is the source of truth. To update a key:
    - `scripts/ops.sh env-get KEY [KEY...]` — print current value(s) on server.
    - `scripts/ops.sh env-set KEY "VALUE"` — replace in place (or append if missing) and restart `golden-pagoda.service`.
    - Quote `VALUE` if it contains spaces, `<`, `>`, or emoji literals (e.g. `<:Name:123>`).
    - Manual fallback: `sudo $EDITOR /opt/golden-pagoda/.env && sudo systemctl restart golden-pagoda`.
- **Data volume**: `/opt/golden-pagoda/data/` ↔ container `/app/data/`. Owned `10001:10001` (workflow + `ExecStartPre` chown it idempotently).
- **Logs**: `sudo docker logs --tail 50 golden-pagoda` or `sudo journalctl -u golden-pagoda -f`.

## Configuration (`.env`)

- `DISCORD_TOKEN`, `TARGET_CHANNEL_ID` — required.
- `OCR_API_KEY` — OCR.space key (engine 3); falls back to local Tesseract if empty.
- `PLATFORM_ROLE_<PC|XBOX|PLAYSTATION|SWITCH|MOBILE>_ID` — auto-resolved from role name on connect, written back.
- `CLAN_ROLE_{1..7}_NAME/_ID/_EMOJI` — 7 clan slots; names auto-resolve to IDs on connect.
- `PLATFORM_EMOJI_<PC|XBOX|PLAYSTATION|SWITCH|MOBILE>` — custom Discord emojis (`<:name:id>` format).
- `INCOMPLETE_ROLE_ID`, `VERIFY_REMOVE_ROLE_ID`, `OUTREACH_ROLE_IDS` — verification flow roles.
- `PASS_REACTION_ID/_NAME`, `FAIL_REACTION`, `PENDING_REACTION_ID/_NAME` — reactions on the original screenshot.
- `REPLY_TTL_SECONDS` — auto-delete bot replies after N seconds.
- `HEALTH_PATH` (default `/tmp/gp_health`), `HEALTH_INTERVAL` (default `20` seconds) — liveness signal.
- `ANALYTICS_DB_PATH` (default `/app/data/analytics.db`) — SQLite path inside the container.
- Never commit `.env`. Update server-side env, then restart the service.

## Slash Commands

- `/clan-emblems role:<role> emoji:<:name:id>` — set per-clan emoji at runtime. Updates in-memory `CLAN_SLOTS`, `os.environ`, AND rewrites `/opt/golden-pagoda/.env`. Requires Manage Server perm.
- `/preview-responses` — post sample pass/fail/incomplete V2 messages to the preview channel.
- `/status` — single ephemeral V2 paginated message with 8 pages (bot, roles, channels, misc, stats, platforms, clans, ocr). Prev/Next/Refresh buttons walk every page. Requires Manage Server perm. The "Bot" page surfaces `healthy`/`unhealthy` based on the health signal age (>90s = stale).
- Sync happens in `on_ready` via `tree.sync()` — currently 3 top-level commands.

## Code Conventions

- No formatter/linter enforced — match surrounding style. Pre-existing E501 line-length warnings are ignored.
- Type hints used (`from __future__ import annotations`). `dict[str, X]` style (3.9+).
- Logging via `logger = logging.getLogger(__name__)`, level INFO.
- Components V2 messages sent via raw HTTP (`_send_v2`, `_interaction_callback` in `bot.py`) — discord.py 2.x has no native v2 support. Flags: `COMPONENTS_V2_FLAG = 1<<15`, `EPHEMERAL_FLAG = 64`. Callback types: 4 initial, 6 deferred-update, 7 update-message.
- Don't add docstrings/comments/type annotations to code you didn't change.
- Tests use `pytest`. Run with `pytest tests/`.

## Repository

- Origin: `https://github.com/aidenlong04/Golden-Pagoda-Image-Reader.git` (only `main`).
- Workspace folder name (`Golden-Pagoda-Screenshot-verify`) differs from repo name — that's expected.

## Things NOT in scope

- Don't suggest Fly.io, Heroku, or other hosts — we're on Hetzner.
- Don't add new env vars without updating both `.env.example` (if present) and the server's `/opt/golden-pagoda/.env`.
- Don't commit secrets, SSH keys, or `.env`.
- Don't remove `data/` or `icons/` from the rsync excludes — both hold runtime state owned by uid 10001 that the deploy user cannot unlink.
