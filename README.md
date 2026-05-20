# Golden-Pagoda-Screenshot-verify

A Discord bot that reads screenshot images in a specific channel and assigns roles based on detected text.

## What it does

- Watches one configured channel for Warframe profile screenshots
- Runs Tesseract OCR (engine `--oem 3`) against the uploaded image
- Parses the **profile name** from the title bar (top text box)
- Detects the **platform** from the icon next to the profile name
  (PC / Xbox / PlayStation / Switch — color-classified)
- Parses the **clan name** from the right-hand box (or treats `UNAFFILIATED` as no clan)
- Assigns the configured platform role and clan role
- Clan name → role mappings can be updated at runtime via `/setclan`

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Install the Tesseract OCR binary on your system (required by `pytesseract`).
3. Copy `.env.example` to `.env` (or otherwise export the variables) and fill in:
   - `DISCORD_TOKEN`, `TARGET_CHANNEL_ID`, optional `GUILD_ID`
   - **4 platform role IDs**: `PLATFORM_ROLE_PC_ID`, `PLATFORM_ROLE_XBOX_ID`,
     `PLATFORM_ROLE_PS_ID`, `PLATFORM_ROLE_SWITCH_ID`
   - **6 clan role slots**: `CLAN_ROLE_1_NAME`/`CLAN_ROLE_1_ID` through
     `CLAN_ROLE_6_NAME`/`CLAN_ROLE_6_ID`. The `_NAME` is matched against the
     OCR'd clan name; the `_ID` is the Discord role assigned on a match.
4. Run the bot:
   ```bash
   python bot.py
   ```

## Slash commands

- `/setclan slot:<1-6> clan_name:<text> role_name:<exact role>` — looks up the
  given role by name in the server and stores its ID against the slot. Overrides
  the env defaults and persists to `clan_roles.json` (configurable via
  `CLAN_CONFIG_PATH`). Requires Manage Roles.
- `/listclans` — show the current 6-slot mapping.

## Notes

- Requires Discord intents: message content, members.
- The bot can only assign roles that already exist in the server, and that sit
  below its own highest role.
- Platform detection uses brand-color heuristics on the top of the screenshot;
  unusual cropping or themes may reduce accuracy.
