#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================
  FREE FIRE LIKE / VISIT BOT — single file (bot.py)
  Built for Railway deployment.
============================================================

Everything the owner needs to configure is either hardcoded
below (so it works out of the box) or editable at runtime via
the /admin panel (DM the bot as the owner).
"""

import os
import re
import time
import sqlite3
import logging
import threading
from datetime import datetime, date, timedelta

import requests
import requests.adapters
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

# ------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ff-bot")

# ------------------------------------------------------------------
### CONFIG ----------------------------------------------------------
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var is missing. Set it in Railway → Variables.")

# Hardcoded fallbacks — these ALWAYS work regardless of what Railway's
# env vars are set to (so the bot never breaks over a variable typo).
HARDCODED_OWNER_IDS = {7892255798}
HARDCODED_BACKUP_CHANNEL_ID = -1003941781570


def _parse_owner_ids(raw: str):
    ids = set()
    for token in raw.replace("\n", ",").replace(" ", "").split(","):
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            log.warning("OWNER_IDS: ignoring invalid entry %r", token)
    return ids


OWNER_IDS = _parse_owner_ids(os.environ.get("OWNER_IDS", "")) | HARDCODED_OWNER_IDS
log.info("Loaded OWNER_IDS: %s", OWNER_IDS)

_backup_env = os.environ.get("BACKUP_CHANNEL_ID", "").strip()
BACKUP_CHANNEL_ID = int(_backup_env) if _backup_env else HARDCODED_BACKUP_CHANNEL_ID
log.info("Using BACKUP_CHANNEL_ID: %s", BACKUP_CHANNEL_ID)

# The two third-party APIs
LIKE_API_URL  = "https://srk-like-api.vercel.app/like?uid={uid}&server_name={region}"
VISIT_API_URL = "http://visit-api10k.up.railway.app/{region}/{uid}"

# Limits
LIKE_LIMIT_PER_DAY  = 1
VISIT_COOLDOWN_SECS = 25

# Default auto-like time (Asia/Kolkata) — editable later via /admin panel
DEFAULT_AUTOLIKE_HOUR   = 4
DEFAULT_AUTOLIKE_MINUTE = 0
AUTOLIKE_WORKERS = 40

BACKUP_INTERVAL_MIN = 30
DB_PATH = "bot_data.db"
TZ = ZoneInfo("Asia/Kolkata")

# How many worker threads telebot uses to process incoming updates in
# parallel — this is what lets the bot handle many simultaneous users
# instead of queueing them one by one.
BOT_THREADS = int(os.environ.get("BOT_THREADS", "120"))

# ------------------------------------------------------------------
# BOT INIT
# ------------------------------------------------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=BOT_THREADS)
BOT_USERNAME = bot.get_me().username
BOT_ID = bot.get_me().id
autolike_executor = ThreadPoolExecutor(max_workers=AUTOLIKE_WORKERS)

# Shared HTTP session with a big connection pool so hundreds of
# simultaneous API calls don't bottleneck on socket setup.
HTTP = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=200, pool_maxsize=200, max_retries=0)
HTTP.mount("https://", _adapter)
HTTP.mount("http://", _adapter)
# A browser-like User-Agent — some free API hosts (Vercel etc.) silently
# reject requests carrying the default "python-requests/x.x" UA.
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
})

# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_seen TEXT
            );
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY, title TEXT, added_at TEXT
            );
            CREATE TABLE IF NOT EXISTS channels (
                chat_id INTEGER PRIMARY KEY, title TEXT, added_at TEXT
            );
            CREATE TABLE IF NOT EXISTS like_usage (
                user_id INTEGER, usage_date TEXT, count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            );
            CREATE TABLE IF NOT EXISTS visit_cooldown (
                user_id INTEGER PRIMARY KEY, last_used REAL
            );
            CREATE TABLE IF NOT EXISTS autolikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, uid TEXT,
                region TEXT, name TEXT, days_left INTEGER, added_by INTEGER, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY, banned_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS verification_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, username TEXT UNIQUE, added_at TEXT
            );
            """
        )
        conn.commit()

        # seed default verification channels only once
        count = conn.execute("SELECT COUNT(*) c FROM verification_channels").fetchone()["c"]
        if count == 0:
            defaults = [
                ("SRK ERA", "@SRK_ERA"),
                ("SRK IMPORTANT", "@SRK_IMP1"),
                ("SN NETWORK", "@snnetwork7"),
                ("SNxHUB", "@snxhub"),
            ]
            conn.executemany(
                "INSERT INTO verification_channels (name, username, added_at) VALUES (?,?,?)",
                [(n, u, datetime.now(TZ).isoformat()) for n, u in defaults],
            )
            conn.commit()

        # seed default settings only if missing
        defaults = {
            "maintenance": "0",
            "autolike_hour": str(DEFAULT_AUTOLIKE_HOUR),
            "autolike_minute": str(DEFAULT_AUTOLIKE_MINUTE),
            "like_reset_hour": "3",
            "like_reset_minute": "58",
            "result_image_file_id": "",
            "deny_msg_type": "",
            "deny_msg_text": "",
            "deny_msg_file_id": "",
            "deny_msg_caption": "",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))
        conn.commit()


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def usage_day() -> str:
    """The daily /like allowance resets at a fixed cutoff time (default
    03:58 IST, just before auto-like fires at 04:00) instead of at
    midnight — so returns 'yesterday' if we're still before today's
    cutoff, matching that reset point."""
    now = datetime.now(TZ)
    cutoff_h = int(get_setting("like_reset_hour", "3"))
    cutoff_m = int(get_setting("like_reset_minute", "58"))
    cutoff_today = now.replace(hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0)
    if now < cutoff_today:
        return (now - timedelta(days=1)).date().isoformat()
    return now.date().isoformat()


# Set for the duration of the daily auto-like blast so that any /like
# command arriving around the same moment (people schedule-send theirs
# for 04:00/04:01 to catch the reset) waits until auto-like's requests
# have already reached the API first.
autolike_in_progress = threading.Event()


def restore_db_from_backup():
    if not BACKUP_CHANNEL_ID:
        return
    try:
        chat = bot.get_chat(BACKUP_CHANNEL_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            log.info("No pinned backup found — starting fresh.")
            return
        file_info = bot.get_file(pinned.document.file_id)
        data = bot.download_file(file_info.file_path)
        with open(DB_PATH, "wb") as f:
            f.write(data)
        log.info("Database restored from backup channel.")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not restore backup (probably first run): %s", e)


def backup_db_job():
    """Returns (ok: bool, detail: str) so callers can show a real result."""
    if not BACKUP_CHANNEL_ID:
        return False, "BACKUP_CHANNEL_ID is not set."
    try:
        member = bot.get_chat_member(BACKUP_CHANNEL_ID, BOT_ID)
        if member.status not in ("administrator", "creator"):
            return False, f"Bot is not an admin in the backup channel (status: {member.status})."
    except Exception as e:  # noqa: BLE001
        return False, f"Cannot see backup channel — check the ID / that the bot is a member: {e}"

    try:
        with _db_lock:
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect("backup_tmp.db")
            src.backup(dst)
            src.close()
            dst.close()
        with open("backup_tmp.db", "rb") as f:
            msg = bot.send_document(
                BACKUP_CHANNEL_ID, f,
                caption=f"🗄 <b>Database Backup</b>\n🕒 {datetime.now(TZ).strftime('%d-%m-%Y %H:%M:%S')}",
            )
        os.remove("backup_tmp.db")

        # Pinning is how a bot "remembers" its own latest message inside a
        # channel with no history API — but pinning needs the specific
        # "Pin Messages" admin right, separate from general admin status.
        pin_ok = True
        pin_detail = ""
        try:
            bot.pin_chat_message(BACKUP_CHANNEL_ID, msg.message_id, disable_notification=True)
        except Exception as e:  # noqa: BLE001
            pin_ok = False
            pin_detail = str(e)

        # Also remember the message id locally as a fallback pointer —
        # not restart-proof by itself, but helps same-session lookups.
        set_setting("last_backup_message_id", str(msg.message_id))

        log.info("Database backed up (message_id=%s, pinned=%s).", msg.message_id, pin_ok)
        if pin_ok:
            return True, f"Sent and pinned as message #{msg.message_id} in the backup channel."
        return True, (
            f"Sent as message #{msg.message_id}, but pinning FAILED ({pin_detail}). "
            "Restore-on-restart relies on the pinned message, so please give the bot "
            "the specific 'Pin Messages' admin right in that channel (not just 'admin')."
        )
    except Exception as e:  # noqa: BLE001
        log.error("Backup failed: %s", e)
        return False, str(e)


# ------------------------------------------------------------------
# STYLE HELPERS
# ------------------------------------------------------------------
_SMALLCAPS = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
)
_BOLD_DIGITS = str.maketrans("0123456789", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗")
_SUPER_DIGITS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def smallcaps(text):
    return str(text).lower().translate(_SMALLCAPS)


def bold_digits(value):
    return str(value).translate(_BOLD_DIGITS)


def super_digits(value):
    return str(value).translate(_SUPER_DIGITS)


def divider():
    return "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"


def box(title, rows, footer=None, footer_label="Time Taken", highlight=None):
    """Styled receipt-card text, wrapped in Telegram's native blockquote."""
    highlight = highlight or set()
    lines = [f"┌ {smallcaps(title)}"]
    for label, value in rows:
        shown = f"<b>{bold_digits(value)}</b>" if label in highlight else value
        lines.append(f"├─ {smallcaps(label)}: {shown}")
    if footer is not None:
        lines.append(f"└─ {smallcaps(footer_label)}: {super_digits(footer)}")
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


# ------------------------------------------------------------------
# LIVE "PROCESSING…" ANIMATION (kept short — 2 lines max, no wrapping)
# ------------------------------------------------------------------
_ANIM_FRAMES = ["⏳", "⌛"]


def start_processing_animation(chat_id, message_id, uid):
    stop_event = threading.Event()

    def _loop():
        i = 0
        while not stop_event.wait(3):
            i += 1
            frame = _ANIM_FRAMES[i % len(_ANIM_FRAMES)]
            try:
                bot.edit_message_text(
                    f"{frame} <b>Processing…</b> 🎮 <code>{uid}</code>",
                    chat_id, message_id,
                )
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop_event, thread


def stop_processing_animation(stop_event, thread):
    stop_event.set()
    thread.join(timeout=1)


# ------------------------------------------------------------------
# API CALLS
# ------------------------------------------------------------------
def _get_json(url, timeout, retries=1):
    """GET with a browser-like User-Agent (some of these free API hosts
    silently block the default python-requests UA) and one quick retry
    on transient failures before giving up. Tries to parse JSON first —
    some of these APIs return a non-200 status code even on a perfectly
    valid response, so we don't want to fail on status code alone."""
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = HTTP.get(url, timeout=timeout)
            elapsed = time.time() - t0
            try:
                data = r.json()
            except ValueError:
                r.raise_for_status()  # genuinely not JSON — raise with real status info
                raise
            return data, elapsed
        except requests.exceptions.Timeout:
            raise  # don't retry timeouts — they already waited long enough
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.error("API call attempt %d failed for %s: %s", attempt + 1, url.split("?")[0], e)
            if attempt < retries:
                time.sleep(1.5)
                continue
            raise last_err


class InvalidAPIResponse(Exception):
    """Raised when the API replies but without the fields we expect —
    e.g. a bad UID/region — so it never gets shown as a fake 'success'."""


def call_like_api(uid: str, region: str, timeout=90):
    url = LIKE_API_URL.format(uid=uid, region=region.lower())
    data, elapsed = _get_json(url, timeout)
    if not isinstance(data, dict) or "PlayerNickname" not in data:
        raise InvalidAPIResponse("Response missing expected player fields")
    return data, elapsed


def call_visit_api(uid: str, region: str, timeout=90):
    url = VISIT_API_URL.format(uid=uid, region=region.lower())
    data, elapsed = _get_json(url, timeout)
    if not isinstance(data, dict) or "nickname" not in data:
        raise InvalidAPIResponse("Response missing expected player fields")
    return data, elapsed


# ------------------------------------------------------------------
# TRACKING
# ------------------------------------------------------------------
def track_user(u: types.User):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_seen) VALUES (?,?,?)",
            (u.id, u.username or "", datetime.now(TZ).isoformat()),
        )
        conn.commit()


def track_group(chat: types.Chat):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO groups (chat_id, title, added_at) VALUES (?,?,?)",
            (chat.id, chat.title or "", datetime.now(TZ).isoformat()),
        )
        conn.commit()


def track_channel(chat: types.Chat):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channels (chat_id, title, added_at) VALUES (?,?,?)",
            (chat.id, chat.title or "", datetime.now(TZ).isoformat()),
        )
        conn.commit()


# ------------------------------------------------------------------
# BAN SYSTEM
# ------------------------------------------------------------------
def is_banned(user_id) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,)).fetchone()
        return row is not None


def ban_user(user_id):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO banned_users (user_id, banned_at) VALUES (?,?)",
            (user_id, datetime.now(TZ).isoformat()),
        )
        conn.commit()


def unban_user(user_id):
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        conn.commit()


# ------------------------------------------------------------------
# VERIFICATION CHANNELS (DB-backed, editable via /admin)
# ------------------------------------------------------------------
def get_verification_channels():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM verification_channels ORDER BY id ASC"
        ).fetchall()


def is_verified(user_id: int):
    """Returns (bool, [missing channel names])."""
    missing = []
    for ch in get_verification_channels():
        try:
            member = bot.get_chat_member(ch["username"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch["name"])
        except Exception:  # noqa: BLE001
            missing.append(ch["name"])
    return (len(missing) == 0), missing


def colored_button(text, style=None, **kwargs):
    """InlineKeyboardButton / KeyboardButton wrapper that adds Bot API 9.4
    colored `style` ('primary' blue / 'success' green / 'danger' red)."""
    is_inline = "callback_data" in kwargs or "url" in kwargs
    cls = types.InlineKeyboardButton if is_inline else types.KeyboardButton
    try:
        return cls(text, style=style, **kwargs) if style else cls(text, **kwargs)
    except TypeError:
        return cls(text, **kwargs)


def verify_keyboard():
    """Layout: 1 big top button (first/primary channel), up to 2 small
    middle buttons, 1 big bottom button, then the check button."""
    channels = get_verification_channels()
    kb = types.InlineKeyboardMarkup()

    def ch_button(ch):
        link = f"https://t.me/{ch['username'].lstrip('@')}"
        return colored_button(f"📢 {ch['name']}", style="primary", url=link)

    if channels:
        kb.row(ch_button(channels[0]))
        mid = channels[1:3]
        if mid:
            kb.row(*[ch_button(c) for c in mid])
        rest = channels[3:]
        for c in rest:
            kb.row(ch_button(c))

    kb.row(colored_button("✅ I've Joined — Check Again", style="success", callback_data="check_verify"))
    return kb


def send_verification_prompt(chat_id):
    text = (
        "🔒 <b>Verification Required</b>\n"
        + divider()
        + "\nJoin all the channels below, then tap "
        "<b>✅ I've Joined</b> to unlock the bot."
    )
    bot.send_message(chat_id, text, reply_markup=verify_keyboard())


# ------------------------------------------------------------------
# ACCESS GUARDS
# ------------------------------------------------------------------
def add_to_group_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(colored_button(
        "➕ ADD ME TO YOUR GROUP", style="success",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
    ))
    return kb


def group_only(message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        bot.send_message(
            message.chat.id,
            "🚫 This bot only works inside <b>groups</b>, not in private chat.\n"
            "Add me to your group to use like/visit commands 👇",
            reply_markup=add_to_group_kb(),
        )
        return False
    return True


def is_group_admin(chat_id, user_id) -> bool:
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator") or user_id in OWNER_IDS
    except Exception:  # noqa: BLE001
        return False


def is_owner(user_id) -> bool:
    return user_id in OWNER_IDS


def deny_owner_only(message):
    """Reply with the owner's custom-configured denial content, or a
    sane default if nothing has been configured via the admin panel."""
    t = get_setting("deny_msg_type", "")
    try:
        if t == "text" and get_setting("deny_msg_text", ""):
            bot.reply_to(message, get_setting("deny_msg_text", ""))
            return
        if t and get_setting("deny_msg_file_id", ""):
            fid = get_setting("deny_msg_file_id", "")
            cap = get_setting("deny_msg_caption", "") or None
            senders = {
                "photo": bot.send_photo, "video": bot.send_video,
                "animation": bot.send_animation, "document": bot.send_document,
                "voice": bot.send_voice,
            }
            if t == "sticker":
                bot.send_sticker(message.chat.id, fid, reply_to_message_id=message.message_id)
                return
            if t in senders:
                senders[t](message.chat.id, fid, caption=cap, reply_to_message_id=message.message_id)
                return
    except Exception as e:  # noqa: BLE001
        log.error("deny_owner_only failed: %s", e)
    bot.reply_to(message, "🚫 Only the bot owner can use this command.")


# ------------------------------------------------------------------
# RESULT DELIVERY (plain text, or as a photo card if owner set one)
# ------------------------------------------------------------------
def deliver_result(chat_id, message_id, text, keyboard):
    img = get_setting("result_image_file_id", "")
    if img:
        try:
            bot.edit_message_media(
                chat_id=chat_id, message_id=message_id,
                media=types.InputMediaPhoto(img, caption=text, parse_mode="HTML"),
                reply_markup=keyboard,
            )
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Could not deliver as photo, falling back to text: %s", e)
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)
    except Exception:  # noqa: BLE001
        bot.send_message(chat_id, text, reply_markup=keyboard)


# ------------------------------------------------------------------
# COMMAND: /myid  — debug helper, works everywhere, no restrictions
# ------------------------------------------------------------------
@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    uid = message.from_user.id
    status = "✅ YES — recognized as owner" if is_owner(uid) else "❌ NO — not in OWNER_IDS"
    bot.reply_to(
        message,
        f"🆔 Your Telegram ID: <code>{uid}</code>\n👑 Owner status: {status}",
    )


# ------------------------------------------------------------------
# COMMAND: /start
# ------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    track_user(message.from_user)
    if message.chat.type in ("group", "supergroup"):
        track_group(message.chat)
    bot.send_message(
        message.chat.id,
        "👋 <b>Welcome!</b>\n"
        + divider()
        + "\n⚡️ Free Fire Like &amp; Visit Bot\n"
        "🎯 Works only inside groups.\n"
        "📌 Commands:\n"
        "   /like IND {uid}\n"
        "   /visit IND {uid}\n",
        reply_markup=add_to_group_kb(),
    )


# ------------------------------------------------------------------
# CALLBACK: verification recheck
# ------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "check_verify")
def cb_check_verify(call):
    ok, missing = is_verified(call.from_user.id)
    if ok:
        bot.answer_callback_query(call.id, "✅ Verification successful!")
        try:
            bot.edit_message_text(
                "✅ <b>Verified!</b> You can now use /like or /visit in the group.",
                call.message.chat.id, call.message.message_id,
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        names = ", ".join(missing)
        bot.answer_callback_query(
            call.id, f"❌ You haven't joined: {names}", show_alert=True,
        )


# ------------------------------------------------------------------
# PARSE HELPERS
# ------------------------------------------------------------------
UID_RE = re.compile(r"^\d{6,12}$")


def parse_region_uid(args, command="like"):
    if len(args) != 2:
        return None, None, f"⚠️ Usage: <code>/{command} IND 1234567890</code>"
    region, uid = args[0].upper(), args[1]
    if not UID_RE.match(uid):
        return None, None, "⚠️ Invalid UID — numbers only."
    return region, uid, None


# ------------------------------------------------------------------
# COMMAND: /like  IND {uid}
# ------------------------------------------------------------------
@bot.message_handler(commands=["like"])
def cmd_like(message):
    track_user(message.from_user)
    if not group_only(message):
        return
    track_group(message.chat)

    if is_banned(message.from_user.id):
        bot.reply_to(message, "🚫 You are banned from using this bot.")
        return
    if get_setting("maintenance", "0") == "1" and not is_owner(message.from_user.id):
        bot.reply_to(message, "🛠 <b>Bot is under maintenance.</b> Please try again later.")
        return

    ok, _ = is_verified(message.from_user.id)
    if not ok:
        send_verification_prompt(message.chat.id)
        return

    args = message.text.split()[1:]
    region, uid, err = parse_region_uid(args, command="like")
    if err:
        bot.reply_to(message, err)
        return

    user_id = message.from_user.id
    today = usage_day()
    if not is_owner(user_id):
        with db() as conn:
            row = conn.execute(
                "SELECT count FROM like_usage WHERE user_id=? AND usage_date=?",
                (user_id, today),
            ).fetchone()
            used = row["count"] if row else 0
        if used >= LIKE_LIMIT_PER_DAY:
            bot.reply_to(
                message,
                "⏳ <b>Daily limit reached!</b>\nYou can only use /like "
                f"{LIKE_LIMIT_PER_DAY}x per day. Try again tomorrow.",
            )
            return

    processing = bot.reply_to(message, f"⏳ <b>Processing…</b> 🎮 <code>{uid}</code>")
    anim_stop, anim_thread = start_processing_animation(message.chat.id, processing.message_id, uid)

    # Let the daily auto-like blast go through to the API first if it's
    # currently running (people schedule-send /like right at reset time).
    if autolike_in_progress.is_set():
        autolike_in_progress.wait(timeout=90)

    try:
        data, elapsed = call_like_api(uid, region)
    except requests.exceptions.Timeout:
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text(
            "⌛ Server is taking too long. It may still be processing — "
            "please check again in a minute before retrying.",
            message.chat.id, processing.message_id)
        return
    except InvalidAPIResponse:
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text(
            "⚠️ Couldn't fetch player info. Please double-check the UID and try again.",
            message.chat.id, processing.message_id)
        return
    except Exception as e:  # noqa: BLE001
        log.error("Like API failed uid=%s region=%s: %s", uid, region, e)
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text("❌ Something went wrong, please try again later.",
                               message.chat.id, processing.message_id)
        return

    stop_processing_animation(anim_stop, anim_thread)
    nickname = data.get("PlayerNickname", "Unknown")
    try:
        bot.edit_message_text(f"⏳ <b>Processing…</b> 🎮 {nickname}",
                               message.chat.id, processing.message_id)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)

    given = data.get("LikesGivenByAPI", 0)
    text = box(
        "Player Information ✨✅",
        [
            ("Nickname", nickname),
            ("UID", data.get("UID", uid)),
            ("Region", data.get("PlayerRegion", region)),
            ("Level", data.get("PlayerLevel", "-")),
            ("Likes", data.get("LikesafterCommand", "-")),
            ("Given", given),
        ],
        footer=f"{elapsed:.2f} seconds",
        highlight={"Likes", "Given"},
    )
    if given == 0:
        text += "\n\n⚠️ <i>This UID already reached max likes for today.</i>"
    else:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO like_usage (user_id, usage_date, count) VALUES (?,?,1) "
                "ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1",
                (user_id, today),
            )
            conn.commit()

    deliver_result(message.chat.id, processing.message_id, text, add_to_group_kb())
    if given > 0:
        bot.send_message(message.chat.id, "🎉 Like sent successfully!")


# ------------------------------------------------------------------
# COMMAND: /visit  IND {uid}
# ------------------------------------------------------------------
_visit_lock = threading.Lock()


@bot.message_handler(commands=["visit"])
def cmd_visit(message):
    track_user(message.from_user)
    if not group_only(message):
        return
    track_group(message.chat)

    if is_banned(message.from_user.id):
        bot.reply_to(message, "🚫 You are banned from using this bot.")
        return
    if get_setting("maintenance", "0") == "1" and not is_owner(message.from_user.id):
        bot.reply_to(message, "🛠 <b>Bot is under maintenance.</b> Please try again later.")
        return

    ok, _ = is_verified(message.from_user.id)
    if not ok:
        send_verification_prompt(message.chat.id)
        return

    args = message.text.split()[1:]
    region, uid, err = parse_region_uid(args, command="visit")
    if err:
        bot.reply_to(message, err)
        return

    user_id = message.from_user.id
    now = time.time()
    with _visit_lock, db() as conn:
        row = conn.execute(
            "SELECT last_used FROM visit_cooldown WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and now - row["last_used"] < VISIT_COOLDOWN_SECS and not is_owner(user_id):
            wait = int(VISIT_COOLDOWN_SECS - (now - row["last_used"]))
            bot.reply_to(message, f"⏳ Please wait <b>{wait}s</b> before using /visit again.")
            return
        conn.execute(
            "INSERT INTO visit_cooldown (user_id, last_used) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_used=?",
            (user_id, now, now),
        )
        conn.commit()

    processing = bot.reply_to(message, f"⏳ <b>Processing…</b> 🎮 <code>{uid}</code>")
    anim_stop, anim_thread = start_processing_animation(message.chat.id, processing.message_id, uid)

    try:
        data, elapsed = call_visit_api(uid, region)
    except requests.exceptions.Timeout:
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text(
            "⌛ Server is taking too long. It may still be processing — "
            "please check again in a minute before retrying.",
            message.chat.id, processing.message_id)
        return
    except InvalidAPIResponse:
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text(
            "⚠️ Couldn't fetch player info. Please double-check the UID and try again.",
            message.chat.id, processing.message_id)
        return
    except Exception as e:  # noqa: BLE001
        log.error("Visit API failed uid=%s region=%s: %s", uid, region, e)
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text("❌ Something went wrong, please try again later.",
                               message.chat.id, processing.message_id)
        return

    stop_processing_animation(anim_stop, anim_thread)
    nickname = data.get("nickname", "Unknown")
    try:
        bot.edit_message_text(f"⏳ <b>Processing…</b> 🎮 {nickname}",
                               message.chat.id, processing.message_id)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)

    text = box(
        "Player Information 👁✅",
        [
            ("Nickname", nickname),
            ("UID", data.get("uid", uid)),
            ("Region", data.get("region", region)),
            ("Level", data.get("level", "-")),
            ("Likes", data.get("likes", "-")),
            ("Success", data.get("success", "-")),
            ("Fail", data.get("fail", "-")),
        ],
        footer=f"{elapsed:.2f} seconds",
        highlight={"Likes", "Success"},
    )

    deliver_result(message.chat.id, processing.message_id, text, add_to_group_kb())
    bot.send_message(message.chat.id, "🎉 Visit sent successfully!")


# ------------------------------------------------------------------
# COMMAND: /auto IND {uid} {days} {name}   — OWNER ONLY (adds)
# ------------------------------------------------------------------
@bot.message_handler(commands=["auto"])
def cmd_auto(message):
    track_user(message.from_user)
    if not group_only(message):
        return
    track_group(message.chat)

    if not is_owner(message.from_user.id):
        deny_owner_only(message)
        return

    parts = message.text.split(maxsplit=4)[1:]
    if len(parts) != 4:
        bot.reply_to(
            message,
            "⚠️ Usage: <code>/auto IND 1234567890 7 MyName</code>\n(region, uid, days, name)",
        )
        return
    region, uid, days_raw, name = parts
    region = region.upper()
    if not UID_RE.match(uid) or not days_raw.isdigit():
        bot.reply_to(message, "⚠️ UID must be numeric and days must be a number.")
        return
    days = int(days_raw)

    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO autolikes (chat_id, uid, region, name, days_left, added_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (message.chat.id, uid, region, name, days, message.from_user.id,
             datetime.now(TZ).isoformat()),
        )
        conn.commit()

    h, m = get_setting("autolike_hour", "4"), get_setting("autolike_minute", "0")
    text = box(
        "Auto-Like Scheduled ⏰✅",
        [
            ("Name", name), ("UID", uid), ("Region", region), ("Days", days),
            ("Runs Daily At", f"{int(h):02d}:{int(m):02d} IST"),
        ],
    )
    bot.reply_to(message, text)


# ------------------------------------------------------------------
# AUTO-LIKE SCHEDULER JOB
# ------------------------------------------------------------------
def run_single_autolike(row):
    try:
        data, elapsed = call_like_api(row["uid"], row["region"])
        return row, data, elapsed, None
    except Exception as e:  # noqa: BLE001
        return row, None, None, e


def autolike_job():
    with db() as conn:
        rows = conn.execute("SELECT * FROM autolikes WHERE days_left > 0").fetchall()
    if not rows:
        return

    autolike_in_progress.set()
    try:
        log.info("Auto-like job starting for %d IDs", len(rows))
        futures = [autolike_executor.submit(run_single_autolike, r) for r in rows]

        to_delete, to_decrement = [], []
        for fut in as_completed(futures):
            row, data, elapsed, err = fut.result()
            chat_id = row["chat_id"]
            if err:
                log.error("Auto-like failed uid=%s region=%s: %s", row["uid"], row["region"], err)
                try:
                    bot.send_message(
                        chat_id,
                        f"❌ Auto-like failed for <b>{row['name']}</b> (UID {row['uid']}). Will retry tomorrow.",
                    )
                except Exception:  # noqa: BLE001
                    pass
            else:
                nickname = data.get("PlayerNickname", row["name"])
                given = data.get("LikesGivenByAPI", 0)
                text = box(
                    "Auto-Like Result ⏰✅",
                    [
                        ("Nickname", nickname), ("UID", data.get("UID", row["uid"])),
                        ("Region", data.get("PlayerRegion", row["region"])),
                        ("Level", data.get("PlayerLevel", "-")),
                        ("Likes", data.get("LikesafterCommand", "-")),
                        ("Given", given), ("Days Left", row["days_left"] - 1),
                    ],
                    footer=f"{elapsed:.2f} seconds",
                    highlight={"Likes", "Given"},
                )
                try:
                    bot.send_message(chat_id, text)
                except Exception as e:  # noqa: BLE001
                    log.error("Could not notify chat %s: %s", chat_id, e)

            new_days = row["days_left"] - 1
            (to_delete if new_days <= 0 else to_decrement).append(
                row["id"] if new_days <= 0 else (new_days, row["id"])
            )

        with _db_lock, db() as conn:
            conn.executemany("UPDATE autolikes SET days_left=? WHERE id=?", to_decrement)
            if to_delete:
                conn.executemany("DELETE FROM autolikes WHERE id=?", [(i,) for i in to_delete])
            conn.commit()
        log.info("Auto-like job finished.")
    finally:
        autolike_in_progress.clear()


# ------------------------------------------------------------------
# GROUP / CHANNEL TRACKING + OWNER DM ALERT ON NEW ADD
# ------------------------------------------------------------------
@bot.my_chat_member_handler()
def on_membership_change(update):
    chat = update.chat
    new_status = update.new_chat_member.status

    if chat.type == "channel":
        if new_status in ("administrator", "member"):
            with db() as conn:
                already = conn.execute(
                    "SELECT 1 FROM channels WHERE chat_id=?", (chat.id,)
                ).fetchone()
            track_channel(chat)
            if not already:
                _notify_owners_new_chat(chat, "channel")
        elif new_status in ("left", "kicked"):
            with _db_lock, db() as conn:
                conn.execute("DELETE FROM channels WHERE chat_id=?", (chat.id,))
                conn.commit()
        return

    if chat.type in ("group", "supergroup"):
        if new_status in ("member", "administrator"):
            with db() as conn:
                already = conn.execute(
                    "SELECT 1 FROM groups WHERE chat_id=?", (chat.id,)
                ).fetchone()
            track_group(chat)
            if not already:
                _notify_owners_new_chat(chat, "group")
        elif new_status in ("left", "kicked"):
            with _db_lock, db() as conn:
                conn.execute("DELETE FROM groups WHERE chat_id=?", (chat.id,))
                conn.commit()


def _notify_owners_new_chat(chat, kind):
    creator_info = "Unknown"
    invite_link = "N/A"
    try:
        admins = bot.get_chat_administrators(chat.id)
        for a in admins:
            if a.status == "creator":
                u = a.user
                creator_info = f"@{u.username}" if u.username else f"{u.first_name} (id: {u.id})"
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        invite_link = bot.export_chat_invite_link(chat.id)
    except Exception:  # noqa: BLE001
        pass

    text = box(
        f"New {kind.title()} Added 🔔",
        [("Title", chat.title or "-"), ("Chat ID", chat.id), ("Owner", creator_info),
         ("Link", invite_link)],
    )
    for oid in OWNER_IDS:
        try:
            bot.send_message(oid, text)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------
# ADMIN PANEL — reply keyboard (colored), owner-only, DM only
# ------------------------------------------------------------------
BTN_STATS       = "📊 Stats"
BTN_AUTOLIST    = "📋 Auto-Like List"
BTN_RM_AUTO     = "🗑 Remove Auto-Like"
BTN_SET_TIME    = "⏰ Set Auto-Like Time"
BTN_BAN         = "🚫 Ban User"
BTN_UNBAN       = "✅ Unban User"
BTN_RM_GROUP    = "➖ Remove From Group"
BTN_BROADCAST   = "📢 Broadcast"
BTN_MAINTENANCE = "🛠 Maintenance ON/OFF"
BTN_SET_IMAGE   = "🖼 Set Result Image"
BTN_SET_DENY    = "💬 Set Restricted Reply"
BTN_VERIFY_CH   = "📡 Verification Channels"
BTN_BACKUP_NOW  = "💾 Backup Now"
BTN_RESTORE_UP  = "📤 Restore From File"
BTN_CLOSE       = "🔙 Close Panel"

# In-memory multi-step state for owner DM flows: {user_id: {"action": "..."}}
admin_state = {}


def admin_panel_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(colored_button(BTN_STATS, style="primary"), colored_button(BTN_AUTOLIST, style="primary"))
    kb.row(colored_button(BTN_RM_AUTO, style="danger"), colored_button(BTN_SET_TIME, style="primary"))
    kb.row(colored_button(BTN_BAN, style="danger"), colored_button(BTN_UNBAN, style="success"))
    kb.row(colored_button(BTN_RM_GROUP, style="danger"), colored_button(BTN_BROADCAST, style="primary"))
    kb.row(colored_button(BTN_MAINTENANCE, style="danger"), colored_button(BTN_SET_IMAGE, style="primary"))
    kb.row(colored_button(BTN_SET_DENY, style="primary"), colored_button(BTN_VERIFY_CH, style="primary"))
    kb.row(colored_button(BTN_BACKUP_NOW, style="success"), colored_button(BTN_RESTORE_UP, style="danger"))
    kb.row(colored_button(BTN_CLOSE, style="danger"))
    return kb


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_owner(message.from_user.id):
        deny_owner_only(message)
        return
    if message.chat.type != "private":
        bot.reply_to(message, "🛠 Please DM me privately to use the admin panel.")
        return
    admin_state.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "🛠 <b>Admin Panel</b>", reply_markup=admin_panel_kb())


# ---- Individual button entry points (each sets up state if needed) ----
def _owner_dm(m):
    return m.chat.type == "private" and is_owner(m.from_user.id)


@bot.message_handler(func=lambda m: m.text == BTN_STATS and _owner_dm(m))
def panel_stats(message):
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        groups = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
        channels = conn.execute("SELECT COUNT(*) c FROM channels").fetchone()["c"]
        autos = conn.execute("SELECT COUNT(*) c FROM autolikes").fetchone()["c"]
        banned = conn.execute("SELECT COUNT(*) c FROM banned_users").fetchone()["c"]
    text = box("Bot Stats 📊", [
        ("Users", users), ("Groups", groups), ("Channels", channels),
        ("Active Auto-Likes", autos), ("Banned Users", banned),
    ])
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda m: m.text == BTN_AUTOLIST and _owner_dm(m))
def panel_autolikes(message):
    with db() as conn:
        rows = conn.execute("SELECT * FROM autolikes ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        bot.send_message(message.chat.id, "📋 No active auto-likes.")
        return
    lines = [f"• {r['name']} | UID {r['uid']} | {r['region']} | {r['days_left']}d left | chat {r['chat_id']}"
              for r in rows]
    bot.send_message(message.chat.id, "📋 <b>Auto-Likes (latest 30)</b>\n" + "\n".join(lines))


@bot.message_handler(func=lambda m: m.text == BTN_RM_AUTO and _owner_dm(m))
def panel_remove_auto_start(message):
    admin_state[message.from_user.id] = {"action": "remove_autolike"}
    bot.send_message(message.chat.id, "🗑 Send me the <b>UID</b> to remove auto-like for.")


@bot.message_handler(func=lambda m: m.text == BTN_SET_TIME and _owner_dm(m))
def panel_set_time_start(message):
    admin_state[message.from_user.id] = {"action": "set_autolike_time"}
    bot.send_message(message.chat.id, "⏰ Send the time in 24h <code>HH:MM</code> format, e.g. <code>04:00</code>")


@bot.message_handler(func=lambda m: m.text == BTN_BAN and _owner_dm(m))
def panel_ban_start(message):
    admin_state[message.from_user.id] = {"action": "ban_user"}
    bot.send_message(message.chat.id, "🚫 Send the numeric Telegram user ID to ban, or forward a message from them.")


@bot.message_handler(func=lambda m: m.text == BTN_UNBAN and _owner_dm(m))
def panel_unban_start(message):
    admin_state[message.from_user.id] = {"action": "unban_user"}
    bot.send_message(message.chat.id, "✅ Send the numeric Telegram user ID to unban, or forward a message from them.")


@bot.message_handler(func=lambda m: m.text == BTN_RM_GROUP and _owner_dm(m))
def panel_remove_group(message):
    with db() as conn:
        groups = conn.execute("SELECT * FROM groups ORDER BY title").fetchall()
        channels = conn.execute("SELECT * FROM channels ORDER BY title").fetchall()
    if not groups and not channels:
        bot.send_message(message.chat.id, "➖ No groups or channels tracked yet.")
        return
    kb = types.InlineKeyboardMarkup()
    for g in groups:
        kb.row(types.InlineKeyboardButton(f"👥 {g['title'] or g['chat_id']}", callback_data=f"leave:g:{g['chat_id']}"))
    for c in channels:
        kb.row(types.InlineKeyboardButton(f"📡 {c['title'] or c['chat_id']}", callback_data=f"leave:c:{c['chat_id']}"))
    bot.send_message(message.chat.id, "➖ Tap to leave / remove:", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("leave:"))
def cb_leave_chat(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "🚫 Not authorized.", show_alert=True)
        return
    _, kind, chat_id_s = call.data.split(":")
    chat_id = int(chat_id_s)
    table = "groups" if kind == "g" else "channels"
    try:
        bot.leave_chat(chat_id)
    except Exception as e:  # noqa: BLE001
        log.warning("leave_chat failed: %s", e)
    with _db_lock, db() as conn:
        conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
        conn.commit()
    bot.answer_callback_query(call.id, "✅ Left / removed.")
    try:
        bot.edit_message_text("✅ Removed.", call.message.chat.id, call.message.message_id)
    except Exception:  # noqa: BLE001
        pass


@bot.message_handler(func=lambda m: m.text == BTN_BROADCAST and _owner_dm(m))
def panel_broadcast_start(message):
    admin_state[message.from_user.id] = {"action": "broadcast_wait_content"}
    bot.send_message(message.chat.id, "📢 Send the message (text/photo/video/etc.) you want to broadcast "
                                       "to every group and channel where I'm admin.")


@bot.message_handler(func=lambda m: m.text == BTN_MAINTENANCE and _owner_dm(m))
def panel_toggle_maintenance(message):
    current = get_setting("maintenance", "0")
    new_val = "0" if current == "1" else "1"
    set_setting("maintenance", new_val)
    state_txt = "🟢 OFF (bot working normally)" if new_val == "0" else "🔴 ON (users blocked, owner still works)"
    bot.send_message(message.chat.id, f"🛠 Maintenance mode is now: {state_txt}")


@bot.message_handler(func=lambda m: m.text == BTN_SET_IMAGE and _owner_dm(m))
def panel_set_image_start(message):
    admin_state[message.from_user.id] = {"action": "set_result_image"}
    bot.send_message(message.chat.id, "🖼 Send a <b>photo</b> to use on every like/visit result, "
                                       "or type <code>clear</code> to remove it (results go back to plain text).")


@bot.message_handler(func=lambda m: m.text == BTN_SET_DENY and _owner_dm(m))
def panel_set_deny_start(message):
    admin_state[message.from_user.id] = {"action": "set_deny_reply"}
    bot.send_message(message.chat.id, "💬 Send the text / photo / video / sticker you want shown to "
                                       "non-owners who try owner-only commands. Type <code>clear</code> to reset.")


@bot.message_handler(func=lambda m: m.text == BTN_VERIFY_CH and _owner_dm(m))
def panel_verify_channels_start(message):
    admin_state[message.from_user.id] = {"action": "verify_channels_edit"}
    channels = get_verification_channels()
    listing = "\n".join(f"• {c['name']} — {c['username']}" for c in channels) or "(none)"
    bot.send_message(
        message.chat.id,
        f"📡 <b>Current channels:</b>\n{listing}\n\n"
        "Send <code>@username</code> to ADD, <code>remove @username</code> to REMOVE, "
        "or <code>done</code> to finish.",
    )


@bot.message_handler(func=lambda m: m.text == BTN_BACKUP_NOW and _owner_dm(m))
def panel_backup_now(message):
    bot.send_message(message.chat.id, "💾 Backing up now…")
    ok, detail = backup_db_job()
    icon = "✅" if ok else "❌"
    bot.send_message(message.chat.id, f"{icon} {detail}")


@bot.message_handler(func=lambda m: m.text == BTN_RESTORE_UP and _owner_dm(m))
def panel_restore_upload_start(message):
    admin_state[message.from_user.id] = {"action": "restore_backup_upload"}
    bot.send_message(
        message.chat.id,
        "📤 Send me the backup <b>.db file</b> as a document — the exact same "
        "file I post in the backup channel (not a photo, send it as 'File'/'Document'). "
        "I'll validate it and switch to it immediately, no restart needed.",
    )


@bot.message_handler(func=lambda m: m.text == BTN_CLOSE and _owner_dm(m))
def panel_close(message):
    admin_state.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "🔙 Panel closed.", reply_markup=types.ReplyKeyboardRemove())


# ------------------------------------------------------------------
# GENERIC HANDLER FOR MULTI-STEP ADMIN FLOWS (owner DM only)
# ------------------------------------------------------------------
_PANEL_BUTTON_TEXTS = {
    BTN_STATS, BTN_AUTOLIST, BTN_RM_AUTO, BTN_SET_TIME, BTN_BAN, BTN_UNBAN,
    BTN_RM_GROUP, BTN_BROADCAST, BTN_MAINTENANCE, BTN_SET_IMAGE, BTN_SET_DENY,
    BTN_VERIFY_CH, BTN_BACKUP_NOW, BTN_RESTORE_UP, BTN_CLOSE,
}


@bot.message_handler(
    func=lambda m: _owner_dm(m) and m.from_user.id in admin_state
    and not (m.content_type == "text" and (m.text.startswith("/") or m.text in _PANEL_BUTTON_TEXTS)),
    content_types=["text", "photo", "video", "animation", "document", "sticker", "voice", "audio"],
)
def owner_flow_handler(message):
    uid = message.from_user.id
    state = admin_state.get(uid)
    if not state:
        return
    action = state["action"]

    # ---- remove auto-like by UID ----
    if action == "remove_autolike":
        text = (message.text or "").strip()
        if not UID_RE.match(text):
            bot.reply_to(message, "⚠️ That doesn't look like a valid UID. Send digits only.")
            return
        with _db_lock, db() as conn:
            rows = conn.execute("SELECT id, chat_id, name FROM autolikes WHERE uid=?", (text,)).fetchall()
            conn.execute("DELETE FROM autolikes WHERE uid=?", (text,))
            conn.commit()
        admin_state.pop(uid, None)
        if rows:
            names = ", ".join(f"{r['name']} (chat {r['chat_id']})" for r in rows)
            bot.reply_to(message, f"🗑 Removed auto-like for UID <code>{text}</code>: {names}")
        else:
            bot.reply_to(message, f"❌ No auto-like found for UID <code>{text}</code>.")
        return

    # ---- set auto-like time ----
    if action == "set_autolike_time":
        text = (message.text or "").strip()
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text)
        if not m:
            bot.reply_to(message, "⚠️ Invalid format. Send as <code>HH:MM</code>, e.g. <code>04:00</code>.")
            return
        h, mi = int(m.group(1)), int(m.group(2))
        set_setting("autolike_hour", str(h))
        set_setting("autolike_minute", str(mi))
        try:
            scheduler.reschedule_job("autolike_job", trigger="cron", hour=h, minute=mi, timezone=TZ)
        except Exception as e:  # noqa: BLE001
            log.error("Could not reschedule autolike job: %s", e)
        admin_state.pop(uid, None)
        bot.reply_to(message, f"⏰ Auto-like will now run daily at <b>{h:02d}:{mi:02d} IST</b>.")
        return

    # ---- ban / unban ----
    if action in ("ban_user", "unban_user"):
        target = None
        if message.forward_from:
            target = message.forward_from.id
        elif message.text and message.text.strip().isdigit():
            target = int(message.text.strip())
        if target is None:
            bot.reply_to(message, "⚠️ Send a numeric user ID, or forward a message from that user.")
            return
        admin_state.pop(uid, None)
        if action == "ban_user":
            ban_user(target)
            bot.reply_to(message, f"🚫 User <code>{target}</code> is now banned.")
        else:
            unban_user(target)
            bot.reply_to(message, f"✅ User <code>{target}</code> is now unbanned.")
        return

    # ---- broadcast: capture content ----
    if action == "broadcast_wait_content":
        admin_state[uid] = {
            "action": "broadcast_confirm",
            "src_chat": message.chat.id,
            "src_msg": message.message_id,
        }
        kb = types.InlineKeyboardMarkup()
        kb.row(
            colored_button("✅ Yes, send it", style="success", callback_data="bc:yes"),
            colored_button("❌ No, cancel", style="danger", callback_data="bc:no"),
        )
        bot.send_message(message.chat.id, "📢 Preview above ⬆️ — send this broadcast to all admin groups/channels?",
                          reply_markup=kb)
        return

    # ---- restore database from an uploaded backup file ----
    if action == "restore_backup_upload":
        if message.content_type != "document":
            bot.reply_to(message, "⚠️ Please send it as a <b>Document/File</b>, not a photo or text.")
            return
        try:
            file_info = bot.get_file(message.document.file_id)
            raw = bot.download_file(file_info.file_path)
        except Exception as e:  # noqa: BLE001
            bot.reply_to(message, f"❌ Couldn't download that file: {e}")
            return

        tmp_path = "restore_upload_tmp.db"
        with open(tmp_path, "wb") as f:
            f.write(raw)

        # validate it's actually a usable sqlite backup before swapping in
        try:
            test_conn = sqlite3.connect(tmp_path)
            tables = {r[0] for r in test_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            test_conn.close()
            if "settings" not in tables or "users" not in tables:
                os.remove(tmp_path)
                bot.reply_to(message, "❌ That file doesn't look like a valid backup for this bot "
                                       "(missing expected tables). Nothing was changed.")
                return
        except Exception as e:  # noqa: BLE001
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            bot.reply_to(message, f"❌ That file isn't a valid database: {e}")
            return

        with _db_lock:
            for suffix in ("", "-wal", "-shm"):
                p = DB_PATH + suffix
                if os.path.exists(p):
                    os.remove(p)
            os.replace(tmp_path, DB_PATH)
        init_db()  # make sure any new tables/settings this bot version needs still exist
        admin_state.pop(uid, None)
        bot.reply_to(message, "✅ Database restored from your uploaded file — live now, no restart needed.")
        return

    # ---- set result image ----
    if action == "set_result_image":
        if message.content_type == "photo":
            file_id = message.photo[-1].file_id
            set_setting("result_image_file_id", file_id)
            admin_state.pop(uid, None)
            bot.reply_to(message, "🖼 Result image saved — it'll now appear on every like/visit result.")
            return
        if message.text and message.text.strip().lower() == "clear":
            set_setting("result_image_file_id", "")
            admin_state.pop(uid, None)
            bot.reply_to(message, "🖼 Result image cleared — results go back to plain text.")
            return
        bot.reply_to(message, "⚠️ Send a photo, or type <code>clear</code>.")
        return

    # ---- set restricted-command denial reply ----
    if action == "set_deny_reply":
        if message.text and message.text.strip().lower() == "clear":
            set_setting("deny_msg_type", "")
            set_setting("deny_msg_text", "")
            set_setting("deny_msg_file_id", "")
            set_setting("deny_msg_caption", "")
            admin_state.pop(uid, None)
            bot.reply_to(message, "💬 Restricted-command reply reset to default.")
            return
        ct = message.content_type
        if ct == "text":
            set_setting("deny_msg_type", "text")
            set_setting("deny_msg_text", message.text)
        elif ct in ("photo", "video", "animation", "document", "voice", "sticker"):
            file_id = {
                "photo": lambda: message.photo[-1].file_id,
                "video": lambda: message.video.file_id,
                "animation": lambda: message.animation.file_id,
                "document": lambda: message.document.file_id,
                "voice": lambda: message.voice.file_id,
                "sticker": lambda: message.sticker.file_id,
            }[ct]()
            set_setting("deny_msg_type", ct)
            set_setting("deny_msg_file_id", file_id)
            set_setting("deny_msg_caption", message.caption or "")
        else:
            bot.reply_to(message, "⚠️ Unsupported type. Send text, photo, video, sticker, or type clear.")
            return
        admin_state.pop(uid, None)
        bot.reply_to(message, "💬 Restricted-command reply saved.")
        return

    # ---- verification channel editor (stays active until 'done') ----
    if action == "verify_channels_edit":
        text = (message.text or "").strip()
        low = text.lower()
        if low == "done":
            admin_state.pop(uid, None)
            bot.reply_to(message, "📡 Done editing verification channels.")
            return
        if low.startswith("remove "):
            uname = text.split(maxsplit=1)[1].strip()
            if not uname.startswith("@"):
                uname = "@" + uname
            with _db_lock, db() as conn:
                conn.execute("DELETE FROM verification_channels WHERE lower(username)=lower(?)", (uname,))
                conn.commit()
            bot.reply_to(message, f"➖ Removed {uname} (if it existed). Send more, or 'done'.")
            return
        if text.startswith("@"):
            name = text.lstrip("@")
            with _db_lock, db() as conn:
                conn.execute(
                    "INSERT INTO verification_channels (name, username, added_at) VALUES (?,?,?) "
                    "ON CONFLICT(username) DO NOTHING",
                    (name, text, datetime.now(TZ).isoformat()),
                )
                conn.commit()
            bot.reply_to(message, f"➕ Added {text}. Send more, or 'done'.")
            return
        bot.reply_to(message, "⚠️ Send <code>@username</code> to add, <code>remove @username</code> to remove, "
                               "or <code>done</code> to finish.")
        return


@bot.callback_query_handler(func=lambda c: c.data in ("bc:yes", "bc:no"))
def cb_broadcast_confirm(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "🚫 Not authorized.", show_alert=True)
        return
    state = admin_state.get(uid)
    if not state or state.get("action") != "broadcast_confirm":
        bot.answer_callback_query(call.id, "⚠️ Nothing pending.")
        return
    admin_state.pop(uid, None)

    if call.data == "bc:no":
        bot.answer_callback_query(call.id, "❌ Cancelled.")
        bot.edit_message_text("❌ Broadcast cancelled.", call.message.chat.id, call.message.message_id)
        return

    bot.answer_callback_query(call.id, "📢 Sending…")
    bot.edit_message_text("📢 Sending broadcast…", call.message.chat.id, call.message.message_id)

    src_chat, src_msg = state["src_chat"], state["src_msg"]
    with db() as conn:
        targets = (
            [("group", r["chat_id"]) for r in conn.execute("SELECT chat_id FROM groups").fetchall()]
            + [("channel", r["chat_id"]) for r in conn.execute("SELECT chat_id FROM channels").fetchall()]
        )

    sent, skipped, failed = 0, 0, 0
    for kind, chat_id in targets:
        try:
            member = bot.get_chat_member(chat_id, BOT_ID)
            if member.status not in ("administrator", "creator"):
                skipped += 1
                continue
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        try:
            bot.copy_message(chat_id, src_chat, src_msg)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        time.sleep(0.05)

    bot.send_message(
        call.message.chat.id,
        f"✅ Broadcast finished.\nSent: {sent}   Skipped (not admin): {skipped}   Failed: {failed}",
    )


# ------------------------------------------------------------------
# SCHEDULER
# ------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=TZ)

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    restore_db_from_backup()
    init_db()

    h = int(get_setting("autolike_hour", str(DEFAULT_AUTOLIKE_HOUR)))
    m = int(get_setting("autolike_minute", str(DEFAULT_AUTOLIKE_MINUTE)))
    scheduler.add_job(autolike_job, "cron", hour=h, minute=m, id="autolike_job", misfire_grace_time=30)
    scheduler.add_job(backup_db_job, "interval", minutes=BACKUP_INTERVAL_MIN, id="backup_job")
    scheduler.start()

    log.info("Bot started as @%s (threads=%d)", BOT_USERNAME, BOT_THREADS)
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:  # noqa: BLE001
            log.error("Polling crashed, restarting in 5s: %s", e)
            time.sleep(5)
