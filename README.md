# Golden Pagoda — Screenshot Verify

Discord bot ("Oda Helper") that OCRs Warframe profile screenshots posted to a
configured channel, parses the in-game name + clan, and assigns the matching
platform / clan roles. Replies use Components V2 (containers, sections, gold
gradient progress card). New members get a guided onboarding experience with
clan buttons, screenshot upload, and automatic re-prompts.

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

### Member onboarding flow

When a new member joins the server:

1. The bot posts a **public welcome** in `TARGET_CHANNEL_ID` that @-mentions
   the member and shows **dynamic clan buttons** (from the live 7-slot clan
   configuration) plus a **"Not listed / No"** button.
2. Clicking a **clan button** opens a screenshot upload modal. The screenshot
   is OCR-verified: the clan the member *claims* must match what the OCR reads
   before any clan role is granted.
3. **"Not listed / No"** assigns the `INCOMPLETE_ROLE_ID` (pending-review role),
   notifies the staff channel (`HELP_CHANNEL_ID`), and acknowledges ephemerally.
4. If the member **hasn't completed onboarding within `ONBOARDING_REPROMPT_HOURS`
   hours** (default 5), the bot re-posts a fresh welcome and deletes the old one.
   A configurable cap (`ONBOARDING_MAX_REPROMPTS`, default 3) prevents pinging
   forever.
5. All onboarding state is persisted in the `onboarding_prompts` SQLite table
   and reconciled on startup so restarts/redeploys never lose or duplicate a
   prompt.
6. The passive screenshot detection + catch-up scan remain as a **self-healing
   fallback** so a member who pastes a screenshot directly (no button) is still
   processed, and joins missed while the bot was offline are recovered on the
   next startup.

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
- `/onboard member:<member>` — post the onboarding welcome prompt for a member
  on demand (the same pipeline as the automatic join welcome — clan buttons +
  screenshot verification). Useful for members who joined while the bot was
  offline or who need a fresh prompt. Mirrored by the **Start onboarding**
  button on the `/manage` Overview page. Requires **Manage Server**.
- `/status` — paginated ephemeral status panel (bot, roles, channels, OCR,
  stats, clans, latency). Surfaces uvloop, RSS, BG tasks, the pooled HTTP
  session and the analytics SQLite WAL state. Requires **Manage Server**.
- `/manage member:<member>` — paginated ephemeral admin console (styled like
  `/status`, with Prev/Next/Refresh). The **Overview**, **Titles** and
  **Data & Clear** pages inspect a member's stored profile + titles and let
  you **clear** them behind a confirm step (the manual backup of the automatic
  on-leave clear below; works for members who have already left). The Overview
  page also offers an **Update from screenshot** button (admin OCR re-verify)
  and a **Start onboarding** button (re-posts the welcome prompt, same as
  `/onboard`). The **Edit**
  page edits a present member's verification data — Discord roles **and** the
  durable store are updated together: in-game name (text modal), platform
  (assigns the platform role), mastery rank incl. Legendary (swaps the MR
  bucket role + stores the exact rank), clan (dynamic buttons whose names and
  emojis come from the live clan slots, like `/status`), and syndicates
  (multi-select that syncs the syndicate roles). A Titles button points you at
  `/titles`. Requires **Manage Server**.
- `/profile [user] [ephemeral] [edit_mastery]` — render a member's profile card.
  Defaults to caller; ephemeral by default. Requires the configured access role
  or Manage Server. Syncs the member's clan/platform from their current roles
  into the durable store on each use.

## Data retention / on-leave clear

The bot keeps a small durable per-member store (`member_profiles` +
`member_titles` in the analytics SQLite DB) so profile cards survive restarts.
When a member **leaves, is kicked, or is banned**, `on_member_remove` fires an
automatic "on-leave data clear": their stored profile and awarded titles are
deleted, their onboarding prompt state is removed, and their verification
telemetry is anonymised (the rows stay for aggregate stats, but `user_id` is
set to `NULL`). The clear is scoped strictly to that `(guild_id, user_id)` pair,
runs off the event loop, and is fail-soft (a gateway event can never crash the
bot). The rendered profile/progress cards hold no persisted state of their own,
so clearing the store leaves nothing to reference. `/manage` is the manual
backup for cases the automatic clear can't cover. Roles are never touched by
either path.

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

## Performance improvements

The following improvements were added to address latency, reliability, and observability issues identified during review.

### New modules

| Module | Purpose |
|---|---|
| `utils/retry.py` | Exponential back-off with jitter (`exponential_backoff`, `retry_sync`) and a three-state circuit breaker (`CircuitBreaker`) |
| `utils/metrics.py` | In-process semaphore utilisation tracker (`SemaphoreMetrics`), rolling latency percentile recorder (`LatencyRecorder`), and `metrics_snapshot()` |

### What changed

**`ocr_engine.py`**
- `_ocr_via_api` now uses `exponential_backoff` (configurable via
  `OCR_RETRY_MAX_ATTEMPTS` / `OCR_RETRY_BASE_DELAY` / `OCR_RETRY_MAX_DELAY`)
  instead of a fixed 1 s sleep.
- A `CircuitBreaker` (`ocr_circuit_breaker` in `utils/retry.py`) short-circuits
  repeated OCR.space API calls after `OCR_CIRCUIT_BREAKER_THRESHOLD` consecutive
  failures, avoiding 60 s timeout pile-ups during an outage.
- An LRU cache (`OCR_CACHE_SIZE`, default 32 entries, keyed by SHA-256 of the
  image bytes) skips the full OCR pipeline for repeated uploads.

**`analytics.py`**
- `summary()` caches its result for `ANALYTICS_SUMMARY_TTL` seconds (default 30)
  so `/status` page refreshes don't re-run ~7 SQL queries each time.
- A dedicated read-only SQLite connection (`_connect_read`) serves analytics
  queries without holding the write lock.
- `record_verification` and `delete_member_data` invalidate the cache on writes.

**`bot.py`**
- `_HEAVY_JOB_SEMAPHORE` concurrency is now tunable via `HEAVY_JOB_CONCURRENCY`
  (default 2; safe for 512 MB Hetzner CX22).
- `_run_heavy` instruments `heavy_semaphore_metrics` so `/status → Latency` shows
  current / peak / queued counts and average queue-wait time.
- `_discord_call_with_retry` wraps role-assignment calls with rate-limit-aware
  exponential back-off (respects Discord's `Retry-After` header).
- A new **Latency** page on `/status` surfaces the semaphore snapshot, session-
  level OCR and analytics-query latency percentiles, and the OCR.space circuit-
  breaker state.

### Tuning guide

All parameters have safe defaults and are backwards-compatible — no `.env`
changes are required to upgrade.  To tune for a more powerful host:

```bash
# Allow 3 concurrent heavy jobs (verify memory headroom first via /status Latency).
scripts/ops.sh env-set HEAVY_JOB_CONCURRENCY 3

# Reduce analytics cache TTL for a high-traffic server where fresh stats matter.
scripts/ops.sh env-set ANALYTICS_SUMMARY_TTL 10

# Increase OCR retries if the OCR.space API is flaky in your region.
scripts/ops.sh env-set OCR_RETRY_MAX_ATTEMPTS 4
scripts/ops.sh env-set OCR_RETRY_BASE_DELAY 2.0
```

## Notes

- Requires Discord intents: message content, members.
- The bot can only assign roles that sit below its own highest role.
- Container runs as uid `10001`; `data/` and `icons/` on the host are
  chowned to that uid by the systemd unit pre-start.
