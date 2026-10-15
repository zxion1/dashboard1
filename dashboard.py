import os
import re
import time
import html
import hmac
import asyncio
import secrets
import datetime
from urllib.parse import quote
from collections import defaultdict

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
import uvicorn

from shared import (
    BOT_NAME, MODULES, COMMANDS,
    load_db, save_db, get_guild, now_ts
)

load_dotenv()
TOKEN = os.getenv("TOKEN")
PORT = int(os.getenv("PORT", 8000))
DASH_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
if not DASH_PASSWORD:
    DASH_PASSWORD = secrets.token_urlsafe(10)
    print(f"🔑 No DASHBOARD_PASSWORD set. Temporary dashboard password: {DASH_PASSWORD}")

API_BASE = "https://discord.com/api/v10"
web_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
sessions = set()
_cache = {}


# ==========================================
# DISCORD REST API CLIENT
# ==========================================
async def discord_req(method: str, path: str, json_data=None, reason: str = None):
    headers = {"Authorization": f"Bot {TOKEN}"}
    if reason:
        headers["X-Audit-Log-Reason"] = quote(reason[:480])
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{API_BASE}{path}", headers=headers, json=json_data) as resp:
            if resp.status == 204:
                return resp.status, None
            try:
                data = await resp.json()
            except Exception:
                data = None
            return resp.status, data


async def fetch_cached(key: str, path: str, ttl: int = 20):
    now = time.time()
    if key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]
    status, data = await discord_req("GET", path)
    if status == 200 and data is not None:
        _cache[key] = (now, data)
        return data
    return _cache.get(key, (0, None))[1]


class SimpleObj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


async def get_all_guilds():
    raw = await fetch_cached("user_guilds", "/users/@me/guilds", ttl=30) or []
    res = []
    for g in raw:
        icon_url = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else None
        res.append(SimpleObj(id=int(g["id"]), name=g["name"], icon=icon_url))
    return res


async def get_guild_details(guild_id: int):
    g_data = await fetch_cached(f"guild_{guild_id}", f"/guilds/{guild_id}?with_counts=true", ttl=20)
    if not g_data:
        return None
    ch_data = await fetch_cached(f"channels_{guild_id}", f"/guilds/{guild_id}/channels", ttl=20) or []
    text_channels = [
        SimpleObj(id=int(c["id"]), name=c["name"], position=c.get("position", 0))
        for c in ch_data if c.get("type") in (0, 5)
    ]
    text_channels.sort(key=lambda c: c.position)

    roles = [
        SimpleObj(
            id=int(r["id"]),
            name=r["name"],
            managed=r.get("managed", False),
            position=r.get("position", 0),
        )
        for r in g_data.get("roles", [])
    ]
    roles.sort(key=lambda r: r.position)

    icon_url = f"https://cdn.discordapp.com/icons/{g_data['id']}/{g_data['icon']}.png" if g_data.get("icon") else None

    return SimpleObj(
        id=int(g_data["id"]),
        name=g_data["name"],
        icon=icon_url,
        owner_id=int(g_data.get("owner_id", 0)),
        member_count=g_data.get("approximate_member_count", "?"),
        channels=ch_data,
        text_channels=text_channels,
        roles=roles,
    )


async def send_mod_log(guild_id: int, g_conf: dict, title: str, description: str, color: int = 0xE74C3C):
    if not g_conf["modules"].get("logging", True) or not g_conf["log"].get("moderation") or not g_conf["modLogChannel"]:
        return
    embed = {
        "title": f"🛡️ {title}",
        "description": description[:4000],
        "color": color,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    await discord_req("POST", f"/channels/{g_conf['modLogChannel']}/messages", json_data={"embeds": [embed]})


# ==========================================
# PREMIUM UI & GLASSMORPHISM CSS
# ==========================================
def e(x):
    return html.escape(str(x), quote=True)


def authed(request: Request):
    return request.cookies.get("session") in sessions


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{
  --bg:#090d16;--side:#0f1624;--card:rgba(22,31,48,0.75);--input:#0b101b;
  --accent:#6366f1;--accent-glow:rgba(99,102,241,0.35);
  --ok:#10b981;--bad:#ef4444;--warn:#f59e0b;
  --text:#f8fafc;--muted:#94a3b8;--border:rgba(255,255,255,0.08)
}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',system-ui,sans-serif}
body{background:radial-gradient(circle at top right,#1e1b4b,var(--bg) 45%);color:var(--text);display:flex;min-height:100vh}
a{color:inherit;text-decoration:none}
.side{width:270px;background:var(--side);border-right:1px solid var(--border);display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow-y:auto;flex-shrink:0}
.brand{padding:22px 24px;font-size:19px;font-weight:800;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.brand span{background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:4px 9px;border-radius:99px;font-size:11px;font-weight:700;letter-spacing:.5px}
.switch-guild{padding:16px 18px 8px}
.nav{padding:8px 12px}
.nav a{display:flex;align-items:center;gap:10px;padding:10px 14px;color:var(--muted);font-weight:500;font-size:14px;border-radius:8px;margin-bottom:3px;transition:.15s}
.nav a:hover{color:#fff;background:rgba(255,255,255,0.04)}
.nav a.on{color:#fff;background:linear-gradient(90deg,rgba(99,102,241,0.22),rgba(99,102,241,0.05));border:1px solid rgba(99,102,241,0.35);font-weight:600}
.main{flex:1;padding:38px 48px;max-width:1180px}
.top-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;flex-wrap:wrap;gap:12px}
h1{font-size:28px;font-weight:800;letter-spacing:-0.5px}
.sub{color:var(--muted);font-size:14px;margin-top:4px;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;background:var(--ok);border-radius:50%;box-shadow:0 0 10px var(--ok);display:inline-block}
.card{background:var(--card);backdrop-filter:blur(12px);padding:24px;border-radius:14px;border:1px solid var(--border);margin-bottom:22px;box-shadow:0 8px 30px rgba(0,0,0,0.25)}
.card h3{margin-bottom:14px;font-size:16px;font-weight:700;color:#e2e8f0;display:flex;align-items:center;gap:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;position:relative;overflow:hidden}
.stat b{display:block;font-size:28px;font-weight:800;margin-top:4px;background:linear-gradient(90deg,#fff,#cbd5e1);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat span{color:var(--muted);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.6px}
label.f{display:block;color:var(--muted);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:14px}
select,input[type=text],input[type=password],input[type=number],input[type=color],textarea{width:100%;padding:11px 13px;background:var(--input);border:1px solid var(--border);border-radius:8px;color:#fff;margin-top:6px;font-size:14px;outline:none;transition:.15s}
input[type=color]{height:44px;padding:4px;cursor:pointer}
select:focus,input:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
textarea{min-height:95px;resize:vertical}
select[multiple]{min-height:170px}
.chk{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:8px;cursor:pointer;transition:.15s}
.chk:hover{background:rgba(255,255,255,0.03)}
.chk input{width:18px;height:18px;accent-color:var(--accent)}
button,.btn{background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;border:none;padding:11px 20px;border-radius:8px;font-weight:600;cursor:pointer;margin-top:16px;font-size:14px;display:inline-flex;align-items:center;gap:8px;box-shadow:0 4px 14px var(--accent-glow);transition:.15s}
button:hover,.btn:hover{transform:translateY(-1px);filter:brightness(1.08)}
button.danger{background:rgba(239,68,68,0.18);color:#fca5a5;border:1px solid rgba(239,68,68,0.35);box-shadow:none;margin-top:0;padding:6px 12px;font-size:13px}
button.danger:hover{background:var(--bad);color:#fff}
table{width:100%;border-collapse:collapse}
th,td{padding:12px 10px;text-align:left;border-bottom:1px solid var(--border);font-size:14px;vertical-align:middle}
th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.notice{padding:14px 18px;border-radius:10px;margin-bottom:22px;background:rgba(16,185,129,0.14);border:1px solid rgba(16,185,129,0.35);color:#6ee7b7;font-weight:600}
.notice.err{background:rgba(239,68,68,0.14);border-color:rgba(239,68,68,0.35);color:#fca5a5}
.muted{color:var(--muted);font-size:13px;line-height:1.5}
.servers{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px;margin-top:24px}
.server{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;text-align:center;transition:.2s}
.server:hover{border-color:var(--accent);transform:translateY(-3px);box-shadow:0 10px 25px rgba(99,102,241,0.2)}
.srv-icon{width:64px;height:64px;border-radius:50%;margin:0 auto 12px;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:800;overflow:hidden}
.srv-icon img{width:100%;height:100%;object-fit:cover}
.login{margin:auto;width:380px}
@media(max-width:850px){body{flex-direction:column}.side{width:100%;height:auto;position:static}.main{padding:24px}}
"""

PAGES = [
    ("overview", "📊", "Overview"),
    ("modules", "🧩", "Modules"),
    ("moderation", "🔨", "Moderation"),
    ("embed", "📢", "Embed Sender"),
    ("automod", "🤖", "AutoMod"),
    ("logging", "📜", "Logging"),
    ("welcome", "👋", "Welcome & Leave"),
    ("autorole", "🎭", "Auto-Role"),
    ("commands", "⌨️", "Commands"),
    ("custom", "💬", "Custom Tags"),
    ("warnings", "⚠️", "Warnings"),
    ("war", "⚔️", "War & Economy"),
]


def shell(title, body):
    return (
        f"<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)} — {BOT_NAME}</title><style>{CSS}</style></head><body>{body}</body></html>"
    )


def layout(guild, all_guilds, page, content, notice=None):
    nav = "".join(
        f"<a href='/manage/{guild.id}/{k}' class='{'on' if k == page else ''}'><span>{icon}</span> {e(name)}</a>"
        for k, icon, name in PAGES
    )
    options = "".join(
        f"<option value='{x.id}' {'selected' if x.id == guild.id else ''}>{e(x.name)}</option>" for x in all_guilds
    )
    note = ""
    if notice:
        note = f"<div class='notice {'err' if notice.startswith('❌') else ''}'>{e(notice)}</div>"
    label = next(n for k, _, n in PAGES if k == page)
    body = (
        f"<div class='side'><div class='brand'>⚡ WarMod<span>PRO</span></div>"
        f"<div class='switch-guild'><select onchange=\"location='/manage/'+this.value+'/{page}'\">{options}</select></div>"
        f"<div class='nav'>{nav}</div>"
        f"<div class='nav' style='margin-top:auto;border-top:1px solid var(--border);padding-top:12px'>"
        f"<a href='/'>🏠 All Servers</a><a href='/logout'>🚪 Log out</a></div></div>"
        f"<div class='main'><div class='top-bar'><div><h1>{e(label)}</h1>"
        f"<div class='sub'><span class='dot'></span> Connected to <b>{e(guild.name)}</b></div></div></div>"
        f"{note}{content}</div>"
    )
    return shell(label, body)


def chk(name, label, on):
    return f"<label class='chk'><input type='checkbox' name='{name}' {'checked' if on else ''}> <span>{label}</span></label>"


def channel_select(name, guild, selected):
    opts = "<option value=''>None (Disabled)</option>" + "".join(
        f"<option value='{c.id}' {'selected' if str(c.id) == str(selected) else ''}># {e(c.name)}</option>"
        for c in guild.text_channels
    )
    return f"<select name='{name}'>{opts}</select>"


def valid_channel(guild, value):
    value = (value or "").strip()
    if value.isdigit() and any(c.id == int(value) for c in guild.text_channels):
        return value
    return None


def action_form(guild, page, inner, button="💾 Save Changes"):
    return f"<form method='POST' action='/manage/{guild.id}/{page}'>{inner}<button type='submit'>{button}</button></form>"


# ==========================================
# PAGE RENDERERS
# ==========================================
def page_overview(guild, g, db):
    warns = sum(len(v) for v in db["warnings"].get(str(guild.id), {}).values())
    enabled_mods = sum(1 for k in MODULES if g["modules"].get(k, True))
    custom_cnt = len(g["customCommands"])

    stats = (
        f"<div class='stats'>"
        f"<div class='stat'><span>Total Members</span><b>{guild.member_count}</b></div>"
        f"<div class='stat'><span>Active Modules</span><b>{enabled_mods} / {len(MODULES)}</b></div>"
        f"<div class='stat'><span>Active Warnings</span><b>{warns}</b></div>"
        f"<div class='stat'><span>Custom Tags</span><b>{custom_cnt}</b></div></div>"
    )

    quick_links = (
        "<div class='grid'>"
        f"<div class='card'><h3>🛡️ Security Status</h3>"
        f"<p class='muted'>• Anti-Link: <b>{'Enabled' if g['automod']['antiLink'] else 'Disabled'}</b><br>"
        f"• Anti-Spam: <b>{'Enabled' if g['automod']['antiSpam'] else 'Disabled'}</b><br>"
        f"• Banned Words: <b>{len(g['automod']['bannedWords'])} words</b></p>"
        f"<a class='btn' href='/manage/{guild.id}/automod'>Configure AutoMod →</a></div>"
        f"<div class='card'><h3>📢 Quick Broadcast</h3>"
        f"<p class='muted'>Send rich custom embeds and announcements straight to any channel in <b>{e(guild.name)}</b>.</p>"
        f"<a class='btn' href='/manage/{guild.id}/embed'>Open Embed Builder →</a></div>"
        "</div>"
    )
    return stats + quick_links


def page_modules(guild, g, db):
    boxes = "".join(chk(f"mod_{k}", f"<b>{e(name)}</b>", g["modules"].get(k, True)) for k, name in MODULES.items())
    return "<div class='card'><h3>🧩 Feature Modules</h3><p class='muted' style='margin-bottom:10px'>Toggle whole modules on or off for this server.</p>" + action_form(guild, "modules", boxes) + "</div>"


def page_moderation(guild, g, db):
    inner = (
        "<div class='grid'><div>"
        "<label class='f'>Target User ID</label><input type='text' name='user_id' placeholder='e.g. 123456789012345678' required>"
        "<label class='f'>Moderation Action</label><select name='action'>"
        "<option value='warn'>⚠️ Warn Member</option><option value='timeout'>🔇 Timeout Member</option>"
        "<option value='untimeout'>🔊 Remove Timeout</option><option value='kick'>👢 Kick Member</option>"
        "<option value='softban'>🧹 Softban (Clear Messages)</option><option value='ban'>🔨 Ban Member</option>"
        "<option value='unban'>✅ Unban User</option></select></div><div>"
        "<label class='f'>Timeout Duration (Minutes)</label><input type='number' name='minutes' value='10' min='1' max='40320'>"
        "<label class='f'>Audit Log Reason</label><input type='text' name='reason' maxlength='300' placeholder='Rule violation'>"
        "</div></div>"
    )
    return "<div class='card'><h3>🔨 Remote Moderation</h3><p class='muted'>Execute instant Discord moderation actions from the web.</p>" + action_form(guild, "moderation", inner, "⚡ Execute Action") + "</div>"


def page_embed(guild, g, db):
    opts = "".join(f"<option value='{c.id}'># {e(c.name)}</option>" for c in guild.text_channels)
    inner = (
        "<div class='grid'><div>"
        f"<label class='f'>Target Channel</label><select name='channel' required>{opts}</select>"
        "<label class='f'>Embed Title</label><input type='text' name='title' maxlength='200' placeholder='Server Announcement' required>"
        "<label class='f'>Accent Color</label><input type='color' name='color' value='#6366f1'>"
        + chk("ping", "Mention @everyone", False) +
        "</div><div>"
        "<label class='f'>Embed Description / Body (Supports Markdown)</label>"
        "<textarea name='description' style='min-height:185px' maxlength='3500' placeholder='Write your announcement here...' required></textarea>"
        "</div></div>"
    )
    return "<div class='card'><h3>📢 Custom Embed Builder</h3><p class='muted'>Design and publish a custom embed directly to any text channel.</p>" + action_form(guild, "embed", inner, "🚀 Send Embed Now") + "</div>"


def page_automod(guild, g, db):
    a = g["automod"]
    inner = (
        chk("antiLink", "Block all external HTTP/HTTPS links", a["antiLink"])
        + chk("antiInvite", "Block Discord server invite links", a["antiInvite"])
        + chk("antiCaps", "Block excessive ALL-CAPS messages (>70% caps)", a["antiCaps"])
        + chk("antiSpam", "Auto-timeout message spammers (5 msgs in 5s → 5m mute)", a["antiSpam"])
        + chk("antiMassMention", "Block mass user/role mentions", a["antiMassMention"])
        + f"<label class='f'>Max Mentions Per Message</label><input type='number' name='maxMentions' min='2' max='50' value='{a['maxMentions']}'>"
        + f"<label class='f'>Banned Words List (One word per line)</label><textarea name='bannedWords'>{e(chr(10).join(a['bannedWords']))}</textarea>"
    )
    return "<div class='card'><h3>🤖 AutoMod Rules</h3>" + action_form(guild, "automod", inner) + "</div>"


def page_logging(guild, g, db):
    inner = (
        "<label class='f'>Audit & Mod Log Channel</label>" + channel_select("modLogChannel", guild, g["modLogChannel"])
        + "<div style='margin-top:12px'>"
        + chk("log_moderation", "Log moderation actions (Bans, Kicks, Warns, AutoMod)", g["log"]["moderation"])
        + chk("log_messages", "Log deleted and edited messages", g["log"]["messages"])
        + chk("log_members", "Log member joins and leaves", g["log"]["members"])
        + "</div>"
    )
    return "<div class='card'><h3>📜 Server Logging</h3>" + action_form(guild, "logging", inner) + "</div>"


def page_welcome(guild, g, db):
    w = g["welcome"]
    inner = (
        "<label class='f'>Welcome & Leave Channel</label>" + channel_select("channel", guild, w["channel"])
        + chk("joinEnabled", "Send welcome embed when a user joins", w["joinEnabled"])
        + f"<label class='f'>Welcome Message</label><textarea name='joinMessage' maxlength='500'>{e(w['joinMessage'])}</textarea>"
        + chk("leaveEnabled", "Send goodbye message when a user leaves", w["leaveEnabled"])
        + f"<label class='f'>Leave Message</label><textarea name='leaveMessage' maxlength='500'>{e(w['leaveMessage'])}</textarea>"
        + "<p class='muted' style='margin-top:8px'>Variables: <code>{user}</code>, <code>{username}</code>, <code>{server}</code>, <code>{count}</code></p>"
    )
    return "<div class='card'><h3>👋 Welcome & Leave Configuration</h3>" + action_form(guild, "welcome", inner) + "</div>"


def page_autorole(guild, g, db):
    roles = [r for r in guild.roles if not r.managed and r.id != guild.id]
    opts = "".join(
        f"<option value='{r.id}' {'selected' if str(r.id) in g['autoRoles'] else ''}>@ {e(r.name)}</option>"
        for r in reversed(roles)
    )
    inner = (
        f"<label class='f'>Select Auto-Roles (Hold Ctrl/Cmd to select multiple)</label><select name='autoRoles' multiple>{opts}</select>"
        "<p class='muted' style='margin-top:8px'>Make sure the bot's highest role is dragged above these roles in Discord Server Settings.</p>"
    )
    return "<div class='card'><h3>🎭 Automatic Join Roles</h3>" + action_form(guild, "autorole", inner) + "</div>"


def page_commands(guild, g, db):
    labels = {**MODULES, "core": "Core"}
    groups = defaultdict(list)
    for name, info in COMMANDS.items():
        groups[info["module"]].append((name, info["description"]))
    search = (
        "<div class='card'><input type='text' placeholder='🔍 Filter slash commands...' "
        "oninput=\"document.querySelectorAll('.cmd-row').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(this.value.toLowerCase())?'':'none')\"></div>"
    )
    cards = ""
    for mod, items in groups.items():
        rows = ""
        for name, desc in items:
            if mod == "core":
                toggle = "<span class='muted'>Locked On</span>"
            else:
                toggle = f"<input type='checkbox' name='cmd_{name}' {'checked' if name not in g['disabledCommands'] else ''}>"
            rows += f"<tr class='cmd-row'><td><b>/{e(name)}</b></td><td class='muted'>{e(desc)}</td><td>{toggle}</td></tr>"
        cards += f"<div class='card'><h3>{e(labels.get(mod, mod))}</h3><table><tr><th>Command</th><th>Description</th><th>Enabled</th></tr>{rows}</table></div>"
    return search + action_form(guild, "commands", cards, "💾 Save Command Toggles")


def page_custom(guild, g, db):
    rows = ""
    for name, resp in g["customCommands"].items():
        rows += (
            f"<tr><td><b>{e(g['prefix'])}{e(name)}</b></td><td class='muted'>{e(resp[:120])}</td><td>"
            f"<form method='POST' action='/manage/{guild.id}/custom' style='display:inline'>"
            f"<input type='hidden' name='action' value='delete'><input type='hidden' name='name' value='{e(name)}'>"
            f"<button class='danger' type='submit'>Delete</button></form></td></tr>"
        )
    table = f"<table><tr><th>Trigger</th><th>Response</th><th></th></tr>{rows}</table>" if rows else "<p class='muted'>No custom commands created yet.</p>"
    prefix_form = action_form(
        guild, "custom",
        f"<input type='hidden' name='action' value='prefix'><label class='f'>Text Prefix</label>"
        f"<input type='text' name='prefix' maxlength='3' value='{e(g['prefix'])}'>", "Update Prefix",
    )
    add_form = action_form(
        guild, "custom",
        "<input type='hidden' name='action' value='add'><label class='f'>Command Name</label><input type='text' name='name' maxlength='32' placeholder='rules' required>"
        "<label class='f'>Bot Response</label><textarea name='response' maxlength='1500' placeholder='Please follow server rules!' required></textarea>", "➕ Add Custom Tag",
    )
    return (
        f"<div class='grid'><div class='card'><h3>⚙️ Prefix Settings</h3>{prefix_form}</div>"
        f"<div class='card'><h3>✨ Create Custom Tag</h3>{add_form}</div></div>"
        f"<div class='card'><h3>💬 Active Custom Tags ({len(g['customCommands'])})</h3>{table}</div>"
    )


def page_warnings(guild, g, db):
    data = db["warnings"].get(str(guild.id), {})
    blocks = ""
    for uid, lst in data.items():
        if not lst:
            continue
        rows = "".join(
            f"<tr><td>#{w['id']}</td><td>{time.strftime('%Y-%m-%d', time.localtime(w['time']))}</td><td>{e(w['mod'])}</td><td>{e(w['reason'])}</td><td>"
            f"<form method='POST' action='/manage/{guild.id}/warnings' style='display:inline'>"
            f"<input type='hidden' name='action' value='delete'><input type='hidden' name='user_id' value='{e(uid)}'><input type='hidden' name='warn_id' value='{w['id']}'>"
            f"<button class='danger' type='submit'>Delete</button></form></td></tr>"
            for w in lst
        )
        clear = (
            f"<form method='POST' action='/manage/{guild.id}/warnings'><input type='hidden' name='action' value='clear'>"
            f"<input type='hidden' name='user_id' value='{e(uid)}'><button class='danger' type='submit'>Clear All Warnings</button></form>"
        )
        blocks += (
            f"<div class='card'><h3>👤 User ID: {e(uid)} — {len(lst)} warning(s)</h3>"
            f"<table><tr><th>ID</th><th>Date</th><th>Moderator</th><th>Reason</th><th></th></tr>{rows}</table><div style='margin-top:12px'>{clear}</div></div>"
        )
    return blocks or "<div class='card'><p class='muted'>🎉 Clean slate! No warnings recorded in this server.</p></div>"


def page_war(guild, g, db):
    top = sorted(db["users"].values(), key=lambda x: x["elo"], reverse=True)[:25]
    rows = "".join(
        f"<tr><td><b>#{i + 1}</b></td><td><b>{e(u['username'])}</b> <span class='muted'>({u['id']})</span></td>"
        f"<td><code>{u['elo']} ELO</code></td><td>{u['wins']}W / {u['losses']}L</td><td>💰 {u['coins']}</td></tr>"
        for i, u in enumerate(top)
    ) or "<tr><td colspan='5' class='muted'>No battles recorded yet.</td></tr>"
    return f"<div class='card'><h3>🏆 Global War & Economy Leaderboard</h3><table><tr><th>Rank</th><th>Player</th><th>Rating</th><th>Record</th><th>Coins</th></tr>{rows}</table></div>"


RENDER = {
    "overview": page_overview, "modules": page_modules, "moderation": page_moderation,
    "embed": page_embed, "automod": page_automod, "logging": page_logging,
    "welcome": page_welcome, "autorole": page_autorole, "commands": page_commands,
    "custom": page_custom, "warnings": page_warnings, "war": page_war,
}


# ==========================================
# SAVE HANDLERS
# ==========================================
async def save_modules(guild, g, db, form):
    for k in MODULES:
        g["modules"][k] = f"mod_{k}" in form
    return "✅ Module settings saved."


async def save_embed(guild, g, db, form):
    ch_id = valid_channel(guild, form.get("channel"))
    if not ch_id:
        return "❌ Invalid text channel selected."
    title = (form.get("title") or "").strip()[:200]
    desc = (form.get("description") or "").strip()[:3500]
    color_hex = (form.get("color") or "#6366f1").lstrip("#")
    try:
        color = int(color_hex, 16)
    except ValueError:
        color = 0x6366F1
    ping = "ping" in form
    payload = {
        "content": "@everyone" if ping else None,
        "embeds": [{
            "title": title,
            "description": desc,
            "color": color,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "footer": {"text": f"Sent via {BOT_NAME} Dashboard"},
        }],
    }
    status, _ = await discord_req("POST", f"/channels/{ch_id}/messages", json_data=payload)
    if status in (200, 201):
        return "✅ Custom embed published to Discord!"
    return f"❌ Failed to send embed (HTTP {status}). Check bot permissions."


async def save_moderation(guild, g, db, form):
    raw = (form.get("user_id") or "").strip()
    if not raw.isdigit():
        return "❌ Enter a valid numeric User ID."
    uid = int(raw)
    if uid == guild.owner_id:
        return "❌ You cannot moderate the server owner."

    action = form.get("action")
    reason = (form.get("reason") or "").strip()[:300] or "No reason provided"
    audit_reason = f"{reason} (by Dashboard)"

    if action == "warn":
        g["warnCounter"] += 1
        entry = {"id": g["warnCounter"], "mod": "Dashboard", "reason": reason, "time": now_ts()}
        lst = db["warnings"].setdefault(str(guild.id), {}).setdefault(str(uid), [])
        lst.append(entry)
        await send_mod_log(guild.id, g, "Member Warned", f"**Target:** <@{uid}>\n**By:** Dashboard\n**Total Warnings:** {len(lst)}\n**Reason:** {reason}", 0xF1C40F)
        return f"⚠️ User {uid} warned. (Total warnings: {len(lst)})"

    if action == "kick":
        status, _ = await discord_req("DELETE", f"/guilds/{guild.id}/members/{uid}", reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Kicked", f"**Target:** <@{uid}>\n**By:** Dashboard\n**Reason:** {reason}")
            return f"✅ Kicked user {uid}."
        return f"❌ Kick failed (HTTP {status})."

    if action == "ban":
        status, _ = await discord_req("PUT", f"/guilds/{guild.id}/bans/{uid}", json_data={"delete_message_seconds": 0}, reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Banned", f"**Target:** <@{uid}>\n**By:** Dashboard\n**Reason:** {reason}")
            return f"🔨 Banned user {uid}."
        return f"❌ Ban failed (HTTP {status})."

    if action == "unban":
        status, _ = await discord_req("DELETE", f"/guilds/{guild.id}/bans/{uid}", reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Unbanned", f"**Target:** <@{uid}>\n**By:** Dashboard\n**Reason:** {reason}", 0x2ECC71)
            return f"✅ Unbanned user {uid}."
        return f"❌ Unban failed (HTTP {status})."

    if action == "softban":
        s1, _ = await discord_req("PUT", f"/guilds/{guild.id}/bans/{uid}", json_data={"delete_message_seconds": 86400}, reason=audit_reason)
        if s1 in (200, 204):
            await discord_req("DELETE", f"/guilds/{guild.id}/bans/{uid}", reason="Softban complete")
            await send_mod_log(guild.id, g, "Member Softbanned", f"**Target:** <@{uid}>\n**By:** Dashboard\n**Reason:** {reason}", 0xE67E22)
            return f"🧹 Softbanned user {uid}."
        return f"❌ Softban failed (HTTP {s1})."

    if action == "timeout":
        minutes = max(1, min(40320, int(form.get("minutes") or 10)))
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)).isoformat()
        status, _ = await discord_req("PATCH", f"/guilds/{guild.id}/members/{uid}", json_data={"communication_disabled_until": until}, reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Timed Out", f"**Target:** <@{uid}>\n**Duration:** {minutes}m\n**By:** Dashboard\n**Reason:** {reason}", 0xF39C12)
            return f"🔇 Timed out user {uid} for {minutes} minutes."
        return f"❌ Timeout failed (HTTP {status})."

    if action == "untimeout":
        status, _ = await discord_req("PATCH", f"/guilds/{guild.id}/members/{uid}", json_data={"communication_disabled_until": None}, reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Timeout Removed", f"**Target:** <@{uid}>\n**By:** Dashboard", 0x2ECC71)
            return f"🔊 Removed timeout from user {uid}."
        return f"❌ Failed to remove timeout (HTTP {status})."

    return "❌ Unknown action."


async def save_automod(guild, g, db, form):
    a = g["automod"]
    for k in ("antiLink", "antiInvite", "antiCaps", "antiSpam", "antiMassMention"):
        a[k] = k in form
    try:
        a["maxMentions"] = max(2, min(50, int(form.get("maxMentions") or 5)))
    except ValueError:
        pass
    words = []
    for line in (form.get("bannedWords") or "").splitlines():
        w = line.strip().lower()[:50]
        if w and w not in words:
            words.append(w)
    a["bannedWords"] = words[:200]
    return "✅ AutoMod configuration saved."


async def save_logging(guild, g, db, form):
    g["modLogChannel"] = valid_channel(guild, form.get("modLogChannel"))
    for k in ("moderation", "messages", "members"):
        g["log"][k] = f"log_{k}" in form
    return "✅ Logging settings updated."


async def save_welcome(guild, g, db, form):
    w = g["welcome"]
    w["channel"] = valid_channel(guild, form.get("channel"))
    w["joinEnabled"] = "joinEnabled" in form
    w["leaveEnabled"] = "leaveEnabled" in form
    w["joinMessage"] = (form.get("joinMessage") or "")[:500]
    w["leaveMessage"] = (form.get("leaveMessage") or "")[:500]
    return "✅ Welcome & leave settings updated."


async def save_autorole(guild, g, db, form):
    role_ids = {r.id for r in guild.roles}
    chosen = [v for v in form.getlist("autoRoles") if v.isdigit() and int(v) in role_ids and int(v) != guild.id]
    g["autoRoles"] = chosen[:10]
    return "✅ Auto-roles updated."


async def save_commands(guild, g, db, form):
    g["disabledCommands"] = [n for n, i in COMMANDS.items() if i["module"] != "core" and f"cmd_{n}" not in form]
    return "✅ Command permissions updated."


async def save_custom(guild, g, db, form):
    action = form.get("action")
    if action == "prefix":
        p = (form.get("prefix") or "").strip()[:3]
        if not p or any(c.isspace() for c in p):
            return "❌ Invalid prefix."
        g["prefix"] = p
        return "✅ Prefix saved."
    name = (form.get("name") or "").lower().strip()
    if action == "delete":
        g["customCommands"].pop(name, None)
        return "✅ Custom tag deleted."
    if action == "add":
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
            return "❌ Name must be 1-32 chars: letters, numbers, - or _."
        if len(g["customCommands"]) >= 100 and name not in g["customCommands"]:
            return "❌ Custom command limit reached."
        g["customCommands"][name] = (form.get("response") or "")[:1500]
        return "✅ Custom tag saved."
    return "❌ Unknown action."


async def save_warnings(guild, g, db, form):
    uid = (form.get("user_id") or "").strip()
    data = db["warnings"].get(str(guild.id), {})
    if form.get("action") == "clear":
        data.pop(uid, None)
        return "✅ Warnings cleared."
    if form.get("action") == "delete":
        try:
            wid = int(form.get("warn_id"))
        except (TypeError, ValueError):
            return "❌ Invalid warning."
        data[uid] = [w for w in data.get(uid, []) if w["id"] != wid]
        return "✅ Warning deleted."
    return "❌ Unknown action."


SAVE = {
    "modules": save_modules, "moderation": save_moderation, "embed": save_embed,
    "automod": save_automod, "logging": save_logging, "welcome": save_welcome,
    "autorole": save_autorole, "commands": save_commands, "custom": save_custom,
    "warnings": save_warnings,
}


# ==========================================
# ROUTES
# ==========================================
def login_page(error=""):
    err = f"<div class='notice err'>{e(error)}</div>" if error else ""
    return shell(
        "Login",
        f"<div class='login' style='margin-top:18vh'><div class='card'><h3>⚡ {BOT_NAME}</h3>"
        f"<p class='muted' style='margin-bottom:14px'>Enter your administrator password to access the control panel.</p>{err}"
        "<form method='POST' action='/login'><label class='f'>Dashboard Password</label>"
        "<input type='password' name='password' autofocus required><button type='submit' style='width:100%;justify-content:center'>🔓 Sign In</button></form></div></div>",
    )


@web_app.get("/login", response_class=HTMLResponse)
async def login_get():
    return login_page()


@web_app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    pw = str(form.get("password") or "")
    if hmac.compare_digest(pw.encode(), DASH_PASSWORD.encode()):
        token = secrets.token_urlsafe(32)
        sessions.add(token)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("session", token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 7)
        return resp
    await asyncio.sleep(1)
    return HTMLResponse(login_page("Invalid password."), status_code=401)


@web_app.get("/logout")
async def logout(request: Request):
    sessions.discard(request.cookies.get("session"))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


@web_app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    guilds = await get_all_guilds()
    if not guilds:
        body = "<div class='main'><h1>No servers connected</h1><p class='sub'>Invite the bot to your Discord server, then refresh this page.</p></div>"
        return shell("Select Server", body)
    cards = ""
    for gd in guilds:
        icon_html = f"<img src='{gd.icon}' alt='icon'>" if gd.icon else gd.name[:2].upper()
        cards += (
            f"<a class='server' href='/manage/{gd.id}/overview'>"
            f"<div class='srv-icon'>{icon_html}</div>"
            f"<h3>{e(gd.name)}</h3><p class='muted'>Manage Server →</p></a>"
        )
    return shell("Select Server", f"<div class='main'><h1>Select a Server</h1><p class='sub'>Choose a Discord server to configure</p><div class='servers'>{cards}</div></div>")


@web_app.get("/manage/{guild_id}", response_class=HTMLResponse)
async def manage_root(request: Request, guild_id: int):
    return RedirectResponse(f"/manage/{guild_id}/overview", status_code=303)


@web_app.get("/manage/{guild_id}/{page}", response_class=HTMLResponse)
async def manage_page(request: Request, guild_id: int, page: str):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    if page not in RENDER:
        return RedirectResponse("/", status_code=303)
    guild, all_guilds = await asyncio.gather(get_guild_details(guild_id), get_all_guilds())
    if not guild:
        return RedirectResponse("/", status_code=303)
    db = load_db()
    g = get_guild(db, guild_id)
    return layout(guild, all_guilds, page, RENDER[page](guild, g, db), request.query_params.get("msg"))


@web_app.post("/manage/{guild_id}/{page}")
async def manage_save(request: Request, guild_id: int, page: str, background_tasks: BackgroundTasks):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    handler = SAVE.get(page)
    guild = await get_guild_details(guild_id)
    if not guild or not handler:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    db = load_db()
    g = get_guild(db, guild_id)
    msg = await handler(guild, g, db, form)
    
    # 🚀 THE FIX: Save to MongoDB in the background so the page reloads instantly!
    background_tasks.add_task(save_db, db)
    
    return RedirectResponse(f"/manage/{guild_id}/{page}?msg={quote(msg)}", status_code=303)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Missing TOKEN in your .env file.")
    print(f"🌐 {BOT_NAME} Dashboard live at http://localhost:{PORT}")
    uvicorn.run(web_app, host="0.0.0.0", port=PORT, log_level="info")
