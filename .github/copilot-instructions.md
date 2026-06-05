# Golden Pagoda — Project Instructions

Discord bot ("**Oda Helper**") that OCRs Warframe profile screenshots, verifies platform + clan, and assigns roles. The maintainer agent for this repo is **Oda Assistant** (see `.github/agents/oda-assistant.agent.md`).

## Stack

- **Runtime**: Python 3.12, `discord.py` 2.x, OCR.space (engine 3) with Tesseract fallback.
- **Entrypoint**: `bot.py` (single file, Discord client + slash commands + V2 component helpers). Pure logic in `logic.py`. Analytics in `analytics.py` (stdlib `sqlite3`, fail-soft). Tests in `tests/`.
- **Container**: `Dockerfile` produces `golden-pagoda:latest` image. Runs as non-root `bot` user (uid `10001`).
- **Hosting**: Hetzner CX22, Ubuntu 24.04, Docker + systemd.
- **Catch-up scan**: On startup, the bot scans recent message history in `TARGET_CHANNEL_ID` for unprocessed screenshots (those without the bot's pass/fail reactions) and verifies them. Configurable via `CATCHUP_LOOKBACK_HOURS` (default 24). State is persisted in `/app/data/catchup_state.json` to avoid re-scanning on every restart.

## Deployment

- **Auto-deploy**: Push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) → rsync (excludes `data/`, `icons/`, `.env`) → `docker build` → `systemctl restart golden-pagoda`.
- **Server**: `5.78.211.130`, SSH user `nomekui` (NOPASSWD sudo, in `docker` group). Root login + password auth disabled.
- **SSH keys** (in codespace): `~/.ssh/hetzner` (interactive), `~/.ssh/gha_deploy` (workflow).
- **GitHub secrets**: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY` (base64-encoded private key — workflow `base64 -d`s it).
- **systemd unit**: `scripts/golden-pagoda.service` → `/etc/systemd/system/golden-pagoda.service`. Hardened: `--init`, `--memory=512m`, `--cpus=1.5`, `--pids-limit=256`, json-file log rotation (10m × 3 files), `SuccessExitStatus=0 137 143`.
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
- `SYNDICATE_EMOJI` — custom Discord emoji (`<:name:id>`) for the Syndicate row on the `/profile` card; empty default (falls back to a bullet). Set via `scripts/ops.sh env-set SYNDICATE_EMOJI "<:name:id>"`.
- `INCOMPLETE_ROLE_ID`, `VERIFY_REMOVE_ROLE_ID` — verification flow roles.
- `PASS_REACTION_ID`, `FAIL_REACTION`, `PENDING_REACTION_ID` — reactions on the original screenshot.
- `REPLY_TTL_SECONDS` — auto-delete bot replies after N seconds.
- `CATCHUP_LOOKBACK_HOURS` (default `24`) — how many hours of message history to scan on startup for missed screenshots.
- `CATCHUP_STATE_PATH` (default `/app/data/catchup_state.json`) — where to persist the last-scanned message ID.
- `CATCHUP_DELAY_SECONDS` (default `1.0`) — sleep between catch-up message processing to avoid rate limits.
- `HEALTH_PATH` (default `/tmp/gp_health`), `HEALTH_INTERVAL` (default `20` seconds) — liveness signal.
- `ANALYTICS_DB_PATH` (default `/app/data/analytics.db`) — SQLite path inside the container.
- Never commit `.env`. Update server-side env, then restart the service.

## Slash Commands

- `/clan-emblems role:<role> emoji:<:name:id>` — set per-clan emoji at runtime. Updates in-memory `CLAN_SLOTS`, `os.environ`, AND rewrites `/opt/golden-pagoda/.env`. Requires Manage Server perm.
- `/preview-responses` — post sample pass/fail/incomplete V2 messages to the preview channel.
- `/status` — single ephemeral V2 paginated message with 8 pages (bot, roles, channels, misc, stats, platforms, clans, ocr). Prev/Next/Refresh buttons walk every page. Requires Manage Server perm. The "Bot" page surfaces `healthy`/`unhealthy` based on the health signal age (>90s = stale).
- `/progress [user] [ephemeral]` — render a member's verification completion (0-100%) as a progress-card PNG (circular avatar + gradient bar + labeled info rows). Defaults to the caller; `ephemeral` hides the reply. Any member can run it.
- `/profile [user] [ephemeral] [edit_mastery]` — render a member's **user profile** card PNG: the same role-derived reference grid as `/progress` (Clan | Platform over Mastery Rank | Syndicate) but **without the progress bar**, under a refined gold "USER PROFILE" eyebrow + name header. Defaults to the caller; **ephemeral defaults to true** (pass `ephemeral:false` to post publicly). `edit_mastery:true` (opt-in, only on your own profile) attaches a Mastery Rank dropdown (1-30 / Legendary 1-8) that swaps your coarse MR role bucket AND stores the exact rank. Any member can run it.
- Sync happens in `on_ready` via `tree.sync()` — currently 5 top-level commands.
- **Progress card** (`_render_progress_card_png` in `bot.py`): laid out in logical units and rendered at `_PROGRESS_SS`× (supersampled) for crisp text/icons on HiDPI. Composites a circular avatar, a numpy-shaded **segmented** progress bar (`_segmented_bar` — one rounded segment per verification category; filled segments share a continuous glassy gradient that flows from Warframe energy cyan through mint into Orokin gold — the gold growing more pronounced toward the filled edge the further a member progresses, fully gold when complete — with gloss + traced outline + leading-edge glow, empty segments are recessed track with a faint amber "pending" tint), and optional `(label, value, emoji_bytes)` info rows. Row icons are aspect-preserved (`ImageOps.contain`, centered — never stretched, via the shared `_paste_emoji_icon` helper); clan/platform/profile/mastery rows render their configured custom emoji (fetched once at 128px via `_fetch_emoji_bytes`, cached per emoji ID) in place of the bullet. The avatar is centered in a fixed header zone so it never drifts. Directly beneath the bar a status line shows the gold "all roles registered" note when complete, or an **amber "Missing: …" pill** (with the warning icon) when a pass still has outstanding categories — its contextual home, replacing the old orphaned bottom row. The reference data fills a **two-column grid** (via the shared module-level `_draw_info_grid` helper — which wraps `_draw_cell` and is reused by the profile card) below the divider, row-major in the order Clan / Mastery Rank / Profile / Platform so it lays out as `Clan | Mastery` over `Profile | Platform` (Profile sits directly under Clan). `CLAN_ROLE_*_EMOJI`, `PLATFORM_EMOJI_*`, `OPERATOR_EMOJI`, `MASTERY_RANK_EMOJI`, and `WARNING_EMOJI` literals feed it. On a pass, `_pass_components` puts the card image on top as a top-level media gallery (type 12) and builds **one** gold-accented V2 container (type 17) holding the in-game-name call-sign choices (server-nick + in-game-name `nick:` buttons, when there's a name worth suggesting). Folding the nickname prompt into that container keeps the whole pass reply to one image + one container (no second embed, no stray action row); when there's no name worth suggesting the container is omitted entirely and the reply is just the card image. The call-sign caption + the two `nick:` buttons come from the shared `_callsign_buttons` helper (single source of truth, reused by the pass card path, the no-card fallback, and the standalone incomplete prompt; the caption text lives in the `_CALLSIGN_CAPTION` constant and buttons in `_nick_button`); when the member picks a call sign, `_strip_nick_prompt` drops any standalone `_NICK_PROMPT_ACCENT` container and reaches into the pass container to remove just the caption + `nick:` buttons (dropping the container if nothing survives). The incomplete reply's nick prompt (`_nickname_prompt_components`) is its own self-contained gold container (caption + buttons nested inside). All Link buttons are built via the shared `_link_button` / `_link_button_row` helpers (the latter applies Discord's 5-per-row cap). The incomplete and `/progress` replies surface a "How to get your roles" Link button (`_help_link_buttons` → `HELP_CHANNEL_ID`).
- **Profile card** (`_render_profile_card_png` in `bot.py`): a sibling of the progress card with the bar removed — same supersampled rounded slate panel and circular avatar, with a refined header of a gold "USER PROFILE" eyebrow above the member name. Purely role-derived (no OCR). The two headline fields **Clan** and **Mastery Rank** are promoted into featured "stat tiles" (`_draw_stat_tile`) laid out side-by-side in the full-width band where the progress bar sits on the `/progress` card — each a rounded slate tile (`_PROGRESS_STAT_TOP`/`_PROGRESS_STAT_BOTTOM` gradient) with a gold left accent strip, the category emoji, a small muted uppercase label and the value in large bold. The remaining fields **Platform** and **Syndicate** flow into the shared `_draw_info_grid` two-column reference grid below a divider. Featured-row order is fixed (`Clan` then `Mastery Rank`); the tile band collapses to one full-width tile if only one is configured, and the bottom grid/divider is omitted when no remaining fields exist. Categories the member hasn't earned render an em-dash "—". Rows come from the async `_member_profile_info_lines(member)` gatherer (Clan slot name + emoji, platform + `PLATFORM_EMOJI_*`, mastery role name + `MASTERY_RANK_EMOJI`, syndicate role names joined + `SYNDICATE_EMOJI`); the Mastery Rank row prefers the **exact stored rank** from the durable member store over the coarse role bucket, formatted through the shared `_format_mastery_display` (`"MR 28"`→`"28"`, `"LR 3"`→`"Legendary 3"`). `/profile` defers then sends just the `profile.png` image (no V2 container) — unless `edit_mastery:true`, which also attaches a native `_MasteryEditorView` (`discord.ui.View` + two `_MasterySelect`s, split because Discord caps a select at 25 options; `interaction_check` restricts it to the profile owner; each select stashes its editor back-ref as `self._editor` — NOT `_parent`, which discord.py reserves for `Item._run_checks`). A pick runs `_apply_mastery_bucket` (`_mr_bucket_role_for` maps the rank to the configured bucket by parsing live role names via `_parse_mr_bucket_range`, robust to non-index-aligned `MR_ROLE_IDS`), `await`s `analytics.upsert_member_profile` synchronously to avoid a re-render race, then edits the message in place with the fresh card.
- **Durable member store** (`analytics.py`): a `member_profiles` table (PK `(guild_id, user_id)`; columns `mastery_rank`, `in_game_name`, `platform`, `clan`, `last_verified_ts`, `updated_ts`) on the same SQLite DB as analytics (`ANALYTICS_DB_PATH`, bind-mounted under `/app/data`, WAL, fail-soft). `upsert_member_profile(**partial)` uses `INSERT … ON CONFLICT … DO UPDATE SET col=COALESCE(excluded.col, col)` so a mastery-only edit never wipes the verify-time snapshot; `get_member_profile(guild_id, user_id)` reads it back. The verify flow persists a full snapshot per pass/incomplete via `_spawn_bg_task` (off the event loop); the `/profile` editor persists the mastery-only override.

## Code Conventions

- No formatter/linter enforced — match surrounding style. Pre-existing E501 line-length warnings are ignored.
- Type hints used (`from __future__ import annotations`). `dict[str, X]` style (3.9+).
- Logging via `logger = logging.getLogger(__name__)`, level INFO.
- Components V2 messages sent via raw HTTP (`_send_v2`, `_interaction_callback` in `bot.py`) — discord.py 2.x has no native v2 support. Flags: `COMPONENTS_V2_FLAG = 1<<15`, `EPHEMERAL_FLAG = 64`. Callback types: 4 initial, 6 deferred-update, 7 update-message.
- Don't add docstrings/comments/type annotations to code you didn't change.
- Tests use `pytest`. Run with `pytest tests/` (35 tests across `test_logic.py`, `test_analytics.py`, `test_bot_smoke.py`, `test_catchup.py`).
- `.env` writers (`_update_env_clan_slots`, `_update_env_platform_ids`, `_update_env_id_list`) share one `_rewrite_env_file(replace_line, missing_lines)` skeleton — add new persisters through it rather than re-implementing the read→replace→append loop. CDN fetches (avatar + emoji) share `_fetch_cdn_bytes`.

## Repository

- Origin: `https://github.com/aidenlong04/Golden-Pagoda-Image-Reader.git` (only `main`).
- Workspace folder name (`Golden-Pagoda-Screenshot-verify`) differs from repo name — that's expected.

## Things NOT in scope

- Don't suggest Fly.io, Heroku, or other hosts — we're on Hetzner.
- Don't add new env vars without updating both `.env.example` and the server's `/opt/golden-pagoda/.env`.
- Don't commit secrets, SSH keys, or `.env`.
- Don't remove `data/` or `icons/` from the rsync excludes — both hold runtime state owned by uid 10001 that the deploy user cannot unlink.
