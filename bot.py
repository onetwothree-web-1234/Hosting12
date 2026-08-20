"""
███████╗██████╗ ██╗  ██╗    ███████╗██████╗  █████╗ 
██╔════╝██╔══██╗██║ ██╔╝    ██╔════╝██╔══██╗██╔══██╗
███████╗██████╔╝█████╔╝     █████╗  ██████╔╝███████║
╚════██║██╔══██╗██╔═██╗     ██╔══╝  ██╔══██╗██╔══██║
███████║██║  ██║██║  ██╗    ███████╗██║  ██║██║  ██║
╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝    ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝
PREMIUM FF LIKE BOT - RAILWAY DEPLOYMENT
"""

import asyncio
import aiohttp
import aiosqlite
import logging
import os
import re
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
    BotCommandScopeAllGroupChats, BotCommandScopeChat,
    WebAppInfo, MenuButtonWebApp, MenuButtonCommands
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, Defaults
)
from telegram.error import TelegramError, RetryAfter, TimedOut

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION - Railway Variables
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Channel Verification - 4 Channels
VERIFICATION_CHANNELS = [
    {"username": "@SRK_ERA", "id": int(os.getenv("CHANNEL_1_ID", "0"))},
    {"username": "@SRK_IMP1", "id": int(os.getenv("CHANNEL_2_ID", "0"))},
    {"username": "@snnetwork7", "id": int(os.getenv("CHANNEL_3_ID", "0"))},
    {"username": "@SNxFF_IND", "id": int(os.getenv("CHANNEL_4_ID", "0"))},
]

# Backup Channel for Database
BACKUP_CHANNEL = os.getenv("BACKUP_CHANNEL", "@your_backup_channel")

# Admin User IDs (comma separated)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "1234567890").split(",") if x.strip()]

# API Endpoints
LIKE_API = "https://srk-like-api.vercel.app/like?uid={uid}&server_name={region}"
VISIT_API = "https://visit-api-10k.vercel.app/{region}/{uid}"

# Rate Limits
VISIT_COOLDOWN_SECONDS = 25
LIKE_DAILY_LIMIT = 1

# Auto Like Time (4:00 AM IST)
AUTO_LIKE_HOUR = 4
AUTO_LIKE_MINUTE = 0

# Database Path
DB_PATH = "bot_database.db"

# Timezone
IST = pytz.timezone('Asia/Kolkata')

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class Database:
    """Advanced Database Manager with backup support"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        
    async def initialize(self):
        """Initialize database with tables"""
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_like_date TEXT,
                total_likes INTEGER DEFAULT 0,
                total_visits INTEGER DEFAULT 0,
                is_verified BOOLEAN DEFAULT 0,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                region TEXT NOT NULL,
                uid TEXT NOT NULL,
                days INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT,
                expires_at TEXT,
                last_run TEXT,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS visit_cooldowns (
                user_id INTEGER PRIMARY KEY,
                last_visit_time TEXT
            )
        """)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS group_settings (
                group_id INTEGER PRIMARY KEY,
                settings_json TEXT
            )
        """)
        await self.conn.commit()
        
    async def close(self):
        """Close database connection"""
        if self.conn:
            await self.conn.close()
    
    async def backup_database(self) -> str:
        """Create backup and return file path"""
        async with self._lock:
            backup_path = f"backup_{int(time.time())}.db"
            source = await aiosqlite.connect(self.db_path)
            target = await aiosqlite.connect(backup_path)
            await source.backup(target)
            await source.close()
            await target.close()
            return backup_path
    
    async def restore_database(self, backup_path: str):
        """Restore database from backup"""
        async with self._lock:
            if self.conn:
                await self.conn.close()
            source = await aiosqlite.connect(backup_path)
            target = await aiosqlite.connect(self.db_path)
            await source.backup(target)
            await source.close()
            await target.close()
            self.conn = await aiosqlite.connect(self.db_path)
    
    # User Operations
    async def get_user(self, user_id: int) -> Optional[Dict]:
        """Get user data"""
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))
            return None
    
    async def create_or_update_user(self, user_id: int, username: str, first_name: str):
        """Create or update user"""
        async with self._lock:
            existing = await self.get_user(user_id)
            if existing:
                await self.conn.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                    (username, first_name, user_id)
                )
            else:
                await self.conn.execute(
                    "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                    (user_id, username, first_name)
                )
            await self.conn.commit()
    
    async def update_like_usage(self, user_id: int):
        """Update like usage date"""
        async with self._lock:
            now = datetime.now(IST).isoformat()
            await self.conn.execute(
                "UPDATE users SET last_like_date = ?, total_likes = total_likes + 1 WHERE user_id = ?",
                (now, user_id)
            )
            await self.conn.commit()
    
    async def update_visit_usage(self, user_id: int):
        """Update visit usage"""
        async with self._lock:
            now = datetime.now(IST).isoformat()
            await self.conn.execute(
                "UPDATE users SET total_visits = total_visits + 1 WHERE user_id = ?",
                (user_id,)
            )
            await self.conn.execute(
                "INSERT OR REPLACE INTO visit_cooldowns (user_id, last_visit_time) VALUES (?, ?)",
                (user_id, now)
            )
            await self.conn.commit()
    
    async def get_visit_cooldown(self, user_id: int) -> Optional[datetime]:
        """Get visit cooldown time"""
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT last_visit_time FROM visit_cooldowns WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                return datetime.fromisoformat(row[0])
            return None
    
    async def can_use_like(self, user_id: int) -> bool:
        """Check if user can use like today"""
        user = await self.get_user(user_id)
        if not user or not user.get('last_like_date'):
            return True
        last = datetime.fromisoformat(user['last_like_date'])
        now = datetime.now(IST)
        return last.date() < now.date()
    
    # Auto Like Operations
    async def add_auto_like(self, region: str, uid: str, days: int, name: str, created_by: int) -> int:
        """Add auto like entry"""
        async with self._lock:
            now = datetime.now(IST)
            expires = now + timedelta(days=days)
            cursor = await self.conn.execute(
                """INSERT INTO auto_likes 
                   (region, uid, days, name, created_by, created_at, expires_at, is_active) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (region.lower(), uid, days, name, created_by, now.isoformat(), expires.isoformat())
            )
            await self.conn.commit()
            return cursor.lastrowid
    
    async def get_active_auto_likes(self) -> List[Dict]:
        """Get all active auto likes"""
        async with self._lock:
            now = datetime.now(IST)
            cursor = await self.conn.execute(
                "SELECT * FROM auto_likes WHERE is_active = 1 AND expires_at > ?",
                (now.isoformat(),)
            )
            rows = await cursor.fetchall()
            if rows:
                columns = [description[0] for description in cursor.description]
                return [dict(zip(columns, row)) for row in rows]
            return []
    
    async def get_auto_likes_by_group(self, group_id: int) -> List[Dict]:
        """Get auto likes for a specific group"""
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM auto_likes WHERE is_active = 1 AND created_by = ?",
                (group_id,)
            )
            rows = await cursor.fetchall()
            if rows:
                columns = [description[0] for description in cursor.description]
                return [dict(zip(columns, row)) for row in rows]
            return []
    
    async def update_auto_like_run(self, auto_id: int):
        """Update auto like last run time"""
        async with self._lock:
            now = datetime.now(IST).isoformat()
            await self.conn.execute(
                "UPDATE auto_likes SET last_run = ? WHERE id = ?",
                (now, auto_id)
            )
            await self.conn.commit()
    
    async def deactivate_expired_auto_likes(self):
        """Deactivate expired auto likes"""
        async with self._lock:
            now = datetime.now(IST).isoformat()
            await self.conn.execute(
                "UPDATE auto_likes SET is_active = 0 WHERE expires_at <= ?",
                (now,)
            )
            await self.conn.commit()
    
    async def remove_auto_like(self, auto_id: int):
        """Remove auto like entry"""
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM auto_likes WHERE id = ?", (auto_id,)
            )
            await self.conn.commit()
    
    # Group Settings
    async def get_group_settings(self, group_id: int) -> Dict:
        """Get group settings"""
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT settings_json FROM group_settings WHERE group_id = ?",
                (group_id,)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return {}
    
    async def set_group_settings(self, group_id: int, settings: Dict):
        """Set group settings"""
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO group_settings (group_id, settings_json) VALUES (?, ?)",
                (group_id, json.dumps(settings))
            )
            await self.conn.commit()
    
    async def get_total_users(self) -> int:
        """Get total users count"""
        async with self._lock:
            cursor = await self.conn.execute("SELECT COUNT(*) FROM users")
            row = await cursor.fetchone()
            return row[0] if row else 0
    
    async def get_total_auto_likes(self) -> int:
        """Get total auto likes"""
        async with self._lock:
            cursor = await self.conn.execute("SELECT COUNT(*) FROM auto_likes WHERE is_active = 1")
            row = await cursor.fetchone()
            return row[0] if row else 0

# ═══════════════════════════════════════════════════════════════════════════════
# API CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class APIClient:
    """Advanced API Client with retry logic"""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(50)  # Max 50 concurrent requests
        
    async def initialize(self):
        """Initialize session"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=120)
            self.session = aiohttp.ClientSession(timeout=timeout)
    
    async def close(self):
        """Close session"""
        if self.session:
            await self.session.close()
            self.session = None
    
    async def call_like_api(self, uid: str, region: str = "ind") -> Dict:
        """Call Like API with retry"""
        url = LIKE_API.format(uid=uid, region=region.lower())
        return await self._make_request(url, "like")
    
    async def call_visit_api(self, uid: str, region: str = "ind") -> Dict:
        """Call Visit API with retry"""
        url = VISIT_API.format(region=region.upper(), uid=uid)
        return await self._make_request(url, "visit")
    
    async def _make_request(self, url: str, api_type: str, retries: int = 3) -> Dict:
        """Make API request with retry logic"""
        if not self.session:
            await self.initialize()
        
        async with self._semaphore:
            for attempt in range(retries):
                try:
                    async with self.session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            return {
                                "success": True,
                                "data": data,
                                "api_type": api_type,
                                "url": url,
                                "attempt": attempt + 1
                            }
                        elif response.status == 429:
                            # Rate limited
                            await asyncio.sleep(2 ** attempt)
                            continue
                        else:
                            if attempt == retries - 1:
                                return {
                                    "success": False,
                                    "error": f"HTTP {response.status}",
                                    "api_type": api_type,
                                    "url": url
                                }
                            await asyncio.sleep(1)
                except asyncio.TimeoutError:
                    if attempt == retries - 1:
                        return {
                            "success": False,
                            "error": "Timeout",
                            "api_type": api_type,
                            "url": url
                        }
                    await asyncio.sleep(2)
                except Exception as e:
                    if attempt == retries - 1:
                        return {
                            "success": False,
                            "error": str(e),
                            "api_type": api_type,
                            "url": url
                        }
                    await asyncio.sleep(1)
        
        return {"success": False, "error": "Max retries exceeded", "api_type": api_type}

# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS - Premium Design
# ═══════════════════════════════════════════════════════════════════════════════

class UIHelper:
    """Premium UI Helper with animations and designs"""
    
    # Font styles
    BOLD = "**"
    ITALIC = "_"
    CODE = "`"
    SPOILER = "||"
    
    # Emojis
    EMOJIS = {
        "crown": "👑",
        "star": "⭐",
        "fire": "🔥",
        "rocket": "🚀",
        "check": "✅",
        "cross": "❌",
        "warning": "⚠️",
        "info": "ℹ️",
        "loading": "⚡",
        "lock": "🔒",
        "unlock": "🔓",
        "database": "💾",
        "users": "👥",
        "chart": "📊",
        "settings": "⚙️",
        "back": "🔙",
        "forward": "🔜",
        "gift": "🎁",
        "trophy": "🏆",
        "medal": "🏅",
        "timer": "⏱️",
        "globe": "🌍",
        "heart": "❤️",
        "bolt": "⚡",
        "sparkles": "✨",
        "party": "🎉",
        "shield": "🛡️",
        "key": "🔑",
        "bell": "🔔",
        "bookmark": "📌",
        "calendar": "📅",
        "chart_up": "📈",
        "chart_down": "📉",
        "target": "🎯",
        "diamond": "💎",
        "gem": "💠",
        "ring": "💍",
        "coin": "🪙",
        "money": "💰",
        "bank": "🏦",
        "credit": "💳",
        "package": "📦",
        "box": "📁",
        "folder": "📂",
        "file": "📄",
        "page": "📃",
        "scroll": "📜",
        "clipboard": "📋",
        "pen": "🖊️",
        "pencil": "✏️",
        "bulb": "💡",
        "flash": "🔦",
        "battery": "🔋",
        "plug": "🔌",
        "wrench": "🔧",
        "hammer": "🔨",
        "gear": "⚙️",
        "tools": "🛠️",
        "magnet": "🧲",
        "microscope": "🔬",
        "telescope": "🔭",
        "satellite": "📡",
        "antenna": "📶",
        "signal": "📡",
        "radio": "📻",
        "tv": "📺",
        "camera": "📷",
        "video": "🎥",
        "film": "🎬",
        "clap": "🎬",
        "music": "🎵",
        "note": "🎶",
        "mic": "🎤",
        "headphone": "🎧",
        "phone": "📱",
        "computer": "💻",
        "keyboard": "⌨️",
        "mouse": "🖱️",
        "monitor": "🖥️",
        "printer": "🖨️",
        "cd": "💿",
        "dvd": "📀",
        "disk": "💾",
        "usb": "🔌",
        "cloud": "☁️",
        "sun": "☀️",
        "moon": "🌙",
        "star2": "🌟",
        "zap": "⚡",
        "snow": "❄️",
        "rain": "🌧️",
        "wind": "💨",
        "fire2": "🔥",
        "water": "💧",
        "earth": "🌍",
        "map": "🗺️",
        "compass": "🧭",
        "flag": "🚩",
        "banner": "🏳️",
        "ribbon": "🎀",
        "badge": "📛",
        "ticket": "🎫",
        "pass": "🎟️",
        "card": "🪪",
        "id": "🆔",
        "key2": "🔐",
        "bell2": "🔕",
        "mega": "📣",
        "speaker": "🔊",
        "silent": "🔇",
        "eye": "👁️",
        "eye_slash": "🙈",
        "finger": "👉",
        "point_up": "☝️",
        "point_down": "👇",
        "point_left": "👈",
        "ok": "👌",
        "thumbs_up": "👍",
        "thumbs_down": "👎",
        "clap2": "👏",
        "pray": "🙏",
        "hands": "🤝",
        "fist": "✊",
        "power": "💪",
        "brain": "🧠",
        "heart2": "💖",
        "heart3": "💕",
        "heart4": "💗",
        "heart5": "💓",
        "heart6": "💞",
        "heart7": "💘",
        "heart8": "💝",
        "break_heart": "💔",
        "love_letter": "💌",
        "kiss": "💋",
        "angel": "😇",
        "devil": "😈",
        "cool": "😎",
        "nerd": "🤓",
        "thinking": "🤔",
        "shock": "😱",
        "scream": "😨",
        "fear": "😰",
        "sad": "😢",
        "cry": "😭",
        "angry": "😠",
        "rage": "😡",
        "sick": "🤢",
        "dead": "💀",
        "ghost": "👻",
        "alien": "👽",
        "robot": "🤖",
        "monkey": "🐵",
        "dog": "🐶",
        "cat": "🐱",
        "lion": "🦁",
        "tiger": "🐯",
        "horse": "🐴",
        "unicorn": "🦄",
        "dragon": "🐉",
        "dinosaur": "🦕",
        "whale": "🐳",
        "dolphin": "🐬",
        "fish": "🐟",
        "octopus": "🐙",
        "crab": "🦀",
        "shrimp": "🦐",
        "squid": "🦑",
        "snail": "🐌",
        "butterfly": "🦋",
        "bee": "🐝",
        "ant": "🐜",
        "spider": "🕷️",
        "scorpion": "🦂",
        "snake": "🐍",
        "turtle": "🐢",
        "crocodile": "🐊",
        "lizard": "🦎",
        "frog": "🐸",
        "rabbit": "🐰",
        "hamster": "🐹",
        "mouse2": "🐭",
        "rat": "🐀",
        "bird": "🐦",
        "chicken": "🐔",
        "rooster": "🐓",
        "duck": "🦆",
        "eagle": "🦅",
        "owl": "🦉",
        "bat": "🦇",
        "wolf": "🐺",
        "fox": "🦊",
        "bear": "🐻",
        "panda": "🐼",
        "koala": "🐨",
        "tiger2": "🐅",
        "leopard": "🐆",
        "horse2": "🐎",
        "zebra": "🦓",
        "deer": "🦌",
        "cow": "🐮",
        "pig": "🐷",
        "boar": "🐗",
        "sheep": "🐑",
        "goat": "🐐",
        "camel": "🐫",
        "elephant": "🐘",
        "rhino": "🦏",
        "hippo": "🦛",
        "giraffe": "🦒",
        "kangaroo": "🦘",
        "monkey2": "🐒",
        "gorilla": "🦍",
        "orangutan": "🦧",
    }
    
    @staticmethod
    def get_border_line() -> str:
        """Get premium border line"""
        return "━" * 25
    
    @staticmethod
    def get_loading_animation() -> List[str]:
        """Get loading animation frames"""
        return ["▱▱▱▱▱▱▱▱▱▱", "▰▱▱▱▱▱▱▱▱▱", "▰▰▱▱▱▱▱▱▱▱", 
                "▰▰▰▱▱▱▱▱▱▱", "▰▰▰▰▱▱▱▱▱▱", "▰▰▰▰▰▱▱▱▱▱",
                "▰▰▰▰▰▰▱▱▱▱", "▰▰▰▰▰▰▰▱▱▱", "▰▰▰▰▰▰▰▰▱▱",
                "▰▰▰▰▰▰▰▰▰▱", "▰▰▰▰▰▰▰▰▰▰"]
    
    @staticmethod
    def format_player_info(data: Dict, api_type: str, time_taken: float) -> str:
        """Format player information message"""
        emoji = UIHelper.EMOJIS
        
        if api_type == "like":
            return (
                f"{emoji['crown']} **『 PREMIUM LIKE SERVICE 』** {emoji['crown']}\n"
                f"{emoji['sparkles']} {UIHelper.get_border_line()} {emoji['sparkles']}\n\n"
                f"{emoji['star']} **Player Information** {emoji['check']}\n"
                f"{emoji['diamond']} ├─ **Nickname:** {data.get('nickname', 'N/A')}\n"
                f"{emoji['id']} ├─ **UID:** {data.get('uid', 'N/A')}\n"
                f"{emoji['globe']} ├─ **Region:** {data.get('region', 'IND').upper()}\n"
                f"{emoji['chart_up']} ├─ **Level:** {data.get('level', 'N/A')}\n"
                f"{emoji['heart']} ├─ **Likes:** {data.get('likes', 'N/A')}\n"
                f"{emoji['trophy']} ├─ **Success:** {data.get('success', 'N/A')}\n"
                f"{emoji['timer']} └─ **Time Taken:** {time_taken:.2f} seconds\n\n"
                f"{emoji['party']} {UIHelper.get_border_line()} {emoji['party']}\n"
                f"{emoji['rocket']} **Premium Bot Service** {emoji['rocket']}"
            )
        else:
            return (
                f"{emoji['crown']} **『 PREMIUM VISIT SERVICE 』** {emoji['crown']}\n"
                f"{emoji['sparkles']} {UIHelper.get_border_line()} {emoji['sparkles']}\n\n"
                f"{emoji['star']} **Player Information** {emoji['check']}\n"
                f"{emoji['diamond']} ├─ **Nickname:** {data.get('nickname', 'N/A')}\n"
                f"{emoji['id']} ├─ **UID:** {data.get('uid', 'N/A')}\n"
                f"{emoji['globe']} ├─ **Region:** {data.get('region', 'IND').upper()}\n"
                f"{emoji['chart_up']} ├─ **Level:** {data.get('level', 'N/A')}\n"
                f"{emoji['heart']} ├─ **Likes:** {data.get('likes', 'N/A')}\n"
                f"{emoji['trophy']} ├─ **Success:** {data.get('success', 'N/A')}\n"
                f"{emoji['cross']} ├─ **Fail:** {data.get('fail', 0)}\n"
                f"{emoji['timer']} └─ **Time Taken:** {time_taken:.2f} seconds\n\n"
                f"{emoji['party']} {UIHelper.get_border_line()} {emoji['party']}\n"
                f"{emoji['rocket']} **Premium Bot Service** {emoji['rocket']}"
            )
    
    @staticmethod
    def format_processing_message(name: str, api_type: str) -> str:
        """Format processing message"""
        emoji = UIHelper.EMOJIS
        service_name = "LIKE" if api_type == "like" else "VISIT"
        return (
            f"{emoji['loading']} **Processing {service_name}...** {emoji['loading']}\n"
            f"{emoji['sparkles']} {UIHelper.get_border_line()} {emoji['sparkles']}\n\n"
            f"{emoji['star']} **Player:** {name}\n"
            f"{emoji['rocket']} **Status:** ⚡ Processing...\n"
            f"{emoji['bolt']} **Speed:** Maximum Priority\n\n"
            f"{emoji['party']} {UIHelper.get_border_line()} {emoji['party']}\n"
            f"{emoji['fire']} **Premium Fast Service** {emoji['fire']}"
        )
    
    @staticmethod
    def get_add_bot_button() -> InlineKeyboardMarkup:
        """Get add bot to group button"""
        emoji = UIHelper.EMOJIS
        keyboard = [[
            InlineKeyboardButton(
                f"{emoji['rocket']} ADD ME TO YOUR GROUP {emoji['rocket']}",
                url="https://t.me/your_bot_username?startgroup=true"
            )
        ]]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def get_verification_keyboard(unverified_channels: List[str]) -> InlineKeyboardMarkup:
        """Get verification keyboard with colorful buttons"""
        emoji = UIHelper.EMOJIS
        keyboard = []
        colors = ["🟢", "🔵", "🟣", "🟠"]
        for i, channel in enumerate(unverified_channels):
            color = colors[i % len(colors)]
            keyboard.append([
                InlineKeyboardButton(
                    f"{color} JOIN {channel} {color}",
                    url=f"https://t.me/{channel.replace('@', '')}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji['check']} I'VE JOINED - VERIFY {emoji['check']}",
                callback_data="verify_channels"
            )
        ])
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def get_main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
        """Get main menu keyboard"""
        emoji = UIHelper.EMOJIS
        buttons = [
            [KeyboardButton(f"{emoji['heart']} LIKE SERVICE {emoji['heart']}")],
            [KeyboardButton(f"{emoji['eye']} VISIT SERVICE {emoji['eye']}")],
            [KeyboardButton(f"{emoji['info']} HELP {emoji['info']}")],
            [KeyboardButton(f"{emoji['users']} MY STATS {emoji['users']}")],
        ]
        if is_admin:
            buttons.extend([
                [KeyboardButton(f"{emoji['settings']} ADMIN PANEL {emoji['settings']}")],
                [KeyboardButton(f"{emoji['mega']} BROADCAST {emoji['mega']}")],
            ])
        return ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    
    @staticmethod
    def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
        """Get admin panel keyboard"""
        emoji = UIHelper.EMOJIS
        keyboard = [
            [
                InlineKeyboardButton(f"{emoji['users']} USERS", callback_data="admin_users"),
                InlineKeyboardButton(f"{emoji['chart']} STATS", callback_data="admin_stats"),
            ],
            [
                InlineKeyboardButton(f"{emoji['settings']} AUTO LIKES", callback_data="admin_auto_likes"),
                InlineKeyboardButton(f"{emoji['database']} BACKUP", callback_data="admin_backup"),
            ],
            [
                InlineKeyboardButton(f"{emoji['mega']} BROADCAST", callback_data="admin_broadcast"),
                InlineKeyboardButton(f"{emoji['lock']} SETTINGS", callback_data="admin_settings"),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class PremiumFFBot:
    """Premium FF Like Bot"""
    
    def __init__(self, token: str):
        self.token = token
        self.db = Database(DB_PATH)
        self.api = APIClient()
        self.scheduler = AsyncIOScheduler(timezone=IST)
        self.app: Optional[Application] = None
        self.processing_messages: Dict[int, int] = {}  # user_id -> message_id
        
    async def initialize(self):
        """Initialize bot components"""
        await self.db.initialize()
        await self.api.initialize()
        
        # Build application
        defaults = Defaults(
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            allow_sending_without_reply=True,
        )
        
        self.app = ApplicationBuilder()\
            .token(self.token)\
            .defaults(defaults)\
            .concurrent_updates(True)\
            .build()
        
        # Register handlers
        self._register_handlers()
        
        # Setup scheduler
        self._setup_scheduler()
        
        # Setup backup scheduler
        self.scheduler.add_job(
            self._scheduled_backup,
            'interval',
            minutes=30,
            id='backup_job',
            replace_existing=True
        )
        
        # Setup auto like scheduler
        self.scheduler.add_job(
            self._scheduled_auto_likes,
            CronTrigger(hour=AUTO_LIKE_HOUR, minute=AUTO_LIKE_MINUTE),
            id='auto_like_job',
            replace_existing=True
        )
        
        self.scheduler.start()
        
        # Restore from backup if exists
        await self._check_and_restore_backup()
        
    def _register_handlers(self):
        """Register all handlers"""
        # Commands
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("like", self.cmd_like))
        self.app.add_handler(CommandHandler("visit", self.cmd_visit))
        self.app.add_handler(CommandHandler("auto", self.cmd_auto_like))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("admin", self.cmd_admin_panel))
        self.app.add_handler(CommandHandler("broadcast", self.cmd_broadcast))
        self.app.add_handler(CommandHandler("myautolikes", self.cmd_my_auto_likes))
        self.app.add_handler(CommandHandler("removeauto", self.cmd_remove_auto))
        
        # Callback queries
        self.app.add_handler(CallbackQueryHandler(self.cb_verify_channels, pattern="^verify_channels$"))
        self.app.add_handler(CallbackQueryHandler(self.cb_admin, pattern="^admin_"))
        self.app.add_handler(CallbackQueryHandler(self.cb_broadcast, pattern="^broadcast_"))
        
        # Message handlers
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            self.handle_group_message
        ))
        
        # Error handler
        self.app.add_error_handler(self.handle_error)
        
    def _setup_scheduler(self):
        """Setup all scheduled jobs"""
        # Auto backup every 30 minutes
        self.scheduler.add_job(
            self._scheduled_backup,
            'interval',
            minutes=30,
            id='backup_job',
            replace_existing=True
        )
        
        # Auto likes at 4:00 AM IST
        self.scheduler.add_job(
            self._scheduled_auto_likes,
            CronTrigger(hour=AUTO_LIKE_HOUR, minute=AUTO_LIKE_MINUTE, timezone=IST),
            id='auto_like_job',
            replace_existing=True
        )
        
        # Deactivate expired auto likes hourly
        self.scheduler.add_job(
            self._deactivate_expired,
            'interval',
            hours=1,
            id='deactivate_expired_job',
            replace_existing=True
        )
        
    async def _scheduled_backup(self):
        """Send database backup to backup channel"""
        try:
            backup_path = await self.db.backup_database()
            if self.app and self.app.bot:
                with open(backup_path, 'rb') as f:
                    await self.app.bot.send_document(
                        chat_id=BACKUP_CHANNEL,
                        document=f,
                        caption=f"📦 **Database Backup**\n🕐 Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST\n💾 Size: {os.path.getsize(backup_path)} bytes",
                        parse_mode=ParseMode.MARKDOWN
                    )
                os.remove(backup_path)
                logger.info("Database backup sent to channel")
        except Exception as e:
            logger.error(f"Backup failed: {e}")
    
    async def _scheduled_auto_likes(self):
        """Process all auto likes at 4:00 AM"""
        try:
            await self.db.deactivate_expired_auto_likes()
            auto_likes = await self.db.get_active_auto_likes()
            
            if not auto_likes:
                logger.info("No auto likes to process")
                return
            
            logger.info(f"Processing {len(auto_likes)} auto likes at 4:00 AM")
            
            # Process all auto likes concurrently with max 50 workers
            tasks = []
            for auto_like in auto_likes:
                tasks.append(self._process_single_auto_like(auto_like))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success_count = sum(1 for r in results if r and not isinstance(r, Exception))
            fail_count = len(results) - success_count
            
            # Send summary to backup channel
            if self.app and self.app.bot:
                summary = (
                    f"🎯 **AUTO LIKE COMPLETED** 🎯\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Success: {success_count}\n"
                    f"❌ Failed: {fail_count}\n"
                    f"📊 Total: {len(auto_likes)}\n"
                    f"🕐 Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST"
                )
                await self.app.bot.send_message(
                    chat_id=BACKUP_CHANNEL,
                    text=summary,
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logger.error(f"Auto like scheduler failed: {e}")
    
    async def _process_single_auto_like(self, auto_like: Dict) -> bool:
        """Process single auto like entry"""
        try:
            start_time = time.time()
            result = await self.api.call_like_api(auto_like['uid'], auto_like['region'])
            
            if result['success']:
                await self.db.update_auto_like_run(auto_like['id'])
                time_taken = time.time() - start_time
                logger.info(f"Auto like success for UID {auto_like['uid']}: {time_taken:.2f}s")
                return True
            else:
                logger.error(f"Auto like failed for UID {auto_like['uid']}: {result['error']}")
                return False
        except Exception as e:
            logger.error(f"Auto like error: {e}")
            return False
    
    async def _deactivate_expired(self):
        """Deactivate expired auto likes"""
        await self.db.deactivate_expired_auto_likes()
    
    async def _check_and_restore_backup(self):
        """Check and restore latest backup from channel"""
        try:
            if self.app and self.app.bot:
                # Get last message from backup channel
                chat = await self.app.bot.get_chat(BACKUP_CHANNEL)
                messages = await self.app.bot.get_chat_history(
                    chat_id=BACKUP_CHANNEL,
                    limit=1
                )
                
                if messages and len(messages) > 0:
                    last_msg = messages[0]
                    if last_msg.document:
                        file = await self.app.bot.get_file(last_msg.document.file_id)
                        backup_path = "latest_backup.db"
                        await file.download_to_drive(backup_path)
                        
                        # Restore if backup is newer
                        if os.path.exists(self.db.db_path):
                            backup_time = os.path.getmtime(backup_path)
                            current_time = os.path.getmtime(self.db.db_path)
                            if backup_time > current_time:
                                await self.db.restore_database(backup_path)
                                logger.info("Database restored from backup channel")
                        
                        os.remove(backup_path)
        except Exception as e:
            logger.error(f"Backup restore check failed: {e}")
    
    async def is_user_verified(self, user_id: int) -> Tuple[bool, List[str]]:
        """Check if user is verified in all channels"""
        if user_id in ADMIN_IDS:
            return True, []
        
        unverified = []
        for channel in VERIFICATION_CHANNELS:
            if channel['id'] == 0:
                continue
            try:
                member = await self.app.bot.get_chat_member(channel['id'], user_id)
                if member.status in ['left', 'kicked', 'banned']:
                    unverified.append(channel['username'])
            except TelegramError:
                # If can't check, assume not verified
                unverified.append(channel['username'])
        
        return len(unverified) == 0, unverified
    
    async def send_animated_processing(self, update: Update, name: str, api_type: str) -> int:
        """Send animated processing message"""
        message = await update.message.reply_text(
            UIHelper.format_processing_message(name, api_type),
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Animate the message
        frames = UIHelper.get_loading_animation()
        for i in range(2):  # Loop animation twice
            for frame in frames:
                await asyncio.sleep(0.1)
                try:
                    text = UIHelper.format_processing_message(name, api_type)
                    text = text.replace("⚡ Processing...", f"⚡ Processing...\n{frame}")
                    await message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                except:
                    break
        
        return message.message_id
    
    async def send_success_popup(self, update: Update, text: str):
        """Send success popup message"""
        await update.message.reply_text(
            f"✅ {text}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    def _extract_uid_and_region(self, args: List[str]) -> Tuple[Optional[str], Optional[str]]:
        """Extract UID and region from command arguments"""
        if len(args) < 2:
            return None, None
        
        region = args[0].lower()
        uid = args[1]
        
        # Validate region
        if region not in ['ind', 'rus', 'in', 'id', 'br', 'eg', 'me', 'bd', 'pk', 'np', 'lk', 'us', 'uk', 'sg', 'tr', 'sa', 'ae', 'kw', 'qa', 'bh', 'om']:
            # Try to find region in uid
            if region.isdigit():
                uid = region
                region = 'ind'
            else:
                return None, None
        
        # Validate UID
        if not uid.isdigit() or len(uid) < 5:
            return None, None
        
        return uid, region
    
    # ═══════════════════════════════════════════════════════════════════════
    # COMMAND HANDLERS
    # ═══════════════════════════════════════════════════════════════════════
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        chat = update.effective_chat
        
        # Save user
        await self.db.create_or_update_user(user.id, user.username or "", user.first_name)
        
        # Only work in groups
        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            welcome = (
                f"👑 **WELCOME TO PREMIUM FF BOT** 👑\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🚀 **Add me to your group to use my services**\n\n"
                f"📋 **Available Services:**\n"
                f"❤️ Like Boost\n"
                f"👁️ Profile Visits\n"
                f"⚡ Auto Like (4 AM)\n\n"
                f"🔗 **Click below to add me:**"
            )
            await update.message.reply_text(
                welcome,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_add_bot_button()
            )
            return
        
        # Check verification
        is_verified, unverified = await self.is_user_verified(user.id)
        
        if not is_verified:
            await update.message.reply_text(
                f"🔒 **VERIFICATION REQUIRED** 🔒\n\n"
                f"Please join all channels to use this bot:\n\n"
                f"{chr(10).join([f'📢 {ch}' for ch in unverified])}\n\n"
                f"After joining, click 'Verify' button.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_verification_keyboard(unverified)
            )
            return
        
        # Mark verified
        await self.db.create_or_update_user(user.id, user.username or "", user.first_name)
        
        welcome_msg = (
            f"{UIHelper.EMOJIS['party']} **WELCOME TO PREMIUM FF BOT** {UIHelper.EMOJIS['party']}\n"
            f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
            f"{UIHelper.EMOJIS['crown']} **Hello, {user.first_name}!**\n"
            f"{UIHelper.EMOJIS['star']} I'm your premium FF service bot\n\n"
            f"{UIHelper.EMOJIS['fire']} **SERVICES:**\n"
            f"{UIHelper.EMOJIS['heart']} ❤️ Like Boost - /like ind UID\n"
            f"{UIHelper.EMOJIS['eye']} 👁️ Profile Visit - /visit IND UID\n"
            f"{UIHelper.EMOJIS['bolt']} ⚡ Auto Like - /auto ind UID days name\n\n"
            f"{UIHelper.EMOJIS['info']} Type /help for full commands list"
        )
        
        await update.message.reply_text(
            welcome_msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=UIHelper.get_main_menu_keyboard(user.id in ADMIN_IDS)
        )
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        user = update.effective_user
        
        # Check verification
        is_verified, unverified = await self.is_user_verified(user.id)
        if not is_verified:
            await update.message.reply_text(
                f"🔒 **VERIFICATION REQUIRED** 🔒\n\n"
                f"Please join all channels first.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_verification_keyboard(unverified)
            )
            return
        
        help_text = (
            f"{UIHelper.EMOJIS['info']} **HELP CENTER** {UIHelper.EMOJIS['info']}\n"
            f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
            f"{UIHelper.EMOJIS['heart']} **LIKE COMMAND:**\n"
            f"{UIHelper.EMOJIS['finger']} /like ind UID\n"
            f"{UIHelper.EMOJIS['lock']} Limit: 1 per day\n\n"
            f"{UIHelper.EMOJIS['eye']} **VISIT COMMAND:**\n"
            f"{UIHelper.EMOJIS['finger']} /visit IND UID\n"
            f"{UIHelper.EMOJIS['timer']} Cooldown: 25 seconds\n\n"
            f"{UIHelper.EMOJIS['bolt']} **AUTO LIKE (ADMIN):**\n"
            f"{UIHelper.EMOJIS['finger']} /auto ind UID days name\n"
            f"{UIHelper.EMOJIS['calendar']} Runs daily at 4:00 AM\n\n"
            f"{UIHelper.EMOJIS['users']} **MY STATS:**\n"
            f"{UIHelper.EMOJIS['finger']} /stats\n\n"
            f"{UIHelper.EMOJIS['rocket']} **PREMIUM FEATURES:**\n"
            f"{UIHelper.EMOJIS['check']} Ultra-fast processing\n"
            f"{UIHelper.EMOJIS['check']} Multiple concurrent requests\n"
            f"{UIHelper.EMOJIS['check']} Auto backup system\n"
            f"{UIHelper.EMOJIS['check']} 24/7 availability"
        )
        
        await update.message.reply_text(
            help_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=UIHelper.get_main_menu_keyboard(user.id in ADMIN_IDS)
        )
    
    async def cmd_like(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /like command"""
        user = update.effective_user
        chat = update.effective_chat
        
        # Only in groups
        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            await update.message.reply_text(
                "❌ This bot only works in groups. Please add me to a group.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Check verification
        is_verified, unverified = await self.is_user_verified(user.id)
        if not is_verified:
            await update.message.reply_text(
                f"🔒 **VERIFICATION REQUIRED** 🔒\n\n"
                f"Please join all channels first.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_verification_keyboard(unverified)
            )
            return
        
        # Check daily limit
        can_like = await self.db.can_use_like(user.id)
        if not can_like:
            user_data = await self.db.get_user(user.id)
            last_like = datetime.fromisoformat(user_data['last_like_date']) if user_data else None
            next_like = last_like + timedelta(days=1) if last_like else datetime.now(IST)
            
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['lock']} **DAILY LIMIT REACHED** {UIHelper.EMOJIS['lock']}\n"
                f"{UIHelper.EMOJIS['timer']} You can use like again after:\n"
                f"{UIHelper.EMOJIS['calendar']} {next_like.strftime('%Y-%m-%d %H:%M:%S')} IST",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Parse arguments
        args = context.args
        uid, region = self._extract_uid_and_region(args)
        
        if not uid or not region:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['warning']} **USAGE:**\n"
                f"{UIHelper.EMOJIS['finger']} /like ind UID\n\n"
                f"{UIHelper.EMOJIS['info']} Example: /like ind 123456789",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Send processing message
        processing_msg = await update.message.reply_text(
            UIHelper.format_processing_message(uid, "like"),
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Animate processing
        frames = UIHelper.get_loading_animation()
        for frame in frames[:6]:
            await asyncio.sleep(0.2)
            try:
                await processing_msg.edit_text(
                    UIHelper.format_processing_message(uid, "like").replace(
                        "⚡ Processing...",
                        f"⚡ Processing...\n{frame}"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                break
        
        # Call API
        start_time = time.time()
        result = await self.api.call_like_api(uid, region)
        time_taken = time.time() - start_time
        
        if result['success']:
            data = result['data']
            
            # Format response
            response_text = UIHelper.format_player_info(data, "like", time_taken)
            
            # Update database
            await self.db.update_like_usage(user.id)
            
            # Edit message with result
            await processing_msg.edit_text(
                response_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_add_bot_button()
            )
            
            # Send success popup
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['party']} **LIKE SUCCESSFUL!** {UIHelper.EMOJIS['party']}",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            error_text = (
                f"{UIHelper.EMOJIS['cross']} **LIKE FAILED** {UIHelper.EMOJIS['cross']}\n"
                f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
                f"{UIHelper.EMOJIS['warning']} **Error:** {result['error']}\n"
                f"{UIHelper.EMOJIS['info']} Please try again later."
            )
            await processing_msg.edit_text(
                error_text,
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def cmd_visit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /visit command"""
        user = update.effective_user
        chat = update.effective_chat
        
        # Only in groups
        if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            await update.message.reply_text(
                "❌ This bot only works in groups. Please add me to a group.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Check verification
        is_verified, unverified = await self.is_user_verified(user.id)
        if not is_verified:
            await update.message.reply_text(
                f"🔒 **VERIFICATION REQUIRED** 🔒\n\n"
                f"Please join all channels first.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_verification_keyboard(unverified)
            )
            return
        
        # Check cooldown
        last_visit = await self.db.get_visit_cooldown(user.id)
        if last_visit:
            elapsed = (datetime.now(IST) - last_visit).total_seconds()
            if elapsed < VISIT_COOLDOWN_SECONDS:
                remaining = int(VISIT_COOLDOWN_SECONDS - elapsed)
                await update.message.reply_text(
                    f"{UIHelper.EMOJIS['timer']} **COOLDOWN ACTIVE** {UIHelper.EMOJIS['timer']}\n"
                    f"{UIHelper.EMOJIS['clock']} Wait {remaining} more seconds before next visit.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
        
        # Parse arguments
        args = context.args
        uid, region = self._extract_uid_and_region(args)
        
        if not uid or not region:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['warning']} **USAGE:**\n"
                f"{UIHelper.EMOJIS['finger']} /visit IND UID\n\n"
                f"{UIHelper.EMOJIS['info']} Example: /visit IND 123456789",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Send processing message
        processing_msg = await update.message.reply_text(
            UIHelper.format_processing_message(uid, "visit"),
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Animate processing
        frames = UIHelper.get_loading_animation()
        for frame in frames[:6]:
            await asyncio.sleep(0.2)
            try:
                await processing_msg.edit_text(
                    UIHelper.format_processing_message(uid, "visit").replace(
                        "⚡ Processing...",
                        f"⚡ Processing...\n{frame}"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                break
        
        # Call API
        start_time = time.time()
        result = await self.api.call_visit_api(uid, region)
        time_taken = time.time() - start_time
        
        if result['success']:
            data = result['data']
            
            # Format response
            response_text = UIHelper.format_player_info(data, "visit", time_taken)
            
            # Update database
            await self.db.update_visit_usage(user.id)
            
            # Edit message with result
            await processing_msg.edit_text(
                response_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_add_bot_button()
            )
            
            # Send success popup
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['party']} **VISIT SUCCESSFUL!** {UIHelper.EMOJIS['party']}",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            error_text = (
                f"{UIHelper.EMOJIS['cross']} **VISIT FAILED** {UIHelper.EMOJIS['cross']}\n"
                f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
                f"{UIHelper.EMOJIS['warning']} **Error:** {result['error']}\n"
                f"{UIHelper.EMOJIS['info']} Please try again later."
            )
            await processing_msg.edit_text(
                error_text,
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def cmd_auto_like(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /auto command - Admin only"""
        user = update.effective_user
        chat = update.effective_chat
        
        # Admin check
        if user.id not in ADMIN_IDS:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['lock']} **ADMIN ONLY** {UIHelper.EMOJIS['lock']}\n"
                f"This command is restricted to admins.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Parse arguments: /auto ind UID days name
        args = context.args
        if len(args) < 3:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['warning']} **USAGE:**\n"
                f"{UIHelper.EMOJIS['finger']} /auto ind UID days name\n\n"
                f"{UIHelper.EMOJIS['info']} Example: /auto ind 123456789 30 MyAutoLike",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        region = args[0].lower()
        uid = args[1]
        days = int(args[2])
        name = " ".join(args[3:]) if len(args) > 3 else f"Auto_{uid}"
        
        # Validate
        if not uid.isdigit() or days <= 0:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['cross']} **INVALID INPUT**\n"
                f"UID must be numeric and days must be positive.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Add auto like
        auto_id = await self.db.add_auto_like(region, uid, days, name, chat.id)
        
        expires = datetime.now(IST) + timedelta(days=days)
        
        success_msg = (
            f"{UIHelper.EMOJIS['party']} **AUTO LIKE ACTIVATED** {UIHelper.EMOJIS['party']}\n"
            f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
            f"{UIHelper.EMOJIS['id']} **UID:** {uid}\n"
            f"{UIHelper.EMOJIS['globe']} **Region:** {region.upper()}\n"
            f"{UIHelper.EMOJIS['calendar']} **Days:** {days}\n"
            f"{UIHelper.EMOJIS['bookmark']} **Name:** {name}\n"
            f"{UIHelper.EMOJIS['timer']} **Runs:** Daily at 4:00 AM IST\n"
            f"{UIHelper.EMOJIS['clock']} **Expires:** {expires.strftime('%Y-%m-%d %H:%M:%S')} IST\n\n"
            f"{UIHelper.EMOJIS['bolt']} **Status:** ⚡ ACTIVE"
        )
        
        await update.message.reply_text(
            success_msg,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        user = update.effective_user
        
        user_data = await self.db.get_user(user.id)
        if not user_data:
            await self.db.create_or_update_user(user.id, user.username or "", user.first_name)
            user_data = await self.db.get_user(user.id)
        
        stats_msg = (
            f"{UIHelper.EMOJIS['chart']} **YOUR STATISTICS** {UIHelper.EMOJIS['chart']}\n"
            f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
            f"{UIHelper.EMOJIS['id']} **User ID:** {user.id}\n"
            f"{UIHelper.EMOJIS['heart']} **Total Likes:** {user_data.get('total_likes', 0)}\n"
            f"{UIHelper.EMOJIS['eye']} **Total Visits:** {user_data.get('total_visits', 0)}\n"
            f"{UIHelper.EMOJIS['calendar']} **Joined:** {user_data.get('joined_at', 'N/A')}\n\n"
            f"{UIHelper.EMOJIS['rocket']} **Premium Bot Service** {UIHelper.EMOJIS['rocket']}"
        )
        
        await update.message.reply_text(
            stats_msg,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def cmd_admin_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /admin command - Admin panel"""
        user = update.effective_user
        
        if user.id not in ADMIN_IDS:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['lock']} **ACCESS DENIED** {UIHelper.EMOJIS['lock']}\n"
                f"Admin only command.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        total_users = await self.db.get_total_users()
        total_auto = await self.db.get_total_auto_likes()
        
        admin_msg = (
            f"{UIHelper.EMOJIS['crown']} **ADMIN PANEL** {UIHelper.EMOJIS['crown']}\n"
            f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
            f"{UIHelper.EMOJIS['users']} **Total Users:** {total_users}\n"
            f"{UIHelper.EMOJIS['bolt']} **Active Auto Likes:** {total_auto}\n"
            f"{UIHelper.EMOJIS['database']} **Database:** Connected\n"
            f"{UIHelper.EMOJIS['timer']} **Backup:** Every 30 mins\n\n"
            f"{UIHelper.EMOJIS['rocket']} **Premium Admin Panel** {UIHelper.EMOJIS['rocket']}"
        )
        
        await update.message.reply_text(
            admin_msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=UIHelper.get_admin_panel_keyboard()
        )
    
    async def cmd_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /broadcast command - Admin only"""
        user = update.effective_user
        
        if user.id not in ADMIN_IDS:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['lock']} **ADMIN ONLY** {UIHelper.EMOJIS['lock']}",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        if not context.args:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['warning']} **USAGE:**\n"
                f"/broadcast Your message here",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        message = " ".join(context.args)
        
        # Get all users
        cursor = await self.db.conn.execute("SELECT user_id FROM users")
        rows = await cursor.fetchall()
        
        success = 0
        failed = 0
        
        for row in rows:
            user_id = row[0]
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=f"{UIHelper.EMOJIS['mega']} **BROADCAST** {UIHelper.EMOJIS['mega']}\n\n{message}",
                    parse_mode=ParseMode.MARKDOWN
                )
                success += 1
                await asyncio.sleep(0.05)  # Rate limit protection
            except:
                failed += 1
        
        await update.message.reply_text(
            f"{UIHelper.EMOJIS['check']} **BROADCAST COMPLETED** {UIHelper.EMOJIS['check']}\n"
            f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
            f"{UIHelper.EMOJIS['check']} **Success:** {success}\n"
            f"{UIHelper.EMOJIS['cross']} **Failed:** {failed}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def cmd_my_auto_likes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /myautolikes command"""
        user = update.effective_user
        
        auto_likes = await self.db.get_auto_likes_by_group(update.effective_chat.id)
        
        if not auto_likes:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['info']} **NO ACTIVE AUTO LIKES**\n"
                f"Use /auto command to add auto likes.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        text = f"{UIHelper.EMOJIS['bolt']} **ACTIVE AUTO LIKES** {UIHelper.EMOJIS['bolt']}\n"
        text += f"{UIHelper.EMOJIS['sparkles']} {UIHelper.get_border_line()} {UIHelper.EMOJIS['sparkles']}\n\n"
        
        for i, auto in enumerate(auto_likes, 1):
            text += (
                f"{UIHelper.EMOJIS['star']} **#{i}**\n"
                f"{UIHelper.EMOJIS['id']} UID: {auto['uid']}\n"
                f"{UIHelper.EMOJIS['globe']} Region: {auto['region'].upper()}\n"
                f"{UIHelper.EMOJIS['bookmark']} Name: {auto['name']}\n"
                f"{UIHelper.EMOJIS['calendar']} Expires: {auto['expires_at'][:10]}\n"
                f"{UIHelper.EMOJIS['finger']} Remove: /removeauto {auto['id']}\n\n"
            )
        
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def cmd_remove_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /removeauto command"""
        user = update.effective_user
        
        if not context.args:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['warning']} **USAGE:**\n"
                f"/removeauto ID",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        auto_id = int(context.args[0])
        await self.db.remove_auto_like(auto_id)
        
        await update.message.reply_text(
            f"{UIHelper.EMOJIS['check']} **AUTO LIKE REMOVED** {UIHelper.EMOJIS['check']}\n"
            f"ID: {auto_id}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    # ═══════════════════════════════════════════════════════════════════════
    # CALLBACK HANDLERS
    # ═══════════════════════════════════════════════════════════════════════
    
    async def cb_verify_channels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle verification callback"""
        query = update.callback_query
        user = query.from_user
        
        await query.answer("Checking verification...")
        
        is_verified, unverified = await self.is_user_verified(user.id)
        
        if is_verified:
            await query.edit_message_text(
                f"{UIHelper.EMOJIS['party']} **VERIFICATION SUCCESSFUL!** {UIHelper.EMOJIS['party']}\n"
                f"{UIHelper.EMOJIS['check']} You can now use the bot.\n"
                f"{UIHelper.EMOJIS['finger']} Type /help for commands.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text(
                f"{UIHelper.EMOJIS['lock']} **VERIFICATION FAILED** {UIHelper.EMOJIS['lock']}\n\n"
                f"Still not joined:\n"
                f"{chr(10).join([f'📢 {ch}' for ch in unverified])}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_verification_keyboard(unverified)
            )
    
    async def cb_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin callback"""
        query = update.callback_query
        user = query.from_user
        
        if user.id not in ADMIN_IDS:
            await query.answer("Access denied", show_alert=True)
            return
        
        action = query.data
        
        if action == "admin_users":
            total_users = await self.db.get_total_users()
            await query.answer(f"Total Users: {total_users}", show_alert=True)
        
        elif action == "admin_stats":
            total_users = await self.db.get_total_users()
            total_auto = await self.db.get_total_auto_likes()
            await query.answer(
                f"Users: {total_users}\nAuto Likes: {total_auto}",
                show_alert=True
            )
        
        elif action == "admin_auto_likes":
            auto_likes = await self.db.get_active_auto_likes()
            text = f"**ACTIVE AUTO LIKES:** {len(auto_likes)}\n\n"
            for auto in auto_likes[:10]:
                text += f"UID: {auto['uid']} | Name: {auto['name']} | Expires: {auto['expires_at'][:10]}\n"
            
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_admin_panel_keyboard()
            )
        
        elif action == "admin_backup":
            await query.answer("Creating backup...")
            backup_path = await self.db.backup_database()
            with open(backup_path, 'rb') as f:
                await self.app.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=f,
                    caption="📦 **DATABASE BACKUP**",
                    parse_mode=ParseMode.MARKDOWN
                )
            os.remove(backup_path)
            await query.answer("Backup sent!", show_alert=True)
        
        elif action == "admin_broadcast":
            await query.edit_message_text(
                f"{UIHelper.EMOJIS['mega']} **BROADCAST MODE** {UIHelper.EMOJIS['mega']}\n\n"
                f"Send message to broadcast using:\n"
                f"/broadcast Your message here",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif action == "admin_settings":
            await query.edit_message_text(
                f"{UIHelper.EMOJIS['settings']} **BOT SETTINGS** {UIHelper.EMOJIS['settings']}\n\n"
                f"• Verification: Enabled\n"
                f"• Like Limit: 1/day\n"
                f"• Visit Cooldown: 25 seconds\n"
                f"• Auto Like Time: 4:00 AM IST\n"
                f"• Backup: Every 30 minutes",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIHelper.get_admin_panel_keyboard()
            )
    
    async def cb_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle broadcast callback"""
        query = update.callback_query
        await query.answer("Use /broadcast command to send messages", show_alert=True)
    
    # ═══════════════════════════════════════════════════════════════════════
    # MESSAGE HANDLERS
    # ═══════════════════════════════════════════════════════════════════════
    
    async def handle_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle group messages - only respond to commands"""
        # Only respond to commands, ignore other messages
        message_text = update.message.text or ""
        
        # Check for button clicks from keyboard
        user = update.effective_user
        
        if "LIKE SERVICE" in message_text:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['heart']} **LIKE SERVICE** {UIHelper.EMOJIS['heart']}\n"
                f"{UIHelper.EMOJIS['finger']} Use: /like ind UID\n"
                f"{UIHelper.EMOJIS['lock']} Limit: 1 per day",
                parse_mode=ParseMode.MARKDOWN
            )
        elif "VISIT SERVICE" in message_text:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['eye']} **VISIT SERVICE** {UIHelper.EMOJIS['eye']}\n"
                f"{UIHelper.EMOJIS['finger']} Use: /visit IND UID\n"
                f"{UIHelper.EMOJIS['timer']} Cooldown: 25 seconds",
                parse_mode=ParseMode.MARKDOWN
            )
        elif "HELP" in message_text:
            await self.cmd_help(update, context)
        elif "MY STATS" in message_text:
            await self.cmd_stats(update, context)
        elif "ADMIN PANEL" in message_text and user.id in ADMIN_IDS:
            await self.cmd_admin_panel(update, context)
        elif "BROADCAST" in message_text and user.id in ADMIN_IDS:
            await update.message.reply_text(
                f"{UIHelper.EMOJIS['mega']} **BROADCAST** {UIHelper.EMOJIS['mega']}\n"
                f"Use: /broadcast Your message",
                parse_mode=ParseMode.MARKDOWN
            )
    
    async def handle_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        error = context.error
        logger.error(f"Update {update} caused error {error}")
        
        try:
            if isinstance(error, RetryAfter):
                await asyncio.sleep(error.retry_after)
            elif isinstance(error, TimedOut):
                logger.warning("Request timed out")
        except:
            pass
    
    async def start(self):
        """Start the bot"""
        await self.initialize()
        logger.info("Bot is starting...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot is running!")
    
    async def stop(self):
        """Stop the bot"""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        await self.api.close()
        await self.db.close()
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("Bot stopped")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    """Main entry point"""
    bot = PremiumFFBot(BOT_TOKEN)
    
    try:
        await bot.start()
        
        # Keep running
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        await bot.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped")