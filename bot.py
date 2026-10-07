import os
import zipfile
import shutil
import logging
import asyncio

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

from telethon import TelegramClient as TelethonClient
from telethon.sessions import StringSession as TelethonStringSession

from pyrogram import Client as PyroClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("session-destroyer")

# ---- Config (set these as environment variables on Railway) ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

WORK_DIR = "work"


# ---------------------------------------------------------------------
# Core logout logic — tries Telethon, then Pyrogram, for both
# file-based sessions and string sessions.
# ---------------------------------------------------------------------

async def logout_file_telethon(session_path_no_ext: str):
    client = TelethonClient(session_path_no_ext, API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("not authorized")
        me = await client.get_me()
        await client.log_out()
        label = getattr(me, "username", None) or getattr(me, "id", "unknown")
        return f"logged out (Telethon) — {label}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def logout_file_pyrogram(session_path_no_ext: str):
    workdir = os.path.dirname(session_path_no_ext) or "."
    name = os.path.basename(session_path_no_ext)
    client = PyroClient(name=name, api_id=API_ID, api_hash=API_HASH, workdir=workdir)
    await client.connect()
    try:
        me = await client.get_me()
        await client.log_out()
        label = getattr(me, "username", None) or getattr(me, "id", "unknown")
        return f"logged out (Pyrogram) — {label}"
    finally:
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception:
            pass


async def logout_session_file(session_path_no_ext: str, display_name: str) -> str:
    """Try Telethon first, fall back to Pyrogram, for a .session file."""
    try:
        result = await logout_file_telethon(session_path_no_ext)
        return f"✅ {display_name} — {result}"
    except Exception:
        try:
            result = await logout_file_pyrogram(session_path_no_ext)
            return f"✅ {display_name} — {result}"
        except Exception:
            return f"⚪ {display_name} — could not log in with either client (likely already invalid)"


async def logout_string_telethon(session_string: str):
    client = TelethonClient(TelethonStringSession(session_string), API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("not authorized")
        me = await client.get_me()
        await client.log_out()
        label = getattr(me, "username", None) or getattr(me, "id", "unknown")
        return f"logged out (Telethon string) — {label}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def logout_string_pyrogram(session_string: str):
    client = PyroClient(
        name="mem",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
    )
    await client.connect()
    try:
        me = await client.get_me()
        await client.log_out()
        label = getattr(me, "username", None) or getattr(me, "id", "unknown")
        return f"logged out (Pyrogram string) — {label}"
    finally:
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception:
            pass


async def logout_string_session(session_string: str, display_name: str) -> str:
    """Try Telethon first, fall back to Pyrogram, for a string session."""
    session_string = session_string.strip()
    if not session_string:
        return f"⚪ {display_name} — empty line, skipped"
    try:
        result = await logout_string_telethon(session_string)
        return f"✅ {display_name} — {result}"
    except Exception:
        try:
            result = await logout_string_pyrogram(session_string)
            return f"✅ {display_name} — {result}"
        except Exception:
            return f"❌ {display_name} — not a valid Telethon or Pyrogram string session"


# ---------------------------------------------------------------------
# File collection — walk a directory and classify files
# ---------------------------------------------------------------------

def collect_targets(root_dir):
    """Returns (session_files, string_entries) where:
    session_files = list of (path_no_ext, display_name)
    string_entries = list of (string, display_name)
    """
    session_files = []
    string_entries = []

    for dirpath, _, files in os.walk(root_dir):
        for f in files:
            full = os.path.join(dirpath, f)
            if f.endswith(".session"):
                session_files.append((full[: -len(".session")], f))
            elif f.endswith(".txt"):
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                        for i, line in enumerate(fh.read().splitlines(), 1):
                            line = line.strip()
                            if line:
                                string_entries.append((line, f"{f}:line{i}"))
                except Exception:
                    pass

    return session_files, string_entries


# ---------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------

async def process_and_report(update: Update, session_files, string_entries):
    total = len(session_files) + len(string_entries)
    if total == 0:
        await update.message.reply_text("⚠️ No session files or string sessions found.")
        return

    status_msg = await update.message.reply_text(f"🔎 Found {total} session(s). Processing...")
    results = []

    for path_no_ext, name in session_files:
        results.append(await logout_session_file(path_no_ext, name))
        await status_msg.edit_text("\n".join(results))
        await asyncio.sleep(1)

    for s, name in string_entries:
        results.append(await logout_string_session(s, name))
        await status_msg.edit_text("\n".join(results))
        await asyncio.sleep(1)

    await update.message.reply_text("Done.\n\n" + "\n".join(results))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    chat_id = update.effective_chat.id
    session_work_dir = os.path.join(WORK_DIR, str(chat_id))
    shutil.rmtree(session_work_dir, ignore_errors=True)
    os.makedirs(session_work_dir, exist_ok=True)

    fname = doc.file_name or "file"
    local_path = os.path.join(session_work_dir, fname)
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(local_path)

    if fname.lower().endswith(".zip"):
        extract_dir = os.path.join(session_work_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            with zipfile.ZipFile(local_path, "r") as z:
                z.extractall(extract_dir)
        except zipfile.BadZipFile:
            await update.message.reply_text("❌ That file isn't a valid zip.")
            return
        session_files, string_entries = collect_targets(extract_dir)

    elif fname.lower().endswith(".session"):
        session_files = [(local_path[: -len(".session")], fname)]
        string_entries = []

    elif fname.lower().endswith(".txt"):
        session_files = []
        _, string_entries = collect_targets(session_work_dir)

    else:
        await update.message.reply_text(
            "Send a .zip, a .session file, or a .txt file with string session(s)."
        )
        return

    await process_and_report(update, session_files, string_entries)
    shutil.rmtree(session_work_dir, ignore_errors=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return
    string_entries = [(line, f"pasted line {i+1}") for i, line in enumerate(lines)]
    await process_and_report(update, [], string_entries)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me any of these and I'll log each session out instantly:\n"
        "• a .zip of .session files and/or .txt files\n"
        "• a single .session file\n"
        "• a .txt file with one string session per line\n"
        "• or just paste a string session as a text message\n\n"
        "Works for both Telethon and Pyrogram sessions — format is auto-detected."
    )


def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
