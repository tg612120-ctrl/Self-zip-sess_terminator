# Session Destroyer Bot

Telegram bot: send it your own session(s) and it logs each one out instantly
(no 24h "terminate all sessions" wait — this is a direct, single-session logout).

Accepts, in any combination:
- a `.zip` containing `.session` files and/or `.txt` files
- a single `.session` file
- a `.txt` file with one string session per line
- a string session pasted directly as a chat message (one or more lines)

Format is auto-detected: each session is tried with **Telethon** first, then
**Pyrogram** if that fails, so you don't need to know which library created it.

**Use this only on sessions that belong to you.**

## Setup

1. Get a bot token from [@BotFather](https://t.me/BotFather) → `BOT_TOKEN`
2. Get `API_ID` and `API_HASH` from https://my.telegram.org (API Development Tools)
3. Copy `.env.example` → set these as environment variables on Railway

## Deploy on Railway

1. Push this folder to a GitHub repo
2. Railway → New Project → Deploy from GitHub repo
3. In Railway → Variables, add:
   - `BOT_TOKEN`
   - `API_ID`
   - `API_HASH`
4. Railway will auto-detect `Procfile` and run `python bot.py` as a worker
5. Open your bot in Telegram, send `/start`, then send your `.zip`

## How it works

- If you send a `.zip`, it's extracted and every `.session` file and every
  line of every `.txt` file inside is treated as a separate session to process
- For each one, the bot tries to connect as **Telethon** first; if that fails
  (wrong format / not authorized), it tries **Pyrogram**
- If a connection succeeds and the session is valid, it calls that library's
  `log_out()` — this logs out *only that one session*, nothing else
- Reports a status line per session: logged out / already invalid / error
- All downloaded and extracted files are deleted from the server right after
  processing finishes

## Notes

- Works for sessions created by either Telethon or Pyrogram — detection is
  automatic, no need to specify which.
- Supports both file-based sessions (`.session`) and string sessions (plain
  text, e.g. Telethon `StringSession` or Pyrogram `session_string`).
- You can paste a string session straight into the chat instead of uploading
  a file.
