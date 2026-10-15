import os
import json
import copy
import time
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "WarMod Pro"
DB_FILE = "database.json"
MONGO_URL = os.getenv("MONGO_URL")

_mongo_col = None
if MONGO_URL:
    try:
        from pymongo import MongoClient
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        _mongo_col = _client["warmod_db"]["state"]
    except Exception as e:
        print(f"⚠️ MongoDB connection error, falling back to local file: {e}")

MODULES = {
    "moderation": "Moderation",
    "automod": "AutoMod",
    "logging": "Logging",
    "welcome": "Welcome & Leave",
    "autorole": "Auto-Role",
    "custom": "Custom Commands",
    "utility": "Utility & Info",
    "announce": "Announcements",
    "war": "War & Economy",
}

SHOP_ITEMS = {
    "sword": {"name": "⚡ Power Booster", "cost": 250, "desc": "+15 bonus power in your next 1v1 War"},
    "shield": {"name": "🛡️ ELO Shield", "cost": 300, "desc": "Prevents ELO loss on your next defeat"},
}

COMMANDS = {
    "kick": {"module": "moderation", "description": "Kick a member from the server"},
    "ban": {"module": "moderation", "description": "Ban a member from the server"},
    "unban": {"module": "moderation", "description": "Unban a user by ID"},
    "softban": {"module": "moderation", "description": "Ban then unban a member to clear their messages"},
    "timeout": {"module": "moderation", "description": "Timeout (mute) a member for some minutes"},
    "untimeout": {"module": "moderation", "description": "Remove a member's timeout"},
    "warn": {"module": "moderation", "description": "Issue a warning (auto-timeouts at threshold)"},
    "warnconfig": {"module": "moderation", "description": "Configure DM notices & auto-timeout on warnings"},
    "dm": {"module": "moderation", "description": "Send an official staff DM to a member"},
    "warnings": {"module": "moderation", "description": "View a member's warnings"},
    "delwarn": {"module": "moderation", "description": "Delete one warning by its ID"},
    "clearwarnings": {"module": "moderation", "description": "Clear all warnings for a member"},
    "purge": {"module": "moderation", "description": "Bulk delete messages (1-100)"},
    "nuke": {"module": "moderation", "description": "Clone and clear the entire channel"},
    "lock": {"module": "moderation", "description": "Lock the current channel"},
    "unlock": {"module": "moderation", "description": "Unlock the current channel"},
    "slowmode": {"module": "moderation", "description": "Set slowmode for this channel (0 to disable)"},
    "nick": {"module": "moderation", "description": "Change or reset a member's nickname"},
    "addrole": {"module": "moderation", "description": "Give a role to a member"},
    "removerole": {"module": "moderation", "description": "Remove a role from a member"},
    "addword": {"module": "automod", "description": "Add a word to the banned words list"},
    "removeword": {"module": "automod", "description": "Remove a word from the banned words list"},
    "words": {"module": "automod", "description": "Show the banned words list"},
    "tag_add": {"module": "custom", "description": "Create or update a custom command"},
    "tag_remove": {"module": "custom", "description": "Delete a custom command"},
    "tags": {"module": "custom", "description": "List custom commands"},
    "say": {"module": "announce", "description": "Make the bot send a message"},
    "announce": {"module": "announce", "description": "Send an announcement embed"},
    "poll": {"module": "announce", "description": "Create a reaction poll"},
    "giveaway": {"module": "announce", "description": "Start an interactive giveaway"},
    "ping": {"module": "utility", "description": "Check the bot's latency & uptime"},
    "userinfo": {"module": "utility", "description": "Show info about a member"},
    "serverinfo": {"module": "utility", "description": "Show info about this server"},
    "roleinfo": {"module": "utility", "description": "Show info about a role"},
    "avatar": {"module": "utility", "description": "Show a member's avatar"},
    "help": {"module": "core", "description": "Interactive command explorer"},
    "setup": {"module": "core", "description": "Quick-configure the bot"},
    "war": {"module": "war", "description": "Challenge a player to a 1v1 duel"},
    "wager": {"module": "war", "description": "Duel a player for a coin bet"},
    "daily": {"module": "war", "description": "Claim your daily coins & streak bonus"},
    "shop": {"module": "war", "description": "View the War item shop"},
    "buy": {"module": "war", "description": "Buy a booster or shield from the shop"},
    "leaderboard": {"module": "war", "description": "View the top War players"},
    "profile": {"module": "war", "description": "View your War stats & inventory"},
}

# --- HIGH SPEED CACHING SYSTEM ---
_cached_db = {}
_last_fetch = 0

def load_db():
    global _cached_db, _last_fetch
    now = time.time()
    
    # Only fetch from the cloud if 5 seconds have passed
    if _mongo_col is not None and (now - _last_fetch > 5):
        try:
            doc = _mongo_col.find_one({"_id": "main"})
            if doc:
                doc.pop("_id", None)
                _cached_db = doc
                _last_fetch = now
        except Exception:
            pass
    elif not _cached_db and os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                _cached_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    for key in ("users", "guilds", "warnings"):
        _cached_db.setdefault(key, {})
    return _cached_db


def save_db(data):
    global _cached_db, _last_fetch
    _cached_db = copy.deepcopy(data)
    _last_fetch = time.time()  # Reset the timer so we don't immediately overwrite our own save
    
    if _mongo_col is not None:
        try:
            payload = copy.deepcopy(data)
            _mongo_col.replace_one({"_id": "main"}, {"_id": "main", **payload}, upsert=True)
            return
        except Exception as e:
            print(f"⚠️ MongoDB save error: {e}")
            
    tmp = f"{DB_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DB_FILE)
# ---------------------------------

def default_guild():
    return {
        "prefix": "!",
        "warnCounter": 0,
        "dmOnMod": True,
        "warnPunish": {"enabled": True, "threshold": 3, "timeoutMinutes": 60},
        "modLogChannel": None,
        "log": {"moderation": True, "messages": True, "members": True},
        "autoRoles": [],
        "welcome": {
            "channel": None,
            "joinEnabled": False,
            "joinMessage": "Welcome {user} to **{server}**! You are member #{count}.",
            "leaveEnabled": False,
            "leaveMessage": "**{username}** has left the server.",
        },
        "automod": {
            "antiLink": False,
            "antiInvite": False,
            "antiCaps": False,
            "antiSpam": False,
            "antiMassMention": False,
            "maxMentions": 5,
            "bannedWords": [],
        },
        "customCommands": {},
        "disabledCommands": [],
        "modules": {k: True for k in MODULES},
    }


def _merge(cur, default):
    for k, v in default.items():
        if k not in cur:
            cur[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(cur[k], dict) and k != "customCommands":
            _merge(cur[k], v)


def get_guild(db, guild_id):
    gid = str(guild_id)
    g = db["guilds"].get(gid)
    if g is None:
        g = default_guild()
        db["guilds"][gid] = g
        save_db(db)
    else:
        _merge(g, default_guild())
    return g


def get_user(db, user_id, username=None):
    uid = str(user_id)
    u = db["users"].get(uid)
    default_u = {
        "id": uid,
        "username": username or "Unknown",
        "elo": 1000,
        "wins": 0,
        "losses": 0,
        "coins": 150,
        "streak": 0,
        "lastDaily": 0,
        "lastDailyWar": 0,
        "items": {"sword": 0, "shield": 0},
    }
    if u is None:
        db["users"][uid] = default_u
        return default_u
    _merge(u, default_u)
    if username:
        u["username"] = username
    return u


def now_ts():
    return int(time.time())
