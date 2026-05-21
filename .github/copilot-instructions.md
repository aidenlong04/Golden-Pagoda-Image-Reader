# Golden Pagoda — Project Instructions

Discord bot that OCRs Warframe profile screenshots, verifies platform + clan, and assigns roles.

## Stack

- **Runtime**: Python 3.12, `discord.py` 2.x, OCR.space (engine 3) with Tesseract fallback
- **Entrypoint**: `bot.py` (single file). Pure logic in `logic.py`. Tests in `tests/`.
- **Container**: `Dockerfile` produces `golden-pagoda:latest` image.
- **Hosting**: Hetzner CX22, Ubuntu 24.04, Docker + systemd.

## Deployment

- **Auto-deploy**: Push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) → rsync to `/opt/golden-pagoda/` on Hetzner → `docker build` → `systemctl restart golden-pagoda`.
- **Server**: `5.78.211.130`, SSH user `nomekui` (NOPASSWD sudo, in `docker` group). Root login + password auth disabled.
- **SSH keys** (in codespace): `~/.ssh/hetzner` (interactive), `~/.ssh/gha_deploy` (workflow).
- **GitHub secrets**: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY` (base64-encoded private key — workflow `base64 -d`s it to avoid CRLF mangling).
- **systemd unit**: `scripts/golden-pagoda.service` → `/etc/systemd/system/golden-pagoda.service`.
- **Manual deploy**: `./scripts/deploy_hetzner.sh nomekui@5.78.211.130 ~/.ssh/hetzner`.
- **Server `.env`**: `/opt/golden-pagoda/.env` (NOT in git — gitignored). Edit with `sudo` then `sudo systemctl restart golden-pagoda`.
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
- Never commit `.env`. Update server-side env, then restart the service.

## Slash Commands

- `/clan-emblems role:<role> emoji:<:name:id>` — set per-clan emoji at runtime. Updates in-memory `CLAN_SLOTS`, `os.environ`, AND rewrites `/opt/golden-pagoda/.env`. Requires Manage Server perm.
- Sync happens in `on_ready` via `tree.sync()`.

## Code Conventions

- No formatter/linter enforced — match surrounding style. Pre-existing E501 line-length warnings are ignored.
- Type hints used (`from __future__ import annotations`). `dict[str, X]` style (3.9+).
- Logging via `logger = logging.getLogger(__name__)`, level INFO.
- Components V2 messages sent via raw HTTP (`_send_v2` in `bot.py`) — discord.py 2.x has no native v2 support.
- Don't add docstrings/comments/type annotations to code you didn't change.
- Tests use `pytest`. Run with `pytest tests/`.

## Repository

- Origin: `https://github.com/aidenlong04/Golden-Pagoda-Image-Reader.git` (only `main`).
- Workspace folder name (`Golden-Pagoda-Screenshot-verify`) differs from repo name — that's expected.

## Things NOT in scope

- Don't suggest Fly.io, Heroku, or other hosts — we're on Hetzner.
- Don't add new env vars without updating both `.env.example` (if present) and the server's `/opt/golden-pagoda/.env`.
- Don't commit secrets, SSH keys, or `.env`.
