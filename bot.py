#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================
  FREE FIRE LIKE / VISIT BOT  —  single file (bot.py)
  Built for Railway deployment (Procfile + requirements.txt
  are in the same package).
============================================================

WHAT YOU MUST SET (Railway → Variables tab):
  BOT_TOKEN          -> token from @BotFather                (required)
  OWNER_IDS          -> your Telegram numeric user id(s),
                         comma separated if more than one     (required)
  BACKUP_CHANNEL_ID  -> numeric id of a channel where the bot
                         is ADMIN (used for DB auto-backup)    (required)

Everything else (the 4 verification channels, the 2 APIs,
timings, limits) is already filled in below exactly as you
asked. Search for "### CONFIG" to jump straight to it.
"""

import os
import re
import io
import time
import sqlite3
import logging
import threading
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

# ------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ff-bot")

# ------------------------------------------------------------------
### CONFIG ----------------------------------------------------------
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var is missing. Set it in Railway → Variables.")

# Hardcoded fallback owner — always treated as bot owner no matter what
# Railway's OWNER_IDS variable is set to (or even if it's missing/wrong).
HARDCODED_OWNER_IDS = {7892255798}


def _parse_owner_ids(raw: str):
    ids = set()
    for token in raw.replace("\n", ",").replace(" ", "").split(","):
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            log.warning("OWNER_IDS: ignoring invalid entry %r (must be numeric)", token)
    return ids


OWNER_IDS = _parse_owner_ids(os.environ.get("OWNER_IDS", "")) | HARDCODED_OWNER_IDS
log.info("Loaded OWNER_IDS: %s", OWNER_IDS)

BACKUP_CHANNEL_ID = os.environ.get("BACKUP_CHANNEL_ID", "").strip()
BACKUP_CHANNEL_ID = int(BACKUP_CHANNEL_ID) if BACKUP_CHANNEL_ID else None

# The 4 channels a user must join before the bot works for them
VERIFY_CHANNELS = [
    {"name": "SRK ERA",        "username": "@SRK_ERA"},
    {"name": "SRK IMPORTANT",  "username": "@SRK_IMP1"},
    {"name": "SN NETWORK",     "username": "@snnetwork7"},
    {"name": "SNxHUB",          "username": "@snxhub"},
]

# The two third-party APIs
LIKE_API_URL  = "https://srk-like-api.vercel.app/like?uid={uid}&server_name={region}"
VISIT_API_URL = "https://visit-api-10k.vercel.app/{region}/{uid}"

# Limits
LIKE_LIMIT_PER_DAY   = 1        # per user, per day
VISIT_COOLDOWN_SECS  = 25       # per user, between /visit uses

# Auto-like: fires every day at this time (Asia/Kolkata)
AUTOLIKE_HOUR   = 4
AUTOLIKE_MINUTE = 0
AUTOLIKE_WORKERS = 25           # parallel workers so all IDs fire almost together

# DB auto-backup interval (minutes)
BACKUP_INTERVAL_MIN = 30

DB_PATH = "bot_data.db"
TZ = ZoneInfo("Asia/Kolkata")

# ------------------------------------------------------------------
# BOT INIT
# ------------------------------------------------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
BOT_USERNAME = bot.get_me().username
executor = ThreadPoolExecutor(max_workers=AUTOLIKE_WORKERS)

# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TEXT
            );
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                added_at TEXT
            );
            CREATE TABLE IF NOT EXISTS like_usage (
                user_id INTEGER,
                usage_date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            );
            CREATE TABLE IF NOT EXISTS visit_cooldown (
                user_id INTEGER PRIMARY KEY,
                last_used REAL
            );
            CREATE TABLE IF NOT EXISTS autolikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                uid TEXT,
                region TEXT,
                name TEXT,
                days_left INTEGER,
                added_by INTEGER,
                created_at TEXT
            );
            """
        )
        conn.commit()


def restore_db_from_backup():
    """On startup, pull the newest backup file from the backup channel
    (it is kept pinned there) so a Railway redeploy never loses data."""
    if not BACKUP_CHANNEL_ID:
        log.warning("BACKUP_CHANNEL_ID not set — skipping restore.")
        return
    try:
        chat = bot.get_chat(BACKUP_CHANNEL_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            log.info("No pinned backup found in backup channel — starting fresh.")
            return
        file_info = bot.get_file(pinned.document.file_id)
        data = bot.download_file(file_info.file_path)
        with open(DB_PATH, "wb") as f:
            f.write(data)
        log.info("Database restored from backup channel pinned message.")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not restore backup (probably first run): %s", e)


def backup_db_job():
    if not BACKUP_CHANNEL_ID:
        return
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
        bot.pin_chat_message(BACKUP_CHANNEL_ID, msg.message_id, disable_notification=True)
        os.remove("backup_tmp.db")
        log.info("Database backed up successfully.")
    except Exception as e:  # noqa: BLE001
        log.error("Backup failed: %s", e)


# ------------------------------------------------------------------
# STYLE HELPERS  (same "font"/box style everywhere, left border only)
# ------------------------------------------------------------------
def box(title, rows, footer=None):
    """rows: list of (label, value) tuples."""
    lines = [f"┌ {title}"]
    for label, value in rows:
        lines.append(f"├─ {label}: {value}")
    if footer:
        lines.append(f"└─ {footer}")
    else:
        last_label, last_value = rows[-1]
        lines[-1] = f"└─ {last_label}: {last_value}"
    return "\n".join(lines)


def divider():
    return "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"


# ------------------------------------------------------------------
# LIVE "PROCESSING…" ANIMATION
# Keeps editing the placeholder message every few seconds while we
# wait on the (sometimes slow, 30-60s) third-party API, so it never
# looks stuck/dead during the wait.
# ------------------------------------------------------------------
_ANIM_FRAMES = ["⏳", "⌛"]
_ANIM_DOTS = ["", ".", "..", "..."]


def start_processing_animation(chat_id, message_id, uid):
    stop_event = threading.Event()

    def _loop():
        i = 0
        while not stop_event.wait(3):
            i += 1
            frame = _ANIM_FRAMES[i % len(_ANIM_FRAMES)]
            dots = _ANIM_DOTS[i % len(_ANIM_DOTS)]
            try:
                bot.edit_message_text(
                    f"{frame} <b>Processing{dots}</b>\n"
                    f"🎮 UID: <code>{uid}</code>\n"
                    "<i>Hang tight, this can take up to a minute.</i>",
                    chat_id, message_id,
                )
            except Exception:  # noqa: BLE001
                pass  # message unchanged / edited elsewhere — ignore

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop_event, thread


def stop_processing_animation(stop_event, thread):
    stop_event.set()
    thread.join(timeout=1)


# ------------------------------------------------------------------
# API CALLS
# ------------------------------------------------------------------
def call_like_api(uid: str, region: str, timeout=90):
    url = LIKE_API_URL.format(uid=uid, region=region.lower())
    t0 = time.time()
    r = requests.get(url, timeout=timeout)
    elapsed = time.time() - t0
    r.raise_for_status()
    return r.json(), elapsed


def call_visit_api(uid: str, region: str, timeout=90):
    url = VISIT_API_URL.format(uid=uid, region=region.lower())
    t0 = time.time()
    r = requests.get(url, timeout=timeout)
    elapsed = time.time() - t0
    r.raise_for_status()
    return r.json(), elapsed


# ------------------------------------------------------------------
# USER / GROUP TRACKING (needed for broadcast + auto-like)
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


# ------------------------------------------------------------------
# VERIFICATION (4 channels)
# ------------------------------------------------------------------
def colored_button(text, style=None, **kwargs):
    """InlineKeyboardButton / KeyboardButton wrapper that adds Bot API 9.4
    colored `style` ('primary' blue / 'success' green / 'danger' red).
    Falls back to a plain button if the running telebot version or the
    user's Telegram client is too old to understand `style` (older
    clients silently ignore the field, so this is always safe)."""
    is_inline = "callback_data" in kwargs or "url" in kwargs
    cls = types.InlineKeyboardButton if is_inline else types.KeyboardButton
    try:
        return cls(text, style=style, **kwargs) if style else cls(text, **kwargs)
    except TypeError:
        # installed telebot version predates Bot API 9.4 support
        return cls(text, **kwargs)


def is_verified(user_id: int) -> bool:
    for ch in VERIFY_CHANNELS:
        try:
            member = bot.get_chat_member(ch["username"], user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:  # noqa: BLE001
            return False
    return True


def verify_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for ch in VERIFY_CHANNELS:
        link = f"https://t.me/{ch['username'].lstrip('@')}"
        buttons.append(colored_button(f"📢 {ch['name']}", style="primary", url=link))
    kb.add(*buttons)
    kb.add(colored_button("✅ I've Joined — Check Again", style="success",
                           callback_data="check_verify"))
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
def group_only(message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        kb = types.InlineKeyboardMarkup()
        kb.add(colored_button(
            "➕ ADD ME TO YOUR GROUP",
            style="success",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
        ))
        bot.send_message(
            message.chat.id,
            "🚫 This bot only works inside <b>groups</b>, not in private chat.\n"
            "Add me to your group to use like/visit commands 👇",
            reply_markup=kb,
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


# ------------------------------------------------------------------
# ADD-TO-GROUP BUTTON (attached under every like/visit result)
# ------------------------------------------------------------------
def add_to_group_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(colored_button(
        "➕ ADD ME TO YOUR GROUP",
        style="success",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
    ))
    return kb


# ------------------------------------------------------------------
# COMMAND: /myid  — debug helper, works everywhere, no restrictions
# ------------------------------------------------------------------
@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    uid = message.from_user.id
    status = "✅ YES — you are recognized as owner" if is_owner(uid) else "❌ NO — not in OWNER_IDS"
    bot.reply_to(
        message,
        f"🆔 Your Telegram ID: <code>{uid}</code>\n"
        f"👑 Owner status: {status}\n\n"
        "⚠️ If this was sent while a group's <b>Remain Anonymous</b> admin "
        "mode is ON, Telegram hides your real ID and this will always show "
        "as not-owner. Turn that off, or use /myid in DM with the bot instead.",
    )


# ------------------------------------------------------------------
# COMMAND: /start
# ------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    track_user(message.from_user)
    if message.chat.type in ("group", "supergroup"):
        track_group(message.chat)
    kb = types.InlineKeyboardMarkup()
    kb.add(colored_button(
        "➕ ADD ME TO YOUR GROUP",
        style="success",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
    ))
    bot.send_message(
        message.chat.id,
        "👋 <b>Welcome!</b>\n"
        + divider()
        + "\n⚡️ Free Fire Like &amp; Visit Bot\n"
        "🎯 Works only inside groups.\n"
        "📌 Commands:\n"
        "   /like IND {uid}\n"
        "   /visit IND {uid}\n",
        reply_markup=kb,
    )


# ------------------------------------------------------------------
# CALLBACK: verification recheck
# ------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "check_verify")
def cb_check_verify(call):
    if is_verified(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Verification successful!")
        try:
            bot.edit_message_text(
                "✅ <b>Verified!</b> You can now use /like or /visit in the group.",
                call.message.chat.id, call.message.message_id,
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        bot.answer_callback_query(
            call.id, "❌ You haven't joined all channels yet.", show_alert=True
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

    if not is_verified(message.from_user.id):
        send_verification_prompt(message.chat.id)
        return

    args = message.text.split()[1:]
    region, uid, err = parse_region_uid(args)
    if err:
        bot.reply_to(message, err)
        return

    user_id = message.from_user.id
    today = date.today().isoformat()
    if not is_owner(user_id):  # owners/admins have unlimited /like — no daily cap
        with _db_lock, db() as conn:
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

    processing = bot.reply_to(message, f"⏳ <b>Processing…</b>\n🎮 UID: <code>{uid}</code>")
    anim_stop, anim_thread = start_processing_animation(message.chat.id, processing.message_id, uid)

    try:
        data, elapsed = call_like_api(uid, region)
    except requests.exceptions.Timeout:
        log.error("Like API timed out for uid=%s region=%s", uid, region)
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text(
            "⌛ The like server is taking too long to respond. It may still be "
            "processing in the background — please check again in a minute "
            "before retrying.",
            message.chat.id, processing.message_id)
        return
    except Exception as e:  # noqa: BLE001
        log.error("Like API call failed for uid=%s region=%s: %s", uid, region, e)
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text("❌ Something went wrong, please try again later.",
                               message.chat.id, processing.message_id)
        return

    stop_processing_animation(anim_stop, anim_thread)
    nickname = data.get("PlayerNickname", "Unknown")
    try:
        bot.edit_message_text(
            f"⏳ <b>Processing…</b>\n🎮 Player: <b>{nickname}</b>",
            message.chat.id, processing.message_id,
        )
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.6)  # small beat so the two-step "name reveal" feels premium

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
        footer=f"⏱ Time Taken: {elapsed:.2f} seconds",
    )
    if given == 0:
        text += "\n\n⚠️ <i>This UID already reached max likes for today.</i>"
    else:
        # only consume the user's daily allowance when a like was ACTUALLY given
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO like_usage (user_id, usage_date, count) VALUES (?,?,1) "
                "ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1",
                (user_id, today),
            )
            conn.commit()

    try:
        bot.edit_message_text(text, message.chat.id, processing.message_id,
                               reply_markup=add_to_group_kb())
    except Exception:  # noqa: BLE001
        bot.send_message(message.chat.id, text, reply_markup=add_to_group_kb())
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

    if not is_verified(message.from_user.id):
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
        if row and now - row["last_used"] < VISIT_COOLDOWN_SECS:
            wait = int(VISIT_COOLDOWN_SECS - (now - row["last_used"]))
            bot.reply_to(message, f"⏳ Please wait <b>{wait}s</b> before using /visit again.")
            return
        conn.execute(
            "INSERT INTO visit_cooldown (user_id, last_used) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_used=?",
            (user_id, now, now),
        )
        conn.commit()

    processing = bot.reply_to(message, f"⏳ <b>Processing…</b>\n🎮 UID: <code>{uid}</code>")
    anim_stop, anim_thread = start_processing_animation(message.chat.id, processing.message_id, uid)

    try:
        data, elapsed = call_visit_api(uid, region)
    except requests.exceptions.Timeout:
        log.error("Visit API timed out for uid=%s region=%s", uid, region)
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text(
            "⌛ The visit server is taking too long to respond. It may still be "
            "processing in the background — please check again in a minute "
            "before retrying.",
            message.chat.id, processing.message_id)
        return
    except Exception as e:  # noqa: BLE001
        log.error("Visit API call failed for uid=%s region=%s: %s", uid, region, e)
        stop_processing_animation(anim_stop, anim_thread)
        bot.edit_message_text("❌ Something went wrong, please try again later.",
                               message.chat.id, processing.message_id)
        return

    stop_processing_animation(anim_stop, anim_thread)
    nickname = data.get("nickname", "Unknown")
    try:
        bot.edit_message_text(
            f"⏳ <b>Processing…</b>\n🎮 Player: <b>{nickname}</b>",
            message.chat.id, processing.message_id,
        )
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.6)

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
        footer=f"⏱ Time Taken: {elapsed:.2f} seconds",
    )

    try:
        bot.edit_message_text(text, message.chat.id, processing.message_id,
                               reply_markup=add_to_group_kb())
    except Exception:  # noqa: BLE001
        bot.send_message(message.chat.id, text, reply_markup=add_to_group_kb())
    bot.send_message(message.chat.id, "🎉 Visit sent successfully!")


# ------------------------------------------------------------------
# COMMAND: /auto IND {uid} {days} {name}   (group admins only)
# ------------------------------------------------------------------
@bot.message_handler(commands=["auto"])
def cmd_auto(message):
    track_user(message.from_user)
    if not group_only(message):
        return
    track_group(message.chat)

    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Only the bot owner can set auto-like.")
        return

    parts = message.text.split(maxsplit=4)[1:]
    if len(parts) != 4:
        bot.reply_to(
            message,
            "⚠️ Usage: <code>/auto IND 1234567890 7 MyName</code>\n"
            "(region, uid, days, name)",
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

    text = box(
        "Auto-Like Scheduled ⏰✅",
        [
            ("Name", name),
            ("UID", uid),
            ("Region", region),
            ("Days", days),
            ("Runs Daily At", f"{AUTOLIKE_HOUR:02d}:{AUTOLIKE_MINUTE:02d} IST"),
        ],
    )
    bot.reply_to(message, text)


# ------------------------------------------------------------------
# COMMAND: /unauto {uid}   — remove an auto-like (group admins only)
# ------------------------------------------------------------------
@bot.message_handler(commands=["unauto"])
def cmd_unauto(message):
    track_user(message.from_user)
    if not group_only(message):
        return
    track_group(message.chat)

    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Only the bot owner can remove auto-like.")
        return

    parts = message.text.split()[1:]
    if len(parts) != 1 or not UID_RE.match(parts[0]):
        bot.reply_to(message, "⚠️ Usage: <code>/unauto 1234567890</code>")
        return
    uid = parts[0]

    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT id, name FROM autolikes WHERE chat_id=? AND uid=?",
            (message.chat.id, uid),
        ).fetchone()
        if not row:
            bot.reply_to(message, f"❌ No active auto-like found for UID <code>{uid}</code> in this group.")
            return
        conn.execute("DELETE FROM autolikes WHERE id=?", (row["id"],))
        conn.commit()

    bot.reply_to(message, f"🗑 Auto-like removed for <b>{row['name']}</b> (UID <code>{uid}</code>).")


# ------------------------------------------------------------------
# AUTO-LIKE SCHEDULER JOB — fires every day at AUTOLIKE_HOUR:MINUTE
# ------------------------------------------------------------------
def run_single_autolike(row):
    try:
        data, elapsed = call_like_api(row["uid"], row["region"])
        return row, data, elapsed, None
    except Exception as e:  # noqa: BLE001
        return row, None, None, e


def autolike_job():
    with _db_lock, db() as conn:
        rows = conn.execute("SELECT * FROM autolikes WHERE days_left > 0").fetchall()

    if not rows:
        return

    log.info("Auto-like job starting for %d IDs", len(rows))
    futures = [executor.submit(run_single_autolike, r) for r in rows]

    to_delete = []
    to_decrement = []
    for fut in as_completed(futures):
        row, data, elapsed, err = fut.result()
        chat_id = row["chat_id"]
        if err:
            log.error("Auto-like failed for uid=%s region=%s: %s",
                      row["uid"], row["region"], err)
            bot.send_message(
                chat_id,
                f"❌ Auto-like failed for <b>{row['name']}</b> (UID {row['uid']}). "
                "Will retry tomorrow.",
            )
        else:
            nickname = data.get("PlayerNickname", row["name"])
            given = data.get("LikesGivenByAPI", 0)
            text = box(
                "Auto-Like Result ⏰✅",
                [
                    ("Nickname", nickname),
                    ("UID", data.get("UID", row["uid"])),
                    ("Region", data.get("PlayerRegion", row["region"])),
                    ("Level", data.get("PlayerLevel", "-")),
                    ("Likes", data.get("LikesafterCommand", "-")),
                    ("Given", given),
                    ("Days Left", row["days_left"] - 1),
                ],
                footer=f"⏱ Time Taken: {elapsed:.2f} seconds",
            )
            try:
                bot.send_message(chat_id, text)
            except Exception as e:  # noqa: BLE001
                log.error("Could not notify chat %s: %s", chat_id, e)

        new_days = row["days_left"] - 1
        if new_days <= 0:
            to_delete.append(row["id"])
        else:
            to_decrement.append((new_days, row["id"]))

    with _db_lock, db() as conn:
        conn.executemany("UPDATE autolikes SET days_left=? WHERE id=?", to_decrement)
        if to_delete:
            conn.executemany("DELETE FROM autolikes WHERE id=?", [(i,) for i in to_delete])
        conn.commit()
    log.info("Auto-like job finished.")


# ------------------------------------------------------------------
# COMMAND: /broadcast  (owner only)
# ------------------------------------------------------------------
@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not is_owner(message.from_user.id):
        return
    text = message.text.partition(" ")[2].strip()
    if not text and not message.reply_to_message:
        bot.reply_to(message, "⚠️ Usage: <code>/broadcast your message</code> "
                               "or reply to a message with /broadcast.")
        return

    with db() as conn:
        user_ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users")]
        group_ids = [r["chat_id"] for r in conn.execute("SELECT chat_id FROM groups")]

    sent, failed = 0, 0
    targets = user_ids + group_ids
    status = bot.reply_to(message, f"📢 Broadcasting to {len(targets)} chats…")

    for cid in targets:
        try:
            if message.reply_to_message:
                bot.copy_message(cid, message.chat.id, message.reply_to_message.message_id)
            else:
                bot.send_message(cid, f"📢 <b>Announcement</b>\n{divider()}\n{text}")
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        time.sleep(0.05)  # avoid hitting Telegram flood limits

    bot.edit_message_text(
        f"✅ Broadcast complete.\nSent: {sent}   Failed: {failed}",
        message.chat.id, status.message_id,
    )


# ------------------------------------------------------------------
# ADMIN PANEL (reply/custom keyboard — NOT inline, per your request)
# ------------------------------------------------------------------
PANEL_BTN_STATS   = "📊 Stats"
PANEL_BTN_BACKUP  = "💾 Backup Now"
PANEL_BTN_AUTOS   = "📋 Auto-Like List"
PANEL_BTN_CLOSE   = "🔙 Close Panel"


def admin_panel_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(colored_button(PANEL_BTN_STATS, style="primary"),
           colored_button(PANEL_BTN_AUTOS, style="primary"))
    kb.row(colored_button(PANEL_BTN_BACKUP, style="success"),
           colored_button(PANEL_BTN_CLOSE, style="danger"))
    return kb


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_owner(message.from_user.id):
        return
    bot.send_message(message.chat.id, "🛠 <b>Admin Panel</b>", reply_markup=admin_panel_kb())


@bot.message_handler(func=lambda m: m.text == PANEL_BTN_STATS and is_owner(m.from_user.id))
def panel_stats(message):
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        groups = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
        autos = conn.execute("SELECT COUNT(*) c FROM autolikes").fetchone()["c"]
    text = box("Bot Stats 📊", [
        ("Users", users), ("Groups", groups), ("Active Auto-Likes", autos),
    ])
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda m: m.text == PANEL_BTN_AUTOS and is_owner(m.from_user.id))
def panel_autolikes(message):
    with db() as conn:
        rows = conn.execute("SELECT * FROM autolikes ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        bot.send_message(message.chat.id, "📋 No active auto-likes.")
        return
    lines = [f"• {r['name']} | UID {r['uid']} | {r['region']} | {r['days_left']}d left"
              for r in rows]
    bot.send_message(message.chat.id, "📋 <b>Auto-Likes (latest 20)</b>\n" + "\n".join(lines))


@bot.message_handler(func=lambda m: m.text == PANEL_BTN_BACKUP and is_owner(m.from_user.id))
def panel_backup(message):
    bot.send_message(message.chat.id, "💾 Backing up now…")
    backup_db_job()
    bot.send_message(message.chat.id, "✅ Backup sent to backup channel.")


@bot.message_handler(func=lambda m: m.text == PANEL_BTN_CLOSE and is_owner(m.from_user.id))
def panel_close(message):
    bot.send_message(message.chat.id, "🔙 Panel closed.", reply_markup=types.ReplyKeyboardRemove())


# ------------------------------------------------------------------
# GROUP TRACKING when bot is added to a new group
# ------------------------------------------------------------------
@bot.my_chat_member_handler()
def on_added_to_group(update):
    if update.new_chat_member.status in ("member", "administrator"):
        if update.chat.type in ("group", "supergroup"):
            track_group(update.chat)


# ------------------------------------------------------------------
# SCHEDULER SETUP
# ------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(autolike_job, "cron", hour=AUTOLIKE_HOUR, minute=AUTOLIKE_MINUTE,
                   id="autolike_job", misfire_grace_time=30)
scheduler.add_job(backup_db_job, "interval", minutes=BACKUP_INTERVAL_MIN, id="backup_job")

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    restore_db_from_backup()
    init_db()  # re-ensure schema exists even after restoring an older backup
    scheduler.start()
    log.info("Bot started as @%s", BOT_USERNAME)
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:  # noqa: BLE001
            log.error("Polling crashed, restarting in 5s: %s", e)
            time.sleep(5)
