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
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
import uvicorn

from shared import (
    MODULES, COMMANDS,
    load_db, save_db, get_guild, format_welcome_text, now_ts
)

load_dotenv()
TOKEN = os.getenv("TOKEN")
PORT = int(os.getenv("PORT", 8000))
DASH_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
if not DASH_PASSWORD:
    DASH_PASSWORD = secrets.token_urlsafe(10)
    print(f"🔑 Temporary dashboard password: {DASH_PASSWORD}")

API_BASE = "https://discord.com/api/v10"
web_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
sessions = set()
_cache = {}
_http_session: aiohttp.ClientSession = None


# ==========================================
# PERSISTENT HIGH-SPEED DISCORD REST CLIENT
# ==========================================
async def get_http() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _http_session


@web_app.on_event("shutdown")
async def shutdown_event():
    if _http_session and not _http_session.closed:
        await _http_session.close()


async def discord_req(method: str, path: str, json_data=None, reason: str = None):
    headers = {"Authorization": f"Bot {TOKEN}"}
    if reason:
        headers["X-Audit-Log-Reason"] = quote(reason[:480])
    session = await get_http()
    try:
        async with session.request(method, f"{API_BASE}{path}", headers=headers, json=json_data) as resp:
            if resp.status == 204:
                return resp.status, None
            try:
                data = await resp.json()
            except Exception:
                data = None
            return resp.status, data
    except Exception:
        return 500, None


async def fetch_cached(key: str, path: str, ttl: int = 30):
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
    raw = await fetch_cached("user_guilds", "/users/@me/guilds", ttl=45) or []
    res = []
    for g in raw:
        icon_url = f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else None
        res.append(SimpleObj(id=int(g["id"]), name=g["name"], icon=icon_url))
    return res


async def get_guild_details(guild_id: int):
    g_data, ch_data, mem_data = await asyncio.gather(
        fetch_cached(f"guild_{guild_id}", f"/guilds/{guild_id}?with_counts=true", ttl=30),
        fetch_cached(f"channels_{guild_id}", f"/guilds/{guild_id}/channels", ttl=30),
        fetch_cached(f"members_{guild_id}", f"/guilds/{guild_id}/members?limit=100", ttl=45),
    )
    if not g_data:
        return None
    ch_data = ch_data or []
    mem_data = mem_data or []

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
            color=f"#{r.get('color', 0):06x}" if r.get("color") else "#94a3b8",
        )
        for r in g_data.get("roles", [])
    ]
    roles.sort(key=lambda r: r.position)

    members = []
    for m in mem_data:
        u = m.get("user", {})
        if not u.get("bot"):
            nick = m.get("nick") or u.get("global_name") or u.get("username", "Unknown")
            members.append(SimpleObj(id=int(u["id"]), name=nick, username=u.get("username", "")))
    members.sort(key=lambda x: x.name.lower())

    icon_url = f"https://cdn.discordapp.com/icons/{g_data['id']}/{g_data['icon']}.png" if g_data.get("icon") else None

    return SimpleObj(
        id=int(g_data["id"]),
        name=g_data["name"],
        icon=icon_url,
        owner_id=int(g_data.get("owner_id", 0)),
        member_count=g_data.get("approximate_member_count", len(members)),
        online_count=g_data.get("approximate_presence_count", 0),
        channels=ch_data,
        text_channels=text_channels,
        roles=roles,
        members=members,
    )


async def send_mod_log(guild_id: int, g_conf: dict, title: str, description: str, color: int = 0x2B2D31):
    if not g_conf["modules"].get("logging", True) or not g_conf["log"].get("moderation") or not g_conf["modLogChannel"]:
        return
    embed = {
        "title": f"🛡️ {title}",
        "description": description[:4000],
        "color": color,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "footer": {"text": "BBC"},
    }
    await discord_req("POST", f"/channels/{g_conf['modLogChannel']}/messages", json_data={"embeds": [embed]})


# ==========================================
# ULTRA-PREMIUM DESIGN SYSTEM (LINEAR / VERCEL UI)
# ==========================================
def e(x):
    return html.escape(str(x), quote=True)


def authed(request: Request):
    return request.cookies.get("session") in sessions


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root {
  --bg: #050508;
  --surface: #0c0d12;
  --card: #11131a;
  --card-hover: #161922;
  --input: #08090d;
  --border: rgba(255,255,255,0.07);
  --border-focus: rgba(99,102,241,0.55);
  --accent: #6366f1;
  --accent-2: #8b5cf6;
  --accent-glow: rgba(99,102,241,0.25);
  --ok: #10b981;
  --warn: #f59e0b;
  --err: #ef4444;
  --text: #f8fafc;
  --muted: #94a3b8;
  --subtle: #64748b;
}
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', system-ui, sans-serif; }
code, pre, .mono { font-family: 'JetBrains Mono', monospace; }
body {
  background-color: var(--bg);
  background-image: 
    radial-gradient(at 0% 0%, rgba(99,102,241,0.10) 0px, transparent 45%),
    radial-gradient(at 100% 0%, rgba(139,92,246,0.07) 0px, transparent 45%);
  color: var(--text);
  display: flex;
  min-height: 100vh;
  overflow-x: hidden;
}
a { color: inherit; text-decoration: none; }

/* SIDEBAR */
.side {
  width: 268px;
  background: rgba(12,13,18,0.88);
  backdrop-filter: blur(20px);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  flex-shrink: 0;
  z-index: 40;
}
.brand {
  padding: 20px 22px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
}
.brand-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 18px;
  font-weight: 800;
  letter-spacing: -0.3px;
}
.brand-logo .orb {
  width: 30px; height: 30px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 15px var(--accent-glow);
  font-size: 14px; font-weight: 800;
}
.badge {
  background: rgba(99,102,241,0.14);
  color: #a5b4fc;
  border: 1px solid rgba(99,102,241,0.3);
  padding: 3px 8px;
  border-radius: 99px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.6px;
}
.guild-switcher { padding: 14px 16px 8px; }
.guild-switcher select {
  background: var(--card);
  border: 1px solid var(--border);
  font-weight: 600;
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
}
.nav-section {
  padding: 10px 20px 4px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--subtle);
}
.nav { padding: 4px 12px; flex: 1; overflow-y: auto; }
.nav a {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 9px 12px;
  color: var(--muted);
  font-weight: 500;
  font-size: 13.5px;
  border-radius: 9px;
  margin-bottom: 2px;
  transition: all 0.15s ease;
  border: 1px solid transparent;
}
.nav a i { width: 17px; height: 17px; stroke-width: 2; opacity: 0.8; }
.nav a:hover { color: #fff; background: rgba(255,255,255,0.04); }
.nav a.on {
  color: #fff;
  background: linear-gradient(90deg, rgba(99,102,241,0.18), rgba(99,102,241,0.03));
  border-color: rgba(99,102,241,0.35);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--accent);
}
.nav a.on i { color: #818cf8; opacity: 1; }
.side-foot {
  padding: 12px;
  border-top: 1px solid var(--border);
  display: flex;
  gap: 8px;
}
.side-foot a {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 9px;
  border-radius: 8px;
  background: var(--card);
  border: 1px solid var(--border);
  font-size: 12.5px;
  font-weight: 600;
  color: var(--muted);
  transition: 0.15s;
}
.side-foot a:hover { color: #fff; border-color: rgba(255,255,255,0.2); }

/* MAIN AREA */
.main {
  flex: 1;
  padding: 32px 44px 60px;
  max-width: 1260px;
  margin: 0 auto;
}
.top-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 28px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  gap: 16px;
}
.guild-pill {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--card);
  border: 1px solid var(--border);
  padding: 8px 14px;
  border-radius: 12px;
}
.guild-pill img, .guild-pill .fallback {
  width: 34px; height: 34px;
  border-radius: 50%;
  object-fit: cover;
  background: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 13px;
}
.pulse {
  width: 8px; height: 8px;
  background: var(--ok);
  border-radius: 50%;
  box-shadow: 0 0 10px var(--ok);
  display: inline-block;
}
h1 { font-size: 24px; font-weight: 800; letter-spacing: -0.5px; }
.sub { color: var(--muted); font-size: 13.5px; margin-top: 4px; }

/* CARDS & GRIDS */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.stat-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px;
  position: relative;
  overflow: hidden;
  transition: 0.2s;
}
.stat-card:hover { border-color: rgba(255,255,255,0.15); transform: translateY(-2px); }
.stat-top { display: flex; justify-content: space-between; align-items: center; color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; }
.stat-icon { width: 34px; height: 34px; border-radius: 9px; background: rgba(99,102,241,0.12); color: #818cf8; display: flex; align-items: center; justify-content: center; }
.stat-val { font-size: 28px; font-weight: 800; margin-top: 10px; letter-spacing: -0.8px; }
.stat-sub { font-size: 12px; color: var(--subtle); margin-top: 4px; }

.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 20px; }
.grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 24px;
  margin-bottom: 20px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.35);
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.card-title { font-size: 15.5px; font-weight: 700; display: flex; align-items: center; gap: 9px; }
.card-title i { width: 18px; height: 18px; color: #818cf8; }

/* FORMS & INPUTS */
label.f {
  display: block;
  color: #cbd5e1;
  font-size: 12px;
  font-weight: 600;
  margin-top: 14px;
  margin-bottom: 6px;
}
select, input[type=text], input[type=password], input[type=number], input[type=color], textarea {
  width: 100%;
  padding: 10px 13px;
  background: var(--input);
  border: 1px solid var(--border);
  border-radius: 9px;
  color: #fff;
  font-size: 13.5px;
  outline: none;
  transition: 0.15s;
}
input[type=color] { height: 42px; padding: 4px; cursor: pointer; }
select:focus, input:focus, textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
}
textarea { min-height: 92px; resize: vertical; line-height: 1.5; }

/* MODERN iOS SWITCHES */
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  background: rgba(255,255,255,0.015);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: 0.15s;
}
.switch-row:hover { background: rgba(255,255,255,0.035); border-color: rgba(255,255,255,0.12); }
.switch-info b { display: block; font-size: 13.5px; font-weight: 600; color: #f1f5f9; }
.switch-info span { font-size: 12px; color: var(--muted); }
.switch { position: relative; display: inline-block; width: 42px; height: 24px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute; cursor: pointer; inset: 0;
  background-color: #262936;
  transition: .2s;
  border-radius: 99px;
  border: 1px solid rgba(255,255,255,0.08);
}
.slider:before {
  position: absolute; content: "";
  height: 18px; width: 18px;
  left: 2px; bottom: 2px;
  background-color: #fff;
  transition: .2s;
  border-radius: 50%;
}
.switch input:checked + .slider {
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  border-color: transparent;
}
.switch input:checked + .slider:before { transform: translateX(18px); }

/* CHIP CHECKBOXES FOR ROLES */
.chip-grid { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.chip-item input { display: none; }
.chip-box {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 13px;
  background: var(--input);
  border: 1px solid var(--border);
  border-radius: 99px;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: 0.15s;
  user-select: none;
}
.chip-dot { width: 8px; height: 8px; border-radius: 50%; }
.chip-item input:checked + .chip-box {
  background: rgba(99,102,241,0.18);
  border-color: var(--accent);
  color: #fff;
  box-shadow: 0 0 12px var(--accent-glow);
}

/* BUTTONS */
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }
button, .btn {
  background: linear-gradient(135deg, var(--accent), #4f46e5);
  color: #fff;
  border: 1px solid rgba(255,255,255,0.1);
  padding: 10px 18px;
  border-radius: 9px;
  font-weight: 600;
  font-size: 13.5px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transition: all 0.15s ease;
  box-shadow: 0 4px 14px var(--accent-glow);
}
button i, .btn i { width: 16px; height: 16px; }
button:hover, .btn:hover { transform: translateY(-1px); filter: brightness(1.1); }
button:disabled { opacity: 0.6; cursor: wait; transform: none; }
button.sec, .btn.sec {
  background: var(--surface);
  border: 1px solid var(--border);
  color: #e2e8f0;
  box-shadow: none;
}
button.sec:hover, .btn.sec:hover { background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.18); }
button.ok { background: linear-gradient(135deg, #10b981, #059669); box-shadow: 0 4px 14px rgba(16,185,129,0.25); }
button.warn-btn { background: linear-gradient(135deg, #f59e0b, #d97706); box-shadow: 0 4px 14px rgba(245,158,11,0.25); }
button.danger {
  background: rgba(239,68,68,0.12);
  color: #fca5a5;
  border: 1px solid rgba(239,68,68,0.3);
  box-shadow: none;
  padding: 7px 12px;
  font-size: 12.5px;
}
button.danger:hover { background: var(--err); color: #fff; }

/* DISCORD LIVE PREVIEW */
.dc-preview {
  background: #313338;
  border-radius: 10px;
  padding: 16px;
  border: 1px solid rgba(255,255,255,0.06);
  font-size: 14px;
}
.dc-msg-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.dc-bot-av {
  width: 36px; height: 36px; border-radius: 50%;
  background: var(--accent); display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 13px;
}
.dc-bot-tag { background: #5865f2; color: #fff; font-size: 10px; font-weight: 700; padding: 2px 5px; border-radius: 4px; margin-left: 5px; }
.dc-embed {
  background: #2b2d31;
  border-left: 4px solid var(--accent);
  border-radius: 4px;
  padding: 12px 16px;
  max-width: 520px;
  margin-left: 46px;
}
.dc-embed-title { font-weight: 700; color: #fff; font-size: 15px; margin-bottom: 6px; }
.dc-embed-desc { color: #dbdee1; font-size: 13.5px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
.dc-embed-foot { color: #949ba4; font-size: 11px; margin-top: 10px; font-weight: 600; }

/* TABLES & PILLS */
table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px 10px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13.5px; }
th { color: var(--subtle); font-size: 11px; text-transform: uppercase; letter-spacing: 0.7px; font-weight: 700; }
.var-pill {
  display: inline-block;
  background: rgba(99,102,241,0.12);
  border: 1px solid rgba(99,102,241,0.3);
  color: #a5b4fc;
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  cursor: pointer;
  margin-right: 6px;
  margin-top: 6px;
  transition: 0.15s;
}
.var-pill:hover { background: var(--accent); color: #fff; }

/* TOAST NOTIFICATION */
#toast-box {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.toast {
  background: rgba(17,19,26,0.95);
  backdrop-filter: blur(16px);
  border: 1px solid rgba(16,185,129,0.45);
  color: #ecfdf5;
  padding: 13px 18px;
  border-radius: 11px;
  font-size: 13.5px;
  font-weight: 600;
  box-shadow: 0 12px 32px rgba(0,0,0,0.5);
  display: flex;
  align-items: center;
  gap: 10px;
  animation: slideIn 0.2s ease-out;
}
.toast.err { border-color: rgba(239,68,68,0.5); color: #fef2f2; }
@keyframes slideIn { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

/* SERVER SELECTOR CARDS */
.servers-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 18px; margin-top: 24px; }
.srv-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 26px 22px;
  text-align: center;
  transition: all 0.2s ease;
  position: relative;
  overflow: hidden;
}
.srv-card:hover {
  border-color: var(--accent);
  transform: translateY(-4px);
  box-shadow: 0 12px 30px rgba(99,102,241,0.18);
}
.srv-av {
  width: 68px; height: 68px; border-radius: 50%;
  margin: 0 auto 14px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  display: flex; align-items: center; justify-content: center;
  font-size: 24px; font-weight: 800; overflow: hidden;
  box-shadow: 0 6px 20px rgba(0,0,0,0.4);
}
.srv-av img { width: 100%; height: 100%; object-fit: cover; }

@media(max-width: 900px) {
  body { flex-direction: column; }
  .side { width: 100%; height: auto; position: static; }
  .main { padding: 22px; }
  .grid { grid-template-columns: 1fr; }
}
"""

JS_CORE = """
function showToast(msg, isErr) {
  const box = document.getElementById('toast-box');
  const el = document.createElement('div');
  el.className = 'toast' + (isErr || msg.startsWith('❌') ? ' err' : '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, 3400);
}

async function submitAjax(form, submitter) {
  const fd = new FormData(form);
  if (submitter && submitter.name) {
    fd.append(submitter.name, submitter.value);
  }
  const btn = submitter || form.querySelector('button[type="submit"]');
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Processing...'; }
  try {
    const resp = await fetch(form.action, {
      method: 'POST',
      body: fd,
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    });
    const data = await resp.json();
    showToast(data.msg || 'Saved!', !data.ok);
    if (data.reload) {
      setTimeout(() => location.reload(), 350);
    }
  } catch (err) {
    showToast('❌ Network error while saving.', true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml; }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  if (window.lucide) lucide.createIcons();

  const params = new URLSearchParams(location.search);
  if (params.get('msg')) {
    showToast(params.get('msg'), params.get('msg').startsWith('❌'));
    history.replaceState({}, '', location.pathname);
  }

  document.querySelectorAll('form.ajax').forEach(form => {
    form.addEventListener('submit', e => {
      e.preventDefault();
      submitAjax(form, e.submitter);
    });
  });
});

function insertVar(id, text) {
  const el = document.getElementById(id);
  if (!el) return;
  const start = el.selectionStart || el.value.length;
  el.value = el.value.slice(0, start) + text + el.value.slice(el.selectionEnd || start);
  el.dispatchEvent(new Event('input'));
  el.focus();
}

function toggleAllCmds(state) {
  document.querySelectorAll('.cmd-toggle').forEach(cb => cb.checked = state);
}
"""

PAGES = [
    ("overview", "layout-dashboard", "Overview", "Core"),
    ("modules", "layers", "Modules", "Core"),
    ("commands", "terminal", "Commands", "Core"),
    ("moderation", "shield-alert", "Moderation", "Security"),
    ("automod", "bot", "AutoMod & Filter", "Security"),
    ("warnings", "alert-triangle", "Active Warnings", "Security"),
    ("logging", "scroll-text", "Audit Logging", "Security"),
    ("embed", "megaphone", "Broadcast Studio", "Engagement"),
    ("polls", "bar-chart-3", "Polls & Giveaways", "Engagement"),
    ("welcome", "hand", "Welcome & Leave", "Engagement"),
    ("autorole", "user-check", "Auto-Role", "Engagement"),
    ("custom", "message-square-code", "Custom Tags", "Engagement"),
]


def shell(title, body):
    return (
        f"<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)} — BBC Console</title>"
        f"<script src='https://unpkg.com/lucide@latest'></script>"
        f"<style>{CSS}</style></head><body>"
        f"<div id='toast-box'></div>{body}<script>{JS_CORE}</script></body></html>"
    )


def layout(guild, all_guilds, page, content, notice=None):
    sections = defaultdict(list)
    for k, icon, name, sec in PAGES:
        sections[sec].append((k, icon, name))

    nav_html = ""
    for sec, items in sections.items():
        nav_html += f"<div class='nav-section'>{e(sec)}</div>"
        for k, icon, name in items:
            nav_html += (
                f"<a href='/manage/{guild.id}/{k}' class='{'on' if k == page else ''}'>"
                f"<i data-lucide='{icon}'></i> <span>{e(name)}</span></a>"
            )

    options = "".join(
        f"<option value='{x.id}' {'selected' if x.id == guild.id else ''}>{e(x.name)}</option>"
        for x in all_guilds
    )
    label = next(n for k, _, n, _ in PAGES if k == page)
    av_html = f"<img src='{guild.icon}' alt=''>" if guild.icon else f"<div class='fallback'>{e(guild.name[:2].upper())}</div>"

    body = (
        f"<aside class='side'>"
        f"<div class='brand'><div class='brand-logo'><div class='orb'>B</div> BBC</div><span class='badge'>CONSOLE</span></div>"
        f"<div class='guild-switcher'><select onchange=\"location='/manage/'+this.value+'/{page}'\">{options}</select></div>"
        f"<nav class='nav'>{nav_html}</nav>"
        f"<div class='side-foot'>"
        f"<a href='/'><i data-lucide='grid' style='width:14px;height:14px'></i> Servers</a>"
        f"<a href='/logout'><i data-lucide='log-out' style='width:14px;height:14px'></i> Sign Out</a>"
        f"</div></aside>"
        f"<main class='main'>"
        f"<div class='top-header'>"
        f"<div><h1>{e(label)}</h1><div class='sub'>Instant cloud synchronization with your Discord server</div></div>"
        f"<div class='guild-pill'>{av_html}<div><div style='font-weight:700;font-size:13.5px'>{e(guild.name)}</div>"
        f"<div style='font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:6px'><span class='pulse'></span> {guild.member_count} Members</div></div></div>"
        f"</div>"
        f"{content}</main>"
    )
    return shell(label, body)


def switch(name, title, subtitle, checked):
    return (
        f"<label class='switch-row'>"
        f"<div class='switch-info'><b>{title}</b><span>{subtitle}</span></div>"
        f"<div class='switch'><input type='checkbox' name='{name}' {'checked' if checked else ''}><span class='slider'></span></div>"
        f"</label>"
    )


def channel_select(name, guild, selected, allow_none=True):
    opts = ("<option value=''>Disabled (No Channel Selected)</option>" if allow_none else "") + "".join(
        f"<option value='{c.id}' {'selected' if str(c.id) == str(selected) else ''}># {e(c.name)}</option>"
        for c in guild.text_channels
    )
    return f"<select name='{name}'>{opts}</select>"


def valid_channel(guild, value):
    value = (value or "").strip()
    if value.isdigit() and any(c.id == int(value) for c in guild.text_channels):
        return value
    return None


# ==========================================
# PAGE RENDERERS
# ==========================================
def page_overview(guild, g, db):
    warns = sum(len(v) for v in db["warnings"].get(str(guild.id), {}).values())
    enabled_mods = sum(1 for k in MODULES if g["modules"].get(k, True))
    active_cmds = len(COMMANDS) - len(g["disabledCommands"])
    custom_cnt = len(g["customCommands"])

    stats = (
        f"<div class='stats-grid'>"
        f"<div class='stat-card'><div class='stat-top'><span>Total Members</span><div class='stat-icon'><i data-lucide='users'></i></div></div>"
        f"<div class='stat-val'>{guild.member_count}</div><div class='stat-sub'>{len(guild.text_channels)} Text Channels • {len(guild.roles)} Roles</div></div>"
        f"<div class='stat-card'><div class='stat-top'><span>Active Modules</span><div class='stat-icon'><i data-lucide='layers'></i></div></div>"
        f"<div class='stat-val'>{enabled_mods} / {len(MODULES)}</div><div class='stat-sub'>{active_cmds} Slash Commands Enabled</div></div>"
        f"<div class='stat-card'><div class='stat-top'><span>Active Warnings</span><div class='stat-icon'><i data-lucide='shield-alert'></i></div></div>"
        f"<div class='stat-val'>{warns}</div><div class='stat-sub'>Auto-Mute at {g['warnPunish']['threshold']} Warnings</div></div>"
        f"<div class='stat-card'><div class='stat-top'><span>Custom Tags</span><div class='stat-icon'><i data-lucide='terminal'></i></div></div>"
        f"<div class='stat-val'>{custom_cnt}</div><div class='stat-sub'>Prefix: <code class='mono'>{e(g['prefix'])}</code></div></div>"
        f"</div>"
    )

    log_ch = next((f"#{c.name}" for c in guild.text_channels if str(c.id) == str(g["modLogChannel"])), "Not Configured")
    wel_ch = next((f"#{c.name}" for c in guild.text_channels if str(c.id) == str(g["welcome"]["channel"])), "Not Configured")

    cards = (
        f"<div class='grid'>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='zap'></i> Quick Server Actions</div></div>"
        f"<p class='sub' style='margin-bottom:14px'>Trigger instant server-wide utilities or jump straight to key tools.</p>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/overview'>"
        f"<div class='btn-row' style='margin-top:0'>"
        f"<a class='btn' href='/manage/{guild.id}/embed'><i data-lucide='megaphone'></i> Broadcast Embed</a>"
        f"<a class='btn sec' href='/manage/{guild.id}/polls'><i data-lucide='bar-chart-3'></i> Launch Poll / Giveaway</a>"
        f"<button type='submit' name='op' value='lockdown' class='danger'><i data-lucide='lock'></i> Lockdown All Channels</button>"
        f"<button type='submit' name='op' value='unlockdown' class='sec'><i data-lucide='unlock'></i> Lift Lockdown</button>"
        f"</div></form></div>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='activity'></i> Configuration Health</div></div>"
        f"<table>"
        f"<tr><td>Audit Log Channel</td><td><b class='mono'>{e(log_ch)}</b></td><td><a href='/manage/{guild.id}/logging' style='color:#818cf8;font-weight:600'>Edit →</a></td></tr>"
        f"<tr><td>Welcome Channel</td><td><b class='mono'>{e(wel_ch)}</b></td><td><a href='/manage/{guild.id}/welcome' style='color:#818cf8;font-weight:600'>Edit →</a></td></tr>"
        f"<tr><td>AutoMod Protection</td><td><b>{'Active' if any(g['automod'][k] for k in ('antiLink','antiInvite','antiSpam','antiCaps')) else 'Standby'}</b></td><td><a href='/manage/{guild.id}/automod' style='color:#818cf8;font-weight:600'>Edit →</a></td></tr>"
        f"</table></div></div>"
    )
    return stats + cards


def page_modules(guild, g, db):
    descs = {
        "moderation": "Kick, ban, timeout, warn, purge, and lockdown commands.",
        "automod": "Automatic link, invite, spam, caps, and profanity filtering.",
        "logging": "Real-time audit logs for deleted messages, edits, and mod actions.",
        "welcome": "Customizable join and leave announcements.",
        "autorole": "Automatically assign roles when new members join.",
        "custom": "Custom prefix text tags and instant responses.",
        "utility": "Server info, avatars, banners, snipe, emoji steal, and timers.",
        "announce": "Native Discord polls, interactive giveaways, and announcements.",
    }
    boxes = "".join(
        switch(f"mod_{k}", e(name), descs.get(k, ""), g["modules"].get(k, True))
        for k, name in MODULES.items()
    )
    return (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='layers'></i> System Modules</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/modules'>"
        f"<div class='grid'>{boxes}</div>"
        f"<div class='btn-row'><button type='submit'><i data-lucide='save'></i> Save Module State</button></div></form></div>"
    )


def page_moderation(guild, g, db):
    mem_opts = "<option value=''>-- Select a Server Member --</option>" + "".join(
        f"<option value='{m.id}'>{e(m.name)} (@{e(m.username)}) — {m.id}</option>"
        for m in guild.members
    )
    wp = g.get("warnPunish", {"enabled": True, "threshold": 3, "timeoutMinutes": 60})

    mod_card = (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='gavel'></i> Execute Moderation Action</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/moderation'>"
        f"<input type='hidden' name='form_type' value='act'>"
        f"<div class='grid'><div>"
        f"<label class='f'>Quick Member Picker</label>"
        f"<select onchange=\"if(this.value)document.getElementById('uid_in').value=this.value\">{mem_opts}</select>"
        f"<label class='f'>Target User ID</label>"
        f"<input type='text' id='uid_in' name='user_id' placeholder='Paste numeric Discord User ID' required>"
        f"<label class='f'>Moderation Action</label><select name='action'>"
        f"<option value='warn'>⚠️ Issue Official Warning (+DM)</option>"
        f"<option value='dm'>📩 Send Official Staff DM</option>"
        f"<option value='timeout'>🔇 Timeout (Mute Member)</option>"
        f"<option value='untimeout'>🔊 Remove Timeout</option>"
        f"<option value='kick'>👢 Kick from Server</option>"
        f"<option value='softban'>🧹 Softban (Wipe Messages)</option>"
        f"<option value='ban'>🔨 Permanently Ban</option>"
        f"<option value='unban'>✅ Unban User by ID</option></select>"
        f"</div><div>"
        f"<label class='f'>Timeout Duration (Minutes)</label>"
        f"<input type='number' name='minutes' value='60' min='1' max='40320'>"
        f"<label class='f'>Reason / DM Message Content</label>"
        f"<textarea name='reason' maxlength='500' placeholder='Enter audit reason or staff DM text...' required></textarea>"
        f"</div></div>"
        f"<div class='btn-row'><button type='submit'><i data-lucide='zap'></i> Execute Action Immediately</button></div></form></div>"
    )

    rules_card = (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='sliders'></i> Warning & Auto-Punishment Rules</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/moderation'>"
        f"<input type='hidden' name='form_type' value='rules'>"
        + switch("dmOnMod", "Send DM Notice on Moderation", "Automatically DM users when they are warned, muted, kicked, or banned.", g.get("dmOnMod", True))
        + switch("autoPunish", "Auto-Timeout on Warning Threshold", "Automatically mute members when they reach the warning limit below.", wp.get("enabled", True))
        + f"<div class='grid'>"
        f"<div><label class='f'>Warning Threshold (2 - 20)</label><input type='number' name='threshold' min='2' max='20' value='{wp.get('threshold', 3)}'></div>"
        f"<div><label class='f'>Auto-Timeout Duration (Minutes)</label><input type='number' name='timeoutMinutes' min='1' max='10080' value='{wp.get('timeoutMinutes', 60)}'></div>"
        f"</div><div class='btn-row'><button type='submit' class='sec'><i data-lucide='save'></i> Save Punishment Policy</button></div></form></div>"
    )
    return mod_card + rules_card


def page_embed(guild, g, db):
    opts = "".join(f"<option value='{c.id}'># {e(c.name)}</option>" for c in guild.text_channels)
    return (
        f"<div class='grid'>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='megaphone'></i> Broadcast Composer</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/embed'>"
        f"<label class='f'>Destination Channel</label><select name='channel' required>{opts}</select>"
        f"<label class='f'>Embed Title</label>"
        f"<input type='text' id='eb_t' name='title' maxlength='200' value='Server Announcement' oninput=\"document.getElementById('pv_t').textContent=this.value||'Untitled'\">"
        f"<label class='f'>Embed Description (Supports Discord Markdown)</label>"
        f"<textarea id='eb_d' name='description' maxlength='3500' oninput=\"document.getElementById('pv_d').textContent=this.value\" required>Hello @everyone! Write your announcement here.</textarea>"
        f"<div class='grid' style='grid-template-columns:1fr 1fr;gap:12px'>"
        f"<div><label class='f'>Accent Color</label><input type='color' name='color' value='#5865f2' oninput=\"document.getElementById('pv_box').style.borderLeftColor=this.value\"></div>"
        f"<div><label class='f'>Thumbnail Image URL (Optional)</label><input type='text' name='thumbnail' placeholder='https://...'></div>"
        f"</div>"
        f"<div style='margin-top:12px'>" + switch("ping", "Mention @everyone", "Ping all members above the embed.", False) + "</div>"
        f"<div class='btn-row'><button type='submit'><i data-lucide='send'></i> Publish to Discord</button></div></form></div>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='eye'></i> Live Discord Preview</div></div>"
        f"<div class='dc-preview'>"
        f"<div class='dc-msg-head'><div class='dc-bot-av'>B</div><div><b>BBC</b><span class='dc-bot-tag'>BOT</span></div></div>"
        f"<div class='dc-embed' id='pv_box' style='border-left-color:#5865f2'>"
        f"<div class='dc-embed-title' id='pv_t'>Server Announcement</div>"
        f"<div class='dc-embed-desc' id='pv_d'>Hello @everyone! Write your announcement here.</div>"
        f"<div class='dc-embed-foot'>BBC • Just now</div>"
        f"</div></div></div></div>"
    )


def page_polls(guild, g, db):
    opts = "".join(f"<option value='{c.id}'># {e(c.name)}</option>" for c in guild.text_channels)
    poll_card = (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='bar-chart-3'></i> Launch Native Discord Poll</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/polls'>"
        f"<input type='hidden' name='kind' value='poll'>"
        f"<label class='f'>Target Channel</label><select name='channel' required>{opts}</select>"
        f"<label class='f'>Poll Question</label><input type='text' name='question' placeholder='Who Has More AURA???' required>"
        f"<label class='f'>Poll Answers (Comma separated, 2 to 10 options)</label>"
        f"<input type='text' name='options' placeholder='❤️ VanatomXD, 🤍 Broken_piece' required>"
        f"<div class='grid' style='grid-template-columns:1fr 1fr;gap:12px'>"
        f"<div><label class='f'>Duration (Hours)</label><input type='number' name='hours' value='8' min='1' max='168'></div>"
        f"<div><label class='f'>Multi-Vote</label><select name='multiple'><option value='0'>Single Answer Only</option><option value='1'>Allow Multiple Answers</option></select></div>"
        f"</div>"
        f"<div style='margin-top:12px'>" + switch("ping", "Ping @everyone", "Mention @everyone below the poll.", False) + "</div>"
        f"<div class='btn-row'><button type='submit'><i data-lucide='vote'></i> Launch Native Poll</button></div></form></div>"
    )

    gaw_card = (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='gift'></i> Launch Giveaway Drop</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/polls'>"
        f"<input type='hidden' name='kind' value='giveaway'>"
        f"<label class='f'>Target Channel</label><select name='channel' required>{opts}</select>"
        f"<label class='f'>Prize Name</label><input type='text' name='prize' placeholder='Discord Nitro (1 Month)' required>"
        f"<div class='grid' style='grid-template-columns:1fr 1fr;gap:12px'>"
        f"<div><label class='f'>Duration (Minutes)</label><input type='number' name='minutes' value='60' min='1' max='10080'></div>"
        f"<div><label class='f'>Winners Count</label><input type='number' name='winners' value='1' min='1' max='20'></div>"
        f"</div>"
        f"<label class='f'>Extra Rules / Notes (Optional)</label><textarea name='description' placeholder='React or click Enter in Discord to participate!'></textarea>"
        f"<div style='margin-top:12px'>" + switch("ping", "Ping @everyone", "Mention @everyone when posting.", False) + "</div>"
        f"<div class='btn-row'><button type='submit' class='ok'><i data-lucide='sparkles'></i> Publish Giveaway</button></div></form>"
        f"<p class='sub' style='margin-top:10px'>Tip: Use <code>/giveaway</code> inside Discord for interactive button entry tracking.</p></div>"
    )
    return f"<div class='grid'>{poll_card}{gaw_card}</div>"


def page_automod(guild, g, db):
    a = g["automod"]
    inner = (
        "<div class='grid'><div>"
        + switch("antiLink", "Block External Links", "Delete HTTP/HTTPS links from non-admins.", a["antiLink"])
        + switch("antiInvite", "Block Discord Invites", "Delete discord.gg server invite links.", a["antiInvite"])
        + switch("antiCaps", "Anti-Caps Filter", "Delete messages with >70% uppercase letters.", a["antiCaps"])
        + switch("antiSpam", "Anti-Spam Protection", "Auto-mute users sending 5+ messages in 5 seconds.", a["antiSpam"])
        + switch("antiMassMention", "Anti-Mass Mention", "Block messages exceeding the mention limit.", a["antiMassMention"])
        + "</div><div>"
        f"<label class='f' style='margin-top:0'>Max Mentions Per Message</label><input type='number' name='maxMentions' min='2' max='50' value='{a['maxMentions']}'>"
        f"<label class='f'>Filtered Words (One word per line)</label><textarea name='bannedWords' style='min-height:210px' placeholder='badword1\nbadword2'>{e(chr(10).join(a['bannedWords']))}</textarea>"
        "</div></div>"
    )
    return (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='bot'></i> Automated Defense Rules</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/automod'>{inner}"
        f"<div class='btn-row'><button type='submit'><i data-lucide='save'></i> Save AutoMod Configuration</button></div></form></div>"
    )


def page_logging(guild, g, db):
    inner = (
        "<label class='f' style='margin-top:0'>Designated Audit Log Channel</label>"
        + channel_select("modLogChannel", guild, g["modLogChannel"])
        + "<div style='margin-top:16px'>"
        + switch("log_moderation", "Moderation & AutoMod Events", "Log bans, kicks, warnings, timeouts, and AutoMod triggers.", g["log"]["moderation"])
        + switch("log_messages", "Message Deletions & Edits", "Log deleted and modified text messages with before/after content.", g["log"]["messages"])
        + switch("log_members", "Member Joins & Leaves", "Log account creation dates and member departures.", g["log"]["members"])
        + "</div>"
    )
    return (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='scroll-text'></i> Audit Log Routing</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/logging'>{inner}"
        f"<div class='btn-row'>"
        f"<button type='submit' name='op' value='save'><i data-lucide='save'></i> Save Logging Rules</button>"
        f"<button type='submit' name='op' value='test_log' class='sec'><i data-lucide='flask-conical'></i> Send Test Log Embed</button>"
        f"</div></form></div>"
    )


def page_welcome(guild, g, db):
    w = g["welcome"]
    preview_join = format_welcome_text(w["joinMessage"], "@NewMember", "NewMember", guild.name, guild.member_count)
    return (
        f"<div class='grid'>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='hand'></i> Welcome & Leave Studio</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/welcome'>"
        f"<label class='f' style='margin-top:0'>Announcement Channel</label>" + channel_select("channel", guild, w["channel"])
        + "<div style='margin-top:12px'>"
        + switch("joinEnabled", "Enable Welcome Embeds", "Send a greeting embed when a new member joins.", w["joinEnabled"])
        + "</div>"
        f"<label class='f'>Welcome Message Template</label>"
        f"<textarea id='w_join' name='joinMessage' maxlength='500' oninput=\"document.getElementById('w_pv').textContent=this.value\">{e(w['joinMessage'])}</textarea>"
        f"<div>"
        f"<span class='var-pill' onclick=\"insertVar('w_join','{{user}}')\">{'{user}'}</span>"
        f"<span class='var-pill' onclick=\"insertVar('w_join','{{username}}')\">{'{username}'}</span>"
        f"<span class='var-pill' onclick=\"insertVar('w_join','{{server}}')\">{'{server}'}</span>"
        f"<span class='var-pill' onclick=\"insertVar('w_join','{{count}}')\">{'{count}'}</span>"
        f"</div>"
        + "<div style='margin-top:16px'>"
        + switch("leaveEnabled", "Enable Leave Embeds", "Post a notice when a member leaves the server.", w["leaveEnabled"])
        + "</div>"
        f"<label class='f'>Leave Message Template</label>"
        f"<textarea id='w_leave' name='leaveMessage' maxlength='500'>{e(w['leaveMessage'])}</textarea>"
        f"<div class='btn-row'>"
        f"<button type='submit' name='op' value='save'><i data-lucide='save'></i> Save Changes</button>"
        f"<button type='submit' name='op' value='test_join' class='ok'><i data-lucide='sparkles'></i> Test Welcome in Discord</button>"
        f"<button type='submit' name='op' value='test_leave' class='sec'><i data-lucide='log-out'></i> Test Leave in Discord</button>"
        f"</div></form></div>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='eye'></i> Live Welcome Preview</div></div>"
        f"<div class='dc-preview'>"
        f"<div class='dc-msg-head'><div class='dc-bot-av'>B</div><div><b>BBC</b><span class='dc-bot-tag'>BOT</span></div></div>"
        f"<div class='dc-embed' style='border-left-color:#10b981'>"
        f"<div class='dc-embed-title'>👋 Welcome to {e(guild.name)}</div>"
        f"<div class='dc-embed-desc' id='w_pv'>{e(preview_join)}</div>"
        f"<div class='dc-embed-foot'>BBC • Just now</div>"
        f"</div></div></div></div>"
    )


def page_autorole(guild, g, db):
    roles = [r for r in guild.roles if not r.managed and r.id != guild.id]
    chips = "".join(
        f"<label class='chip-item'><input type='checkbox' name='autoRoles' value='{r.id}' {'checked' if str(r.id) in g['autoRoles'] else ''}>"
        f"<span class='chip-box'><span class='chip-dot' style='background:{r.color}'></span> {e(r.name)}</span></label>"
        for r in reversed(roles)
    ) or "<p class='sub'>No assignable roles found.</p>"

    return (
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='user-check'></i> Automatic Join Roles</div></div>"
        f"<p class='sub' style='margin-bottom:14px'>Click any role badges below to automatically assign them when a member joins. Ensure the bot's role is placed above these roles in Discord.</p>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/autorole'>"
        f"<div class='chip-grid'>{chips}</div>"
        f"<div class='btn-row'><button type='submit'><i data-lucide='save'></i> Save Auto-Roles</button></div></form></div>"
    )


def page_commands(guild, g, db):
    labels = {**MODULES, "core": "Core System"}
    groups = defaultdict(list)
    for name, info in COMMANDS.items():
        groups[info["module"]].append((name, info["description"]))

    toolbar = (
        "<div class='card' style='display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap'>"
        "<input type='text' style='max-width:380px;margin:0' placeholder='🔍 Search 35+ slash commands...' "
        "oninput=\"document.querySelectorAll('.cmd-item').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(this.value.toLowerCase())?'':'none')\">"
        "<div class='btn-row' style='margin:0'>"
        "<button type='button' class='sec' onclick='toggleAllCmds(true)'>Enable All</button>"
        "<button type='button' class='sec' onclick='toggleAllCmds(false)'>Disable All</button>"
        "</div></div>"
    )

    sections = ""
    for mod, items in groups.items():
        cards = ""
        for name, desc in items:
            if mod == "core":
                sw = "<span class='badge'>LOCKED ON</span>"
            else:
                checked = "checked" if name not in g["disabledCommands"] else ""
                sw = f"<div class='switch'><input class='cmd-toggle' type='checkbox' name='cmd_{name}' {checked}><span class='slider'></span></div>"
            cards += (
                f"<label class='switch-row cmd-item' style='margin:0'>"
                f"<div class='switch-info'><b class='mono'>/{e(name)}</b><span>{e(desc)}</span></div>{sw}</label>"
            )
        sections += (
            f"<div class='card'><div class='card-head'><div class='card-title'>{e(labels.get(mod, mod))}</div></div>"
            f"<div class='grid-3'>{cards}</div></div>"
        )
    return toolbar + f"<form class='ajax' method='POST' action='/manage/{guild.id}/commands'>{sections}<div class='btn-row'><button type='submit'><i data-lucide='save'></i> Save Command Permissions</button></div></form>"


def page_custom(guild, g, db):
    rows = ""
    for name, resp in g["customCommands"].items():
        rows += (
            f"<tr><td><code class='mono' style='color:#a5b4fc;font-weight:700'>{e(g['prefix'])}{e(name)}</code></td>"
            f"<td style='color:var(--muted)'>{e(resp[:140])}</td><td style='text-align:right'>"
            f"<form class='ajax' method='POST' action='/manage/{guild.id}/custom' style='display:inline'>"
            f"<input type='hidden' name='action' value='delete'><input type='hidden' name='name' value='{e(name)}'>"
            f"<button class='danger' type='submit'>Delete</button></form></td></tr>"
        )
    table = f"<table><tr><th>Trigger</th><th>Response</th><th></th></tr>{rows}</table>" if rows else "<p class='sub'>No custom tags configured yet.</p>"

    return (
        f"<div class='grid'>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='sliders'></i> Command Prefix</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/custom'>"
        f"<input type='hidden' name='action' value='prefix'>"
        f"<label class='f' style='margin-top:0'>Server Text Prefix</label>"
        f"<input type='text' name='prefix' maxlength='3' value='{e(g['prefix'])}'>"
        f"<div class='btn-row'><button type='submit'><i data-lucide='save'></i> Update Prefix</button></div></form></div>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='plus-circle'></i> Create Custom Tag</div></div>"
        f"<form class='ajax' method='POST' action='/manage/{guild.id}/custom'>"
        f"<input type='hidden' name='action' value='add'>"
        f"<label class='f' style='margin-top:0'>Tag Name</label><input type='text' name='name' maxlength='32' placeholder='rules' required>"
        f"<label class='f'>Response Text</label><textarea name='response' maxlength='1500' placeholder='Please follow server rules!' required></textarea>"
        f"<div class='btn-row'><button type='submit'><i data-lucide='plus'></i> Save Custom Tag</button></div></form></div></div>"
        f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='message-square-code'></i> Active Custom Tags ({len(g['customCommands'])})</div></div>{table}</div>"
    )


def page_warnings(guild, g, db):
    data = db["warnings"].get(str(guild.id), {})
    mem_map = {str(m.id): f"{m.name} (@{m.username})" for m in guild.members}
    blocks = ""
    for uid, lst in data.items():
        if not lst:
            continue
        u_label = mem_map.get(str(uid), f"User ID: {uid}")
        rows = "".join(
            f"<tr><td><code class='mono'>#{w['id']}</code></td><td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(w['time']))}</td>"
            f"<td>{e(w['mod'])}</td><td>{e(w['reason'])}</td><td style='text-align:right'>"
            f"<form class='ajax' method='POST' action='/manage/{guild.id}/warnings' style='display:inline'>"
            f"<input type='hidden' name='action' value='delete'><input type='hidden' name='user_id' value='{e(uid)}'><input type='hidden' name='warn_id' value='{w['id']}'>"
            f"<button class='danger' type='submit'>Delete</button></form></td></tr>"
            for w in lst
        )
        clear = (
            f"<form class='ajax' method='POST' action='/manage/{guild.id}/warnings'>"
            f"<input type='hidden' name='action' value='clear'><input type='hidden' name='user_id' value='{e(uid)}'>"
            f"<button class='danger' type='submit'>Clear All Warnings</button></form>"
        )
        blocks += (
            f"<div class='card'><div class='card-head'><div class='card-title'><i data-lucide='user'></i> {e(u_label)} — {len(lst)} Warning(s)</div>{clear}</div>"
            f"<table><tr><th>ID</th><th>Date</th><th>Moderator</th><th>Reason</th><th></th></tr>{rows}</table></div>"
        )
    return blocks or "<div class='card'><p class='sub'>🎉 Clean record! No active warnings in this server.</p></div>"


RENDER = {
    "overview": page_overview, "modules": page_modules, "moderation": page_moderation,
    "embed": page_embed, "polls": page_polls, "automod": page_automod,
    "logging": page_logging, "welcome": page_welcome, "autorole": page_autorole,
    "commands": page_commands, "custom": page_custom, "warnings": page_warnings,
}


# ==========================================
# SAVE HANDLERS
# ==========================================
async def save_overview(guild, g, db, form):
    op = form.get("op")
    if op in ("lockdown", "unlockdown"):
        lock = op == "lockdown"
        count = 0
        for c in guild.text_channels[:25]:
            overwrites = {"id": str(guild.id), "type": 0, "deny": "2048" if lock else "0", "allow": "0"}
            st, _ = await discord_req(
                "PUT",
                f"/channels/{c.id}/permissions/{guild.id}",
                json_data=overwrites,
                reason="Dashboard Lockdown" if lock else "Dashboard Unlockdown",
            )
            if st in (200, 204):
                count += 1
        return f"🚨 Locked {count} channels." if lock else f"🔓 Unlocked {count} channels."
    return "✅ Overview updated."


async def save_modules(guild, g, db, form):
    for k in MODULES:
        g["modules"][k] = f"mod_{k}" in form
    return "✅ Module states synced!"


async def save_embed(guild, g, db, form):
    ch_id = valid_channel(guild, form.get("channel"))
    if not ch_id:
        return "❌ Invalid channel selected."
    title = (form.get("title") or "").strip()[:200]
    desc = (form.get("description") or "").strip()[:3500]
    thumb = (form.get("thumbnail") or "").strip()
    color_hex = (form.get("color") or "#5865f2").lstrip("#")
    try:
        color = int(color_hex, 16)
    except ValueError:
        color = 0x5865F2
    embed_obj = {
        "title": title,
        "description": desc,
        "color": color,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "footer": {"text": "BBC"},
    }
    if thumb.startswith("http"):
        embed_obj["thumbnail"] = {"url": thumb}
    payload = {"content": "@everyone" if "ping" in form else None, "embeds": [embed_obj]}
    status, _ = await discord_req("POST", f"/channels/{ch_id}/messages", json_data=payload)
    if status in (200, 201):
        return "🚀 Broadcast published to Discord!"
    return f"❌ Failed to publish (HTTP {status})."


async def save_polls(guild, g, db, form):
    ch_id = valid_channel(guild, form.get("channel"))
    if not ch_id:
        return "❌ Select a valid text channel."
    kind = form.get("kind")
    ping = "ping" in form

    if kind == "poll":
        q = (form.get("question") or "").strip()[:300]
        raw_opts = [o.strip() for o in (form.get("options") or "").split(",") if o.strip()]
        if len(raw_opts) < 2 or len(raw_opts) > 10:
            return "❌ Provide between 2 and 10 comma-separated answers."
        hours = max(1, min(168, int(form.get("hours") or 8)))
        mult = form.get("multiple") == "1"
        default_emojis = ["❤️", "🤍", "💙", "💜", "💚", "💛", "🧡", "⚡", "🔥", "👑"]
        answers = []
        for i, opt in enumerate(raw_opts):
            parts = opt.split(" ", 1)
            if len(parts) == 2 and not parts[0].isalnum():
                emoji_name, text = parts[0], parts[1].strip()
            else:
                emoji_name, text = default_emojis[i % len(default_emojis)], opt
            answers.append({"poll_media": {"text": text[:55], "emoji": {"name": emoji_name}}})

        payload = {
            "content": "@everyone" if ping else None,
            "poll": {
                "question": {"text": q},
                "answers": answers,
                "duration": hours,
                "allow_multiselect": mult,
                "layout_type": 1,
            },
        }
        status, _ = await discord_req("POST", f"/channels/{ch_id}/messages", json_data=payload)
        if status in (200, 201):
            return "📊 Native Poll published to Discord!"
        return f"❌ Poll failed (HTTP {status})."

    if kind == "giveaway":
        prize = (form.get("prize") or "").strip()[:200]
        mins = max(1, min(10080, int(form.get("minutes") or 60)))
        winners = max(1, min(20, int(form.get("winners") or 1)))
        desc = (form.get("description") or "").strip()[:1000]
        end_ts = now_ts() + (mins * 60)
        lines = []
        if desc:
            lines.append(f"{desc}\n")
        lines.append(f"🎁 **Prize:** `{prize}`")
        lines.append(f"🏆 **Winners:** `{winners}`")
        lines.append(f"⏰ **Ends:** <t:{end_ts}:R> (<t:{end_ts}:f>)")
        lines.append("\n*React with 🎉 below to enter!*")
        embed = {
            "title": f"🎉 GIVEAWAY — {prize}",
            "description": "\n".join(lines),
            "color": 0x5865F2,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "footer": {"text": "BBC"},
        }
        status, msg_data = await discord_req(
            "POST",
            f"/channels/{ch_id}/messages",
            json_data={"content": "@everyone" if ping else None, "embeds": [embed]},
        )
        if status in (200, 201) and msg_data:
            await discord_req("PUT", f"/channels/{ch_id}/messages/{msg_data['id']}/reactions/%F0%9F%8E%89/@me")
            return "🎉 Giveaway published & reaction added!"
        return f"❌ Giveaway failed (HTTP {status})."

    return "❌ Unknown operation."


async def save_moderation(guild, g, db, form):
    if form.get("form_type") == "rules":
        g["dmOnMod"] = "dmOnMod" in form
        g["warnPunish"] = {
            "enabled": "autoPunish" in form,
            "threshold": max(2, min(20, int(form.get("threshold") or 3))),
            "timeoutMinutes": max(1, min(10080, int(form.get("timeoutMinutes") or 60))),
        }
        return "✅ Warning & Auto-Punishment rules saved!"

    raw = (form.get("user_id") or "").strip()
    if not raw.isdigit():
        return "❌ Enter a valid numeric User ID."
    uid = int(raw)
    if uid == guild.owner_id:
        return "❌ Cannot moderate the server owner."

    action = form.get("action")
    reason = (form.get("reason") or "").strip()[:450] or "No reason provided"
    audit_reason = f"{reason} (by BBC Console)"

    if action == "dm":
        st, dm_ch = await discord_req("POST", "/users/@me/channels", json_data={"recipient_id": str(uid)})
        if st in (200, 201) and dm_ch and dm_ch.get("id"):
            embed = {
                "title": f"📩 Official Notice — {guild.name}",
                "description": reason,
                "color": 0x5865F2,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "footer": {"text": "BBC"},
            }
            s2, _ = await discord_req("POST", f"/channels/{dm_ch['id']}/messages", json_data={"embeds": [embed]})
            if s2 in (200, 201):
                return f"📩 Official DM delivered to {uid}!"
        return "❌ Could not DM user (their DMs may be closed)."

    if action == "warn":
        g["warnCounter"] += 1
        entry = {"id": g["warnCounter"], "mod": "Console", "reason": reason, "time": now_ts()}
        lst = db["warnings"].setdefault(str(guild.id), {}).setdefault(str(uid), [])
        lst.append(entry)
        await send_mod_log(guild.id, g, "Member Warned", f"**Target:** <@{uid}>\n**By:** Console\n**Warnings:** {len(lst)}\n**Reason:** {reason}", 0xF59E0B)
        return f"⚠️ User {uid} warned (Total: {len(lst)})."

    if action == "kick":
        status, _ = await discord_req("DELETE", f"/guilds/{guild.id}/members/{uid}", reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Kicked", f"**Target:** <@{uid}>\n**By:** Console\n**Reason:** {reason}")
            return f"👢 Kicked user {uid}."
        return f"❌ Kick failed (HTTP {status})."

    if action == "ban":
        status, _ = await discord_req("PUT", f"/guilds/{guild.id}/bans/{uid}", json_data={"delete_message_seconds": 0}, reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Banned", f"**Target:** <@{uid}>\n**By:** Console\n**Reason:** {reason}", 0xEF4444)
            return f"🔨 Banned user {uid}."
        return f"❌ Ban failed (HTTP {status})."

    if action == "unban":
        status, _ = await discord_req("DELETE", f"/guilds/{guild.id}/bans/{uid}", reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Unbanned", f"**Target:** <@{uid}>\n**By:** Console\n**Reason:** {reason}", 0x10B981)
            return f"✅ Unbanned user {uid}."
        return f"❌ Unban failed (HTTP {status})."

    if action == "softban":
        s1, _ = await discord_req("PUT", f"/guilds/{guild.id}/bans/{uid}", json_data={"delete_message_seconds": 86400}, reason=audit_reason)
        if s1 in (200, 204):
            await discord_req("DELETE", f"/guilds/{guild.id}/bans/{uid}", reason="Softban complete")
            await send_mod_log(guild.id, g, "Member Softbanned", f"**Target:** <@{uid}>\n**By:** Console\n**Reason:** {reason}", 0xF59E0B)
            return f"🧹 Softbanned user {uid}."
        return f"❌ Softban failed (HTTP {s1})."

    if action == "timeout":
        minutes = max(1, min(40320, int(form.get("minutes") or 60)))
        until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)).isoformat()
        status, _ = await discord_req("PATCH", f"/guilds/{guild.id}/members/{uid}", json_data={"communication_disabled_until": until}, reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Member Timed Out", f"**Target:** <@{uid}>\n**Duration:** {minutes}m\n**By:** Console\n**Reason:** {reason}", 0xF59E0B)
            return f"🔇 Timed out user {uid} for {minutes}m."
        return f"❌ Timeout failed (HTTP {status})."

    if action == "untimeout":
        status, _ = await discord_req("PATCH", f"/guilds/{guild.id}/members/{uid}", json_data={"communication_disabled_until": None}, reason=audit_reason)
        if status in (200, 204):
            await send_mod_log(guild.id, g, "Timeout Removed", f"**Target:** <@{uid}>\n**By:** Console", 0x10B981)
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
    return "🛡️ AutoMod defense rules saved!"


async def save_logging(guild, g, db, form):
    g["modLogChannel"] = valid_channel(guild, form.get("modLogChannel"))
    for k in ("moderation", "messages", "members"):
        g["log"][k] = f"log_{k}" in form
    if form.get("op") == "test_log":
        if not g["modLogChannel"]:
            return "❌ Select an Audit Log Channel first."
        embed = {
            "title": "🛡️ Audit Log Test",
            "description": f"Audit logging is active and connected to **{guild.name}**.",
            "color": 0x10B981,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "footer": {"text": "BBC"},
        }
        st, _ = await discord_req("POST", f"/channels/{g['modLogChannel']}/messages", json_data={"embeds": [embed]})
        if st in (200, 201):
            return "✅ Saved & sent test log embed!"
        return f"❌ Saved, but test log failed (HTTP {st})."
    return "✅ Audit logging settings saved!"


async def save_welcome(guild, g, db, form):
    w = g["welcome"]
    w["channel"] = valid_channel(guild, form.get("channel"))
    w["joinEnabled"] = "joinEnabled" in form
    w["leaveEnabled"] = "leaveEnabled" in form
    w["joinMessage"] = (form.get("joinMessage") or "")[:500]
    w["leaveMessage"] = (form.get("leaveMessage") or "")[:500]

    op = form.get("op", "save")
    if op in ("test_join", "test_leave"):
        if not w["channel"]:
            return "❌ Select a Welcome & Leave Channel first."
        is_join = op == "test_join"
        tpl = w["joinMessage"] if is_join else w["leaveMessage"]
        text = format_welcome_text(tpl, "@TestUser", "TestUser", guild.name, guild.member_count)
        embed = {
            "title": f"🧪 [TEST] {'👋 Welcome to ' + guild.name if is_join else '📤 Member Left'}",
            "description": text,
            "color": 0x10B981 if is_join else 0x2B2D31,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "footer": {"text": "BBC"},
        }
        status, _ = await discord_req("POST", f"/channels/{w['channel']}/messages", json_data={"embeds": [embed]})
        if status in (200, 201):
            return f"🧪 Sent {'Welcome' if is_join else 'Leave'} test embed to Discord!"
        return f"❌ Test failed (HTTP {status}). Check bot permissions."

    return "✅ Welcome & Leave configuration saved!"


async def save_autorole(guild, g, db, form):
    role_ids = {r.id for r in guild.roles}
    chosen = [v for v in form.getlist("autoRoles") if v.isdigit() and int(v) in role_ids and int(v) != guild.id]
    g["autoRoles"] = chosen[:15]
    return f"🎭 Saved {len(g['autoRoles'])} auto-role(s)!"


async def save_commands(guild, g, db, form):
    g["disabledCommands"] = [n for n, i in COMMANDS.items() if i["module"] != "core" and f"cmd_{n}" not in form]
    return "✅ Slash command permissions synced!"


async def save_custom(guild, g, db, form):
    action = form.get("action")
    if action == "prefix":
        p = (form.get("prefix") or "").strip()[:3]
        if not p or any(c.isspace() for c in p):
            return "❌ Invalid prefix."
        g["prefix"] = p
        return f"✅ Server prefix updated to {p}"
    name = (form.get("name") or "").lower().strip()
    if action == "delete":
        g["customCommands"].pop(name, None)
        return ("🗑️ Custom tag deleted.", True)
    if action == "add":
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
            return "❌ Name must be 1-32 chars (letters, numbers, - or _)."
        g["customCommands"][name] = (form.get("response") or "")[:1500]
        return ("✨ Custom tag created!", True)
    return "❌ Unknown action."


async def save_warnings(guild, g, db, form):
    uid = (form.get("user_id") or "").strip()
    data = db["warnings"].get(str(guild.id), {})
    if form.get("action") == "clear":
        data.pop(uid, None)
        return ("🧹 Warnings cleared.", True)
    if form.get("action") == "delete":
        try:
            wid = int(form.get("warn_id"))
        except (TypeError, ValueError):
            return "❌ Invalid warning."
        data[uid] = [w for w in data.get(uid, []) if w["id"] != wid]
        return ("🗑️ Warning removed.", True)
    return "❌ Unknown action."


SAVE = {
    "overview": save_overview, "modules": save_modules, "moderation": save_moderation,
    "embed": save_embed, "polls": save_polls, "automod": save_automod,
    "logging": save_logging, "welcome": save_welcome, "autorole": save_autorole,
    "commands": save_commands, "custom": save_custom, "warnings": save_warnings,
}


# ==========================================
# ROUTES & AJAX HANDLER
# ==========================================
def login_page(error=""):
    err = f"<div style='padding:12px;border-radius:8px;background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.4);color:#fca5a5;margin-bottom:14px;font-size:13px;font-weight:600'>{e(error)}</div>" if error else ""
    return shell(
        "Sign In",
        f"<div style='margin:auto;width:390px;padding:20px'><div class='card' style='padding:32px'>"
        f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:18px'>"
        f"<div style='width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent-2));display:flex;align-items:center;justify-content:center;font-weight:800;font-size:18px'>B</div>"
        f"<div><h2 style='font-size:19px;font-weight:800'>BBC Console</h2><p class='sub' style='margin:0'>Administrator Authentication</p></div></div>{err}"
        f"<form method='POST' action='/login'><label class='f'>Access Password</label>"
        f"<input type='password' name='password' placeholder='••••••••••••' autofocus required>"
        f"<button type='submit' style='width:100%;margin-top:18px'>🔓 Sign In to Console</button></form></div></div>",
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
    await asyncio.sleep(0.8)
    return HTMLResponse(login_page("Invalid administrator password."), status_code=401)


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
        body = "<div class='main'><h1>No Servers Connected</h1><p class='sub'>Invite the bot to your Discord server, then refresh this page.</p></div>"
        return shell("Select Server", body)
    cards = ""
    for gd in guilds:
        icon_html = f"<img src='{gd.icon}' alt='icon'>" if gd.icon else gd.name[:2].upper()
        cards += (
            f"<a class='srv-card' href='/manage/{gd.id}/overview'>"
            f"<div class='srv-av'>{icon_html}</div>"
            f"<h3 style='font-size:16px;font-weight:700;margin-bottom:6px'>{e(gd.name)}</h3>"
            f"<span class='badge'>OPEN CONSOLE →</span></a>"
        )
    return shell(
        "Select Server",
        f"<div class='main'><div class='top-header'><div><h1>Select a Server</h1><p class='sub'>Choose a connected Discord server to manage</p></div>"
        f"<a class='btn sec' href='/logout'><i data-lucide='log-out'></i> Sign Out</a></div><div class='servers-grid'>{cards}</div></div>",
    )


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
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not authed(request):
        if is_ajax:
            return JSONResponse({"ok": False, "msg": "❌ Session expired. Refresh to sign in."}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    handler = SAVE.get(page)
    guild = await get_guild_details(guild_id)
    if not guild or not handler:
        if is_ajax:
            return JSONResponse({"ok": False, "msg": "❌ Server or handler not found."}, status_code=404)
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    db = load_db()
    g = get_guild(db, guild_id)
    res = await handler(guild, g, db, form)

    reload_page = False
    if isinstance(res, tuple):
        msg, reload_page = res
    else:
        msg = res

    background_tasks.add_task(save_db, db)

    if is_ajax:
        return JSONResponse({"ok": not msg.startswith("❌"), "msg": msg, "reload": reload_page})
    return RedirectResponse(f"/manage/{guild_id}/{page}?msg={quote(msg)}", status_code=303)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Missing TOKEN in your .env file.")
    print(f"🌐 BBC Console live at http://localhost:{PORT}")
    uvicorn.run(web_app, host="0.0.0.0", port=PORT, log_level="info")
