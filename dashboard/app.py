"""
app.py  –  ByGorgii Admin Dashboard
Liest Stats direkt aus dashboard_stats.json (von main.py geschrieben).
Live-Events kommen über eine shared Queue wenn Bot + Dashboard im selben Prozess laufen.
"""

import os, json, datetime, threading, time, sys, queue as _queue, subprocess, re
from functools import wraps
from urllib.parse import quote
from flask import (Flask, render_template, redirect, url_for,
                   session, request, jsonify, Response)
from flask_sock import Sock
import requests
from dotenv import load_dotenv

# ── Pfade ───────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.abspath(__file__))
_ROOT     = os.path.dirname(_HERE)
_ENV_FILE = os.path.join(_ROOT, ".env")
_STATS_FILE = os.path.join(_ROOT, "dashboard_stats.json")

load_dotenv(_ENV_FILE)

# ── Flask ────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(_HERE, "templates"),
    static_folder=os.path.join(_HERE, "static"),
)
sock = Sock(app)

# ── Config ───────────────────────────────────────────────────
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN", "")
GUILD_ID              = int(os.getenv("GUILD_ID", "0"))
SECRET_KEY            = os.getenv("DASHBOARD_SECRET", "change_me")
DASHBOARD_PORT        = int(os.getenv("DASHBOARD_PORT", "5000"))
DASHBOARD_DEBUG       = os.getenv("DASHBOARD_DEBUG", "false").lower() == "true"
_REDIRECT_URI_ENV     = os.getenv("DISCORD_REDIRECT_URI", "").strip()

ADMIN_USER_IDS: set[int] = set()
for _x in os.getenv("ADMIN_USER_IDS", "").split(","):
    _x = _x.strip()
    if _x.isdigit():
        ADMIN_USER_IDS.add(int(_x))

DISCORD_API   = "https://discord.com/api/v10"
app.secret_key = SECRET_KEY
app.jinja_env.globals["enumerate"] = enumerate
app.jinja_env.globals["zip"]       = zip
app.jinja_env.globals["len"]       = len
app.jinja_env.globals["int"]       = int
app.jinja_env.globals["str"]       = str
app.jinja_env.globals["max"]       = max

# ── WebSocket Subscriber Queues (für Live-Events) ────────────
_ws_queues: list[_queue.Queue] = []
_ws_lock = threading.Lock()

def ws_subscribe() -> _queue.Queue:
    q = _queue.Queue(maxsize=200)
    with _ws_lock:
        _ws_queues.append(q)
    return q

def ws_unsubscribe(q: _queue.Queue):
    with _ws_lock:
        try: _ws_queues.remove(q)
        except ValueError: pass

def ws_broadcast(event_type: str, data: dict):
    """Sendet ein Event an alle Browser-Clients."""
    payload = json.dumps({
        "type": event_type,
        "data": data,
        "ts":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, default=str)
    dead = []
    with _ws_lock:
        queues = list(_ws_queues)
    for q in queues:
        try:
            q.put_nowait(payload)
        except _queue.Full:
            dead.append(q)
    for q in dead:
        ws_unsubscribe(q)

# ── Versuche bot_patch aus main.py zu nutzen (gleicher Prozess) ──
# Wenn Bot + Dashboard zusammen laufen, teilen sie dashboard_state
_bot_state = None
try:
    # Versuche die bereits im Hauptprozess laufende Instanz zu finden
    import importlib, sys as _sys
    if 'dashboard_state' in dir(_sys.modules.get('__main__', None)):
        _bot_state = _sys.modules['__main__'].dashboard_state
        print("✅ Bot-State direkt verbunden (gleicher Prozess)")
    else:
        # Standalone-Modus: Wir lesen nur aus dashboard_stats.json
        print("ℹ️  Standalone-Modus: Lese aus dashboard_stats.json")
except Exception:
    pass

# ── Stats aus JSON lesen (funktioniert immer, egal ob Bot läuft) ──
def load_stats() -> dict:
    """Liest die aktuellen Stats aus dashboard_stats.json."""
    try:
        with open(_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "messages": {}, "voice_time": {}, "voice_days": {},
            "daily_active": {}, "gambling": {}, "mod_actions": [],
            "deletions": [], "joins": [],
        }

def get_summary(stats: dict) -> dict:
    return {
        "total_messages":    sum(v.get("total", 0) for v in stats.get("messages", {}).values()),
        "total_voice_secs":  sum(stats.get("voice_time", {}).values()),
        "total_mod_actions": len(stats.get("mod_actions", [])),
        "total_deletions":   len(stats.get("deletions", [])),
        "total_joins":       len(stats.get("joins", [])),
    }

def get_message_leaderboard(stats: dict, limit: int = 15) -> list:
    items = [
        {"uid": k, "username": v.get("username", k), "total": v.get("total", 0)}
        for k, v in stats.get("messages", {}).items()
    ]
    return sorted(items, key=lambda x: x["total"], reverse=True)[:limit]

def get_voice_leaderboard(stats: dict, limit: int = 15) -> list:
    msg = stats.get("messages", {})
    items = [
        {"uid": uid, "username": msg.get(uid, {}).get("username", f"User {uid}"), "seconds": secs}
        for uid, secs in stats.get("voice_time", {}).items()
    ]
    return sorted(items, key=lambda x: x["seconds"], reverse=True)[:limit]

def get_daily_messages(stats: dict, days: int = 14) -> list:
    da = stats.get("daily_active", {})
    result = []
    for i in range(days - 1, -1, -1):
        d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        result.append({"date": d, "count": sum(da.get(d, {}).values())})
    return result

def get_daily_voice(stats: dict, days: int = 14) -> list:
    vd = stats.get("voice_days", {})
    result = []
    for i in range(days - 1, -1, -1):
        d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        total = sum(ud.get(d, 0) for ud in vd.values())
        result.append({"date": d, "seconds": total})
    return result

def get_gambling_leaderboard(stats: dict, limit: int = 10) -> list:
    items = [
        {"uid": k, "username": v.get("username", k),
         "wins": v.get("wins", 0), "losses": v.get("losses", 0),
         "profit": v.get("profit", 0)}
        for k, v in stats.get("gambling", {}).items()
    ]
    return sorted(items, key=lambda x: x["profit"], reverse=True)[:limit]

# ── Datei-Watcher: sendet Events wenn sich dashboard_stats.json ändert ──
_last_mtime = 0.0
_last_stats: dict = {}

def _watch_stats_file():
    """Hintergrund-Thread: Überwacht dashboard_stats.json auf Änderungen."""
    global _last_mtime, _last_stats
    while True:
        try:
            if os.path.exists(_STATS_FILE):
                mtime = os.path.getmtime(_STATS_FILE)
                if mtime != _last_mtime:
                    _last_mtime = mtime
                    new_stats = load_stats()

                    # Neue Deletions?
                    old_del_count = len(_last_stats.get("deletions", []))
                    new_del_count = len(new_stats.get("deletions", []))
                    if new_del_count > old_del_count:
                        for entry in new_stats["deletions"][-(new_del_count - old_del_count):]:
                            ws_broadcast("ai_delete", entry)

                    # Neue Joins?
                    old_join_count = len(_last_stats.get("joins", []))
                    new_join_count = len(new_stats.get("joins", []))
                    if new_join_count > old_join_count:
                        for entry in new_stats["joins"][-(new_join_count - old_join_count):]:
                            ws_broadcast("member_join", entry)

                    # Neue Mod-Aktionen?
                    old_mod_count = len(_last_stats.get("mod_actions", []))
                    new_mod_count = len(new_stats.get("mod_actions", []))
                    if new_mod_count > old_mod_count:
                        for entry in new_stats["mod_actions"][-(new_mod_count - old_mod_count):]:
                            ws_broadcast("twitch_mod", entry)

                    # Nachrichten-Update?
                    old_msg = sum(v.get("total", 0) for v in _last_stats.get("messages", {}).values())
                    new_msg = sum(v.get("total", 0) for v in new_stats.get("messages", {}).values())
                    if new_msg > old_msg:
                        ws_broadcast("message_count", {"total": new_msg})

                    # Voice-Zeit Update?
                    old_vc = sum(_last_stats.get("voice_time", {}).values())
                    new_vc = sum(new_stats.get("voice_time", {}).values())
                    if new_vc != old_vc:
                        ws_broadcast("voice_update", {"total_seconds": new_vc})

                    _last_stats = new_stats

        except Exception as e:
            pass
        time.sleep(1)  # jede Sekunde prüfen

# Watcher im Hintergrund starten
_watcher = threading.Thread(target=_watch_stats_file, daemon=True)
_watcher.start()

# ── Now Playing State (wird von Bot-Seite gesetzt oder aus main.py gelesen) ──
_now_playing: dict | None = None

def set_now_playing(data: dict | None):
    global _now_playing
    _now_playing = data
    ws_broadcast("now_playing", data or {})

def get_now_playing() -> dict | None:
    # Wenn Bot im gleichen Prozess läuft, direkt aus dessen State lesen
    if _bot_state is not None:
        return getattr(_bot_state, "now_playing", None)
    return _now_playing

# ── Redirect URI ────────────────────────────────────────────
def get_redirect_uri() -> str:
    if _REDIRECT_URI_ENV:
        return _REDIRECT_URI_ENV
    return f"http://localhost:{DASHBOARD_PORT}/auth/callback"

# ── Discord API ──────────────────────────────────────────────
def _bh() -> dict:
    return {"Authorization": f"Bot {DISCORD_TOKEN}"}

def _dget(path: str, timeout: int = 5):
    try:
        r = requests.get(f"{DISCORD_API}{path}", headers=_bh(), timeout=timeout)
        return r.json() if r.ok else None
    except Exception:
        return None

def fetch_bot_info() -> dict:
    d = _dget("/users/@me")
    if d:
        av = d.get("avatar")
        return {
            "id": d["id"], "username": d["username"],
            "avatar_url": (
                f"https://cdn.discordapp.com/avatars/{d['id']}/{av}.webp?size=128"
                if av else
                f"https://cdn.discordapp.com/embed/avatars/{int(d.get('discriminator','0'))%5}.png"
            ),
        }
    return {"id": "", "username": "Bot", "avatar_url": ""}

def fetch_guild_info() -> dict:
    if not GUILD_ID: return {}
    d = _dget(f"/guilds/{GUILD_ID}?with_counts=true")
    if d:
        icon = d.get("icon")
        return {
            "id": d["id"], "name": d.get("name", ""),
            "icon_url": f"https://cdn.discordapp.com/icons/{d['id']}/{icon}.webp?size=128" if icon else "",
            "member_count": d.get("approximate_member_count", 0),
            "online_count":  d.get("approximate_presence_count", 0),
        }
    return {}

def fetch_guild_channels() -> list:
    if not GUILD_ID: return []
    return _dget(f"/guilds/{GUILD_ID}/channels") or []

def fetch_guild_members(limit: int = 100) -> list:
    if not GUILD_ID: return []
    return _dget(f"/guilds/{GUILD_ID}/members?limit={limit}") or []

# ── OAuth2 ───────────────────────────────────────────────────
def discord_oauth_url() -> str:
    return (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={quote(get_redirect_uri(), safe='')}"
        "&response_type=code&scope=identify"
    )

def exchange_code(code: str) -> dict:
    r = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={"client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
              "grant_type": "authorization_code", "code": code,
              "redirect_uri": get_redirect_uri()},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f"Token-Fehler: {r.status_code} – {r.text}")
    return r.json()

def get_discord_user(token: str) -> dict:
    r = requests.get(f"{DISCORD_API}/users/@me",
                     headers={"Authorization": f"Bearer {token}"}, timeout=5)
    r.raise_for_status()
    return r.json()

# ── Auth Decorator ───────────────────────────────────────────
def require_admin(f):
    @wraps(f)
    def dec(*a, **kw):
        u = session.get("discord_user")
        if not u: return redirect(url_for("login"))
        if int(u["id"]) not in ADMIN_USER_IDS:
            return render_template("403.html"), 403
        return f(*a, **kw)
    return dec

@app.context_processor
def _ctx():
    return {
        "admin_ids":    list(ADMIN_USER_IDS),
        "redirect_uri": get_redirect_uri(),
        "tunnel_url":   "",
    }

# ── Settings ─────────────────────────────────────────────────
ENV_PATH = _ENV_FILE

EDITABLE_SETTINGS = [
    ("GUILD_ID",               "Guild ID",               "text",   "core"),
    ("ALLOWED_CHANNEL_ID",     "Allowed Channel",        "text",   "core"),
    ("LOG_CHANNEL_ID",         "Log Kanal",              "text",   "core"),
    ("MOD_ROLE_NAME",          "Mod Rolle",              "text",   "core"),
    ("AUTO_ROLE_NAME",         "Auto-Rolle",             "text",   "core"),
    ("ADMIN_USER_IDS",         "Admin User IDs",         "text",   "core"),
    ("TWITCH_LOG_CHANNEL_ID",  "Twitch Log Kanal",       "text",   "twitch"),
    ("STREAMER_CHANNEL",       "Streamer Kanal",         "text",   "twitch"),
    ("STREAMER_USER_ID",       "Streamer User ID",       "text",   "twitch"),
    ("TEMP_VOICE_CHANNEL_ID",  "TempVC Trigger Kanal",   "text",   "voice"),
    ("TEMP_VOICE_CATEGORY_ID", "TempVC Kategorie",       "text",   "voice"),
    ("REVIEW_CHANNEL_ID",      "Review Kanal",           "text",   "review"),
    ("GAMBLING_CHANNEL_ID",    "Gambling Kanal",         "text",   "gambling"),
    ("FISHING_CHANNEL_ID",     "Angel Kanal",            "text",   "gambling"),
    ("CRASH_CHEAT",            "Crash Cheat (Admins)",   "bool",   "gambling"),
    ("MUSIC_ENABLED",          "Musik Modul",            "bool",   "music"),
    ("MUSIC_IDLE_TIMEOUT",     "Musik Idle Timeout (s)", "number", "music"),
    ("AI_MODEL_NAME",          "AI Modell",              "text",   "ai"),
    ("MODERATION_MODEL",       "Moderation Modell",      "text",   "ai"),
    ("PROXYCHECK_API_KEY",     "ProxyCheck API Key",     "text",   "ai"),
    ("IGNORED_CHANNELS",       "Ignorierte Kanäle",      "text",   "ai"),
    ("TICKET_CATEGORY_ID",     "Ticket Kategorie",       "text",   "tickets"),
    ("TICKET_LOG_CHANNEL_ID",  "Ticket Log Kanal",       "text",   "tickets"),
    ("TICKET_SUPPORT_ROLE",    "Support Rolle",          "text",   "tickets"),
]

def load_env_settings() -> dict:
    return {k: os.getenv(k, "") for k, *_ in EDITABLE_SETTINGS}

def save_env_setting(key: str, value: str):
    lines = open(ENV_PATH, "r", encoding="utf-8").readlines() if os.path.exists(ENV_PATH) else []
    found, new = False, []
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
            new.append(f"{key}={value}\n"); found = True
        else:
            new.append(line)
    if not found: new.append(f"{key}={value}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f: f.writelines(new)
    os.environ[key] = value

# ── Favicon ──────────────────────────────────────────────────
@app.route("/favicon.ico")
def favicon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#5865f2"/><path d="M8 12h16M8 16h16M8 20h10" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg>'
    return Response(svg, mimetype="image/svg+xml")

# ══════════════════════════════════════════════════════════════
#  ROUTES – SEITEN
# ══════════════════════════════════════════════════════════════

@app.route("/")
@require_admin
def index():
    stats    = load_stats()
    summary  = get_summary(stats)
    channels = fetch_guild_channels()
    return render_template("index.html",
        bot=fetch_bot_info(),
        guild=fetch_guild_info(),
        vc_list=[c for c in channels if c.get("type") == 2],
        now_playing=get_now_playing(),
        summary=summary,
        recent_deletions=list(reversed(stats.get("deletions", [])))[:10],
        recent_joins=list(reversed(stats.get("joins", [])))[:10],
        recent_mod=list(reversed(stats.get("mod_actions", [])))[:10],
    )

@app.route("/stats")
@require_admin
def stats_page():
    stats = load_stats()
    return render_template("stats.html",
        bot=fetch_bot_info(),
        guild=fetch_guild_info(),
        summary=get_summary(stats),
        msg_leaderboard=get_message_leaderboard(stats, 15),
        voice_leaderboard=get_voice_leaderboard(stats, 15),
        daily_messages=get_daily_messages(stats, 14),
        daily_voice=get_daily_voice(stats, 14),
        gambling_lb=get_gambling_leaderboard(stats, 10),
    )

@app.route("/logs")
@require_admin
def logs_page():
    stats = load_stats()
    return render_template("logs.html",
        bot=fetch_bot_info(),
        guild=fetch_guild_info(),
        deletions=list(reversed(stats.get("deletions", [])))[:50],
        joins=list(reversed(stats.get("joins", [])))[:50],
        mod_actions=list(reversed(stats.get("mod_actions", [])))[:100],
        mod_stats={a: sum(1 for x in stats.get("mod_actions",[]) if x.get("action")==a)
                   for a in set(x.get("action","") for x in stats.get("mod_actions",[]))},
    )

@app.route("/members")
@require_admin
def members_page():
    return render_template("members.html",
        bot=fetch_bot_info(),
        guild=fetch_guild_info(),
        members=fetch_guild_members(100),
    )

@app.route("/music")
@require_admin
def music_page():
    return render_template("music.html",
        bot=fetch_bot_info(),
        guild=fetch_guild_info(),
        now_playing=get_now_playing(),
    )

@app.route("/settings", methods=["GET", "POST"])
@require_admin
def settings_page():
    saved, errors = False, []
    if request.method == "POST":
        for key, label, typ, _sec in EDITABLE_SETTINGS:
            if typ == "bool":
                val = "true" if request.form.get(key + "_check") else "false"
            else:
                val = request.form.get(key, "").strip()
            if typ == "number" and val:
                try: int(val)
                except ValueError: errors.append(f"{label}: Muss eine Zahl sein."); continue
            try: save_env_setting(key, val)
            except Exception as e: errors.append(f"{key}: {e}")
        if not errors:
            saved = True
            ws_broadcast("settings_changed", {})
    return render_template("settings.html",
        bot=fetch_bot_info(), guild=fetch_guild_info(),
        settings=load_env_settings(), editable=EDITABLE_SETTINGS,
        saved=saved, errors=errors,
    )

# ══════════════════════════════════════════════════════════════
#  ROUTES – API
# ══════════════════════════════════════════════════════════════

@app.route("/api/status")
@require_admin
def api_status():
    stats = load_stats()
    return jsonify({
        "bot":         fetch_bot_info(),
        "guild":       fetch_guild_info(),
        "now_playing": get_now_playing(),
        "summary":     get_summary(stats),
        "ts":          datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })

@app.route("/api/stats/messages")
@require_admin
def api_stats_messages():
    stats = load_stats()
    return jsonify({
        "leaderboard": get_message_leaderboard(stats, 15),
        "daily":       get_daily_messages(stats, 14),
    })

@app.route("/api/stats/voice")
@require_admin
def api_stats_voice():
    stats = load_stats()
    return jsonify({
        "leaderboard": get_voice_leaderboard(stats, 15),
        "daily":       get_daily_voice(stats, 14),
    })

@app.route("/api/stats/mod")
@require_admin
def api_stats_mod():
    stats = load_stats()
    return jsonify({
        "actions": list(reversed(stats.get("mod_actions", [])))[:50],
        "stats":   {a: sum(1 for x in stats.get("mod_actions",[]) if x.get("action")==a)
                    for a in set(x.get("action","") for x in stats.get("mod_actions",[]))},
    })

@app.route("/api/logs/deletions")
@require_admin
def api_deletions():
    return jsonify(list(reversed(load_stats().get("deletions", [])))[:30])

@app.route("/api/logs/joins")
@require_admin
def api_joins():
    return jsonify(list(reversed(load_stats().get("joins", [])))[:30])

@app.route("/api/settings", methods=["GET"])
@require_admin
def api_settings_get():
    return jsonify(load_env_settings())

@app.route("/api/settings", methods=["POST"])
@require_admin
def api_settings_post():
    data    = request.get_json(silent=True) or {}
    allowed = {k for k, *_ in EDITABLE_SETTINGS}
    errors  = []
    for key, val in data.items():
        if key not in allowed: errors.append(f"Unbekannt: {key}"); continue
        try: save_env_setting(key, str(val))
        except Exception as e: errors.append(str(e))
    if errors: return jsonify({"ok": False, "errors": errors}), 400
    ws_broadcast("settings_changed", {})
    return jsonify({"ok": True})

@app.route("/api/channels")
@require_admin
def api_channels():
    return jsonify(fetch_guild_channels())

# ── Auth ─────────────────────────────────────────────────────
@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return render_template("login_no_oauth.html")
    return render_template("login.html",
        oauth_url=discord_oauth_url(),
        redirect_uri=get_redirect_uri(),
        tunnel_url="", error_msg=None,
    )

@app.route("/auth/callback")
def auth_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    if error:
        return render_template("login.html",
            oauth_url=discord_oauth_url(), redirect_uri=get_redirect_uri(),
            tunnel_url="", error_msg=f"{error}: {request.args.get('error_description','')}",
        ), 400
    if not code: return redirect(url_for("login"))
    try:
        token_data = exchange_code(code)
        user       = get_discord_user(token_data["access_token"])
        if int(user["id"]) not in ADMIN_USER_IDS:
            return render_template("403.html"), 403
        session["discord_user"] = user
        session["access_token"] = token_data["access_token"]
        return redirect(url_for("index"))
    except Exception as e:
        return render_template("login.html",
            oauth_url=discord_oauth_url(), redirect_uri=get_redirect_uri(),
            tunnel_url="", error_msg=str(e),
        ), 400

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── WebSocket ─────────────────────────────────────────────────
@sock.route("/ws")
def websocket(ws):
    u = session.get("discord_user")
    if not u or int(u["id"]) not in ADMIN_USER_IDS:
        ws.close(); return

    q = ws_subscribe()
    # Sofort aktuellen Status senden
    try:
        stats = load_stats()
        ws.send(json.dumps({
            "type": "initial_state",
            "data": {
                "now_playing": get_now_playing(),
                "summary":     get_summary(stats),
            },
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, default=str))
    except Exception:
        pass

    try:
        while True:
            msg = None
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    msg = q.get_nowait(); break
                except _queue.Empty:
                    time.sleep(0.1)
            ws.send(msg if msg else json.dumps({
                "type": "ping",
                "ts":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }))
    except Exception:
        pass
    finally:
        ws_unsubscribe(q)

# ── Errors ────────────────────────────────────────────────────
@app.errorhandler(403)
def e403(e): return render_template("403.html"), 403
@app.errorhandler(404)
def e404(e): return render_template("404.html"), 404
@app.errorhandler(500)
def e500(e): return f"<h2>500</h2><pre>{e}</pre>", 500

# ── Start ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"╔══════════════════════════════════════════════╗")
    print(f"║  ByGorgii Admin Dashboard                    ║")
    print(f"╠══════════════════════════════════════════════╣")
    print(f"║  Stats-Datei : {'gefunden ✅' if os.path.exists(_STATS_FILE) else 'nicht gefunden (wird erstellt)':<28} ║")
    print(f"║  Admin-IDs   : {str(ADMIN_USER_IDS)[:28]:<28} ║")
    print(f"║  Redirect URI: {get_redirect_uri()[:28]:<28} ║")
    print(f"║  Port        : {DASHBOARD_PORT:<28} ║")
    print(f"╚══════════════════════════════════════════════╝")
    print(f"\n  Dashboard: http://localhost:{DASHBOARD_PORT}\n")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=DASHBOARD_DEBUG, threaded=True)
