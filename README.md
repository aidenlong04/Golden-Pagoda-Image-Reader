# Golden Pagoda — Screenshot Verify

Discord bot ("Oda Helper") that OCRs Warframe profile screenshots posted to a
configured channel, parses the in-game name + clan, and assigns the matching
platform / clan roles. Replies use Components V2 (containers, sections, gold
gradient progress card).

## What it does

- Watches one configured channel (`TARGET_CHANNEL_ID`) for image uploads.
- Runs OCR via [OCR.space](https://ocr.space/) engine 3 with a local
  Tesseract fallback if `OCR_API_KEY` is unset.
- Parses the **in-game name** from the title bar.
- Parses the **clan name** from the right-hand panel and matches it against
  the configured 7-slot clan list.
- Assigns the matching clan role plus any roles the member is missing,
  then sends a Components V2 reply with a gold progress card.
- On startup, scans the last `CATCHUP_LOOKBACK_HOURS` hours of channel
  history for screenshots that were missed while offline.

## Stack

- Python 3.12, `discord.py` 2.x (raw HTTP for Components V2)
- Pillow + NumPy for the progress card render
- SQLite (stdlib) for analytics (WAL mode, persistent connection)
- Pooled `aiohttp.ClientSession` for Discord REST + CDN
- Docker + systemd on Hetzner CX22; GitHub Actions auto-deploy on push to
  `main`

## Setup (local)

```bash
pip install -r requirements.txt
cp .env.example .env  # or export the variables manually
python bot.py
```

Required env: `DISCORD_TOKEN`, `TARGET_CHANNEL_ID`. See
[.github/copilot-instructions.md](.github/copilot-instructions.md) for the
full env reference.

## Slash commands

- `/clan-emblems role:<role> emoji:<:name:id>` — set the per-clan emoji at
  runtime. Updates in-memory state, `os.environ`, and rewrites the server's
  `.env`. Requires **Manage Server**.
- `/titles action:<add|remove> member:<member> title:<text> [reason]` — grant
  or remove a member's cosmetic profile title. Requires **Manage Server**.
- `/status` — paginated ephemeral status panel (bot, roles, channels, OCR,
  stats, clans, latency). Surfaces uvloop, RSS, BG tasks, the pooled HTTP
  session and the analytics SQLite WAL state. Requires **Manage Server**.
- `/manage member:<member>` — paginated ephemeral admin console (styled like
  `/status`, with Prev/Next/Refresh) to inspect a member's stored profile +
  titles and, on the last page, **clear** them behind a confirm step. This is
  the manual backup of the automatic on-leave clear below; it works for
  members who have already left the server. Requires **Manage Server**.

## Data retention / on-leave clear

The bot keeps a small durable per-member store (`member_profiles` +
`member_titles` in the analytics SQLite DB) so profile cards survive restarts.
When a member **leaves, is kicked, or is banned**, `on_member_remove` fires an
automatic "on-leave data clear": their stored profile and awarded titles are
deleted and their verification telemetry is anonymised (the rows stay for
aggregate stats, but `user_id` is set to `NULL`). The clear is scoped strictly
to that `(guild_id, user_id)` pair, runs off the event loop, and is fail-soft
(a gateway event can never crash the bot). The rendered profile/progress cards
hold no persisted state of their own, so clearing the store leaves nothing to
reference. `/manage` is the manual backup for cases the automatic clear can't
cover. Roles are never touched by either path.

## Deployment

Push to `main` triggers `.github/workflows/deploy.yml`:

1. rsync the repo to `/opt/golden-pagoda` (excludes `data/`, `icons/`, `.env`)
2. `docker build golden-pagoda:latest`
3. `systemctl restart golden-pagoda`

The systemd unit is hardened (`--init`, `--memory=512m`, `--cpus=1.5`,
`--pids-limit=256`, JSON log rotation). A sibling
`golden-pagoda-watchdog.service` blocks on `docker events` and restarts the
container on `health_status: unhealthy` or `die`, with cooldown +
sliding-window restart cap.

Server `.env` is the source of truth (`/opt/golden-pagoda/.env`); see
`scripts/ops.sh env-get` / `env-set` for in-place edits.

## Tests

```bash
pytest tests/
```

## Notes

- Requires Discord intents: message content, members.
- The bot can only assign roles that sit below its own highest role.
- Container runs as uid `10001`; `data/` and `icons/` on the host are
  chowned to that uid by the systemd unit pre-start.
