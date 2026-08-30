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
import json
import sqlite3
import logging
import threading
import contextlib
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
DEFAULT_AUTOLIKE_MINUTE = 2
AUTOLIKE_WORKERS = 25

BACKUP_INTERVAL_MIN = 30
DB_PATH = "bot_data.db"
TZ = ZoneInfo("Asia/Kolkata")

# How many worker threads telebot uses to process incoming updates in
# parallel — this is what lets the bot handle many simultaneous users
# instead of queueing them one by one.
BOT_THREADS = int(os.environ.get("BOT_THREADS", "60"))

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
_adapter = requests.adapters.HTTPAdapter(pool_connections=60, pool_maxsize=60, max_retries=0)
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
            CREATE TABLE IF NOT EXISTS uid_restrictions (
                uid TEXT PRIMARY KEY, block_like INTEGER DEFAULT 0, block_visit INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS message_templates (
                key TEXT PRIMARY KEY, text TEXT, entities_json TEXT
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
            "flood_msg_type": "",
            "flood_msg_text": "",
            "flood_msg_file_id": "",
            "flood_msg_caption": "",
            "ban_msg_type": "",
            "ban_msg_text": "",
            "ban_msg_file_id": "",
            "ban_msg_caption": "",
            "btn_text_add_group": "ADD ME TO YOUR GROUP",
            "btn_text_verify_check": "I've Joined — Check Again",
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

# ------------------------------------------------------------------
# EARLY-MORNING SEQUENCING
# Between the reset time (default 03:58) and the guaranteed auto-like
# time (default 04:02), regular /like requests are processed ONE AT A
# TIME so each result can be checked: the moment any of them gets 70+
# likes given, that's read as "the API has opened up for today" and
# auto-like fires immediately. If nobody's request crosses 70+ likes,
# the scheduled cron at 04:02 fires auto-like anyway as a guaranteed
# fallback — either way it only ever runs once per day.
# ------------------------------------------------------------------
AUTOLIKE_TRIGGER_THRESHOLD = 70
_early_window_lock = threading.Lock()
_autolike_trigger_lock = threading.Lock()
_autolike_last_run_date = None  # usage_day() string of the last date auto-like was triggered for


def _in_early_window():
    now = datetime.now(TZ)
    reset_h = int(get_setting("like_reset_hour", "3"))
    reset_m = int(get_setting("like_reset_minute", "58"))
    auto_h = int(get_setting("autolike_hour", str(DEFAULT_AUTOLIKE_HOUR)))
    auto_m = int(get_setting("autolike_minute", str(DEFAULT_AUTOLIKE_MINUTE)))
    start = now.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
    end = now.replace(hour=auto_h, minute=auto_m, second=0, microsecond=0)
    return start <= now <= end


def _maybe_trigger_autolike(reason):
    """Idempotent — only the FIRST caller each day actually starts the
    batch, whether that's the 70+ signal or the guaranteed cron fallback."""
    global _autolike_last_run_date
    today = usage_day()
    with _autolike_trigger_lock:
        if _autolike_last_run_date == today:
            return False
        _autolike_last_run_date = today
    log.info("Triggering auto-like batch (%s)", reason)
    threading.Thread(target=autolike_job, daemon=True).start()
    return True


def autolike_cron_trigger():
    """Registered with APScheduler — acts as the guaranteed fallback
    trigger if no one's /like crossed the 70+ threshold first."""
    _maybe_trigger_autolike("guaranteed cron fallback")


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
_FULLWIDTH = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９",
)


def smallcaps(text):
    return str(text).lower().translate(_SMALLCAPS)


def bold_digits(value):
    return str(value).translate(_BOLD_DIGITS)


def super_digits(value):
    return str(value).translate(_SUPER_DIGITS)


def fullwidth(text):
    """Visually bigger-looking unicode style — Telegram has no real font-size
    control, so this is used for one-line celebratory confirmations."""
    return str(text).translate(_FULLWIDTH)


def divider():
    return "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"


# ------------------------------------------------------------------
# EDITABLE MESSAGE TEMPLATES — supports Telegram Premium custom emoji
# and any other formatting, because we capture and replay the exact
# entities the owner's message had (rather than re-parsing HTML/text),
# and safely shift entity offsets around {placeholder} substitutions.
# ------------------------------------------------------------------
TEMPLATE_DEFS = {
    "tpl_like_success": {
        "label": "❤️ Like Success",
        "default": "🎉 Like sent successfully!",
        "placeholders": ["nickname", "uid", "region", "level", "before", "after", "given", "time"],
    },
    "tpl_like_maxed": {
        "label": "❌ Like Max Reached",
        "default": "⚠️ This UID already reached max likes for today.",
        "placeholders": ["nickname", "uid", "region", "level", "before", "after", "time"],
    },
    "tpl_visit_success": {
        "label": "👁 Visit Success",
        "default": "🎉 Visit sent successfully!",
        "placeholders": ["nickname", "uid", "region", "level", "likes", "success", "fail", "time"],
    },
}

BUTTON_NAME_DEFS = {
    "btn_text_add_group": {"label": "➕ Add-Group Button", "default": "ADD ME TO YOUR GROUP"},
    "btn_text_verify_check": {"label": "✅ Verify-Check Button", "default": "I've Joined — Check Again"},
}


def _utf16_len_of(s: str) -> int:
    total = 0
    for ch in s:
        total += 2 if ord(ch) > 0xFFFF else 1
    return total


def _py_index_from_utf16(s: str, utf16_offset: int) -> int:
    """Convert a Telegram UTF-16 code-unit offset into a Python string index."""
    units = 0
    for i, ch in enumerate(s):
        if units >= utf16_offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(s)


def _entities_to_dicts(entities):
    if not entities:
        return []
    out = []
    for e in entities:
        d = {"type": e.type, "offset": e.offset, "length": e.length}
        if getattr(e, "custom_emoji_id", None):
            d["custom_emoji_id"] = e.custom_emoji_id
        if getattr(e, "url", None):
            d["url"] = e.url
        if getattr(e, "language", None):
            d["language"] = e.language
        out.append(d)
    return out


def _dicts_to_entities(dicts):
    return [types.MessageEntity(
        type=d["type"], offset=d["offset"], length=d["length"],
        url=d.get("url"), language=d.get("language"), custom_emoji_id=d.get("custom_emoji_id"),
    ) for d in dicts]


def save_template(key, text, entities):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO message_templates (key, text, entities_json) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET text=excluded.text, entities_json=excluded.entities_json",
            (key, text, json.dumps(_entities_to_dicts(entities))),
        )
        conn.commit()


def get_template_raw(key):
    """Returns (text, entities_dicts) — from DB if the owner customized it,
    else the built-in default (no entities)."""
    with db() as conn:
        row = conn.execute("SELECT text, entities_json FROM message_templates WHERE key=?", (key,)).fetchone()
    if row:
        return row["text"], json.loads(row["entities_json"] or "[]")
    return TEMPLATE_DEFS[key]["default"], []


def render_template(key, values):
    """Returns (text, [telebot MessageEntity]) with every {placeholder}
    substituted and every entity's offset correctly shifted to match —
    UTF-16 aware, so premium/custom emoji land exactly where they should."""
    text, entity_dicts = get_template_raw(key)

    spans = [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"\{(\w+)\}", text)]
    spans.sort()

    py_entities = []
    for e in entity_dicts:
        start_py = _py_index_from_utf16(text, e["offset"])
        end_py = _py_index_from_utf16(text, e["offset"] + e["length"])
        py_entities.append((start_py, end_py, e))

    parts, cursor, replacements = [], 0, []
    for start_py, end_py, ph_key in spans:
        parts.append(text[cursor:start_py])
        value = str(values.get(ph_key, "{" + ph_key + "}"))
        new_start = sum(len(p) for p in parts)
        parts.append(value)
        new_end = sum(len(p) for p in parts)
        replacements.append((start_py, end_py, new_start, new_end))
        cursor = end_py
    parts.append(text[cursor:])
    new_text = "".join(parts)

    new_entity_dicts = []
    for start_py, end_py, e in py_entities:
        delta, skip = 0, False
        for r_start, r_end, r_new_start, r_new_end in replacements:
            if r_end <= start_py:
                delta += (r_new_end - r_new_start) - (r_end - r_start)
            elif r_start < end_py and r_end > start_py:
                skip = True
                break
        if skip:
            continue
        ns, ne = start_py + delta, end_py + delta
        new_e = dict(e)
        new_e["offset"] = _utf16_len_of(new_text[:ns])
        new_e["length"] = _utf16_len_of(new_text[ns:ne])
        new_entity_dicts.append(new_e)

    return new_text, _dicts_to_entities(new_entity_dicts)


def send_rendered_template(chat_id, key, values, reply_markup=None):
    text, entities = render_template(key, values)
    try:
        if entities:
            bot.send_message(chat_id, text, entities=entities, parse_mode=None, reply_markup=reply_markup)
        else:
            bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:  # noqa: BLE001
        log.error("send_rendered_template(%s) failed: %s", key, e)
        bot.send_message(chat_id, text, reply_markup=reply_markup)


def button_text(key):
    return get_setting(key, BUTTON_NAME_DEFS[key]["default"])


def box(title, rows, footer=None, footer_label="Time Taken", highlight=None):
    """Styled receipt-card text (plain — no blockquote wrapper)."""
    highlight = highlight or set()
    lines = [f"┌ {smallcaps(title)}"]
    for label, value in rows:
        shown = f"<b>{bold_digits(value)}</b>" if label in highlight else value
        lines.append(f"├─ {smallcaps(label)}: {shown}")
    if footer is not None:
        lines.append(f"└─ {smallcaps(footer_label)}: {super_digits(footer)}")
    return "\n".join(lines)


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


def list_banned_users(limit=30):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM banned_users ORDER BY banned_at DESC LIMIT ?", (limit,)
        ).fetchall()


# ------------------------------------------------------------------
# PER-UID RESTRICTIONS — owner can block /like and/or /visit for a
# specific game UID (e.g. their own auto-liked UID, to avoid an
# accidental manual /like eating into that day's API allowance).
# ------------------------------------------------------------------
def get_uid_restriction(uid):
    with db() as conn:
        return conn.execute("SELECT * FROM uid_restrictions WHERE uid=?", (uid,)).fetchone()


def set_uid_restriction(uid, block_like, block_visit):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO uid_restrictions (uid, block_like, block_visit) VALUES (?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET block_like=excluded.block_like, block_visit=excluded.block_visit",
            (uid, int(block_like), int(block_visit)),
        )
        conn.commit()


def clear_uid_restriction(uid):
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM uid_restrictions WHERE uid=?", (uid,))
        conn.commit()


def list_uid_restrictions(limit=30):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM uid_restrictions WHERE block_like=1 OR block_visit=1 LIMIT ?", (limit,)
        ).fetchall()


# ------------------------------------------------------------------
# VERIFICATION CHANNELS (DB-backed, editable via /admin)
# ------------------------------------------------------------------
def get_verification_channels():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM verification_channels ORDER BY id ASC"
        ).fetchall()


_verify_executor = ThreadPoolExecutor(max_workers=10)
# Fire-and-forget executor for non-critical writes (user/group tracking)
# so the main handler never waits on them before replying.
_bg_executor = ThreadPoolExecutor(max_workers=10)
_verify_cache = {}
_verify_cache_lock = threading.Lock()
VERIFY_CACHE_TTL = 300  # seconds — an already-verified user isn't re-checked every command


def _check_one_channel(ch, user_id):
    try:
        member = bot.get_chat_member(ch["username"], user_id)
        if member.status in ("left", "kicked"):
            return ch["name"]
    except Exception:  # noqa: BLE001
        return ch["name"]
    return None


def is_verified(user_id: int, use_cache=True):
    """Returns (bool, [missing channel names]). Checks all channels in
    PARALLEL (not one-by-one) and caches a positive result briefly so
    repeat commands from an already-verified user don't re-check at all."""
    if use_cache:
        with _verify_cache_lock:
            cached = _verify_cache.get(user_id)
        if cached and time.time() - cached[2] < VERIFY_CACHE_TTL:
            return cached[0], cached[1]

    channels = get_verification_channels()
    futures = [_verify_executor.submit(_check_one_channel, ch, user_id) for ch in channels]
    results = [f.result() for f in futures]
    missing = [m for m in results if m]
    ok = len(missing) == 0
    with _verify_cache_lock:
        _verify_cache[user_id] = (ok, missing, time.time())
    return ok, missing


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

    kb.row(colored_button(button_text("btn_text_verify_check"), style="success", callback_data="check_verify"))
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
        button_text("btn_text_add_group"), style="success",
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


def send_custom_reply(message, prefix, default_text):
    """Shared implementation for any owner-configurable reply (denial,
    flood-warning, etc.) — reads {prefix}_type/_text/_file_id/_caption
    from settings, falls back to default_text if nothing was configured."""
    t = get_setting(f"{prefix}_type", "")
    try:
        if t == "text" and get_setting(f"{prefix}_text", ""):
            bot.reply_to(message, get_setting(f"{prefix}_text", ""))
            return
        if t and get_setting(f"{prefix}_file_id", ""):
            fid = get_setting(f"{prefix}_file_id", "")
            cap = get_setting(f"{prefix}_caption", "") or None
            if t == "sticker":
                bot.send_sticker(message.chat.id, fid, reply_to_message_id=message.message_id)
                return
            senders = {
                "photo": bot.send_photo, "video": bot.send_video,
                "animation": bot.send_animation, "document": bot.send_document,
                "voice": bot.send_voice,
            }
            if t in senders:
                senders[t](message.chat.id, fid, caption=cap, reply_to_message_id=message.message_id)
                return
    except Exception as e:  # noqa: BLE001
        log.error("send_custom_reply(%s) failed: %s", prefix, e)
    bot.reply_to(message, default_text)


def deny_owner_only(message):
    send_custom_reply(message, "deny_msg", "🚫 Only the bot owner can use this command.")


# ------------------------------------------------------------------
# FLOOD PROTECTION
# ------------------------------------------------------------------
_last_command_time = {}
_last_command_lock = threading.Lock()
FLOOD_WINDOW_SECONDS = 10


def check_flood(user_id) -> bool:
    """Returns True if this user just sent 2 commands within the flood
    window and this one should be blocked instead of processed."""
    if user_id in OWNER_IDS:
        return False
    now = time.time()
    with _last_command_lock:
        last = _last_command_time.get(user_id)
        _last_command_time[user_id] = now
    return last is not None and (now - last) < FLOOD_WINDOW_SECONDS


def send_flood_reply(message):
    send_custom_reply(message, "flood_msg", "⏳ You're sending commands too fast — please slow down a bit.")


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
    ok, missing = is_verified(call.from_user.id, use_cache=False)
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
OWNER_EXAMPLE_UID = "3784469468"


def parse_region_uid(args, command="like"):
    """Forgiving parser: only really cares whether a UID-looking token
    is present. Region can come before or after it. Any non-IND region
    is rejected outright; a missing UID shows a ready-to-copy example
    (using the UID they typed if they gave one, else the owner's)."""
    uid_token = next((a for a in args if UID_RE.match(a)), None)
    region_token = next((a for a in args if not UID_RE.match(a)), None)

    if region_token and region_token.upper() != "IND":
        return None, None, "🌍 Only available for <b>India (IND)</b> region."

    if not uid_token:
        return None, None, f"⚠️ Usage: <code>/{command} IND {OWNER_EXAMPLE_UID}</code>"

    if not region_token:
        return None, None, f"⚠️ Usage: <code>/{command} IND {uid_token}</code>"

    return "IND", uid_token, None


# ------------------------------------------------------------------
# COMMAND: /like  IND {uid}
# ------------------------------------------------------------------
@bot.message_handler(commands=["like"])
def cmd_like(message):
    if not group_only(message):
        return

    args = message.text.split()[1:]
    region, uid, err = parse_region_uid(args, command="like")
    if err:
        bot.reply_to(message, err)
        return

    # Reply INSTANTLY — every check below happens after, editing this
    # same message if something needs to block/notify instead of
    # making the user wait through several DB reads for any feedback.
    processing = bot.reply_to(message, f"⏳ <b>Processing…</b> 🎮 <code>{uid}</code>")

    _bg_executor.submit(track_user, message.from_user)
    _bg_executor.submit(track_group, message.chat)

    if is_banned(message.from_user.id):
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except Exception:  # noqa: BLE001
            pass
        send_custom_reply(message, "ban_msg", "🚫 You are banned from using this bot.")
        return
    if check_flood(message.from_user.id):
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except Exception:  # noqa: BLE001
            pass
        send_flood_reply(message)
        return
    if get_setting("maintenance", "0") == "1" and not is_owner(message.from_user.id):
        bot.edit_message_text("🛠 <b>Bot is under maintenance.</b> Please try again later.",
                               message.chat.id, processing.message_id)
        return

    ok, _ = is_verified(message.from_user.id)
    if not ok:
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except Exception:  # noqa: BLE001
            pass
        send_verification_prompt(message.chat.id)
        return

    restriction = get_uid_restriction(uid)
    if restriction and restriction["block_like"]:
        bot.edit_message_text("🔒 Likes are currently disabled for this UID.",
                               message.chat.id, processing.message_id)
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
            bot.edit_message_text(
                "⏳ <b>Daily limit reached!</b>\nYou can only use /like "
                f"{LIKE_LIMIT_PER_DAY}x per day. Try again tomorrow.",
                message.chat.id, processing.message_id,
            )
            return

    anim_stop, anim_thread = start_processing_animation(message.chat.id, processing.message_id, uid)

    # During the early-morning window (reset time → guaranteed auto-like
    # time), process one /like at a time so each result can be checked —
    # the moment one crosses the threshold, auto-like fires immediately.
    early = _in_early_window() and _autolike_last_run_date != usage_day()
    lock_ctx = _early_window_lock if early else contextlib.nullcontext()

    with lock_ctx:
        # Let the daily auto-like blast go through to the API first if
        # it's currently running (or just got triggered by the line above).
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

        if early and data.get("LikesGivenByAPI", 0) >= AUTOLIKE_TRIGGER_THRESHOLD:
            _maybe_trigger_autolike(f"70+ threshold reached by UID {uid}")

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
            ("Before", data.get("LikesbeforeCommand", "-")),
            ("After", data.get("LikesafterCommand", "-")),
            ("Given", given),
        ],
        footer=f"{elapsed:.2f} seconds",
        highlight={"Before", "After", "Given"},
    )
    tpl_values = {
        "nickname": nickname, "uid": data.get("UID", uid), "region": data.get("PlayerRegion", region),
        "level": data.get("PlayerLevel", "-"), "before": data.get("LikesbeforeCommand", "-"),
        "after": data.get("LikesafterCommand", "-"), "given": given, "time": f"{elapsed:.2f}s",
    }
    if given == 0:
        deliver_result(message.chat.id, processing.message_id, text, add_to_group_kb())
        send_rendered_template(message.chat.id, "tpl_like_maxed", tpl_values)
    else:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO like_usage (user_id, usage_date, count) VALUES (?,?,1) "
                "ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1",
                (user_id, today),
            )
            conn.commit()
        deliver_result(message.chat.id, processing.message_id, text, add_to_group_kb())
        send_rendered_template(message.chat.id, "tpl_like_success", tpl_values)


# ------------------------------------------------------------------
# COMMAND: /visit  IND {uid}
# ------------------------------------------------------------------
_visit_lock = threading.Lock()


@bot.message_handler(commands=["visit"])
def cmd_visit(message):
    if not group_only(message):
        return

    args = message.text.split()[1:]
    region, uid, err = parse_region_uid(args, command="visit")
    if err:
        bot.reply_to(message, err)
        return

    processing = bot.reply_to(message, f"⏳ <b>Processing…</b> 🎮 <code>{uid}</code>")

    _bg_executor.submit(track_user, message.from_user)
    _bg_executor.submit(track_group, message.chat)

    if is_banned(message.from_user.id):
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except Exception:  # noqa: BLE001
            pass
        send_custom_reply(message, "ban_msg", "🚫 You are banned from using this bot.")
        return
    if check_flood(message.from_user.id):
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except Exception:  # noqa: BLE001
            pass
        send_flood_reply(message)
        return
    if get_setting("maintenance", "0") == "1" and not is_owner(message.from_user.id):
        bot.edit_message_text("🛠 <b>Bot is under maintenance.</b> Please try again later.",
                               message.chat.id, processing.message_id)
        return

    ok, _ = is_verified(message.from_user.id)
    if not ok:
        try:
            bot.delete_message(message.chat.id, processing.message_id)
        except Exception:  # noqa: BLE001
            pass
        send_verification_prompt(message.chat.id)
        return

    restriction = get_uid_restriction(uid)
    if restriction and restriction["block_visit"]:
        bot.edit_message_text("🔒 Visits are currently disabled for this UID.",
                               message.chat.id, processing.message_id)
        return

    user_id = message.from_user.id
    now = time.time()
    with _visit_lock, db() as conn:
        row = conn.execute(
            "SELECT last_used FROM visit_cooldown WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and now - row["last_used"] < VISIT_COOLDOWN_SECS and not is_owner(user_id):
            wait = int(VISIT_COOLDOWN_SECS - (now - row["last_used"]))
            bot.edit_message_text(f"⏳ Please wait <b>{wait}s</b> before using /visit again.",
                                   message.chat.id, processing.message_id)
            return
        conn.execute(
            "INSERT INTO visit_cooldown (user_id, last_used) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_used=?",
            (user_id, now, now),
        )
        conn.commit()

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
    send_rendered_template(message.chat.id, "tpl_visit_success", {
        "nickname": nickname, "uid": data.get("uid", uid), "region": data.get("region", region),
        "level": data.get("level", "-"), "likes": data.get("likes", "-"),
        "success": data.get("success", "-"), "fail": data.get("fail", "-"), "time": f"{elapsed:.2f}s",
    })


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
                        ("Before", data.get("LikesbeforeCommand", "-")),
                        ("After", data.get("LikesafterCommand", "-")),
                        ("Given", given), ("Days Left", row["days_left"] - 1),
                    ],
                    footer=f"{elapsed:.2f} seconds",
                    highlight={"Before", "After", "Given"},
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


def get_chat_link(chat_id_or_chat):
    """Public chats have a @username — always prefer t.me/username since
    that works for everyone. Only fall back to a private invite link
    (which needs a specific bot permission) when there's no username."""
    try:
        chat = chat_id_or_chat if hasattr(chat_id_or_chat, "username") else bot.get_chat(chat_id_or_chat)
    except Exception:  # noqa: BLE001
        return "N/A"
    if chat.username:
        return f"https://t.me/{chat.username}"
    try:
        return bot.export_chat_invite_link(chat.id)
    except Exception:  # noqa: BLE001
        return "Private (no public link, bot lacks invite permission)"


def _notify_owners_new_chat(chat, kind):
    creator_info = "Unknown"
    try:
        admins = bot.get_chat_administrators(chat.id)
        for a in admins:
            if a.status == "creator":
                u = a.user
                creator_info = f"@{u.username}" if u.username else f"{u.first_name} (id: {u.id})"
                break
    except Exception:  # noqa: BLE001
        pass
    link = get_chat_link(chat)

    text = box(
        f"New {kind.title()} Added 🔔",
        [("Title", chat.title or "-"), ("Chat ID", chat.id), ("Owner", creator_info),
         ("Link", link)],
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
BTN_RESTRICT_UID = "🔒 Restrict UID"
BTN_SET_BAN_MSG = "🚫 Set Ban Message"
BTN_RM_GROUP    = "➖ Remove From Group"
BTN_BROADCAST   = "📢 Broadcast"
BTN_MAINTENANCE = "🛠 Maintenance ON/OFF"
BTN_SET_IMAGE   = "🖼 Set Result Image"
BTN_SET_DENY    = "💬 Set Restricted Reply"
BTN_SET_FLOOD   = "⏱ Set Flood Reply"
BTN_VERIFY_CH   = "📡 Verification Channels"
BTN_CHANNEL_LIST = "📋 Channel List"
BTN_EDIT_MSGS   = "✏️ Edit Messages"
BTN_EDIT_BTNS   = "🔤 Edit Button Names"
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
    kb.row(colored_button(BTN_RESTRICT_UID, style="danger"))
    kb.row(colored_button(BTN_RM_GROUP, style="danger"), colored_button(BTN_BROADCAST, style="primary"))
    kb.row(colored_button(BTN_MAINTENANCE, style="danger"), colored_button(BTN_SET_IMAGE, style="primary"))
    kb.row(colored_button(BTN_SET_DENY, style="primary"), colored_button(BTN_SET_FLOOD, style="primary"))
    kb.row(colored_button(BTN_SET_BAN_MSG, style="primary"), colored_button(BTN_VERIFY_CH, style="primary"))
    kb.row(colored_button(BTN_CHANNEL_LIST, style="primary"))
    kb.row(colored_button(BTN_EDIT_MSGS, style="primary"), colored_button(BTN_EDIT_BTNS, style="primary"))
    kb.row(colored_button(BTN_BACKUP_NOW, style="success"), colored_button(BTN_RESTORE_UP, style="danger"))
    kb.row(colored_button(BTN_CLOSE, style="danger"))
    return kb


def back_only_kb():
    """Small persistent keyboard shown while waiting for input in a
    multi-step admin flow, so the owner can always bail out."""
    try:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    except TypeError:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(colored_button("🔙 Back", style="danger"))
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
    bot.send_message(message.chat.id, "🗑 Send me the <b>UID</b> to remove auto-like for.",
                      reply_markup=back_only_kb())


@bot.message_handler(func=lambda m: m.text == BTN_SET_TIME and _owner_dm(m))
def panel_set_time_start(message):
    admin_state[message.from_user.id] = {"action": "set_autolike_time"}
    bot.send_message(message.chat.id, "⏰ Send the time in 24h <code>HH:MM</code> format, e.g. <code>04:00</code>",
                      reply_markup=back_only_kb())


@bot.message_handler(func=lambda m: m.text == BTN_BAN and _owner_dm(m))
def panel_ban_start(message):
    admin_state[message.from_user.id] = {"action": "ban_user"}
    bot.send_message(message.chat.id, "🚫 Send the numeric Telegram user ID to ban, or forward a message from them.",
                      reply_markup=back_only_kb())


@bot.message_handler(func=lambda m: m.text == BTN_UNBAN and _owner_dm(m))
def panel_unban_start(message):
    admin_state[message.from_user.id] = {"action": "unban_user"}
    rows = list_banned_users()
    if rows:
        listing = "\n".join(f"• <code>{r['user_id']}</code>" for r in rows)
        intro = f"🚫 <b>Currently banned:</b>\n{listing}\n\n"
    else:
        intro = "🚫 No one is currently banned.\n\n"
    bot.send_message(message.chat.id, intro + "✅ Send the numeric user ID to unban, or forward a message from them.",
                      reply_markup=back_only_kb())


@bot.message_handler(func=lambda m: m.text == BTN_RESTRICT_UID and _owner_dm(m))
def panel_restrict_uid_start(message):
    admin_state[message.from_user.id] = {"action": "restrict_uid_input"}
    rows = list_uid_restrictions()
    if rows:
        listing = "\n".join(
            f"• <code>{r['uid']}</code> — "
            f"{'Like ' if r['block_like'] else ''}{'Visit' if r['block_visit'] else ''}".strip()
            for r in rows
        )
        intro = f"🔒 <b>Currently restricted UIDs:</b>\n{listing}\n\n"
    else:
        intro = "🔒 No UIDs are currently restricted.\n\n"
    bot.send_message(message.chat.id, intro + "Send the <b>UID</b> you want to restrict (or clear).",
                      reply_markup=back_only_kb())


@bot.message_handler(func=lambda m: m.text == BTN_SET_BAN_MSG and _owner_dm(m))
def panel_set_ban_msg_start(message):
    admin_state[message.from_user.id] = {"action": "set_ban_reply"}
    bot.send_message(message.chat.id, "🚫 Send the text / photo / video / sticker to show a user when they try "
                                       "using the bot while banned. Type <code>clear</code> to reset.",
                      reply_markup=back_only_kb())


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


@bot.callback_query_handler(func=lambda c: c.data.startswith("restrict:"))
def cb_restrict_uid(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "🚫 Not authorized.", show_alert=True)
        return
    _, target_uid, choice = call.data.split(":")
    if choice == "clear":
        clear_uid_restriction(target_uid)
        result = f"✅ Restriction cleared for UID <code>{target_uid}</code>."
    else:
        block_like = choice in ("like", "both")
        block_visit = choice in ("visit", "both")
        set_uid_restriction(target_uid, block_like, block_visit)
        what = {"like": "Likes", "visit": "Visits", "both": "Likes & Visits"}[choice]
        result = f"🔒 {what} blocked for UID <code>{target_uid}</code>."
    bot.answer_callback_query(call.id, "✅ Done.")
    try:
        bot.edit_message_text(result, call.message.chat.id, call.message.message_id)
    except Exception:  # noqa: BLE001
        bot.send_message(call.message.chat.id, result)



@bot.message_handler(func=lambda m: m.text == BTN_BROADCAST and _owner_dm(m))
def panel_broadcast_start(message):
    admin_state[message.from_user.id] = {"action": "broadcast_wait_content"}
    bot.send_message(message.chat.id, "📢 Send the message (text/photo/video/etc.) you want to broadcast "
                                       "to every group and channel where I'm admin.",
                      reply_markup=back_only_kb())


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
                                       "or type <code>clear</code> to remove it (results go back to plain text).",
                      reply_markup=back_only_kb())


@bot.message_handler(func=lambda m: m.text == BTN_SET_DENY and _owner_dm(m))
def panel_set_deny_start(message):
    admin_state[message.from_user.id] = {"action": "set_deny_reply"}
    bot.send_message(message.chat.id, "💬 Send the text / photo / video / sticker you want shown to "
                                       "non-owners who try owner-only commands. Type <code>clear</code> to reset.",
                      reply_markup=back_only_kb())


@bot.message_handler(func=lambda m: m.text == BTN_SET_FLOOD and _owner_dm(m))
def panel_set_flood_start(message):
    admin_state[message.from_user.id] = {"action": "set_flood_reply"}
    bot.send_message(message.chat.id, "⏱ Send the text / photo / video / sticker you want shown to users "
                                       "who send 2 commands within 10 seconds. Type <code>clear</code> to reset.",
                      reply_markup=back_only_kb())


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
        reply_markup=back_only_kb(),
    )


@bot.message_handler(func=lambda m: m.text == BTN_CHANNEL_LIST and _owner_dm(m))
def panel_channel_list(message):
    with db() as conn:
        rows = conn.execute("SELECT chat_id, title FROM channels ORDER BY title").fetchall()
    if not rows:
        bot.send_message(message.chat.id, "📋 No channels tracked yet.")
        return
    bot.send_message(message.chat.id, f"📋 Fetching links for {len(rows)} channel(s)…")
    lines = []
    for r in rows:
        link = get_chat_link(r["chat_id"])
        lines.append(f"• <b>{r['title'] or r['chat_id']}</b>\n  ID: <code>{r['chat_id']}</code>\n  {link}")
    bot.send_message(message.chat.id, "📋 <b>Channel List</b>\n\n" + "\n\n".join(lines))


@bot.message_handler(func=lambda m: m.text == BTN_EDIT_MSGS and _owner_dm(m))
def panel_edit_messages_menu(message):
    kb = types.InlineKeyboardMarkup()
    for key, info in TEMPLATE_DEFS.items():
        kb.row(types.InlineKeyboardButton(info["label"], callback_data=f"edittpl:{key}"))
    bot.send_message(message.chat.id, "✏️ Which message do you want to edit?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("edittpl:"))
def cb_edit_template_pick(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "🚫 Not authorized.", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    info = TEMPLATE_DEFS[key]
    admin_state[call.from_user.id] = {"action": "edit_template", "key": key}
    bot.answer_callback_query(call.id)

    text, entity_dicts = get_template_raw(key)
    bot.send_message(call.message.chat.id, f"📄 <b>Current — {info['label']}</b> (exactly as it will send):")
    try:
        if entity_dicts:
            bot.send_message(call.message.chat.id, text, entities=_dicts_to_entities(entity_dicts), parse_mode=None)
        else:
            bot.send_message(call.message.chat.id, text)
    except Exception:  # noqa: BLE001
        bot.send_message(call.message.chat.id, text)

    placeholders = ", ".join(f"{{{p}}}" for p in info["placeholders"])
    bot.send_message(
        call.message.chat.id,
        f"✏️ Send the new version for <b>{info['label']}</b> now.\n"
        f"You can include Telegram Premium emoji, bold, links — sent exactly as you type it.\n"
        f"Available placeholders: {placeholders}",
        reply_markup=back_only_kb(),
    )


@bot.message_handler(func=lambda m: m.text == BTN_EDIT_BTNS and _owner_dm(m))
def panel_edit_buttons_menu(message):
    kb = types.InlineKeyboardMarkup()
    for key, info in BUTTON_NAME_DEFS.items():
        kb.row(types.InlineKeyboardButton(info["label"], callback_data=f"editbtn:{key}"))
    bot.send_message(message.chat.id, "🔤 Which button do you want to rename?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("editbtn:"))
def cb_edit_button_pick(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "🚫 Not authorized.", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    info = BUTTON_NAME_DEFS[key]
    admin_state[call.from_user.id] = {"action": "edit_button_name", "key": key}
    bot.answer_callback_query(call.id)
    current = button_text(key)
    bot.send_message(
        call.message.chat.id,
        f"🔤 Current text for <b>{info['label']}</b>: <code>{current}</code>\n\n"
        "⚠️ Note: Telegram buttons can only show plain text — animated Premium "
        "emoji won't render on a button (Bot API limitation), but any normal "
        "emoji/unicode character works fine.\n\n"
        "Send the new button text now.",
        reply_markup=back_only_kb(),
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
        reply_markup=back_only_kb(),
    )


@bot.message_handler(func=lambda m: m.text == BTN_CLOSE and _owner_dm(m))
def panel_close(message):
    admin_state.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "🔙 Panel closed.", reply_markup=types.ReplyKeyboardRemove())


# ------------------------------------------------------------------
# GENERIC HANDLER FOR MULTI-STEP ADMIN FLOWS (owner DM only)
# ------------------------------------------------------------------
_PANEL_BUTTON_TEXTS = {
    BTN_STATS, BTN_AUTOLIST, BTN_RM_AUTO, BTN_SET_TIME, BTN_BAN, BTN_UNBAN, BTN_RESTRICT_UID,
    BTN_RM_GROUP, BTN_BROADCAST, BTN_MAINTENANCE, BTN_SET_IMAGE, BTN_SET_DENY, BTN_SET_FLOOD,
    BTN_SET_BAN_MSG, BTN_VERIFY_CH, BTN_CHANNEL_LIST, BTN_EDIT_MSGS, BTN_EDIT_BTNS,
    BTN_BACKUP_NOW, BTN_RESTORE_UP, BTN_CLOSE,
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
        log.warning("owner_flow_handler fired with NO pending state for uid=%s, text=%r", uid,
                    message.text if message.content_type == "text" else message.content_type)
        return
    action = state["action"]
    log.info("owner_flow_handler: uid=%s action=%s content_type=%s", uid, action, message.content_type)

    # ---- universal cancel/back, works from any pending state ----
    if message.content_type == "text" and message.text.strip().lower() in ("back", "cancel", "🔙 back"):
        admin_state.pop(uid, None)
        try:
            bot.send_message(message.chat.id, "🔙 Back to panel.", reply_markup=admin_panel_kb())
        except Exception as e:  # noqa: BLE001
            log.error("Back-to-panel send failed: %s", e)
            bot.send_message(message.chat.id, "🔙 Back to panel. Send /admin to reopen the buttons.")
        return

    # ---- edit a message template (captures entities → premium emoji works) ----
    if action == "edit_template":
        if message.content_type != "text":
            bot.reply_to(message, "⚠️ Please send this as a plain text message (not a photo/sticker/etc).")
            return
        key = state["key"]
        save_template(key, message.text, message.entities)
        admin_state.pop(uid, None)
        bot.reply_to(message, f"✅ Saved. Here's exactly how it will now appear:")
        text, entity_dicts = get_template_raw(key)
        try:
            if entity_dicts:
                bot.send_message(message.chat.id, text, entities=_dicts_to_entities(entity_dicts), parse_mode=None)
            else:
                bot.send_message(message.chat.id, text)
        except Exception:  # noqa: BLE001
            bot.send_message(message.chat.id, text)
        return

    # ---- rename a button (plain text only — Bot API limitation) ----
    if action == "edit_button_name":
        if message.content_type != "text":
            bot.reply_to(message, "⚠️ Button text must be plain text.")
            return
        key = state["key"]
        set_setting(key, message.text.strip())
        admin_state.pop(uid, None)
        bot.reply_to(message, f"✅ Button text updated to: {message.text.strip()}")
        return

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

    # ---- restrict a UID: ask which action(s) to block ----
    if action == "restrict_uid_input":
        text = (message.text or "").strip()
        if not UID_RE.match(text):
            bot.reply_to(message, "⚠️ That doesn't look like a valid UID. Send digits only.")
            return
        admin_state.pop(uid, None)
        kb = types.InlineKeyboardMarkup()
        kb.row(colored_button("🚫 Block Likes", style="danger", callback_data=f"restrict:{text}:like"))
        kb.row(colored_button("🚫 Block Visits", style="danger", callback_data=f"restrict:{text}:visit"))
        kb.row(colored_button("🚫 Block Both", style="danger", callback_data=f"restrict:{text}:both"))
        kb.row(colored_button("✅ Clear Restriction", style="success", callback_data=f"restrict:{text}:clear"))
        bot.reply_to(message, f"🔒 What should be blocked for UID <code>{text}</code>?",
                     reply_markup=kb)
        bot.send_message(message.chat.id, "🔙 Back to panel.", reply_markup=admin_panel_kb())
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
    if action in ("set_deny_reply", "set_flood_reply", "set_ban_reply"):
        prefix = {"set_deny_reply": "deny_msg", "set_flood_reply": "flood_msg",
                  "set_ban_reply": "ban_msg"}[action]
        label = {"set_deny_reply": "Restricted-command", "set_flood_reply": "Flood-warning",
                 "set_ban_reply": "Ban"}[action]
        if message.text and message.text.strip().lower() == "clear":
            set_setting(f"{prefix}_type", "")
            set_setting(f"{prefix}_text", "")
            set_setting(f"{prefix}_file_id", "")
            set_setting(f"{prefix}_caption", "")
            admin_state.pop(uid, None)
            bot.reply_to(message, f"💬 {label} reply reset to default.")
            return
        ct = message.content_type
        if ct == "text":
            set_setting(f"{prefix}_type", "text")
            set_setting(f"{prefix}_text", message.text)
        elif ct in ("photo", "video", "animation", "document", "voice", "sticker"):
            file_id = {
                "photo": lambda: message.photo[-1].file_id,
                "video": lambda: message.video.file_id,
                "animation": lambda: message.animation.file_id,
                "document": lambda: message.document.file_id,
                "voice": lambda: message.voice.file_id,
                "sticker": lambda: message.sticker.file_id,
            }[ct]()
            set_setting(f"{prefix}_type", ct)
            set_setting(f"{prefix}_file_id", file_id)
            set_setting(f"{prefix}_caption", message.caption or "")
        else:
            bot.reply_to(message, "⚠️ Unsupported type. Send text, photo, video, sticker, or type clear.")
            return
        admin_state.pop(uid, None)
        bot.reply_to(message, f"💬 {label} reply saved.")
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
    scheduler.add_job(autolike_cron_trigger, "cron", hour=h, minute=m, id="autolike_job", misfire_grace_time=30)
    scheduler.add_job(backup_db_job, "interval", minutes=BACKUP_INTERVAL_MIN, id="backup_job")
    scheduler.start()

    log.info("Bot started as @%s (threads=%d)", BOT_USERNAME, BOT_THREADS)
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:  # noqa: BLE001
            log.error("Polling crashed, restarting in 5s: %s", e)
            time.sleep(5)
