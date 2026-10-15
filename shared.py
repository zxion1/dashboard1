import os
import json
import copy
import time
import threading
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "BBC"
DB_FILE = "database.json"
MONGO_URL = os.getenv("MONGO_URL")

_mongo_col = None
if MONGO_URL:
    try:
        from pymongo import MongoClient
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        _mongo_col = _client["bbc_db"]["state"]
    except Exception as e:
        print(f"⚠️ MongoDB connection error, falling back to local file: {e}")

MODULES = {
    "moderation": "Moderation",
    "automod": "AutoMod",
    "logging": "Logging",
    "welcome": "Welcome & Leave",
    "autorole": "Auto-Role",
    "custom": "Custom Tags",
    "utility": "Utility & Tools",
    "announce": "Announcements & Giveaways",
}

COMMANDS = {
    "kick": {"module": "moderation", "description": "Kick a member from the server"},
    "ban": {"module": "moderation", "description": "Ban a member from the server"},
    "unban": {"module": "moderation", "description": "Unban a user by ID"},
    "softban": {"module": "moderation", "description": "Ban then unban a member to wipe their messages"},
    "timeout": {"module": "moderation", "description": "Timeout (mute) a member"},
    "untimeout": {"module": "moderation", "description": "Remove a member's timeout"},
    "warn": {"module": "moderation", "description": "Issue an official warning"},
    "warnconfig": {"module": "moderation", "description": "Configure DM notices & auto-timeout on warnings"},
    "dm": {"module": "moderation", "description": "Send an official staff DM to a member"},
    "warnings": {"module": "moderation", "description": "View a member's warnings"},
    "delwarn": {"module": "moderation", "description": "Delete a single warning by ID"},
    "clearwarnings": {"module": "moderation", "description": "Clear all warnings for a member"},
    "purge": {"module": "moderation", "description": "Bulk delete messages (1-100)"},
    "nuke": {"module": "moderation", "description": "Clone and clear the entire channel"},
    "lock": {"module": "moderation", "description": "Lock the current channel"},
    "unlock": {"module": "moderation", "description": "Unlock the current channel"},
    "lockdown": {"module": "moderation", "description": "Lock all text channels in the server"},
    "unlockdown": {"module": "moderation", "description": "Unlock all text channels in the server"},
    "slowmode": {"module": "moderation", "description": "Set channel slowmode (0 to disable)"},
    "nick": {"module": "moderation", "description": "Change or reset a member's nickname"},
    "addrole": {"module": "moderation", "description": "Give a role to a member"},
    "removerole": {"module": "moderation", "description": "Remove a role from a member"},
    "roleall": {"module": "moderation", "description": "Give a role to all human members"},
    "addword": {"module": "automod", "description": "Add a word to the banned words filter"},
    "removeword": {"module": "automod", "description": "Remove a word from the banned words filter"},
    "words": {"module": "automod", "description": "View all filtered words"},
    "tag_add": {"module": "custom", "description": "Create or update a custom tag"},
    "tag_remove": {"module": "custom", "description": "Delete a custom tag"},
    "tags": {"module": "custom", "description": "List all custom tags"},
    "say": {"module": "announce", "description": "Send a clean message as the bot"},
    "announce": {"module": "announce", "description": "Publish a formatted announcement embed"},
    "poll": {"module": "announce", "description": "Create a native Discord voting poll"},
    "giveaway": {"module": "announce", "description": "Launch an advanced interactive giveaway"},
    "reroll": {"module": "announce", "description": "Reroll a giveaway winner by message ID"},
    "ping": {"module": "utility", "description": "View bot latency and uptime"},
    "userinfo": {"module": "utility", "description": "Inspect a member's profile and roles"},
    "serverinfo": {"module": "utility", "description": "View detailed server statistics"},
    "membercount": {"module": "utility", "description": "View human and bot member counts"},
    "roleinfo": {"module": "utility", "description": "Inspect role details and permissions"},
    "avatar": {"module": "utility", "description": "Get a member's high-res avatar"},
    "banner": {"module": "utility", "description": "Get a member's profile banner"},
    "snipe": {"module": "utility", "description": "View the last deleted message in this channel"},
    "steal": {"module": "utility", "description": "Clone a custom emoji into this server"},
    "timer": {"module": "utility", "description": "Set a countdown reminder"},
    "help": {"module": "core", "description": "Browse all commands by category"},
    "setup": {"module": "core", "description": "Configure channels & test welcome/leave messages"},
}

_cached_db = None
_last_fetch = 0
_lock = threading.Lock()


def _bg_fetch():
    global _cached_db, _last_fetch
    if _mongo_col is not None:
        try:
            doc = _mongo_col.find_one({"_id": "main"})
            if doc:
                doc.pop("_id", None)
                with _lock:
                    _cached_db = doc
                    _last_fetch = time.time()
        except Exception:
            pass


def _bg_save(payload):
    if _mongo_col is not None:
        try:
            _mongo_col.replace_one({"_id": "main"}, {"_id": "main", **payload}, upsert=True)
            return
        except Exception as e:
            print(f"⚠️ MongoDB background save error: {e}")
    try:
        tmp = f"{DB_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, DB_FILE)
    except OSError:
        pass


def load_db():
    global _cached_db, _last_fetch
    now = time.time()

    if _cached_db is None:
        _cached_db = {"guilds": {}, "warnings": {}, "giveaways": {}}
        if _mongo_col is not None:
            _bg_fetch()
        elif os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    _cached_db = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        _last_fetch = now

    elif _mongo_col is not None and (now - _last_fetch > 6):
        _last_fetch = now
        threading.Thread(target=_bg_fetch, daemon=True).start()

    for key in ("guilds", "warnings", "giveaways"):
        _cached_db.setdefault(key, {})
    return _cached_db


def save_db(data):
    global _cached_db, _last_fetch
    with _lock:
        _cached_db = copy.deepcopy(data)
        _last_fetch = time.time()
        payload = copy.deepcopy(data)
    threading.Thread(target=_bg_save, args=(payload,), daemon=True).start()


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
            "joinMessage": "Welcome {user} to **{server}**! You are member **#{count}**.",
            "leaveEnabled": False,
            "leaveMessage": "**{username}** has left **{server}**.",
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


def format_welcome_text(template: str, member_mention: str, username: str, server_name: str, count: int) -> str:
    return (
        template
        .replace("{user}", member_mention)
        .replace("{username}", username)
        .replace("{server}", server_name)
        .replace("{count}", str(count))
    )


def now_ts():
    return int(time.time())
