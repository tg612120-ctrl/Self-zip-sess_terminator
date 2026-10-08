import os
import sqlite3
import zipfile
import shutil
import logging
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient as TelethonClient
from telethon.sessions import StringSession as TelethonStringSession, MemorySession
from telethon.crypto import AuthKey
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.errors import SessionPasswordNeededError

from pyrogram import Client as PyroClient
from pyrogram.raw.functions.account import GetAuthorizations, ResetAuthorization

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("session-destroyer")

# ---- Config (set these as environment variables on Railway) ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

WORK_DIR = "work"

MODE_DESTROYER = "destroyer"
MODE_TERMINATOR = "terminator"
MODE_GENERATE = "generate"


# ---------------------------------------------------------------------
# Manual sqlite fallback — for session files whose schema has extra
# columns (e.g. some third-party clients like TurboTel add a
# tmp_auth_key column), which breaks Telethon's built-in loader.
# ---------------------------------------------------------------------

def manual_telethon_session_from_sqlite(session_file_path: str):
    conn = sqlite3.connect(session_file_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1")
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise RuntimeError("no row in sessions table")

    dc_id, server_address, port, auth_key_bytes = row
    if not auth_key_bytes or len(auth_key_bytes) < 256:
        raise RuntimeError("auth_key missing or too short")

    session = MemorySession()
    session.set_dc(dc_id, server_address, port)
    session.auth_key = AuthKey(data=auth_key_bytes)
    return session


# ---------------------------------------------------------------------
# Getting a connected, authorized client — Telethon and Pyrogram,
# from either a file path or a string session.
# ---------------------------------------------------------------------

async def get_authorized_telethon_client(session_path_no_ext=None, session_string=None):
    if session_string:
        client = TelethonClient(TelethonStringSession(session_string), API_ID, API_HASH)
        await client.connect()
    else:
        session_file_path = session_path_no_ext + ".session"
        try:
            client = TelethonClient(session_path_no_ext, API_ID, API_HASH)
            await client.connect()
        except Exception:
            manual_session = manual_telethon_session_from_sqlite(session_file_path)
            client = TelethonClient(manual_session, API_ID, API_HASH)
            await client.connect()

    if not await client.is_user_authorized():
        try:
            await client.disconnect()
        except Exception:
            pass
        raise RuntimeError("not authorized")
    return client


async def get_authorized_pyrogram_client(session_path_no_ext=None, session_string=None):
    if session_string:
        client = PyroClient(
            name="mem", api_id=API_ID, api_hash=API_HASH,
            session_string=session_string, in_memory=True,
        )
    else:
        workdir = os.path.dirname(session_path_no_ext) or "."
        name = os.path.basename(session_path_no_ext)
        client = PyroClient(name=name, api_id=API_ID, api_hash=API_HASH, workdir=workdir)

    await client.connect()
    try:
        await client.get_me()
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise
    return client


async def disconnect_quietly(client, lib):
    try:
        if lib == "Pyrogram":
            if client.is_connected:
                await client.disconnect()
        else:
            await client.disconnect()
    except Exception:
        pass


# ---------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------

async def action_destroy_self_telethon(client):
    me = await client.get_me()
    label = getattr(me, "username", None) or getattr(me, "id", "unknown")
    await client.log_out()
    return f"logged out (Telethon) — {label}"


async def action_destroy_self_pyrogram(client):
    me = await client.get_me()
    label = getattr(me, "username", None) or getattr(me, "id", "unknown")
    await client.log_out()
    return f"logged out (Pyrogram) — {label}"


async def action_terminate_others_telethon(client):
    me = await client.get_me()
    label = getattr(me, "username", None) or getattr(me, "id", "unknown")
    auths = await client(GetAuthorizationsRequest())
    killed, failed = 0, 0
    for a in auths.authorizations:
        if a.current:
            continue
        try:
            await client(ResetAuthorizationRequest(hash=a.hash))
            killed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.3)
    extra = f", {failed} failed" if failed else ""
    return f"kept this session, terminated {killed} other(s){extra} — {label}"


async def action_terminate_others_pyrogram(client):
    me = await client.get_me()
    label = getattr(me, "username", None) or getattr(me, "id", "unknown")
    auths = await client.invoke(GetAuthorizations())
    killed, failed = 0, 0
    for a in auths.authorizations:
        if a.current:
            continue
        try:
            await client.invoke(ResetAuthorization(hash=a.hash))
            killed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.3)
    extra = f", {failed} failed" if failed else ""
    return f"kept this session, terminated {killed} other(s){extra} — {label}"


# ---------------------------------------------------------------------
# Unified per-session processor
# ---------------------------------------------------------------------

async def process_one(display_name, mode, session_path_no_ext=None, session_string=None):
    client, lib = None, None
    e_tel = e_pyro = None

    try:
        client = await get_authorized_telethon_client(session_path_no_ext, session_string)
        lib = "Telethon"
    except Exception as e1:
        e_tel = e1
        try:
            client = await get_authorized_pyrogram_client(session_path_no_ext, session_string)
            lib = "Pyrogram"
        except Exception as e2:
            e_pyro = e2

    if client is None:
        return (
            f"⚪ {display_name} — failed both ways\n"
            f"    Telethon: {type(e_tel).__name__}: {e_tel}\n"
            f"    Pyrogram: {type(e_pyro).__name__}: {e_pyro}"
        )

    try:
        if mode == MODE_TERMINATOR:
            action = action_terminate_others_telethon if lib == "Telethon" else action_terminate_others_pyrogram
        else:
            action = action_destroy_self_telethon if lib == "Telethon" else action_destroy_self_pyrogram
        result = await action(client)
        return f"✅ {display_name} — {result}"
    except Exception as e:
        return f"❌ {display_name} — action failed: {type(e).__name__}: {e}"
    finally:
        await disconnect_quietly(client, lib)


# ---------------------------------------------------------------------
# File collection — walk a directory and classify files
# ---------------------------------------------------------------------

def collect_targets(root_dir):
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
# Session generation — ordinary phone + OTP + (optional) 2FA login,
# for the user's OWN account(s), producing sessions to store for later.
# Supports choosing Telethon or Pyrogram format, and generating several
# accounts in one go (bundled into a zip at the end).
# ---------------------------------------------------------------------

def gen_format_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔸 Telethon String", callback_data="genfmt:telethon")],
        [InlineKeyboardButton("🔹 Pyrogram String", callback_data="genfmt:pyrogram")],
    ])


def gen_more_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add another account", callback_data="genmore:yes")],
        [InlineKeyboardButton("✅ Done — send me the result", callback_data="genmore:no")],
    ])


async def start_generate_phone_step(update_or_query, context: ContextTypes.DEFAULT_TYPE, send_fn):
    state = context.user_data["gen_state"]
    state["step"] = "phone"
    n = len(state["collected"]) + 1
    await send_fn(f"Account #{n} — send the phone number with country code, e.g. `+919876543210`:",
                   parse_mode="Markdown")


async def gen_dir_for(update: Update) -> str:
    chat_id = update.effective_chat.id
    d = os.path.join(WORK_DIR, str(chat_id), "generated")
    os.makedirs(d, exist_ok=True)
    return d


async def telethon_login_file(gen_dir, safe_phone, client_mem):
    """Given a connected+authorized in-memory Telethon client, write out a
    matching .session (sqlite) file without opening a second connection."""
    file_session_path = os.path.join(gen_dir, safe_phone)
    file_client = TelethonClient(file_session_path, API_ID, API_HASH)
    file_client.session.set_dc(
        client_mem.session.dc_id, client_mem.session.server_address, client_mem.session.port
    )
    file_client.session.auth_key = client_mem.session.auth_key
    file_client.session.save()
    return file_session_path + ".session"


async def finish_one_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data["gen_state"]
    fmt = state["format"]
    client = state["client"]
    phone = state["phone"]
    safe_phone = phone.replace("+", "").replace(" ", "")
    gen_dir = await gen_dir_for(update)

    me = await client.get_me()
    label = getattr(me, "username", None) or getattr(me, "id", "unknown")

    if fmt == "telethon":
        session_string = client.session.save()
        session_file = await telethon_login_file(gen_dir, safe_phone, client)
        await client.disconnect()
    else:  # pyrogram
        session_string = await client.export_session_string()
        await client.disconnect()
        session_file = os.path.join(gen_dir, f"{safe_phone}.session")

    state["collected"].append({"phone": phone, "label": label, "file": session_file, "string": session_string})

    await update.message.reply_text(
        f"✅ Account #{len(state['collected'])} done — {label} ({fmt})\n\n"
        f"🔑 String session:\n`{session_string}`",
        parse_mode="Markdown",
    )
    state.pop("client", None)
    state.pop("phone_code_hash", None)
    await update.message.reply_text("Add one more account, or finish up:", reply_markup=gen_more_keyboard())


async def cancel_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
    state = context.user_data.get("gen_state")
    if state and state.get("client"):
        try:
            await state["client"].disconnect()
        except Exception:
            pass
    await update.message.reply_text(f"❌ {reason}")
    context.user_data.pop("gen_state", None)


async def handle_generate_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data["gen_state"]
    step = state["step"]
    fmt = state["format"]
    text = (update.message.text or "").strip()

    if step == "phone":
        phone = text
        try:
            if fmt == "telethon":
                client = TelethonClient(TelethonStringSession(), API_ID, API_HASH)
                await client.connect()
                sent = await client.send_code_request(phone)
                phone_code_hash = sent.phone_code_hash
            else:
                gen_dir = await gen_dir_for(update)
                safe_phone = phone.replace("+", "").replace(" ", "")
                client = PyroClient(
                    name=safe_phone, api_id=API_ID, api_hash=API_HASH,
                    workdir=gen_dir, in_memory=False,
                )
                await client.connect()
                sent = await client.send_code(phone)
                phone_code_hash = sent.phone_code_hash
        except Exception as e:
            await cancel_generate(update, context, f"Could not send code: {type(e).__name__}: {e}")
            return

        state.update({"phone": phone, "client": client, "phone_code_hash": phone_code_hash, "step": "otp"})
        await update.message.reply_text("📩 Code sent via Telegram. Enter the code you received:")

    elif step == "otp":
        otp = text
        client = state["client"]
        phone = state["phone"]
        try:
            if fmt == "telethon":
                await client.sign_in(phone=phone, code=otp, phone_code_hash=state["phone_code_hash"])
            else:
                await client.sign_in(phone_number=phone, phone_code_hash=state["phone_code_hash"], phone_code=otp)
        except SessionPasswordNeededError:
            state["step"] = "2fa"
            await update.message.reply_text("🔒 This account has 2-step verification. Send the password:")
            return
        except Exception as e:
            # Pyrogram raises its own SessionPasswordNeeded under a different class
            if type(e).__name__ == "SessionPasswordNeeded":
                state["step"] = "2fa"
                await update.message.reply_text("🔒 This account has 2-step verification. Send the password:")
                return
            await cancel_generate(update, context, f"{type(e).__name__}: {e}")
            return
        await finish_one_account(update, context)

    elif step == "2fa":
        password = text
        client = state["client"]
        try:
            if fmt == "telethon":
                await client.sign_in(password=password)
            else:
                await client.check_password(password)
        except Exception as e:
            await cancel_generate(update, context, f"Incorrect password / error: {type(e).__name__}: {e}")
            return
        await finish_one_account(update, context)


async def on_gen_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    state = context.user_data.get("gen_state")
    if not state:
        await query.edit_message_text("Session expired, send /start to begin again.")
        return

    if choice == "yes":
        await query.edit_message_text("Okay, next account:")
        await start_generate_phone_step(update, context, query.message.reply_text)
        return

    # choice == "no" -> package and deliver everything collected
    collected = state["collected"]
    chat_id = update.effective_chat.id
    gen_dir = await gen_dir_for(update)

    if not collected:
        await query.edit_message_text("No accounts were generated.")
        context.user_data.pop("gen_state", None)
        return

    # Build a .txt with one string session per line (label as a comment above it)
    strings_txt_path = os.path.join(gen_dir, "string_sessions.txt")
    with open(strings_txt_path, "w", encoding="utf-8") as f:
        for item in collected:
            f.write(f"# {item['label']} ({item['phone']})\n{item['string']}\n\n")

    if len(collected) == 1:
        item = collected[0]
        await query.edit_message_text(f"✅ Done — 1 account ({item['label']}).")
        await context.bot.send_document(
            chat_id, item["file"],
            caption=f"📁 .session file for {item['label']} — keep this private.",
        )
        await context.bot.send_document(
            chat_id, strings_txt_path,
            caption="📝 String session (.txt) — keep this private.",
        )
    else:
        zip_path = os.path.join(gen_dir, "sessions.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in collected:
                zf.write(item["file"], arcname=os.path.basename(item["file"]))
            zf.write(strings_txt_path, arcname="string_sessions.txt")
        await query.edit_message_text(f"✅ Done — {len(collected)} accounts bundled into a zip.")
        await context.bot.send_document(
            chat_id, zip_path,
            caption=f"📦 {len(collected)} .session files + string_sessions.txt — keep this private.",
        )

    shutil.rmtree(os.path.join(WORK_DIR, str(chat_id)), ignore_errors=True)
    context.user_data.pop("gen_state", None)


# ---------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------

def mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Self Destroyer", callback_data=f"mode:{MODE_DESTROYER}")],
        [InlineKeyboardButton("🛡️ Terminator", callback_data=f"mode:{MODE_TERMINATOR}")],
        [InlineKeyboardButton("🆕 Generate Session", callback_data=f"mode:{MODE_GENERATE}")],
    ])


MODE_NAMES = {
    MODE_DESTROYER: "🔴 Self Destroyer (logs the sent session itself out)",
    MODE_TERMINATOR: "🛡️ Terminator (keeps the sent session, kills every OTHER session on that account)",
    MODE_GENERATE: "🆕 Generate Session (log in with your own phone + OTP, get a session to store)",
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mode", None)
    await update.message.reply_text(
        "Choose a mode:\n\n"
        "🔴 *Self Destroyer* — logs out the session(s) you send.\n"
        "🛡️ *Terminator* — logs IN with the session(s) you send and terminates "
        "every OTHER active session on that account, keeping only itself "
        "(useful for securing an ID instantly).",
        reply_markup=mode_keyboard(),
        parse_mode="Markdown",
    )


async def on_mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["mode"] = mode

    if mode == MODE_GENERATE:
        await query.edit_message_text(
            f"Mode set: {MODE_NAMES[mode]}\n\n"
            "This logs in to YOUR OWN account(s) using your own phone and the "
            "code Telegram sends to it — same as logging into Telegram Desktop.\n\n"
            "Pick the session format:",
            reply_markup=gen_format_keyboard(),
        )
        return

    await query.edit_message_text(
        f"Mode set: {MODE_NAMES[mode]}\n\n"
        "Now send me any of these:\n"
        "• a .zip of .session files and/or .txt files\n"
        "• a single .session file\n"
        "• a .txt file with one string session per line\n"
        "• or paste a string session as a text message\n\n"
        "Send /start anytime to change mode."
    )


async def on_gen_format_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fmt = query.data.split(":", 1)[1]
    context.user_data["gen_state"] = {"step": "phone", "format": fmt, "collected": []}
    await query.edit_message_text(f"Format: {fmt.capitalize()}")
    await start_generate_phone_step(update, context, query.message.reply_text)


async def process_and_report(update: Update, mode, session_files, string_entries):
    total = len(session_files) + len(string_entries)
    if total == 0:
        await update.message.reply_text("⚠️ No session files or string sessions found.")
        return

    status_msg = await update.message.reply_text(f"🔎 Found {total} session(s). Processing...")
    results = []

    for path_no_ext, name in session_files:
        results.append(await process_one(name, mode, session_path_no_ext=path_no_ext))
        await status_msg.edit_text("\n".join(results))
        await asyncio.sleep(1)

    for s, name in string_entries:
        results.append(await process_one(name, mode, session_string=s))
        await status_msg.edit_text("\n".join(results))
        await asyncio.sleep(1)

    await update.message.reply_text("Done.\n\n" + "\n".join(results))


async def require_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "mode" not in context.user_data:
        await update.message.reply_text(
            "Pick a mode first:", reply_markup=mode_keyboard()
        )
        return None
    return context.user_data["mode"]


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "gen_state" in context.user_data:
        await update.message.reply_text("Finish the phone/OTP steps with text first, or send /start to cancel.")
        return

    mode = await require_mode(update, context)
    if not mode:
        return

    if mode == MODE_GENERATE:
        await update.message.reply_text("Send /start and pick a mode again — generate mode doesn't take files.")
        return

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

    await process_and_report(update, mode, session_files, string_entries)
    shutil.rmtree(session_work_dir, ignore_errors=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "gen_state" in context.user_data:
        await handle_generate_text(update, context)
        return

    mode = await require_mode(update, context)
    if not mode:
        return

    if mode == MODE_GENERATE:
        # mode was set but gen_state got cleared/lost — restart the flow
        await update.message.reply_text("Pick the session format:", reply_markup=gen_format_keyboard())
        return

    text = update.message.text or ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return
    string_entries = [(line, f"pasted line {i+1}") for i, line in enumerate(lines)]
    await process_and_report(update, mode, [], string_entries)


def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_mode_chosen, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_gen_format_chosen, pattern=r"^genfmt:"))
    app.add_handler(CallbackQueryHandler(on_gen_more, pattern=r"^genmore:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
