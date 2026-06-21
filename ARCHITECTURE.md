# Architecture Map

A reference map of the Golden Pagoda ("Oda Helper") Discord bot: module
dependency graph, entry points, shared state, and external I/O. This is the
"map first" deliverable — kept deliberately high-level so it stays accurate as
the code evolves.

## Module dependency graph

```
config.py        env coercion helpers (_int_env, _float_env, _csv, _csv_ids) — no internal deps
logic.py         pure OCR-parse + mastery-format helpers; ClanSlot dataclass — no internal deps
utils/retry.py   exponential_backoff + retry wrappers — no internal deps
utils/metrics.py latency + heavy-job metrics (metrics_snapshot, ocr_latency, heavy_semaphore_metrics)
records_index.py JSON user_id -> [record message_ids] index — stdlib only
envstore.py      .env rewriters (_rewrite_env_file skeleton) — imports logic.ClanSlot
ocr_engine.py    Ollama -> OCR.space -> Tesseract chain — imports config, utils.retry
analytics.py     SQLite (events, member_titles, onboarding_prompts), fail-soft — stdlib sqlite3
cards.py         Pillow/numpy card rendering — imports logic._mastery_label_value
bot.py           Discord client, slash commands, V2 helpers, verify/role/onboarding/records flow
                 imports: logic, analytics, config, ocr_engine, envstore, cards, utils.metrics
```

`bot.py` re-exports selected `cards.py`, `envstore.py`, and `logic.py` symbols
so tests can resolve them as `bot.*`.

## Entry points

- **Gateway events** (`@client.event` in `bot.py`):
  - `on_ready` — resolves roles/clan slots from live guilds, syncs the 5 slash
    commands, starts `_health_task` and the onboarding reprompt loop.
  - `on_member_join` — posts the onboarding welcome (dynamic clan buttons).
  - `on_member_remove` — schedules the on-leave privacy data clear.
  - `on_member_update` — refreshes a member's record when tracked roles change
    (only if a record already exists).
  - `on_interaction` — dispatches button/modal/select interactions
    (`onboard:` / `manage:` / `status` / mreview custom-id branches).
- **Slash commands** (`@tree.command`): `/clan-emblems`, `/status`, `/profile`,
  `/titles`, `/manage`. Synced in `on_ready`; none are groups.
- **Process entry**: `python bot.py` (macOS/Linux) / `./run.ps1` (Windows).

## Shared state & globals (all in `bot.py` unless noted)

- Config-derived (read mostly): `DISCORD_TOKEN`, `TARGET_CHANNEL_ID`,
  `ONBOARDING_CHANNEL_ID`, `MEMBER_RECORDS_CHANNEL_ID`, accent colours, role-id
  lists (`MR_ROLE_IDS`, `SYNDICATE_ROLE_IDS`, `PLATFORM_ROLE_IDS`).
- Mutable runtime: `CLAN_SLOTS` (rewritten by `/clan-emblems` + `on_ready`
  resync), `_COMMAND_IDS`, `_EMOJI_BYTES_CACHE`, `_record_profile_cache`
  (60s TTL), `_JOIN_LAST_POST` debounce.
- Concurrency primitives: `_BG_TASKS` (strong refs for scheduled tasks),
  `_HEAVY_JOB_SEMAPHORE` (bounds renders/OCR), `_HTTP_SESSION` +
  `_HTTP_SESSION_LOCK`, per-user `_record_write_lock`.
- `analytics.py` module globals: `_conn`, `_read_conn`, `_initialized`,
  `_disabled` (fail-soft connection state).

## External I/O sites

- **Discord REST**: discord.py `client.http` + raw aiohttp via
  `_DISCORD_API_BASE` (`https://discord.com/api/v10`) for Components V2
  (`_send_v2`, `_post_channel_v2`, `_v2_multipart_request`,
  `_edit_channel_message_v2`, `_interaction_callback`). All funnel rate-limit
  retries through `_discord_call_with_retry`.
- **OCR** (`ocr_engine.py`): Ollama (`OLLAMA_URL`, default
  `http://localhost:11434`) -> OCR.space (`https://api.ocr.space`, engine 3)
  -> local Tesseract. Invoked from `bot.py` only via `_run_heavy`.
- **SQLite** (`analytics.py`): `ANALYTICS_DB_PATH`. Always called from `bot.py`
  through `asyncio.to_thread` — never on the event loop.
- **Records channel HTTP**: read/write the per-member record message
  (`_fetch_record_message`, `_edit_channel_message_v2`); the channel is the
  source of truth for profile data.
- **CDN fetches**: avatars + custom emojis via `_fetch_cdn_bytes`
  (`https://cdn.discordapp.com/...`), cached per id in `_EMOJI_BYTES_CACHE`.
- **Local files**: `.env` rewrites (`envstore.py`), records index JSON
  (`records_index.py`), health signal (`HEALTH_PATH`).

## Concurrency & reliability invariants

- Heavy work (Pillow/numpy renders, OCR) → `_run_heavy` (semaphore-bounded).
- SQLite + records-index JSON → `asyncio.to_thread` (off the loop).
- Background coroutines → `_spawn_bg_task` (strong ref + `_bg_task_done`
  logs any unhandled exception).
- SQLite/records I/O is fail-soft: gateway events never crash the bot.
- A role-only refresh never mints a screenshot-less record.
