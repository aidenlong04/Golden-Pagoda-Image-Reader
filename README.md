# Golden-Pagoda-Screenshot-verify

A Discord bot that reads screenshot images in a specific channel and assigns roles based on detected text.

## What it does

- Watches one configured channel for image attachments
- Runs OCR against the uploaded screenshot
- Matches OCR text against configured keyword-to-role rules
- Assigns the matching role to the user
- Replies with clear feedback when the screenshot cannot be read or does not match any rule

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Install the Tesseract OCR binary on your system (required by `pytesseract`).
3. Set environment variables:
   - `DISCORD_TOKEN`: bot token
   - `TARGET_CHANNEL_ID`: numeric Discord channel ID to monitor
   - `ROLE_RULES`: comma-separated rules in `keyword:Role Name` format
     - Example: `golden pagoda:Golden Pagoda Verified,legend:Legend Verified`
4. Run the bot:
   ```bash
   python bot.py
   ```

## Notes

- The bot requires Discord intents for message content and members.
- The bot can only assign roles that already exist in the server.
