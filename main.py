import os
import sys
import shutil
import random
import discord
from discord.ext import commands, tasks
from discord import app_commands
from twitchio.ext import commands as t_commands
from dotenv import load_dotenv
from openai import OpenAI
import asyncio
import json
import time
import datetime
import calendar
import re
import io
import tempfile
import aiohttp
from collections import deque
from urllib.parse import urlparse, quote
from functools import wraps

# Dashboard-Imports (Flask läuft im selben Prozess in einem Thread)
from flask import (Flask as _Flask, render_template as _render,
                   redirect as _redirect, url_for as _url_for,
                   session as _session, request as _request,
                   jsonify as _jsonify, Response as _Response)
import requests as _requests

# ══════════════════════════════════════════════════════════
#   DASHBOARD BRIDGE  (direkt eingebaut – kein separater Import)
# ══════════════════════════════════════════════════════════
import threading
import queue as _queue

# ── WebSocket Broadcast (wird von Flask-Sock genutzt) ──────────
_ws_queues: list = []
_ws_lock = threading.Lock()

def _ws_subscribe() -> _queue.Queue:
    q = _queue.Queue(maxsize=200)
    with _ws_lock:
        _ws_queues.append(q)
    return q

def _ws_unsubscribe(q):
    with _ws_lock:
        try: _ws_queues.remove(q)
        except ValueError: pass

def _ws_broadcast(event_type: str, data: dict):
    payload = json.dumps({
        "type": event_type,
        "data": data,
        "ts":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, default=str)
    dead = []
    with _ws_lock:
        queues = list(_ws_queues)
    for q in queues:
        try: q.put_nowait(payload)
        except _queue.Full: dead.append(q)
    for q in dead: _ws_unsubscribe(q)

_STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_stats.json")
OWNER_RECOVERY_WINDOW = 600  # 10 Minuten


class _StatsTracker:
    """Persistente Statistiken: Nachrichten, Voice-Zeit, Mod-Aktionen, etc."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = self._load()
        self._voice_sessions: dict = {}

    def _load(self) -> dict:
        try:
            with open(_STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "messages": {}, "voice_time": {}, "voice_days": {},
                "daily_active": {}, "gambling": {}, "mod_actions": [],
                "deletions": [], "joins": [],
            }

    def _save(self):
        try:
            with open(_STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Stats] Speicherfehler: {e}")

    def add_message(self, user_id: int, username: str):
        uid = str(user_id)
        today = datetime.date.today().isoformat()
        with self._lock:
            m = self._data["messages"]
            if uid not in m:
                m[uid] = {"total": 0, "username": username, "daily": {}}
            m[uid]["total"] = m[uid].get("total", 0) + 1
            m[uid]["username"] = username
            m[uid]["daily"][today] = m[uid]["daily"].get(today, 0) + 1
            da = self._data["daily_active"]
            if today not in da:
                da[today] = {}
            da[today][uid] = da[today].get(uid, 0) + 1
            self._save()

    def get_message_leaderboard(self, limit: int = 10) -> list:
        with self._lock:
            items = [
                {"uid": k, "username": v.get("username", k), "total": v.get("total", 0)}
                for k, v in self._data["messages"].items()
            ]
        return sorted(items, key=lambda x: x["total"], reverse=True)[:limit]

    def get_daily_messages(self, days: int = 7) -> list:
        with self._lock:
            da = self._data["daily_active"]
        result = []
        for i in range(days - 1, -1, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            result.append({"date": d, "count": sum(da.get(d, {}).values())})
        return result

    def voice_join(self, user_id: int):
        import time
        self._voice_sessions[user_id] = time.time()

    def voice_leave(self, user_id: int, username: str):
        import time
        if user_id not in self._voice_sessions:
            return
        duration = int(time.time() - self._voice_sessions.pop(user_id))
        if duration < 5:
            return
        uid = str(user_id)
        today = datetime.date.today().isoformat()
        with self._lock:
            self._data["voice_time"][uid] = self._data["voice_time"].get(uid, 0) + duration
            if uid not in self._data["voice_days"]:
                self._data["voice_days"][uid] = {}
            self._data["voice_days"][uid][today] = self._data["voice_days"][uid].get(today, 0) + duration
            m = self._data["messages"]
            if uid not in m:
                m[uid] = {"total": 0, "username": username, "daily": {}}
            m[uid]["username"] = username
            self._save()

    def get_voice_leaderboard(self, limit: int = 10) -> list:
        with self._lock:
            vt = self._data["voice_time"]
            msg = self._data["messages"]
        items = [
            {"uid": uid, "username": msg.get(uid, {}).get("username", f"User {uid}"), "seconds": secs}
            for uid, secs in vt.items()
        ]
        return sorted(items, key=lambda x: x["seconds"], reverse=True)[:limit]

    def get_daily_voice(self, days: int = 7) -> list:
        with self._lock:
            vd = self._data["voice_days"]
        result = []
        for i in range(days - 1, -1, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            total = sum(ud.get(d, 0) for ud in vd.values())
            result.append({"date": d, "seconds": total})
        return result

    def add_mod_action(self, action: str, target: str, moderator: str,
                       reason: str = "", duration: int = None) -> dict:
        entry = {
            "action": action, "target": target, "moderator": moderator,
            "reason": reason or "", "duration": duration,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self._lock:
            self._data["mod_actions"].append(entry)
            if len(self._data["mod_actions"]) > 500:
                self._data["mod_actions"] = self._data["mod_actions"][-500:]
            self._save()
        return entry

    def get_mod_actions(self, limit: int = 50) -> list:
        with self._lock:
            return list(reversed(self._data["mod_actions"]))[:limit]

    def get_mod_stats(self) -> dict:
        with self._lock:
            counts: dict = {}
            for a in self._data["mod_actions"]:
                counts[a["action"]] = counts.get(a["action"], 0) + 1
        return counts

    def add_deletion(self, author: str, content: str, reason: str, channel: str) -> dict:
        entry = {
            "author": author, "content": content[:200], "reason": reason[:200],
            "channel": channel, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self._lock:
            self._data["deletions"].append(entry)
            if len(self._data["deletions"]) > 200:
                self._data["deletions"] = self._data["deletions"][-200:]
            self._save()
        return entry

    def get_deletions(self, limit: int = 50) -> list:
        with self._lock:
            return list(reversed(self._data["deletions"]))[:limit]

    def add_join(self, name: str, uid: str, suspicious: bool,
                 age_days: int, avatar: str = "") -> dict:
        entry = {
            "name": name, "id": uid, "suspicious": suspicious,
            "age_days": age_days, "avatar": avatar,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self._lock:
            self._data["joins"].append(entry)
            if len(self._data["joins"]) > 200:
                self._data["joins"] = self._data["joins"][-200:]
            self._save()
        return entry

    def get_joins(self, limit: int = 50) -> list:
        with self._lock:
            return list(reversed(self._data["joins"]))[:limit]

    def add_gambling_result(self, user_id: int, username: str, won: bool, amount: int):
        uid = str(user_id)
        with self._lock:
            g = self._data["gambling"]
            if uid not in g:
                g[uid] = {"username": username, "wins": 0, "losses": 0, "profit": 0}
            g[uid]["username"] = username
            if won:
                g[uid]["wins"] += 1; g[uid]["profit"] += amount
            else:
                g[uid]["losses"] += 1; g[uid]["profit"] -= amount
            self._save()

    def get_gambling_leaderboard(self, limit: int = 10) -> list:
        with self._lock:
            items = [
                {"uid": k, "username": v.get("username", k),
                 "wins": v.get("wins", 0), "losses": v.get("losses", 0),
                 "profit": v.get("profit", 0)}
                for k, v in self._data["gambling"].items()
            ]
        return sorted(items, key=lambda x: x["profit"], reverse=True)[:limit]

    def get_summary(self) -> dict:
        with self._lock:
            return {
                "total_messages":    sum(v.get("total", 0) for v in self._data["messages"].values()),
                "total_voice_secs":  sum(self._data["voice_time"].values()),
                "total_mod_actions": len(self._data["mod_actions"]),
                "total_deletions":   len(self._data["deletions"]),
                "total_joins":       len(self._data["joins"]),
            }


class _DashboardState:
    """Live-Daten für WebSocket-Clients (Dashboard)."""

    def __init__(self):
        self.now_playing: dict | None = None
        self.temp_vcs:    dict        = {}
        self.active_games: dict       = {}

    def push_event(self, event_type: str, data: dict):
        # Direkt an alle WebSocket-Clients senden (Thread-safe)
        _ws_broadcast(event_type, data)

    def push_event_sync(self, event_type: str, data: dict):
        # Alias – funktioniert aus jedem Thread
        _ws_broadcast(event_type, data)

    def update_now_playing(self, song_info: dict | None):
        self.now_playing = song_info
        self.push_event("now_playing", song_info or {})

    def update_temp_vcs(self, tvc: dict):
        result = {}
        for ch_id, data in tvc.items():
            result[str(ch_id)] = {
                "owner_id":        data.get("owner_id"),
                "locked":          data.get("locked", False),
                "limit":           data.get("limit", 0),
                "text_channel_id": data.get("text_channel_id"),
            }
        self.temp_vcs = result
        self.push_event("temp_vcs_update", result)

    def add_deletion(self, author: str, content: str, reason: str, channel: str):
        entry = stats_tracker.add_deletion(author, content, reason, channel)
        self.push_event("ai_delete", entry)

    def add_join(self, name: str, uid: str, suspicious: bool,
                 age_days: int, avatar: str = ""):
        entry = stats_tracker.add_join(name, uid, suspicious, age_days, avatar)
        self.push_event("member_join", entry)

    def add_mod_action(self, action: str, target: str, moderator: str,
                       reason: str = "", duration: int = None):
        entry = stats_tracker.add_mod_action(action, target, moderator, reason, duration)
        self.push_event("twitch_mod", entry)

    def add_message(self, user_id: int, username: str):
        stats_tracker.add_message(user_id, username)
        self.push_event("message_count", {"uid": str(user_id), "username": username})

    def voice_join(self, user_id: int, username: str, channel_name: str):
        stats_tracker.voice_join(user_id)
        self.push_event("vc_join", {
            "uid": str(user_id), "username": username, "channel_name": channel_name,
        })

    def voice_leave(self, user_id: int, username: str, channel_name: str):
        stats_tracker.voice_leave(user_id, username)
        self.push_event("vc_leave", {
            "uid": str(user_id), "username": username, "channel_name": channel_name,
        })


# Owner-Recovery Tracking
_owner_recovery: dict[int, dict] = {}


def is_only_voice_state_change(before, after) -> bool:
    """True wenn nur Mute/Deaf/Stream geändert, KEIN Kanalwechsel."""
    if before.channel is None or after.channel is None:
        return False
    if before.channel.id != after.channel.id:
        return False
    return (
        before.self_mute   != after.self_mute   or
        before.self_deaf   != after.self_deaf   or
        before.mute        != after.mute        or
        before.deaf        != after.deaf        or
        before.self_stream != after.self_stream or
        before.self_video  != after.self_video
    )


def record_owner_left(channel_id: int, owner_id: int):
    _owner_recovery[channel_id] = {
        "original_owner_id": owner_id,
        "left_at": datetime.datetime.now(datetime.timezone.utc),
    }


def check_owner_recovery(channel_id: int, member_id: int) -> bool:
    rec = _owner_recovery.get(channel_id)
    if not rec or rec["original_owner_id"] != member_id:
        return False
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - rec["left_at"]).total_seconds()
    if elapsed <= OWNER_RECOVERY_WINDOW:
        return True
    _owner_recovery.pop(channel_id, None)
    return False


def clear_owner_recovery(channel_id: int):
    _owner_recovery.pop(channel_id, None)


# Instanzen erstellen
stats_tracker   = _StatsTracker()
dashboard_state = _DashboardState()
_dashboard_enabled = True
print("✅ [Dashboard] Bridge initialisiert")


# ══════════════════════════════════════════════════════════
#                 UMGEBUNGSVARIABLEN LADEN
# ══════════════════════════════════════════════════════════

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ALLOWED_CHANNEL_ID = int(os.getenv("ALLOWED_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TWITCH_LOG_CHANNEL_ID = int(os.getenv("TWITCH_LOG_CHANNEL_ID", "0"))
MOD_ROLE_NAME = os.getenv("MOD_ROLE_NAME", "Moderator")


# Temp Voice Chat
TEMP_VOICE_CHANNEL_ID = int(os.getenv("TEMP_VOICE_CHANNEL_ID", "0"))
TEMP_VOICE_CATEGORY_ID = int(os.getenv("TEMP_VOICE_CATEGORY_ID", "0"))

# Review
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", os.getenv("REVIEW_CHANNEL_ID", "0")))
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = set()
if ADMIN_USER_IDS_RAW.strip():
    try:
        ADMIN_USER_IDS = set(
            int(uid.strip())
            for uid in ADMIN_USER_IDS_RAW.split(",")
            if uid.strip()
        )
    except ValueError:
        print("⚠️ Warnung: ADMIN_USER_IDS enthält ungültige Werte!")

### MUSIC MODULE START ###
MUSIC_ENABLED = os.getenv("MUSIC_ENABLED", "true").lower() in ("true", "1", "yes")
MUSIC_IDLE_TIMEOUT = int(os.getenv("MUSIC_IDLE_TIMEOUT", "180"))
### MUSIC MODULE END ###

# Twitch
TWITCH_TOKEN = os.getenv("TWITCH_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BOT_ID = os.getenv("TWITCH_BOT_ID")
STREAMER_CHANNEL = os.getenv("STREAMER_CHANNEL", "").lower()
STREAMER_USER_ID = os.getenv("STREAMER_USER_ID", "")
STREAMER_OAUTH_TOKEN = os.getenv("STREAMER_OAUTH_TOKEN", "").replace("oauth:", "")

# OpenRouter / AI – Key-Rotation
OPENROUTER_KEYS_RAW = os.getenv(
    "OPENROUTER_KEYS",
    os.getenv("OPENROUTER_API_KEY", "")
)
OPENROUTER_KEYS = [
    k.strip() for k in OPENROUTER_KEYS_RAW.split(",") if k.strip()
]
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
AI_MODEL_NAME = os.getenv("AI_MODEL_NAME", "openai/gpt-4o-mini")
MODERATION_MODEL = os.getenv("MODERATION_MODEL", "openai/gpt-4o-mini")

# ProxyCheck.io
PROXYCHECK_API_KEY = os.getenv("PROXYCHECK_API_KEY", "")

# Dateien
DATA_FILE = os.getenv("DATA_FILE", "data.json")
GAMBLING_BANK_FILE = os.getenv("GAMBLING_BANK_FILE", "bank.json")
# Gambling Kanal
GAMBLING_CHANNEL_ID = int(os.getenv("GAMBLING_CHANNEL_ID", "0"))
CRASH_CHEAT = os.getenv("CRASH_CHEAT", "false").lower() in ("true", "1", "yes")
# Angel Kanal
FISHING_CHANNEL_ID = int(os.getenv("FISHING_CHANNEL_ID", "0"))
# Spotify
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# Ticket System
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0"))
TICKET_SUPPORT_ROLE = os.getenv("TICKET_SUPPORT_ROLE", "Support")

# UserInfo PDF-Kanal
USERINFO_CHANNEL_ID = int(os.getenv("USERINFO_CHANNEL_ID", "0"))

# Clips-Kanal (neue Twitch-Clips werden hier gepostet)
CLIPS_CHANNEL_ID = int(os.getenv("CLIPS_CHANNEL_ID", "0"))

# Verifier-System
VERIFIED_ROLE_ID   = int(os.getenv("VERIFIED_ROLE_ID", "0"))
VERIFIER_URL       = os.getenv("VERIFIER_URL", "")
VERIFIER_WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Message-Log-Kanal (bearbeitete & gelöschte Nachrichten)
MESSAGE_LOG_CHANNEL_ID = int(os.getenv("MESSAGE_LOG_CHANNEL_ID", "0"))

# Auto-Rolle
AUTO_ROLE_NAME = os.getenv("AUTO_ROLE_NAME", "Zuschauer")



# ══════════════════════════════════════════════════════════
#                      KONSTANTEN
# ══════════════════════════════════════════════════════════

COLOR_SUCCESS = 0x2ecc71
COLOR_ERROR = 0xe74c3c
COLOR_WARNING = 0xf39c12
COLOR_GOLD = 0xf1c40f
COLOR_INFO = 0x3498db
COLOR_TWITCH = 0x9146FF
COLOR_AI = 0x7289da
COLOR_MOD_BAN = 0xFF0000
COLOR_MOD_TIMEOUT = 0xFF6600
COLOR_MOD_WARN = 0xFFCC00
COLOR_MOD_DELETE = 0xFF4444
COLOR_MOD_UNBAN = 0x00FF88
COLOR_MOD_UNTIMEOUT = 0x00CCFF
COLOR_VOICE = 0x5865F2
COLOR_WEEKLY = 0x9b59b6
COLOR_TICKET = 0x2980b9
COLOR_RR = 0x8B0000
COLOR_BATTLE = 0xFF4500
COLOR_SPOTIFY = 0x1DB954
COLOR_ABSENCE = 0x95A5A6
COLOR_RADIO = 0x1ABC9C
COLOR_QUIZ = 0xE91E63
COLOR_CONNECT4 = 0xFFC300
COLOR_TICTACTOE = 0x00BCD4

# 4 Gewinnt
C4_ROWS = 6
C4_COLS = 7
C4_EMPTY = "⚫"
C4_RED = "🔴"
C4_YELLOW = "🟡"

# Tic Tac Toe
TTT_EMPTY = "⬜"
TTT_X = "❌"
TTT_O = "⭕"

# Radio-Stationen
RADIO_STATIONS = {
    "lofi": {
        "name": "🎵 Lofi Hip Hop",
        "url": "https://play.streamafrica.net/lofiradio",
        "emoji": "🎵",
        "description": "Chillige Lofi Beats zum Entspannen",
    },
    "jazz": {
        "name": "🎷 Jazz Radio",
        "url": "https://streaming.radio.co/s774887f7b/listen",
        "emoji": "🎷",
        "description": "Smooth Jazz & Blues",
    },
    "rock": {
        "name": "🎸 Classic Rock",
        "url": "https://stream.zeno.fm/fyn8eh3h5f8uv",
        "emoji": "🎸",
        "description": "Die besten Rock-Klassiker",
    },
    "pop": {
        "name": "🎤 Pop Hits",
        "url": "https://stream.zeno.fm/0r0xa792kwzuv",
        "emoji": "🎤",
        "description": "Aktuelle Pop-Hits",
    },
    "electronic": {
        "name": "🎧 Electronic",
        "url": "https://stream.zeno.fm/4d6byke4re8uv",
        "emoji": "🎧",
        "description": "Electronic & Dance Music",
    },
    "classical": {
        "name": "🎻 Klassik",
        "url": "https://stream.zeno.fm/ezh22mn9x18uv",
        "emoji": "🎻",
        "description": "Klassische Musik",
    },
    "hiphop": {
        "name": "🎤 Hip Hop",
        "url": "https://stream.zeno.fm/f3wvbbqmdg8uv",
        "emoji": "🔥",
        "description": "Hip Hop & Rap",
    },
    "chill": {
        "name": "🌊 Chill Vibes",
        "url": "https://stream.zeno.fm/ngk2a5b2p18uv",
        "emoji": "🌊",
        "description": "Entspannte Chill-Musik",
    },
}

# Song Quiz
SONG_QUIZ_DURATION = 20
SONG_QUIZ_GUESS_DURATION = 60
SONG_QUIZ_POINTS = {
    "title": 50,
    "artist": 30,
    "both": 100,
}

# Russisches Roulette
RR_MIN_PLAYERS = 2
RR_MAX_PLAYERS = 6
RR_JOIN_TIMEOUT = 30
RR_CHAMBERS = 6

# ══════════════════════════════════════════════════════════
#         SONG QUIZ – DATEN
# ══════════════════════════════════════════════════════════

# { channel_id: { ... } }
active_quizzes: dict[int, dict] = {}

QUIZ_SONGS = [
    {"title": "Never Gonna Give You Up", "artist": "Rick Astley"},
    {"title": "Bohemian Rhapsody", "artist": "Queen"},
    {"title": "Smells Like Teen Spirit", "artist": "Nirvana"},
    {"title": "Billie Jean", "artist": "Michael Jackson"},
    {"title": "Sweet Child O Mine", "artist": "Guns N Roses"},
    {"title": "Stairway to Heaven", "artist": "Led Zeppelin"},
    {"title": "Hotel California", "artist": "Eagles"},
    {"title": "Imagine", "artist": "John Lennon"},
    {"title": "Wonderwall", "artist": "Oasis"},
    {"title": "Lose Yourself", "artist": "Eminem"},
    {"title": "Shape of You", "artist": "Ed Sheeran"},
    {"title": "Blinding Lights", "artist": "The Weeknd"},
    {"title": "Bad Guy", "artist": "Billie Eilish"},
    {"title": "Uptown Funk", "artist": "Bruno Mars"},
    {"title": "Rolling in the Deep", "artist": "Adele"},
    {"title": "Despacito", "artist": "Luis Fonsi"},
    {"title": "Old Town Road", "artist": "Lil Nas X"},
    {"title": "Gangnam Style", "artist": "PSY"},
    {"title": "Somebody That I Used To Know", "artist": "Gotye"},
    {"title": "Take On Me", "artist": "a-ha"},
    {"title": "Africa", "artist": "Toto"},
    {"title": "Eye of the Tiger", "artist": "Survivor"},
    {"title": "Thunderstruck", "artist": "AC DC"},
    {"title": "Mr Brightside", "artist": "The Killers"},
    {"title": "Stressed Out", "artist": "Twenty One Pilots"},
    {"title": "Radioactive", "artist": "Imagine Dragons"},
    {"title": "Counting Stars", "artist": "OneRepublic"},
    {"title": "Viva la Vida", "artist": "Coldplay"},
    {"title": "Havana", "artist": "Camila Cabello"},
    {"title": "Rockstar", "artist": "Post Malone"},
    {"title": "Sicko Mode", "artist": "Travis Scott"},
    {"title": "Sunflower", "artist": "Post Malone"},
    {"title": "Lucid Dreams", "artist": "Juice WRLD"},
    {"title": "Happier", "artist": "Marshmello"},
    {"title": "Shallow", "artist": "Lady Gaga"},
    {"title": "Dance Monkey", "artist": "Tones And I"},
    {"title": "Señorita", "artist": "Shawn Mendes"},
    {"title": "Circles", "artist": "Post Malone"},
    {"title": "Watermelon Sugar", "artist": "Harry Styles"},
    {"title": "Levitating", "artist": "Dua Lipa"},
    {"title": "drivers license", "artist": "Olivia Rodrigo"},
    {"title": "Stay", "artist": "The Kid LAROI"},
    {"title": "Heat Waves", "artist": "Glass Animals"},
    {"title": "As It Was", "artist": "Harry Styles"},
    {"title": "Anti Hero", "artist": "Taylor Swift"},
    {"title": "Flowers", "artist": "Miley Cyrus"},
    {"title": "Calm Down", "artist": "Rema"},
    {"title": "Cruel Summer", "artist": "Taylor Swift"},
    {"title": "Paint The Town Red", "artist": "Doja Cat"},
    {"title": "Greedy", "artist": "Tate McRae"},
]

# Musik Battle
BATTLE_LISTEN_SECONDS = 30
BATTLE_VOTE_SECONDS = 20

### MUSIC MODULE START ###
COLOR_MUSIC = 0xFF1DB8
QUEUE_PAGE_SIZE = 10
### MUSIC MODULE END ###

AI_FOOTER = (
    "\n\n-# 🛈 Ich bin eine KI und kann Fehler machen. "
    "Bitte überprüfe wichtige Informationen."
)


FLAG_TO_LANGUAGE = {
    "🇩🇪": "Deutsch", "🇺🇸": "Englisch", "🇬🇧": "Englisch",
    "🇫🇷": "Französisch", "🇪🇸": "Spanisch", "🇮🇹": "Italienisch",
    "🇵🇹": "Portugiesisch", "🇷🇺": "Russisch", "🇯🇵": "Japanisch",
    "🇨🇳": "Chinesisch", "🇰🇷": "Koreanisch", "🇳🇱": "Niederländisch",
    "🇵🇱": "Polnisch", "🇹🇷": "Türkisch", "🇸🇪": "Schwedisch",
    "🇳🇴": "Norwegisch", "🇩🇰": "Dänisch", "🇫🇮": "Finnisch",
    "🇬🇷": "Griechisch", "🇨🇿": "Tschechisch", "🇭🇺": "Ungarisch",
    "🇷🇴": "Rumänisch", "🇧🇬": "Bulgarisch", "🇭🇷": "Kroatisch",
    "🇸🇰": "Slowakisch", "🇸🇮": "Slowenisch", "🇦🇪": "Arabisch",
    "🇸🇦": "Arabisch", "🇮🇱": "Hebräisch", "🇮🇳": "Hindi",
    "🇹🇭": "Thai", "🇻🇳": "Vietnamesisch", "🇮🇩": "Indonesisch",
}

MONTH_NAMES = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember"
]

WEEKDAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# ══════════════════════════════════════════════════════════
#          FEATURE: INTELLIGENTE API-KEY-ROTATION
# ══════════════════════════════════════════════════════════



class KeyRotator:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.current_index = 0
        self.last_reset = datetime.datetime.now(datetime.timezone.utc)
        self._lock = asyncio.Lock()
        print(f"🔑 KeyRotator initialisiert mit {len(self.keys)} Key(s).")

    def _build_client(self, index: int) -> OpenAI:
        return OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=self.keys[index],
        )

    async def get_client(self) -> OpenAI:
        async with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            if (now - self.last_reset) >= datetime.timedelta(hours=24):
                print("🔄 24h vergangen – starte wieder bei Key 0.")
                self.current_index = 0
                self.last_reset = now
            return self._build_client(self.current_index)

    async def rotate(self):
        async with self._lock:
            old_index = self.current_index
            self.current_index = (self.current_index + 1) % len(self.keys)
            print(
                f"⚠️ Rate-Limit! Wechsel von Key {old_index} "
                f"zu Key {self.current_index}."
            )

    async def call_with_rotation(self, call_fn):
        attempts = len(self.keys)
        last_error = None
        for attempt in range(attempts):
            client = await self.get_client()
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, lambda c=client: call_fn(c)
                )
                return result
            except Exception as e:
                error_str = str(e).lower()
                if (
                    "429" in error_str
                    or "rate limit" in error_str
                    or "rate_limit" in error_str
                ):
                    print(
                        f"Rate-Limit bei Key {self.current_index} "
                        f"(Versuch {attempt + 1}/{attempts})"
                    )
                    last_error = e
                    await self.rotate()
                    await asyncio.sleep(1)
                else:
                    raise e
        print(f"❌ Alle {attempts} Keys haben Rate-Limit erreicht!")
        if last_error:
            raise last_error
        raise RuntimeError("Alle API-Keys erschöpft.")


key_rotator = None
ai_enabled = False

if OPENROUTER_KEYS:
    key_rotator = KeyRotator(OPENROUTER_KEYS)
    ai_enabled = True
else:
    print("⚠️ Keine OpenRouter API Keys gefunden – AI deaktiviert.")


# ══════════════════════════════════════════════════════════
#                   DATENMANAGEMENT
# ══════════════════════════════════════════════════════════

def load_data() -> dict:
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("users", {})
    data.setdefault("streams", {})
    return data


def save_data(data: dict) -> None:
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Fehler beim Speichern: {e}")


# ══════════════════════════════════════════════════════════
#         TEMP VOICE CHAT – DATENVERWALTUNG (RAM)
# ══════════════════════════════════════════════════════════

# { channel_id: { "owner_id": int, "text_channel_id": int,
#                 "control_message_id": int,
#                 "allowed_users": set,
#                 "locked": bool, "limit": int } }
temp_voice_channels: dict[int, dict] = {}

# ══════════════════════════════════════════════════════════
#         TICKET SYSTEM – DATENVERWALTUNG (RAM)
# ══════════════════════════════════════════════════════════

# { channel_id: { "user_id": int, "ticket_id": int,
#                 "status": str, "created_at": str,
#                 "claimed_by": int | None } }
open_tickets: dict[int, dict] = {}
ticket_counter: int = 0

TICKET_FILE = "tickets.json"


def load_tickets() -> dict:
    try:
        with open(TICKET_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"counter": 0, "tickets": {}}


def save_tickets(data: dict) -> None:
    try:
        with open(TICKET_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Ticket Speicherfehler: {e}")


def persist_tickets() -> None:
    """Synchronisiert open_tickets (RAM) komplett mit tickets.json."""
    data = load_tickets()
    data["tickets"] = {str(k): dict(v) for k, v in open_tickets.items()}
    save_tickets(data)


def get_next_ticket_id() -> int:
    data = load_tickets()
    data["counter"] = data.get("counter", 0) + 1
    save_tickets(data)
    return data["counter"]

# ══════════════════════════════════════════════════════════
#         ABWESENHEITS-SYSTEM – DATENVERWALTUNG
# ══════════════════════════════════════════════════════════

ABSENCE_FILE = "absences.json"


def load_absences() -> dict:
    try:
        with open(ABSENCE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"absences": {}}


def save_absences(data: dict) -> None:
    try:
        with open(ABSENCE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Absence Speicherfehler: {e}")


# ══════════════════════════════════════════════════════════
#         RUSSISCHES ROULETTE – DATEN
# ══════════════════════════════════════════════════════════

# { channel_id: { "host": int, "players": list, "einsatz": int, "started": bool } }
active_rr_games: dict[int, dict] = {}

# ══════════════════════════════════════════════════════════
#         MUSIK BATTLE – DATEN
# ══════════════════════════════════════════════════════════

# { channel_id: { ... } }
active_battles: dict[int, dict] = {}

# ══════════════════════════════════════════════════════════
#            SLASH COMMANDS: GAMBLING SYSTEM
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#                    DISCORD BOT
# ══════════════════════════════════════════════════════════

class DiscordBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Persistente Views registrieren (Buttons überleben Neustarts)
        # - Ticket-Panel-Button (Ticket erstellen)
        self.add_view(TicketOpenView())
        # - Fallback für Temp-Voice-Panel-Buttons ohne Message-Zuordnung
        #   (die echten Views werden pro Kanal in on_ready neu registriert)
        self.add_view(VoiceChannelControlView(0, 0))
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print("✅ Discord: Slash-Commands synchronisiert")


# ══════════════════════════════════════════════════════════
#                DECORATORS / CHECKS
# ══════════════════════════════════════════════════════════

def is_allowed_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if (
            ALLOWED_CHANNEL_ID
            and interaction.channel_id != ALLOWED_CHANNEL_ID
        ):
            await interaction.response.send_message(
                f"❌ Nur in <#{ALLOWED_CHANNEL_ID}> verfügbar.",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


def is_admin_user():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(
                "❌ Du hast keine Berechtigung für diesen Befehl.",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


def is_gambling_channel():
    """Check ob der Befehl im Gambling-Kanal ausgeführt wird."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if GAMBLING_CHANNEL_ID and interaction.channel_id != GAMBLING_CHANNEL_ID:
            await interaction.response.send_message(
                f"❌ Gambling-Befehle nur in <#{GAMBLING_CHANNEL_ID}> erlaubt!",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)

def is_fishing_channel():
    """Check ob der Befehl im Angel-Kanal ausgeführt wird."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if FISHING_CHANNEL_ID and interaction.channel_id != FISHING_CHANNEL_ID:
            await interaction.response.send_message(
                f"❌ Angel-Befehle nur in <#{FISHING_CHANNEL_ID}> erlaubt!",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


d_bot = DiscordBot()

@d_bot.tree.command(name="balance", description="💰 Zeigt deinen aktuellen Kontostand")
@is_gambling_channel()
async def cmd_balance(interaction: discord.Interaction):
    current = get_balance(interaction.user.id)
    embed = discord.Embed(
        title="💰 Kontostand",
        description=f"Du hast **{current:,} 🪙**",
        color=COLOR_GOLD,
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(name="a", description="💰 Hole dir deine täglichen Coins ab")
@is_gambling_channel()
async def cmd_daily(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    bank = load_bank()
    now = datetime.datetime.now(datetime.timezone.utc)

    last_daily_str = bank.get(f"{uid}_daily", None)
    if last_daily_str:
        last_daily = datetime.datetime.fromisoformat(last_daily_str)
        diff = now - last_daily
        if diff.total_seconds() < 86400:
            remaining_seconds = 86400 - int(diff.total_seconds())
            next_claim = now + datetime.timedelta(seconds=remaining_seconds)
            next_timestamp = int(next_claim.timestamp())
            embed = discord.Embed(
                title="⏰ Daily bereits abgeholt",
                description=(
                    f"Du hast deine täglichen Coins bereits abgeholt!\n\n"
                    f"Nächster Claim: <t:{next_timestamp}:R>"
                ),
                color=COLOR_ERROR,
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

    streak = bank.get(f"{uid}_daily_streak", 0)
    if last_daily_str:
        last_daily = datetime.datetime.fromisoformat(last_daily_str)
        diff = now - last_daily
        if diff.total_seconds() < 172800:
            streak += 1
        else:
            streak = 1
    else:
        streak = 1

    base_reward = 500
    streak_bonus = min(streak * 10, 100)
    total_reward = base_reward + streak_bonus

    bank.setdefault(uid, 1000)
    bank[uid] = bank.get(uid, 1000) + total_reward
    bank[f"{uid}_daily"] = now.isoformat()
    bank[f"{uid}_daily_streak"] = streak

    week_bonus = 0
    week_bonus_text = ""
    if streak > 0 and streak % 7 == 0:
        week_bonus = 250
        bank[uid] = bank.get(uid, 1000) + week_bonus
        week_bonus_text = f"\n🎁 **7-Tage Streak Bonus:** +{week_bonus} 🪙"

    save_bank(bank)

    embed = discord.Embed(
        title="💰 Daily abgeholt!",
        description=(
            f"**Basis:** +{base_reward} 🪙\n"
            f"**Streak-Bonus:** +{streak_bonus} 🪙\n"
            f"**Gesamt:** +{total_reward} 🪙{week_bonus_text}\n\n"
            f"🔥 **Streak:** {streak} Tag{'e' if streak != 1 else ''}\n"
            f"💰 **Kontostand:** {bank.get(uid, 1000):,} 🪙"
        ),
        color=COLOR_SUCCESS,
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="Nächster Claim in 24 Stunden")

    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="bet", description="🎰 Spiele an der Slot-Maschine")
@app_commands.describe(einsatz="Anzahl der Coins die du setzen willst")
@is_gambling_channel()
async def cmd_bet(interaction: discord.Interaction, einsatz: int):
    user_id = interaction.user.id
    balance = get_balance(user_id)

    if einsatz <= 0:
        return await interaction.response.send_message(
            "❌ Du musst mindestens 1 Coin setzen!",
            ephemeral=True,
        )

    if einsatz > balance:
        return await interaction.response.send_message(
            f"❌ Du hast nicht genug Coins!\n💰 Kontostand: **{balance:,} 🪙**",
            ephemeral=True,
        )

    final_result = [random.choice(SLOT_EMOJIS) for _ in range(3)]
    display = ["🌀", "🌀", "🌀"]

    embed = discord.Embed(
        title="🎰 Die Walzen drehen sich...",
        description=f"# {display[0]} | {display[1]} | {display[2]}",
        color=COLOR_INFO,
    )
    embed.set_footer(text=f"Einsatz: {einsatz:,} 🪙")
    await interaction.response.send_message(embed=embed)

    for i in range(3):
        for _ in range(3):
            await asyncio.sleep(0.25)
            display[i] = random.choice(SLOT_EMOJIS)
            embed.description = f"# {display[0]} | {display[1]} | {display[2]}"
            await interaction.edit_original_response(embed=embed)

        display[i] = final_result[i]
        embed.description = f"# {display[0]} | {display[1]} | {display[2]}"
        await interaction.edit_original_response(embed=embed)

    if final_result[0] == final_result[1] == final_result[2]:
        if final_result[0] == "7️⃣":
            win = einsatz * 50
            status = f"🔥🔥🔥 **MEGA JACKPOT** 🔥🔥🔥\nDu gewinnst **{win:,} 🪙**!"
        elif final_result[0] == "💎":
            win = einsatz * 25
            status = f"💎💎💎 **DIAMANT JACKPOT** 💎💎💎\nDu gewinnst **{win:,} 🪙**!"
        else:
            win = einsatz * 10
            status = f"🎉 **JACKPOT!** 🎉\nDu gewinnst **{win:,} 🪙**!"
        color = COLOR_GOLD
    elif len(set(final_result)) == 2:
        win = einsatz * 2
        status = f"✨ **Gewinn!**\nDu erhältst **{win:,} 🪙**!"
        color = COLOR_SUCCESS
    else:
        win = -einsatz
        status = f"💀 **Verloren!**\nDu verlierst **{abs(win):,} 🪙**."
        color = COLOR_ERROR

    new_balance = update_balance(user_id, win)

    embed.title = "🎰 Slot-Ergebnis"
    embed.color = color
    embed.description = f"# {display[0]} | {display[1]} | {display[2]}\n\n{status}"
    embed.set_footer(text=f"Kontostand: {new_balance:,} 🪙")

    await interaction.edit_original_response(embed=embed)


@d_bot.tree.command(name="pay", description="💸 Überweise Coins an einen anderen User")
@app_commands.describe(
    member="Der User der die Coins bekommen soll",
    betrag="Anzahl der Coins die du überweisen willst",
)
@is_gambling_channel()
async def cmd_pay(interaction: discord.Interaction, member: discord.Member, betrag: int):
    sender_id = interaction.user.id
    receiver_id = member.id

    if sender_id == receiver_id:
        return await interaction.response.send_message(
            "❌ Du kannst dir nicht selbst Coins überweisen!",
            ephemeral=True,
        )

    if member.bot:
        return await interaction.response.send_message(
            "❌ Du kannst Bots keine Coins überweisen!",
            ephemeral=True,
        )

    if betrag <= 0:
        return await interaction.response.send_message(
            "❌ Der Betrag muss mindestens 1 Coin sein!",
            ephemeral=True,
        )

    sender_balance = get_balance(sender_id)
    if betrag > sender_balance:
        return await interaction.response.send_message(
            f"❌ Du hast nicht genug Coins!\n💰 Kontostand: **{sender_balance:,} 🪙**",
            ephemeral=True,
        )

    update_balance(sender_id, -betrag)
    new_receiver_balance = update_balance(receiver_id, betrag)
    new_sender_balance = get_balance(sender_id)

    embed = discord.Embed(
        title="💸 Überweisung erfolgreich!",
        color=COLOR_SUCCESS,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="📤 Von", value=interaction.user.mention, inline=True)
    embed.add_field(name="📥 An", value=member.mention, inline=True)
    embed.add_field(name="💰 Betrag", value=f"**{betrag:,} 🪙**", inline=True)
    embed.add_field(name="📊 Dein Kontostand", value=f"**{new_sender_balance:,} 🪙**", inline=True)
    embed.add_field(name=f"📊 {member.display_name}", value=f"**{new_receiver_balance:,} 🪙**", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)

    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="give", description="💰 Admin: Coins an User vergeben oder abziehen")
@app_commands.describe(
    member="Der User der Coins bekommen oder verlieren soll",
    betrag="Anzahl der Coins (kann negativ sein)",
)
@is_gambling_channel()
@is_admin_user()
async def cmd_give(interaction: discord.Interaction, member: discord.Member, betrag: int):
    new_balance = update_balance(member.id, betrag)

    if betrag >= 0:
        embed = discord.Embed(
            title="💰 Coins vergeben",
            description=f"**{betrag:,} 🪙** wurden {member.mention} gutgeschrieben.",
            color=COLOR_SUCCESS,
        )
    else:
        embed = discord.Embed(
            title="💸 Coins abgezogen",
            description=f"**{abs(betrag):,} 🪙** wurden {member.mention} abgezogen.",
            color=COLOR_ERROR,
        )

    embed.add_field(name="💰 Neuer Kontostand", value=f"**{new_balance:,} 🪙**", inline=True)
    embed.set_footer(text=f"Admin: {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="setbalance", description="💰 Admin: Setzt den Kontostand eines Users")
@app_commands.describe(
    member="Der User dessen Kontostand gesetzt werden soll",
    betrag="Der neue Kontostand",
)
@is_gambling_channel()
@is_admin_user()
async def cmd_setbalance(interaction: discord.Interaction, member: discord.Member, betrag: int):
    if betrag < 0:
        return await interaction.response.send_message(
            "❌ Der Kontostand kann nicht negativ sein!",
            ephemeral=True,
        )

    new_balance = set_balance(member.id, betrag)

    embed = discord.Embed(
        title="💰 Kontostand gesetzt",
        description=f"Der Kontostand von {member.mention} wurde auf **{new_balance:,} 🪙** gesetzt.",
        color=COLOR_INFO,
    )
    embed.set_footer(text=f"Admin: {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="coinflip", description="🪙 Kopf oder Zahl")
@app_commands.describe(
    wahl="Deine Wahl",
    einsatz="Anzahl der Coins die du setzen willst",
)
@app_commands.choices(wahl=[
    app_commands.Choice(name="Kopf", value="kopf"),
    app_commands.Choice(name="Zahl", value="zahl"),
])
@is_gambling_channel()
async def cmd_coinflip(interaction: discord.Interaction, wahl: str, einsatz: int):
    user_id = interaction.user.id
    balance = get_balance(user_id)

    if einsatz <= 0:
        return await interaction.response.send_message(
            "❌ Du musst mindestens 1 Coin setzen!",
            ephemeral=True,
        )

    if einsatz > balance:
        return await interaction.response.send_message(
            f"❌ Du hast nicht genug Coins!\n💰 Kontostand: **{balance:,} 🪙**",
            ephemeral=True,
        )

    result = random.choice(["kopf", "zahl"])
    result_display = "👑 Kopf" if result == "kopf" else "🔢 Zahl"
    wahl_display = "👑 Kopf" if wahl == "kopf" else "🔢 Zahl"

    embed = discord.Embed(
        title="🪙 Die Münze wird geworfen...",
        description="🪙",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed)

    await asyncio.sleep(0.5)
    embed.description = "🔄"
    await interaction.edit_original_response(embed=embed)

    await asyncio.sleep(0.5)
    embed.description = "🪙"
    await interaction.edit_original_response(embed=embed)

    await asyncio.sleep(0.5)

    if wahl == result:
        win = einsatz
        new_balance = update_balance(user_id, win)
        embed = discord.Embed(
            title="🪙 Coinflip - Gewonnen!",
            description=(
                f"**Ergebnis:** {result_display}\n"
                f"**Deine Wahl:** {wahl_display}\n\n"
                f"🎉 Du gewinnst **{win:,} 🪙**!"
            ),
            color=COLOR_SUCCESS,
        )
    else:
        win = -einsatz
        new_balance = update_balance(user_id, win)
        embed = discord.Embed(
            title="🪙 Coinflip - Verloren!",
            description=(
                f"**Ergebnis:** {result_display}\n"
                f"**Deine Wahl:** {wahl_display}\n\n"
                f"💀 Du verlierst **{abs(win):,} 🪙**!"
            ),
            color=COLOR_ERROR,
        )

    embed.set_footer(text=f"Kontostand: {new_balance:,} 🪙")
    await interaction.edit_original_response(embed=embed)


@d_bot.tree.command(name="coinleaderboard", description="🏆 Zeigt die reichsten User")
@is_gambling_channel()
async def cmd_coinleaderboard(interaction: discord.Interaction):
    bank = load_bank()

    balances = {}
    for key, value in bank.items():
        if key.isdigit():
            balances[key] = value

    if not balances:
        return await interaction.response.send_message(
            "❌ Noch keine Coin-Daten vorhanden!",
            ephemeral=True,
        )

    sorted_balances = sorted(
        balances.items(), key=lambda x: x[1], reverse=True
    )[:10]

    lines = []
    for i, (uid, balance) in enumerate(sorted_balances, 1):
        member = interaction.guild.get_member(int(uid)) if interaction.guild else None
        name = member.display_name if member else f"User {uid}"

        if i == 1:
            medal = "🥇"
        elif i == 2:
            medal = "🥈"
        elif i == 3:
            medal = "🥉"
        else:
            medal = f"`#{i}`"

        lines.append(f"{medal} **{name}** — {balance:,} 🪙")

    embed = discord.Embed(
        title="🏆 Coin-Leaderboard",
        description="\n".join(lines),
        color=COLOR_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    uid = str(interaction.user.id)
    if uid in balances:
        sorted_all = sorted(balances.items(), key=lambda x: x[1], reverse=True)
        position = next((i + 1 for i, (u, _) in enumerate(sorted_all) if u == uid), None)
        if position:
            embed.set_footer(
                text=f"Deine Position: #{position} mit {balances[uid]:,} 🪙"
            )

    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="gamblehelp", description="📚 Zeigt alle Gambling-Befehle")
@is_gambling_channel()
async def cmd_gamblehelp(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎰 Gambling Hilfe",
        description="Alle verfügbaren Spiele und Befehle",
        color=COLOR_INFO,
    )
    embed.add_field(
        name="💰 Coins",
        value=(
            "`/balance` — Kontostand\n"
            "`/daily` — Tägliche 500+ Coins\n"
            "`/pay @user <betrag>` — Coins senden\n"
            "`/coinleaderboard` — Reichstenliste"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎲 Schnelle Spiele",
        value=(
            "`/bet <einsatz>` — Slot-Maschine\n"
            "`/coinflip <kopf/zahl> <einsatz>` — Münzwurf\n"
            "`/roulette <farbe> <einsatz>` — Roulette\n"
            "`/scratch [einsatz]` — Rubbellose (ab 50 🪙)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎮 Strategie-Spiele",
        value=(
            "`/blackjack <einsatz>` — Blackjack gegen Dealer\n"
            "`/highlow <einsatz>` — Höher oder Tiefer?\n"
            "`/mines <einsatz> [minen]` — Minenfeld\n"
            "`/towers <einsatz>` — Turm erklimmen\n"
            "`/crash <einsatz>` — Crash Graph\n"
            "`/plinko <einsatz>` — Plinko Pyramide"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Multiplayer & Events",
        value=(
            "`/duel @user <einsatz>` — Würfelduell\n"
            "`/horserace <pferd> <einsatz>` — Pferderennen"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔐 Jackpot",
        value=(
            "`/vault <code>` — Tresor knacken (25 🪙/Versuch)\n"
            "`/vaultinfo` — Jackpot-Stand anzeigen"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎣 Angeln",
        value=(
            "`/fish` — Angel auswerfen\n"
            "`/upgrade` — Angel verbessern\n"
            "`/upgradecooldown` — Wartezeit verkürzen\n"
            "`/buybait <menge>` — Köder kaufen\n"
            "`/fishstats` — Angel-Statistiken\n"
            "`/fishtop` — Angel-Leaderboard\n"
            "`/shop` — Angel-Shop"
        ),
        inline=False,
    )
    if ADMIN_USER_IDS:
        embed.add_field(
            name="👑 Admin",
            value=(
                "`/give @user <betrag>` — Coins geben/abziehen\n"
                "`/setbalance @user <betrag>` — Kontostand setzen"
            ),
            inline=False,
        )
    embed.set_footer(text="🍀 Viel Glück!")
    await interaction.response.send_message(embed=embed, ephemeral=True)
# ══════════════════════════════════════════════════════════
#              CASINO LIMIT
# ══════════════════════════════════════════════════════════

MAX_BET = 2500


async def check_bet(interaction: discord.Interaction, einsatz: int) -> bool:
    """Prüft Einsatz-Limits. Gibt True zurück wenn alles ok ist."""
    if einsatz <= 0:
        await interaction.response.send_message("❌ Mindestens 1 Coin!", ephemeral=True)
        return False
    if einsatz > MAX_BET:
        await interaction.response.send_message(
            f"❌ Maximaler Einsatz ist **{MAX_BET:,} 🪙**!",
            ephemeral=True,
        )
        return False
    if einsatz > get_balance(interaction.user.id):
        await interaction.response.send_message(
            f"❌ Nicht genug Coins! ({get_balance(interaction.user.id):,} 🪙)",
            ephemeral=True,
        )
        return False
    return True


async def check_active_game(interaction: discord.Interaction) -> bool:
    """Prüft ob User bereits spielt. Gibt True zurück wenn frei."""
    if interaction.user.id in active_games:
        await interaction.response.send_message(
            f"❌ Du spielst gerade schon **{active_games[interaction.user.id]}**!",
            ephemeral=True,
        )
        return False
    return True


# ══════════════════════════════════════════════════════════
#            SPIEL 1: MINES (MINENFELD)
# ══════════════════════════════════════════════════════════

class MinesView(discord.ui.View):
    def __init__(self, user_id: int, einsatz: int, mine_count: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.einsatz = einsatz
        self.mine_count = mine_count
        self.grid_size = 4
        self.total_fields = self.grid_size * self.grid_size
        self.safe_fields = self.total_fields - mine_count
        self.revealed = set()
        self.mines = set(random.sample(range(self.total_fields), mine_count))
        self.game_over = False
        self.multiplier = 1.0
        self._build_buttons()

    def _calc_multiplier(self):
        safe_revealed = len([r for r in self.revealed if r not in self.mines])
        if safe_revealed == 0:
            return 1.0
        mult = 1.0
        for i in range(safe_revealed):
            total_left = self.total_fields - i
            safe_left = self.safe_fields - i
            if safe_left <= 0 or total_left <= 0:
                break
            mult *= total_left / safe_left
        return round(mult * 0.97, 2)

    def _build_buttons(self):
        self.clear_items()
        for i in range(self.total_fields):
            row = i // self.grid_size
            if i in self.revealed:
                if i in self.mines:
                    btn = discord.ui.Button(label="💣", style=discord.ButtonStyle.danger, row=row, disabled=True)
                else:
                    btn = discord.ui.Button(label="💎", style=discord.ButtonStyle.success, row=row, disabled=True)
            elif self.game_over:
                if i in self.mines:
                    btn = discord.ui.Button(label="💣", style=discord.ButtonStyle.danger, row=row, disabled=True)
                else:
                    btn = discord.ui.Button(label="⬜", style=discord.ButtonStyle.secondary, row=row, disabled=True)
            else:
                btn = discord.ui.Button(label="⬜", style=discord.ButtonStyle.secondary, row=row)
                btn.callback = self._make_callback(i)
            self.add_item(btn)

        safe_count = len([r for r in self.revealed if r not in self.mines])
        if not self.game_over and safe_count > 0:
            win = int(self.einsatz * self.multiplier)
            profit = win - self.einsatz
            cashout_btn = discord.ui.Button(
                label=f"💰 Cashout ({win:,} 🪙 / +{profit:,})",
                style=discord.ButtonStyle.success, row=4,
            )
            cashout_btn.callback = self._cashout_callback
            self.add_item(cashout_btn)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
            if index in self.mines:
                self.game_over = True
                self.revealed.add(index)
                active_games.pop(self.user_id, None)
                self._build_buttons()
                safe_count = len([r for r in self.revealed if r not in self.mines])
                embed = discord.Embed(
                    title="💣 BOOM! Mine getroffen!",
                    description=f"Du hast **{self.einsatz:,} 🪙** verloren!",
                    color=COLOR_ERROR,
                )
                embed.add_field(name="💎 Aufgedeckt", value=f"{safe_count}", inline=True)
                embed.add_field(name="📈 Multiplikator", value=f"×{self.multiplier:.2f}", inline=True)
                embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                self.revealed.add(index)
                self.multiplier = self._calc_multiplier()
                self._build_buttons()
                safe_count = len([r for r in self.revealed if r not in self.mines])
                win = int(self.einsatz * self.multiplier)
                profit = win - self.einsatz
                if safe_count >= self.safe_fields:
                    self.game_over = True
                    active_games.pop(self.user_id, None)
                    update_balance(self.user_id, win)
                    self._build_buttons()
                    embed = discord.Embed(
                        title="🏆 ALLE SICHEREN FELDER GEFUNDEN!",
                        description=f"Du gewinnst **{win:,} 🪙** (×{self.multiplier:.2f})!\nGewinn: +{profit:,} 🪙",
                        color=COLOR_GOLD,
                    )
                    embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                    await interaction.response.edit_message(embed=embed, view=None)
                    return
                embed = discord.Embed(
                    title="💎 Sicher!",
                    description=(
                        f"**Aktueller Gewinn:** {win:,} 🪙 (×{self.multiplier:.2f})\n"
                        f"**Profit:** +{profit:,} 🪙\n\n"
                        f"💎 Aufgedeckt: **{safe_count}** / {self.safe_fields}\n"
                        f"💣 Minen: **{self.mine_count}**"
                    ),
                    color=COLOR_SUCCESS,
                )
                embed.set_footer(text="Weiter aufdecken oder 💰 Cashout!")
                await interaction.response.edit_message(embed=embed, view=self)
        return callback

    async def _cashout_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        self.game_over = True
        active_games.pop(self.user_id, None)
        win = int(self.einsatz * self.multiplier)
        profit = win - self.einsatz
        update_balance(self.user_id, win)
        self._build_buttons()
        safe_count = len([r for r in self.revealed if r not in self.mines])
        embed = discord.Embed(
            title="💰 Cashout!",
            description=f"Du nimmst **{win:,} 🪙** mit!\nGewinn: **+{profit:,} 🪙**",
            color=COLOR_GOLD,
        )
        embed.add_field(name="📈 Multiplikator", value=f"×{self.multiplier:.2f}", inline=True)
        embed.add_field(name="💎 Aufgedeckt", value=f"{safe_count}/{self.safe_fields}", inline=True)
        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        if not self.game_over:
            active_games.pop(self.user_id, None)
            self.game_over = True


@d_bot.tree.command(name="mines", description="💣 Spiele Mines - decke Felder auf ohne eine Mine zu treffen!")
@app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)", minen="Anzahl der Minen (1-12)")
@is_gambling_channel()
async def cmd_mines(interaction: discord.Interaction, einsatz: int, minen: int = 3):
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return
    if minen < 1 or minen > 12:
        return await interaction.response.send_message("❌ Minen müssen zwischen 1 und 12 sein!", ephemeral=True)

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    active_games[user_id] = "Mines"
    view = MinesView(user_id, einsatz, minen)
    embed = discord.Embed(
        title="💣 Mines",
        description=(
            f"Decke Felder auf ohne eine Mine zu treffen!\n\n"
            f"**Einsatz:** {einsatz:,} 🪙\n**Minen:** {minen}\n"
            f"**Sichere Felder:** {view.safe_fields}\n\n"
            f"Je mehr Minen, desto höher der Multiplikator!"
        ),
        color=COLOR_INFO,
    )
    embed.set_footer(text="Klicke auf ⬜ um ein Feld aufzudecken!")
    await interaction.response.send_message(embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#            SPIEL 2: CRASH
# ══════════════════════════════════════════════════════════

CRASH_MAX_MULTIPLIER = 7.0

# Plinko
PLINKO_ROWS = 8
PLINKO_MULTIPLIERS = [10.0, 3.0, 1.5, 0.5, 0.2, 0.5, 1.5, 3.0, 10.0]
PLINKO_COLORS = {
    10.0: "🟥",
    3.0: "🟧",
    1.5: "🟨",
    0.5: "🟩",
    0.2: "⬜",
}

class CrashView(discord.ui.View):
    def __init__(self, user_id: int, einsatz: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.einsatz = einsatz
        self.crashed = False
        self.cashed_out = False
        self.multiplier = 1.0
        self.crash_point = self._generate_crash_point()

    def _generate_crash_point(self) -> float:
        # Admin Cheat
        if CRASH_CHEAT and self.user_id in ADMIN_USER_IDS:
            r = random.random()
            if r < 0.50:
                return round(random.uniform(5.0, CRASH_MAX_MULTIPLIER), 2)
            else:
                return round(random.uniform(2.0, CRASH_MAX_MULTIPLIER), 2)

        # Normale User — schwerer zu gewinnen
        r = random.random()

        # 5% Chance auf Sofort-Crash (×1.00)
        if r < 0.05:
            return 1.0

        # 15% Chance auf frühen Crash (×1.01 - ×1.30)
        if r < 0.20:
            return round(random.uniform(1.01, 1.30), 2)

        # 30% Chance auf niedrigen Crash (×1.30 - ×2.00)
        if r < 0.50:
            return round(random.uniform(1.30, 2.00), 2)

        # 30% Chance auf mittleren Crash (×2.00 - ×4.00)
        if r < 0.80:
            return round(random.uniform(2.00, 4.00), 2)

        # 15% Chance auf hohen Crash (×4.00 - ×7.00)
        if r < 0.95:
            return round(random.uniform(4.00, 7.00), 2)

        # 5% Chance auf sehr hohen Crash (×7.00 - Max)
        return round(random.uniform(7.00, CRASH_MAX_MULTIPLIER), 2)

    @discord.ui.button(label="💰 CASHOUT (×1.00)", style=discord.ButtonStyle.success)
    async def btn_cashout(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        if self.crashed or self.cashed_out:
            return
        self.cashed_out = True
        active_games.pop(self.user_id, None)
        win = int(self.einsatz * self.multiplier)
        profit = win - self.einsatz
        update_balance(self.user_id, win)
        embed = discord.Embed(
            title="💰 Cashout!",
            description=(
                f"Du hast bei **×{self.multiplier:.2f}** ausgecasht!\n\n"
                f"💰 Auszahlung: **{win:,} 🪙**\n"
                f"📈 Gewinn: **+{profit:,} 🪙**"
            ),
            color=COLOR_SUCCESS,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙 • Crash wäre bei ×{self.crash_point:.2f}")
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


@d_bot.tree.command(name="crash", description="📈 Crash - Cashout bevor der Graph abstürzt!")
@app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)")
@is_gambling_channel()
async def cmd_crash(interaction: discord.Interaction, einsatz: int):
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    active_games[user_id] = "Crash"
    view = CrashView(user_id, einsatz)

    embed = discord.Embed(
        title="📈 Crash — ×1.00",
        description=(
            f"**Einsatz:** {einsatz:,} 🪙\n\n"
            f"📈 Der Graph steigt...\n"
            f"`▓░░░░░░░░░░░░░░░░░░░`"
        ),
        color=COLOR_INFO,
    )
    embed.set_footer(text=f"Drücke CASHOUT bevor es crasht! (Max ×{CRASH_MAX_MULTIPLIER:.1f})")
    await interaction.response.send_message(embed=embed, view=view)

    if CRASH_CHEAT and user_id in ADMIN_USER_IDS:
        try:
            await interaction.followup.send(
                f"🔮 **Crash-Cheat:** Der Graph crasht bei **×{view.crash_point:.2f}**",
                ephemeral=True,
            )
        except Exception:
            pass

    step = 0
    while not view.crashed and not view.cashed_out:
        for _ in range(10):
            await asyncio.sleep(0.1)
            if view.cashed_out or view.crashed:
                break

        if view.cashed_out or view.crashed:
            break

        step += 1
        next_multiplier = round(view.multiplier + 0.1 + (step * 0.05) + random.uniform(0, 0.1), 2)

        # Max-Multiplikator erzwingen
        if next_multiplier >= CRASH_MAX_MULTIPLIER:
            next_multiplier = CRASH_MAX_MULTIPLIER

        if next_multiplier >= view.crash_point:
            await asyncio.sleep(0.05)
            if view.cashed_out:
                break

            view.multiplier = view.crash_point
            view.crashed = True
            active_games.pop(user_id, None)
            bar_len = min(20, int(view.multiplier * 2))
            bar = "▓" * bar_len + "💥"
            embed = discord.Embed(
                title=f"💥 CRASH bei ×{view.crash_point:.2f}!",
                description=f"Du hast **{einsatz:,} 🪙** verloren!\n\n`{bar}`",
                color=COLOR_ERROR,
            )
            embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
            view.stop()
            try:
                await interaction.edit_original_response(embed=embed, view=None)
            except Exception:
                pass
            break

        # Bei Max-Multiplikator Auto-Cashout
        if next_multiplier >= CRASH_MAX_MULTIPLIER and not view.cashed_out:
            view.multiplier = CRASH_MAX_MULTIPLIER
            view.cashed_out = True
            active_games.pop(user_id, None)
            win = int(einsatz * CRASH_MAX_MULTIPLIER)
            profit = win - einsatz
            update_balance(user_id, win)
            embed = discord.Embed(
                title=f"🏆 MAX MULTIPLIER! ×{CRASH_MAX_MULTIPLIER:.1f}!",
                description=(
                    f"Auto-Cashout bei Maximum!\n\n"
                    f"💰 Auszahlung: **{win:,} 🪙**\n"
                    f"📈 Gewinn: **+{profit:,} 🪙**"
                ),
                color=COLOR_GOLD,
            )
            embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
            view.stop()
            try:
                await interaction.edit_original_response(embed=embed, view=None)
            except Exception:
                pass
            break

        view.multiplier = next_multiplier
        bar_len = min(20, int(view.multiplier * 2))
        bar = "▓" * bar_len + "░" * (20 - bar_len)
        win = int(einsatz * view.multiplier)
        profit = win - einsatz
        view.btn_cashout.label = f"💰 CASHOUT (×{view.multiplier:.2f} = {win:,} 🪙)"

        embed = discord.Embed(
            title=f"📈 Crash — ×{view.multiplier:.2f}",
            description=(
                f"**Einsatz:** {einsatz:,} 🪙\n"
                f"**Aktuell:** {win:,} 🪙 (+{profit:,})\n\n`{bar}`"
            ),
            color=COLOR_SUCCESS if view.multiplier < 3 else COLOR_GOLD,
        )
        embed.set_footer(text=f"Drücke CASHOUT bevor es crasht! (Max ×{CRASH_MAX_MULTIPLIER:.1f})")
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception:
            break


# ══════════════════════════════════════════════════════════
#            SPIEL 3: BLACKJACK
# ══════════════════════════════════════════════════════════

class BlackjackView(discord.ui.View):
    def __init__(self, user_id: int, einsatz: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.einsatz = einsatz
        self.deck = create_deck()
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]
        self.game_over = False

    def _build_embed(self, reveal_dealer: bool = False) -> discord.Embed:
        p_val = hand_value(self.player_hand)
        d_val = hand_value(self.dealer_hand)
        if reveal_dealer:
            dealer_display = f"{hand_str(self.dealer_hand)} — **{d_val}**"
        else:
            dealer_display = f"{card_str(self.dealer_hand[0])} `❓` — **?**"
        embed = discord.Embed(title="🃏 Blackjack", color=COLOR_INFO)
        embed.add_field(name="🤖 Dealer", value=dealer_display, inline=False)
        embed.add_field(name="👤 Du", value=f"{hand_str(self.player_hand)} — **{p_val}**", inline=False)
        embed.add_field(name="💰 Einsatz", value=f"{self.einsatz:,} 🪙", inline=True)
        return embed

    @discord.ui.button(label="🃏 Hit", style=discord.ButtonStyle.primary)
    async def btn_hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        if self.game_over:
            return
        self.player_hand.append(self.deck.pop())
        p_val = hand_value(self.player_hand)
        if p_val > 21:
            self.game_over = True
            active_games.pop(self.user_id, None)
            embed = self._build_embed(reveal_dealer=True)
            embed.title = "💥 Bust! Du hast verloren!"
            embed.color = COLOR_ERROR
            embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
            self.stop()
            await interaction.response.edit_message(embed=embed, view=None)
        elif p_val == 21:
            await self._stand(interaction)
        else:
            embed = self._build_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="✋ Stand", style=discord.ButtonStyle.danger)
    async def btn_stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        if self.game_over:
            return
        await self._stand(interaction)

    @discord.ui.button(label="⏫ Double Down", style=discord.ButtonStyle.success)
    async def btn_double(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        if self.game_over:
            return
        if len(self.player_hand) > 2:
            return await interaction.response.send_message("❌ Double Down nur beim ersten Zug!", ephemeral=True)
        double_amount = min(self.einsatz, MAX_BET - self.einsatz)
        if double_amount <= 0 or get_balance(self.user_id) < double_amount:
            return await interaction.response.send_message("❌ Nicht genug Coins oder Limit erreicht!", ephemeral=True)
        update_balance(self.user_id, -double_amount)
        self.einsatz += double_amount
        self.player_hand.append(self.deck.pop())
        if hand_value(self.player_hand) > 21:
            self.game_over = True
            active_games.pop(self.user_id, None)
            embed = self._build_embed(reveal_dealer=True)
            embed.title = "💥 Bust! Du hast verloren! (Double Down)"
            embed.color = COLOR_ERROR
            embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
            self.stop()
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await self._stand(interaction)

    async def _stand(self, interaction: discord.Interaction):
        self.game_over = True
        active_games.pop(self.user_id, None)
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())
        p_val = hand_value(self.player_hand)
        d_val = hand_value(self.dealer_hand)
        embed = self._build_embed(reveal_dealer=True)

        if d_val > 21:
            update_balance(self.user_id, self.einsatz * 2)
            embed.title = "🎉 Dealer Bust! Du gewinnst!"
            embed.color = COLOR_SUCCESS
        elif p_val > d_val:
            update_balance(self.user_id, self.einsatz * 2)
            embed.title = "🎉 Du gewinnst!"
            embed.color = COLOR_SUCCESS
        elif p_val == d_val:
            update_balance(self.user_id, self.einsatz)
            embed.title = "🤝 Unentschieden! Einsatz zurück."
            embed.color = COLOR_WARNING
        else:
            embed.title = "💀 Dealer gewinnt!"
            embed.color = COLOR_ERROR

        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        if not self.game_over:
            active_games.pop(self.user_id, None)


@d_bot.tree.command(name="blackjack", description="🃏 Spiele Blackjack gegen den Dealer!")
@app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)")
@is_gambling_channel()
async def cmd_blackjack(interaction: discord.Interaction, einsatz: int):
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    active_games[user_id] = "Blackjack"
    view = BlackjackView(user_id, einsatz)

    if hand_value(view.player_hand) == 21:
        win = int(einsatz * 2.5)
        update_balance(user_id, win)
        active_games.pop(user_id, None)
        embed = view._build_embed(reveal_dealer=True)
        embed.title = "🃏 BLACKJACK! ×2.5 Gewinn!"
        embed.color = COLOR_GOLD
        embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
        return await interaction.response.send_message(embed=embed)

    embed = view._build_embed()
    embed.set_footer(text="Hit = Karte ziehen • Stand = Halten • Double = Verdoppeln")
    await interaction.response.send_message(embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#            SPIEL 4: PFERDERENNEN
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="horserace", description="🐎 Wette auf ein Pferd im Rennen!")
@app_commands.describe(pferd="Nummer des Pferdes (1-5)", einsatz="Dein Einsatz in Coins (max 2.500)")
@is_gambling_channel()
async def cmd_horserace(interaction: discord.Interaction, pferd: int, einsatz: int):
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return
    if pferd < 1 or pferd > 5:
        return await interaction.response.send_message("❌ Wähle ein Pferd von 1 bis 5!", ephemeral=True)

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    active_games[user_id] = "Pferderennen"
    positions = [0] * 5
    chosen = pferd - 1

    horse_list = "\n".join(
        f"{'👉 ' if i == chosen else '   '}`{i+1}.` {h['emoji']} {h['name']}"
        for i, h in enumerate(HORSES)
    )
    embed = discord.Embed(
        title="🏇 Pferderennen!",
        description=f"**Dein Pferd:** {HORSES[chosen]['emoji']} {HORSES[chosen]['name']}\n**Einsatz:** {einsatz:,} 🪙\n\n{horse_list}",
        color=COLOR_INFO,
    )
    embed.set_footer(text="Das Rennen beginnt...")
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(1.5)

    winner = None
    while winner is None:
        await asyncio.sleep(0.6)
        for i in range(5):
            positions[i] += random.randint(1, 3)
            if positions[i] >= RACE_TRACK_LENGTH:
                positions[i] = RACE_TRACK_LENGTH
                if winner is None:
                    winner = i

        track_lines = []
        for i, h in enumerate(HORSES):
            pos = min(positions[i], RACE_TRACK_LENGTH)
            track = "░" * pos + h["emoji"] + "░" * (RACE_TRACK_LENGTH - pos) + "🏁"
            marker = " 👈" if i == chosen else ""
            track_lines.append(f"`{i+1}.` {track}{marker}")

        embed.description = f"**Einsatz:** {einsatz:,} 🪙\n\n" + "\n".join(track_lines)
        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            break

    active_games.pop(user_id, None)

    if winner == chosen:
        win = einsatz * 5
        profit = win - einsatz
        update_balance(user_id, win)
        embed.title = f"🎉 {HORSES[winner]['emoji']} {HORSES[winner]['name']} gewinnt! DU GEWINNST!"
        embed.color = COLOR_GOLD
        embed.set_footer(text=f"+{profit:,} 🪙 • Kontostand: {get_balance(user_id):,} 🪙")
    else:
        embed.title = f"💀 {HORSES[winner]['emoji']} {HORSES[winner]['name']} gewinnt! Du verlierst."
        embed.color = COLOR_ERROR
        embed.set_footer(text=f"-{einsatz:,} 🪙 • Kontostand: {get_balance(user_id):,} 🪙")

    await interaction.edit_original_response(embed=embed, view=None)


# ══════════════════════════════════════════════════════════
#            SPIEL 5: TOWERS
# ══════════════════════════════════════════════════════════

class TowersView(discord.ui.View):
    def __init__(self, user_id: int, einsatz: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.einsatz = einsatz
        self.level = 0
        self.max_levels = 8
        self.game_over = False
        self.multipliers = [1.0, 1.5, 2.0, 3.0, 4.5, 7.0, 10.0, 15.0, 25.0]
        self.safe_columns = [random.randint(0, 2) for _ in range(self.max_levels)]
        self._build_level()

    def _build_level(self):
        self.clear_items()
        for i in range(3):
            btn = discord.ui.Button(label=f"🚪 Tür {i+1}", style=discord.ButtonStyle.primary, row=0)
            btn.callback = self._make_callback(i)
            self.add_item(btn)
        cashout_btn = discord.ui.Button(
            label=f"💰 Cashout (×{self.multipliers[self.level]:.1f})",
            style=discord.ButtonStyle.success, row=1, disabled=self.level == 0,
        )
        cashout_btn.callback = self._cashout
        self.add_item(cashout_btn)

    def _build_floors(self):
        floors = ""
        for l in range(self.max_levels - 1, -1, -1):
            if l < self.level:
                floors += f"✅ Ebene {l+1} — ×{self.multipliers[l+1]:.1f}\n"
            elif l == self.level:
                floors += f"👉 **Ebene {l+1}** — ×{self.multipliers[l+1]:.1f}\n"
            else:
                floors += f"❓ Ebene {l+1} — ×{self.multipliers[l+1]:.1f}\n"
        return floors

    def _make_callback(self, col: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
            if self.game_over:
                return
            if col == self.safe_columns[self.level]:
                self.level += 1
                if self.level >= self.max_levels:
                    self.game_over = True
                    active_games.pop(self.user_id, None)
                    win = int(self.einsatz * self.multipliers[self.level])
                    profit = win - self.einsatz
                    update_balance(self.user_id, win)
                    embed = discord.Embed(
                        title="🏆 ALLE LEVEL GESCHAFFT!",
                        description=f"Du gewinnst **{win:,} 🪙** (×{self.multipliers[self.level]:.1f})!\nGewinn: +{profit:,} 🪙",
                        color=COLOR_GOLD,
                    )
                    embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                    self.stop()
                    await interaction.response.edit_message(embed=embed, view=None)
                else:
                    self._build_level()
                    win = int(self.einsatz * self.multipliers[self.level])
                    profit = win - self.einsatz
                    embed = discord.Embed(
                        title=f"✅ Ebene {self.level} geschafft!",
                        description=(
                            f"**Aktueller Gewinn:** {win:,} 🪙 (×{self.multipliers[self.level]:.1f})\n"
                            f"**Profit:** +{profit:,} 🪙\n"
                            f"**Nächste Ebene:** ×{self.multipliers[self.level + 1]:.1f}\n\n"
                            f"Wähle die nächste Tür oder Cashout!"
                        ),
                        color=COLOR_SUCCESS,
                    )
                    embed.add_field(name="🗼 Turm", value=self._build_floors(), inline=False)
                    embed.set_footer(text=f"Einsatz: {self.einsatz:,} 🪙")
                    await interaction.response.edit_message(embed=embed, view=self)
            else:
                self.game_over = True
                active_games.pop(self.user_id, None)
                embed = discord.Embed(
                    title=f"💀 Falle auf Ebene {self.level + 1}!",
                    description=f"Du hast **{self.einsatz:,} 🪙** verloren!\nDie sichere Tür war **Tür {self.safe_columns[self.level] + 1}**.",
                    color=COLOR_ERROR,
                )
                embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                self.stop()
                await interaction.response.edit_message(embed=embed, view=None)
        return callback

    async def _cashout(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        self.game_over = True
        active_games.pop(self.user_id, None)
        win = int(self.einsatz * self.multipliers[self.level])
        profit = win - self.einsatz
        update_balance(self.user_id, win)
        embed = discord.Embed(
            title="💰 Cashout!",
            description=f"Du nimmst **{win:,} 🪙** mit! (×{self.multipliers[self.level]:.1f})\nGewinn: +{profit:,} 🪙",
            color=COLOR_GOLD,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        if not self.game_over:
            active_games.pop(self.user_id, None)


@d_bot.tree.command(name="towers", description="🗼 Klettere den Turm hoch - wähle die richtige Tür!")
@app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)")
@is_gambling_channel()
async def cmd_towers(interaction: discord.Interaction, einsatz: int):
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    active_games[user_id] = "Towers"
    view = TowersView(user_id, einsatz)

    embed = discord.Embed(
        title="🗼 Towers",
        description=f"Klettere so hoch wie du kannst!\nEine Tür ist sicher, zwei sind Fallen.\n\n**Einsatz:** {einsatz:,} 🪙",
        color=COLOR_INFO,
    )
    embed.add_field(name="🗼 Turm", value=view._build_floors(), inline=False)
    embed.set_footer(text="Wähle eine Tür!")
    await interaction.response.send_message(embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#            SPIEL 6: SCRATCH CARDS (AUSKOMMENTIERT)
# ══════════════════════════════════════════════════════════

# @d_bot.tree.command(name="scratch", description="🎟️ Kaufe ein Rubbellos!")
# @app_commands.describe(einsatz="Preis des Loses (50-2500 🪙)")
# @is_gambling_channel()
# async def cmd_scratch(interaction: discord.Interaction, einsatz: int = 50):
#     if not await check_bet(interaction, einsatz):
#         return
#     if einsatz < 50:
#         return await interaction.response.send_message("❌ Mindestens 50 🪙!", ephemeral=True)
#     user_id = interaction.user.id
#     update_balance(user_id, -einsatz)
#     grid = [random.choice(SCRATCH_SYMBOLS) for _ in range(9)]
#     roll = random.random()
#     if roll < 0.03:
#         symbol = random.choice(SCRATCH_SYMBOLS)
#         positions = random.sample(range(9), 3)
#         for p in positions:
#             grid[p] = symbol
#     hidden = "||❓|| " * 3 + "\n" + "||❓|| " * 3 + "\n" + "||❓|| " * 3
#     embed = discord.Embed(
#         title="🎟️ Rubbellos",
#         description=f"**Preis:** {einsatz:,} 🪙\n\n{hidden}",
#         color=COLOR_INFO,
#     )
#     await interaction.response.send_message(embed=embed)
#     await asyncio.sleep(1.5)
#     revealed = ""
#     for i in range(0, 9, 3):
#         revealed += f"||{grid[i]}|| ||{grid[i+1]}|| ||{grid[i+2]}||\n"
#     from collections import Counter
#     counts = Counter(grid)
#     max_count = max(counts.values())
#     max_symbol = max(counts, key=counts.get)
#     if max_count >= 3:
#         multiplier = {3: 2, 4: 5, 5: 10, 6: 25, 7: 50, 8: 100, 9: 500}.get(max_count, 2)
#         win = einsatz * multiplier
#         update_balance(user_id, win)
#         embed = discord.Embed(
#             title=f"🎉 {max_count}× {max_symbol} — GEWINN!",
#             description=f"\n{revealed}\n💰 **{win:,} 🪙**! (×{multiplier})",
#             color=COLOR_GOLD if max_count >= 4 else COLOR_SUCCESS,
#         )
#     else:
#         embed = discord.Embed(
#             title="😔 Kein Gewinn",
#             description=f"\n{revealed}\n*Vielleicht beim nächsten Mal!*",
#             color=COLOR_ERROR,
#         )
#     embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
#     await interaction.edit_original_response(embed=embed)


# ══════════════════════════════════════════════════════════
#            SPIEL 7: HIGH OR LOW
# ══════════════════════════════════════════════════════════

class HighLowView(discord.ui.View):
    def __init__(self, user_id: int, einsatz: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.einsatz = einsatz
        self.current_pot = einsatz
        self.deck = create_deck()
        self.current_card = self.deck.pop()
        self.round = 0
        self.game_over = False

    def _card_rank(self, card: dict) -> int:
        order = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
        return order.index(card["name"])

    @discord.ui.button(label="⬆️ Höher", style=discord.ButtonStyle.success)
    async def btn_higher(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        await self._guess(interaction, "higher")

    @discord.ui.button(label="⬇️ Tiefer", style=discord.ButtonStyle.danger)
    async def btn_lower(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        await self._guess(interaction, "lower")

    @discord.ui.button(label="💰 Cashout", style=discord.ButtonStyle.secondary)
    async def btn_cashout(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
        self.game_over = True
        active_games.pop(self.user_id, None)
        profit = self.current_pot - self.einsatz
        update_balance(self.user_id, self.current_pot)
        embed = discord.Embed(
            title="💰 Cashout!",
            description=f"Du nimmst **{self.current_pot:,} 🪙** mit!\nGewinn: +{profit:,} 🪙",
            color=COLOR_GOLD,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

    async def _guess(self, interaction: discord.Interaction, direction: str):
        if self.game_over:
            return
        next_card = self.deck.pop()
        current_rank = self._card_rank(self.current_card)
        next_rank = self._card_rank(next_card)
        correct = (
            (direction == "higher" and next_rank > current_rank) or
            (direction == "lower" and next_rank < current_rank) or
            (next_rank == current_rank)
        )
        if correct:
            self.round += 1
            self.current_pot *= 2
            self.current_card = next_card
            profit = self.current_pot - self.einsatz
            embed = discord.Embed(
                title=f"✅ Richtig! Runde {self.round}",
                description=(
                    f"**Karte:** {card_str(next_card)}\n"
                    f"**Pot:** {self.current_pot:,} 🪙 (×{2**self.round})\n"
                    f"**Profit:** +{profit:,} 🪙\n\n"
                    f"Nächste Karte: höher oder tiefer als {card_str(next_card)}?"
                ),
                color=COLOR_SUCCESS,
            )
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            self.game_over = True
            active_games.pop(self.user_id, None)
            embed = discord.Embed(
                title=f"💀 Falsch! Karte war {card_str(next_card)}",
                description=f"Du verlierst **{self.einsatz:,} 🪙**!",
                color=COLOR_ERROR,
            )
            embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
            self.stop()
            await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        if not self.game_over:
            active_games.pop(self.user_id, None)


@d_bot.tree.command(name="highlow", description="🃏 Ist die nächste Karte höher oder tiefer?")
@app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)")
@is_gambling_channel()
async def cmd_highlow(interaction: discord.Interaction, einsatz: int):
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    active_games[user_id] = "High or Low"
    view = HighLowView(user_id, einsatz)
    embed = discord.Embed(
        title="🃏 High or Low",
        description=(
            f"**Karte:** {card_str(view.current_card)}\n"
            f"**Einsatz:** {einsatz:,} 🪙\n\n"
            f"Ist die nächste Karte **höher** oder **tiefer**?"
        ),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#            SPIEL 8: ROULETTE
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="roulette", description="🎡 Setze auf Rot, Schwarz oder Grün!")
@app_commands.describe(farbe="Wähle eine Farbe", einsatz="Dein Einsatz in Coins (max 2.500)")
@app_commands.choices(farbe=[
    app_commands.Choice(name="🔴 Rot (×2)", value="rot"),
    app_commands.Choice(name="⚫ Schwarz (×2)", value="schwarz"),
    app_commands.Choice(name="🟢 Grün (×14)", value="grün"),
])
@is_gambling_channel()
async def cmd_roulette(interaction: discord.Interaction, farbe: str, einsatz: int):
    if not await check_bet(interaction, einsatz):
        return

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    farbe_display = {"rot": "🔴 Rot", "schwarz": "⚫ Schwarz", "grün": "🟢 Grün"}

    embed = discord.Embed(
        title="🎡 Roulette dreht sich...",
        description=f"**Deine Wahl:** {farbe_display[farbe]}\n**Einsatz:** {einsatz:,} 🪙\n\n🎡 Das Rad dreht...",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed)

    spin_emojis = ["🔴", "⚫", "🔴", "⚫", "🟢", "🔴", "⚫"]
    for _ in range(5):
        await asyncio.sleep(0.4)
        emoji = random.choice(spin_emojis)
        embed.description = f"**Deine Wahl:** {farbe_display[farbe]}\n**Einsatz:** {einsatz:,} 🪙\n\n🎡 {emoji} {emoji} {emoji}"
        await interaction.edit_original_response(embed=embed)

    await asyncio.sleep(0.5)

    number = random.randint(0, 36)
    if number == 0:
        result_color = "grün"
        result_display = "🟢 0 Grün"
    elif number in ROULETTE_RED:
        result_color = "rot"
        result_display = f"🔴 {number} Rot"
    else:
        result_color = "schwarz"
        result_display = f"⚫ {number} Schwarz"

    if farbe == result_color:
        multiplier = 14 if farbe == "grün" else 2
        win = einsatz * multiplier
        profit = win - einsatz
        update_balance(user_id, win)
        embed = discord.Embed(
            title=f"🎉 {result_display} — GEWINN!",
            description=(
                f"**Deine Wahl:** {farbe_display[farbe]}\n\n"
                f"💰 Auszahlung: **{win:,} 🪙** (×{multiplier})\n"
                f"📈 Gewinn: **+{profit:,} 🪙**"
            ),
            color=COLOR_GOLD if farbe == "grün" else COLOR_SUCCESS,
        )
    else:
        embed = discord.Embed(
            title=f"💀 {result_display} — Verloren!",
            description=f"**Deine Wahl:** {farbe_display[farbe]}\n\n😔 Du verlierst **{einsatz:,} 🪙**.",
            color=COLOR_ERROR,
        )

    embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
    await interaction.edit_original_response(embed=embed)


# ══════════════════════════════════════════════════════════
#            SPIEL 9: DUELL
# ══════════════════════════════════════════════════════════

class DuelView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, einsatz: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.einsatz = einsatz
        self.accepted = False

    @discord.ui.button(label="⚔️ Annehmen!", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message(
                f"❌ Nur {self.opponent.mention} kann das Duell annehmen!", ephemeral=True,
            )
        if get_balance(self.opponent.id) < self.einsatz:
            return await interaction.response.send_message(
                f"❌ Du hast nicht genug Coins! ({get_balance(self.opponent.id):,} 🪙)", ephemeral=True,
            )

        self.accepted = True
        update_balance(self.opponent.id, -self.einsatz)

        roll_1 = random.randint(1, 100)
        roll_2 = random.randint(1, 100)
        while roll_1 == roll_2:
            roll_2 = random.randint(1, 100)

        pot = self.einsatz * 2
        fee = int(pot * 0.05)
        payout = pot - fee

        if roll_1 > roll_2:
            winner = self.challenger
        else:
            winner = self.opponent

        update_balance(winner.id, payout)
        active_games.pop(self.challenger.id, None)
        active_games.pop(self.opponent.id, None)

        embed = discord.Embed(title="⚔️ Duell Ergebnis!", color=COLOR_GOLD)
        embed.add_field(name=f"🎲 {self.challenger.display_name}", value=f"**{roll_1}**", inline=True)
        embed.add_field(name="⚔️", value="VS", inline=True)
        embed.add_field(name=f"🎲 {self.opponent.display_name}", value=f"**{roll_2}**", inline=True)
        embed.add_field(
            name="🏆 Gewinner",
            value=f"{winner.mention} gewinnt **{payout:,} 🪙**!",
            inline=False,
        )
        embed.set_footer(text=f"Pot: {pot:,} • Gebühr (5%): {fee:,} 🪙")
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("❌ Nicht für dich!", ephemeral=True)
        update_balance(self.challenger.id, self.einsatz)
        active_games.pop(self.challenger.id, None)
        embed = discord.Embed(
            title="❌ Duell abgelehnt!",
            description=f"{self.opponent.mention} hat das Duell abgelehnt.\nEinsatz wurde zurückgegeben.",
            color=COLOR_ERROR,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        if not self.accepted:
            update_balance(self.challenger.id, self.einsatz)
            active_games.pop(self.challenger.id, None)


@d_bot.tree.command(name="duel", description="⚔️ Fordere jemanden zum Würfelduell heraus!")
@app_commands.describe(gegner="Der User den du herausforderst", einsatz="Einsatz pro Person (max 2.500)")
@is_gambling_channel()
async def cmd_duel(interaction: discord.Interaction, gegner: discord.Member, einsatz: int):
    if gegner.id == interaction.user.id:
        return await interaction.response.send_message("❌ Du kannst dich nicht selbst herausfordern!", ephemeral=True)
    if gegner.bot:
        return await interaction.response.send_message("❌ Du kannst keinen Bot herausfordern!", ephemeral=True)
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)
    active_games[user_id] = "Duell"
    view = DuelView(interaction.user, gegner, einsatz)
    embed = discord.Embed(
        title="⚔️ Duell-Herausforderung!",
        description=(
            f"{interaction.user.mention} fordert {gegner.mention} heraus!\n\n"
            f"**Einsatz:** {einsatz:,} 🪙 pro Person\n"
            f"**Pot:** {einsatz * 2:,} 🪙 (5% Gebühr)\n\n"
            f"{gegner.mention}, nimmst du an?"
        ),
        color=COLOR_WARNING,
    )
    embed.set_footer(text="Läuft in 60 Sekunden ab")
    await interaction.response.send_message(content=gegner.mention, embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#            SPIEL 10: THE VAULT (SAFE-KNACKER)
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="vault", description="🔐 Versuche den Tresor zu knacken!")
@app_commands.describe(code="Dein 3-stelliger Code-Versuch (000-999)")
@is_gambling_channel()
async def cmd_vault(interaction: discord.Interaction, code: str):
    user_id = interaction.user.id
    attempt_cost = 25
    code = code.strip()
    if not code.isdigit() or len(code) != 3:
        return await interaction.response.send_message(
            "❌ Der Code muss 3 Ziffern haben! (z.B. `042`, `999`, `000`)", ephemeral=True,
        )
    if get_balance(user_id) < attempt_cost:
        return await interaction.response.send_message(
            f"❌ Ein Versuch kostet **{attempt_cost} 🪙**! Du hast nur {get_balance(user_id):,} 🪙.",
            ephemeral=True,
        )

    update_balance(user_id, -attempt_cost)
    vault = get_vault_data()
    vault["attempts"] = vault.get("attempts", 0) + 1
    vault["jackpot"] = vault.get("jackpot", 1000) + attempt_cost

    if code == vault["code"]:
        jackpot = vault["jackpot"]
        update_balance(user_id, jackpot)
        embed = discord.Embed(
            title="🔓🔓🔓 TRESOR GEKNACKT! 🔓🔓🔓",
            description=(
                f"**{interaction.user.mention}** hat den Code **{code}** geknackt!\n\n"
                f"💰 **JACKPOT: {jackpot:,} 🪙**\n\n"
                f"📊 Nach **{vault['attempts']}** Versuchen insgesamt!"
            ),
            color=COLOR_GOLD,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
        vault["code"] = generate_vault_code()
        vault["jackpot"] = 1000
        vault["attempts"] = 0
        save_vault(vault)
        await interaction.response.send_message(embed=embed)
    else:
        save_vault(vault)
        correct = vault["code"]
        correct_digits = sum(1 for a, b in zip(code, correct) if a == b)
        close_digits = sum(1 for a, b in zip(code, correct) if a != b and abs(int(a) - int(b)) <= 1)

        hint = ""
        if correct_digits > 0:
            hint += f"🟢 **{correct_digits}** Ziffer(n) richtig!\n"
        if close_digits > 0:
            hint += f"🟡 **{close_digits}** Ziffer(n) fast richtig (±1)!\n"
        if correct_digits == 0 and close_digits == 0:
            hint += "🔴 Keine Ziffer stimmt oder ist nah dran.\n"

        embed = discord.Embed(
            title="🔐 Falscher Code!",
            description=(
                f"**Versuch:** `{code}` ❌\n\n{hint}\n"
                f"🏦 **Tresor-Jackpot:** {vault['jackpot']:,} 🪙\n"
                f"📊 **Versuche gesamt:** {vault['attempts']}\n"
                f"💰 **Versuch-Kosten:** {attempt_cost} 🪙"
            ),
            color=COLOR_ERROR,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙 • Code: 000-999")
        await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(name="vaultinfo", description="🏦 Zeigt den aktuellen Tresor-Jackpot")
@is_gambling_channel()
async def cmd_vaultinfo(interaction: discord.Interaction):
    vault = get_vault_data()
    embed = discord.Embed(
        title="🏦 Der Tresor",
        description=(
            f"💰 **Aktueller Jackpot:** {vault['jackpot']:,} 🪙\n"
            f"📊 **Bisherige Versuche:** {vault['attempts']}\n"
            f"🎯 **Versuch-Kosten:** 25 🪙\n\n"
            f"*Errate den 3-stelligen Code (000-999) und räume den Tresor leer!*\n"
            f"*Der Jackpot wächst mit jedem Fehlversuch!*\n\n"
            f"`/vault <code>` — z.B. `/vault 042`"
        ),
        color=COLOR_GOLD,
    )
    embed.set_footer(text="Jeder Versuch gibt Hinweise!")
    await interaction.response.send_message(embed=embed)
    
# ══════════════════════════════════════════════════════════
#            SLASH COMMANDS: ANGEL SYSTEM
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="fish", description="🎣 Wirf deine Angel aus und fange Fische!")
@is_fishing_channel()
async def cmd_fish(interaction: discord.Interaction):
    user_id = interaction.user.id
    fishing_data = get_fishing_data(user_id)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    user_cooldown = get_user_cooldown(fishing_data)

    # Cooldown prüfen
    last_time = fishing_data.get("last_fish_time", 0)
    time_diff = now - last_time

    if time_diff < user_cooldown:
        next_fish_time = last_time + user_cooldown
        next_timestamp = int(next_fish_time)
        return await interaction.response.send_message(
            f"⏳ Du kannst <t:{next_timestamp}:R> wieder angeln!\n"
            f"🕐 Nächster Wurf: <t:{next_timestamp}:T>",
            ephemeral=True,
        )

    # Cooldown SOFORT setzen
    fishing_data["last_fish_time"] = now
    save_fishing_data(user_id, fishing_data)

    # Köder-Bonus
    has_bait = fishing_data.get("bait", 0) > 0
    bait_text = ""
    if has_bait:
        fishing_data["bait"] -= 1
        bait_text = "\n🪱 **Köder benutzt!** (+30% Wert-Bonus)"

    # Angel-Animation
    rod = FISHING_RODS[fishing_data["rod"]]
    frames = get_fishing_animation()

    embed = discord.Embed(
        title=f"🎣 {interaction.user.display_name} angelt...",
        description=f"**Angel:** {rod['name']}\n\n{frames[0]}",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed)

    for frame in frames[1:]:
        await asyncio.sleep(0.8)
        embed.description = f"**Angel:** {rod['name']}\n\n{frame}"
        await interaction.edit_original_response(embed=embed)

    await asyncio.sleep(0.5)

    # Fang bestimmen
    catch = pick_fish(fishing_data["rod"])

    # Köder-Bonus anwenden
    if has_bait and catch["type"] != "junk":
        catch["value"] = int(catch["value"] * 1.3)

    # Daten aktualisieren
    fishing_data["total_caught"] = fishing_data.get("total_caught", 0) + 1

    rarity_color = FISH_RARITY_COLORS.get(catch["rarity"], COLOR_INFO)
    rarity_name = FISH_RARITY_NAMES.get(catch["rarity"], "Unbekannt")

    # Nächster Wurf Timestamp
    next_fish_time = int(now + user_cooldown)
    cooldown_text = f"\n\n⏱️ Nächster Wurf: <t:{next_fish_time}:R>"

    if catch["type"] == "junk":
        embed = discord.Embed(
            title="🗑️ Müll gefangen!",
            description=(
                f"{catch['emoji']} Du hast **{catch['name']}** aus dem Wasser gezogen.\n\n"
                f"💰 Wert: **{catch['value']} 🪙**\n"
                f"*Naja, besser als nichts...*{bait_text}{cooldown_text}"
            ),
            color=0x95a5a6,
        )
    elif catch["type"] == "treasure":
        embed = discord.Embed(
            title="🏴‍☠️ SCHATZ GEFUNDEN!",
            description=(
                f"{catch['emoji']} Du hast einen **{catch['name']}** gefunden!\n\n"
                f"💰 Wert: **{catch['value']:,} 🪙**\n"
                f"✨ *Was für ein Glücksfund!*{bait_text}{cooldown_text}"
            ),
            color=COLOR_GOLD,
        )
    else:
        if catch["rarity"] == "legendary":
            title = "🌟 LEGENDÄRER FANG! 🌟"
            fishing_data["legendary_caught"] = fishing_data.get("legendary_caught", 0) + 1
        elif catch["rarity"] == "epic":
            title = "🟪 EPISCHER FANG!"
        elif catch["rarity"] == "rare":
            title = "🟦 Seltener Fang!"
        elif catch["rarity"] == "uncommon":
            title = "🟩 Guter Fang!"
        else:
            title = "🐟 Fisch gefangen!"

        embed = discord.Embed(
            title=title,
            description=(
                f"{catch['emoji']} Du hast einen **{catch['name']}** gefangen!\n\n"
                f"💰 Wert: **{catch['value']:,} 🪙**\n"
                f"📊 Seltenheit: {rarity_name}{bait_text}{cooldown_text}"
            ),
            color=rarity_color,
        )

        if catch["value"] > fishing_data.get("biggest_catch", 0):
            fishing_data["biggest_catch"] = catch["value"]
            fishing_data["biggest_catch_name"] = catch["name"]
            embed.add_field(
                name="🏆 Neuer Rekord!",
                value="Das ist dein wertvollster Fang aller Zeiten!",
                inline=False,
            )

    # Inventar updaten
    inventory = fishing_data.get("inventory", {})
    catch_name = catch["name"]
    if catch_name in inventory:
        inventory[catch_name]["count"] = inventory[catch_name].get("count", 0) + 1
        inventory[catch_name]["total_value"] = inventory[catch_name].get("total_value", 0) + catch["value"]
    else:
        inventory[catch_name] = {
            "emoji": catch["emoji"],
            "count": 1,
            "total_value": catch["value"],
            "rarity": catch["rarity"],
        }
    fishing_data["inventory"] = inventory

    # Coins gutschreiben
    fishing_data["total_earned"] = fishing_data.get("total_earned", 0) + catch["value"]
    update_balance(user_id, catch["value"])
    new_balance = get_balance(user_id)

    embed.set_footer(text=f"Kontostand: {new_balance:,} 🪙 • Angel: {rod['name']}")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    save_fishing_data(user_id, fishing_data)

    await interaction.edit_original_response(embed=embed)


@d_bot.tree.command(name="fishstats", description="🎣 Zeigt deine Angel-Statistiken")
@is_fishing_channel()
async def cmd_fishstats(interaction: discord.Interaction):
    fishing_data = get_fishing_data(interaction.user.id)
    rod_key = fishing_data.get("rod", "basic")
    rod = FISHING_RODS[rod_key]

    embed = discord.Embed(
        title=f"🎣 Angel-Statistiken: {interaction.user.display_name}",
        color=COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    embed.add_field(
        name="🎣 Angel",
        value=rod["name"],
        inline=True,
    )
    embed.add_field(
        name="🐟 Gefangen",
        value=f"**{fishing_data.get('total_caught', 0):,}**",
        inline=True,
    )
    embed.add_field(
        name="💰 Verdient",
        value=f"**{fishing_data.get('total_earned', 0):,} 🪙**",
        inline=True,
    )
    embed.add_field(
        name="🏆 Bester Fang",
        value=f"**{fishing_data.get('biggest_catch_name', 'Noch nichts')}** ({fishing_data.get('biggest_catch', 0):,} 🪙)",
        inline=True,
    )
    embed.add_field(
        name="🌟 Legendäre",
        value=f"**{fishing_data.get('legendary_caught', 0)}**",
        inline=True,
    )
    embed.add_field(
        name="🪱 Köder",
        value=f"**{fishing_data.get('bait', 0)}**",
        inline=True,
    )

    cd_level = fishing_data.get("cooldown_level", 0)
    cd_info = COOLDOWN_UPGRADES.get(cd_level, COOLDOWN_UPGRADES[0])
    embed.add_field(
        name="⏱️ Cooldown",
        value=f"**{cd_info['cooldown']}s** ({cd_info['name']})",
        inline=True,
    )

    # Top 5 häufigste Fänge
    inventory = fishing_data.get("inventory", {})
    if inventory:
        sorted_inv = sorted(
            inventory.items(),
            key=lambda x: x[1]["count"],
            reverse=True,
        )[:5]
        inv_lines = []
        for name, data in sorted_inv:
            rarity_name = FISH_RARITY_NAMES.get(data["rarity"], "")
            inv_lines.append(
                f"{data['emoji']} **{name}** × {data['count']} "
                f"({data['total_value']:,} 🪙) {rarity_name}"
            )
        embed.add_field(
            name="📦 Häufigste Fänge",
            value="\n".join(inv_lines),
            inline=False,
        )

    # Nächste Angel anzeigen
    next_rod = None
    rod_order = ["basic", "iron", "gold", "diamond", "legendary"]
    current_idx = rod_order.index(rod_key)
    if current_idx < len(rod_order) - 1:
        next_key = rod_order[current_idx + 1]
        next_rod = FISHING_RODS[next_key]
        balance = get_balance(interaction.user.id)
        progress = make_progress_bar(balance, next_rod["cost"], 10)
        embed.add_field(
            name="⬆️ Nächstes Upgrade",
            value=(
                f"{next_rod['name']} — **{next_rod['cost']:,} 🪙**\n"
                f"{progress}\n"
                f"`/upgrade` zum Kaufen"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="⬆️ Angel",
            value="🌟 Du hast die beste Angel!",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(name="upgrade", description="⬆️ Verbessere deine Angel")
@is_fishing_channel()
async def cmd_upgrade(interaction: discord.Interaction):
    user_id = interaction.user.id
    fishing_data = get_fishing_data(user_id)
    rod_key = fishing_data.get("rod", "basic")

    rod_order = ["basic", "iron", "gold", "diamond", "legendary"]
    current_idx = rod_order.index(rod_key)

    if current_idx >= len(rod_order) - 1:
        return await interaction.response.send_message(
            "🌟 Du hast bereits die beste Angel!",
            ephemeral=True,
        )

    next_key = rod_order[current_idx + 1]
    next_rod = FISHING_RODS[next_key]
    current_rod = FISHING_RODS[rod_key]
    balance = get_balance(user_id)

    if balance < next_rod["cost"]:
        return await interaction.response.send_message(
            f"❌ Du hast nicht genug Coins!\n\n"
            f"**{next_rod['name']}** kostet **{next_rod['cost']:,} 🪙**\n"
            f"💰 Dein Kontostand: **{balance:,} 🪙**\n"
            f"❌ Dir fehlen: **{next_rod['cost'] - balance:,} 🪙**",
            ephemeral=True,
        )

    # Upgrade durchführen
    update_balance(user_id, -next_rod["cost"])
    fishing_data["rod"] = next_key
    save_fishing_data(user_id, fishing_data)

    new_balance = get_balance(user_id)

    embed = discord.Embed(
        title="⬆️ Angel verbessert!",
        description=(
            f"**{current_rod['name']}** → **{next_rod['name']}**\n\n"
            f"📈 **Wert-Bonus:** ×{next_rod['bonus']}\n"
            f"🍀 **Glücks-Bonus:** +{next_rod['luck']}%\n\n"
            f"💰 Bezahlt: **{next_rod['cost']:,} 🪙**"
        ),
        color=COLOR_SUCCESS,
    )
    embed.set_footer(text=f"Kontostand: {new_balance:,} 🪙")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed)

@d_bot.tree.command(name="upgradecooldown", description="⏱️ Verkürze deine Angel-Wartezeit!")
@is_fishing_channel()
async def cmd_upgradecooldown(interaction: discord.Interaction):
    user_id = interaction.user.id
    fishing_data = get_fishing_data(user_id)
    current_level = fishing_data.get("cooldown_level", 0)

    # Max Level?
    max_level = max(COOLDOWN_UPGRADES.keys())
    if current_level >= max_level:
        return await interaction.response.send_message(
            "🌟 Du hast bereits den schnellsten Cooldown! (**15s**)",
            ephemeral=True,
        )

    next_level = current_level + 1
    next_info = COOLDOWN_UPGRADES[next_level]
    current_info = COOLDOWN_UPGRADES[current_level]
    balance = get_balance(user_id)

    if balance < next_info["cost"]:
        return await interaction.response.send_message(
            f"❌ Du hast nicht genug Coins!\n\n"
            f"**{next_info['name']}** kostet **{next_info['cost']:,} 🪙**\n"
            f"💰 Dein Kontostand: **{balance:,} 🪙**\n"
            f"❌ Dir fehlen: **{next_info['cost'] - balance:,} 🪙**",
            ephemeral=True,
        )

    # Upgrade kaufen
    update_balance(user_id, -next_info["cost"])
    fishing_data["cooldown_level"] = next_level
    save_fishing_data(user_id, fishing_data)

    new_balance = get_balance(user_id)

    embed = discord.Embed(
        title="⏱️ Cooldown verbessert!",
        description=(
            f"**{current_info['name']}** → **{next_info['name']}**\n\n"
            f"⏱️ **Cooldown:** {current_info['cooldown']}s → **{next_info['cooldown']}s**\n"
            f"⚡ **{current_info['cooldown'] - next_info['cooldown']}s schneller!**\n\n"
            f"💰 Bezahlt: **{next_info['cost']:,} 🪙**"
        ),
        color=COLOR_SUCCESS,
    )
    embed.set_footer(text=f"Kontostand: {new_balance:,} 🪙")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed)

@d_bot.tree.command(name="buybait", description="🪱 Kaufe Köder für bessere Fänge")
@app_commands.describe(menge="Anzahl der Köder (je 10 🪙)")
@is_fishing_channel()
async def cmd_buybait(interaction: discord.Interaction, menge: int = 10):
    user_id = interaction.user.id

    if menge <= 0:
        return await interaction.response.send_message(
            "❌ Du musst mindestens 1 Köder kaufen!",
            ephemeral=True,
        )

    if menge > 100:
        return await interaction.response.send_message(
            "❌ Du kannst maximal 100 Köder auf einmal kaufen!",
            ephemeral=True,
        )

    total_cost = menge * BAIT_COST
    balance = get_balance(user_id)

    if balance < total_cost:
        return await interaction.response.send_message(
            f"❌ Du hast nicht genug Coins!\n\n"
            f"**{menge}× Köder** kosten **{total_cost:,} 🪙**\n"
            f"💰 Dein Kontostand: **{balance:,} 🪙**",
            ephemeral=True,
        )

    update_balance(user_id, -total_cost)
    fishing_data = get_fishing_data(user_id)
    fishing_data["bait"] = fishing_data.get("bait", 0) + menge
    save_fishing_data(user_id, fishing_data)

    new_balance = get_balance(user_id)

    embed = discord.Embed(
        title="🪱 Köder gekauft!",
        description=(
            f"Du hast **{menge}× Köder** für **{total_cost:,} 🪙** gekauft!\n\n"
            f"🪱 Köder insgesamt: **{fishing_data['bait']}**\n"
            f"💰 Kontostand: **{new_balance:,} 🪙**\n\n"
            f"*Köder werden automatisch beim Angeln benutzt\n"
            f"und geben +30% Wert-Bonus!*"
        ),
        color=COLOR_SUCCESS,
    )

    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="fishtop", description="🏆 Angel-Leaderboard")
@is_fishing_channel()
async def cmd_fishtop(interaction: discord.Interaction):
    bank = load_bank()

    # Alle Angel-Daten sammeln
    fisher_stats = []
    for key, value in bank.items():
        if key.endswith("_fishing") and isinstance(value, dict):
            uid = key.replace("_fishing", "")
            if uid.isdigit():
                fisher_stats.append({
                    "uid": uid,
                    "total_earned": value.get("total_earned", 0),
                    "total_caught": value.get("total_caught", 0),
                    "biggest_catch": value.get("biggest_catch", 0),
                    "biggest_catch_name": value.get("biggest_catch_name", "?"),
                    "legendary_caught": value.get("legendary_caught", 0),
                    "rod": value.get("rod", "basic"),
                })

    if not fisher_stats:
        return await interaction.response.send_message(
            "❌ Noch niemand hat geangelt!",
            ephemeral=True,
        )

    # Nach Gesamtverdienst sortieren
    fisher_stats.sort(key=lambda x: x["total_earned"], reverse=True)

    lines = []
    for i, stats in enumerate(fisher_stats[:10], 1):
        member = interaction.guild.get_member(int(stats["uid"])) if interaction.guild else None
        name = member.display_name if member else f"User {stats['uid']}"
        rod = FISHING_RODS.get(stats["rod"], FISHING_RODS["basic"])

        if i == 1:
            medal = "🥇"
        elif i == 2:
            medal = "🥈"
        elif i == 3:
            medal = "🥉"
        else:
            medal = f"`#{i}`"

        lines.append(
            f"{medal} **{name}** {rod['name']}\n"
            f"╰ 💰 {stats['total_earned']:,} 🪙 • "
            f"🐟 {stats['total_caught']:,} Fänge • "
            f"🌟 {stats['legendary_caught']} Legendäre"
        )

    embed = discord.Embed(
        title="🏆 Angel-Leaderboard",
        description="\n\n".join(lines),
        color=COLOR_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    # Eigene Position
    uid = str(interaction.user.id)
    for i, stats in enumerate(fisher_stats, 1):
        if stats["uid"] == uid:
            embed.set_footer(
                text=f"Deine Position: #{i} mit {stats['total_earned']:,} 🪙 verdient"
            )
            break

    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="shop", description="🏪 Zeigt den Angel-Shop und Twitch-Belohnungen")
@is_fishing_channel()
async def cmd_shop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    fishing_data = get_fishing_data(interaction.user.id)
    current_rod = fishing_data.get("rod", "basic")
    balance = get_balance(interaction.user.id)

    rod_order = ["basic", "iron", "gold", "diamond", "legendary"]

    # ── Seite 1: Angel-Shop ──────────────────────────────

    embed = discord.Embed(
        title="🏪 Shop",
        description=f"💰 Dein Kontostand: **{balance:,} 🪙**",
        color=COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    # Angeln
    rod_lines = []
    for key in rod_order:
        rod = FISHING_RODS[key]
        if key == current_rod:
            status = "✅ Ausgerüstet"
        elif rod_order.index(key) < rod_order.index(current_rod):
            status = "✅ Besitzt"
        elif balance >= rod["cost"]:
            status = "💰 Kaufbar"
        else:
            status = f"❌ {rod['cost']:,} 🪙"

        rod_lines.append(
            f"{rod['name']} — {status}\n"
            f"╰ Bonus: ×{rod['bonus']} Wert • +{rod['luck']}% Glück"
        )

    embed.add_field(
        name="🎣 Angeln",
        value="\n\n".join(rod_lines),
        inline=False,
    )

        # Cooldown Upgrades
    cd_level = fishing_data.get("cooldown_level", 0)
    cd_lines = []
    for level, info in COOLDOWN_UPGRADES.items():
        if level == 0:
            continue
        if level <= cd_level:
            status = "✅ Gekauft"
        elif level == cd_level + 1:
            if balance >= info["cost"]:
                status = "💰 Kaufbar"
            else:
                status = f"❌ {info['cost']:,} 🪙"
        else:
            status = f"🔒 {info['cost']:,} 🪙"

        cd_lines.append(
            f"{info['name']} — {status}\n"
            f"╰ Cooldown: **{info['cooldown']}s** • `/upgradecooldown`"
        )

    current_cd = COOLDOWN_UPGRADES.get(cd_level, COOLDOWN_UPGRADES[0])
    embed.add_field(
        name=f"⏱️ Cooldown-Upgrades (Aktuell: {current_cd['cooldown']}s)",
        value="\n\n".join(cd_lines) if cd_lines else "Alle Upgrades gekauft!",
        inline=False,
    )

    embed.add_field(
        name="🪱 Köder",
        value=(
            f"**{BAIT_COST} 🪙** pro Stück\n"
            f"╰ +30% Wert-Bonus auf Fänge\n"
            f"╰ `/buybait <menge>` zum Kaufen\n"
            f"╰ Du hast: **{fishing_data.get('bait', 0)}** Köder"
        ),
        inline=False,
    )

    # ── Twitch Belohnungen ───────────────────────────────

    is_live = await check_streamer_live()
    rewards = await get_cached_rewards()

    if rewards:
        live_indicator = "🟢 LIVE" if is_live else "🔴 OFFLINE"

        reward_lines = []
        for i, reward in enumerate(rewards, 1):
            affordable = "✅" if balance >= reward["cost"] else "❌"
            paused = " ⏸️" if reward.get("is_paused") else ""

            reward_lines.append(
                f"{affordable} **{reward['title']}**{paused}\n"
                f"╰ 💰 {reward['cost']:,} 🪙 • `/redeem {i}`"
            )

        embed.add_field(
            name=f"🎯 Twitch Belohnungen ({live_indicator})",
            value="\n\n".join(reward_lines) if reward_lines else "*Keine Belohnungen verfügbar*",
            inline=False,
        )

        if not is_live:
            embed.add_field(
                name="⚠️ Hinweis",
                value="*Twitch-Belohnungen können nur eingelöst werden wenn der Streamer **LIVE** ist!*",
                inline=False,
            )
    else:
        embed.add_field(
            name="🎯 Twitch Belohnungen",
            value="*Keine Belohnungen verfügbar oder nicht konfiguriert.*",
            inline=False,
        )

    embed.add_field(
        name="📋 Befehle",
        value=(
            "`/fish` — Angeln\n"
            "`/upgrade` — Angel verbessern\n"
            "`/buybait <menge>` — Köder kaufen\n"
            "`/redeem <nummer>` — Twitch-Belohnung einlösen\n"
            "`/fishstats` — Statistiken\n"
            "`/fishtop` — Leaderboard"
        ),
        inline=False,
    )

    await interaction.followup.send(embed=embed, ephemeral=True)

#reedeem
@d_bot.tree.command(name="redeem", description="🎯 Löse eine Twitch-Belohnung mit Coins ein")
@app_commands.describe(nummer="Nummer der Belohnung aus /shop")
@is_fishing_channel()
async def cmd_redeem(interaction: discord.Interaction, nummer: int):
    await interaction.response.defer()

    user_id = interaction.user.id
    balance = get_balance(user_id)

    # Live-Check
    is_live = await check_streamer_live()
    if not is_live:
        embed = discord.Embed(
            title="🔴 Streamer ist offline!",
            description=(
                f"Twitch-Belohnungen können nur eingelöst werden "
                f"wenn **{STREAMER_CHANNEL}** **LIVE** ist!\n\n"
                f"💰 Deine Coins wurden **nicht** abgezogen."
            ),
            color=COLOR_ERROR,
        )
        return await interaction.followup.send(embed=embed)

    # Rewards laden
    rewards = await get_cached_rewards()
    if not rewards:
        return await interaction.followup.send(
            "❌ Keine Twitch-Belohnungen verfügbar!",
            ephemeral=True,
        )

    # Nummer prüfen
    if nummer < 1 or nummer > len(rewards):
        return await interaction.followup.send(
            f"❌ Ungültige Nummer! Nutze `/shop` um die verfügbaren Belohnungen zu sehen.\n"
            f"Gültige Nummern: **1 – {len(rewards)}**",
            ephemeral=True,
        )

    reward = rewards[nummer - 1]

    # Pausiert?
    if reward.get("is_paused"):
        return await interaction.followup.send(
            f"⏸️ **{reward['title']}** ist momentan pausiert und kann nicht eingelöst werden!",
            ephemeral=True,
        )

    # Coins prüfen
    if balance < reward["cost"]:
        return await interaction.followup.send(
            f"❌ Du hast nicht genug Coins!\n\n"
            f"**{reward['title']}** kostet **{reward['cost']:,} 🪙**\n"
            f"💰 Dein Kontostand: **{balance:,} 🪙**\n"
            f"❌ Dir fehlen: **{reward['cost'] - balance:,} 🪙**",
            ephemeral=True,
        )

    # Coins abziehen
    update_balance(user_id, -reward["cost"])
    new_balance = get_balance(user_id)

    # Twitch-Chat Nachricht senden
    twitch_sent = False
    if twitch_bot_ref and STREAMER_CHANNEL:
        try:
            # Den Twitch-Kanal finden
            channel = twitch_bot_ref.get_channel(STREAMER_CHANNEL)
            if channel:
                # Discord-Name oder Twitch-Name holen
                data = load_data()
                uid = str(user_id)
                if uid in data["users"]:
                    display_name = data["users"][uid].get("twitch_name", interaction.user.display_name)
                else:
                    display_name = interaction.user.display_name

                await channel.send(
                    f"🎯 {display_name} hat für {reward['cost']:,} Coins "
                    f"「{reward['title']}」 eingelöst!"
                )
                twitch_sent = True
                print(
                    f"🎯 [Redeem] {display_name} hat '{reward['title']}' "
                    f"für {reward['cost']:,} Coins eingelöst"
                )
        except Exception as e:
            print(f"❌ Twitch-Chat Nachricht fehlgeschlagen: {e}")

    # Erfolgs-Embed
    embed = discord.Embed(
        title="🎯 Belohnung eingelöst!",
        description=(
            f"Du hast **{reward['title']}** für "
            f"**{reward['cost']:,} 🪙** eingelöst!"
        ),
        color=COLOR_SUCCESS,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    embed.add_field(
        name="🎁 Belohnung",
        value=f"**{reward['title']}**",
        inline=True,
    )
    embed.add_field(
        name="💰 Bezahlt",
        value=f"**{reward['cost']:,} 🪙**",
        inline=True,
    )
    embed.add_field(
        name="💳 Neuer Kontostand",
        value=f"**{new_balance:,} 🪙**",
        inline=True,
    )

    if reward.get("prompt"):
        embed.add_field(
            name="📝 Beschreibung",
            value=reward["prompt"][:200],
            inline=False,
        )

    if twitch_sent:
        embed.add_field(
            name="📺 Twitch-Chat",
            value="✅ Nachricht wurde im Chat gesendet!",
            inline=False,
        )
    else:
        embed.add_field(
            name="📺 Twitch-Chat",
            value="⚠️ Chat-Nachricht konnte nicht gesendet werden.",
            inline=False,
        )

    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(
        text=f"Eingelöst von {interaction.user.display_name} • 1 Kanalpunkt = 1 Coin"
    )

    await interaction.followup.send(embed=embed)

    # Log in Discord senden
    log_embed = discord.Embed(
        title="🎯 Twitch-Belohnung eingelöst",
        description=(
            f"{interaction.user.mention} hat eine Twitch-Belohnung "
            f"mit Coins eingelöst."
        ),
        color=COLOR_TWITCH,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    log_embed.add_field(name="👤 User", value=interaction.user.mention, inline=True)
    log_embed.add_field(name="🎁 Belohnung", value=reward["title"], inline=True)
    log_embed.add_field(name="💰 Preis", value=f"{reward['cost']:,} 🪙", inline=True)
    log_embed.add_field(name="📺 Chat gesendet", value="✅" if twitch_sent else "❌", inline=True)
    log_embed.set_thumbnail(url=interaction.user.display_avatar.url)

    await send_log(log_embed, d_bot)

# ══════════════════════════════════════════════════════════
#         GAMBLING SYSTEM – DATENVERWALTUNG
# ══════════════════════════════════════════════════════════

SLOT_EMOJIS = ["🍒", "🍋", "🍇", "💎", "🎰", "🔔", "🍎", "⭐", "7️⃣"]

# ══════════════════════════════════════════════════════════
#              ERWEITERTE GAMBLING KONSTANTEN
# ══════════════════════════════════════════════════════════

# Blackjack
CARD_VALUES = {
    'A': 11, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7,
    '8': 8, '9': 9, '10': 10, 'J': 10, 'Q': 10, 'K': 10,
}
CARD_SUITS = ['♠️', '♥️', '♦️', '♣️']
CARD_NAMES = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']

# Pferderennen
HORSES = [
    {"name": "Blitz", "emoji": "🐎"},
    {"name": "Einhorn", "emoji": "🦄"},
    {"name": "Traktor", "emoji": "🚜"},
    {"name": "Hund", "emoji": "🐕"},
    {"name": "Schwein", "emoji": "🐖"},
]
RACE_TRACK_LENGTH = 15

# Roulette
ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
ROULETTE_BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

# Scratch Cards
SCRATCH_SYMBOLS = ["🍒", "🍋", "🍇", "💎", "⭐", "7️⃣", "🔔", "🍎", "👑"]

# Vault
VAULT_FILE = "vault.json"

# Aktive Spiele tracken (gegen Spam/Exploits)
active_games: dict[int, str] = {}  # user_id -> game_name

# ══════════════════════════════════════════════════════════
#              VAULT (TRESOR) DATENVERWALTUNG
# ══════════════════════════════════════════════════════════

def load_vault() -> dict:
    try:
        with open(VAULT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"jackpot": 1000, "code": None, "attempts": 0}


def save_vault(vault: dict) -> None:
    try:
        with open(VAULT_FILE, 'w', encoding='utf-8') as f:
            json.dump(vault, f, indent=4)
    except Exception as e:
        print(f"❌ Vault Speicherfehler: {e}")


def generate_vault_code() -> str:
    return f"{random.randint(0, 999):03d}"


def get_vault_data() -> dict:
    vault = load_vault()
    if vault.get("code") is None:
        vault["code"] = generate_vault_code()
        vault["jackpot"] = max(vault.get("jackpot", 1000), 1000)
        save_vault(vault)
    return vault

# ══════════════════════════════════════════════════════════
#              BLACKJACK HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════

def create_deck() -> list:
    deck = []
    for suit in CARD_SUITS:
        for name in CARD_NAMES:
            deck.append({"name": name, "suit": suit})
    random.shuffle(deck)
    return deck


def card_str(card: dict) -> str:
    return f"`{card['name']}{card['suit']}`"


def hand_str(hand: list) -> str:
    return " ".join(card_str(c) for c in hand)


def hand_value(hand: list) -> int:
    value = sum(CARD_VALUES[c["name"]] for c in hand)
    aces = sum(1 for c in hand if c["name"] == "A")
    while value > 21 and aces > 0:
        value -= 10
        aces -= 1
    return value

# ══════════════════════════════════════════════════════════
#              ANGEL SYSTEM – KONFIGURATION
# ══════════════════════════════════════════════════════════

FISH_TYPES = [
    # (Name, Emoji, Wert Min, Wert Max, Gewicht/Rarität)
    # Gewöhnlich (60% Chance)
    {"name": "Sardine", "emoji": "🐟", "min": 5, "max": 15, "rarity": "common", "weight": 20},
    {"name": "Hering", "emoji": "🐟", "min": 8, "max": 20, "rarity": "common", "weight": 20},
    {"name": "Makrele", "emoji": "🐟", "min": 10, "max": 25, "rarity": "common", "weight": 15},
    {"name": "Barsch", "emoji": "🐠", "min": 15, "max": 35, "rarity": "common", "weight": 10},
    # Ungewöhnlich (25% Chance)
    {"name": "Lachs", "emoji": "🐠", "min": 30, "max": 60, "rarity": "uncommon", "weight": 8},
    {"name": "Forelle", "emoji": "🐠", "min": 35, "max": 70, "rarity": "uncommon", "weight": 7},
    {"name": "Thunfisch", "emoji": "🐠", "min": 40, "max": 80, "rarity": "uncommon", "weight": 5},
    {"name": "Karpfen", "emoji": "🐠", "min": 45, "max": 85, "rarity": "uncommon", "weight": 5},
    # Selten (10% Chance)
    {"name": "Schwertfisch", "emoji": "🦈", "min": 80, "max": 150, "rarity": "rare", "weight": 3},
    {"name": "Kugelfisch", "emoji": "🐡", "min": 100, "max": 200, "rarity": "rare", "weight": 2},
    {"name": "Oktopus", "emoji": "🐙", "min": 120, "max": 250, "rarity": "rare", "weight": 2},
    # Episch (4% Chance)
    {"name": "Hai", "emoji": "🦈", "min": 200, "max": 500, "rarity": "epic", "weight": 1},
    {"name": "Wal", "emoji": "🐋", "min": 300, "max": 600, "rarity": "epic", "weight": 1},
    # Legendär (1% Chance)
    {"name": "Goldener Koi", "emoji": "✨", "min": 500, "max": 1500, "rarity": "legendary", "weight": 0.3},
    {"name": "Diamant-Delfin", "emoji": "💎", "min": 1000, "max": 3000, "rarity": "legendary", "weight": 0.2},
]

FISH_RARITY_COLORS = {
    "common": 0x95a5a6,       # Grau
    "uncommon": 0x2ecc71,     # Grün
    "rare": 0x3498db,         # Blau
    "epic": 0x9b59b6,         # Lila
    "legendary": 0xf1c40f,    # Gold
}

FISH_RARITY_NAMES = {
    "common": "⬜ Gewöhnlich",
    "uncommon": "🟩 Ungewöhnlich",
    "rare": "🟦 Selten",
    "epic": "🟪 Episch",
    "legendary": "🟨 Legendär",
}

JUNK_ITEMS = [
    {"name": "Alter Schuh", "emoji": "👟", "value": 1},
    {"name": "Leere Dose", "emoji": "🥫", "value": 2},
    {"name": "Seetang", "emoji": "🌿", "value": 3},
    {"name": "Zerbrochene Flasche", "emoji": "🍾", "value": 1},
    {"name": "Plastiktüte", "emoji": "🛍️", "value": 0},
    {"name": "Alte Zeitung", "emoji": "📰", "value": 1},
    {"name": "Rostige Büchse", "emoji": "🪣", "value": 2},
]

TREASURE_ITEMS = [
    {"name": "Goldmünze", "emoji": "🪙", "min": 50, "max": 150},
    {"name": "Schatztruhe", "emoji": "🧰", "min": 200, "max": 500},
    {"name": "Antike Vase", "emoji": "🏺", "min": 100, "max": 300},
    {"name": "Diamantring", "emoji": "💍", "min": 300, "max": 800},
    {"name": "Goldbarren", "emoji": "🥇", "min": 500, "max": 1500},
]

FISHING_RODS = {
    "basic": {"name": "🪵 Holzangel", "cost": 0, "bonus": 1.0, "luck": 0},
    "iron": {"name": "⚙️ Eisenangel", "cost": 2000, "bonus": 1.3, "luck": 5},
    "gold": {"name": "✨ Goldangel", "cost": 10000, "bonus": 1.6, "luck": 10},
    "diamond": {"name": "💎 Diamantangel", "cost": 50000, "bonus": 2.0, "luck": 20},
    "legendary": {"name": "🌟 Legendäre Angel", "cost": 200000, "bonus": 3.0, "luck": 35},
}

FISHING_COOLDOWN = 30  # Standard-Cooldown
BAIT_COST = 10

COOLDOWN_UPGRADES = {
    0: {"name": "⏱️ Standard", "cooldown": 30, "cost": 0},
    1: {"name": "⏱️ Schnell", "cooldown": 25, "cost": 15000},
    2: {"name": "⚡ Turbo-Schnell", "cooldown": 20, "cost": 50000},
    3: {"name": "🌟 Blitz-Schnell", "cooldown": 15, "cost": 150000},
}

# ══════════════════════════════════════════════════════════
#              JOBS / MINIGAMES KONFIGURATION
# ══════════════════════════════════════════════════════════

# Job 1: Holzfäller
WOODCUTTING_TREES = [
    {"name": "Birke", "emoji": "🌳", "min": 5, "max": 15, "rarity": "common", "weight": 25},
    {"name": "Eiche", "emoji": "🌳", "min": 10, "max": 25, "rarity": "common", "weight": 20},
    {"name": "Kiefer", "emoji": "🌲", "min": 15, "max": 35, "rarity": "uncommon", "weight": 15},
    {"name": "Mahagoni", "emoji": "🌲", "min": 30, "max": 60, "rarity": "uncommon", "weight": 10},
    {"name": "Teak", "emoji": "🪵", "min": 50, "max": 100, "rarity": "rare", "weight": 5},
    {"name": "Ebenholz", "emoji": "🪵", "min": 80, "max": 180, "rarity": "rare", "weight": 3},
    {"name": "Goldene Eiche", "emoji": "✨", "min": 150, "max": 400, "rarity": "epic", "weight": 1.5},
    {"name": "Weltenbaum", "emoji": "🌟", "min": 400, "max": 1200, "rarity": "legendary", "weight": 0.3},
]

WOODCUTTING_AXES = {
    "stone": {"name": "🪨 Steinaxt", "cost": 0, "bonus": 1.0, "luck": 0},
    "iron": {"name": "⚙️ Eisenaxt", "cost": 2000, "bonus": 1.3, "luck": 5},
    "gold": {"name": "✨ Goldaxt", "cost": 10000, "bonus": 1.6, "luck": 10},
    "diamond": {"name": "💎 Diamantaxt", "cost": 50000, "bonus": 2.0, "luck": 20},
    "legendary": {"name": "🌟 Legendäre Axt", "cost": 200000, "bonus": 3.0, "luck": 35},
}

WOODCUTTING_COOLDOWN_UPGRADES = {
    0: {"name": "⏱️ Standard", "cooldown": 35, "cost": 0},
    1: {"name": "⏱️ Schnell", "cooldown": 28, "cost": 15000},
    2: {"name": "⚡ Turbo", "cooldown": 22, "cost": 50000},
    3: {"name": "🌟 Blitz", "cooldown": 16, "cost": 150000},
}

# Job 2: Bergbau
MINING_ORES = [
    {"name": "Kohle", "emoji": "⬛", "min": 5, "max": 15, "rarity": "common", "weight": 25},
    {"name": "Kupfer", "emoji": "🟤", "min": 10, "max": 25, "rarity": "common", "weight": 20},
    {"name": "Eisen", "emoji": "⚙️", "min": 15, "max": 35, "rarity": "uncommon", "weight": 15},
    {"name": "Silber", "emoji": "⬜", "min": 30, "max": 65, "rarity": "uncommon", "weight": 10},
    {"name": "Gold", "emoji": "🟡", "min": 60, "max": 120, "rarity": "rare", "weight": 5},
    {"name": "Platin", "emoji": "💠", "min": 100, "max": 220, "rarity": "rare", "weight": 3},
    {"name": "Smaragd", "emoji": "💚", "min": 200, "max": 500, "rarity": "epic", "weight": 1.5},
    {"name": "Diamant", "emoji": "💎", "min": 500, "max": 1500, "rarity": "legendary", "weight": 0.3},
]

MINING_PICKAXES = {
    "stone": {"name": "🪨 Steinspitzhacke", "cost": 0, "bonus": 1.0, "luck": 0},
    "iron": {"name": "⚙️ Eisenspitzhacke", "cost": 2000, "bonus": 1.3, "luck": 5},
    "gold": {"name": "✨ Goldspitzhacke", "cost": 10000, "bonus": 1.6, "luck": 10},
    "diamond": {"name": "💎 Diamantspitzhacke", "cost": 50000, "bonus": 2.0, "luck": 20},
    "legendary": {"name": "🌟 Legendäre Spitzhacke", "cost": 200000, "bonus": 3.0, "luck": 35},
}

MINING_COOLDOWN_UPGRADES = {
    0: {"name": "⏱️ Standard", "cooldown": 40, "cost": 0},
    1: {"name": "⏱️ Schnell", "cooldown": 32, "cost": 15000},
    2: {"name": "⚡ Turbo", "cooldown": 25, "cost": 50000},
    3: {"name": "🌟 Blitz", "cooldown": 18, "cost": 150000},
}

# Job 3: Jagen
HUNTING_ANIMALS = [
    {"name": "Hase", "emoji": "🐰", "min": 8, "max": 20, "rarity": "common", "weight": 25},
    {"name": "Fuchs", "emoji": "🦊", "min": 15, "max": 35, "rarity": "common", "weight": 18},
    {"name": "Hirsch", "emoji": "🦌", "min": 25, "max": 55, "rarity": "uncommon", "weight": 12},
    {"name": "Wolf", "emoji": "🐺", "min": 40, "max": 80, "rarity": "uncommon", "weight": 10},
    {"name": "Bär", "emoji": "🐻", "min": 70, "max": 150, "rarity": "rare", "weight": 5},
    {"name": "Adler", "emoji": "🦅", "min": 100, "max": 250, "rarity": "rare", "weight": 3},
    {"name": "Weißer Tiger", "emoji": "🐯", "min": 250, "max": 600, "rarity": "epic", "weight": 1.5},
    {"name": "Goldener Drache", "emoji": "🐉", "min": 600, "max": 1800, "rarity": "legendary", "weight": 0.2},
]

HUNTING_BOWS = {
    "wooden": {"name": "🏹 Holzbogen", "cost": 0, "bonus": 1.0, "luck": 0},
    "iron": {"name": "⚙️ Eisenbogen", "cost": 2000, "bonus": 1.3, "luck": 5},
    "gold": {"name": "✨ Goldbogen", "cost": 10000, "bonus": 1.6, "luck": 10},
    "diamond": {"name": "💎 Diamantbogen", "cost": 50000, "bonus": 2.0, "luck": 20},
    "legendary": {"name": "🌟 Legendärer Bogen", "cost": 200000, "bonus": 3.0, "luck": 35},
}

HUNTING_COOLDOWN_UPGRADES = {
    0: {"name": "⏱️ Standard", "cooldown": 45, "cost": 0},
    1: {"name": "⏱️ Schnell", "cooldown": 35, "cost": 15000},
    2: {"name": "⚡ Turbo", "cooldown": 27, "cost": 50000},
    3: {"name": "🌟 Blitz", "cooldown": 20, "cost": 150000},
}

# Job 4: Schmieden (Skill-basiert – Reaktionsspiel)
SMITHING_ITEMS = [
    {"name": "Eisennagel", "emoji": "🔩", "base_value": 15, "difficulty": "easy"},
    {"name": "Kette", "emoji": "⛓️", "base_value": 30, "difficulty": "easy"},
    {"name": "Dolch", "emoji": "🗡️", "base_value": 50, "difficulty": "medium"},
    {"name": "Schwert", "emoji": "⚔️", "base_value": 80, "difficulty": "medium"},
    {"name": "Schild", "emoji": "🛡️", "base_value": 120, "difficulty": "hard"},
    {"name": "Rüstung", "emoji": "🦺", "base_value": 180, "difficulty": "hard"},
    {"name": "Kronleuchter", "emoji": "👑", "base_value": 300, "difficulty": "master"},
]

SMITHING_HAMMERS = {
    "stone": {"name": "🪨 Steinhammer", "cost": 0, "bonus": 1.0, "quality_bonus": 0},
    "iron": {"name": "⚙️ Eisenhammer", "cost": 3000, "bonus": 1.3, "quality_bonus": 5},
    "gold": {"name": "✨ Goldhammer", "cost": 12000, "bonus": 1.6, "quality_bonus": 10},
    "diamond": {"name": "💎 Diamanthammer", "cost": 60000, "bonus": 2.0, "quality_bonus": 20},
    "legendary": {"name": "🌟 Meisterhammer", "cost": 250000, "bonus": 3.0, "quality_bonus": 35},
}

SMITHING_COOLDOWN_UPGRADES = {
    0: {"name": "⏱️ Standard", "cooldown": 60, "cost": 0},
    1: {"name": "⏱️ Schnell", "cooldown": 45, "cost": 20000},
    2: {"name": "⚡ Turbo", "cooldown": 35, "cost": 60000},
    3: {"name": "🌟 Blitz", "cooldown": 25, "cost": 180000},
}

SMITHING_QUALITY = {
    "schlecht": {"emoji": "💩", "multiplier": 0.3, "name": "Schrott"},
    "normal": {"emoji": "⚪", "multiplier": 0.7, "name": "Normal"},
    "gut": {"emoji": "🟢", "multiplier": 1.0, "name": "Gut"},
    "excellent": {"emoji": "🔵", "multiplier": 1.5, "name": "Exzellent"},
    "meisterwerk": {"emoji": "🟣", "multiplier": 2.5, "name": "Meisterwerk"},
    "legendär": {"emoji": "🟡", "multiplier": 4.0, "name": "Legendär"},
}

# Job 5: Kochen (Skill-basiert – Rezepte merken)
COOKING_RECIPES = [
    {"name": "Spiegelei", "emoji": "🍳", "base_value": 10, "ingredients": ["🥚", "🧈"], "difficulty": "easy"},
    {"name": "Sandwich", "emoji": "🥪", "base_value": 20, "ingredients": ["🍞", "🧀", "🥬"], "difficulty": "easy"},
    {"name": "Suppe", "emoji": "🍲", "base_value": 35, "ingredients": ["🥕", "🧅", "🥔", "💧"], "difficulty": "medium"},
    {"name": "Pizza", "emoji": "🍕", "base_value": 55, "ingredients": ["🍞", "🧀", "🍅", "🌿"], "difficulty": "medium"},
    {"name": "Sushi", "emoji": "🍣", "base_value": 80, "ingredients": ["🍚", "🐟", "🥑", "🌿", "🫚"], "difficulty": "hard"},
    {"name": "Steak Dinner", "emoji": "🥩", "base_value": 120, "ingredients": ["🥩", "🧈", "🧄", "🌿", "🥔", "🍷"], "difficulty": "hard"},
    {"name": "Goldenes Bankett", "emoji": "👑", "base_value": 250, "ingredients": ["🥩", "🐟", "🧀", "🍷", "🌿", "🧄", "🍞"], "difficulty": "master"},
]

COOKING_TOOLS = {
    "basic": {"name": "🍳 Bratpfanne", "cost": 0, "bonus": 1.0, "time_bonus": 0},
    "iron": {"name": "⚙️ Eisentopf", "cost": 3000, "bonus": 1.3, "time_bonus": 2},
    "gold": {"name": "✨ Goldkessel", "cost": 12000, "bonus": 1.6, "time_bonus": 4},
    "diamond": {"name": "💎 Diamant-Wok", "cost": 60000, "bonus": 2.0, "time_bonus": 6},
    "legendary": {"name": "🌟 Meisterkoch-Set", "cost": 250000, "bonus": 3.0, "time_bonus": 10},
}

COOKING_COOLDOWN_UPGRADES = {
    0: {"name": "⏱️ Standard", "cooldown": 50, "cost": 0},
    1: {"name": "⏱️ Schnell", "cooldown": 40, "cost": 20000},
    2: {"name": "⚡ Turbo", "cooldown": 30, "cost": 60000},
    3: {"name": "🌟 Blitz", "cooldown": 20, "cost": 180000},
}

ALL_INGREDIENTS = ["🥚", "🧈", "🍞", "🧀", "🥬", "🥕", "🧅", "🥔", "💧",
                    "🍅", "🌿", "🍚", "🐟", "🥑", "🫚", "🥩", "🧄", "🍷",
                    "🌶️", "🍋", "🫒", "🥛", "🍯"]

# ══════════════════════════════════════════════════════════
#              ANGEL SYSTEM – HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════

def get_fishing_data(user_id: int) -> dict:
    """Lädt Angel-Daten eines Users aus der Bank."""
    bank = load_bank()
    uid = str(user_id)
    fishing_key = f"{uid}_fishing"

    if fishing_key not in bank:
        bank[fishing_key] = {
            "rod": "basic",
            "cooldown_level": 0,
            "total_caught": 0,
            "total_earned": 0,
            "biggest_catch": 0,
            "biggest_catch_name": "Noch nichts",
            "legendary_caught": 0,
            "last_fish_time": 0,
            "bait": 0,
            "inventory": {},
        }
        save_bank(bank)

    # Migration: cooldown_level hinzufügen falls nicht vorhanden
    if "cooldown_level" not in bank[fishing_key]:
        bank[fishing_key]["cooldown_level"] = 0
        save_bank(bank)

    return bank[fishing_key]


def get_user_cooldown(fishing_data: dict) -> int:
    """Gibt den aktuellen Cooldown des Users in Sekunden zurück."""
    level = fishing_data.get("cooldown_level", 0)
    return COOLDOWN_UPGRADES.get(level, COOLDOWN_UPGRADES[0])["cooldown"]

# ══════════════════════════════════════════════════════════
#              JOBS – GENERISCHE DATENVERWALTUNG
# ══════════════════════════════════════════════════════════

def get_job_data(user_id: int, job_key: str) -> dict:
    """Lädt Job-Daten eines Users."""
    bank = load_bank()
    uid = str(user_id)
    key = f"{uid}_{job_key}"
    if key not in bank:
        bank[key] = {
            "tool": list({"woodcutting": WOODCUTTING_AXES, "mining": MINING_PICKAXES,
                          "hunting": HUNTING_BOWS, "smithing": SMITHING_HAMMERS,
                          "cooking": COOKING_TOOLS}.get(job_key, {}).keys())[0],
            "cooldown_level": 0,
            "total_earned": 0,
            "total_actions": 0,
            "best_value": 0,
            "best_name": "Noch nichts",
            "last_action_time": 0,
            "legendary_count": 0,
        }
        save_bank(bank)
    return bank[key]


def save_job_data(user_id: int, job_key: str, job_data: dict) -> None:
    bank = load_bank()
    uid = str(user_id)
    bank[f"{uid}_{job_key}"] = job_data
    save_bank(bank)


def get_job_cooldown(job_data: dict, cooldown_upgrades: dict) -> int:
    level = job_data.get("cooldown_level", 0)
    return cooldown_upgrades.get(level, cooldown_upgrades[0])["cooldown"]


def pick_job_item(items: list, tool_key: str, tools: dict) -> dict:
    """Wählt ein Item basierend auf Gewicht und Tool-Bonus."""
    tool = tools[tool_key]
    luck = tool["luck"]
    weights = []
    for item in items:
        w = item["weight"]
        if item["rarity"] in ("rare", "epic", "legendary"):
            w += luck / 10
        weights.append(w)
    chosen = random.choices(items, weights=weights, k=1)[0]
    value = random.randint(chosen["min"], chosen["max"])
    value = int(value * tool["bonus"])
    return {
        "name": chosen["name"],
        "emoji": chosen["emoji"],
        "value": value,
        "rarity": chosen["rarity"],
    }


def save_fishing_data(user_id: int, fishing_data: dict) -> None:
    """Speichert Angel-Daten eines Users."""
    bank = load_bank()
    uid = str(user_id)
    bank[f"{uid}_fishing"] = fishing_data
    save_bank(bank)


def pick_fish(rod_key: str) -> dict:
    """Wählt einen Fisch basierend auf Gewicht und Angel-Bonus."""
    rod = FISHING_RODS[rod_key]
    luck_bonus = rod["luck"]

    # Erst bestimmen was passiert: Müll, Fisch oder Schatz
    roll = random.uniform(0, 100)

    # Schatz-Chance: 3% + luck_bonus/5
    treasure_chance = 3 + luck_bonus / 5
    # Müll-Chance: 15% - luck_bonus/3
    junk_chance = max(2, 15 - luck_bonus / 3)

    if roll < treasure_chance:
        # Schatz gefunden!
        treasure = random.choice(TREASURE_ITEMS)
        value = random.randint(treasure["min"], treasure["max"])
        value = int(value * rod["bonus"])
        return {
            "type": "treasure",
            "name": treasure["name"],
            "emoji": treasure["emoji"],
            "value": value,
            "rarity": "legendary",
        }
    elif roll < treasure_chance + junk_chance:
        # Müll gefangen
        junk = random.choice(JUNK_ITEMS)
        return {
            "type": "junk",
            "name": junk["name"],
            "emoji": junk["emoji"],
            "value": junk["value"],
            "rarity": "common",
        }
    else:
        # Fisch fangen – gewichtet
        weights = []
        for fish in FISH_TYPES:
            weight = fish["weight"]
            # Bessere Angel = höhere Chance auf seltene Fische
            if fish["rarity"] in ("rare", "epic", "legendary"):
                weight += luck_bonus / 10
            weights.append(weight)

        chosen_fish = random.choices(FISH_TYPES, weights=weights, k=1)[0]
        value = random.randint(chosen_fish["min"], chosen_fish["max"])
        value = int(value * rod["bonus"])

        return {
            "type": "fish",
            "name": chosen_fish["name"],
            "emoji": chosen_fish["emoji"],
            "value": value,
            "rarity": chosen_fish["rarity"],
        }


def get_fishing_animation() -> list[str]:
    """Gibt zufällige Angel-Animations-Frames zurück."""
    animations = [
        ["🌊🎣", "🌊💨🎣", "🌊🐟💨🎣", "🌊🐟🎣"],
        ["🎣🌊", "🎣💦🌊", "🎣🐠💦🌊", "🎣🐠🌊"],
        ["🪝🌊", "🪝💧🌊", "🪝🫧🌊", "🪝🐟🌊"],
    ]
    return random.choice(animations)

def load_bank() -> dict:
    try:
        with open(GAMBLING_BANK_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_bank(bank: dict) -> None:
    try:
        with open(GAMBLING_BANK_FILE, 'w', encoding='utf-8') as f:
            json.dump(bank, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Fehler beim Speichern der Bank: {e}")


def get_balance(user_id: int) -> int:
    bank = load_bank()
    return bank.get(str(user_id), 1000)


def set_balance(user_id: int, amount: int) -> int:
    bank = load_bank()
    bank[str(user_id)] = amount
    save_bank(bank)
    return amount


def update_balance(user_id: int, amount: int) -> int:
    bank = load_bank()
    uid = str(user_id)
    bank[uid] = bank.get(uid, 1000) + amount
    save_bank(bank)
    return bank[uid]
# ══════════════════════════════════════════════════════════
#              ACTIVITY TRACKER HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════

def make_progress_bar(
    value: float, max_val: float, length: int = 10
) -> str:
    if max_val <= 0:
        percentage = 0.0
    else:
        percentage = min(value / max_val, 1.0)
    filled = round(length * percentage)
    empty = length - filled
    bar = "█" * filled + "░" * empty
    pct = percentage * 100
    return f"`{bar}` **{pct:.0f}%**"


def make_streak_calendar(
    data: dict, uid: str, year: int, month: int
) -> str:
    cal = calendar.Calendar(firstweekday=0)
    days = cal.itermonthdays(year, month)
    today_str = str(datetime.date.today())
    lines = ["`Mo Di Mi Do Fr Sa So`"]
    week = []
    for day in days:
        if day == 0:
            week.append("  ")
        else:
            date_str = f"{year}-{month:02d}-{day:02d}"
            if (
                date_str in data["streams"]
                and uid in data["streams"][date_str]
            ):
                week.append("✅")
            elif date_str == today_str:
                week.append("🔵")
            elif (
                date_str < today_str
                and date_str in data["streams"]
            ):
                week.append("❌")
            elif date_str > today_str:
                week.append("⬜")
            else:
                week.append("➖")
        if len(week) == 7:
            lines.append(" ".join(week))
            week = []
    if week:
        while len(week) < 7:
            week.append("  ")
        lines.append(" ".join(week))
    return "\n".join(lines)


def get_month_stats(
    data: dict, uid: str, year: int, month: int
) -> dict:
    today = datetime.date.today()
    days_in_month = calendar.monthrange(year, month)[1]
    stream_days = 0
    present_days = 0
    absent_days = 0
    for day in range(1, days_in_month + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        date_obj = datetime.date(year, month, day)
        if date_obj > today:
            break
        if date_str in data["streams"]:
            stream_days += 1
            # Vergleich: uid kann String oder in Liste (als String) sein
            stream_list = data["streams"][date_str]
            if uid in stream_list or str(uid) in stream_list:
                present_days += 1
            else:
                absent_days += 1
    return {
        "stream_days": stream_days,
        "present": present_days,
        "absent": absent_days,
    }


def get_current_streak(data: dict, uid: str) -> int:
    uid_s = str(uid)
    sorted_dates = sorted(data["streams"].keys(), reverse=True)
    streak = 0
    for date_str in sorted_dates:
        stream_list = data["streams"][date_str]
        if uid_s in stream_list or uid in stream_list:
            streak += 1
        else:
            # Streak nur brechen wenn es ein echter Stream-Tag war
            # (nicht bei Tagen ohne Stream)
            break
    return streak


def get_longest_streak(data: dict, uid: str) -> int:
    uid_s = str(uid)
    sorted_dates = sorted(data["streams"].keys())
    longest = 0
    current = 0
    for date_str in sorted_dates:
        stream_list = data["streams"][date_str]
        if uid_s in stream_list or uid in stream_list:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def get_rank_emoji(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**#{rank}**")


def get_activity_grade(percentage: float) -> tuple:
    if percentage >= 90:
        return "S+", "🔥", 0x00ff00
    elif percentage >= 75:
        return "A", "⭐", 0x2ecc71
    elif percentage >= 60:
        return "B", "👍", 0x3498db
    elif percentage >= 40:
        return "C", "📊", 0xf39c12
    elif percentage >= 20:
        return "D", "⚠️", 0xe67e22
    else:
        return "F", "💤", 0xe74c3c


def parse_time(time_str: str) -> int | None:
    time_str = time_str.strip().lower()
    pattern = re.match(r'^(\d+)(s|m|h|d)$', time_str)
    if not pattern:
        return None
    value = int(pattern.group(1))
    unit = pattern.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def get_user_compare_stats(data: dict, uid: str) -> dict:
    total_days = len(data["streams"])
    present = sum(
        1 for d in data["streams"] if uid in data["streams"][d]
    )
    absent = total_days - present
    pct = (present / total_days * 100) if total_days > 0 else 0
    current_streak = get_current_streak(data, uid)
    longest_streak = get_longest_streak(data, uid)
    all_counts = {
        u: sum(1 for d in data["streams"] if u in data["streams"][d])
        for u in data["users"]
    }
    sorted_users = sorted(
        all_counts.items(), key=lambda x: x[1], reverse=True
    )
    rank = next(
        (i + 1 for i, (u, _) in enumerate(sorted_users) if u == uid),
        len(sorted_users),
    )
    last_seen = "Noch nie"
    for date_str in sorted(data["streams"].keys(), reverse=True):
        if uid in data["streams"][date_str]:
            last_seen = date_str
            break
    first_seen = "Unbekannt"
    for date_str in sorted(data["streams"].keys()):
        if uid in data["streams"][date_str]:
            first_seen = date_str
            break
    grade, grade_emoji, grade_color = get_activity_grade(pct)
    return {
        "present": present,
        "absent": absent,
        "pct": pct,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "rank": rank,
        "last_seen": last_seen,
        "first_seen": first_seen,
        "grade": grade,
        "grade_emoji": grade_emoji,
        "grade_color": grade_color,
        "twitch": data["users"][uid]["twitch_name"],
        "total_days": total_days,
    }

# ══════════════════════════════════════════════════════════
#       ERWEITERTE STATISTIK-HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════

def get_user_message_stats(data: dict, uid: str, start_date: str = None, end_date: str = None) -> dict:
    """Berechnet Nachrichten-Statistiken eines Users für einen Zeitraum."""
    msg_counts = data.get("message_counts", {})
    total_messages = 0
    total_stream_days = 0
    active_hours: dict[str, int] = {}
    daily_counts: list[int] = []

    for date_str, day_data in msg_counts.items():
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue

        if uid in day_data:
            user_day = day_data[uid]
            count = user_day.get("count", 0)
            total_messages += count
            daily_counts.append(count)
            total_stream_days += 1

            for hour, h_count in user_day.get("hours", {}).items():
                active_hours[hour] = active_hours.get(hour, 0) + h_count

    avg_per_stream = total_messages / total_stream_days if total_stream_days > 0 else 0

    # Durchschnittliche Stunden pro Stream (geschätzt auf 3-4h)
    total_hours = len(active_hours) if active_hours else 1
    avg_per_hour = total_messages / max(1, total_stream_days * 3)  # ~3h pro Stream

    # Peak-Stunde finden
    peak_hour = max(active_hours, key=active_hours.get) if active_hours else "0"
    peak_hour_count = active_hours.get(peak_hour, 0)

    return {
        "total_messages": total_messages,
        "total_stream_days": total_stream_days,
        "avg_per_stream": round(avg_per_stream, 1),
        "avg_per_hour": round(avg_per_hour, 1),
        "daily_counts": daily_counts,
        "active_hours": active_hours,
        "peak_hour": int(peak_hour),
        "peak_hour_count": peak_hour_count,
    }


def get_month_message_stats(data: dict, uid: str, year: int, month: int) -> dict:
    """Nachrichten-Stats für einen bestimmten Monat."""
    start = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end = f"{year}-{month:02d}-{last_day}"
    return get_user_message_stats(data, uid, start, end)


def get_year_message_stats(data: dict, uid: str, year: int) -> dict:
    """Nachrichten-Stats für ein ganzes Jahr."""
    start = f"{year}-01-01"
    end = f"{year}-12-31"
    return get_user_message_stats(data, uid, start, end)


def get_month_activity_stats(data: dict, uid: str, year: int, month: int) -> dict:
    """Komplette Aktivitäts-Statistik für einen Monat."""
    today = datetime.date.today()
    days_in_month = calendar.monthrange(year, month)[1]
    stream_days = 0
    present_days = 0
    absent_days = 0

    for day in range(1, days_in_month + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        date_obj = datetime.date(year, month, day)
        if date_obj > today:
            break
        if date_str in data["streams"]:
            stream_days += 1
            if uid in data["streams"][date_str]:
                present_days += 1
            else:
                absent_days += 1

    pct = (present_days / stream_days * 100) if stream_days > 0 else 0
    grade, grade_emoji, grade_color = get_activity_grade(pct)

    msg_stats = get_month_message_stats(data, uid, year, month)

    return {
        "stream_days": stream_days,
        "present": present_days,
        "absent": absent_days,
        "pct": pct,
        "grade": grade,
        "grade_emoji": grade_emoji,
        "grade_color": grade_color,
        "messages": msg_stats,
    }

# ══════════════════════════════════════════════════════════
#        FEATURE: VPN/PROXY CHECK (proxycheck.io)
# ══════════════════════════════════════════════════════════

async def check_proxy(ip: str) -> dict:
    if not PROXYCHECK_API_KEY:
        return {}
    try:
        url = (
            f"https://proxycheck.io/v2/{ip}"
            f"?key={PROXYCHECK_API_KEY}&vpn=1&asn=1"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("status") == "ok" and ip in data:
                        return data[ip]
    except Exception as e:
        print(f"Fehler bei proxycheck.io Abfrage: {e}")
    return {}


# ══════════════════════════════════════════════════════════
#            AI MODERATION & HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════


# Globale Referenz auf den Discord Bot für Twitch-Events
discord_bot_ref = None
# Globale Referenz auf den Twitch Bot für Chat-Nachrichten
twitch_bot_ref = None

# ══════════════════════════════════════════════════════════
#         TICKET SYSTEM – VIEWS, MODALS & FUNKTIONEN
# ══════════════════════════════════════════════════════════

class TicketCreateModal(discord.ui.Modal, title="Ticket erstellen"):
    betreff = discord.ui.TextInput(
        label="Betreff",
        placeholder="Kurze Beschreibung deines Anliegens",
        min_length=3,
        max_length=100,
    )
    beschreibung = discord.ui.TextInput(
        label="Beschreibung",
        placeholder="Beschreibe dein Problem oder deine Frage ausführlich...",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket(
            interaction,
            self.betreff.value,
            self.beschreibung.value,
        )


class TicketOpenView(discord.ui.View):
    """Panel-View mit dem Ticket-Erstell-Button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫 Ticket erstellen",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_create_btn",
    )
    async def btn_create(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        # Prüfen ob User bereits ein offenes Ticket hat
        for ch_id, data in list(open_tickets.items()):
            if data.get("user_id") == interaction.user.id and data.get("status") in ("open", "claimed"):
                channel = interaction.guild.get_channel(ch_id)
                if channel:
                    return await interaction.response.send_message(
                        f"❌ Du hast bereits ein offenes Ticket: {channel.mention}",
                        ephemeral=True,
                    )
                # Kanal wurde extern gelöscht → veralteten Eintrag aufräumen
                open_tickets.pop(ch_id, None)
        persist_tickets()
        await interaction.response.send_modal(TicketCreateModal())


class TicketControlView(discord.ui.View):
    """View innerhalb des Ticket-Kanals.

    Robust gegen Bot-Neustarts: Der Ticket-Kanal wird aus der
    INTERACTION heraus gelöst (interaction.channel_id), nicht aus
    einem eventuell veralteten View-Zustand.
    """

    def __init__(self, ticket_channel_id: int, claimed_by_name: str = ""):
        super().__init__(timeout=None)
        self.ticket_channel_id = ticket_channel_id
        # Claim-Button bei Neuanlage (z. B. nach Bot-Neustart) direkt
        # im richtigen Zustand anzeigen.
        if claimed_by_name:
            self.btn_claim.label = f"✅ Geclaimed von {claimed_by_name[:30]}"
            self.btn_claim.disabled = True

    def _ticket_id_for(self, interaction: discord.Interaction) -> int:
        """Löst die Ticket-Kanal-ID aus der Interaction (robust)."""
        return interaction.channel_id or self.ticket_channel_id

    @discord.ui.button(
        label="✅ Claimen",
        style=discord.ButtonStyle.success,
        custom_id="ticket_claim_btn",
        row=0,
    )
    async def btn_claim(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        channel_id = self._ticket_id_for(interaction)
        ticket = open_tickets.get(channel_id)
        if not ticket:
            return await interaction.response.send_message(
                "❌ Ticket nicht gefunden.", ephemeral=True
            )

        support_role = discord.utils.get(
            interaction.guild.roles, name=TICKET_SUPPORT_ROLE
        )
        is_support = (
            support_role in interaction.user.roles
            if support_role
            else interaction.user.guild_permissions.manage_channels
        )

        if not is_support:
            return await interaction.response.send_message(
                f"❌ Du brauchst die Rolle **{TICKET_SUPPORT_ROLE}**!",
                ephemeral=True,
            )

        if ticket.get("claimed_by"):
            claimer = interaction.guild.get_member(ticket["claimed_by"])
            claimer_name = claimer.mention if claimer else "Jemand"
            return await interaction.response.send_message(
                f"❌ Dieses Ticket wurde bereits von {claimer_name} geclaimed!",
                ephemeral=True,
            )

        ticket["claimed_by"] = interaction.user.id
        ticket["claimed_by_name"] = interaction.user.display_name
        ticket["status"] = "claimed"
        ticket["ticket_message_id"] = interaction.message.id if interaction.message else None
        persist_tickets()

        embed = discord.Embed(
            title="✅ Ticket geclaimed",
            description=(
                f"{interaction.user.mention} kümmert sich jetzt um dieses Ticket!"
            ),
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        await interaction.response.send_message(embed=embed)

        # Button im ORIGINALEN Ticket-Embed aktualisieren
        # (nicht in der Claim-Ankündigung!)
        button.label = f"✅ Geclaimed von {interaction.user.display_name[:30]}"
        button.disabled = True
        try:
            msg = interaction.message
            if msg is not None:
                await msg.edit(view=self)
        except Exception as e:
            print(f"⚠️ [Ticket] Button-Update fehlgeschlagen: {e}")

        # Log senden (darf das Ticket nicht blockieren)
        try:
            await send_ticket_log(
                interaction.guild,
                "claim",
                ticket,
                interaction.user,
            )
        except Exception as e:
            print(f"⚠️ [Ticket] Claim-Log Fehler: {e}")

    @discord.ui.button(
        label="🔒 Schließen",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_btn",
        row=0,
    )
    async def btn_close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        channel_id = self._ticket_id_for(interaction)
        ticket = open_tickets.get(channel_id)
        if not ticket:
            return await interaction.response.send_message(
                "❌ Ticket nicht gefunden.", ephemeral=True
            )

        support_role = discord.utils.get(
            interaction.guild.roles, name=TICKET_SUPPORT_ROLE
        )
        is_support = (
            support_role in interaction.user.roles
            if support_role
            else interaction.user.guild_permissions.manage_channels
        )
        is_ticket_owner = interaction.user.id == ticket.get("user_id")

        if not is_support and not is_ticket_owner:
            return await interaction.response.send_message(
                "❌ Nur Support-Mitarbeiter oder der Ticket-Ersteller können das Ticket schließen!",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "🔒 Ticket wird geschlossen...", ephemeral=True
        )
        await close_ticket(
            interaction.guild,
            channel_id,
            interaction.user,
        )

    @discord.ui.button(
        label="📄 Transcript",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket_transcript_btn",
        row=0,
    )
    async def btn_transcript(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        channel_id = self._ticket_id_for(interaction)
        support_role = discord.utils.get(
            interaction.guild.roles, name=TICKET_SUPPORT_ROLE
        )
        is_support = (
            support_role in interaction.user.roles
            if support_role
            else interaction.user.guild_permissions.manage_channels
        )

        if not is_support:
            return await interaction.response.send_message(
                f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann Transcripts erstellen!",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            transcript = await generate_transcript(interaction.guild, channel_id)
        except Exception as e:
            print(f"⚠️ [Ticket] Transcript-Fehler: {e}")
            transcript = None
        if transcript:
            await interaction.followup.send(
                "📄 **Ticket-Transcript:**",
                file=discord.File(
                    fp=transcript,
                    filename=f"transcript-{channel_id}.txt",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Transcript konnte nicht erstellt werden.",
                ephemeral=True,
            )


# Registrierte Ticket-Views (pro Ticket-Kanal) – werden beim Schließen
# wieder entfernt, damit keine alten Views im Speicher bleiben.
ticket_views: dict[int, TicketControlView] = {}


def remove_bot_view(view) -> None:
    """Entfernt einen View aus dem Bot (discord.py-Version-unabhängig)."""
    try:
        if hasattr(d_bot, "remove_view"):
            d_bot.remove_view(view)
            return
        # discord.py < 2.8 hat kein Client.remove_view → direkt
        # über den ViewStore
        d_bot._connection._view_store.remove_view(view)
    except Exception as e:
        print(f"⚠️ [Bot] View-Entfernung fehlgeschlagen: {e}")


def register_ticket_view(
    channel_id: int,
    message_id: int = None,
    claimed_by_name: str = "",
) -> TicketControlView:
    """Registriert die Control-View für ein Ticket (persistierend).

    Wichtig: Bei `message_id` wird der View exakt der Ticket-Message
    zugeordnet (discord.py dispatcht pro Message), sonst als globaler
    Fallback. Der Callback löst den Kanal ohnehin aus der Interaction.
    """
    view = TicketControlView(channel_id, claimed_by_name)
    ticket_views[channel_id] = view
    try:
        if message_id:
            d_bot.add_view(view, message_id=int(message_id))
        else:
            d_bot.add_view(view)
    except Exception as e:
        print(f"⚠️ [Tickets] View-Registrierung fehlgeschlagen: {e}")
    return view


async def find_ticket_message_id(guild: discord.Guild, channel_id: int) -> int:
    """Sucht die Ticket-Message (mit Claim-Button) in einem Kanal."""
    try:
        channel = guild.get_channel(channel_id)
        if not channel:
            return 0
        async for m in channel.history(limit=30):
            comps = getattr(m, "components", [])
            for row in comps:
                for b in getattr(row, "children", []):
                    if getattr(b, "custom_id", None) == "ticket_claim_btn":
                        return m.id
    except Exception:
        pass
    return 0


async def create_ticket(
    interaction: discord.Interaction,
    betreff: str,
    beschreibung: str,
) -> None:
    """Erstellt einen neuen Ticket-Kanal."""
    guild = interaction.guild
    member = interaction.user
    ticket_id = get_next_ticket_id()

    # Kategorie holen
    category = None
    if TICKET_CATEGORY_ID:
        category = guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            category = None

    # Support-Rolle holen
    support_role = discord.utils.get(guild.roles, name=TICKET_SUPPORT_ROLE)

    # Berechtigungen
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
        ),
        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
        ),
    }

    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{ticket_id:04d}-{member.name[:10]}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket #{ticket_id:04d} | Erstellt von {member.name} | {betreff}",
            reason=f"Ticket #{ticket_id:04d}: {member.display_name}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Ich habe keine Berechtigung um einen Ticket-Kanal zu erstellen!",
            ephemeral=True,
        )
        return
    except Exception as e:
        await interaction.followup.send(
            f"❌ Fehler beim Erstellen: {e}", ephemeral=True
        )
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    now_str = now.isoformat()

    open_tickets[channel.id] = {
        "user_id": member.id,
        "ticket_id": ticket_id,
        "status": "open",
        "created_at": now_str,
        "claimed_by": None,
        "claimed_by_name": "",
        "betreff": betreff,
        "beschreibung": beschreibung,
    }

    # Ticket in JSON speichern
    persist_tickets()

    # Ticket-Embed senden
    embed = discord.Embed(
        title=f"🎫 Ticket #{ticket_id:04d}",
        color=COLOR_TICKET,
        timestamp=now,
    )
    embed.add_field(name="👤 Erstellt von", value=member.mention, inline=True)
    embed.add_field(name="🆔 Ticket-ID", value=f"`#{ticket_id:04d}`", inline=True)
    embed.add_field(name="📋 Status", value="🟢 Offen", inline=True)
    embed.add_field(name="📝 Betreff", value=betreff, inline=False)
    embed.add_field(name="💬 Beschreibung", value=beschreibung, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(
        text="Support wird sich bald melden • Bitte gedulde dich"
    )

    view = TicketControlView(channel.id)

    mention_text = f"{member.mention}"
    if support_role:
        mention_text += f" | {support_role.mention}"

    ticket_msg = await channel.send(content=mention_text, embed=embed, view=view)

    # Persistierend registrieren – exakt an die Ticket-Message gebunden,
    # damit die Buttons auch nach einem Bot-Neustart funktionieren
    register_ticket_view(channel.id, message_id=ticket_msg.id)

    # Message-ID merken (wird beim Neustart zur Re-Registrierung genutzt)
    open_tickets[channel.id]["ticket_message_id"] = ticket_msg.id
    persist_tickets()

    # Bestätigung an den User
    await interaction.followup.send(
        f"✅ Dein Ticket wurde erstellt: {channel.mention}\n"
        f"📝 **Betreff:** {betreff}",
        ephemeral=True,
    )

    # Log senden
    await send_ticket_log(guild, "open", open_tickets[channel.id], member)
    print(f"🎫 [Ticket] #{ticket_id:04d} erstellt von {member.display_name}")


async def close_ticket(
    guild: discord.Guild,
    channel_id: int,
    closed_by: discord.Member,
) -> None:
    """Schließt und löscht einen Ticket-Kanal.

    Jede Teilaufgabe (Transcript, Log, DM) ist gefangen, damit der
    Kanal IMMER zuverlässig gelöscht wird – auch wenn ein Log-Kanal
    fehlt oder der User keine DMs empfängt.
    """
    ticket = open_tickets.get(channel_id)
    if not ticket:
        return

    channel = guild.get_channel(channel_id)
    if not channel:
        open_tickets.pop(channel_id, None)
        persist_tickets()
        return

    ticket["status"] = "closed"
    try:
        ticket_num = int(ticket.get("ticket_id") or 0)
    except (TypeError, ValueError):
        ticket_num = 0

    # Transcript generieren vor dem Löschen (optional)
    transcript = None
    try:
        transcript = await generate_transcript(guild, channel_id)
    except Exception as e:
        print(f"⚠️ [Ticket] Transcript-Fehler: {e}")

    # Log senden (optional – darf das Schließen nicht blockieren)
    try:
        await send_ticket_log(guild, "close", ticket, closed_by, transcript)
    except Exception as e:
        print(f"⚠️ [Ticket] Close-Log Fehler: {e}")

    # DM an Ticket-Ersteller (optional)
    try:
        ticket_user = guild.get_member(ticket.get("user_id") or 0)
        if ticket_user:
            created_str = ticket.get("created_at", "")
            created_ts = 0
            try:
                created_ts = int(
                    datetime.datetime.fromisoformat(created_str).timestamp()
                )
            except Exception:
                pass
            desc = f"Dein Ticket wurde von {closed_by.mention} geschlossen.\n\n"
            desc += f"**Betreff:** {ticket.get('betreff', '')}\n"
            if created_ts:
                desc += f"**Erstellt:** <t:{created_ts}:R>"
            close_embed = discord.Embed(
                title=f"🔒 Ticket #{ticket_num:04d} geschlossen",
                description=desc,
                color=COLOR_ERROR,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            await ticket_user.send(embed=close_embed)
    except Exception:
        pass  # DMs blockiert → nicht weiter schlimm

    # Aus RAM + JSON entfernen
    open_tickets.pop(channel_id, None)
    persist_tickets()

    # Registrierte View entfernen (damit sie nach dem Neustart
    # nicht erneut für einen gelöschten Kanal geladen wird)
    view = ticket_views.pop(channel_id, None)
    if view is not None:
        remove_bot_view(view)

    # Kanal schließen
    await asyncio.sleep(3)
    try:
        await channel.delete(reason=f"Ticket geschlossen von {closed_by.display_name}")
        print(f"🔒 [Ticket] #{ticket_num:04d} geschlossen von {closed_by.display_name}")
    except Exception as e:
        print(f"❌ [Ticket] Fehler beim Löschen: {e}")


async def generate_transcript(
    guild: discord.Guild,
    channel_id: int,
) -> "io.BytesIO | None":
    """Generiert einen Text-Transcript des Ticket-Kanals."""
    import io
    channel = guild.get_channel(channel_id)
    if not channel:
        return None

    ticket = open_tickets.get(channel_id, {})
    try:
        _tid = int(ticket.get("ticket_id") or 0)
    except (TypeError, ValueError):
        _tid = 0
    lines = [
        f"═══════════════════════════════════════════",
        f"  TICKET TRANSCRIPT",
        f"  Ticket #: {_tid:04d}",
        f"  Betreff: {ticket.get('betreff', '?')}",
        f"  Erstellt von: {ticket.get('user_id', '?')}",
        f"  Datum: {datetime.datetime.now(datetime.timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}",
        f"═══════════════════════════════════════════",
        "",
    ]

    try:
        async for msg in channel.history(limit=500, oldest_first=True):
            timestamp = msg.created_at.strftime("%d.%m.%Y %H:%M")
            content = msg.content or ""
            if msg.embeds:
                content += f" [Embed: {msg.embeds[0].title or 'Kein Titel'}]"
            if msg.attachments:
                content += f" [Anhang: {', '.join(a.filename for a in msg.attachments)}]"
            lines.append(f"[{timestamp}] {msg.author.display_name}: {content}")
    except Exception as e:
        lines.append(f"Fehler beim Laden der Nachrichten: {e}")

    content = "\n".join(lines)
    return io.BytesIO(content.encode("utf-8"))


async def send_ticket_log(
    guild: discord.Guild,
    action: str,
    ticket: dict,
    actor: discord.Member,
    transcript=None,
) -> None:
    """Sendet ein Log-Embed in den Ticket-Log-Kanal.

    Nie eine Exception nach außen werfen – ein fehlender Log-Kanal
    darf kein Ticket-Feature kaputt machen.
    """
    try:
        return await _send_ticket_log_inner(guild, action, ticket, actor, transcript)
    except Exception as e:
        print(f"⚠️ [Ticket] Log-Fehler: {type(e).__name__}: {e}")


async def _send_ticket_log_inner(
    guild: discord.Guild,
    action: str,
    ticket: dict,
    actor: discord.Member,
    transcript=None,
) -> None:
    log_channel_id = TICKET_LOG_CHANNEL_ID or LOG_CHANNEL_ID
    log_channel = guild.get_channel(log_channel_id)
    if not log_channel:
        return

    try:
        _tid = int(ticket.get("ticket_id") or 0)
    except (TypeError, ValueError):
        _tid = 0

    action_configs = {
        "open": {
            "title": "🎫 Ticket geöffnet",
            "color": COLOR_SUCCESS,
        },
        "close": {
            "title": "🔒 Ticket geschlossen",
            "color": COLOR_ERROR,
        },
        "claim": {
            "title": "✅ Ticket geclaimed",
            "color": COLOR_INFO,
        },
    }
    cfg = action_configs.get(action, {"title": "📋 Ticket-Aktion", "color": COLOR_INFO})

    ticket_user = guild.get_member(ticket.get("user_id", 0))

    embed = discord.Embed(
        title=cfg["title"],
        color=cfg["color"],
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="🆔 Ticket",
        value=f"`#{_tid:04d}`",
        inline=True,
    )
    embed.add_field(
        name="👤 Erstellt von",
        value=ticket_user.mention if ticket_user else f"`{ticket.get('user_id')}`",
        inline=True,
    )
    embed.add_field(
        name="🛡️ Aktion von",
        value=actor.mention,
        inline=True,
    )
    embed.add_field(
        name="📝 Betreff",
        value=ticket.get("betreff", "?"),
        inline=False,
    )
    if ticket.get("beschreibung") and action == "open":
        embed.add_field(
            name="💬 Beschreibung",
            value=ticket["beschreibung"][:500],
            inline=False,
        )
    embed.set_footer(
        text=f"Ticket System • {datetime.datetime.now(datetime.timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}"
    )
    if ticket_user:
        embed.set_thumbnail(url=ticket_user.display_avatar.url)

    if transcript and action == "close":
        await log_channel.send(
            embed=embed,
            file=discord.File(
                fp=transcript,
                filename=f"transcript-ticket-{_tid:04d}.txt",
            ),
        )
    else:
        await log_channel.send(embed=embed)

# ══════════════════════════════════════════════════════════
#         FEATURE: RUSSISCHES ROULETTE (MULTIPLAYER)
# ══════════════════════════════════════════════════════════

class RussianRouletteJoinView(discord.ui.View):
    def __init__(self, host: discord.Member, einsatz: int, channel_id: int):
        super().__init__(timeout=RR_JOIN_TIMEOUT)
        self.host = host
        self.einsatz = einsatz
        self.channel_id = channel_id
        self.players: list[discord.Member] = [host]
        self.started = False
        self.cancelled = False

    @discord.ui.button(label="🔫 Mitspielen!", style=discord.ButtonStyle.danger)
    async def btn_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started or self.cancelled:
            return
        if interaction.user.id == self.host.id:
            return await interaction.response.send_message("❌ Bereits Host!", ephemeral=True)
        if any(p.id == interaction.user.id for p in self.players):
            return await interaction.response.send_message("❌ Bereits dabei!", ephemeral=True)
        if len(self.players) >= RR_MAX_PLAYERS:
            return await interaction.response.send_message("❌ Voll!", ephemeral=True)
        if get_balance(interaction.user.id) < self.einsatz:
            return await interaction.response.send_message(
                f"❌ Brauchst **{self.einsatz:,} 🪙**!", ephemeral=True
            )

        self.players.append(interaction.user)
        update_balance(interaction.user.id, -self.einsatz)

        player_list = "\n".join(f"🔫 {p.mention}" for p in self.players)
        embed = discord.Embed(
            title="🔫 Russisches Roulette – Lobby",
            description=(
                f"**Host:** {self.host.mention}\n"
                f"**Einsatz:** {self.einsatz:,} 🪙 pro Person\n"
                f"**Pot:** {self.einsatz * len(self.players):,} 🪙\n\n"
                f"**Spieler ({len(self.players)}/{RR_MAX_PLAYERS}):**\n{player_list}"
            ),
            color=COLOR_RR,
        )
        embed.set_footer(text=f"Min: {RR_MIN_PLAYERS} • Host klickt ▶️ Starten")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶️ Starten!", style=discord.ButtonStyle.success)
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message("❌ Nur der Host!", ephemeral=True)
        if len(self.players) < RR_MIN_PLAYERS:
            return await interaction.response.send_message(
                f"❌ Mindestens **{RR_MIN_PLAYERS}** Spieler!", ephemeral=True
            )
        self.started = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🔫 Russisches Roulette startet...",
                description="🔄 Die Trommel wird geladen...",
                color=COLOR_RR,
            ),
            view=None,
        )
        await play_russian_roulette(interaction.channel, self.players, self.einsatz)

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message("❌ Nur der Host!", ephemeral=True)
        self.cancelled = True
        self.stop()
        for p in self.players:
            update_balance(p.id, self.einsatz)
        active_rr_games.pop(self.channel_id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Abgebrochen",
                description="Einsätze zurückgegeben.",
                color=COLOR_ERROR,
            ),
            view=None,
        )

    async def on_timeout(self):
        if not self.started and not self.cancelled:
            if len(self.players) >= RR_MIN_PLAYERS:
                self.started = True
                channel = self.host.guild.get_channel(self.channel_id)
                if channel:
                    await channel.send(
                        embed=discord.Embed(
                            title="🔫 Zeit abgelaufen – Startet automatisch!",
                            color=COLOR_RR,
                        )
                    )
                    await play_russian_roulette(channel, self.players, self.einsatz)
            else:
                for p in self.players:
                    update_balance(p.id, self.einsatz)
                active_rr_games.pop(self.channel_id, None)


class RRTurnView(discord.ui.View):
    """View für den Spieler der gerade dran ist."""

    def __init__(self, player: discord.Member, chamber: int, current_chamber: int, chambers: int):
        super().__init__(timeout=15)
        self.player = player
        self.chamber = chamber
        self.current_chamber = current_chamber
        self.chambers = chambers
        self.choice = None  # "shoot", "spin", "pass"
        self.done = asyncio.Event()

    @discord.ui.button(label="🔫 Abdrücken!", style=discord.ButtonStyle.danger, row=0)
    async def btn_shoot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
        self.choice = "shoot"
        self.stop()
        self.done.set()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🔫 Abdrücken...",
                description=f"**{self.player.display_name}** drückt ab...",
                color=COLOR_RR,
            ),
            view=None,
        )

    @discord.ui.button(label="🔄 Trommel drehen", style=discord.ButtonStyle.primary, row=0)
    async def btn_spin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
        self.choice = "spin"
        self.stop()
        self.done.set()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🔄 Trommel wird neu gedreht...",
                description=f"**{self.player.display_name}** dreht die Trommel neu!",
                color=COLOR_WARNING,
            ),
            view=None,
        )

    @discord.ui.button(label="👉 Weitergeben", style=discord.ButtonStyle.secondary, row=0)
    async def btn_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
        self.choice = "pass"
        self.stop()
        self.done.set()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="👉 Weitergeben...",
                description=(
                    f"**{self.player.display_name}** gibt den Revolver weiter!\n"
                    f"*Aber der nächste MUSS abdrücken!*"
                ),
                color=COLOR_INFO,
            ),
            view=None,
        )

    @discord.ui.button(label="🎲 Russisch (2 Kammern)", style=discord.ButtonStyle.danger, row=1)
    async def btn_double(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
        self.choice = "double"
        self.stop()
        self.done.set()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🎲 DOPPELT ODER NICHTS!",
                description=(
                    f"**{self.player.display_name}** wählt 2 Kammern!\n"
                    f"*2/{self.chambers} Chance getroffen zu werden – aber Überlebende bekommen ×2 Pot-Anteil!*"
                ),
                color=0xFF0000,
            ),
            view=None,
        )

    async def on_timeout(self):
        if self.choice is None:
            self.choice = "shoot"  # Auto-Abdrücken bei Timeout
            self.done.set()


class RRForceShootView(discord.ui.View):
    """View wenn der Spieler abdrücken MUSS (nach Weitergeben)."""

    def __init__(self, player: discord.Member):
        super().__init__(timeout=10)
        self.player = player
        self.done = asyncio.Event()
        self.pressed = False

    @discord.ui.button(label="🔫 ABDRÜCKEN!", style=discord.ButtonStyle.danger)
    async def btn_shoot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
        self.pressed = True
        self.stop()
        self.done.set()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🔫 Abdrücken...",
                description=f"**{self.player.display_name}** drückt ab...",
                color=COLOR_RR,
            ),
            view=None,
        )

    async def on_timeout(self):
        self.pressed = True
        self.done.set()


def build_rr_status(alive: list, eliminated: list, current_player, pot: int, round_num: int) -> discord.Embed:
    """Baut das Status-Embed für Russisches Roulette."""
    alive_str = "\n".join(
        f"{'👉 ' if p.id == current_player.id else '   '}🟢 {p.display_name}"
        for p in alive
    )
    dead_str = "\n".join(f"💀 ~~{p.display_name}~~" for p in eliminated) if eliminated else "*Noch niemand*"

    embed = discord.Embed(
        title=f"🔫 Russisches Roulette – Runde {round_num}",
        color=COLOR_RR,
    )
    embed.add_field(name="🟢 Am Leben", value=alive_str, inline=True)
    embed.add_field(name="💀 Ausgeschieden", value=dead_str, inline=True)
    embed.add_field(name="💰 Pot", value=f"**{pot:,} 🪙**", inline=True)

    return embed


async def play_russian_roulette(
    channel: discord.TextChannel,
    players: list[discord.Member],
    einsatz: int,
):
    """Spielt eine interaktive Runde Russisches Roulette."""
    active_rr_games.pop(channel.id, None)
    pot = einsatz * len(players)
    alive = list(players)
    eliminated = []
    random.shuffle(alive)
    chamber = random.randint(0, RR_CHAMBERS - 1)
    current_chamber = 0
    round_num = 0
    bonus_multipliers: dict[int, float] = {}  # user_id -> bonus mult
    force_shoot_next = False

    # Intro
    embed = discord.Embed(
        title="🔫 Russisches Roulette",
        description=(
            f"**{len(alive)} Spieler** • **{RR_CHAMBERS} Kammern** • 1 Kugel\n"
            f"💰 **Pot:** {pot:,} 🪙\n\n"
            f"**Aktionen pro Zug:**\n"
            f"🔫 **Abdrücken** — Normal schießen (1/{RR_CHAMBERS})\n"
            f"🔄 **Trommel drehen** — Kammern neu mischen\n"
            f"👉 **Weitergeben** — Nächster MUSS schießen\n"
            f"🎲 **Doppelt** — 2 Kammern prüfen, aber ×2 Bonus wenn du überlebst\n\n"
            f"*Bei Timeout wird automatisch abgedrückt!*"
        ),
        color=COLOR_RR,
    )
    await channel.send(embed=embed)
    await asyncio.sleep(3)

    player_index = 0

    while len(alive) > 1:
        player = alive[player_index % len(alive)]
        round_num += 1

        # Status anzeigen
        status_embed = build_rr_status(alive, eliminated, player, pot, round_num)
        status_embed.set_footer(
            text=f"Kammer {current_chamber + 1}/{RR_CHAMBERS} • {player.display_name} ist dran!"
        )
        await channel.send(embed=status_embed)
        await asyncio.sleep(1)

        if force_shoot_next:
            # Spieler MUSS schießen
            force_view = RRForceShootView(player)
            force_embed = discord.Embed(
                title=f"🔫 {player.display_name} – DU MUSST SCHIESSEN!",
                description=(
                    f"Der vorherige Spieler hat den Revolver **weitergegeben**!\n\n"
                    f"Drücke auf **🔫 ABDRÜCKEN** !"
                ),
                color=0xFF0000,
            )
            force_msg = await channel.send(
                content=player.mention, embed=force_embed, view=force_view
            )

            try:
                await asyncio.wait_for(force_view.done.wait(), timeout=12)
            except asyncio.TimeoutError:
                pass

            force_shoot_next = False
            choice = "shoot"
        else:
            # Normale Auswahl
            turn_view = RRTurnView(player, chamber, current_chamber, RR_CHAMBERS)
            turn_embed = discord.Embed(
                title=f"🔫 {player.display_name} ist dran!",
                description=(
                    f"Was willst du tun?\n\n"
                    f"🔫 **Abdrücken** — Schuss! (1/{RR_CHAMBERS} Risiko)\n"
                    f"🔄 **Trommel drehen** — Kammern neu mischen\n"
                    f"👉 **Weitergeben** — Nächster muss schießen\n"
                    f"🎲 **Doppelt** — 2 Kammern, aber ×2 Bonus!\n\n"
                    f"⏱️ **10 Sekunden** oder Auto-Schuss!"
                ),
                color=COLOR_RR,
            )
            turn_msg = await channel.send(
                content=player.mention, embed=turn_embed, view=turn_view
            )

            try:
                await asyncio.wait_for(turn_view.done.wait(), timeout=17)
            except asyncio.TimeoutError:
                pass

            choice = turn_view.choice or "shoot"

        await asyncio.sleep(1.5)

        # ── Aktion verarbeiten ───────────────────────────

        if choice == "spin":
            # Trommel neu drehen
            chamber = random.randint(0, RR_CHAMBERS - 1)
            current_chamber = 0

            await channel.send(
                embed=discord.Embed(
                    title="🔄 Trommel neu gedreht!",
                    description=(
                        f"**{player.display_name}** hat die Trommel neu gemischt.\n"
                        f"*Alle Kammern wurden zurückgesetzt!*\n\n"
                        f"Jetzt muss {player.display_name} trotzdem **abdrücken**!"
                    ),
                    color=COLOR_WARNING,
                )
            )
            await asyncio.sleep(1.5)

            # Nach Spin muss man trotzdem schießen
            suspense = random.choice([
                "🔫 *klick*...",
                "🔫 Der Finger zittert am Abzug...",
                "🔫 Schweißperlen auf der Stirn...",
                "🔫 Das Herz schlägt schneller...",
            ])
            await channel.send(suspense)
            await asyncio.sleep(2)

            # Schuss prüfen
            if current_chamber == chamber:
                # GETROFFEN
                alive.remove(player)
                eliminated.append(player)
                player_index = player_index % max(1, len(alive))

                await channel.send(
                    embed=discord.Embed(
                        title="💥 BANG!",
                        description=(
                            f"**{player.mention}** wurde getroffen! 💀\n"
                            f"*Die neu gedrehte Trommel war nicht gnädig...*"
                        ),
                        color=COLOR_ERROR,
                    )
                )

                try:
                    await player.timeout(
                        datetime.timedelta(seconds=60),
                        reason="Russisches Roulette – getroffen 🔫",
                    )
                except Exception:
                    pass

                # Neue Trommel
                chamber = random.randint(0, RR_CHAMBERS - 1)
                current_chamber = 0
                await asyncio.sleep(2)

                if len(alive) > 1:
                    await channel.send("🔄 *Neue Trommel wird geladen...*")
                    await asyncio.sleep(1.5)
            else:
                await channel.send(
                    embed=discord.Embed(
                        title="😮‍💨 *klick* – Überlebt!",
                        description=f"**{player.mention}** hat Glück!",
                        color=COLOR_SUCCESS,
                    )
                )
                current_chamber = (current_chamber + 1) % RR_CHAMBERS
                player_index += 1
                await asyncio.sleep(1.5)

        elif choice == "pass":
            # Weitergeben
            await channel.send(
                embed=discord.Embed(
                    title="👉 Weitergegeben!",
                    description=(
                        f"**{player.display_name}** gibt den Revolver weiter.\n"
                        f"*Der nächste Spieler MUSS abdrücken!*"
                    ),
                    color=COLOR_INFO,
                )
            )
            force_shoot_next = True
            player_index += 1
            await asyncio.sleep(1.5)

        elif choice == "double":
            # Doppelt – 2 Kammern prüfen
            suspense_msgs = [
                "🎲 *Zwei Klicks...*",
                "🔫 Erster Klick...",
            ]
            for msg in suspense_msgs:
                await channel.send(msg)
                await asyncio.sleep(1.5)

            # Prüfe 2 Kammern
            hit = False
            for i in range(2):
                check_chamber = (current_chamber + i) % RR_CHAMBERS
                if check_chamber == chamber:
                    hit = True
                    break

            if hit:
                # GETROFFEN bei Doppelt
                alive.remove(player)
                eliminated.append(player)
                player_index = player_index % max(1, len(alive))

                await channel.send("🔫 Zweiter Klick...")
                await asyncio.sleep(1)
                await channel.send(
                    embed=discord.Embed(
                        title="💥💥 DOPPEL-BANG!",
                        description=(
                            f"**{player.mention}** hat das Risiko gewählt und **verloren**! 💀\n"
                            f"*Doppelt hält nicht immer besser...*"
                        ),
                        color=COLOR_ERROR,
                    )
                )

                try:
                    await player.timeout(
                        datetime.timedelta(seconds=90),
                        reason="Russisches Roulette – Doppel-Tod 🔫🔫",
                    )
                except Exception:
                    pass

                chamber = random.randint(0, RR_CHAMBERS - 1)
                current_chamber = 0
                await asyncio.sleep(2)

                if len(alive) > 1:
                    await channel.send("🔄 *Neue Trommel wird geladen...*")
                    await asyncio.sleep(1.5)
            else:
                # Überlebt Doppelt – Bonus!
                bonus_multipliers[player.id] = bonus_multipliers.get(player.id, 1.0) + 0.5
                current_chamber = (current_chamber + 2) % RR_CHAMBERS

                await channel.send("🔫 Zweiter Klick...")
                await asyncio.sleep(1)
                await channel.send(
                    embed=discord.Embed(
                        title="😎 ÜBERLEBT! Doppelt!",
                        description=(
                            f"**{player.mention}** hat 2 Kammern überlebt!\n"
                            f"🎯 **Bonus: ×{bonus_multipliers[player.id]:.1f}** auf den Gewinn-Anteil!\n"
                            f"*Mut wird belohnt!*"
                        ),
                        color=COLOR_GOLD,
                    )
                )
                player_index += 1
                await asyncio.sleep(2)

        else:
            # Normaler Schuss
            suspense = random.choice([
                "🔫 *klick*...",
                "🔫 Der Finger zittert...",
                "🔫 Alle halten den Atem an...",
                "🔫 Die Trommel dreht sich langsam...",
                "🔫 *tick... tick... tick...*",
            ])
            await channel.send(suspense)
            await asyncio.sleep(2)

            if current_chamber == chamber:
                # GETROFFEN
                alive.remove(player)
                eliminated.append(player)
                player_index = player_index % max(1, len(alive))

                bang_line = random.choice([
                    'Das war leider die falsche Kammer...',
                    'Pech gehabt...',
                    'Die Kugel hat ihr Ziel gefunden...',
                    'Das Glück war nicht auf deiner Seite...',
                    'F in den Chat...',
                ])
                await channel.send(
                    embed=discord.Embed(
                        title="💥 BANG!",
                        description=(
                            f"**{player.mention}** wurde getroffen! 💀\n\n"
                            f"*{bang_line}*"
                        ),
                        color=COLOR_ERROR,
                    )
                )

                try:
                    await player.timeout(
                        datetime.timedelta(seconds=60),
                        reason="Russisches Roulette – getroffen 🔫",
                    )
                except Exception:
                    pass

                chamber = random.randint(0, RR_CHAMBERS - 1)
                current_chamber = 0
                await asyncio.sleep(2)

                if len(alive) > 1:
                    await channel.send("🔄 *Neue Trommel wird geladen...*")
                    await asyncio.sleep(1.5)
            else:
                survive_line = random.choice([
                    'Die Kammer war leer!',
                    'Noch einmal davon gekommen...',
                    'Das Glück ist auf deiner Seite!',
                    'Phew... Das war knapp!',
                ])
                await channel.send(
                    embed=discord.Embed(
                        title="😮‍💨 *klick* – Überlebt!",
                        description=(
                            f"**{player.mention}** hat Glück!\n"
                            f"*{survive_line}*"
                        ),
                        color=COLOR_SUCCESS,
                    )
                )
                current_chamber = (current_chamber + 1) % RR_CHAMBERS
                player_index += 1
                await asyncio.sleep(1.5)

    # ── Gewinner ─────────────────────────────────────────

    winner = alive[0]
    winner_bonus = bonus_multipliers.get(winner.id, 1.0)
    fee = int(pot * 0.05)
    base_payout = pot - fee
    payout = int(base_payout * winner_bonus)

    # Falls Bonus, den Extra-Betrag berechnen
    bonus_text = ""
    if winner_bonus > 1.0:
        bonus_text = f"\n🎲 **Doppelt-Bonus:** ×{winner_bonus:.1f} → **{payout:,} 🪙**!"

    update_balance(winner.id, payout)

    # Statistiken
    loser_list = "\n".join(
        f"💀 {p.display_name} (-{einsatz:,} 🪙)"
        for p in eliminated
    )

    # Survival-Statistik
    total_rounds = round_num
    doubles_survived = int((winner_bonus - 1.0) / 0.5) if winner_bonus > 1.0 else 0

    embed = discord.Embed(
        title="🏆 ÜBERLEBT!",
        description=(
            f"**{winner.mention}** ist der letzte Überlebende!\n\n"
            f"💰 **Gewinn: {payout:,} 🪙**{bonus_text}\n"
            f"📊 Pot: {pot:,} 🪙 (5% Gebühr: {fee:,} 🪙)\n"
            f"🔫 Runden: {total_rounds}\n"
            f"👥 Besiegt: {len(eliminated)}"
        ),
        color=COLOR_GOLD,
    )

    if doubles_survived > 0:
        embed.add_field(
            name="🎲 Mut-Bonus",
            value=f"**{doubles_survived}×** Doppelt überlebt → ×{winner_bonus:.1f} Bonus!",
            inline=False,
        )

    embed.add_field(name="💀 Gefallene", value=loser_list, inline=False)
    embed.set_thumbnail(url=winner.display_avatar.url)
    embed.set_footer(
        text=f"Kontostand {winner.display_name}: {get_balance(winner.id):,} 🪙"
    )

    await channel.send(embed=embed)

# ══════════════════════════════════════════════════════════
#         MUSIK-BATTLE – TTS & AUDIO HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════

async def generate_tts_file(text: str) -> str | None:
    """Generiert eine TTS MP3-Datei und gibt den Pfad zurück."""
    try:
        from gtts import gTTS
        loop = asyncio.get_event_loop()

        def _create_tts():
            tts = gTTS(text=text, lang='de', slow=False)
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix='.mp3', prefix='tts_'
            )
            tts.save(tmp.name)
            tmp.close()
            return tmp.name

        path = await loop.run_in_executor(None, _create_tts)
        return path
    except ImportError:
        print("⚠️ gTTS nicht installiert! pip install gTTS")
        return None
    except Exception as e:
        print(f"❌ TTS Fehler: {e}")
        return None


async def play_audio_file_and_wait(
    voice_client: discord.VoiceClient,
    file_path: str,
    timeout: int = 30,
) -> None:
    """Spielt eine Audio-Datei ab und wartet bis sie fertig ist."""
    if not voice_client or not voice_client.is_connected():
        return

    done_event = asyncio.Event()

    def after_play(error):
        if error:
            print(f"❌ Audio-Playback Fehler: {error}")
        done_event.set()

    try:
        source = discord.FFmpegPCMAudio(file_path)
        voice_client.play(source, after=after_play)
        await asyncio.wait_for(done_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        if voice_client.is_playing():
            voice_client.stop()
    except Exception as e:
        print(f"❌ Audio abspielen Fehler: {e}")


async def play_stream_url_for_duration(
    voice_client: discord.VoiceClient,
    stream_url: str,
    duration: int = 20,
) -> None:
    """Spielt einen Stream für eine bestimmte Dauer ab und stoppt dann."""
    if not voice_client or not voice_client.is_connected():
        return

    done_event = asyncio.Event()

    def after_play(error):
        if error:
            print(f"❌ Stream-Playback Fehler: {error}")
        done_event.set()

    try:
        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        voice_client.play(source, after=after_play)

        # Warte die gewünschte Dauer, dann stoppe
        try:
            await asyncio.wait_for(done_event.wait(), timeout=duration)
        except asyncio.TimeoutError:
            if voice_client.is_playing():
                voice_client.stop()
                # Kurz warten bis stop wirksam wird
                await asyncio.sleep(0.5)
    except Exception as e:
        print(f"❌ Stream abspielen Fehler: {e}")


def cleanup_tts_file(path: str) -> None:
    """Löscht eine temporäre TTS-Datei."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════
#         FEATURE: MUSIK-BATTLES
# ══════════════════════════════════════════════════════════

class MusicBattleAcceptView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, einsatz: int, channel_id: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.einsatz = einsatz
        self.channel_id = channel_id
        self.accepted = False

    @discord.ui.button(label="🎵 Annehmen!", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message(
                "❌ Nur der Herausgeforderte kann annehmen!", ephemeral=True
            )
        if get_balance(self.opponent.id) < self.einsatz:
            return await interaction.response.send_message(
                f"❌ Du brauchst mindestens **{self.einsatz:,} 🪙**!", ephemeral=True
            )

        self.accepted = True
        update_balance(self.opponent.id, -self.einsatz)
        self.stop()

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🎵 Musik-Battle angenommen!",
                description="Beide Spieler müssen jetzt jeweils einen Song wählen!",
                color=COLOR_BATTLE,
            ),
            view=None,
        )

        await run_music_battle(
            interaction.channel,
            self.challenger,
            self.opponent,
            self.einsatz,
        )

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("❌ Nicht für dich!", ephemeral=True)
        update_balance(self.challenger.id, self.einsatz)
        active_battles.pop(self.channel_id, None)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Musik-Battle abgelehnt",
                description=f"{self.opponent.mention} hat abgelehnt. Einsatz zurückgegeben.",
                color=COLOR_ERROR,
            ),
            view=None,
        )

    async def on_timeout(self):
        if not self.accepted:
            update_balance(self.challenger.id, self.einsatz)
            active_battles.pop(self.channel_id, None)


class SongSelectModal(discord.ui.Modal, title="🎵 Song auswählen"):
    song_query = discord.ui.TextInput(
        label="Song-Titel oder URL",
        placeholder="z.B. Never Gonna Give You Up",
        min_length=2,
        max_length=200,
    )

    def __init__(self, player: discord.Member, battle_data: dict):
        super().__init__()
        self.player = player
        self.battle_data = battle_data

    async def on_submit(self, interaction: discord.Interaction):
        query = self.song_query.value

        await interaction.response.send_message(
            f"🔍 Suche nach **{query}**...", ephemeral=True
        )

        results = await search_tracks(query, limit=1)
        if not results:
            await interaction.followup.send(
                "❌ Kein Song gefunden! Bitte versuche einen anderen Titel.",
                ephemeral=True,
            )
            return

        entry = results[0]
        song_info = {
            "title": entry.get("title", "Unbekannt"),
            "webpage_url": _get_reference_url(entry, query),
            "duration": int(entry.get("duration", 0)) if entry.get("duration") else 0,
            "thumbnail": _entry_thumbnail(entry),
        }

        if self.player.id == self.battle_data["challenger_id"]:
            self.battle_data["song_1"] = song_info
        else:
            self.battle_data["song_2"] = song_info

        await interaction.followup.send(
            f"✅ Song gewählt: **{song_info['title']}**", ephemeral=True
        )


class SongSelectButtonView(discord.ui.View):
    def __init__(self, player: discord.Member, battle_data: dict):
        super().__init__(timeout=60)
        self.player = player
        self.battle_data = battle_data

    @discord.ui.button(label="🎵 Song wählen", style=discord.ButtonStyle.primary)
    async def btn_select(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message(
                "❌ Dieser Button ist nicht für dich!", ephemeral=True
            )
        await interaction.response.send_modal(
            SongSelectModal(self.player, self.battle_data)
        )


class BattleVoteView(discord.ui.View):
    def __init__(self, player1: discord.Member, player2: discord.Member):
        super().__init__(timeout=BATTLE_VOTE_SECONDS)
        self.player1 = player1
        self.player2 = player2
        self.votes: dict[int, int] = {}  # user_id -> player_nr (1 oder 2)

    @discord.ui.button(label="🔴 Song 1", style=discord.ButtonStyle.danger)
    async def btn_vote1(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in (self.player1.id, self.player2.id):
            return await interaction.response.send_message(
                "❌ Spieler können nicht voten!", ephemeral=True
            )
        self.votes[interaction.user.id] = 1
        await interaction.response.send_message(
            f"✅ Du hast für **Song 1** gestimmt! ({len([v for v in self.votes.values() if v == 1])} Votes)",
            ephemeral=True,
        )

    @discord.ui.button(label="🔵 Song 2", style=discord.ButtonStyle.primary)
    async def btn_vote2(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in (self.player1.id, self.player2.id):
            return await interaction.response.send_message(
                "❌ Spieler können nicht voten!", ephemeral=True
            )
        self.votes[interaction.user.id] = 2
        await interaction.response.send_message(
            f"✅ Du hast für **Song 2** gestimmt! ({len([v for v in self.votes.values() if v == 2])} Votes)",
            ephemeral=True,
        )


async def run_music_battle(
    channel: discord.TextChannel,
    player1: discord.Member,
    player2: discord.Member,
    einsatz: int,
):
    """Führt ein komplettes Musik-Battle mit TTS und Voice-Playback durch."""
    battle_data = {
        "challenger_id": player1.id,
        "opponent_id": player2.id,
        "song_1": None,
        "song_2": None,
    }
    active_battles[channel.id] = battle_data

    guild = channel.guild
    state = get_music_state(guild.id, d_bot)

    # Prüfen ob Bot in einem Voice-Kanal ist
    voice_client = state.voice_client
    was_playing = False
    paused_song = None

    if voice_client and voice_client.is_connected():
        # Musik pausieren falls sie läuft
        if voice_client.is_playing():
            was_playing = True
            paused_song = state.current
            voice_client.pause()
            await channel.send(
                embed=discord.Embed(
                    title="⏸️ Musik pausiert",
                    description="Die aktuelle Musik wurde für das Battle pausiert!",
                    color=COLOR_BATTLE,
                )
            )
            await asyncio.sleep(1)
    else:
        # Bot ist nicht im VC – prüfen ob beide Spieler in einem VC sind
        vc_to_join = None
        if player1.voice and player1.voice.channel:
            vc_to_join = player1.voice.channel
        elif player2.voice and player2.voice.channel:
            vc_to_join = player2.voice.channel

        if vc_to_join:
            try:
                voice_client = await vc_to_join.connect(self_deaf=True)
                state.voice_client = voice_client
            except Exception as e:
                await channel.send(f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}")
                update_balance(player1.id, einsatz)
                update_balance(player2.id, einsatz)
                active_battles.pop(channel.id, None)
                return
        else:
            await channel.send(
                embed=discord.Embed(
                    title="⚠️ Kein Voice-Kanal",
                    description=(
                        "Mindestens ein Spieler muss in einem Voice-Kanal sein!\n"
                        "Die Songs werden trotzdem als Links präsentiert."
                    ),
                    color=COLOR_WARNING,
                )
            )
            voice_client = None

    # ── Song-Auswahl Phase ───────────────────────────────

    embed = discord.Embed(
        title="🎵 Song-Auswahl Phase",
        description=(
            f"Beide Spieler müssen jetzt ihren Song wählen!\n\n"
            f"🔴 {player1.mention} – Klicke auf **Song wählen**\n"
            f"🔵 {player2.mention} – Klicke auf **Song wählen**\n\n"
            f"⏰ Ihr habt **60 Sekunden**!"
        ),
        color=COLOR_BATTLE,
    )

    view1 = SongSelectButtonView(player1, battle_data)
    view2 = SongSelectButtonView(player2, battle_data)

    await channel.send(
        content=f"{player1.mention} wähle deinen Song:",
        embed=embed, view=view1,
    )

    await channel.send(
        content=f"{player2.mention} wähle deinen Song:",
        view=view2,
    )

    # Warten bis beide Songs gewählt sind
    for _ in range(60):
        await asyncio.sleep(1)
        if battle_data["song_1"] and battle_data["song_2"]:
            break

    if not battle_data["song_1"] or not battle_data["song_2"]:
        update_balance(player1.id, einsatz)
        update_balance(player2.id, einsatz)
        active_battles.pop(channel.id, None)
        await channel.send(
            embed=discord.Embed(
                title="❌ Musik-Battle abgebrochen",
                description="Nicht beide Spieler haben einen Song gewählt. Einsätze zurückgegeben.",
                color=COLOR_ERROR,
            )
        )
        # Musik fortsetzen falls pausiert
        if was_playing and voice_client and voice_client.is_connected():
            if voice_client.is_paused():
                voice_client.resume()
        return

    song1 = battle_data["song_1"]
    song2 = battle_data["song_2"]

    # ── Songs ankündigen ─────────────────────────────────

    embed = discord.Embed(
        title="🎵 MUSIK-BATTLE STARTET!",
        description=(
            f"**🔴 {player1.display_name}:** {song1['title']}\n"
            f"**🔵 {player2.display_name}:** {song2['title']}\n\n"
            f"💰 **Pot:** {einsatz * 2:,} 🪙\n\n"
            f"🔊 Die Songs werden jetzt im Voice-Kanal angespielt!\n"
            f"Jeweils **{BATTLE_LISTEN_SECONDS} Sekunden** pro Song."
        ),
        color=COLOR_BATTLE,
    )
    await channel.send(embed=embed)
    await asyncio.sleep(2)

    # ── Song 1 abspielen ─────────────────────────────────

    tts_files = []

    if voice_client and voice_client.is_connected():
        # TTS Ankündigung Song 1
        tts1_text = f"{player1.display_name} hat {song1['title']} gewählt."
        tts1_path = await generate_tts_file(tts1_text)
        if tts1_path:
            tts_files.append(tts1_path)

        embed1 = discord.Embed(
            title=f"🔴 Song 1 – {player1.display_name}",
            description=f"**{song1['title']}**\n\n🔊 Wird jetzt abgespielt...",
            color=0xFF0000,
        )
        if song1.get("thumbnail"):
            embed1.set_thumbnail(url=song1["thumbnail"])
        embed1.set_footer(text=f"⏱️ {BATTLE_LISTEN_SECONDS} Sekunden Vorschau")
        await channel.send(embed=embed1)

        # TTS abspielen
        if tts1_path:
            await play_audio_file_and_wait(voice_client, tts1_path, timeout=15)
            await asyncio.sleep(0.5)

        # Song 1 Stream auflösen und abspielen (zufälliger Ausschnitt)
        song1_url = None
        song1_duration = song1.get("duration", 0)
        if song1.get("webpage_url"):
            dummy_song = SongInfo(
                song1["title"], "", song1["webpage_url"],
                song1_duration, "", None,
            )
            song1_url = await resolve_song_url(dummy_song)

        if song1_url:
            skip1 = 0
            if song1_duration > BATTLE_LISTEN_SECONDS + 30:
                max_start = song1_duration - BATTLE_LISTEN_SECONDS - 15
                skip1 = random.randint(15, max(15, max_start))
            elif song1_duration > BATTLE_LISTEN_SECONDS + 10:
                skip1 = random.randint(5, song1_duration - BATTLE_LISTEN_SECONDS - 5)

            if skip1 > 0:
                seek_opts = {
                    'before_options': f'-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {skip1}',
                    'options': '-vn',
                }
                done1 = asyncio.Event()
                def after1(e):
                    done1.set()
                try:
                    src1 = discord.FFmpegPCMAudio(song1_url, **seek_opts)
                    voice_client.play(src1, after=after1)
                    try:
                        await asyncio.wait_for(done1.wait(), timeout=BATTLE_LISTEN_SECONDS)
                    except asyncio.TimeoutError:
                        if voice_client.is_playing():
                            voice_client.stop()
                            await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"❌ Battle Song1 Fehler: {e}")
            else:
                await play_stream_url_for_duration(
                    voice_client, song1_url, BATTLE_LISTEN_SECONDS
                )
        else:
            await channel.send(
                f"⚠️ Konnte Song 1 nicht abspielen. "
                f"[Hier anhören]({song1.get('webpage_url', '')})"
            )
            await asyncio.sleep(BATTLE_LISTEN_SECONDS)
            

        await asyncio.sleep(1)

        # ── Song 2 abspielen ─────────────────────────────

        tts2_text = f"{player2.display_name} hat {song2['title']} gewählt."
        tts2_path = await generate_tts_file(tts2_text)
        if tts2_path:
            tts_files.append(tts2_path)

        embed2 = discord.Embed(
            title=f"🔵 Song 2 – {player2.display_name}",
            description=f"**{song2['title']}**\n\n🔊 Wird jetzt abgespielt...",
            color=0x0000FF,
        )
        if song2.get("thumbnail"):
            embed2.set_thumbnail(url=song2["thumbnail"])
        embed2.set_footer(text=f"⏱️ {BATTLE_LISTEN_SECONDS} Sekunden Vorschau")
        await channel.send(embed=embed2)

        # TTS abspielen
        if tts2_path:
            await play_audio_file_and_wait(voice_client, tts2_path, timeout=15)
            await asyncio.sleep(0.5)

        # Song 2 Stream auflösen und abspielen (zufälliger Ausschnitt)
        song2_url = None
        song2_duration = song2.get("duration", 0)
        if song2.get("webpage_url"):
            dummy_song2 = SongInfo(
                song2["title"], "", song2["webpage_url"],
                song2_duration, "", None,
            )
            song2_url = await resolve_song_url(dummy_song2)

        if song2_url:
            skip2 = 0
            if song2_duration > BATTLE_LISTEN_SECONDS + 30:
                max_start = song2_duration - BATTLE_LISTEN_SECONDS - 15
                skip2 = random.randint(15, max(15, max_start))
            elif song2_duration > BATTLE_LISTEN_SECONDS + 10:
                skip2 = random.randint(5, song2_duration - BATTLE_LISTEN_SECONDS - 5)

            if skip2 > 0:
                seek_opts2 = {
                    'before_options': f'-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {skip2}',
                    'options': '-vn',
                }
                done2 = asyncio.Event()
                def after2(e):
                    done2.set()
                try:
                    src2 = discord.FFmpegPCMAudio(song2_url, **seek_opts2)
                    voice_client.play(src2, after=after2)
                    try:
                        await asyncio.wait_for(done2.wait(), timeout=BATTLE_LISTEN_SECONDS)
                    except asyncio.TimeoutError:
                        if voice_client.is_playing():
                            voice_client.stop()
                            await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"❌ Battle Song2 Fehler: {e}")
            else:
                await play_stream_url_for_duration(
                    voice_client, song2_url, BATTLE_LISTEN_SECONDS
                )
        else:
            await channel.send(
                f"⚠️ Konnte Song 2 nicht abspielen. "
                f"[Hier anhören]({song2.get('webpage_url', '')})"
            )
            await asyncio.sleep(BATTLE_LISTEN_SECONDS)

        await asyncio.sleep(1)

        # TTS: Voting Ankündigung
        tts_vote_text = "Abstimmung! Stimmt jetzt für euren Favoriten!"
        tts_vote_path = await generate_tts_file(tts_vote_text)
        if tts_vote_path:
            tts_files.append(tts_vote_path)
            await play_audio_file_and_wait(voice_client, tts_vote_path, timeout=10)
            await asyncio.sleep(0.5)

    else:
        # Kein Voice – nur Text-basiert
        embed1 = discord.Embed(
            title=f"🔴 Song 1 – {player1.display_name}",
            description=f"**{song1['title']}**",
            color=0xFF0000,
        )
        if song1.get("webpage_url"):
            embed1.add_field(
                name="🔗 Link",
                value=f"[Anhören]({song1['webpage_url']})",
                inline=True,
            )
        if song1.get("thumbnail"):
            embed1.set_thumbnail(url=song1["thumbnail"])
        await channel.send(embed=embed1)

        await asyncio.sleep(3)

        embed2 = discord.Embed(
            title=f"🔵 Song 2 – {player2.display_name}",
            description=f"**{song2['title']}**",
            color=0x0000FF,
        )
        if song2.get("webpage_url"):
            embed2.add_field(
                name="🔗 Link",
                value=f"[Anhören]({song2['webpage_url']})",
                inline=True,
            )
        if song2.get("thumbnail"):
            embed2.set_thumbnail(url=song2["thumbnail"])
        await channel.send(embed=embed2)

        await asyncio.sleep(2)

    # ── Voting Phase ─────────────────────────────────────

    vote_view = BattleVoteView(player1, player2)
    vote_embed = discord.Embed(
        title="🗳️ ABSTIMMUNG!",
        description=(
            f"Stimmt für euren Favoriten!\n\n"
            f"🔴 **Song 1:** {song1['title']} ({player1.display_name})\n"
            f"🔵 **Song 2:** {song2['title']} ({player2.display_name})\n\n"
            f"⏰ Ihr habt **{BATTLE_VOTE_SECONDS} Sekunden**!\n"
            f"*Spieler können nicht für sich selbst voten!*"
        ),
        color=COLOR_BATTLE,
    )
    await channel.send(embed=vote_embed, view=vote_view)

    # Warten auf Voting-Ende
    await asyncio.sleep(BATTLE_VOTE_SECONDS)

    # ── Ergebnis ─────────────────────────────────────────

    votes_1 = len([v for v in vote_view.votes.values() if v == 1])
    votes_2 = len([v for v in vote_view.votes.values() if v == 2])
    total_votes = len(vote_view.votes)

    pot = einsatz * 2
    fee = int(pot * 0.05)
    payout = pot - fee

    if votes_1 > votes_2:
        winner = player1
        loser = player2
        winner_song = song1
    elif votes_2 > votes_1:
        winner = player2
        loser = player1
        winner_song = song2
    else:
        if random.random() < 0.5:
            winner = player1
            loser = player2
            winner_song = song1
        else:
            winner = player2
            loser = player1
            winner_song = song2

    update_balance(winner.id, payout)

    # Voter belohnen
    voter_reward = 10
    for voter_id in vote_view.votes:
        update_balance(voter_id, voter_reward)

    # TTS Gewinner Ankündigung
    if voice_client and voice_client.is_connected():
        if votes_1 == votes_2:
            tts_result = f"Gleichstand! Der Münzwurf entscheidet: {winner.display_name} gewinnt das Battle!"
        else:
            tts_result = f"{winner.display_name} gewinnt das Musik-Battle mit {max(votes_1, votes_2)} zu {min(votes_1, votes_2)} Stimmen!"

        tts_result_path = await generate_tts_file(tts_result)
        if tts_result_path:
            tts_files.append(tts_result_path)
            await play_audio_file_and_wait(voice_client, tts_result_path, timeout=15)
            await asyncio.sleep(0.5)

    result_embed = discord.Embed(
        title="🏆 Musik-Battle Ergebnis!",
        color=COLOR_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    result_embed.add_field(
        name=f"🔴 {player1.display_name}",
        value=f"**{song1['title']}**\n🗳️ **{votes_1}** Votes",
        inline=True,
    )
    result_embed.add_field(name="⚔️", value="VS", inline=True)
    result_embed.add_field(
        name=f"🔵 {player2.display_name}",
        value=f"**{song2['title']}**\n🗳️ **{votes_2}** Votes",
        inline=True,
    )
    if votes_1 == votes_2:
        result_embed.add_field(
            name="🏆 Gewinner (Münzwurf bei Gleichstand)",
            value=f"{winner.mention} gewinnt **{payout:,} 🪙**!",
            inline=False,
        )
    else:
        result_embed.add_field(
            name="🏆 Gewinner",
            value=f"{winner.mention} gewinnt **{payout:,} 🪙**!",
            inline=False,
        )

    if total_votes > 0:
        result_embed.add_field(
            name="🗳️ Abstimmung",
            value=f"**{total_votes}** Voter (je +{voter_reward} 🪙 Belohnung)",
            inline=False,
        )
    else:
        result_embed.add_field(
            name="🗳️ Abstimmung",
            value="*Niemand hat abgestimmt – Münzwurf!*",
            inline=False,
        )

    result_embed.set_thumbnail(url=winner.display_avatar.url)
    result_embed.set_footer(
        text=f"Pot: {pot:,} • Gebühr: {fee:,} 🪙 • Kontostand {winner.display_name}: {get_balance(winner.id):,} 🪙"
    )

    await channel.send(embed=result_embed)
    active_battles.pop(channel.id, None)

    # ── TTS-Dateien aufräumen ────────────────────────────

    for tts_path in tts_files:
        cleanup_tts_file(tts_path)

    # ── Musik fortsetzen ─────────────────────────────────

    if was_playing and voice_client and voice_client.is_connected():
        await asyncio.sleep(1)

        if paused_song and state.current == paused_song:
            # Versuche den pausierten Song fortzusetzen
            if voice_client.is_paused():
                voice_client.resume()
                await channel.send(
                    embed=discord.Embed(
                        title="▶️ Musik fortgesetzt",
                        description=f"**{paused_song.title}** wird weiter abgespielt!",
                        color=COLOR_MUSIC,
                    ),
                    delete_after=10,
                )
            else:
                # Song wurde gestoppt – nächsten aus Queue spielen
                await play_next(state, channel)
        else:
            # Nächsten Song aus Queue spielen
            await play_next(state, channel)

# ══════════════════════════════════════════════════════════
#         FEATURE: ABWESENHEITS-SYSTEM FÜR MODS
# ══════════════════════════════════════════════════════════

def is_user_absent(uid: str, date_str: str) -> bool:
    """Prüft ob ein User für ein bestimmtes Datum abgemeldet ist."""
    absences = load_absences()
    user_absences = absences.get("absences", {}).get(uid, [])
    return date_str in user_absences


def add_absence(uid: str, date_str: str, reason: str = "") -> None:
    """Fügt eine Abwesenheit hinzu."""
    absences = load_absences()
    if uid not in absences["absences"]:
        absences["absences"][uid] = []
    if date_str not in absences["absences"][uid]:
        absences["absences"][uid].append(date_str)
    # Reason separat speichern
    reason_key = f"{uid}_reasons"
    if reason_key not in absences:
        absences[reason_key] = {}
    absences[reason_key][date_str] = reason
    save_absences(absences)


def remove_absence(uid: str, date_str: str) -> None:
    """Entfernt eine Abwesenheit."""
    absences = load_absences()
    if uid in absences.get("absences", {}):
        if date_str in absences["absences"][uid]:
            absences["absences"][uid].remove(date_str)
    reason_key = f"{uid}_reasons"
    if reason_key in absences and date_str in absences[reason_key]:
        del absences[reason_key][date_str]
    save_absences(absences)


def get_user_absences(uid: str) -> list[dict]:
    """Gibt alle Abwesenheiten eines Users zurück."""
    absences = load_absences()
    dates = absences.get("absences", {}).get(uid, [])
    reason_key = f"{uid}_reasons"
    reasons = absences.get(reason_key, {})
    result = []
    for d in sorted(dates):
        result.append({
            "date": d,
            "reason": reasons.get(d, "Kein Grund angegeben"),
        })
    return result

# ══════════════════════════════════════════════════════════
#         FEATURE: RADIO SYSTEM
# ══════════════════════════════════════════════════════════

async def search_radio_station(query: str) -> dict | None:
    """Sucht einen Radiosender über die radio-browser API."""
    try:
        search_url = (
            f"https://de1.api.radio-browser.info/json/stations/byname/{query}"
            f"?limit=1&order=clickcount&reverse=true&hidebroken=true"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                search_url,
                headers={"User-Agent": "DiscordBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        station = data[0]
                        return {
                            "name": station.get("name", "Unbekannt"),
                            "url": station.get("url_resolved") or station.get("url", ""),
                            "country": station.get("country", ""),
                            "tags": station.get("tags", ""),
                            "favicon": station.get("favicon", ""),
                            "codec": station.get("codec", ""),
                            "bitrate": station.get("bitrate", 0),
                        }
    except Exception as e:
        print(f"❌ Radio-Suche Fehler: {e}")
    return None


async def search_radio_stations_list(query: str, limit: int = 10) -> list[dict]:
    """Sucht mehrere Radiosender."""
    try:
        search_url = (
            f"https://de1.api.radio-browser.info/json/stations/byname/{query}"
            f"?limit={limit}&order=clickcount&reverse=true&hidebroken=true"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                search_url,
                headers={"User-Agent": "DiscordBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = []
                    for station in data:
                        url = station.get("url_resolved") or station.get("url", "")
                        if not url:
                            continue
                        results.append({
                            "name": station.get("name", "Unbekannt"),
                            "url": url,
                            "country": station.get("country", ""),
                            "tags": station.get("tags", ""),
                            "favicon": station.get("favicon", ""),
                            "codec": station.get("codec", ""),
                            "bitrate": station.get("bitrate", 0),
                        })
                    return results
    except Exception as e:
        print(f"❌ Radio-Suche Fehler: {e}")
    return []


async def start_radio_stream(
    interaction: discord.Interaction,
    station: dict,
    state,
) -> None:
    """Startet einen Radio-Stream im Voice-Kanal."""
    user_vc = interaction.user.voice.channel

    # Voice verbinden
    if state.voice_client is None or not state.voice_client.is_connected():
        try:
            perms = user_vc.permissions_for(interaction.guild.me)
            if not perms.connect or not perms.speak:
                await interaction.followup.send(
                    f"❌ Keine Berechtigung für **{user_vc.name}**!",
                    ephemeral=True,
                )
                return
            state.voice_client = await user_vc.connect(self_deaf=True)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Konnte nicht beitreten: {e}", ephemeral=True
            )
            return

    # Aktuelle Musik stoppen
    if state.voice_client.is_playing():
        state.voice_client.stop()
        await asyncio.sleep(0.5)

    state.queue.clear()
    state.current = None
    state.reset_idle()

    # Radio-Stream starten
    try:
        source = discord.FFmpegPCMAudio(
            station["url"],
            before_options='-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            options='-vn',
        )

        state.voice_client.play(source, after=lambda e: None)

        radio_song = SongInfo(
            f"📻 {station['name']}", station["url"], "",
            0, station.get("favicon", ""), interaction.user,
        )
        state.current = radio_song

        embed = discord.Embed(
            title=f"📻 {station['name']}",
            color=COLOR_RADIO,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        if station.get("country"):
            embed.add_field(name="🌍 Land", value=station["country"], inline=True)
        if station.get("bitrate") and station["bitrate"] > 0:
            embed.add_field(name="🔊 Bitrate", value=f"{station['bitrate']} kbps", inline=True)
        if station.get("tags"):
            tags = station["tags"][:100]
            embed.add_field(name="🏷️ Tags", value=tags, inline=True)

        embed.add_field(name="👤 Gestartet von", value=interaction.user.mention, inline=True)
        embed.add_field(name="🔊 Kanal", value=user_vc.name, inline=True)

        if station.get("favicon") and station["favicon"].startswith("http"):
            embed.set_thumbnail(url=station["favicon"])

        embed.set_footer(text="📻 Radio • /stop zum Stoppen • /radio zum Wechseln")

        await interaction.followup.send(embed=embed)

        try:
            await d_bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=f"📻 {station['name'][:128]}",
                )
            )
        except Exception:
            pass

    except Exception as e:
        await interaction.followup.send(f"❌ Konnte Radio nicht starten: {e}")


class RadioPresetSelectView(discord.ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=30)
        self.user = user

        options = []
        for key, station in RADIO_STATIONS.items():
            options.append(
                discord.SelectOption(
                    label=station["name"],
                    description=station["description"][:100],
                    value=key,
                    emoji=station["emoji"],
                )
            )

        self.select = discord.ui.Select(
            placeholder="📻 Sender auswählen...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(
                "❌ Nicht für dich!", ephemeral=True
            )

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                "❌ Du musst in einem Voice-Kanal sein!", ephemeral=True
            )

        station_key = self.select.values[0]
        preset = RADIO_STATIONS[station_key]

        await interaction.response.defer()

        state = get_music_state(interaction.guild_id, d_bot)

        station_data = {
            "name": preset["name"],
            "url": preset["url"],
            "country": "",
            "tags": preset["description"],
            "favicon": "",
            "bitrate": 0,
        }

        await start_radio_stream(interaction, station_data, state)


class RadioSearchSelectView(discord.ui.View):
    def __init__(self, user: discord.Member, stations: list[dict]):
        super().__init__(timeout=30)
        self.user = user
        self.stations = stations

        options = []
        for i, station in enumerate(stations[:10]):
            name = station["name"][:100]
            country = station.get("country", "")[:50]
            bitrate = station.get("bitrate", 0)
            desc = f"{country}"
            if bitrate > 0:
                desc += f" • {bitrate}kbps"
            desc = desc[:100] if desc else "Radiosender"

            options.append(
                discord.SelectOption(
                    label=name,
                    description=desc,
                    value=str(i),
                )
            )

        self.select = discord.ui.Select(
            placeholder="📻 Sender auswählen...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(
                "❌ Nicht für dich!", ephemeral=True
            )

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                "❌ Du musst in einem Voice-Kanal sein!", ephemeral=True
            )

        idx = int(self.select.values[0])
        station = self.stations[idx]

        await interaction.response.defer()

        state = get_music_state(interaction.guild_id, d_bot)
        await start_radio_stream(interaction, station, state)

# ══════════════════════════════════════════════════════════
#         FEATURE: SONG QUIZ
# ══════════════════════════════════════════════════════════

async def run_song_quiz(
    channel: discord.TextChannel,
    guild: discord.Guild,
    rounds: int,
    host: discord.Member,
):
    """Führt ein komplettes Song-Quiz durch."""
    state = get_music_state(guild.id, d_bot)
    voice_client = state.voice_client

    was_playing = False
    paused_song = None

    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            was_playing = True
            paused_song = state.current
            voice_client.pause()
            await channel.send(
                embed=discord.Embed(
                    title="⏸️ Musik pausiert",
                    description="Die aktuelle Musik wurde für das Song-Quiz pausiert!",
                    color=COLOR_QUIZ,
                )
            )
            await asyncio.sleep(1)
    else:
        if host.voice and host.voice.channel:
            try:
                voice_client = await host.voice.channel.connect(self_deaf=True)
                state.voice_client = voice_client
            except Exception as e:
                await channel.send(f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}")
                active_quizzes.pop(channel.id, None)
                return
        else:
            await channel.send("❌ Du musst in einem Voice-Kanal sein für das Song-Quiz!")
            active_quizzes.pop(channel.id, None)
            return

    available_songs = list(QUIZ_SONGS)
    random.shuffle(available_songs)
    quiz_songs = available_songs[:min(rounds, len(available_songs))]

    scores: dict[int, int] = {}
    tts_files = []

    embed = discord.Embed(
        title="🎵 SONG QUIZ STARTET!",
        description=(
            f"**{len(quiz_songs)} Runden**\n\n"
            f"🎧 Ein **{SONG_QUIZ_DURATION}s Ausschnitt** wird abgespielt.\n"
            f"📝 Danach habt ihr **{SONG_QUIZ_GUESS_DURATION}s** zum Raten!\n\n"
            f"**Punkte:**\n"
            f"🎯 Titel erraten: **{SONG_QUIZ_POINTS['title']} Punkte**\n"
            f"🎤 Künstler erraten: **{SONG_QUIZ_POINTS['artist']} Punkte**\n"
            f"⭐ Beides erraten: **{SONG_QUIZ_POINTS['both']} Punkte**\n\n"
            f"*Schreibt eure Antwort einfach in den Chat!*"
        ),
        color=COLOR_QUIZ,
    )
    await channel.send(embed=embed)
    await asyncio.sleep(3)

    for round_num, song_data in enumerate(quiz_songs, 1):
        # Prüfe ob Quiz abgebrochen wurde
        if channel.id not in active_quizzes:
            break

        title = song_data["title"]
        artist = song_data["artist"]
        search_query = f"{artist} - {title}"

        # TTS Ankündigung
        tts_text = f"Runde {round_num}. Welcher Song ist das?"
        tts_path = await generate_tts_file(tts_text)
        if tts_path:
            tts_files.append(tts_path)
            if voice_client and voice_client.is_connected():
                await play_audio_file_and_wait(voice_client, tts_path, timeout=10)
                await asyncio.sleep(0.3)

        # Song suchen
        results = await search_tracks(search_query, limit=1)
        stream_url = None
        song_duration = 0

        if results:
            entry = results[0]
            webpage_url = _get_reference_url(entry, "")
            song_duration = int(entry.get("duration", 0)) if entry.get("duration") else 0
            if webpage_url:
                dummy = SongInfo(title, "", webpage_url, song_duration, "", None)
                stream_url = await resolve_song_url(dummy)

        round_embed = discord.Embed(
            title=f"🎵 Runde {round_num}/{len(quiz_songs)}",
            description=(
                f"🔊 **Hört genau hin!** ({SONG_QUIZ_DURATION}s Ausschnitt)\n\n"
                f"Danach habt ihr **{SONG_QUIZ_GUESS_DURATION} Sekunden** zum Raten!\n"
                f"Schreibt **Titel** und/oder **Künstler** in den Chat!"
            ),
            color=COLOR_QUIZ,
        )
        round_embed.set_footer(text="🎯 Titel = 50P • 🎤 Künstler = 30P • ⭐ Beides = 100P")
        await channel.send(embed=round_embed)

        # Song abspielen – zufälliger Ausschnitt
        if stream_url and voice_client and voice_client.is_connected():
            # Zufälligen Startpunkt berechnen
            skip_seconds = 0
            if song_duration > SONG_QUIZ_DURATION + 30:
                # Nicht die ersten 15s (oft Intro) und nicht die letzten 15s
                max_start = song_duration - SONG_QUIZ_DURATION - 15
                min_start = min(15, max_start)
                skip_seconds = random.randint(min_start, max(min_start, max_start))
            elif song_duration > SONG_QUIZ_DURATION + 10:
                skip_seconds = random.randint(5, song_duration - SONG_QUIZ_DURATION - 5)

            # FFmpeg mit Seek-Option
            seek_options = FFMPEG_OPTIONS.copy()
            if skip_seconds > 0:
                seek_options = {
                    'before_options': f'-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {skip_seconds}',
                    'options': '-vn',
                }

            done_event = asyncio.Event()

            def after_clip(error):
                if error:
                    print(f"❌ Quiz-Clip Fehler: {error}")
                done_event.set()

            try:
                source = discord.FFmpegPCMAudio(stream_url, **seek_options)
                voice_client.play(source, after=after_clip)

                try:
                    await asyncio.wait_for(done_event.wait(), timeout=SONG_QUIZ_DURATION)
                except asyncio.TimeoutError:
                    if voice_client.is_playing():
                        voice_client.stop()
                        await asyncio.sleep(0.3)
            except Exception as e:
                print(f"❌ Quiz Song abspielen Fehler: {e}")
                await asyncio.sleep(2)
        else:
            await channel.send("⚠️ Song konnte nicht abgespielt werden – ratet trotzdem!")

        # ── Rate-Phase ───────────────────────────────────

        rate_embed = discord.Embed(
            title=f"⏰ RATEN! Runde {round_num}/{len(quiz_songs)}",
            description=(
                f"🤔 Welcher Song war das?\n\n"
                f"Schreibt **Titel**, **Künstler** oder **beides** in den Chat!\n"
                f"⏰ Ihr habt **{SONG_QUIZ_GUESS_DURATION} Sekunden**!"
            ),
            color=COLOR_WARNING,
        )
        rate_msg = await channel.send(embed=rate_embed)

        # Zeitstempel für die Rate-Phase
        guess_start = datetime.datetime.now(datetime.timezone.utc)

        title_lower = title.lower().strip()
        artist_lower = artist.lower().strip()

        title_variants = set()
        title_variants.add(title_lower)
        title_variants.add(title_lower.replace("'", ""))
        title_variants.add(title_lower.replace("'", ""))
        title_variants.add(title_lower.replace("-", " "))
        title_variants.add(title_lower.replace(".", ""))
        # Auch einzelne Wörter mit 5+ Buchstaben
        for word in title_lower.split():
            if len(word) >= 5:
                title_variants.add(word)

        artist_variants = set()
        artist_variants.add(artist_lower)
        artist_variants.add(artist_lower.replace("'", ""))
        artist_variants.add(artist_lower.replace("'", ""))
        artist_variants.add(artist_lower.replace("-", " "))
        artist_variants.add(artist_lower.replace(".", ""))
        for word in artist_lower.split():
            if len(word) >= 4:
                artist_variants.add(word)

        round_winners = {}
        title_guessed = False
        artist_guessed = False

        # Rate-Schleife
        end_time = guess_start + datetime.timedelta(seconds=SONG_QUIZ_GUESS_DURATION)

        while datetime.datetime.now(datetime.timezone.utc) < end_time:
            if channel.id not in active_quizzes:
                break

            remaining = (end_time - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            if remaining <= 0:
                break

            # Nachrichten der letzten 2 Sekunden prüfen
            check_after = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=2.5)

            try:
                async for msg in channel.history(limit=20, after=check_after):
                    if msg.author.bot:
                        continue
                    if msg.author.id in round_winners:
                        continue
                    if msg.created_at < guess_start:
                        continue

                    guess = msg.content.lower().strip()
                    if len(guess) < 3:
                        continue

                    got_title = False
                    got_artist = False

                    for tv in title_variants:
                        if tv in guess or guess in tv:
                            if len(min(tv, guess, key=len)) >= 3:
                                got_title = True
                                break

                    for av in artist_variants:
                        if av in guess or guess in av:
                            if len(min(av, guess, key=len)) >= 3:
                                got_artist = True
                                break

                    if got_title and got_artist and not (title_guessed and artist_guessed):
                        round_winners[msg.author.id] = "both"
                        points = SONG_QUIZ_POINTS["both"]
                        scores[msg.author.id] = scores.get(msg.author.id, 0) + points
                        title_guessed = True
                        artist_guessed = True
                        try:
                            await msg.add_reaction("⭐")
                        except Exception:
                            pass
                    elif got_title and not title_guessed:
                        round_winners[msg.author.id] = "title"
                        points = SONG_QUIZ_POINTS["title"]
                        scores[msg.author.id] = scores.get(msg.author.id, 0) + points
                        title_guessed = True
                        try:
                            await msg.add_reaction("🎯")
                        except Exception:
                            pass
                    elif got_artist and not artist_guessed:
                        round_winners[msg.author.id] = "artist"
                        points = SONG_QUIZ_POINTS["artist"]
                        scores[msg.author.id] = scores.get(msg.author.id, 0) + points
                        artist_guessed = True
                        try:
                            await msg.add_reaction("🎤")
                        except Exception:
                            pass

            except Exception as e:
                print(f"❌ [Quiz] Nachrichten-Check Fehler: {e}")

            # Wenn beides erraten, Runde beenden
            if title_guessed and artist_guessed:
                break

            await asyncio.sleep(2)

        # ── Runden-Auflösung ─────────────────────────────

        result_lines = []
        if round_winners:
            for uid, guess_type in round_winners.items():
                member = guild.get_member(uid)
                name = member.mention if member else f"<@{uid}>"
                if guess_type == "both":
                    result_lines.append(f"⭐ {name} — Titel + Künstler! (+{SONG_QUIZ_POINTS['both']}P)")
                elif guess_type == "title":
                    result_lines.append(f"🎯 {name} — Titel erraten! (+{SONG_QUIZ_POINTS['title']}P)")
                elif guess_type == "artist":
                    result_lines.append(f"🎤 {name} — Künstler erraten! (+{SONG_QUIZ_POINTS['artist']}P)")
        else:
            result_lines.append("❌ Niemand hat es erraten!")

        reveal_embed = discord.Embed(
            title=f"🎵 Auflösung Runde {round_num}",
            description=(
                f"**🎵 {title}**\n"
                f"**🎤 {artist}**\n\n"
                + "\n".join(result_lines)
            ),
            color=COLOR_SUCCESS if round_winners else COLOR_ERROR,
        )

        if scores:
            sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            score_lines = []
            for i, (uid, pts) in enumerate(sorted_scores[:5], 1):
                member = guild.get_member(uid)
                name = member.display_name if member else f"User {uid}"
                medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"`#{i}`"
                score_lines.append(f"{medal} **{name}** — {pts} Punkte")
            reveal_embed.add_field(
                name="📊 Punktestand",
                value="\n".join(score_lines),
                inline=False,
            )

        await channel.send(embed=reveal_embed)

        # Aufräumen
        try:
            await rate_msg.delete()
        except Exception:
            pass

        if round_num < len(quiz_songs) and channel.id in active_quizzes:
            await asyncio.sleep(3)

    # ── Endergebnis ──────────────────────────────────────

    active_quizzes.pop(channel.id, None)

    if not scores:
        final_embed = discord.Embed(
            title="🎵 Song Quiz Vorbei!",
            description="Niemand hat einen Song erraten! 😅",
            color=COLOR_ERROR,
        )
        await channel.send(embed=final_embed)
    else:
        sorted_final = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        winner_id, winner_points = sorted_final[0]
        winner_member = guild.get_member(winner_id)

        coin_rewards = {0: 200, 1: 100, 2: 50}
        for i, (uid, pts) in enumerate(sorted_final[:3]):
            reward = coin_rewards.get(i, 0)
            if reward > 0:
                update_balance(uid, reward)

        if voice_client and voice_client.is_connected():
            winner_name = winner_member.display_name if winner_member else "Unbekannt"
            tts_winner = f"{winner_name} gewinnt das Song Quiz mit {winner_points} Punkten!"
            tts_path = await generate_tts_file(tts_winner)
            if tts_path:
                tts_files.append(tts_path)
                await play_audio_file_and_wait(voice_client, tts_path, timeout=15)

        final_lines = []
        for i, (uid, pts) in enumerate(sorted_final, 1):
            member = guild.get_member(uid)
            name = member.mention if member else f"<@{uid}>"
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"`#{i}`"
            reward = coin_rewards.get(i - 1, 0)
            reward_str = f" (+{reward} 🪙)" if reward > 0 else ""
            final_lines.append(f"{medal} {name} — **{pts} Punkte**{reward_str}")

        final_embed = discord.Embed(
            title="🏆 Song Quiz Ergebnis!",
            description="\n".join(final_lines),
            color=COLOR_GOLD,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        final_embed.add_field(
            name="📊 Quiz-Info",
            value=(
                f"🎵 Runden: **{len(quiz_songs)}**\n"
                f"👥 Teilnehmer: **{len(scores)}**"
            ),
            inline=False,
        )
        final_embed.set_footer(text="🎵 Song Quiz • /songquiz für eine neue Runde!")
        if winner_member:
            final_embed.set_thumbnail(url=winner_member.display_avatar.url)
        await channel.send(embed=final_embed)

    for tts_path in tts_files:
        cleanup_tts_file(tts_path)

    # Musik fortsetzen
    if was_playing and voice_client and voice_client.is_connected():
        await asyncio.sleep(1)
        if paused_song and state.current == paused_song:
            if voice_client.is_paused():
                voice_client.resume()
                await channel.send(
                    embed=discord.Embed(
                        title="▶️ Musik fortgesetzt",
                        description=f"**{paused_song.title}** wird weiter abgespielt!",
                        color=COLOR_MUSIC,
                    ),
                    delete_after=10,
                )
            else:
                await play_next(state, channel)
        else:
            await play_next(state, channel)

# ══════════════════════════════════════════════════════════
#         FEATURE: 4 GEWINNT (CONNECT 4)
# ══════════════════════════════════════════════════════════

class Connect4Game:
    def __init__(self, player1_id: int, player2_id: int):
        self.board = [[C4_EMPTY for _ in range(C4_COLS)] for _ in range(C4_ROWS)]
        self.player1_id = player1_id
        self.player2_id = player2_id
        self.current_turn = player1_id
        self.winner = None
        self.game_over = False
        self.winning_cells: list[tuple[int, int]] = []

    @property
    def current_piece(self) -> str:
        return C4_RED if self.current_turn == self.player1_id else C4_YELLOW

    @property
    def other_player(self) -> int:
        return self.player2_id if self.current_turn == self.player1_id else self.player1_id

    def drop_piece(self, col: int) -> int:
        """Lässt einen Stein in die Spalte fallen. Gibt die Zeile zurück oder -1."""
        for row in range(C4_ROWS - 1, -1, -1):
            if self.board[row][col] == C4_EMPTY:
                self.board[row][col] = self.current_piece
                return row
        return -1

    def check_winner(self, row: int, col: int) -> bool:
        """Prüft ob der letzte Zug zum Sieg geführt hat."""
        piece = self.board[row][col]
        directions = [(0, 1), (1, 0), (1, 1), (1, -1)]

        for dr, dc in directions:
            cells = [(row, col)]
            # Vorwärts
            r, c = row + dr, col + dc
            while 0 <= r < C4_ROWS and 0 <= c < C4_COLS and self.board[r][c] == piece:
                cells.append((r, c))
                r += dr
                c += dc
            # Rückwärts
            r, c = row - dr, col - dc
            while 0 <= r < C4_ROWS and 0 <= c < C4_COLS and self.board[r][c] == piece:
                cells.append((r, c))
                r -= dr
                c -= dc

            if len(cells) >= 4:
                self.winning_cells = cells[:4]
                return True
        return False

    def is_full(self) -> bool:
        return all(self.board[0][col] != C4_EMPTY for col in range(C4_COLS))

    def render_board(self) -> str:
        lines = []
        for row in range(C4_ROWS):
            cells = []
            for col in range(C4_COLS):
                cells.append(self.board[row][col])
            lines.append("".join(cells))
        # Spalten-Nummern
        numbers = "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
        lines.append(numbers)
        return "\n".join(lines)


class Connect4View(discord.ui.View):
    def __init__(
        self,
        game: Connect4Game,
        player1: discord.Member,
        player2: discord.Member,
        einsatz: int,
    ):
        super().__init__(timeout=120)
        self.game = game
        self.player1 = player1
        self.player2 = player2
        self.einsatz = einsatz
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for col in range(C4_COLS):
            disabled = self.board_full_at(col) or self.game.game_over
            btn = discord.ui.Button(
                label=str(col + 1),
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
                row=0 if col < 4 else 1,
            )
            btn.callback = self._make_callback(col)
            self.add_item(btn)

    def board_full_at(self, col: int) -> bool:
        return self.game.board[0][col] != C4_EMPTY

    def _make_callback(self, col: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.game.current_turn:
                if interaction.user.id in (self.game.player1_id, self.game.player2_id):
                    return await interaction.response.send_message(
                        "❌ Du bist nicht dran!", ephemeral=True
                    )
                return await interaction.response.send_message(
                    "❌ Du spielst nicht mit!", ephemeral=True
                )

            if self.game.game_over:
                return

            row = self.game.drop_piece(col)
            if row == -1:
                return await interaction.response.send_message(
                    "❌ Diese Spalte ist voll!", ephemeral=True
                )

            # Gewinn prüfen
            if self.game.check_winner(row, col):
                self.game.game_over = True
                self.game.winner = self.game.current_turn
                active_games.pop(self.game.player1_id, None)
                active_games.pop(self.game.player2_id, None)

                winner = self.player1 if self.game.winner == self.game.player1_id else self.player2
                loser = self.player2 if winner == self.player1 else self.player1

                pot = self.einsatz * 2
                fee = int(pot * 0.05)
                payout = pot - fee
                update_balance(winner.id, payout)

                self._build_buttons()

                embed = discord.Embed(
                    title=f"🏆 4 Gewinnt – {winner.display_name} gewinnt!",
                    description=(
                        f"{self.game.render_board()}\n\n"
                        f"🎉 {winner.mention} gewinnt **{payout:,} 🪙**!"
                    ),
                    color=COLOR_GOLD,
                )
                embed.set_footer(
                    text=f"Pot: {pot:,} • Gebühr: {fee:,} 🪙 • Kontostand {winner.display_name}: {get_balance(winner.id):,} 🪙"
                )
                self.stop()
                await interaction.response.edit_message(embed=embed, view=None)
                return

            # Unentschieden prüfen
            if self.game.is_full():
                self.game.game_over = True
                active_games.pop(self.game.player1_id, None)
                active_games.pop(self.game.player2_id, None)

                update_balance(self.game.player1_id, self.einsatz)
                update_balance(self.game.player2_id, self.einsatz)

                embed = discord.Embed(
                    title="🤝 4 Gewinnt – Unentschieden!",
                    description=(
                        f"{self.game.render_board()}\n\n"
                        f"Das Spielfeld ist voll! Einsätze zurückgegeben."
                    ),
                    color=COLOR_WARNING,
                )
                self.stop()
                await interaction.response.edit_message(embed=embed, view=None)
                return

            # Nächster Spieler
            self.game.current_turn = self.game.other_player
            current_player = self.player1 if self.game.current_turn == self.game.player1_id else self.player2
            current_piece = self.game.current_piece

            self._build_buttons()

            embed = discord.Embed(
                title="🟡🔴 4 Gewinnt",
                description=(
                    f"{self.game.render_board()}\n\n"
                    f"{current_piece} **{current_player.display_name}** ist dran!\n\n"
                    f"🔴 {self.player1.display_name} vs 🟡 {self.player2.display_name}\n"
                    f"💰 Pot: **{self.einsatz * 2:,} 🪙**"
                ),
                color=COLOR_CONNECT4,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return callback

    async def on_timeout(self):
        if not self.game.game_over:
            self.game.game_over = True
            # Wer dran war verliert
            loser_id = self.game.current_turn
            winner_id = self.game.other_player
            winner = self.player1 if winner_id == self.game.player1_id else self.player2

            pot = self.einsatz * 2
            fee = int(pot * 0.05)
            payout = pot - fee
            update_balance(winner.id, payout)

            active_games.pop(self.game.player1_id, None)
            active_games.pop(self.game.player2_id, None)


class Connect4AcceptView(discord.ui.View):
    def __init__(
        self,
        challenger: discord.Member,
        opponent: discord.Member,
        einsatz: int,
    ):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.einsatz = einsatz
        self.accepted = False

    @discord.ui.button(label="✅ Annehmen!", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message(
                f"❌ Nur {self.opponent.mention} kann annehmen!", ephemeral=True
            )
        if get_balance(self.opponent.id) < self.einsatz:
            return await interaction.response.send_message(
                f"❌ Du hast nicht genug Coins! ({get_balance(self.opponent.id):,} 🪙)",
                ephemeral=True,
            )

        self.accepted = True
        update_balance(self.opponent.id, -self.einsatz)
        active_games[self.opponent.id] = "4 Gewinnt"

        game = Connect4Game(self.challenger.id, self.opponent.id)
        view = Connect4View(game, self.challenger, self.opponent, self.einsatz)

        embed = discord.Embed(
            title="🟡🔴 4 Gewinnt",
            description=(
                f"{game.render_board()}\n\n"
                f"{C4_RED} **{self.challenger.display_name}** beginnt!\n\n"
                f"🔴 {self.challenger.display_name} vs 🟡 {self.opponent.display_name}\n"
                f"💰 Pot: **{self.einsatz * 2:,} 🪙**"
            ),
            color=COLOR_CONNECT4,
        )
        embed.set_footer(text="Wähle eine Spalte (1-7) • Der Stein fällt automatisch nach unten!")

        self.stop()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("❌ Nicht für dich!", ephemeral=True)
        update_balance(self.challenger.id, self.einsatz)
        active_games.pop(self.challenger.id, None)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ 4 Gewinnt abgelehnt",
                description=f"{self.opponent.mention} hat abgelehnt. Einsatz zurückgegeben.",
                color=COLOR_ERROR,
            ),
            view=None,
        )

    async def on_timeout(self):
        if not self.accepted:
            update_balance(self.challenger.id, self.einsatz)
            active_games.pop(self.challenger.id, None)


# ══════════════════════════════════════════════════════════
#         FEATURE: TIC TAC TOE
# ══════════════════════════════════════════════════════════

class TicTacToeGame:
    def __init__(self, player1_id: int, player2_id: int):
        self.board = [TTT_EMPTY for _ in range(9)]
        self.player1_id = player1_id  # X
        self.player2_id = player2_id  # O
        self.current_turn = player1_id
        self.winner = None
        self.game_over = False

    @property
    def current_piece(self) -> str:
        return TTT_X if self.current_turn == self.player1_id else TTT_O

    @property
    def other_player(self) -> int:
        return self.player2_id if self.current_turn == self.player1_id else self.player1_id

    def make_move(self, pos: int) -> bool:
        if self.board[pos] != TTT_EMPTY:
            return False
        self.board[pos] = self.current_piece
        return True

    def check_winner(self) -> str | None:
        """Gibt das Gewinner-Symbol zurück oder None."""
        win_lines = [
            (0, 1, 2), (3, 4, 5), (6, 7, 8),  # Reihen
            (0, 3, 6), (1, 4, 7), (2, 5, 8),  # Spalten
            (0, 4, 8), (2, 4, 6),              # Diagonalen
        ]
        for a, b, c in win_lines:
            if self.board[a] == self.board[b] == self.board[c] != TTT_EMPTY:
                return self.board[a]
        return None

    def is_full(self) -> bool:
        return TTT_EMPTY not in self.board

    def render_board(self) -> str:
        lines = []
        for row in range(3):
            cells = []
            for col in range(3):
                cells.append(self.board[row * 3 + col])
            lines.append(" ".join(cells))
        return "\n".join(lines)


class TicTacToeView(discord.ui.View):
    def __init__(
        self,
        game: TicTacToeGame,
        player1: discord.Member,
        player2: discord.Member,
        einsatz: int,
    ):
        super().__init__(timeout=120)
        self.game = game
        self.player1 = player1
        self.player2 = player2
        self.einsatz = einsatz
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for i in range(9):
            row = i // 3
            if self.game.board[i] == TTT_EMPTY and not self.game.game_over:
                btn = discord.ui.Button(
                    label="\u200b",
                    style=discord.ButtonStyle.secondary,
                    row=row,
                )
                btn.callback = self._make_callback(i)
            elif self.game.board[i] == TTT_X:
                btn = discord.ui.Button(
                    label="X",
                    style=discord.ButtonStyle.danger,
                    row=row,
                    disabled=True,
                )
            elif self.game.board[i] == TTT_O:
                btn = discord.ui.Button(
                    label="O",
                    style=discord.ButtonStyle.primary,
                    row=row,
                    disabled=True,
                )
            else:
                btn = discord.ui.Button(
                    label="\u200b",
                    style=discord.ButtonStyle.secondary,
                    row=row,
                    disabled=True,
                )
            self.add_item(btn)

    def _make_callback(self, pos: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.game.current_turn:
                if interaction.user.id in (self.game.player1_id, self.game.player2_id):
                    return await interaction.response.send_message(
                        "❌ Du bist nicht dran!", ephemeral=True
                    )
                return await interaction.response.send_message(
                    "❌ Du spielst nicht mit!", ephemeral=True
                )

            if self.game.game_over:
                return

            if not self.game.make_move(pos):
                return await interaction.response.send_message(
                    "❌ Dieses Feld ist bereits belegt!", ephemeral=True
                )

            # Gewinn prüfen
            winner_piece = self.game.check_winner()
            if winner_piece:
                self.game.game_over = True
                self.game.winner = self.game.current_turn
                active_games.pop(self.game.player1_id, None)
                active_games.pop(self.game.player2_id, None)

                winner = self.player1 if self.game.winner == self.game.player1_id else self.player2

                pot = self.einsatz * 2
                fee = int(pot * 0.05)
                payout = pot - fee
                update_balance(winner.id, payout)

                self._build_buttons()

                embed = discord.Embed(
                    title=f"🏆 Tic Tac Toe – {winner.display_name} gewinnt!",
                    description=(
                        f"{self.game.render_board()}\n\n"
                        f"🎉 {winner.mention} gewinnt **{payout:,} 🪙**!"
                    ),
                    color=COLOR_GOLD,
                )
                embed.set_footer(
                    text=f"Pot: {pot:,} • Gebühr: {fee:,} 🪙"
                )
                self.stop()
                await interaction.response.edit_message(embed=embed, view=self)
                return

            # Unentschieden
            if self.game.is_full():
                self.game.game_over = True
                active_games.pop(self.game.player1_id, None)
                active_games.pop(self.game.player2_id, None)

                update_balance(self.game.player1_id, self.einsatz)
                update_balance(self.game.player2_id, self.einsatz)

                self._build_buttons()

                embed = discord.Embed(
                    title="🤝 Tic Tac Toe – Unentschieden!",
                    description=(
                        f"{self.game.render_board()}\n\n"
                        f"Kein Gewinner! Einsätze zurückgegeben."
                    ),
                    color=COLOR_WARNING,
                )
                self.stop()
                await interaction.response.edit_message(embed=embed, view=self)
                return

            # Nächster Spieler
            self.game.current_turn = self.game.other_player
            current_player = self.player1 if self.game.current_turn == self.game.player1_id else self.player2

            self._build_buttons()

            embed = discord.Embed(
                title="❌⭕ Tic Tac Toe",
                description=(
                    f"{self.game.render_board()}\n\n"
                    f"{self.game.current_piece} **{current_player.display_name}** ist dran!\n\n"
                    f"❌ {self.player1.display_name} vs ⭕ {self.player2.display_name}\n"
                    f"💰 Pot: **{self.einsatz * 2:,} 🪙**"
                ),
                color=COLOR_TICTACTOE,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return callback

    async def on_timeout(self):
        if not self.game.game_over:
            self.game.game_over = True
            winner_id = self.game.other_player
            winner = self.player1 if winner_id == self.game.player1_id else self.player2

            pot = self.einsatz * 2
            fee = int(pot * 0.05)
            payout = pot - fee
            update_balance(winner.id, payout)

            active_games.pop(self.game.player1_id, None)
            active_games.pop(self.game.player2_id, None)


class TicTacToeAcceptView(discord.ui.View):
    def __init__(
        self,
        challenger: discord.Member,
        opponent: discord.Member,
        einsatz: int,
    ):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.einsatz = einsatz
        self.accepted = False

    @discord.ui.button(label="✅ Annehmen!", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message(
                f"❌ Nur {self.opponent.mention} kann annehmen!", ephemeral=True
            )
        if get_balance(self.opponent.id) < self.einsatz:
            return await interaction.response.send_message(
                f"❌ Du hast nicht genug Coins! ({get_balance(self.opponent.id):,} 🪙)",
                ephemeral=True,
            )

        self.accepted = True
        update_balance(self.opponent.id, -self.einsatz)
        active_games[self.opponent.id] = "Tic Tac Toe"

        game = TicTacToeGame(self.challenger.id, self.opponent.id)
        view = TicTacToeView(game, self.challenger, self.opponent, self.einsatz)

        embed = discord.Embed(
            title="❌⭕ Tic Tac Toe",
            description=(
                f"{game.render_board()}\n\n"
                f"❌ **{self.challenger.display_name}** beginnt!\n\n"
                f"❌ {self.challenger.display_name} vs ⭕ {self.opponent.display_name}\n"
                f"💰 Pot: **{self.einsatz * 2:,} 🪙**"
            ),
            color=COLOR_TICTACTOE,
        )
        embed.set_footer(text="Klicke auf ein leeres Feld!")

        self.stop()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("❌ Nicht für dich!", ephemeral=True)
        update_balance(self.challenger.id, self.einsatz)
        active_games.pop(self.challenger.id, None)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Tic Tac Toe abgelehnt",
                description=f"{self.opponent.mention} hat abgelehnt. Einsatz zurückgegeben.",
                color=COLOR_ERROR,
            ),
            view=None,
        )

    async def on_timeout(self):
        if not self.accepted:
            update_balance(self.challenger.id, self.einsatz)
            active_games.pop(self.challenger.id, None)

# ══════════════════════════════════════════════════════════
#         FEATURE: SPOTIFY SUPPORT (via spotdl)
# ══════════════════════════════════════════════════════════

def is_spotify_url(query: str) -> bool:
    """Prüft ob eine URL eine Spotify-URL ist."""
    return any(x in query.lower() for x in [
        "open.spotify.com",
        "spotify.com/track",
        "spotify.com/playlist",
        "spotify.com/album",
        "spotify:track:",
        "spotify:playlist:",
        "spotify:album:",
    ])


def extract_spotify_id(url: str) -> tuple[str, str]:
    """Extrahiert Typ und ID aus Spotify-URL."""
    patterns = [
        r'open\.spotify\.com/(track|playlist|album)/([a-zA-Z0-9]+)',
        r'spotify\.com/(track|playlist|album)/([a-zA-Z0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1), match.group(2)
    uri_match = re.search(r'spotify:(track|playlist|album):([a-zA-Z0-9]+)', url)
    if uri_match:
        return uri_match.group(1), uri_match.group(2)
    return "", ""


async def fetch_spotify_tracks(url: str) -> list[dict]:
    """
    Löst Spotify-URLs über spotdl auf.
    spotdl kann Track-Metadaten ohne Spotify-Premium extrahieren.
    """
    spotify_type, spotify_id = extract_spotify_id(url)
    if not spotify_type or not spotify_id:
        print(f"❌ [Spotify] Konnte URL nicht parsen: {url}")
        return []

    print(f"🟢 [Spotify] Typ: {spotify_type}, ID: {spotify_id}")

    # ── spotdl Methode ───────────────────────────────────
    try:
        from spotdl import Spotdl

        loop = asyncio.get_event_loop()

        def _get_tracks():
            # Öffentliche Client-Credentials (funktionieren ohne Premium)
            client_id = SPOTIFY_CLIENT_ID if SPOTIFY_CLIENT_ID else "5f573c9620494bae87890c0f08a60293"
            client_secret = SPOTIFY_CLIENT_SECRET if SPOTIFY_CLIENT_SECRET else "212476d9b0f3472eaa762d90b19b0ba8"

            spotdl_client = Spotdl(
                client_id=client_id,
                client_secret=client_secret,
                no_cache=True,
            )
            songs = spotdl_client.search([url])
            results = []
            for song in songs:
                artists_str = ", ".join(song.artists) if song.artists else ""
                search_query = f"{artists_str} - {song.name}" if artists_str else song.name
                results.append({
                    "search_query": search_query,
                    "title": search_query,
                    "duration": song.duration if hasattr(song, 'duration') else 0,
                    "thumbnail": song.cover_url if hasattr(song, 'cover_url') else "",
                    "spotify_url": song.url if hasattr(song, 'url') else url,
                })
            return results

        tracks = await loop.run_in_executor(None, _get_tracks)

        if tracks:
            print(f"✅ [Spotify] spotdl: {len(tracks)} Tracks gefunden")
            return tracks

    except ImportError:
        print("❌ [Spotify] spotdl nicht installiert! Führe aus: pip install spotdl")
    except Exception as e:
        print(f"⚠️ [Spotify] spotdl Fehler: {type(e).__name__}: {e}")

    # ── Fallback: Webseite scrapen ───────────────────────
    print("🔄 [Spotify] Fallback: Webseiten-Scraping...")
    try:
        open_url = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                open_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    tracks = []

                    if spotify_type == "track":
                        og_match = re.search(
                            r'<meta property="og:title" content="([^"]+)"', html
                        )
                        og_desc = re.search(
                            r'<meta property="og:description" content="([^"]+)"', html
                        )
                        if og_match:
                            track_name = og_match.group(1).strip()
                            artist_name = ""
                            if og_desc:
                                desc = og_desc.group(1)
                                for pattern in [
                                    r'(?:by|von)\s+(.+?)(?:\s+on\s+|\s+·\s+|$)',
                                    r'^(.+?)\s+·\s+',
                                    r'·\s+(.+?)(?:\s+·|$)',
                                ]:
                                    m = re.search(pattern, desc, re.IGNORECASE)
                                    if m:
                                        artist_name = m.group(1).strip()
                                        break

                            search_query = (
                                f"{artist_name} - {track_name}"
                                if artist_name else track_name
                            )
                            tracks.append({
                                "search_query": search_query,
                                "title": search_query,
                                "duration": 0,
                                "thumbnail": "",
                                "spotify_url": url,
                            })

                    elif spotify_type in ("playlist", "album"):
                        track_matches = re.findall(
                            r'"name"\s*:\s*"([^"]{2,80})".*?"artists".*?"name"\s*:\s*"([^"]{2,60})"',
                            html,
                        )
                        seen = set()
                        for tname, tartist in track_matches[:50]:
                            sq = f"{tartist} - {tname}"
                            if sq not in seen and len(tname) > 1:
                                seen.add(sq)
                                tracks.append({
                                    "search_query": sq,
                                    "title": sq,
                                    "duration": 0,
                                    "thumbnail": "",
                                    "spotify_url": url,
                                })

                    if tracks:
                        print(f"✅ [Spotify] Scraping: {len(tracks)} Tracks")
                        return tracks

    except Exception as e:
        print(f"⚠️ [Spotify] Scraping fehlgeschlagen: {e}")

    print(f"❌ [Spotify] Alle Methoden fehlgeschlagen für: {url}")
    return []


async def play_spotify_tracks(
    state,
    spotify_tracks: list[dict],
    requester: discord.Member,
    text_channel: discord.TextChannel,
) -> int:
    """Löst Spotify-Tracks über YouTube auf und fügt sie zur Queue hinzu."""
    added = 0
    for sp_track in spotify_tracks:
        results = await search_tracks(sp_track["search_query"], limit=1)
        if results:
            entry = results[0]
            webpage_url = _get_reference_url(entry, "")
            duration = sp_track.get("duration", 0) or (
                int(entry.get("duration", 0)) if entry.get("duration") else 0
            )
            thumbnail = sp_track.get("thumbnail", "") or entry.get("thumbnail", "")
            title = sp_track.get("title", entry.get("title", "Unbekannt"))

            song = SongInfo(title, "", webpage_url, duration, thumbnail, requester)
            state.queue.append(song)
            added += 1
    return added

# ══════════════════════════════════════════════════════════
#         FEATURE: SCHMIEDEN (Reaktionsspiel)
# ══════════════════════════════════════════════════════════

class SmithingView(discord.ui.View):
    def __init__(self, user_id: int, item: dict, hammer: dict):
        super().__init__(timeout=15)
        self.user_id = user_id
        self.item = item
        self.hammer = hammer
        self.hits = 0
        self.required_hits = {"easy": 3, "medium": 5, "hard": 7, "master": 10}.get(
            item["difficulty"], 5
        )
        self.target_emoji = random.choice(["🔨", "⚒️", "🛠️"])
        self.done = False
        self.start_time = datetime.datetime.now(datetime.timezone.utc)
        self._build()

    def _build(self):
        self.clear_items()
        if self.done:
            return
        emojis = ["🔨", "⚒️", "🛠️", "🪓", "⛏️"]
        positions = list(range(5))
        random.shuffle(positions)
        self.target_emoji = random.choice(["🔨", "⚒️", "🛠️"])
        for i in range(5):
            emoji = self.target_emoji if i == positions[0] else random.choice(
                [e for e in emojis if e != self.target_emoji]
            )
            btn = discord.ui.Button(
                label=emoji, style=discord.ButtonStyle.secondary, row=0
            )
            btn.callback = self._make_callback(emoji == self.target_emoji)
            self.add_item(btn)

    def _make_callback(self, is_correct: bool):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("❌!", ephemeral=True)
            if self.done:
                return

            if is_correct:
                self.hits += 1
                if self.hits >= self.required_hits:
                    self.done = True
                    elapsed = (
                        datetime.datetime.now(datetime.timezone.utc) - self.start_time
                    ).total_seconds()
                    quality = self._calc_quality(elapsed)
                    value = int(
                        self.item["base_value"]
                        * quality["multiplier"]
                        * self.hammer["bonus"]
                    )
                    update_balance(self.user_id, value)

                    job_data = get_job_data(self.user_id, "smithing")
                    job_data["total_earned"] = job_data.get("total_earned", 0) + value
                    job_data["total_actions"] = job_data.get("total_actions", 0) + 1
                    if value > job_data.get("best_value", 0):
                        job_data["best_value"] = value
                        job_data["best_name"] = f"{quality['name']} {self.item['name']}"
                    save_job_data(self.user_id, "smithing", job_data)

                    embed = discord.Embed(
                        title=f"{quality['emoji']} {quality['name']}!",
                        description=(
                            f"{self.item['emoji']} Du hast **{self.item['name']}** geschmiedet!\n\n"
                            f"📊 Qualität: {quality['emoji']} **{quality['name']}** (×{quality['multiplier']})\n"
                            f"⏱️ Zeit: **{elapsed:.1f}s**\n"
                            f"💰 Wert: **{value:,} 🪙**"
                        ),
                        color=COLOR_GOLD if quality["multiplier"] >= 2.5 else COLOR_SUCCESS,
                    )
                    embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                    self.stop()
                    await interaction.response.edit_message(embed=embed, view=None)
                    return

                self._build()
                embed = discord.Embed(
                    title=f"⚒️ Schmieden – {self.item['emoji']} {self.item['name']}",
                    description=(
                        f"✅ Treffer! Klicke auf **{self.target_emoji}**!\n\n"
                        f"🔨 Schläge: **{self.hits}/{self.required_hits}**\n"
                        f"⏱️ Schneller = bessere Qualität!"
                    ),
                    color=COLOR_INFO,
                )
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                self.hits = max(0, self.hits - 1)
                self._build()
                embed = discord.Embed(
                    title=f"⚒️ Schmieden – {self.item['emoji']} {self.item['name']}",
                    description=(
                        f"❌ Daneben! Klicke auf **{self.target_emoji}**!\n\n"
                        f"🔨 Schläge: **{self.hits}/{self.required_hits}**"
                    ),
                    color=COLOR_WARNING,
                )
                await interaction.response.edit_message(embed=embed, view=self)
        return callback

    def _calc_quality(self, elapsed: float) -> dict:
        quality_bonus = self.hammer.get("quality_bonus", 0)
        speed_score = max(0, 100 - (elapsed * 8)) + quality_bonus
        if speed_score >= 90:
            return SMITHING_QUALITY["legendär"]
        elif speed_score >= 75:
            return SMITHING_QUALITY["meisterwerk"]
        elif speed_score >= 55:
            return SMITHING_QUALITY["excellent"]
        elif speed_score >= 35:
            return SMITHING_QUALITY["gut"]
        elif speed_score >= 15:
            return SMITHING_QUALITY["normal"]
        else:
            return SMITHING_QUALITY["schlecht"]

    async def on_timeout(self):
        if not self.done:
            value = int(self.item["base_value"] * 0.2)
            update_balance(self.user_id, value)
            job_data = get_job_data(self.user_id, "smithing")
            job_data["total_earned"] = job_data.get("total_earned", 0) + value
            job_data["total_actions"] = job_data.get("total_actions", 0) + 1
            save_job_data(self.user_id, "smithing", job_data)


# ══════════════════════════════════════════════════════════
#         FEATURE: KOCHEN (Rezept-Merkspiel)
# ══════════════════════════════════════════════════════════

class CookingView(discord.ui.View):
    def __init__(self, user_id: int, recipe: dict, tool: dict):
        super().__init__(timeout=20)
        self.user_id = user_id
        self.recipe = recipe
        self.tool = tool
        self.selected: list[str] = []
        self.correct_ingredients = list(recipe["ingredients"])
        self.done = False
        self.start_time = datetime.datetime.now(datetime.timezone.utc)

        # Zutaten-Auswahl: richtige + falsche mischen
        wrong_count = min(8, len(ALL_INGREDIENTS)) - len(self.correct_ingredients)
        wrong_pool = [i for i in ALL_INGREDIENTS if i not in self.correct_ingredients]
        wrong = random.sample(wrong_pool, min(wrong_count, len(wrong_pool)))
        all_options = list(self.correct_ingredients) + wrong
        random.shuffle(all_options)
        self.options = all_options[:15]  # Max 15 Buttons (3 Reihen á 5)
        self._build()

    def _build(self):
        self.clear_items()
        if self.done:
            return
        for i, ingredient in enumerate(self.options):
            row = i // 5
            already_selected = ingredient in self.selected
            btn = discord.ui.Button(
                label=ingredient,
                style=discord.ButtonStyle.success if already_selected else discord.ButtonStyle.secondary,
                row=row,
                disabled=already_selected,
            )
            btn.callback = self._make_callback(ingredient)
            self.add_item(btn)

        # Fertig-Button
        if self.selected:
            done_btn = discord.ui.Button(
                label=f"✅ Fertig! ({len(self.selected)}/{len(self.correct_ingredients)})",
                style=discord.ButtonStyle.primary,
                row=3,
            )
            done_btn.callback = self._finish
            self.add_item(done_btn)

    def _make_callback(self, ingredient: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("❌!", ephemeral=True)
            if self.done:
                return
            self.selected.append(ingredient)
            self._build()
            embed = discord.Embed(
                title=f"🍳 Kochen – {self.recipe['emoji']} {self.recipe['name']}",
                description=(
                    f"Wähle die richtigen Zutaten!\n\n"
                    f"📝 Gewählt: {' '.join(self.selected)}\n"
                    f"📊 {len(self.selected)}/{len(self.correct_ingredients)} Zutaten"
                ),
                color=COLOR_INFO,
            )
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    async def _finish(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return
        if self.done:
            return
        self.done = True
        self.stop()

        elapsed = (
            datetime.datetime.now(datetime.timezone.utc) - self.start_time
        ).total_seconds()

        # Prüfen wie viele richtig sind
        correct = sum(1 for s in self.selected if s in self.correct_ingredients)
        wrong = sum(1 for s in self.selected if s not in self.correct_ingredients)
        missing = len(self.correct_ingredients) - correct
        accuracy = correct / len(self.correct_ingredients) if self.correct_ingredients else 0

        # Bewertung
        time_bonus = max(0, 1.0 - (elapsed / 20)) * 0.5
        tool_bonus = self.tool.get("time_bonus", 0) / 100
        score = accuracy - (wrong * 0.15) + time_bonus + tool_bonus
        score = max(0, min(1.0, score))

        if score >= 0.95:
            quality = "⭐ Perfekt"
            mult = 2.0
        elif score >= 0.8:
            quality = "🟢 Lecker"
            mult = 1.3
        elif score >= 0.6:
            quality = "🟡 Okay"
            mult = 0.8
        elif score >= 0.3:
            quality = "🟠 Meh"
            mult = 0.4
        else:
            quality = "💩 Verbrannt"
            mult = 0.1

        value = int(self.recipe["base_value"] * mult * self.tool["bonus"])
        update_balance(self.user_id, value)

        job_data = get_job_data(self.user_id, "cooking")
        job_data["total_earned"] = job_data.get("total_earned", 0) + value
        job_data["total_actions"] = job_data.get("total_actions", 0) + 1
        if value > job_data.get("best_value", 0):
            job_data["best_value"] = value
            job_data["best_name"] = f"{quality} {self.recipe['name']}"
        save_job_data(self.user_id, "cooking", job_data)

        correct_str = " ".join(self.correct_ingredients)
        selected_str = " ".join(self.selected) if self.selected else "Nichts"

        embed = discord.Embed(
            title=f"{self.recipe['emoji']} {quality}!",
            description=(
                f"Du hast **{self.recipe['name']}** gekocht!\n\n"
                f"✅ Richtig: **{correct}** / {len(self.correct_ingredients)}\n"
                f"❌ Falsch: **{wrong}**\n"
                f"⏱️ Zeit: **{elapsed:.1f}s**\n\n"
                f"📝 Rezept: {correct_str}\n"
                f"🍳 Deine Auswahl: {selected_str}\n\n"
                f"💰 Verdient: **{value:,} 🪙**"
            ),
            color=COLOR_GOLD if mult >= 1.3 else COLOR_SUCCESS if mult >= 0.8 else COLOR_ERROR,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        if not self.done:
            self.done = True
            value = int(self.recipe["base_value"] * 0.1)
            update_balance(self.user_id, value)
            job_data = get_job_data(self.user_id, "cooking")
            job_data["total_earned"] = job_data.get("total_earned", 0) + value
            job_data["total_actions"] = job_data.get("total_actions", 0) + 1
            save_job_data(self.user_id, "cooking", job_data)

async def send_log(embed: discord.Embed, bot):
    try:
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(embed=embed)
        else:
            print(f"⚠️ Log-Kanal mit ID {LOG_CHANNEL_ID} nicht gefunden!")
    except Exception as e:
        print(f"Fehler beim Senden des Logs: {e}")


async def send_twitch_mod_log(embed: discord.Embed):
    """Sendet ein Embed in den Twitch-Mod-Log-Kanal."""
    try:
        if discord_bot_ref is None:
            print("⚠️ Discord Bot Referenz nicht gesetzt!")
            return
        log_channel = discord_bot_ref.get_channel(TWITCH_LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(embed=embed)
        else:
            print(
                f"⚠️ Twitch-Log-Kanal mit ID "
                f"{TWITCH_LOG_CHANNEL_ID} nicht gefunden!"
            )
    except Exception as e:
        print(f"Fehler beim Senden des Twitch-Mod-Logs: {e}")




async def translate_message(
    original_text: str, target_language: str
) -> str | None:
    if not ai_enabled or not key_rotator:
        return None
    try:
        prompt = (
            f"Übersetze den folgenden Text ins {target_language}.\n\n"
            f"WICHTIG:\n"
            f"- Übersetze NUR den Inhalt, keine Kommentare oder Erklärungen\n"
            f"- Behalte Ton, Stil und Emojis bei\n"
            f"- Namen/Eigennamen NICHT übersetzen\n\n"
            f"Text:\n{original_text}"
        )
        response = await key_rotator.call_with_rotation(
            lambda c: c.chat.completions.create(
                model=AI_MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                extra_headers={"X-Title": "Discord Translation Bot"},
            )
        )
        if (
            not response
            or not response.choices
            or not response.choices[0].message.content
        ):
            return None
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Übersetzungsfehler: {e}")
        return None


async def process_ai_reply(message: discord.Message, user_input: str):
    if not ai_enabled or not key_rotator:
        return
    if not message or not message.channel:
        return
    try:
        async with message.channel.typing():
            response = await key_rotator.call_with_rotation(
                lambda c: c.chat.completions.create(
                    model=AI_MODEL_NAME,
                    messages=[{"role": "user", "content": user_input}],
                    extra_headers={"X-Title": "Discord AI Bot"},
                )
            )
        ai_message = response.choices[0].message.content
        max_length = 2000 - len(AI_FOOTER)
        if ai_message and len(ai_message) > max_length:
            ai_message = ai_message[:max_length - 3] + "..."
        await message.reply(f"{ai_message}{AI_FOOTER}")
    except Exception as e:
        # KEINE Fehlermeldung mehr öffentlich in den Chat –
        # nur Logging + optional eine stille DM an den User.
        print(f"KI-Antwort Fehler: {e}")
        try:
            await message.author.send(
                "⚠️ Ich konnte gerade keine KI-Antwort erstellen "
                "(API-Fehler). Bitte probiere es später noch einmal."
            )
        except Exception:
            pass  # DMs blockiert → still bleiben


# ══════════════════════════════════════════════════════════
#          FEATURE: TWITCH MOD LOGGER HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════

def format_duration(seconds: int) -> str:
    """Formatiert Sekunden in lesbare Zeitangabe."""
    if seconds < 60:
        return f"{seconds} Sekunde{'n' if seconds != 1 else ''}"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} Minute{'n' if minutes != 1 else ''}"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes > 0:
            return f"{hours}h {minutes}min"
        return f"{hours} Stunde{'n' if hours != 1 else ''}"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        if hours > 0:
            return f"{days}T {hours}h"
        return f"{days} Tag{'e' if days != 1 else ''}"


def build_twitch_mod_embed(
    action: str,
    moderator: str,
    target_user: str,
    reason: str = None,
    duration: int = None,
    deleted_message: str = None,
    channel: str = None,
) -> discord.Embed:
    """Erstellt ein schönes Twitch-Mod-Log-Embed."""
    now = datetime.datetime.now(datetime.timezone.utc)

    action_configs = {
        "ban": {
            "title": "🔨 User gebannt",
            "color": COLOR_MOD_BAN,
            "emoji": "🔨",
            "description": f"**{target_user}** wurde permanent gebannt.",
        },
        "timeout": {
            "title": "⏱️ User getimeoutet",
            "color": COLOR_MOD_TIMEOUT,
            "emoji": "⏱️",
            "description": f"**{target_user}** wurde temporär getimeoutet.",
        },
        "warn": {
            "title": "⚠️ User verwarnt",
            "color": COLOR_MOD_WARN,
            "emoji": "⚠️",
            "description": f"**{target_user}** wurde verwarnt.",
        },
        "delete": {
            "title": "🗑️ Nachricht gelöscht",
            "color": COLOR_MOD_DELETE,
            "emoji": "🗑️",
            "description": (
                f"Eine Nachricht von **{target_user}** wurde gelöscht."
            ),
        },
        "unban": {
            "title": "✅ User entbannt",
            "color": COLOR_MOD_UNBAN,
            "emoji": "✅",
            "description": f"**{target_user}** wurde entbannt.",
        },
        "untimeout": {
            "title": "✅ Timeout aufgehoben",
            "color": COLOR_MOD_UNTIMEOUT,
            "emoji": "✅",
            "description": (
                f"Der Timeout von **{target_user}** wurde aufgehoben."
            ),
        },
    }

    config = action_configs.get(action, {
        "title": "📋 Moderations-Aktion",
        "color": COLOR_INFO,
        "emoji": "📋",
        "description": f"Aktion an **{target_user}**.",
    })

    embed = discord.Embed(
        title=config["title"],
        description=config["description"],
        color=config["color"],
        timestamp=now,
    )

    if channel:
        embed.add_field(
            name="📺 Kanal",
            value=f"[{channel}](https://twitch.tv/{channel})",
            inline=True,
        )

    embed.add_field(
        name="🛡️ Moderator",
        value=f"`{moderator}`",
        inline=True,
    )

    embed.add_field(
        name="👤 User",
        value=f"[{target_user}](https://twitch.tv/{target_user})",
        inline=True,
    )

    if duration is not None and action == "timeout":
        embed.add_field(
            name="⏳ Dauer",
            value=f"**{format_duration(duration)}**",
            inline=True,
        )

    if reason and reason.strip():
        embed.add_field(
            name="📝 Begründung",
            value=f"```{reason}```",
            inline=False,
        )
    else:
        embed.add_field(
            name="📝 Begründung",
            value="*Keine Begründung angegeben*",
            inline=False,
        )

    if deleted_message:
        embed.add_field(
            name="💬 Gelöschte Nachricht",
            value=f"```{deleted_message[:500]}```",
            inline=False,
        )

    embed.set_footer(
        text=(
            f"Twitch Mod Logger • "
            f"{now.strftime('%d.%m.%Y um %H:%M:%S')} UTC"
        )
    )

    return embed


# ══════════════════════════════════════════════════════════
#       TEMP VOICE CHAT – EMBED & PANEL UPDATE
# ══════════════════════════════════════════════════════════

def build_vc_control_embed(
    guild: discord.Guild, voice_channel_id: int
) -> discord.Embed:
    """Erstellt das aktuelle Status-Embed für den VC-Kontrollpanel."""
    vc_data = temp_voice_channels.get(voice_channel_id, {})
    voice_channel = guild.get_channel(voice_channel_id)

    owner_id = vc_data.get("owner_id", 0)
    owner = guild.get_member(owner_id)
    owner_str = owner.mention if owner else f"<@{owner_id}>"

    locked = vc_data.get("locked", False)
    allowed_users = vc_data.get("allowed_users", set())

    current_limit = (
        voice_channel.user_limit if voice_channel else vc_data.get("limit", 0)
    )
    limit_str = str(current_limit) if current_limit > 0 else "∞"

    member_count = len(voice_channel.members) if voice_channel else 0
    bitrate_str = (
        f"`{voice_channel.bitrate // 1000} kbps`"
        if voice_channel else "Unbekannt"
    )
    channel_name = voice_channel.name if voice_channel else "Unbekannt"

    # Liste der aktuellen Mitglieder
    if voice_channel and voice_channel.members:
        member_list = "\n".join(
            f"{'👑 ' if m.id == owner_id else '👤 '}{m.display_name}"
            for m in voice_channel.members
        )
    else:
        member_list = "*Niemand im Kanal*"

    embed = discord.Embed(
        title="🎙️ Kanal-Einstellungen",
        description=(
            "Hier kannst du deinen Voice-Kanal verwalten.\n"
            "Nur der **Kanal-Admin** kann die Buttons benutzen."
        ),
        color=COLOR_VOICE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    embed.add_field(
        name="📛 Kanalname",
        value=f"`{channel_name}`",
        inline=False,
    )
    embed.add_field(
        name="👑 Admin",
        value=owner_str,
        inline=True,
    )
    embed.add_field(
        name="🔒 Status",
        value="Gesperrt 🔒" if locked else "Offen 🔓",
        inline=True,
    )
    embed.add_field(
        name="👥 Nutzer",
        value=f"`{member_count}` / `{limit_str}`",
        inline=True,
    )
    embed.add_field(
        name="🎙️ Bitrate",
        value=bitrate_str,
        inline=True,
    )
    embed.add_field(
        name="📋 Whitelist",
        value=(
            f"`{len(allowed_users)}` User"
            if allowed_users else "Nicht aktiv"
        ),
        inline=True,
    )
    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True,
    )
    embed.add_field(
        name="🧑‍🤝‍🧑 Im Kanal",
        value=member_list,
        inline=False,
    )

    embed.set_footer(
        text="Temp Voice System • Wird gelöscht wenn alle raus sind"
    )

    return embed


async def update_temp_voice_panel(
    guild: discord.Guild,
    voice_channel_id: int,
):
    """Aktualisiert das Control-Embed im Text-Kanal."""
    vc_data = temp_voice_channels.get(voice_channel_id)
    if not vc_data:
        return

    text_channel = guild.get_channel(vc_data.get("text_channel_id", 0))
    if not text_channel:
        return

    control_message_id = vc_data.get("control_message_id", 0)
    if not control_message_id:
        return

    try:
        control_message = await text_channel.fetch_message(control_message_id)
        new_view = VoiceChannelControlView(
            vc_data["owner_id"],
            voice_channel_id,
        )
        await control_message.edit(
            embed=build_vc_control_embed(guild, voice_channel_id),
            view=new_view,
        )
    except discord.NotFound:
        print(f"⚠️ [TempVC] Control-Message {control_message_id} nicht gefunden")
    except Exception as e:
        print(f"❌ [TempVC] Fehler beim Aktualisieren des Panels: {e}")


# ══════════════════════════════════════════════════════════
#       TEMP VOICE CHAT – DISCORD UI VIEWS & MODALS
# ══════════════════════════════════════════════════════════

class VoiceChannelControlView(discord.ui.View):
    """
    Persistente Control-View für den Temp-Voice-Kanal.
    Wird im Text-Kanal des Voice-Kanals gepostet.
    """

    def __init__(self, owner_id: int, voice_channel_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.voice_channel_id = voice_channel_id

        # Lock-Button dynamisch anpassen
        vc_data = temp_voice_channels.get(voice_channel_id)
        if vc_data and vc_data.get("locked"):
            self.btn_lock.label = "🔓 Entsperren"
            self.btn_lock.style = discord.ButtonStyle.success
        else:
            self.btn_lock.label = "🔒 Sperren"
            self.btn_lock.style = discord.ButtonStyle.danger

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        """Prüft ob der Interagierende der Kanal-Owner ist."""
        return interaction.user.id == self.owner_id

    async def _owner_check(
        self, interaction: discord.Interaction
    ) -> bool:
        """Gibt Fehlermeldung aus wenn kein Owner, gibt False zurück."""
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "❌ Nur der **Kanal-Admin** kann diese Einstellung ändern!",
                ephemeral=True,
            )
            return False
        return True

    # ── Kanal umbenennen ──────────────────────────────────

    @discord.ui.button(
        label="✏️ Umbenennen",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="vc_rename",
    )
    async def btn_rename(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_modal(
            RenameChannelModal(self.voice_channel_id)
        )

    # ── Limit setzen ─────────────────────────────────────

    @discord.ui.button(
        label="👥 Limit",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="vc_limit",
    )
    async def btn_limit(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_modal(
            SetLimitModal(self.voice_channel_id)
        )

    # ── Bitrate ändern ───────────────────────────────────

    @discord.ui.button(
        label="🎙️ Bitrate",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="vc_bitrate",
    )
    async def btn_bitrate(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_modal(
            SetBitrateModal(self.voice_channel_id)
        )

    # ── Kanal sperren/entsperren ─────────────────────────

    @discord.ui.button(
        label="🔒 Sperren",
        style=discord.ButtonStyle.danger,
        row=1,
        custom_id="vc_lock",
    )
    async def btn_lock(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._owner_check(interaction):
            return

        vc_data = temp_voice_channels.get(self.voice_channel_id)
        if not vc_data:
            await interaction.response.send_message(
                "❌ Kanal nicht gefunden.", ephemeral=True
            )
            return

        voice_channel = interaction.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.send_message(
                "❌ Voice-Kanal nicht gefunden.", ephemeral=True
            )
            return

        try:
            if vc_data["locked"]:
                await voice_channel.set_permissions(
                    interaction.guild.default_role,
                    connect=None,
                )
                vc_data["locked"] = False
                await interaction.response.send_message(
                    "🔓 Kanal wurde **entsperrt**. Jeder kann jetzt joinen.",
                    ephemeral=True,
                )
            else:
                await voice_channel.set_permissions(
                    interaction.guild.default_role,
                    connect=False,
                )
                vc_data["locked"] = True
                await interaction.response.send_message(
                    "🔒 Kanal wurde **gesperrt**. Niemand kann mehr joinen.",
                    ephemeral=True,
                )

            await update_temp_voice_panel(
                interaction.guild, self.voice_channel_id
            )
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ Fehler: {e}", ephemeral=True
                )

    # ── User einladen ────────────────────────────────────

    @discord.ui.button(
        label="➕ Einladen",
        style=discord.ButtonStyle.success,
        row=1,
        custom_id="vc_invite",
    )
    async def btn_invite(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_modal(
            InviteUserModal(self.voice_channel_id, interaction.guild)
        )

    # ── User kicken ──────────────────────────────────────

    @discord.ui.button(
        label="👢 Kicken",
        style=discord.ButtonStyle.danger,
        row=1,
        custom_id="vc_kick",
    )
    async def btn_kick(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._owner_check(interaction):
            return

        voice_channel = interaction.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.send_message(
                "❌ Voice-Kanal nicht gefunden.", ephemeral=True
            )
            return

        vc_data = temp_voice_channels.get(self.voice_channel_id)
        if not vc_data:
            await interaction.response.send_message(
                "❌ Kanal nicht gefunden.", ephemeral=True
            )
            return

        kickable = [
            m for m in voice_channel.members
            if m.id != vc_data["owner_id"] and m.id != d_bot.user.id
        ]

        if not kickable:
            await interaction.response.send_message(
                "❌ Es ist niemand im Kanal den du kicken könntest.",
                ephemeral=True,
            )
            return

        view = KickUserSelectView(
            self.voice_channel_id,
            vc_data["owner_id"],
            kickable,
        )
        await interaction.response.send_message(
            "👢 Wähle den User den du kicken möchtest:",
            view=view,
            ephemeral=True,
        )

    # ── Ownership übertragen ─────────────────────────────

    @discord.ui.button(
        label="👑 Admin übertragen",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="vc_transfer",
    )
    async def btn_transfer(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._owner_check(interaction):
            return
        await interaction.response.send_modal(
            TransferOwnerModal(
                self.voice_channel_id,
                interaction.guild,
                self,
            )
        )


# ── Kick Select Menu ─────────────────────────────────────

class KickUserSelectView(discord.ui.View):
    def __init__(
        self,
        voice_channel_id: int,
        owner_id: int,
        kickable_members: list[discord.Member],
    ):
        super().__init__(timeout=30)
        self.voice_channel_id = voice_channel_id
        self.owner_id = owner_id

        options = [
            discord.SelectOption(
                label=m.display_name,
                description=f"ID: {m.id}",
                value=str(m.id),
            )
            for m in kickable_members[:25]
        ]

        self.select = discord.ui.Select(
            placeholder="User zum Kicken auswählen...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        user_id = int(self.select.values[0])
        member = interaction.guild.get_member(user_id)

        if not member:
            await interaction.response.edit_message(
                content="❌ User nicht mehr auf dem Server.",
                view=None,
            )
            return

        voice_channel = interaction.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.edit_message(
                content="❌ Voice-Kanal nicht mehr vorhanden.",
                view=None,
            )
            return

        if member not in voice_channel.members:
            await interaction.response.edit_message(
                content=f"❌ **{member.display_name}** ist nicht mehr im Kanal.",
                view=None,
            )
            return

        try:
            await member.move_to(None, reason="Temp VC: Vom Admin gekickt")
            await voice_channel.set_permissions(
                member, connect=False
            )
            await interaction.response.edit_message(
                content=f"👢 **{member.display_name}** wurde aus dem Kanal gekickt!",
                view=None,
            )
            await update_temp_voice_panel(
                interaction.guild, self.voice_channel_id
            )
        except discord.Forbidden:
            await interaction.response.edit_message(
                content=(
                    f"❌ Ich habe keine Berechtigung um "
                    f"**{member.display_name}** zu kicken.\n"
                    f"-# Der Bot braucht die Berechtigung 'Mitglieder verschieben'."
                ),
                view=None,
            )
        except Exception as e:
            await interaction.response.edit_message(
                content=f"❌ Fehler: {e}",
                view=None,
            )

    async def on_timeout(self):
        pass


# ── Modals ────────────────────────────────────────────────

class RenameChannelModal(discord.ui.Modal, title="Kanal umbenennen"):
    new_name = discord.ui.TextInput(
        label="Neuer Kanalname",
        placeholder="z.B. Mein cooler Kanal",
        min_length=1,
        max_length=100,
    )

    def __init__(self, voice_channel_id: int):
        super().__init__()
        self.voice_channel_id = voice_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        voice_channel = interaction.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.send_message(
                "❌ Kanal nicht gefunden.", ephemeral=True
            )
            return
        try:
            await voice_channel.edit(name=self.new_name.value)

            vc_data = temp_voice_channels.get(self.voice_channel_id)
            if vc_data:
                text_ch = interaction.guild.get_channel(
                    vc_data.get("text_channel_id", 0)
                )
                if text_ch:
                    safe_name = re.sub(
                        r'[^a-z0-9\-]', '-',
                        self.new_name.value.lower()
                        .replace("ä", "ae")
                        .replace("ö", "oe")
                        .replace("ü", "ue")
                        .replace("ß", "ss")
                        .replace(" ", "-")
                    )
                    safe_name = re.sub(r'-+', '-', safe_name).strip('-')
                    await text_ch.edit(name=f"💬{safe_name}")

            await interaction.response.send_message(
                f"✅ Kanal wurde in **{self.new_name.value}** umbenannt!",
                ephemeral=True,
            )

            await update_temp_voice_panel(
                interaction.guild, self.voice_channel_id
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Keine Berechtigung zum Umbenennen.", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Fehler: {e}", ephemeral=True
            )


class SetLimitModal(discord.ui.Modal, title="Nutzer-Limit setzen"):
    limit = discord.ui.TextInput(
        label="Maximale Nutzerzahl (0 = kein Limit)",
        placeholder="z.B. 5",
        min_length=1,
        max_length=2,
    )

    def __init__(self, voice_channel_id: int):
        super().__init__()
        self.voice_channel_id = voice_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit_val = int(self.limit.value)
            if limit_val < 0 or limit_val > 99:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "❌ Ungültige Zahl. Bitte eine Zahl zwischen 0 und 99.",
                ephemeral=True,
            )
            return

        voice_channel = interaction.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.send_message(
                "❌ Kanal nicht gefunden.", ephemeral=True
            )
            return

        try:
            await voice_channel.edit(user_limit=limit_val)

            vc_data = temp_voice_channels.get(self.voice_channel_id)
            if vc_data:
                vc_data["limit"] = limit_val

            limit_str = str(limit_val) if limit_val > 0 else "kein Limit"
            await interaction.response.send_message(
                f"✅ Nutzer-Limit auf **{limit_str}** gesetzt!",
                ephemeral=True,
            )

            await update_temp_voice_panel(
                interaction.guild, self.voice_channel_id
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Fehler: {e}", ephemeral=True
            )


class SetBitrateModal(discord.ui.Modal, title="Bitrate ändern"):
    bitrate = discord.ui.TextInput(
        label="Bitrate in kbps (8 - 96)",
        placeholder="z.B. 64",
        min_length=1,
        max_length=3,
    )

    def __init__(self, voice_channel_id: int):
        super().__init__()
        self.voice_channel_id = voice_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            br = int(self.bitrate.value)
            if br < 8 or br > 96:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "❌ Ungültige Bitrate. Bitte eine Zahl zwischen 8 und 96.",
                ephemeral=True,
            )
            return

        voice_channel = interaction.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.send_message(
                "❌ Kanal nicht gefunden.", ephemeral=True
            )
            return

        try:
            await voice_channel.edit(bitrate=br * 1000)
            await interaction.response.send_message(
                f"✅ Bitrate auf **{br} kbps** gesetzt!", ephemeral=True
            )

            await update_temp_voice_panel(
                interaction.guild, self.voice_channel_id
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Keine Berechtigung.", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Fehler: {e}", ephemeral=True
            )


class InviteUserModal(discord.ui.Modal, title="User einladen"):
    username = discord.ui.TextInput(
        label="Username oder User-ID",
        placeholder="z.B. MaxMustermann oder 123456789",
        min_length=1,
        max_length=100,
    )

    def __init__(self, voice_channel_id: int, guild: discord.Guild):
        super().__init__()
        self.voice_channel_id = voice_channel_id
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        search = self.username.value.strip()
        member = None

        if search.isdigit():
            member = self.guild.get_member(int(search))

        if not member:
            search_lower = search.lower()
            for m in self.guild.members:
                if (
                    m.name.lower() == search_lower
                    or m.display_name.lower() == search_lower
                    or str(m).lower() == search_lower
                ):
                    member = m
                    break

        if not member:
            await interaction.response.send_message(
                f"❌ User `{search}` nicht gefunden.", ephemeral=True
            )
            return

        voice_channel = self.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.send_message(
                "❌ Kanal nicht gefunden.", ephemeral=True
            )
            return

        try:
            await voice_channel.set_permissions(
                member,
                connect=True,
                view_channel=True,
            )
            vc_data = temp_voice_channels.get(self.voice_channel_id)
            if vc_data:
                vc_data["allowed_users"].add(member.id)

            await interaction.response.send_message(
                f"✅ **{member.display_name}** wurde eingeladen und kann "
                f"jetzt dem Kanal beitreten!",
                ephemeral=True,
            )

            await update_temp_voice_panel(
                interaction.guild, self.voice_channel_id
            )

            try:
                owner = self.guild.get_member(
                    temp_voice_channels[self.voice_channel_id]["owner_id"]
                )
                owner_name = owner.display_name if owner else "Jemand"
                await member.send(
                    f"🎙️ **{owner_name}** hat dich in den Voice-Kanal "
                    f"**{voice_channel.name}** eingeladen!\n"
                    f"Du kannst dem Kanal jetzt beitreten."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Keine Berechtigung.", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Fehler: {e}", ephemeral=True
            )


class TransferOwnerModal(discord.ui.Modal, title="Admin übertragen"):
    username = discord.ui.TextInput(
        label="Neuer Admin (Username oder User-ID)",
        placeholder="z.B. MaxMustermann oder 123456789",
        min_length=1,
        max_length=100,
    )

    def __init__(
        self,
        voice_channel_id: int,
        guild: discord.Guild,
        view: VoiceChannelControlView,
    ):
        super().__init__()
        self.voice_channel_id = voice_channel_id
        self.guild = guild
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        search = self.username.value.strip()
        voice_channel = self.guild.get_channel(self.voice_channel_id)
        if not voice_channel:
            await interaction.response.send_message(
                "❌ Kanal nicht gefunden.", ephemeral=True
            )
            return

        member = None
        if search.isdigit():
            member = self.guild.get_member(int(search))
        if not member:
            search_lower = search.lower()
            for m in self.guild.members:
                if (
                    m.name.lower() == search_lower
                    or m.display_name.lower() == search_lower
                ):
                    member = m
                    break

        if not member:
            await interaction.response.send_message(
                f"❌ User `{search}` nicht gefunden.", ephemeral=True
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ Du bist bereits der Admin.", ephemeral=True
            )
            return

        vc_data = temp_voice_channels.get(self.voice_channel_id)
        if not vc_data:
            await interaction.response.send_message(
                "❌ Kanal-Daten nicht gefunden.", ephemeral=True
            )
            return

        old_owner = self.guild.get_member(vc_data["owner_id"])

        try:
            await voice_channel.set_permissions(
                member,
                manage_channels=True,
                connect=True,
                speak=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
            )

            if old_owner:
                await voice_channel.set_permissions(
                    old_owner,
                    manage_channels=False,
                    move_members=False,
                    mute_members=False,
                    deafen_members=False,
                    connect=True,
                    speak=True,
                )

            text_ch = self.guild.get_channel(
                vc_data.get("text_channel_id", 0)
            )
            if text_ch:
                await text_ch.set_permissions(
                    member,
                    read_messages=True,
                    send_messages=True,
                    manage_messages=True,
                )
                if old_owner:
                    await text_ch.set_permissions(
                        old_owner,
                        read_messages=True,
                        send_messages=True,
                        manage_messages=False,
                    )

            vc_data["owner_id"] = member.id
            self.view.owner_id = member.id

            await interaction.response.send_message(
                f"✅ Admin wurde an **{member.display_name}** übertragen!",
                ephemeral=True,
            )

            await update_temp_voice_panel(
                interaction.guild, self.voice_channel_id
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Fehler: {e}", ephemeral=True
            )


# ══════════════════════════════════════════════════════════
#         TEMP VOICE CHAT – KERN FUNKTIONEN
# ══════════════════════════════════════════════════════════

async def create_temp_voice_channel(
    member: discord.Member,
    guild: discord.Guild,
) -> None:
    """
    Erstellt einen temporären Voice- und Text-Kanal für einen User.
    Wird aufgerufen wenn jemand dem Erstell-Kanal beitritt.
    """
    try:
        category = None
        if TEMP_VOICE_CATEGORY_ID:
            category = guild.get_channel(TEMP_VOICE_CATEGORY_ID)
            if not isinstance(category, discord.CategoryChannel):
                category = None

        if category is None:
            trigger_channel = guild.get_channel(TEMP_VOICE_CHANNEL_ID)
            if trigger_channel and trigger_channel.category:
                category = trigger_channel.category

        channel_name = f"🎙️ {member.display_name}'s Kanal"

        overwrites_vc = {
            guild.default_role: discord.PermissionOverwrite(
                connect=True,
                view_channel=True,
            ),
            member: discord.PermissionOverwrite(
                connect=True,
                speak=True,
                view_channel=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
                stream=True,
            ),
            guild.me: discord.PermissionOverwrite(
                connect=True,
                view_channel=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
            ),
        }

        voice_channel = await guild.create_voice_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites_vc,
            reason=f"Temp Voice: {member.display_name}",
        )

        safe_name = re.sub(
            r'[^a-z0-9\-]', '-',
            member.display_name.lower()
            .replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
            .replace(" ", "-")
        )
        safe_name = re.sub(r'-+', '-', safe_name).strip('-')

        overwrites_tc = {
            guild.default_role: discord.PermissionOverwrite(
                read_messages=False,
                send_messages=False,
            ),
            member: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            ),
            guild.me: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                manage_messages=True,
                embed_links=True,
            ),
        }

        text_channel = await guild.create_text_channel(
            name=f"💬{safe_name}s-kanal",
            category=category,
            overwrites=overwrites_tc,
            topic=(
                f"Privater Kanal von {member.display_name} | "
                f"Voice: {channel_name} | VC-ID: {voice_channel.id}"
            ),
            reason=f"Temp Voice Text-Kanal: {member.display_name}",
        )

        temp_voice_channels[voice_channel.id] = {
            "owner_id":          member.id,
            "original_owner_id": member.id,
            "text_channel_id":   text_channel.id,
            "control_message_id": 0,
            "allowed_users":     set(),
            "locked":            False,
            "limit":             0,
        }
        # Dashboard: Temp-VC erstellt
        dashboard_state.update_temp_vcs(temp_voice_channels)

        try:
            await member.move_to(voice_channel)
        except discord.HTTPException as e:
            print(f"⚠️ Konnte {member.display_name} nicht moven: {e}")

        embed = build_vc_control_embed(guild, voice_channel.id)
        view = VoiceChannelControlView(member.id, voice_channel.id)

        control_msg = await text_channel.send(
            content=(
                f"👋 Willkommen {member.mention}!\n"
                f"Dies ist dein privater Kanal. "
                f"Nutze die Buttons unten um ihn zu verwalten."
            ),
            embed=embed,
            view=view,
        )

        temp_voice_channels[voice_channel.id]["control_message_id"] = (
            control_msg.id
        )

        try:
            await control_msg.pin()
        except discord.HTTPException:
            pass

        print(
            f"✅ [TempVC] Kanal erstellt für {member.display_name}: "
            f"VC={voice_channel.id}, TC={text_channel.id}"
        )

    except discord.Forbidden:
        print(
            f"❌ [TempVC] Keine Berechtigung für "
            f"{member.display_name}!"
        )
    except Exception as e:
        print(f"❌ [TempVC] Fehler bei Kanalerstellung: {e}")


async def delete_temp_voice_channel(
    voice_channel_id: int,
    guild: discord.Guild,
) -> None:
    """
    Löscht einen temporären Voice- und Text-Kanal.
    Wird aufgerufen wenn der Kanal leer ist.
    """
    vc_data = temp_voice_channels.pop(voice_channel_id, None)
    if not vc_data:
        return
    # Dashboard: Temp-VC gelöscht
    dashboard_state.update_temp_vcs(temp_voice_channels)

    voice_channel = guild.get_channel(voice_channel_id)
    if voice_channel:
        try:
            await voice_channel.delete(
                reason="Temp Voice: Kanal ist leer"
            )
            print(f"🗑️ [TempVC] Voice-Kanal {voice_channel_id} gelöscht")
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"❌ [TempVC] Fehler beim Löschen des VC: {e}")

    text_channel_id = vc_data.get("text_channel_id", 0)
    if text_channel_id:
        text_channel = guild.get_channel(text_channel_id)
        if text_channel:
            try:
                await text_channel.delete(
                    reason="Temp Voice: Kanal ist leer"
                )
                print(
                    f"🗑️ [TempVC] Text-Kanal {text_channel_id} gelöscht"
                )
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"❌ [TempVC] Fehler beim Löschen des TC: {e}")


# ══════════════════════════════════════════════════════════
#          FEATURE: WEEKLY REVIEW SYSTEM
# ══════════════════════════════════════════════════════════

def get_week_dates(reference_date: datetime.date = None) -> tuple[datetime.date, datetime.date]:
    """Gibt Montag und Sonntag der aktuellen/referenzierten Woche zurück."""
    if reference_date is None:
        reference_date = datetime.date.today()
    monday = reference_date - datetime.timedelta(days=reference_date.weekday())
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


def get_weekly_user_stats(
    data: dict, uid: str, monday: datetime.date, sunday: datetime.date
) -> dict:
    """Berechnet die Wochen-Statistik eines Users."""
    week_stream_days = 0
    week_present = 0
    week_absent = 0
    day_details = []
    today = datetime.date.today()

    for i in range(7):
        day = monday + datetime.timedelta(days=i)
        date_str = str(day)

        if day > today:
            day_details.append(("future", WEEKDAY_SHORT[i]))
            continue

        if date_str in data["streams"]:
            week_stream_days += 1
            if uid in data["streams"][date_str]:
                week_present += 1
                day_details.append(("present", WEEKDAY_SHORT[i]))
            else:
                week_absent += 1
                day_details.append(("absent", WEEKDAY_SHORT[i]))
        else:
            day_details.append(("no_stream", WEEKDAY_SHORT[i]))

    pct = (week_present / week_stream_days * 100) if week_stream_days > 0 else 0
    grade, grade_emoji, grade_color = get_activity_grade(pct)

    return {
        "stream_days": week_stream_days,
        "present": week_present,
        "absent": week_absent,
        "pct": pct,
        "grade": grade,
        "grade_emoji": grade_emoji,
        "grade_color": grade_color,
        "day_details": day_details,
    }


def build_weekly_day_line(day_details: list) -> str:
    """Baut die Wochen-Tages-Ansicht: Mo Di Mi Do Fr Sa So mit Emojis."""
    icons = {
        "present": "✅",
        "absent": "❌",
        "no_stream": "➖",
        "future": "⬜",
    }
    header = " ".join(f"`{d[1]}`" for d in day_details)
    values = "  ".join(f"{icons[d[0]]}" for d in day_details)
    return f"{header}\n{values}"


async def send_monthly_review(channel: discord.TextChannel, guild: discord.Guild, year: int = None, month: int = None):
    """Sendet den monatlichen Aktivitätsbericht."""
    data = load_data()
    if not data["users"]:
        await channel.send("❌ Keine User registriert.")
        return

    today = datetime.date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    month_name = MONTH_NAMES[month]
    days_in_month = calendar.monthrange(year, month)[1]
    now = datetime.datetime.now(datetime.timezone.utc)

    # Header
    header = discord.Embed(
        title=f"📅 Monatsbericht – {month_name} {year}",
        description=(
            f"Aktivitätsbericht für **{month_name} {year}**\n"
            f"Zeitraum: 01.{month:02d}.{year} – {days_in_month}.{month:02d}.{year}"
        ),
        color=COLOR_WEEKLY,
        timestamp=now,
    )
    header.set_footer(text=f"Tracking basiert auf Chat-Aktivität • {STREAMER_CHANNEL}")
    await channel.send(embed=header)

    # Stats für alle User
    user_stats = {}
    for uid in data["users"]:
        stats = get_month_activity_stats(data, uid, year, month)
        user_stats[uid] = stats

    sorted_users = sorted(
        user_stats.items(),
        key=lambda x: (x[1]["present"], x[1]["messages"]["total_messages"]),
        reverse=True,
    )

    # Einzelne User
    for rank, (uid, stats) in enumerate(sorted_users, 1):
        user_info = data["users"][uid]
        twitch_name = user_info.get("twitch_name", "?")
        display_name = user_info.get("display_name", "Unbekannt")
        member = guild.get_member(int(uid))
        mention = member.mention if member else f"**{display_name}**"
        avatar_url = member.display_avatar.url if member else None

        msg = stats["messages"]
        bar = make_progress_bar(stats["present"], stats["stream_days"], 10)
        current_streak = get_current_streak(data, uid)
        flames = "🔥" * min(current_streak, 7) if current_streak > 0 else "—"

        embed = discord.Embed(
            title=f"{get_rank_emoji(rank)} {display_name}",
            color=stats["grade_color"],
            timestamp=now,
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(
            name="👤 User",
            value=f"{mention}\n[{twitch_name}](https://twitch.tv/{twitch_name})",
            inline=True,
        )
        embed.add_field(
            name=f"{stats['grade_emoji']} Note",
            value=f"**{stats['grade']}**",
            inline=True,
        )
        embed.add_field(
            name="⚡ Streak",
            value=f"**{current_streak}** Tage\n{flames}",
            inline=True,
        )
        embed.add_field(
            name="📺 Streams",
            value=f"✅ **{stats['present']}** / {stats['stream_days']}\n❌ {stats['absent']} gefehlt",
            inline=True,
        )
        embed.add_field(
            name="💬 Nachrichten",
            value=(
                f"📝 **{msg['total_messages']:,}** gesamt\n"
                f"📊 Ø **{msg['avg_per_stream']:.0f}** / Stream\n"
                f"⏱️ Ø **{msg['avg_per_hour']:.0f}** / Stunde"
            ),
            inline=True,
        )
        embed.add_field(
            name="🕐 Peak-Zeit",
            value=f"**{msg['peak_hour']:02d}:00** Uhr\n({msg['peak_hour_count']} Nachrichten)",
            inline=True,
        )
        embed.add_field(
            name="📈 Anwesenheits-Quote",
            value=bar,
            inline=False,
        )

        cal_display = make_streak_calendar(data, uid, year, month)
        embed.add_field(
            name=f"🗓️ Kalender – {month_name}",
            value=cal_display,
            inline=False,
        )

        embed.set_footer(text=f"Rang #{rank} von {len(sorted_users)} • {month_name} {year}")
        await channel.send(embed=embed)
        await asyncio.sleep(0.5)

    # Leaderboard
    lb_lines = []
    for rank, (uid, stats) in enumerate(sorted_users, 1):
        display_name = data["users"][uid].get("display_name", "?")
        member = guild.get_member(int(uid))
        mention = member.mention if member else f"**{display_name}**"
        msg = stats["messages"]

        if rank <= 3:
            medal = ["🥇", "🥈", "🥉"][rank - 1]
            lb_lines.append(
                f"{medal} {mention}\n"
                f"╰ {stats['present']}/{stats['stream_days']} Tage ({stats['pct']:.0f}%) "
                f"• 💬 {msg['total_messages']:,} Nachrichten"
            )
        else:
            lb_lines.append(
                f"`#{rank}` {mention} — "
                f"{stats['present']}/{stats['stream_days']} ({stats['pct']:.0f}%) "
                f"• 💬 {msg['total_messages']:,}"
            )

    total_streams = sum(s["stream_days"] for s in user_stats.values()) // max(1, len(user_stats))
    total_msgs = sum(s["messages"]["total_messages"] for s in user_stats.values())
    avg_pct = sum(s["pct"] for s in user_stats.values()) / max(1, len(user_stats))

    lb_embed = discord.Embed(
        title=f"🏆 Monats-Leaderboard – {month_name} {year}",
        description="\n\n".join(lb_lines),
        color=COLOR_GOLD,
        timestamp=now,
    )
    lb_embed.add_field(
        name="📊 Monats-Zusammenfassung",
        value=(
            f"📺 **Streams:** `{total_streams}`\n"
            f"👥 **User:** `{len(data['users'])}`\n"
            f"💬 **Nachrichten gesamt:** `{total_msgs:,}`\n"
            f"📈 **Ø Quote:** `{avg_pct:.0f}%`"
        ),
        inline=False,
    )
    lb_embed.set_footer(text=f"📅 {month_name} {year}")
    await channel.send(embed=lb_embed)

    print(f"✅ [Review] Monatsbericht für {month_name} {year} gesendet ({len(sorted_users)} User)")


async def send_yearly_review(channel: discord.TextChannel, guild: discord.Guild, year: int = None):
    """Sendet den jährlichen Aktivitätsbericht."""
    data = load_data()
    if not data["users"]:
        await channel.send("❌ Keine User registriert.")
        return

    if year is None:
        year = datetime.date.today().year

    now = datetime.datetime.now(datetime.timezone.utc)

    header = discord.Embed(
        title=f"🎆 Jahresbericht {year}",
        description=(
            f"**Das große Jahres-Review!**\n"
            f"Zeitraum: 01.01.{year} – 31.12.{year}"
        ),
        color=COLOR_GOLD,
        timestamp=now,
    )
    await channel.send(embed=header)

    # Stats pro User fürs ganze Jahr
    user_year_stats = {}
    for uid in data["users"]:
        total_present = 0
        total_streams = 0
        monthly_data = []

        for m in range(1, 13):
            m_stats = get_month_activity_stats(data, uid, year, m)
            total_present += m_stats["present"]
            total_streams += m_stats["stream_days"]
            monthly_data.append(m_stats)

        total_pct = (total_present / total_streams * 100) if total_streams > 0 else 0
        grade, grade_emoji, grade_color = get_activity_grade(total_pct)
        year_msgs = get_year_message_stats(data, uid, year)
        longest = get_longest_streak(data, uid)

        user_year_stats[uid] = {
            "present": total_present,
            "stream_days": total_streams,
            "absent": total_streams - total_present,
            "pct": total_pct,
            "grade": grade,
            "grade_emoji": grade_emoji,
            "grade_color": grade_color,
            "messages": year_msgs,
            "monthly": monthly_data,
            "longest_streak": longest,
        }

    sorted_users = sorted(
        user_year_stats.items(),
        key=lambda x: (x[1]["present"], x[1]["messages"]["total_messages"]),
        reverse=True,
    )

    for rank, (uid, stats) in enumerate(sorted_users, 1):
        user_info = data["users"][uid]
        display_name = user_info.get("display_name", "?")
        twitch_name = user_info.get("twitch_name", "?")
        member = guild.get_member(int(uid))
        mention = member.mention if member else f"**{display_name}**"
        avatar_url = member.display_avatar.url if member else None
        msg = stats["messages"]

        # Monats-Verlauf als Mini-Chart
        month_bars = []
        for m in range(1, 13):
            m_stats = stats["monthly"][m - 1]
            if m_stats["stream_days"] > 0:
                pct = m_stats["present"] / m_stats["stream_days"]
                bar_char = "█" if pct >= 0.8 else "▓" if pct >= 0.5 else "░" if pct > 0 else "·"
            else:
                bar_char = "·"
            month_bars.append(bar_char)

        chart = "`" + "".join(month_bars) + "`"
        chart_labels = "`JFMAMJJASOND`"

        embed = discord.Embed(
            title=f"{get_rank_emoji(rank)} {display_name} – Jahresrückblick {year}",
            color=stats["grade_color"],
            timestamp=now,
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(
            name="👤 User",
            value=f"{mention}\n[{twitch_name}](https://twitch.tv/{twitch_name})",
            inline=True,
        )
        embed.add_field(
            name=f"{stats['grade_emoji']} Jahres-Note",
            value=f"**{stats['grade']}**",
            inline=True,
        )
        embed.add_field(
            name="🏆 Längste Streak",
            value=f"**{stats['longest_streak']}** Tage",
            inline=True,
        )
        embed.add_field(
            name="📺 Streams",
            value=(
                f"✅ **{stats['present']}** anwesend\n"
                f"❌ **{stats['absent']}** gefehlt\n"
                f"📊 **{stats['pct']:.0f}%** Quote"
            ),
            inline=True,
        )
        embed.add_field(
            name="💬 Nachrichten",
            value=(
                f"📝 **{msg['total_messages']:,}** gesamt\n"
                f"📊 Ø **{msg['avg_per_stream']:.0f}** / Stream\n"
                f"⏱️ Ø **{msg['avg_per_hour']:.0f}** / Stunde"
            ),
            inline=True,
        )
        embed.add_field(
            name="🕐 Peak-Zeit",
            value=f"**{msg['peak_hour']:02d}:00** Uhr",
            inline=True,
        )

        bar = make_progress_bar(stats["present"], stats["stream_days"], 12)
        embed.add_field(name="📈 Jahres-Quote", value=bar, inline=False)

        embed.add_field(
            name="📊 Monats-Verlauf",
            value=f"{chart_labels}\n{chart}\n╰ █ >80% ▓ >50% ░ <50% · kein Stream",
            inline=False,
        )

        embed.set_footer(text=f"Rang #{rank} • Jahresbericht {year}")
        await channel.send(embed=embed)
        await asyncio.sleep(0.5)

    # Jahres-Leaderboard
    total_msgs_all = sum(s["messages"]["total_messages"] for s in user_year_stats.values())
    total_streams_avg = sum(s["stream_days"] for s in user_year_stats.values()) // max(1, len(user_year_stats))

    lb_lines = []
    for rank, (uid, stats) in enumerate(sorted_users[:10], 1):
        display_name = data["users"][uid].get("display_name", "?")
        member = guild.get_member(int(uid))
        mention = member.mention if member else f"**{display_name}**"
        medal = get_rank_emoji(rank)
        lb_lines.append(
            f"{medal} {mention}\n"
            f"╰ {stats['present']}/{stats['stream_days']} ({stats['pct']:.0f}%) "
            f"• 💬 {stats['messages']['total_messages']:,}"
        )

    lb_embed = discord.Embed(
        title=f"🏆 Jahres-Leaderboard {year}",
        description="\n\n".join(lb_lines),
        color=COLOR_GOLD,
        timestamp=now,
    )
    lb_embed.add_field(
        name="📊 Jahres-Zusammenfassung",
        value=(
            f"📺 **Ø Streams/Monat:** `{total_streams_avg}`\n"
            f"👥 **User:** `{len(data['users'])}`\n"
            f"💬 **Nachrichten gesamt:** `{total_msgs_all:,}`"
        ),
        inline=False,
    )
    lb_embed.set_footer(text=f"🎆 Jahresbericht {year}")
    await channel.send(embed=lb_embed)

    print(f"✅ [Review] Jahresbericht {year} gesendet ({len(sorted_users)} User)")

### MUSIC MODULE START ###
# ══════════════════════════════════════════════════════════
#     FEATURE: MUSIK-MODUL (yt-dlp + FFmpeg Streaming)
# ══════════════════════════════════════════════════════════

YDL = None
if MUSIC_ENABLED:
    try:
        import yt_dlp
        YDL = True
        print("🎵 yt-dlp erfolgreich geladen.")
    except ImportError:
        print("⚠️ yt-dlp nicht installiert – Musik-Modul deaktiviert. (pip install yt-dlp)")
        MUSIC_ENABLED = False
        YDL = None

FFMPEG_AVAILABLE = True  # wird in check_system_requirements() geprüft

# YouTube-Stream-URLs (googlevideo.com) verlangen inzwischen einen
# Referer-Header – ohne diesen bekommt FFmpeg eine 403 Forbidden,
# egal welcher Player-Client die URL geliefert hat. Deshalb senden
# wir Referer + User-Agent mit jedem Fetch (für lokale Dateien
# und andere Streams ist der Header harmlos).
_YT_HEADERS = (
    "Referer: https://www.youtube.com/\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36\r\n"
)

FFMPEG_OPTIONS = {
    # shlex-tauglich: Header-Zeichenkette in Anführungszeichen,
    # damit das \r\n erhalten bleibt
    'before_options': (
        "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        f'-headers "{_YT_HEADERS}"'
    ),
    'options': '-vn',
}

# Netzwerk-Timeouts für yt-dlp – verhindert, dass der Bot auf dem Pi
# ewig hängt wenn ein Stream nicht erreichbar ist (statt "hängt"
# wird der Song übersprungen und der nächste versucht).
YDL_TIMEOUTS = {
    'socket_timeout': 20,
    'request_timeout': 20,
    'extractor_retries': 2,
}

YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'extract_flat': False,
    'cachedir': False,
    'no_cache_dir': True,
    **YDL_TIMEOUTS,
}

YTDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0', # Erzwingt IPv4
    'extract_flat': True,        # 👈 Ändern auf True für High-Speed Suche
    'cachedir': False,
    **YDL_TIMEOUTS,
}

YTDL_PLAYLIST_OPTIONS = {
    **YTDL_FORMAT_OPTIONS,
    'noplaylist': False,
    'extract_flat': True,
    'playlistend': 50,
}


class SongInfo:
    __slots__ = ('title', 'url', 'webpage_url', 'duration', 'thumbnail', 'requester')

    def __init__(self, title, url, webpage_url, duration, thumbnail, requester):
        self.title = title
        self.url = url
        self.webpage_url = webpage_url
        self.duration = int(duration) if duration else 0
        self.thumbnail = thumbnail
        self.requester = requester

    @property
    def duration_str(self) -> str:
        if self.duration <= 0:
            return "Live"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"


class GuildMusicState:
    def __init__(self, guild_id: int, bot):
        self.guild_id = guild_id
        self.bot = bot
        self.queue: deque[SongInfo] = deque()
        self.current: SongInfo | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.autoplay: bool = False
        self.history: deque[str] = deque(maxlen=10)
        self._idle_seconds: int = 0
        self._lock = asyncio.Lock()
        # Counting consecutive playback failures – prevents an
        # endless "Konnte nicht abspielen" message loop
        # (e.g., when YouTube is temporarily unreachable)
        self.consecutive_failures: int = 0

    async def cleanup(self):
        self.queue.clear()
        self.current = None
        self._idle_seconds = 0
        self.history.clear()
        if self.voice_client and self.voice_client.is_connected():
            try:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                await self.voice_client.disconnect(force=True)
            except Exception:
                pass
        self.voice_client = None
        # Präsenz zurücksetzen
        try:
            await d_bot.change_presence(activity=None)
        except Exception:
            pass
        # Dashboard: Musik gestoppt
        dashboard_state.update_now_playing(None)
    def reset_idle(self):
        self._idle_seconds = 0

    def increment_idle(self):
        self._idle_seconds += 1

    @property
    def is_idle_timeout(self) -> bool:
        return self._idle_seconds >= MUSIC_IDLE_TIMEOUT

    def note_failure(self) -> int:
        self.consecutive_failures += 1
        return self.consecutive_failures


music_states: dict[int, GuildMusicState] = {}


def get_music_state(guild_id: int, bot) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id, bot)
    return music_states[guild_id]


async def ytdl_extract(query: str, options: dict) -> dict | None:
    if not MUSIC_ENABLED:
        return None
    # Netzwerk-Timeouts immer mitgeben (Aufrufer können überschreiben)
    opts = {**YDL_TIMEOUTS, **options}
    loop = asyncio.get_running_loop()
    try:
        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(query, download=False)
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        print(f"❌ [Music] yt-dlp Fehler: {e}")
        return None


# Fallback-Kette für YouTube-Player-Clients (Reihenfolge = Priorität):
#   tv        → zuverlässigste Stream-URLs ohne Sonderbehandlung
#   android   → sehr zuverlässig
#   ios       → zuverlässig
#   web       → braucht Referer-Header (den FFmpeg jetzt mitschickt)
#   None      → yt-dlp-Standard (inkl. mweb/android_vr) als letztes
# Jeder Client wird NUR ausprobiert, wenn der vorherige fehlschlägt –
# auch wenn das Playback selbst eine 403 zurückgibt (neuer Stand:
# ein erfolgreicher Extract heißt NICHT, dass FFmpeg die URL laden
# kann).
YDL_PLAYER_CLIENT_FALLBACKS = [
    {'extractor_args': {'youtube': {'player_client': ['tv']}}},
    {'extractor_args': {'youtube': {'player_client': ['android']}}},
    {'extractor_args': {'youtube': {'player_client': ['ios']}}},
    {'extractor_args': {'youtube': {'player_client': ['web']}}},
    None,  # yt-dlp-Standard
]


def _ydl_resolve_once(webpage_url: str, extra_opts: dict | None = None) -> dict | None:
    """Einmaliger yt-dlp-Resolve (läuft im Executor, nie im Event-Loop)."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
        'extract_flat': False,
        'nocheckcertificate': True,
        **YDL_TIMEOUTS,
    }
    if extra_opts:
        ydl_opts.update(extra_opts)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(webpage_url, download=False)


def _resolve_with_client(webpage_url: str, extra_opts: dict | None = None) -> tuple[str | None, dict | None]:
    """Einmaliger yt-dlp-Resolve mit einem bestimmten Client.

    Liefert (stream_url, info). Läuft nur synchron – immer im Executor
    aufrufen.
    """
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
        'extract_flat': False,
        'nocheckcertificate': True,
        'cachedir': False,
        **YDL_TIMEOUTS,
    }
    if extra_opts:
        ydl_opts.update(extra_opts)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(webpage_url, download=False)

    if not info:
        return None, None

    stream_url = None
    if 'url' in info and info['url']:
        stream_url = info['url']
    elif 'entries' in info and info['entries']:
        first = info['entries'][0]
        if first and first.get('url'):
            info = first
            stream_url = first['url']
    return stream_url, info


async def resolve_song_url(song: SongInfo) -> str | None:
    """
    Stream-URL Auflösung mit Fallback-Kette (mehrere Player-Clients).
    Holt die Audio-URL und repariert bei Bedarf die Referenz-URL.
    Gibt None zurück wenn alles fehlschlägt (Song wird übersprungen).

    Hinweis: Für das eigentliche Playback nutzt play_next() den
    robusteren _play_song_robust()-Pfad, der auch auf 403-Fehler
    beim FFmpeg-Fetch reagiert.
    """
    if not song.webpage_url:
        return None

    loop = asyncio.get_running_loop()
    for i, extra_opts in enumerate(YDL_PLAYER_CLIENT_FALLBACKS):
        try:
            stream_url, info = await loop.run_in_executor(
                None, _resolve_with_client, song.webpage_url, extra_opts
            )
        except Exception as e:
            print(f"⚠️ [Music] Resolve Versuch {i + 1} fehlgeschlagen: "
                  f"{type(e).__name__}: {e}")
            continue

        if stream_url and info:
            ref_url = _get_reference_url(info, song.webpage_url)
            if ref_url:
                song.webpage_url = ref_url
            return stream_url

    print(f"❌ [Music] Alle Resolve-Versuche fehlgeschlagen: {song.title}")
    return None


def _download_song_blocking(webpage_url: str, out_dir: str) -> str | None:
    """Lädt einen Song als Datei herunter (yt-dlp, läuft im Executor).

    Das ist das ultimative Mittel gegen 403/IP-Wechsel-Problem:
    yt-dlp lädt die Stream-URL selbst (mit allen Headern & Retries)
    herunter, FFmpeg spielt nur noch die lokale Datei ab.
    """
    import os
    opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
        'nocheckcertificate': True,
        'cachedir': False,
        'outtmpl': os.path.join(out_dir, "%(id)s.%(ext)s"),
        **YDL_TIMEOUTS,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
        if not info:
            return None
        if 'entries' in info:
            entries = [e for e in info['entries'] if e]
            info = entries[0] if entries else None
        if not info:
            return None
        target = ydl.prepare_filename(info)
        ydl.download([info.get('webpage_url') or webpage_url])
    return target if target and os.path.exists(target) else None


async def download_song_file(webpage_url: str) -> str | None:
    """Download mit Timeout – liefert Pfad oder None."""
    import shutil as _shutil
    tmp_dir = tempfile.mkdtemp(prefix="music_dl_")
    loop = asyncio.get_running_loop()
    try:
        path = await asyncio.wait_for(
            loop.run_in_executor(None, _download_song_blocking, webpage_url, tmp_dir),
            timeout=180,
        )
    except asyncio.TimeoutError:
        print("⚠️ [Music] Download-Fallback: Timeout (180 s)")
        path = None
    except Exception as e:
        print(f"⚠️ [Music] Download-Fehler: {type(e).__name__}: {e}")
        path = None
    if not path:
        _shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    return path

async def search_tracks(query: str, limit: int = 1) -> list[dict]:
    """
    Schnelle Suche:
    - URL -> direkt laden
    - Text -> ytsearchX mit frei wählbarem Limit
    - Für /play: limit=1
    - Für /search: limit=10
    """

    # Wenn es eine URL ist -> direkt extrahieren
    if _is_url(query):
        info = await ytdl_extract(query, {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'source_address': '0.0.0.0',
        })
        if info:
            info["webpage_url"] = _get_reference_url(info, query)
            return [info]
        return []

    # Limit absichern
    limit = max(1, min(limit, 10))

    # Schnelle Textsuche
    info = await ytdl_extract(f"ytsearch{limit}:{query}", {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'source_address': '0.0.0.0',
        'extract_flat': True,   # schnell
        'skip_download': True,
        'cachedir': False,
        'no_cache_dir': True,
    })

    if info and 'entries' in info:
        results = []
        for e in info['entries']:
            if not e:
                continue

            # Webseite-URL sicher setzen
            if not e.get('webpage_url') and e.get('id'):
                e['webpage_url'] = f"https://www.youtube.com/watch?v={e['id']}"

            results.append(e)

        return results

    return []

async def extract_playlist(url: str) -> list[dict]:
    """Extrahiert alle Tracks einer YouTube-Playlist."""
    info = await ytdl_extract(url, YTDL_PLAYLIST_OPTIONS)
    if not info:
        return []
    if 'entries' in info:
        return [e for e in info['entries'] if e]
    return [info] if info.get('url') or info.get('webpage_url') else []


async def get_related_track(video_url: str) -> dict | None:
    """Holt einen verwandten Track für Autoplay."""
    video_id = _extract_video_id(video_url)
    print(f"🔄 [Music] Autoplay: Video-ID = '{video_id}' aus URL: {video_url[:60]}")

    if not video_id or len(video_id) != 11:
        print(f"⚠️ [Music] Autoplay: Ungültige Video-ID, versuche Titel-Suche")
        # Fallback: Suche nach zufälliger Musik
        try:
            info = await ytdl_extract("ytsearch3:popular music audio 2024", YTDL_SEARCH_OPTIONS)
            if info and 'entries' in info:
                entries = [e for e in info['entries'] if e and e.get('title')]
                if entries:
                    chosen = random.choice(entries)
                    print(f"🔄 [Music] Autoplay (Random): {chosen.get('title')}")
                    return chosen
        except Exception:
            pass
        return None

    # Methode 1: YouTube Radio Mix
    try:
        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        print(f"🔄 [Music] Autoplay: Versuche Mix: {mix_url}")
        opts = {
            'format': 'bestaudio/best',
            'noplaylist': False,
            'extract_flat': False,
            'playlistend': 8,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'nocheckcertificate': True,
            'source_address': '0.0.0.0',
            'cachedir': False,
        }
        info = await ytdl_extract(mix_url, opts)
        if info and 'entries' in info:
            entries = [
                e for e in info['entries']
                if e
                and e.get('title')
                and 'videoplayback' not in e.get('title', '')
                and e.get('id', '') != video_id
            ]
            if entries:
                chosen = random.choice(entries[:5])
                # Sicherstellen dass webpage_url gesetzt ist
                if not chosen.get('webpage_url') and chosen.get('id'):
                    chosen['webpage_url'] = f"https://www.youtube.com/watch?v={chosen['id']}"
                print(f"🔄 [Music] Autoplay (Mix): {chosen.get('title')}")
                return chosen
            else:
                print(f"⚠️ [Music] Autoplay: Mix hatte {len(info.get('entries', []))} Einträge, aber keine brauchbaren")
        else:
            print(f"⚠️ [Music] Autoplay: Mix lieferte keine Entries")
    except Exception as e:
        print(f"⚠️ [Music] Autoplay Mix Fehler: {e}")

    # Methode 2: Titel des aktuellen Songs suchen und ähnliche finden
    try:
        print(f"🔄 [Music] Autoplay: Versuche Titel-basierte Suche...")
        title_url = f"https://www.youtube.com/watch?v={video_id}"
        title_info = await ytdl_extract(title_url, {
            **YTDL_FORMAT_OPTIONS,
            'extract_flat': False,
            'ignoreerrors': True,
        })
        search_query = "popular music audio"
        if title_info and title_info.get('title') and 'videoplayback' not in title_info['title']:
            search_query = f"{title_info['title']} similar audio"
            print(f"🔄 [Music] Autoplay: Suche nach '{search_query}'")

        fallback_info = await ytdl_extract(f"ytsearch5:{search_query}", YTDL_SEARCH_OPTIONS)
        if fallback_info and 'entries' in fallback_info:
            entries = [
                e for e in fallback_info['entries']
                if e
                and e.get('title')
                and 'videoplayback' not in e.get('title', '')
                and e.get('id', '') != video_id
            ]
            if entries:
                chosen = random.choice(entries[:3])
                if not chosen.get('webpage_url') and chosen.get('id'):
                    chosen['webpage_url'] = f"https://www.youtube.com/watch?v={chosen['id']}"
                print(f"🔄 [Music] Autoplay (Suche): {chosen.get('title')}")
                return chosen
    except Exception as e:
        print(f"⚠️ [Music] Autoplay Suche Fehler: {e}")

    # Methode 3: Absoluter Fallback
    try:
        print(f"🔄 [Music] Autoplay: Letzter Versuch mit generischer Suche...")
        info = await ytdl_extract("ytsearch3:trending music audio", YTDL_SEARCH_OPTIONS)
        if info and 'entries' in info:
            entries = [e for e in info['entries'] if e and e.get('title')]
            if entries:
                chosen = random.choice(entries)
                if not chosen.get('webpage_url') and chosen.get('id'):
                    chosen['webpage_url'] = f"https://www.youtube.com/watch?v={chosen['id']}"
                print(f"🔄 [Music] Autoplay (Fallback): {chosen.get('title')}")
                return chosen
    except Exception:
        pass

    print("❌ [Music] Autoplay: Alle Methoden fehlgeschlagen")
    return None


def _extract_video_id(url: str) -> str:
    if not url:
        return ""
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""

def _get_reference_url(entry: dict, fallback_url: str = "") -> str:
    """
    Baut eine stabile Referenz-URL für SongInfo.webpage_url.
    Bevorzugt YouTube Watch-URLs statt direkter googlevideo-Streams.
    """

    # 1) Erst bevorzugte Kandidaten prüfen
    candidates = [
        entry.get("webpage_url"),
        entry.get("original_url"),
        fallback_url,
    ]

    for candidate in candidates:
        if not candidate or not isinstance(candidate, str):
            continue

        video_id = _extract_video_id(candidate)
        if video_id and len(video_id) == 11:
            return f"https://www.youtube.com/watch?v={video_id}"

        if "youtube.com/watch" in candidate or "youtu.be/" in candidate:
            return candidate

    # 2) Falls yt-dlp nur eine ID geliefert hat
    entry_id = str(entry.get("id", "")).strip()
    extractor = str(entry.get("extractor") or entry.get("extractor_key") or "").lower()

    if entry_id and len(entry_id) == 11 and "youtube" in extractor:
        return f"https://www.youtube.com/watch?v={entry_id}"

    # 3) Notfalls irgendeine brauchbare Nicht-googlevideo-URL zurückgeben
    for candidate in candidates:
        if (
            candidate
            and isinstance(candidate, str)
            and candidate.startswith(("http://", "https://"))
            and "googlevideo.com" not in candidate
        ):
            return candidate

    return ""

def _entry_thumbnail(entry: dict) -> str:
    """Liest das Thumbnail eines yt-dlp-Entries.

    `extract_flat`-Suche liefert `thumbnails` (Liste),
    eine Voll-Extraktion liefert `thumbnail` (String).
    """
    if not entry:
        return ""
    thumb = entry.get("thumbnail")
    if isinstance(thumb, str) and thumb:
        return thumb
    thumbs = entry.get("thumbnails") or []
    for t in thumbs:
        url = t.get("url") if isinstance(t, dict) else None
        if url:
            return url
    return ""


def _is_url(query: str) -> bool:
    return query.startswith(('http://', 'https://', 'www.'))


def _is_playlist_url(query: str) -> bool:
    return _is_url(query) and ('list=' in query or '/playlist' in query)


# Wartezeit (Sekunden) bis ein Stream-Versuch als "läuft" gilt –
# 403-Fehler zeigen sich in dieser Zeit (FFmpeg bricht sofort ab)
STREAM_CONFIRM_SECONDS = 8


def _client_name(extra_opts: dict | None) -> str:
    """Kurzbezeichnung eines Player-Clients für Log-Meldungen."""
    if not extra_opts:
        return "Standard"
    clients = extra_opts.get("extractor_args", {}).get("youtube", {}).get("player_client", [])
    return "+".join(clients) if clients else "?"


async def _announce_now_playing(
    state: GuildMusicState,
    song: SongInfo,
    text_channel: discord.TextChannel = None,
) -> None:
    """History, Dashboard-Now-Playing, Embed und Präsenz aktualisieren."""
    # History tracken – MUSS eine youtube.com/watch URL sein
    history_url = _get_reference_url({"webpage_url": song.webpage_url}, song.webpage_url)
    if history_url and "youtube.com/watch" in history_url:
        song.webpage_url = history_url
        state.history.append(history_url)
        print(f"📝 [Music] History gespeichert: {history_url}")

    # Dashboard: Now Playing
    try:
        _req = song.requester.display_name if song.requester else "Autoplay"
    except Exception:
        _req = "?"
    try:
        dashboard_state.update_now_playing({
            "title":        song.title,
            "thumbnail":    song.thumbnail or "",
            "requester":    _req,
            "duration_str": song.duration_str,
            "webpage_url":  song.webpage_url or "",
            "duration":     song.duration,
        })
    except Exception:
        pass

    # Now Playing Embed senden
    if text_channel:
        try:
            req_mention = song.requester.mention if song.requester else "Unbekannt"
        except Exception:
            req_mention = "Unbekannt"
        title_short = song.title[:80] + "..." if len(song.title) > 80 else song.title
        embed = discord.Embed(
            title="🎶 Jetzt spielt",
            description=f"**{title_short}**",
            color=COLOR_MUSIC,
        )
        embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
        embed.add_field(name="👤 Angefragt von", value=req_mention, inline=True)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        remaining = int(sum(s.duration for s in state.queue))
        if state.queue:
            embed.add_field(
                name="📋 In Warteschlange",
                value=f"{len(state.queue)} Songs ({remaining // 60}:{remaining % 60:02d})",
                inline=False,
            )
        embed.set_footer(text=f"Autoplay: {'✅ An' if state.autoplay else '❌ Aus'}")

        # Link-Button zum Song
        view = None
        if song.webpage_url and song.webpage_url.startswith("http"):
            class SongLinkView(discord.ui.View):
                def __init__(self, url: str):
                    # Timeout statt persistierend – sonst sammelt sich
                    # für jeden gespielten Song eine View im Speicher an
                    super().__init__(timeout=1800)
                    self.add_item(discord.ui.Button(
                        label="▶️ Auf YouTube öffnen",
                        url=url,
                        style=discord.ButtonStyle.link,
                    ))
            view = SongLinkView(song.webpage_url)

        try:
            await text_channel.send(
                embed=embed,
                view=view,
                delete_after=song.duration + 5 if song.duration > 0 else 60,
            )
        except Exception as e:
            print(f"⚠️ [Music] Embed senden fehlgeschlagen: {e}")

    # Bot-Präsenz aktualisieren
    try:
        title_short = song.title[:128] if song.title else "Musik"
        await d_bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=title_short,
            )
        )
    except Exception as e:
        print(f"⚠️ [Music] Präsenz-Update fehlgeschlagen: {e}")


async def _play_song_robust(
    state: GuildMusicState,
    song: SongInfo,
    text_channel: discord.TextChannel = None,
) -> bool:
    """Spielt einen Song ab – mit automatischer Fallback-Kette.

    Reihenfolge:
      1. Stream-URL mit YouTube-Client tv
      2. … android → ios → web → Standard
      3. Datei-Download (yt-dlp) → lokales Playback

    Ein 403 vom Server wird als "früher Fehler" erkannt, sobald
    FFmpeg sofort abbricht – dann wird automatisch der nächste
    Client / der Download probiert. Der Song bleibt in der Queue.
    """
    voice = state.voice_client
    if not voice or not voice.is_connected():
        return False

    # Laufenden Loop direkt einfangen (funktioniert unabhängig vom
    # Bot-Zustand; state.bot.loop ist nur nach Login verfügbar)
    loop = asyncio.get_running_loop()

    # ── 1) Stream-Versuche mit verschiedenen Clients ──────────
    for i, extra_opts in enumerate(YDL_PLAYER_CLIENT_FALLBACKS):
        if not voice.is_connected():
            return False

        try:
            stream_url, info = await loop.run_in_executor(
                None, _resolve_with_client, song.webpage_url, extra_opts
            )
        except Exception as e:
            print(f"⚠️ [Music] Client '{_client_name(extra_opts)}' – "
                  f"Resolve fehlgeschlagen: {type(e).__name__}: {e}")
            continue

        if not stream_url or not info:
            print(f"⚠️ [Music] Client '{_client_name(extra_opts)}' lieferte keine URL")
            continue

        # Referenz-URL reparieren (stabile Watch-URL für Autoplay/History)
        ref_url = _get_reference_url(info, song.webpage_url)
        if ref_url:
            song.webpage_url = ref_url

        try:
            source = discord.FFmpegOpusAudio(stream_url, **FFMPEG_OPTIONS)
        except Exception as e:
            print(f"⚠️ [Music] FFmpeg-Start fehlgeschlagen "
                  f"(Client '{_client_name(extra_opts)}'): {e}")
            continue

        flag = {"confirmed": False, "early_error": None}

        def after_playing(error, _flag=flag):
            if error is None:
                # Track beendet → nächsten Song
                asyncio.run_coroutine_threadsafe(play_next(state, text_channel), loop)
                return
            if _flag["confirmed"]:
                # Fehler NACH Start (Netzwerk-Blip, Reconnect fehlgeschlagen)
                # → nächsten Song
                print(f"❌ [Music] Playback-Fehler (nach Start): {error}")
                asyncio.run_coroutine_threadsafe(play_next(state, text_channel), loop)
            else:
                # Früher Fehler (z. B. 403 Forbidden) → nächster Client
                _flag["early_error"] = error

        try:
            voice.play(source, after=after_playing)
        except Exception as e:
            print(f"⚠️ [Music] play() fehlgeschlagen: {e}")
            continue

        # Auf frühe Fehler warten (403 zeigt sich sofort);
        # is_playing() allein reicht NICHT – es ist direkt nach
        # play() True, auch wenn FFmpeg gleich scheitern wird.
        deadline = time.monotonic() + STREAM_CONFIRM_SECONDS
        failed = False
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if flag["early_error"] is not None:
                print(f"⚠️ [Music] Stream abgelehnt (Client "
                      f"'{_client_name(extra_opts)}'): {flag['early_error']}")
                failed = True
                break

        if failed:
            try:
                voice.stop()
            except Exception:
                pass
            continue

        # ── Playback läuft (oder puffert) ──
        flag["confirmed"] = True
        state.consecutive_failures = 0
        print(f"🎶 [Music] Spielt jetzt: {song.title} "
              f"(Client '{_client_name(extra_opts)}')")
        await _announce_now_playing(state, song, text_channel)
        return True

    # ── 2) Datei-Download als letztes Mittel ───────────────────
    print(f"🔄 [Music] Alle Stream-Versuche fehlgeschlagen – lade "
          f"**{song.title}** als Datei herunter …")
    path = await download_song_file(song.webpage_url)
    if not path:
        return False
    if not voice.is_connected():
        import shutil as _shutil
        _shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        return False

    try:
        source = discord.FFmpegOpusAudio(path, **FFMPEG_OPTIONS)
    except Exception as e:
        print(f"❌ [Music] FFmpeg-Datei-Fehler: {e}")
        return False

    def after_file(error):
        if error:
            print(f"❌ [Music] Datei-Playback-Fehler: {error}")
        # Datei aufräumen + nächsten Song
        try:
            import shutil as _shutil
            _shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        except Exception:
            pass
        asyncio.run_coroutine_threadsafe(play_next(state, text_channel), loop)

    try:
        voice.play(source, after=after_file)
    except Exception as e:
        print(f"❌ [Music] Datei-play() fehlgeschlagen: {e}")
        return False

    state.consecutive_failures = 0
    print(f"🎶 [Music] Spielt jetzt (Datei-Download): {song.title}")
    await _announce_now_playing(state, song, text_channel)
    return True


async def play_next(state: GuildMusicState, text_channel: discord.TextChannel = None):
    if not state.voice_client or not state.voice_client.is_connected():
        return

    # FFmpeg fehlt → Playback ist unmöglich (häufigster Grund für
    # "Musik funktioniert nicht" auf dem Raspberry Pi)
    if not FFMPEG_AVAILABLE:
        if text_channel:
            try:
                await text_channel.send(
                    "❌ **Musik-Playback ist nicht möglich:** FFmpeg ist nicht "
                    "installiert!\n"
                    "-# Raspberry Pi: `sudo apt update && sudo apt install -y ffmpeg` "
                    "→ danach den Bot neu starten.",
                    delete_after=60,
                )
            except Exception:
                pass
        print("❌ [Music] FFmpeg fehlt – kann nicht abspielen "
              "(sudo apt install ffmpeg)")
        state.queue.clear()
        return

    # Queue leer → Autoplay versuchen
    if not state.queue:
        if state.autoplay and state.history:
            last_url = state.history[-1]
            print(f"🔄 [Music] Autoplay: Suche verwandten Track für {last_url}")
            related = await get_related_track(last_url)
            if related:
                title = related.get('title', 'Unbekannt')
                webpage_url = related.get('webpage_url') or related.get('url', '')
                duration = int(related.get('duration', 0)) if related.get('duration') else 0
                thumbnail = related.get('thumbnail', '')
                bot_member = state.voice_client.guild.me
                autoplay_song = SongInfo(title, '', webpage_url, duration, thumbnail, bot_member)
                state.queue.append(autoplay_song)
                print(f"🔄 [Music] Autoplay: {title} zur Queue hinzugefügt")
                if text_channel:
                    embed = discord.Embed(
                        title="🔄 Autoplay",
                        description=f"**{title}**",
                        color=COLOR_MUSIC,
                    )
                    embed.set_footer(text="Basierend auf deinen letzten Songs")
                    try:
                        await text_channel.send(embed=embed, delete_after=30)
                    except Exception:
                        pass
            else:
                print("⚠️ [Music] Autoplay: Kein Track gefunden, stoppe.")
                state.current = None
                return
        else:
            state.current = None
            return

    if not state.queue:
        state.current = None
        return

    song = state.queue.popleft()
    state.current = song
    state.reset_idle()

    # Robustes Playback:
    #   1) Stream-URL mit mehreren YouTube-Clients (tv → android →
    #      ios → web → Standard) – auch Server-Ablehnungen (403)
    #      werden abgefangen, dann wird der nächste Client probiert
    #   2) Datei-Download (yt-dlp) als letztes Mittel
    played = await _play_song_robust(state, song, text_channel)

    if not played:
        state.current = None  # wieder "idle"-fähig machen
        failures = state.note_failure()
        if text_channel and failures <= 3:
            # nach 3 Fehlern keine Spam-Loop mehr, nur Logging
            try:
                await text_channel.send(
                    f"❌ Konnte **{song.title}** nicht abspielen. Überspringe...",
                    delete_after=10,
                )
            except Exception:
                pass
        elif failures == 4:
            print(f"⚠️ [Music] {failures - 1} Songs in Folge fehlgeschlagen – "
                  f"weitere Fehler-Meldungen im Chat werden unterdrückt")
        if state.queue:
            # Nächsten Song versuchen (ohne Rekursion im Lock)
            asyncio.create_task(play_next(state, text_channel))
        return

# ── Queue Pagination View ────────────────────────────────

class QueuePaginationView(discord.ui.View):
    def __init__(self, state: GuildMusicState, page: int = 0):
        super().__init__(timeout=60)
        self.state = state
        self.page = page
        self.max_page = max(0, (len(state.queue) - 1) // QUEUE_PAGE_SIZE)
        self._update_buttons()

    def _update_buttons(self):
        self.btn_prev.disabled = self.page <= 0
        self.btn_next.disabled = self.page >= self.max_page

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📋 Warteschlange",
            color=COLOR_MUSIC,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        if self.state.current:
            try:
                req = self.state.current.requester.mention if self.state.current.requester else "Unbekannt"
            except Exception:
                req = "Unbekannt"
            title_short = self.state.current.title[:80] + "..." if len(self.state.current.title) > 80 else self.state.current.title
            np_value = f"**{title_short}** — `{self.state.current.duration_str}`\n{req}"
            embed.add_field(
                name="🎶 Jetzt spielt",
                value=np_value[:1024],
                inline=False,
            )
        else:
            embed.add_field(name="🎶 Jetzt spielt", value="*Nichts*", inline=False)

        queue_list = list(self.state.queue)
        total = len(queue_list)

        if total == 0:
            embed.add_field(name="📋 Warteschlange", value="*Leer*", inline=False)
        else:
            start = self.page * QUEUE_PAGE_SIZE
            end = min(start + QUEUE_PAGE_SIZE, total)
            page_items = queue_list[start:end]
            lines = []
            for i, song in enumerate(page_items, start + 1):
                try:
                    req = song.requester.display_name if song.requester else "?"
                except Exception:
                    req = "?"
                title_short = song.title[:50] + "..." if len(song.title) > 50 else song.title
                lines.append(f"`{i}.` **{title_short}** — `{song.duration_str}`")
            total_duration = int(sum(s.duration for s in queue_list))
            total_min = total_duration // 60
            total_sec = total_duration % 60
            field_value = "\n".join(lines)
            if len(field_value) > 1024:
                field_value = field_value[:1021] + "..."
            embed.add_field(
                name=f"📋 Songs {start + 1}–{end} von {total} ({total_min}:{total_sec:02d})",
                value=field_value,
                inline=False,
            )

        embed.set_footer(
            text=f"Seite {self.page + 1}/{self.max_page + 1} • Autoplay: {'✅' if self.state.autoplay else '❌'}"
        )
        return embed

    @discord.ui.button(label="◀️ Zurück", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self.max_page = max(0, (len(self.state.queue) - 1) // QUEUE_PAGE_SIZE)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="▶️ Weiter", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.max_page = max(0, (len(self.state.queue) - 1) // QUEUE_PAGE_SIZE)
        self.page = min(self.max_page, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def on_timeout(self):
        pass


# ── Search Result Select View ────────────────────────────

class SearchResultSelectView(discord.ui.View):
    def __init__(self, results: list[dict], requester: discord.Member, guild_id: int):
        super().__init__(timeout=30)
        self.results = results
        self.requester = requester
        self.guild_id = guild_id

        options = []
        for i, entry in enumerate(results[:10]):
            title = entry.get('title', 'Unbekannt')[:100]
            dur = int(entry.get('duration', 0)) if entry.get('duration') else 0
            m, s = divmod(dur, 60)
            dur_str = f"{m}:{s:02d}" if dur > 0 else "?"
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    description=f"Dauer: {dur_str}",
                    value=str(i),
                )
            )

        self.select = discord.ui.Select(
            placeholder="🎵 Song auswählen...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        entry = self.results[idx]
        title = entry.get('title', 'Unbekannt')
        webpage_url = _get_reference_url(entry)
        duration = int(entry.get('duration', 0)) if entry.get('duration') else 0
        thumbnail = _entry_thumbnail(entry)
        song = SongInfo(title, '', webpage_url, duration, thumbnail, self.requester)
        state = get_music_state(self.guild_id, d_bot)

        if not self.requester.voice or not self.requester.voice.channel:
            await interaction.response.edit_message(
                content="❌ Du musst in einem Voice-Kanal sein!", view=None
            )
            return

        user_vc = self.requester.voice.channel
        if state.voice_client is None or not state.voice_client.is_connected():
            try:
                perms = user_vc.permissions_for(interaction.guild.me)
                if not perms.connect or not perms.speak:
                    await interaction.response.edit_message(
                        content=f"❌ Keine Berechtigung für **{user_vc.name}**!", view=None
                    )
                    return
                state.voice_client = await user_vc.connect(self_deaf=True)
            except Exception as e:
                await interaction.response.edit_message(
                    content=f"❌ Konnte nicht beitreten: {e}", view=None
                )
                return

        state.queue.append(song)
        state.reset_idle()

        if not state.voice_client.is_playing() and state.current is None:
            await interaction.response.edit_message(
                content=f"🎵 **{title}** wird geladen...", view=None
            )
            await play_next(state, interaction.channel)
        else:
            pos = len(state.queue)
            embed = discord.Embed(
                title="➕ Zur Warteschlange hinzugefügt",
                description=f"[{title}]({webpage_url})",
                color=COLOR_MUSIC,
            )
            embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
            embed.add_field(name="📋 Position", value=f"#{pos}", inline=True)
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            await interaction.response.edit_message(content=None, embed=embed, view=None)

    async def on_timeout(self):
        pass

### MUSIC MODULE END ###


# ══════════════════════════════════════════════════════════
#                     TWITCH BOT
# ══════════════════════════════════════════════════════════

class TwitchBot(t_commands.Bot):

    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            client_id=TWITCH_CLIENT_ID,
            client_secret=TWITCH_CLIENT_SECRET,
            prefix='!',
            initial_channels=(
                [STREAMER_CHANNEL] if STREAMER_CHANNEL else []
            ),
        )

    async def event_ready(self):
        global twitch_bot_ref
        twitch_bot_ref = self
        print("")
        print("═══════════════════════════════════")
        print("        TWITCH BOT STATUS")
        print("═══════════════════════════════════")
        print(f"  ✅ Eingeloggt als: {self.nick}")
        channels = [c.name for c in self.connected_channels]
        print(f"  📺 Kanäle: {channels}")
        print(
            f"  📋 Mod-Logger: "
            f"{'✅ Aktiv' if TWITCH_LOG_CHANNEL_ID else '❌ Deaktiviert'}"
        )
        print("═══════════════════════════════════")
        print("")

    async def event_message(self, message):
        if message.echo or not message.author:
            return
        chatter = message.author.name.lower()

        # Nur loggen wenn Debug nötig (reduziert Spam)
        # print(f"💬 [Twitch] {message.author.name}: {message.content}")

        data = load_data()
        today = str(datetime.date.today())
        now_hour = datetime.datetime.now(datetime.timezone.utc).hour
        changed = False

        for discord_id, info in data["users"].items():
            if info.get("twitch_name", "").lower() == chatter:
                # ── Anwesenheits-Tracking ──────────────────────────
                # Stelle sicher dass der Stream-Tag existiert
                if today not in data["streams"]:
                    data["streams"][today] = []

                # discord_id ist ein String (JSON-Key), Vergleich muss konsistent sein
                if discord_id not in data["streams"][today]:
                    data["streams"][today].append(discord_id)
                    print(f"  ⭐ [Twitch] {message.author.name} → erfasst für {today}")
                    changed = True

                # ── Nachrichten-Tracking ───────────────────────────
                data.setdefault("message_counts", {})
                data["message_counts"].setdefault(today, {})
                data["message_counts"][today].setdefault(discord_id, {"count": 0, "hours": {}})

                data["message_counts"][today][discord_id]["count"] += 1
                hour_key = str(now_hour)
                data["message_counts"][today][discord_id]["hours"][hour_key] = (
                    data["message_counts"][today][discord_id]["hours"].get(hour_key, 0) + 1
                )
                changed = True

                # Dashboard-Tracking: Nachricht im Discord-Stats-System
                try:
                    stats_tracker.add_message(int(discord_id), info.get("display_name", message.author.name))
                except Exception:
                    pass

                break

        if changed:
            save_data(data)

        await self.handle_commands(message)

# ══════════════════════════════════════════════════════════
#        TWITCH API – REWARDS & LIVE CHECK
# ══════════════════════════════════════════════════════════

_twitch_app_token: str | None = None
_twitch_app_token_expires: float = 0


async def get_twitch_app_token() -> str | None:
    """Holt einen App-Access-Token von Twitch."""
    global _twitch_app_token, _twitch_app_token_expires

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if _twitch_app_token and now < _twitch_app_token_expires:
        return _twitch_app_token

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return None

    try:
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _twitch_app_token = data["access_token"]
                    _twitch_app_token_expires = now + data.get("expires_in", 3600) - 60
                    return _twitch_app_token
    except Exception as e:
        print(f"❌ Twitch App-Token Fehler: {e}")

    return None


async def check_streamer_live() -> bool:
    """Prüft ob der Streamer gerade live ist."""
    if not STREAMER_CHANNEL:
        return False

    token = await get_twitch_app_token()
    if not token:
        return False

    try:
        url = f"https://api.twitch.tv/helix/streams?user_login={STREAMER_CHANNEL}"
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return len(data.get("data", [])) > 0
    except Exception as e:
        print(f"❌ Live-Check Fehler: {e}")

    return False


async def fetch_channel_rewards() -> list[dict]:
    """Holt alle Kanalpunkt-Belohnungen des Streamers."""
    if not STREAMER_USER_ID or not STREAMER_OAUTH_TOKEN:
        print("⚠️ STREAMER_USER_ID oder STREAMER_OAUTH_TOKEN fehlt!")
        return []

    try:
        url = (
            f"https://api.twitch.tv/helix/channel_points/custom_rewards"
            f"?broadcaster_id={STREAMER_USER_ID}"
        )
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {STREAMER_OAUTH_TOKEN}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rewards = []
                    for r in data.get("data", []):
                        if not r.get("is_enabled", False):
                            continue
                        rewards.append({
                            "id": r["id"],
                            "title": r["title"],
                            "cost": r["cost"],
                            "prompt": r.get("prompt", ""),
                            "is_paused": r.get("is_paused", False),
                            "is_in_stock": r.get("is_in_stock", True),
                            "color": r.get("background_color", "#9146FF"),
                            "image": (
                                r.get("image", {}).get("url_4x", "")
                                if r.get("image") else
                                r.get("default_image", {}).get("url_4x", "")
                            ),
                        })
                    print(f"✅ {len(rewards)} Twitch-Rewards geladen")
                    return rewards
                else:
                    error_text = await resp.text()
                    print(f"❌ Rewards API Fehler ({resp.status}): {error_text}")

                    # Token abgelaufen
                    if resp.status == 401:
                        print(
                            "⚠️ STREAMER_OAUTH_TOKEN ist abgelaufen oder ungültig!\n"
                            "   Generiere einen neuen Token und trage ihn in die .env ein."
                        )

    except Exception as e:
        print(f"❌ Fehler beim Laden der Rewards: {e}")

    return []

# Cache für Rewards (wird alle 5 Minuten aktualisiert)
_rewards_cache: list[dict] = []
_rewards_cache_time: float = 0
REWARDS_CACHE_DURATION = 300  # 5 Minuten


async def get_cached_rewards() -> list[dict]:
    """Gibt gecachte Rewards zurück, aktualisiert bei Bedarf."""
    global _rewards_cache, _rewards_cache_time

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if _rewards_cache and now - _rewards_cache_time < REWARDS_CACHE_DURATION:
        return _rewards_cache

    rewards = await fetch_channel_rewards()
    if rewards:
        _rewards_cache = rewards
        _rewards_cache_time = now

    return _rewards_cache

    # ══════════════════════════════════════════════════════
    #           TWITCH MOD EVENTS
    # ══════════════════════════════════════════════════════

    async def event_ban(self, event):
        """Wird ausgelöst wenn ein User gebannt wird."""
        try:
            if not TWITCH_LOG_CHANNEL_ID:
                return
            print(
                f"🔨 [Twitch MOD] Ban: {event.user_login} "
                f"von {event.moderator_user_login}"
            )
            embed = build_twitch_mod_embed(
                action="ban",
                moderator=event.moderator_user_login,
                target_user=event.user_login,
                reason=getattr(event, "reason", None),
                channel=STREAMER_CHANNEL,
            )
            await send_twitch_mod_log(embed)
            dashboard_state.add_mod_action(
                action="ban", target=event.user_login,
                moderator=event.moderator_user_login,
                reason=getattr(event, "reason", "") or "",
            )
        except Exception as e:
            print(f"Fehler in event_ban: {e}")

    async def event_unban(self, event):
        """Wird ausgelöst wenn ein User entbannt wird."""
        try:
            if not TWITCH_LOG_CHANNEL_ID:
                return
            print(
                f"✅ [Twitch MOD] Unban: {event.user_login} "
                f"von {event.moderator_user_login}"
            )
            embed = build_twitch_mod_embed(
                action="unban",
                moderator=event.moderator_user_login,
                target_user=event.user_login,
                channel=STREAMER_CHANNEL,
            )
            await send_twitch_mod_log(embed)
            dashboard_state.add_mod_action(
                action="unban", target=event.user_login,
                moderator=event.moderator_user_login,
            )
        except Exception as e:
            print(f"Fehler in event_unban: {e}")

    async def event_timeout(self, event):
        """Wird ausgelöst wenn ein User getimeoutet wird."""
        try:
            if not TWITCH_LOG_CHANNEL_ID:
                return
            duration = getattr(event, "duration", None)
            print(
                f"⏱️ [Twitch MOD] Timeout: {event.user_login} "
                f"für {duration}s von {event.moderator_user_login}"
            )
            embed = build_twitch_mod_embed(
                action="timeout",
                moderator=event.moderator_user_login,
                target_user=event.user_login,
                reason=getattr(event, "reason", None),
                duration=duration,
                channel=STREAMER_CHANNEL,
            )
            await send_twitch_mod_log(embed)
            dashboard_state.add_mod_action(
                action="timeout", target=event.user_login,
                moderator=event.moderator_user_login,
                reason=getattr(event, "reason", "") or "",
                duration=duration,
            )
        except Exception as e:
            print(f"Fehler in event_timeout: {e}")

    async def event_untimeout(self, event):
        """Wird ausgelöst wenn ein Timeout aufgehoben wird."""
        try:
            if not TWITCH_LOG_CHANNEL_ID:
                return
            print(
                f"✅ [Twitch MOD] Untimeout: {event.user_login} "
                f"von {event.moderator_user_login}"
            )
            embed = build_twitch_mod_embed(
                action="untimeout",
                moderator=event.moderator_user_login,
                target_user=event.user_login,
                channel=STREAMER_CHANNEL,
            )
            await send_twitch_mod_log(embed)
            dashboard_state.add_mod_action(
                action="untimeout", target=event.user_login,
                moderator=event.moderator_user_login,
            )
        except Exception as e:
            print(f"Fehler in event_untimeout: {e}")

    async def event_message_delete(self, event):
        """Wird ausgelöst wenn eine Nachricht gelöscht wird."""
        try:
            if not TWITCH_LOG_CHANNEL_ID:
                return
            deleted_msg = getattr(event, "message_body", None)
            target = getattr(event, "target_user_login", "Unbekannt")
            mod = getattr(event, "moderator_user_login", "Unbekannt")
            print(
                f"🗑️ [Twitch MOD] Nachricht gelöscht von {target} "
                f"durch {mod}"
            )
            embed = build_twitch_mod_embed(
                action="delete",
                moderator=mod,
                target_user=target,
                deleted_message=deleted_msg,
                channel=STREAMER_CHANNEL,
            )
            await send_twitch_mod_log(embed)
            dashboard_state.add_mod_action(
                action="delete", target=target,
                moderator=mod,
                reason=str(deleted_msg or "")[:200],
            )
        except Exception as e:
            print(f"Fehler in event_message_delete: {e}")

    async def event_warn(self, event):
        """Wird ausgelöst wenn ein User verwarnt wird."""
        try:
            if not TWITCH_LOG_CHANNEL_ID:
                return
            print(
                f"⚠️ [Twitch MOD] Warn: {event.user_login} "
                f"von {event.moderator_user_login}"
            )
            embed = build_twitch_mod_embed(
                action="warn",
                moderator=event.moderator_user_login,
                target_user=event.user_login,
                reason=getattr(event, "reason", None),
                channel=STREAMER_CHANNEL,
            )
            await send_twitch_mod_log(embed)
            dashboard_state.add_mod_action(
                action="warn", target=event.user_login,
                moderator=event.moderator_user_login,
                reason=getattr(event, "reason", "") or "",
            )
        except Exception as e:
            print(f"Fehler in event_warn: {e}")

    # ══════════════════════════════════════════════════════
    #           TWITCH CHAT COMMANDS
    # ══════════════════════════════════════════════════════

    @t_commands.command(name="ping")
    async def cmd_ping(self, ctx: t_commands.Context):
        await ctx.send("🏓 Pong! Bot ist aktiv.")

    @t_commands.command(name="bot")
    async def cmd_bot(self, ctx: t_commands.Context):
        data = load_data()
        await ctx.send(
            f"🤖 Activity-Tracker | {len(data['users'])} User registriert"
        )

    @t_commands.command(name="stats")
    async def cmd_stats(self, ctx: t_commands.Context):
        data = load_data()
        chatter = ctx.author.name.lower()
        for uid, info in data["users"].items():
            if info.get("twitch_name", "").lower() == chatter:
                total = len(data["streams"])
                present = sum(
                    1 for d in data["streams"]
                    if uid in data["streams"][d]
                )
                pct = (present / total * 100) if total > 0 else 0
                await ctx.send(
                    f"📊 {ctx.author.name}: "
                    f"{present}/{total} Streams ({pct:.0f}%)"
                )
                return
        await ctx.send(f"❌ {ctx.author.name} ist nicht registriert.")


# ══════════════════════════════════════════════════════════
#         REVIEW SCHEDULER (Monatlich + Jährlich)
# ══════════════════════════════════════════════════════════

@tasks.loop(minutes=1)
async def review_scheduler():
    """Prüft jede Minute ob ein Review fällig ist."""
    if not REVIEW_CHANNEL_ID:
        return

    # Deutschland-Zeit (CET/CEST)
    try:
        import zoneinfo
        de_tz = zoneinfo.ZoneInfo("Europe/Berlin")
    except ImportError:
        de_tz = datetime.timezone(datetime.timedelta(hours=1))

    now_de = datetime.datetime.now(de_tz)
    today = now_de.date()
    last_day = calendar.monthrange(today.year, today.month)[1]

    # Monatsbericht: Letzter Tag des Monats um 22:00 DE-Zeit
    if today.day == last_day and now_de.hour == 22 and now_de.minute == 0:
        try:
            channel = d_bot.get_channel(REVIEW_CHANNEL_ID)
            if channel:
                await send_monthly_review(channel, channel.guild, today.year, today.month)
        except Exception as e:
            print(f"❌ [Review] Monatsbericht Fehler: {e}")

    # Jahresbericht: 1. Dezember um 22:00 DE-Zeit
    if today.month == 12 and today.day == 1 and now_de.hour == 22 and now_de.minute == 0:
        try:
            channel = d_bot.get_channel(REVIEW_CHANNEL_ID)
            if channel:
                # Jahresbericht für das aktuelle Jahr
                await send_yearly_review(channel, channel.guild, today.year)
        except Exception as e:
            print(f"❌ [Review] Jahresbericht Fehler: {e}")


@review_scheduler.before_loop
async def before_review_scheduler():
    await d_bot.wait_until_ready()
    print("✅ [Review] Scheduler gestartet – Monatlich letzter Tag 22:00 DE / Jährlich 1. Dez 22:00 DE")


# ── Stream-Erkennung: Live-Check alle 5 Minuten ─────────────────
_stream_was_live: bool = False


@tasks.loop(minutes=5)
async def stream_live_tracker():
    """
    Prüft alle 5 Minuten ob der Streamer live ist.
    Wenn ja → trägt den heutigen Tag in data["streams"] ein,
    damit die Anwesenheit korrekt erfasst wird.
    Diese Funktion ist UNABHÄNGIG vom Twitch-Chat-Tracking.
    """
    global _stream_was_live
    if not STREAMER_CHANNEL:
        return

    is_live = await check_streamer_live()

    if is_live:
        today = str(datetime.date.today())
        data = load_data()

        # Tag in streams eintragen falls noch nicht vorhanden
        if today not in data["streams"]:
            data["streams"][today] = []
            save_data(data)
            print(f"📺 [StreamTracker] Neuer Stream-Tag erkannt: {today}")

        if not _stream_was_live:
            _stream_was_live = True
            print(f"🟢 [StreamTracker] Streamer ist LIVE – Tag {today} erfasst")
    else:
        if _stream_was_live:
            _stream_was_live = False
            print("🔴 [StreamTracker] Streamer ist OFFLINE")


@stream_live_tracker.before_loop
async def before_stream_live_tracker():
    await d_bot.wait_until_ready()
    print("✅ [StreamTracker] Stream-Erkennungs-Loop gestartet (alle 5 Min.)")


# ── Clip-Tracker: Neue Twitch-Clips erkennen und posten ─────────
_known_clip_ids: set[str] = set()
_clip_tracker_initialized: bool = False


async def _fetch_recent_clips(limit: int = 20) -> list[dict]:
    """Holt die neuesten Clips des Streamers über die Twitch-API."""
    if not STREAMER_USER_ID or not TWITCH_CLIENT_ID:
        return []

    token = await get_twitch_app_token()
    if not token:
        return []

    try:
        url = (
            f"https://api.twitch.tv/helix/clips"
            f"?broadcaster_id={STREAMER_USER_ID}"
            f"&first={limit}"
        )
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
                else:
                    print(f"⚠️ [ClipTracker] API Fehler: {resp.status}")
    except Exception as e:
        print(f"❌ [ClipTracker] Fehler: {e}")

    return []


@tasks.loop(minutes=2)
async def clip_tracker():
    """
    Prüft alle 2 Minuten ob neue Clips beim Streamer erstellt wurden.
    Neue Clips werden automatisch in CLIPS_CHANNEL_ID gepostet.
    """
    global _known_clip_ids, _clip_tracker_initialized

    if not CLIPS_CHANNEL_ID or not STREAMER_USER_ID:
        return

    clips = await _fetch_recent_clips(limit=20)
    if not clips:
        return

    if not _clip_tracker_initialized:
        # Beim ersten Start alle vorhandenen Clips merken, aber NICHT posten
        for clip in clips:
            _known_clip_ids.add(clip["id"])
        _clip_tracker_initialized = True
        print(f"✅ [ClipTracker] Initialisiert mit {len(_known_clip_ids)} bekannten Clips")
        return

    # Neue Clips finden (die noch nicht in _known_clip_ids sind)
    new_clips = [c for c in clips if c["id"] not in _known_clip_ids]

    for clip in reversed(new_clips):  # älteste zuerst posten
        _known_clip_ids.add(clip["id"])

        # Clip-Daten aufbereiten
        clip_title    = clip.get("title", "Unbekannter Clip")
        clip_url      = clip.get("url", "")
        clip_creator  = clip.get("creator_name", "Unbekannt")
        clip_thumb    = clip.get("thumbnail_url", "")
        clip_duration = clip.get("duration", 0)
        clip_views    = clip.get("view_count", 0)
        created_at    = clip.get("created_at", "")

        # Zeitstempel parsen
        try:
            created_dt = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            created_ts = int(created_dt.timestamp())
            created_display = f"<t:{created_ts}:R>"
        except Exception:
            created_display = created_at[:10] if created_at else "Unbekannt"

        print(f"🎬 [ClipTracker] Neuer Clip von {clip_creator}: {clip_title}")

        # Embed erstellen
        embed = discord.Embed(
            title=f"🎬 {clip_title}",
            url=clip_url,
            color=0x9146FF,  # Twitch-Lila
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        embed.add_field(
            name="👤 Erstellt von",
            value=f"[{clip_creator}](https://twitch.tv/{clip_creator.lower()})",
            inline=True,
        )
        embed.add_field(
            name="🕐 Erstellt",
            value=created_display,
            inline=True,
        )
        embed.add_field(
            name="⏱️ Länge",
            value=f"{clip_duration:.0f}s",
            inline=True,
        )
        if clip_views > 0:
            embed.add_field(
                name="👁️ Views",
                value=str(clip_views),
                inline=True,
            )

        embed.add_field(
            name="📺 Streamer",
            value=f"[{STREAMER_CHANNEL}](https://twitch.tv/{STREAMER_CHANNEL})",
            inline=True,
        )

        if clip_thumb:
            # Thumbnail-URL bereinigen (Twitch gibt manchmal Platzhalter zurück)
            thumb_clean = clip_thumb.replace("%{width}", "1280").replace("%{height}", "720")
            embed.set_image(url=thumb_clean)

        embed.set_footer(text="Twitch Clips • ByGorgii Community")

        # In Clips-Kanal senden
        for guild in d_bot.guilds:
            clips_channel = guild.get_channel(CLIPS_CHANNEL_ID)
            if clips_channel:
                try:
                    await clips_channel.send(
                        content=f"🎬 **Neuer Clip!** [{clip_title}]({clip_url})",
                        embed=embed,
                    )
                except Exception as e:
                    print(f"❌ [ClipTracker] Senden fehlgeschlagen: {e}")
                break


@clip_tracker.before_loop
async def before_clip_tracker():
    await d_bot.wait_until_ready()
    print("✅ [ClipTracker] Clip-Erkennungs-Loop gestartet (alle 2 Min.)")


### MUSIC MODULE START ###
# ══════════════════════════════════════════════════════════
#         MUSIK-MODUL – IDLE TIMER TASK
# ══════════════════════════════════════════════════════════

@tasks.loop(seconds=1)
async def music_idle_checker():
    """Prüft jede Sekunde ob Musik-Bots idle sind oder der VC leer ist."""
    if not MUSIC_ENABLED:
        return

    to_cleanup = []

    for guild_id, state in list(music_states.items()):
        if not state.voice_client or not state.voice_client.is_connected():
            continue

        vc = state.voice_client.channel
        if not vc:
            to_cleanup.append(guild_id)
            continue

        # Prüfe ob der VC leer ist (nur der Bot drin)
        human_members = [m for m in vc.members if not m.bot]
        if not human_members:
            print(f"🎵 [Music] VC leer in Guild {guild_id} – verlasse Kanal")
            to_cleanup.append(guild_id)
            continue

        # Idle-Timer: Nur zählen wenn WIRKLICH nichts läuft –
        # weder spielt noch wartet noch wird gerade ein Song
        # aufgelöst (dauert auf dem Raspberry Pi manchmal lange)
        if (not state.voice_client.is_playing()
                and not state.voice_client.is_paused()
                and state.current is None
                and not state.queue):
            state.increment_idle()
            if state.is_idle_timeout:
                print(f"🎵 [Music] Idle-Timeout in Guild {guild_id} ({MUSIC_IDLE_TIMEOUT}s) – verlasse Kanal")
                to_cleanup.append(guild_id)
        else:
            state.reset_idle()

    for guild_id in to_cleanup:
        state = music_states.get(guild_id)
        if state:
            if state.voice_client and state.voice_client.channel:
                guild = d_bot.get_guild(guild_id)
                if guild:
                    for ch in guild.text_channels:
                        try:
                            await ch.send(
                                "👋 Ich verlasse den Voice-Kanal wegen Inaktivität.",
                                delete_after=15,
                            )
                            break
                        except (discord.Forbidden, discord.HTTPException):
                            continue
            await state.cleanup()


@music_idle_checker.before_loop
async def before_music_idle_checker():
    await d_bot.wait_until_ready()
    if MUSIC_ENABLED:
        print("✅ [Music] Idle-Checker gestartet")

### MUSIC MODULE END ###

# ══════════════════════════════════════════════════════════
#                    DISCORD EVENTS
# ══════════════════════════════════════════════════════════

@d_bot.event
async def on_ready():
    global discord_bot_ref
    discord_bot_ref = d_bot
    print("")
    print("═══════════════════════════════════")
    print("       DISCORD BOT STATUS")
    print("═══════════════════════════════════")
    print(f"  ✅ Bot: {d_bot.user} (ID: {d_bot.user.id})")
    print(f"  📡 Server: {len(d_bot.guilds)}")
    print(f"  🤖 AI: {'✅ Aktiv' if ai_enabled else '❌ Deaktiviert'}")
    print(f"  🔑 API Keys: {len(OPENROUTER_KEYS)}")
    print(f"  📝 Log-Kanal: {LOG_CHANNEL_ID}")
    print(
        f"  📋 Twitch-Mod-Log: "
        f"{'✅ ' + str(TWITCH_LOG_CHANNEL_ID) if TWITCH_LOG_CHANNEL_ID else '❌ Deaktiviert'}"
    )
    print(
        f"  🎙️ Temp-Voice: "
        f"{'✅ Kanal-ID: ' + str(TEMP_VOICE_CHANNEL_ID) if TEMP_VOICE_CHANNEL_ID else '❌ Deaktiviert'}"
    )
    print(
        f"  📅 Review: "
        f"{'✅ Kanal-ID: ' + str(REVIEW_CHANNEL_ID) if REVIEW_CHANNEL_ID else '❌ Deaktiviert'}"
    )
    ### MUSIC MODULE START ###
    print(
        f"  🎵 Musik-Modul: "
        f"{'✅ Aktiv' if MUSIC_ENABLED else '❌ Deaktiviert'}"
    )
    ### MUSIC MODULE END ###
    print(f"  👑 Admin Users: {len(ADMIN_USER_IDS)} konfiguriert")
    print(
        f"  🎯 Twitch Rewards: "
        f"{'✅ Streamer-ID: ' + STREAMER_USER_ID if STREAMER_USER_ID else '❌ Keine STREAMER_USER_ID'}"
    )
    if PROXYCHECK_API_KEY:
        print("  🛡️ ProxyCheck.io: ✅ Aktiv")
    if MESSAGE_LOG_CHANNEL_ID:
        print(f"  📝 Message-Log: ✅ Kanal {MESSAGE_LOG_CHANNEL_ID}")

    print("═══════════════════════════════════")
    print("")

    # ── Temp Voice Channels nach Neustart wiederherstellen ──
    if TEMP_VOICE_CHANNEL_ID:
        for guild in d_bot.guilds:
            trigger_channel = guild.get_channel(TEMP_VOICE_CHANNEL_ID)
            if not trigger_channel:
                continue
            category = trigger_channel.category
            if not category:
                continue
            for vc in category.voice_channels:
                # Überspringe den Trigger-Kanal selbst
                if vc.id == TEMP_VOICE_CHANNEL_ID:
                    continue
                # Prüfe ob es ein Temp-Kanal ist (erstellt vom Bot)
                if vc.id in temp_voice_channels:
                    continue  # Bereits bekannt
                # Wenn der Kanal leer ist → sofort löschen
                if len(vc.members) == 0:
                    try:
                        await vc.delete(reason="Temp Voice: Leerer Kanal nach Bot-Neustart")
                        print(f"🗑️ [TempVC] Leeren Kanal nach Neustart gelöscht: {vc.name}")
                    except Exception as e:
                        print(f"⚠️ [TempVC] Konnte leeren Kanal nicht löschen: {e}")
                    # Zugehörigen Text-Kanal suchen und löschen
                    for tc in category.text_channels:
                        if tc.topic and str(vc.id) in tc.topic:
                            try:
                                await tc.delete(reason="Temp Voice: Leerer Kanal nach Bot-Neustart")
                                print(f"🗑️ [TempVC] Zugehörigen Text-Kanal gelöscht: {tc.name}")
                            except Exception:
                                pass
                            break
                else:
                    # Kanal hat Mitglieder → wieder registrieren
                    owner = vc.members[0]  # Ersten Member als Owner setzen
                    # Suche zugehörigen Text-Kanal
                    text_channel_id = 0
                    control_message_id = 0
                    for tc in category.text_channels:
                        if tc.topic and str(vc.id) in tc.topic:
                            text_channel_id = tc.id
                            # Versuche die gepinnte Control-Message zu finden
                            try:
                                pins = await tc.pins()
                                if pins:
                                    control_message_id = pins[0].id
                            except Exception:
                                pass
                            break

                    temp_voice_channels[vc.id] = {
                        "owner_id": owner.id,
                        "text_channel_id": text_channel_id,
                        "control_message_id": control_message_id,
                        "allowed_users": set(),
                        "locked": False,
                        "limit": vc.user_limit,
                    }
                    # Control-View neu registrieren, damit die Panel-Buttons
                    # (Umbenennen/Limit/Bitrate/...) nach Neustart wieder gehen.
                    # Mit Message-ID, damit jeder VC seine eigene View hat!
                    try:
                        vc_view = VoiceChannelControlView(owner.id, vc.id)
                        d_bot.add_view(
                            vc_view,
                            message_id=control_message_id or None,
                        )
                    except Exception as e:
                        print(f"⚠️ [TempVC] View-Registrierung fehlgeschlagen: {e}")
                    print(f"♻️ [TempVC] Kanal wiederhergestellt nach Neustart: {vc.name} (Owner: {owner.display_name})")

        # Auch Text-Kanäle ohne zugehörigen VC aufräumen
        if category:
            for tc in category.text_channels:
                if tc.topic and "Voice:" in tc.topic:
                    # Prüfe ob der referenzierte VC noch existiert
                    has_vc = False
                    for vc in category.voice_channels:
                        if str(vc.id) in (tc.topic or ""):
                            has_vc = True
                            break
                    if not has_vc:
                        try:
                            await tc.delete(reason="Temp Voice: Verwaister Text-Kanal nach Neustart")
                            print(f"🗑️ [TempVC] Verwaisten Text-Kanal gelöscht: {tc.name}")
                        except Exception:
                            pass

    recovered = len(temp_voice_channels)
    if recovered > 0:
        print(f"✅ [TempVC] {recovered} Temp-Kanäle nach Neustart wiederhergestellt")

    # Tickets nach Neustart wiederherstellen
    ticket_data = load_tickets()
    restored_tickets = 0
    for ch_id_str, ticket in ticket_data.get("tickets", {}).items():
        try:
            ch_id = int(ch_id_str)
        except (TypeError, ValueError):
            continue
        if ticket.get("status") in ("open", "claimed"):
            for guild in d_bot.guilds:
                channel = guild.get_channel(ch_id)
                if channel:
                    open_tickets[ch_id] = ticket
                    # WICHTIG: View neu registrieren, sonst funktionieren
                    # die Buttons (Claim/Schließen/Transcript) nach dem
                    # Neustart nicht!
                    msg_id = int(ticket.get("ticket_message_id") or 0)
                    if not msg_id:
                        # Alte Tickets ohne gespeicherte Message-ID →
                        # Ticket-Message (mit Claim-Button) im Kanal suchen
                        msg_id = await find_ticket_message_id(guild, ch_id)
                    register_ticket_view(
                        ch_id,
                        message_id=msg_id or None,
                        claimed_by_name=ticket.get("claimed_by_name", "") or "",
                    )
                    restored_tickets += 1
                    break
    # Verwaiste Ticket-Einträge (Kanal nicht mehr da) aus dem JSON streichen
    if ticket_data.get("tickets"):
        stale = []
        for k in ticket_data["tickets"]:
            try:
                if int(k) not in open_tickets:
                    stale.append(k)
            except (TypeError, ValueError):
                stale.append(k)
        if stale:
            for k in stale:
                ticket_data["tickets"].pop(k, None)
            save_tickets(ticket_data)
            print(f"🧹 [Tickets] {len(stale)} verwaiste Ticket-Einträge aufgeräumt")
    if restored_tickets:
        print(f"✅ [Tickets] {restored_tickets} Tickets nach Neustart wiederhergestellt")

    # Weekly Scheduler starten
    if REVIEW_CHANNEL_ID and not review_scheduler.is_running():
        review_scheduler.start()

    # Stream-Tracker immer starten wenn Streamer-Kanal konfiguriert
    if STREAMER_CHANNEL and not stream_live_tracker.is_running():
        stream_live_tracker.start()

    # Clip-Tracker starten
    if CLIPS_CHANNEL_ID and STREAMER_USER_ID and not clip_tracker.is_running():
        clip_tracker.start()

    ### MUSIC MODULE START ###
    if MUSIC_ENABLED and not music_idle_checker.is_running():
        music_idle_checker.start()
    ### MUSIC MODULE END ###

async def _ensure_admin_access(guild: discord.Guild, channel_id: int):
    """ADMIN_USER_IDS bekommen immer Zugriff auf Temp-VCs."""
    vc_data = temp_voice_channels.get(channel_id)
    if not vc_data:
        return
    voice_channel = guild.get_channel(channel_id)
    text_channel  = guild.get_channel(vc_data.get("text_channel_id", 0))
    if not voice_channel:
        return
    for admin_id in ADMIN_USER_IDS:
        admin = guild.get_member(admin_id)
        if not admin:
            continue
        current = voice_channel.permissions_for(admin)
        if not current.connect or not current.manage_channels:
            try:
                await voice_channel.set_permissions(
                    admin,
                    connect=True, speak=True, view_channel=True,
                    manage_channels=True, move_members=True,
                    mute_members=True, deafen_members=True,
                )
                if text_channel:
                    await text_channel.set_permissions(
                        admin,
                        read_messages=True, send_messages=True,
                        manage_messages=True,
                    )
            except Exception:
                pass


@d_bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """
    BUG-FIX:  Mute/Deaf löst KEINEN Owner-Transfer aus.
    NEU:      Owner-Recovery (10 Min. Fenster).
    NEU:      ADMIN_USER_IDS haben immer Kanalzugriff.
    NEU:      Voice-Zeit-Tracking für Dashboard.
    """

    # ── Musik-Cleanup: Bot disconnected ──────────────────
    if member.id == d_bot.user.id and before.channel is not None and after.channel is None:
        guild_id = member.guild.id
        if guild_id in music_states:
            state = music_states[guild_id]
            print(f"🎵 [Music] Bot disconnected in Guild {guild_id} – Cleanup")
            state.queue.clear()
            state.current = None
            state._idle_seconds = 0
            state.history.clear()
            state.voice_client = None
            try:
                await d_bot.change_presence(activity=None)
            except Exception:
                pass
            dashboard_state.update_now_playing(None)

    # ── BUG-FIX: Nur Audio-State geändert → nichts tun ───
    if is_only_voice_state_change(before, after):
        return

    # ── Voice-Zeit Tracking (alle User) ──────────────────
    if not member.bot:
        channel_changed = (
            (before.channel is None and after.channel is not None) or
            (before.channel is not None and after.channel is None) or
            (before.channel is not None and after.channel is not None
             and before.channel.id != after.channel.id)
        )
        if channel_changed:
            try:
                if before.channel is not None:
                    dashboard_state.voice_leave(
                        member.id, member.display_name, before.channel.name
                    )
                if after.channel is not None:
                    dashboard_state.voice_join(
                        member.id, member.display_name, after.channel.name
                    )
            except Exception:
                pass

    if not TEMP_VOICE_CHANNEL_ID:
        return

    # ── User betritt Trigger-Kanal ────────────────────────
    if (
        after.channel is not None
        and after.channel.id == TEMP_VOICE_CHANNEL_ID
    ):
        await create_temp_voice_channel(member, member.guild)
        return

    # ── User betritt existierenden Temp-Kanal ────────────
    if (
        after.channel is not None
        and after.channel.id in temp_voice_channels
        and after.channel.id != TEMP_VOICE_CHANNEL_ID
    ):
        channel_id = after.channel.id
        vc_data = temp_voice_channels.get(channel_id)

        # Owner-Recovery prüfen
        if vc_data and check_owner_recovery(channel_id, member.id):
            clear_owner_recovery(channel_id)
            current_owner_id = vc_data.get("owner_id")
            voice_channel    = member.guild.get_channel(channel_id)
            current_owner    = member.guild.get_member(current_owner_id) if current_owner_id else None
            try:
                await voice_channel.set_permissions(
                    member,
                    manage_channels=True, connect=True, speak=True,
                    move_members=True, mute_members=True, deafen_members=True,
                )
                if current_owner and current_owner.id != member.id:
                    await voice_channel.set_permissions(
                        current_owner,
                        manage_channels=False, move_members=False,
                        mute_members=False, deafen_members=False,
                        connect=True, speak=True,
                    )
                    text_ch = member.guild.get_channel(vc_data.get("text_channel_id", 0))
                    if text_ch:
                        await text_ch.set_permissions(
                            member, read_messages=True,
                            send_messages=True, manage_messages=True,
                        )
                        await text_ch.set_permissions(
                            current_owner, read_messages=True,
                            send_messages=True, manage_messages=False,
                        )
                        await text_ch.send(
                            f"👑 **{member.mention}** ist zurückgekehrt und hat den "
                            f"Admin-Rang zurückbekommen!"
                        )
                vc_data["owner_id"] = member.id
                print(f"👑 [TempVC] Owner-Recovery: {member.display_name}")
                dashboard_state.push_event_sync("vc_owner_recovery", {
                    "channel_id": channel_id, "username": member.display_name,
                })
            except Exception as e:
                print(f"❌ [TempVC] Owner-Recovery Fehler: {e}")

        await _ensure_admin_access(member.guild, channel_id)
        await update_temp_voice_panel(member.guild, channel_id)
        dashboard_state.update_temp_vcs(temp_voice_channels)
        return

    # ── User verlässt Temp-Kanal ──────────────────────────
    if before.channel is not None and before.channel.id in temp_voice_channels:
        channel_id = before.channel.id

        await asyncio.sleep(1)

        voice_channel = member.guild.get_channel(channel_id)
        if voice_channel is None:
            temp_voice_channels.pop(channel_id, None)
            return

        # Kanal leer → löschen
        if len(voice_channel.members) == 0:
            clear_owner_recovery(channel_id)
            await delete_temp_voice_channel(channel_id, member.guild)
            dashboard_state.push_event_sync("vc_deleted", {"channel_id": channel_id})
            dashboard_state.update_temp_vcs(temp_voice_channels)
            return

        # Owner gegangen → neuen Owner + Recovery starten
        vc_data = temp_voice_channels.get(channel_id)
        if vc_data and member.id == vc_data["owner_id"]:
            record_owner_left(channel_id, member.id)

            remaining = [m for m in voice_channel.members if not m.bot]
            if remaining:
                new_owner = remaining[0]
                old_owner = member
                try:
                    await voice_channel.set_permissions(
                        new_owner,
                        manage_channels=True, connect=True, speak=True,
                        move_members=True, mute_members=True, deafen_members=True,
                    )
                    await voice_channel.set_permissions(old_owner, overwrite=None)

                    text_ch = member.guild.get_channel(vc_data.get("text_channel_id", 0))
                    if text_ch:
                        await text_ch.set_permissions(
                            new_owner, read_messages=True,
                            send_messages=True, manage_messages=True,
                        )
                        await text_ch.set_permissions(old_owner, overwrite=None)
                        await text_ch.send(
                            f"👑 **{new_owner.mention}** ist jetzt Admin!\n"
                            f"-# Der vorherige Admin kann innerhalb von 10 Min. "
                            f"zurückkehren um den Rang zurückzubekommen."
                        )

                    vc_data["owner_id"] = new_owner.id
                    print(f"👑 [TempVC] {old_owner.display_name} → {new_owner.display_name}")
                    dashboard_state.push_event_sync("vc_owner_change", {
                        "channel_id": channel_id,
                        "old_owner":  old_owner.display_name,
                        "new_owner":  new_owner.display_name,
                    })
                except Exception as e:
                    print(f"❌ [TempVC] Ownership-Transfer Fehler: {e}")

        await update_temp_voice_panel(member.guild, channel_id)
        dashboard_state.update_temp_vcs(temp_voice_channels)


@d_bot.event
async def on_member_join(member: discord.Member):
    # ── Auto-Rolle vergeben ──────────────────────────────
    if AUTO_ROLE_NAME:
        try:
            role = discord.utils.get(member.guild.roles, name=AUTO_ROLE_NAME)
            if role:
                await member.add_roles(role, reason="Auto-Rolle bei Server-Beitritt")
                print(f"✅ [AutoRole] {member.display_name} hat die Rolle '{AUTO_ROLE_NAME}' erhalten")
            else:
                print(f"⚠️ [AutoRole] Rolle '{AUTO_ROLE_NAME}' nicht gefunden!")
        except discord.Forbidden:
            print(f"❌ [AutoRole] Keine Berechtigung um {member.display_name} die Rolle zu geben!")
        except Exception as e:
            print(f"❌ [AutoRole] Fehler: {e}")

    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        account_age = now - member.created_at
        account_age_hours = account_age.total_seconds() / 3600
        account_age_days = account_age.days
        has_no_avatar = member.avatar is None
        suspicious_name = False
        digit_count = sum(c.isdigit() for c in member.name)
        special_char_count = sum(not c.isalnum() for c in member.name)
        if digit_count > 5 or special_char_count > 3:
            suspicious_name = True
        alarm_triggered = False
        embed_color = discord.Color.green()
        alarm_level = "✅ Normal"
        reasons = []
        if account_age_hours < 24:
            alarm_triggered = True
            embed_color = discord.Color.red()
            alarm_level = "🚨 KRITISCH"
            reasons.append(
                f"Account ist nur {account_age_hours:.1f} Stunden alt (< 24h)"
            )
        elif account_age_days < 7:
            alarm_triggered = True
            embed_color = discord.Color.orange()
            alarm_level = "⚠️ WARNUNG"
            reasons.append(
                f"Account ist nur {account_age_days} Tage alt (< 7 Tage)"
            )
        if has_no_avatar and (suspicious_name or account_age_days < 7):
            if not alarm_triggered:
                alarm_triggered = True
                embed_color = discord.Color.orange()
                alarm_level = "⚠️ WARNUNG"
            reasons.append("Kein Profilbild vorhanden")
            if suspicious_name:
                reasons.append(
                    "Verdächtiger Username (viele Zahlen/Sonderzeichen)"
                )
        vpn_detected = False
        if PROXYCHECK_API_KEY:
            if account_age_hours < 24:
                vpn_detected = True
                reasons.append(
                    "🔴 Hohes Risiko: Extrem neuer Account "
                    "(möglicher VPN/Proxy-User)"
                )
                if not alarm_triggered:
                    alarm_triggered = True
                    embed_color = discord.Color.red()
                    alarm_level = "🚨 HOHES RISIKO"
            elif account_age_days < 3 and has_no_avatar:
                reasons.append(
                    "🟠 Mittleres Risiko: Neuer Account ohne Profilbild"
                )
                if not alarm_triggered:
                    alarm_triggered = True
                    embed_color = discord.Color.orange()
                    alarm_level = "⚠️ VERDÄCHTIG"
        if alarm_triggered:
            embed = discord.Embed(
                title=f"{alarm_level} – Neues Mitglied",
                description=f"{member.mention} ist dem Server beigetreten",
                color=embed_color,
                timestamp=now,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(
                name="👤 Username", value=member.name, inline=True
            )
            embed.add_field(
                name="🆔 User-ID", value=str(member.id), inline=True
            )
            embed.add_field(
                name="📅 Account erstellt",
                value=f"<t:{int(member.created_at.timestamp())}:R>",
                inline=True,
            )
            embed.add_field(
                name="⏱️ Account-Alter",
                value=(
                    f"{account_age_days} Tage, "
                    f"{int(account_age_hours % 24)} Stunden"
                ),
                inline=True,
            )
            embed.add_field(
                name="🖼️ Profilbild",
                value="Nein" if has_no_avatar else "Ja",
                inline=True,
            )
            if vpn_detected:
                embed.add_field(
                    name="🛡️ VPN/Proxy-Status",
                    value="⚠️ Hohes Risiko erkannt",
                    inline=True,
                )
            if reasons:
                embed.add_field(
                    name="📊 Alarm-Gründe",
                    value="\n".join(f"• {r}" for r in reasons),
                    inline=False,
                )
            embed.set_footer(text=f"Mitglied #{member.guild.member_count}")
            await send_log(embed, d_bot)
        # Dashboard: Join immer melden (auch ohne Alarm)
        try:
            dashboard_state.add_join(
                name=member.display_name,
                uid=str(member.id),
                suspicious=alarm_triggered,
                age_days=account_age_days,
                avatar=str(member.display_avatar.url),
            )
        except Exception:
            pass

        # Verifikations-DM senden wenn Verifier konfiguriert
        if VERIFIER_URL and VERIFIED_ROLE_ID:
            try:
                # Prüfen ob User die Rolle schon hat
                already_verified = any(r.id == VERIFIED_ROLE_ID for r in member.roles)
                if not already_verified:
                    verify_embed = discord.Embed(
                        title="✅ Verifizierung erforderlich",
                        description=(
                            f"Hey **{member.display_name}**! Willkommen auf dem Server.\n\n"
                            f"Um Zugang zu allen Kanälen zu erhalten, musst du dich einmalig verifizieren.\n\n"
                            f"**[🔗 Klicke hier um dich zu verifizieren]({VERIFIER_URL}/verify)**"
                        ),
                        color=0x6366f1,
                    )
                    verify_embed.set_footer(text="ByGorgii Community • Verifikation")
                    try:
                        await member.send(embed=verify_embed)
                        print(f"📨 [Verifier] DM gesendet an {member.display_name}")
                    except discord.Forbidden:
                        # DMs deaktiviert – in Welcome-Kanal senden falls konfiguriert
                        print(f"⚠️ [Verifier] DM fehlgeschlagen (DMs deaktiviert): {member.display_name}")
            except Exception as e:
                print(f"❌ [Verifier] DM-Fehler: {e}")

    except Exception as e:
        print(f"Fehler in on_member_join: {e}")


@d_bot.event
async def on_message(message: discord.Message):
    if message.author == d_bot.user:
        return
    # KI-Chat: Bot wird angepingt → antwortet auf die Frage
    if d_bot.user.mentioned_in(message) and ai_enabled:
        user_input = (
            message.content
            .replace(f'<@{d_bot.user.id}>', '')
            .replace(f'<@!{d_bot.user.id}>', '')
            .strip()
        )
        if not user_input:
            await message.reply("Wie kann ich dir helfen?")
        else:
            try:
                await process_ai_reply(message, user_input)
            except Exception as e:
                # Fehler nie öffentlich senden, nur loggen
                print(f"❌ [AI] on_message Fehler: {type(e).__name__}: {e}")
    # Dashboard: Nachricht zählen
    try:
        if not message.author.bot and message.guild:
            dashboard_state.add_message(
                message.author.id, message.author.display_name
            )
    except Exception:
        pass
    await d_bot.process_commands(message)


@d_bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Loggt bearbeitete Nachrichten in den MESSAGE_LOG_CHANNEL."""
    if not MESSAGE_LOG_CHANNEL_ID:
        return
    if before.author.bot:
        return
    if before.content == after.content:
        return  # Nur Content-Änderungen loggen, kein Embed-Update etc.

    log_channel = d_bot.get_channel(MESSAGE_LOG_CHANNEL_ID)
    if not log_channel:
        return

    embed = discord.Embed(
        title="✏️ Nachricht bearbeitet",
        color=0xf59e0b,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_author(
        name=before.author.display_name,
        icon_url=before.author.display_avatar.url,
    )
    embed.add_field(name="Kanal", value=before.channel.mention, inline=True)
    embed.add_field(name="User", value=f"{before.author.mention} (`{before.author.id}`)", inline=True)
    embed.add_field(name="⬛ Vorher", value=before.content[:1024] or "*leer*", inline=False)
    embed.add_field(name="✅ Nachher", value=after.content[:1024] or "*leer*", inline=False)
    embed.add_field(name="🔗 Springe zur Nachricht", value=f"[Klick]({after.jump_url})", inline=False)
    embed.set_footer(text=f"User-ID: {before.author.id}")
    try:
        await log_channel.send(embed=embed)
    except Exception as e:
        print(f"❌ [MessageLog] Edit-Log Fehler: {e}")


@d_bot.event
async def on_message_delete(message: discord.Message):
    """Loggt gelöschte Nachrichten in den MESSAGE_LOG_CHANNEL."""
    if not MESSAGE_LOG_CHANNEL_ID:
        return
    if message.author.bot:
        return
    if not message.content and not message.attachments:
        return

    log_channel = d_bot.get_channel(MESSAGE_LOG_CHANNEL_ID)
    if not log_channel:
        return

    embed = discord.Embed(
        title="🗑️ Nachricht gelöscht",
        color=0xef4444,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(name="Kanal", value=message.channel.mention, inline=True)
    embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
    if message.content:
        embed.add_field(name="💬 Inhalt", value=message.content[:1024], inline=False)
    if message.attachments:
        files = "\n".join(a.filename for a in message.attachments)
        embed.add_field(name="📎 Anhänge", value=files, inline=False)
    embed.set_footer(text=f"User-ID: {message.author.id} · Nachrichten-ID: {message.id}")
    try:
        await log_channel.send(embed=embed)
    except Exception as e:
        print(f"❌ [MessageLog] Delete-Log Fehler: {e}")


@d_bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not ai_enabled:
        return
    try:
        if payload.user_id == d_bot.user.id:
            return
        emoji_str = str(payload.emoji)
        if emoji_str not in FLAG_TO_LANGUAGE:
            return
        channel = d_bot.get_channel(payload.channel_id)
        if not channel:
            return
        message = await channel.fetch_message(payload.message_id)
        if not message or not message.content:
            return
        user = await d_bot.fetch_user(payload.user_id)
        if not user:
            return
        target_language = FLAG_TO_LANGUAGE[emoji_str]
        translation = await translate_message(
            message.content, target_language
        )
        if translation:
            try:
                dm_channel = await user.create_dm()
                await dm_channel.send(
                    f"{emoji_str} **Übersetzung ({target_language}):**\n"
                    f"{translation}\n\n"
                    f"[Zum Post springen]({message.jump_url})"
                )
            except discord.Forbidden:
                await channel.send(
                    f"{user.mention} {emoji_str} "
                    f"**Übersetzung ({target_language}):**\n"
                    f"{translation}\n\n"
                    f"-# 💡 Tipp: Aktiviere DMs vom Server, "
                    f"um Übersetzungen privat zu erhalten!",
                    delete_after=30,
                )
        else:
            try:
                dm_channel = await user.create_dm()
                await dm_channel.send(
                    "❌ Die Übersetzung ist fehlgeschlagen."
                )
            except discord.Forbidden:
                await channel.send(
                    f"{user.mention} ❌ Die Übersetzung ist fehlgeschlagen.",
                    delete_after=10,
                )
    except Exception as e:
        print(f"Fehler in on_raw_reaction_add: {e}")


# ══════════════════════════════════════════════════════════
#            ACTIVITY EMBED BUILDER (Zentral)
# ══════════════════════════════════════════════════════════

async def build_and_send_activity(
    interaction: discord.Interaction,
    member: discord.Member,
    monat: int,
    jahr: int,
):
    data = load_data()
    uid = str(member.id)
    now = datetime.datetime.now(datetime.timezone.utc)

    if monat < 1 or monat > 12:
        return await interaction.followup.send(
            "❌ Ungültiger Monat (1-12).", ephemeral=True
        )

    if uid not in data["users"]:
        embed = discord.Embed(
            title="❌ Nicht registriert",
            description=(
                f"{member.mention} hat keinen verknüpften Twitch-Account.\n"
                f"Nutze `/connect` um einen Account zu verbinden."
            ),
            color=COLOR_ERROR,
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    twitch_name = data["users"][uid]["twitch_name"]
    month_stats = get_month_stats(data, uid, jahr, monat)
    msg_stats = get_month_message_stats(data, uid, jahr, monat)
    month_name = MONTH_NAMES[monat]
    total_stream_days = len(data["streams"])
    total_present = sum(
        1 for d in data["streams"] if uid in data["streams"][d]
    )
    total_absent = total_stream_days - total_present
    total_pct = (
        (total_present / total_stream_days * 100)
        if total_stream_days > 0 else 0
    )
    current_streak = get_current_streak(data, uid)
    longest_streak = get_longest_streak(data, uid)
    grade, grade_emoji, grade_color = get_activity_grade(total_pct)

    all_counts = {}
    for u in data["users"]:
        all_counts[u] = sum(
            1 for d in data["streams"] if u in data["streams"][d]
        )
    sorted_users = sorted(
        all_counts.items(), key=lambda x: x[1], reverse=True
    )
    rank = next(
        (i + 1 for i, (u, _) in enumerate(sorted_users) if u == uid),
        len(sorted_users),
    )

    last_seen = "Noch nie"
    for date_str in sorted(data["streams"].keys(), reverse=True):
        if uid in data["streams"][date_str]:
            last_seen = date_str
            break

    cal_display = make_streak_calendar(data, uid, jahr, monat)

    embed = discord.Embed(
        title=f"📊 Aktivitätsbericht: {member.display_name}",
        description=f"Statistik für **{month_name} {jahr}**",
        color=grade_color,
        timestamp=now,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="🎮 Twitch Account",
        value=f"[{twitch_name}](https://twitch.tv/{twitch_name})",
        inline=True,
    )
    embed.add_field(
        name="🏅 Rang",
        value=f"{get_rank_emoji(rank)} von {len(data['users'])}",
        inline=True,
    )
    embed.add_field(
        name=f"{grade_emoji} Bewertung",
        value=f"**{grade}**",
        inline=True,
    )
    embed.add_field(
        name="📺 Streams gesamt",
        value=f"**{month_stats['stream_days']}**",
        inline=True,
    )
    embed.add_field(
        name="✅ Anwesend",
        value=f"**{month_stats['present']}**",
        inline=True,
    )
    embed.add_field(
        name="❌ Fehltage",
        value=f"**{month_stats['absent']}**",
        inline=True,
    )
    progress = make_progress_bar(
        month_stats["present"], month_stats["stream_days"], 12
    )
    embed.add_field(
        name=f"📈 Monats-Quote ({month_name})",
        value=progress,
        inline=False,
    )
    embed.add_field(
        name="💬 Chat-Aktivität (Monat)",
        value=(
            f"📝 **{msg_stats['total_messages']:,}** Nachrichten\n"
            f"📊 Ø **{msg_stats['avg_per_stream']:.0f}** pro Stream\n"
            f"⏱️ Ø **{msg_stats['avg_per_hour']:.0f}** pro Stunde\n"
            f"🕐 Peak: **{msg_stats['peak_hour']:02d}:00** Uhr"
        ),
        inline=False,
    )
    total_progress = make_progress_bar(total_present, total_stream_days, 12)
    embed.add_field(
        name="📊 Gesamt-Quote (Alle Zeiten)",
        value=(
            f"{total_progress}\n"
            f"╰ `{total_present}` anwesend  •  "
            f"`{total_absent}` verpasst  •  "
            f"`{total_stream_days}` total"
        ),
        inline=False,
    )
    flames = (
        "🔥" * min(current_streak, 10) if current_streak > 0 else "—"
    )
    embed.add_field(
        name="⚡ Aktuelle Streak",
        value=f"**{current_streak}** Tage\n{flames}",
        inline=True,
    )
    embed.add_field(
        name="🏆 Längste Streak",
        value=f"**{longest_streak}** Tage",
        inline=True,
    )
    embed.add_field(
        name="🕐 Zuletzt gesehen",
        value=f"`{last_seen}`",
        inline=True,
    )
    embed.add_field(
        name=f"🗓️ Kalender — {month_name} {jahr}",
        value=(
            f"{cal_display}\n"
            f"╰ ✅ Da • ❌ Gefehlt • 🔵 Heute • ➖ Kein Stream"
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"Tracking basiert auf Chat-Aktivität • {STREAMER_CHANNEL}",
        icon_url=member.display_avatar.url,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════
#             FEATURE: REMINDER SYSTEM
# ══════════════════════════════════════════════════════════

async def send_reminder(
    user: discord.User,
    channel: discord.TextChannel,
    reminder_text: str,
    seconds: int,
):
    await asyncio.sleep(seconds)
    try:
        await channel.send(
            f"⏰ {user.mention} **Dein Reminder:**\n{reminder_text}"
        )
    except discord.Forbidden:
        try:
            dm = await user.create_dm()
            await dm.send(
                f"⏰ **Dein Reminder:**\n{reminder_text}\n"
                f"-# (Ich konnte dich nicht im Kanal pingen)"
            )
        except Exception as e:
            print(f"Fehler beim Senden des Reminders (DM): {e}")
    except Exception as e:
        print(f"Fehler beim Senden des Reminders: {e}")


@d_bot.tree.command(
    name="remind",
    description="Setzt einen Reminder. Beispiel: /remind 10m Pizza",
)
@app_commands.describe(
    zeit="Zeitangabe (z.B. 10s, 5m, 2h, 1d)",
    nachricht="Was soll ich dich erinnern?",
)
async def remind_command(
    interaction: discord.Interaction,
    zeit: str,
    nachricht: str,
):
    seconds = parse_time(zeit)
    if seconds is None:
        await interaction.response.send_message(
            "❌ Ungültiges Zeitformat!\n"
            "**Erlaubte Formate:** `10s`, `5m`, `2h`, `1d`\n"
            "**Beispiel:** `/remind 10m Pizza aus dem Ofen holen`",
            ephemeral=True,
        )
        return
    if seconds < 5:
        await interaction.response.send_message(
            "❌ Der Reminder muss mindestens 5 Sekunden in der Zukunft liegen.",
            ephemeral=True,
        )
        return
    if seconds > 86400 * 7:
        await interaction.response.send_message(
            "❌ Der Reminder darf maximal 7 Tage in der Zukunft liegen.",
            ephemeral=True,
        )
        return
    remind_at = datetime.datetime.now(
        datetime.timezone.utc
    ) + datetime.timedelta(seconds=seconds)
    remind_timestamp = int(remind_at.timestamp())
    await interaction.response.send_message(
        f"✅ **Reminder gesetzt!**\n"
        f"📝 **Nachricht:** {nachricht}\n"
        f"⏰ **Erinnerung:** <t:{remind_timestamp}:R> "
        f"(<t:{remind_timestamp}:T>)",
        ephemeral=True,
    )
    asyncio.create_task(
        send_reminder(
            user=interaction.user,
            channel=interaction.channel,
            reminder_text=nachricht,
            seconds=seconds,
        )
    )


# ══════════════════════════════════════════════════════════
#            SLASH COMMANDS: ACTIVITY TRACKER
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(
    name="connect",
    description="Verknüpfe deinen Twitch-Account",
)
@app_commands.describe(twitch_name="Dein exakter Twitch-Benutzername")
@is_allowed_channel()
async def cmd_connect(
    interaction: discord.Interaction, twitch_name: str
):
    if not any(
        role.name == MOD_ROLE_NAME for role in interaction.user.roles
    ):
        embed = discord.Embed(
            title="❌ Keine Berechtigung",
            description=f"Du benötigst die Rolle **{MOD_ROLE_NAME}**.",
            color=COLOR_ERROR,
        )
        return await interaction.response.send_message(
            embed=embed, ephemeral=True
        )
    clean_name = twitch_name.strip().lower()
    if not clean_name or len(clean_name) < 2:
        return await interaction.response.send_message(
            "❌ Ungültiger Name.", ephemeral=True
        )
    data = load_data()
    user_id = str(interaction.user.id)
    for uid, info in data["users"].items():
        if (
            info.get("twitch_name", "").lower() == clean_name
            and uid != user_id
        ):
            embed = discord.Embed(
                title="❌ Bereits vergeben",
                description=(
                    f"`{clean_name}` ist bereits mit einem "
                    f"anderen Account verknüpft."
                ),
                color=COLOR_ERROR,
            )
            return await interaction.response.send_message(
                embed=embed, ephemeral=True
            )
    data["users"][user_id] = {
        "twitch_name": clean_name,
        "display_name": interaction.user.display_name,
    }
    save_data(data)
    embed = discord.Embed(
        title="🔗 Erfolgreich verknüpft!",
        color=COLOR_SUCCESS,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(
        name="",
        value=(
            f">>> **Discord:** {interaction.user.mention}\n"
            f"**Twitch:** [{clean_name}](https://twitch.tv/{clean_name})\n\n"
            f"✅ Deine Chat-Aktivität wird ab jetzt erfasst!"
        ),
        inline=False,
    )
    embed.set_footer(text="Activity Tracker • Verknüpfung aktiv")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(
    name="disconnect",
    description="Entferne deine Twitch-Verknüpfung",
)
@is_allowed_channel()
async def cmd_disconnect(interaction: discord.Interaction):
    data = load_data()
    user_id = str(interaction.user.id)
    if user_id not in data["users"]:
        return await interaction.response.send_message(
            "❌ Kein Account verknüpft.", ephemeral=True
        )
    old_name = data["users"][user_id]["twitch_name"]
    del data["users"][user_id]
    save_data(data)
    embed = discord.Embed(
        title="🔓 Verknüpfung entfernt",
        description=f"Account **{old_name}** wurde getrennt.",
        color=COLOR_ERROR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(
    name="activity",
    description="Zeigt den Aktivitätsbericht eines Users",
)
@app_commands.describe(
    member="Der User dessen Aktivität du sehen willst",
    monat="Monat (1-12) — Standard: aktueller Monat",
    jahr="Jahr — Standard: aktuelles Jahr",
)
@is_allowed_channel()
async def cmd_activity(
    interaction: discord.Interaction,
    member: discord.Member,
    monat: int = None,
    jahr: int = None,
):
    await interaction.response.defer(ephemeral=True)
    today = datetime.date.today()
    if monat is None:
        monat = today.month
    if jahr is None:
        jahr = today.year
    await build_and_send_activity(interaction, member, monat, jahr)


@d_bot.tree.command(
    name="myactivity",
    description="Zeigt deine eigene Aktivität",
)
@is_allowed_channel()
async def cmd_myactivity(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    today = datetime.date.today()
    await build_and_send_activity(
        interaction, interaction.user, today.month, today.year
    )


@d_bot.tree.command(
    name="compare",
    description="Vergleiche die Aktivität zweier User direkt miteinander",
)
@app_commands.describe(
    user_1="Erster User",
    user_2="Zweiter User",
)
@is_allowed_channel()
async def cmd_compare(
    interaction: discord.Interaction,
    user_1: discord.Member,
    user_2: discord.Member,
):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    now = datetime.datetime.now(datetime.timezone.utc)

    uid1 = str(user_1.id)
    uid2 = str(user_2.id)

    not_registered = []
    if uid1 not in data["users"]:
        not_registered.append(user_1.mention)
    if uid2 not in data["users"]:
        not_registered.append(user_2.mention)
    if not_registered:
        return await interaction.followup.send(
            f"❌ Folgende User sind nicht registriert: "
            f"{', '.join(not_registered)}",
            ephemeral=True,
        )
    if uid1 == uid2:
        return await interaction.followup.send(
            "❌ Du kannst keinen User mit sich selbst vergleichen!",
            ephemeral=True,
        )

    s1 = get_user_compare_stats(data, uid1)
    s2 = get_user_compare_stats(data, uid2)
    total_days = s1["total_days"]

    def wa(val1, val2):
        if val1 > val2:
            return "◀️", ""
        elif val2 > val1:
            return "", "▶️"
        else:
            return "🤝", "🤝"

    ap = wa(s1["present"], s2["present"])
    aq = wa(s1["pct"], s2["pct"])
    ar = wa(s2["rank"], s1["rank"])
    ast = wa(s1["current_streak"], s2["current_streak"])
    al = wa(s1["longest_streak"], s2["longest_streak"])

    if s1["present"] > s2["present"]:
        embed_color = 0x3498db
    elif s2["present"] > s1["present"]:
        embed_color = 0xe74c3c
    else:
        embed_color = COLOR_GOLD

    bar1 = make_progress_bar(s1["present"], total_days, 8)
    bar2 = make_progress_bar(s2["present"], total_days, 8)

    score1 = sum([
        s1["present"] > s2["present"],
        s1["pct"] > s2["pct"],
        s1["current_streak"] > s2["current_streak"],
        s1["longest_streak"] > s2["longest_streak"],
        s1["rank"] < s2["rank"],
    ])
    score2 = sum([
        s2["present"] > s1["present"],
        s2["pct"] > s1["pct"],
        s2["current_streak"] > s1["current_streak"],
        s2["longest_streak"] > s1["longest_streak"],
        s2["rank"] < s1["rank"],
    ])

    if score1 > score2:
        winner_text = (
            f"🏆 **{user_1.display_name}** gewinnt! "
            f"({score1}:{score2} Kategorien)"
        )
    elif score2 > score1:
        winner_text = (
            f"🏆 **{user_2.display_name}** gewinnt! "
            f"({score2}:{score1} Kategorien)"
        )
    else:
        winner_text = f"🤝 **Unentschieden!** ({score1}:{score2})"

    embed = discord.Embed(
        title="⚔️ Aktivitäts-Vergleich",
        color=embed_color,
        timestamp=now,
    )
    embed.add_field(
        name="👤 User 1",
        value=(
            f"{user_1.mention}\n"
            f"[{s1['twitch']}](https://twitch.tv/{s1['twitch']})"
        ),
        inline=True,
    )
    embed.add_field(name="⚔️", value="**VS**", inline=True)
    embed.add_field(
        name="👤 User 2",
        value=(
            f"{user_2.mention}\n"
            f"[{s2['twitch']}](https://twitch.tv/{s2['twitch']})"
        ),
        inline=True,
    )
    embed.add_field(
        name=f"📊 {user_1.display_name}",
        value=(
            f"✅ Anwesend: **{s1['present']}** Tage {ap[0]}\n"
            f"📈 Quote: **{s1['pct']:.1f}%** {aq[0]}\n"
            f"🏅 Rang: **#{s1['rank']}** {ar[0]}\n"
            f"⚡ Streak: **{s1['current_streak']}** Tage {ast[0]}\n"
            f"🏆 Max. Streak: **{s1['longest_streak']}** Tage {al[0]}\n"
            f"🎓 Note: **{s1['grade_emoji']} {s1['grade']}**\n"
            f"🕐 Zuletzt: `{s1['last_seen']}`\n"
            f"🌱 Seit: `{s1['first_seen']}`"
        ),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name=f"📊 {user_2.display_name}",
        value=(
            f"{ap[1]} ✅ Anwesend: **{s2['present']}** Tage\n"
            f"{aq[1]} 📈 Quote: **{s2['pct']:.1f}%**\n"
            f"{ar[1]} 🏅 Rang: **#{s2['rank']}**\n"
            f"{ast[1]} ⚡ Streak: **{s2['current_streak']}** Tage\n"
            f"{al[1]} 🏆 Max. Streak: **{s2['longest_streak']}** Tage\n"
            f"🎓 Note: **{s2['grade_emoji']} {s2['grade']}**\n"
            f"🕐 Zuletzt: `{s2['last_seen']}`\n"
            f"🌱 Seit: `{s2['first_seen']}`"
        ),
        inline=True,
    )
    embed.add_field(
        name=f"📊 Gesamt-Quote {user_1.display_name}",
        value=bar1,
        inline=False,
    )
    embed.add_field(
        name=f"📊 Gesamt-Quote {user_2.display_name}",
        value=bar2,
        inline=False,
    )
    embed.add_field(
        name="🎯 Gesamtergebnis",
        value=winner_text,
        inline=False,
    )
    embed.set_footer(
        text=(
            f"📅 Basierend auf {total_days} erfassten Stream-Tagen • "
            f"Tracking basiert auf Chat-Aktivität"
        )
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@d_bot.tree.command(
    name="leaderboard",
    description="Top-Liste der aktivsten Chat-Teilnehmer",
)
@is_allowed_channel()
async def cmd_leaderboard(interaction: discord.Interaction):
    data = load_data()
    if not data["users"]:
        return await interaction.response.send_message(
            "❌ Keine User registriert.", ephemeral=True
        )
    total_days = len(data["streams"])
    counts = {}
    for uid in data["users"]:
        counts[uid] = sum(
            1 for d in data["streams"] if uid in data["streams"][d]
        )
    ranking = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    lines = []
    for i, (uid, count) in enumerate(ranking[:15]):
        name = data["users"][uid].get("display_name", "Unbekannt")
        twitch = data["users"][uid].get("twitch_name", "?")
        bar = make_progress_bar(count, total_days, 8)
        if i < 3:
            medal = ["🥇", "🥈", "🥉"][i]
            lines.append(
                f"{medal} **{name}** (`{twitch}`)\n╰ {count} Tage — {bar}"
            )
        else:
            pct = (count / total_days * 100) if total_days > 0 else 0
            lines.append(
                f"`#{i + 1}` **{name}** (`{twitch}`) — "
                f"{count} Tage ({pct:.0f}%)"
            )
    embed = discord.Embed(
        title="🏆 Aktivitäts-Ranking",
        description="\n\n".join(lines),
        color=COLOR_GOLD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text=f"📅 Erfasste Stream-Tage: {total_days}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(
    name="monthlyreview",
    description="📅 Sendet den Monatsbericht manuell (nur Admins)",
)
@app_commands.describe(
    monat="Monat (1-12) — Standard: aktueller Monat",
    jahr="Jahr — Standard: aktuelles Jahr",
)
@is_admin_user()
async def cmd_monthlyreview(interaction: discord.Interaction, monat: int = None, jahr: int = None):
    await interaction.response.defer(ephemeral=True)
    today = datetime.date.today()
    if monat is None:
        monat = today.month
    if jahr is None:
        jahr = today.year
    try:
        review_channel = d_bot.get_channel(REVIEW_CHANNEL_ID)
        if not review_channel:
            review_channel = interaction.channel
        await send_monthly_review(review_channel, interaction.guild, jahr, monat)
        await interaction.followup.send(
            f"✅ Monatsbericht für {MONTH_NAMES[monat]} {jahr} gesendet!",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


@d_bot.tree.command(
    name="yearlyreview",
    description="🎆 Sendet den Jahresbericht manuell (nur Admins)",
)
@app_commands.describe(jahr="Jahr — Standard: aktuelles Jahr")
@is_admin_user()
async def cmd_yearlyreview(interaction: discord.Interaction, jahr: int = None):
    await interaction.response.defer(ephemeral=True)
    if jahr is None:
        jahr = datetime.date.today().year
    try:
        review_channel = d_bot.get_channel(REVIEW_CHANNEL_ID)
        if not review_channel:
            review_channel = interaction.channel
        await send_yearly_review(review_channel, interaction.guild, jahr)
        await interaction.followup.send(
            f"✅ Jahresbericht für {jahr} gesendet!",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

### MUSIC MODULE START ###
# ══════════════════════════════════════════════════════════
#            SLASH COMMANDS: MUSIK-MODUL
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="play", description="🎵 Spielt einen Song oder eine Playlist ab (YouTube + Spotify)")
@app_commands.describe(query="Songtitel, YouTube-URL, Spotify-URL oder Playlist")
async def cmd_play(interaction: discord.Interaction, query: str):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul ist deaktiviert.", ephemeral=True)
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Du musst in einem Voice-Kanal sein!", ephemeral=True)
        return

    await interaction.response.defer()
    user_vc = interaction.user.voice.channel
    state = get_music_state(interaction.guild_id, d_bot)

    if state.voice_client is None or not state.voice_client.is_connected():
        perms = user_vc.permissions_for(interaction.guild.me)
        if not perms.connect or not perms.speak:
            await interaction.followup.send(
                f"❌ Ich habe keine Berechtigung für **{user_vc.name}**!\n"
                f"-# Der Bot braucht `Verbinden` und `Sprechen` Rechte."
            )
            return
        try:
            state.voice_client = await user_vc.connect(self_deaf=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}")
            return
    elif state.voice_client.channel.id != user_vc.id:
        try:
            await state.voice_client.move_to(user_vc)
        except Exception as e:
            await interaction.followup.send(f"❌ Konnte nicht zum Kanal wechseln: {e}")
            return

    state.reset_idle()

    # ── Spotify-URL erkennen ─────────────────────────────
    if is_spotify_url(query):
        spotify_tracks = await fetch_spotify_tracks(query)
        if not spotify_tracks:
            await interaction.followup.send("❌ Konnte keine Songs von Spotify laden.")
            return

        if len(spotify_tracks) == 1:
            # Einzelner Track
            sp = spotify_tracks[0]
            results = await search_tracks(sp["search_query"], limit=1)
            if not results:
                await interaction.followup.send(f"❌ Konnte **{sp['title']}** nicht auf YouTube finden.")
                return
            entry = results[0]
            webpage_url = _get_reference_url(entry, "")
            duration = sp.get("duration", 0) or (int(entry.get("duration", 0)) if entry.get("duration") else 0)
            thumbnail = sp.get("thumbnail", "") or entry.get("thumbnail", "")
            song = SongInfo(sp["title"], "", webpage_url, duration, thumbnail, interaction.user)
            state.queue.append(song)

            if not state.voice_client.is_playing() and state.current is None:
                msg = await interaction.followup.send(
                    f"<:spotify:1234> **{sp['title']}** wird geladen...", wait=True
                )
                try:
                    await msg.delete(delay=5)
                except Exception:
                    pass
                await play_next(state, interaction.channel)
            else:
                embed = discord.Embed(
                    title="➕ Spotify → Warteschlange",
                    description=f"🟢 **{sp['title']}**",
                    color=COLOR_SPOTIFY,
                )
                embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
                embed.add_field(name="📋 Position", value=f"#{len(state.queue)}", inline=True)
                if thumbnail:
                    embed.set_thumbnail(url=thumbnail)
                await interaction.followup.send(embed=embed)
        else:
            # Playlist / Album
            added = await play_spotify_tracks(state, spotify_tracks, interaction.user, interaction.channel)
            embed = discord.Embed(
                title="🟢 Spotify Playlist/Album geladen!",
                description=f"**{added}** Songs zur Warteschlange hinzugefügt!",
                color=COLOR_SPOTIFY,
            )
            embed.set_footer(text="Songs werden über YouTube abgespielt")
            await interaction.followup.send(embed=embed)
            if not state.voice_client.is_playing() and state.current is None:
                await play_next(state, interaction.channel)
        return

    # ── YouTube Playlist ─────────────────────────────────
    if _is_playlist_url(query):
        playlist_entries = await extract_playlist(query)
        if not playlist_entries:
            await interaction.followup.send("❌ Konnte keine Songs aus der Playlist laden.")
            return
        songs_added = 0
        for entry in playlist_entries:
            title = entry.get('title', 'Unbekannt')
            webpage_url = _get_reference_url(entry)
            duration = int(entry.get('duration', 0)) if entry.get('duration') else 0
            thumbnail = _entry_thumbnail(entry)
            if not webpage_url:
                continue
            song = SongInfo(title, '', webpage_url, duration, thumbnail, interaction.user)
            state.queue.append(song)
            songs_added += 1
        embed = discord.Embed(
            title="📋 Playlist hinzugefügt",
            description=f"**{songs_added}** Songs zur Warteschlange hinzugefügt!",
            color=COLOR_MUSIC,
        )
        await interaction.followup.send(embed=embed)
        if not state.voice_client.is_playing() and state.current is None:
            await play_next(state, interaction.channel)
        return

    # ── YouTube Einzelsuche ──────────────────────────────
    results = await search_tracks(query, limit=1)
    if not results:
        await interaction.followup.send(f"❌ Keine Ergebnisse für: `{query}`")
        return

    entry = results[0]
    title = entry.get('title', 'Unbekannt')
    webpage_url = _get_reference_url(entry, query)
    duration = int(entry.get('duration', 0)) if entry.get('duration') else 0
    thumbnail = _entry_thumbnail(entry)

    song = SongInfo(title, '', webpage_url, duration, thumbnail, interaction.user)
    state.queue.append(song)

    if not state.voice_client.is_playing() and state.current is None:
        msg = await interaction.followup.send(f"🎵 **{title}** wird geladen...", wait=True)
        try:
            await msg.delete(delay=5)
        except Exception:
            pass
        await play_next(state, interaction.channel)
    else:
        pos = len(state.queue)
        embed = discord.Embed(
            title="➕ Zur Warteschlange hinzugefügt",
            description=f"[{title}]({webpage_url})",
            color=COLOR_MUSIC,
        )
        embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
        embed.add_field(name="📋 Position", value=f"#{pos}", inline=True)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        await interaction.followup.send(embed=embed)

@d_bot.tree.command(name="search", description="🎵 Suche nach Songs und wähle einen aus")
@app_commands.describe(query="Songtitel zum Suchen")
async def cmd_search(interaction: discord.Interaction, query: str):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul ist deaktiviert.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    results = await search_tracks(query, limit=10)
    if not results:
        await interaction.followup.send(f"❌ Keine Ergebnisse für: `{query}`", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🔍 Suchergebnisse für: {query}",
        color=COLOR_MUSIC,
    )
    lines = []
    for i, entry in enumerate(results[:10], 1):
        title = entry.get('title', 'Unbekannt')
        dur = int(entry.get('duration', 0)) if entry.get('duration') else 0
        m, s = divmod(dur, 60)
        dur_str = f"{m}:{s:02d}" if dur > 0 else "?"
        lines.append(f"`{i}.` **{title}** — `{dur_str}`")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Wähle einen Song aus dem Dropdown-Menü")

    view = SearchResultSelectView(results[:10], interaction.user, interaction.guild_id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@d_bot.tree.command(name="skip", description="🎵 Überspringt den aktuellen Song")
async def cmd_skip(interaction: discord.Interaction):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
        return
    state = get_music_state(interaction.guild_id, d_bot)
    if not state.voice_client or not state.voice_client.is_playing():
        await interaction.response.send_message("❌ Es wird gerade nichts abgespielt.", ephemeral=True)
        return
    state.voice_client.stop()
    await interaction.response.send_message("⏭️ Song übersprungen!", delete_after=5)


@d_bot.tree.command(name="stop", description="🎵 Stoppt die Musik und leert die Warteschlange")
async def cmd_stop(interaction: discord.Interaction):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
        return
    state = get_music_state(interaction.guild_id, d_bot)
    if not state.voice_client or not state.voice_client.is_connected():
        await interaction.response.send_message("❌ Ich bin in keinem Voice-Kanal.", ephemeral=True)
        return
    await state.cleanup()
    await interaction.response.send_message(
        "⏹️ Musik gestoppt, Warteschlange geleert und Kanal verlassen.",
        delete_after=10,
    )


@d_bot.tree.command(name="queue", description="🎵 Zeigt die aktuelle Warteschlange")
async def cmd_queue(interaction: discord.Interaction):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
        return
    state = get_music_state(interaction.guild_id, d_bot)
    view = QueuePaginationView(state, page=0)
    embed = view._build_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@d_bot.tree.command(name="shuffle", description="🎵 Mischt die Warteschlange zufällig (Fisher-Yates)")
async def cmd_shuffle(interaction: discord.Interaction):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
        return
    state = get_music_state(interaction.guild_id, d_bot)
    if len(state.queue) < 2:
        await interaction.response.send_message("❌ Zu wenig Songs zum Mischen.", ephemeral=True)
        return
    queue_list = list(state.queue)
    for i in range(len(queue_list) - 1, 0, -1):
        j = random.randint(0, i)
        queue_list[i], queue_list[j] = queue_list[j], queue_list[i]
    state.queue = deque(queue_list)
    await interaction.response.send_message(
        f"🔀 **{len(queue_list)}** Songs wurden gemischt!", delete_after=10
    )


@d_bot.tree.command(name="autoplay", description="🎵 Automatisch ähnliche Songs spielen wenn die Queue leer ist")
@app_commands.describe(mode="Autoplay ein- oder ausschalten")
@app_commands.choices(mode=[
    app_commands.Choice(name="An", value="on"),
    app_commands.Choice(name="Aus", value="off"),
])
async def cmd_autoplay(interaction: discord.Interaction, mode: str):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
        return
    state = get_music_state(interaction.guild_id, d_bot)
    if mode == "on":
        state.autoplay = True
        await interaction.response.send_message(
            "🔄 **Autoplay aktiviert!** Ähnliche Songs werden automatisch gespielt.",
            delete_after=10,
        )
    else:
        state.autoplay = False
        await interaction.response.send_message("⏹️ **Autoplay deaktiviert.**", delete_after=10)


@d_bot.tree.command(name="nowplaying", description="🎵 Zeigt den aktuell spielenden Song")
async def cmd_nowplaying(interaction: discord.Interaction):
    if not MUSIC_ENABLED:
        await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
        return
    state = get_music_state(interaction.guild_id, d_bot)
    if not state.current:
        await interaction.response.send_message("❌ Es wird gerade nichts abgespielt.", ephemeral=True)
        return
    song = state.current
    try:
        req_mention = song.requester.mention if song.requester else "Unbekannt"
    except Exception:
        req_mention = "Unbekannt"
    embed = discord.Embed(
        title="🎶 Jetzt spielt",
        description=f"[{song.title}]({song.webpage_url})",
        color=COLOR_MUSIC,
    )
    embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
    embed.add_field(name="👤 Angefragt von", value=req_mention, inline=True)
    embed.add_field(name="📋 Queue", value=f"{len(state.queue)} Songs", inline=True)
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    embed.set_footer(text=f"Autoplay: {'✅ An' if state.autoplay else '❌ Aus'}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

### MUSIC MODULE END ###


@d_bot.tree.command(
    name="status",
    description="Zeigt den aktuellen Bot-Status",
)
@is_allowed_channel()
async def cmd_status(interaction: discord.Interaction):
    data = load_data()
    today = str(datetime.date.today())
    today_count = len(data["streams"].get(today, []))

    # Nächsten Review berechnen
    next_review = "Deaktiviert"
    if REVIEW_CHANNEL_ID:
        next_sunday = datetime.date.today()
        while next_sunday.weekday() != 6:
            next_sunday += datetime.timedelta(days=1)
        next_review_dt = datetime.datetime(
            next_sunday.year, next_sunday.month, next_sunday.day,
            20, 0, 0, tzinfo=datetime.timezone.utc
        )
        next_review = f"<t:{int(next_review_dt.timestamp())}:R>"

    ### MUSIC MODULE START ###
    active_music = sum(
        1 for s in music_states.values()
        if s.voice_client and s.voice_client.is_connected()
    )
    ### MUSIC MODULE END ###

    embed = discord.Embed(
        title="🤖 Bot-Status",
        color=COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="📡 Systeme",
        value=(
            f"Discord Bot ─── ✅ Online\n"
            f"Twitch Bot ──── "
            f"{'✅ Aktiv' if STREAMER_CHANNEL else '❌ Deaktiviert'}\n"
            f"Twitch Mod Log ─ "
            f"{'✅ Aktiv' if TWITCH_LOG_CHANNEL_ID else '❌ Deaktiviert'}\n"
            f"AI-Moderation ─ "
            f"{'✅ Aktiv' if ai_enabled else '❌ Deaktiviert'}\n"
            f"API-Key Rotation ─ "
            f"{'✅ ' + str(len(OPENROUTER_KEYS)) + ' Keys' if len(OPENROUTER_KEYS) > 1 else '➖ Einzelner Key'}\n"
            f"VPN-Erkennung ─ "
            f"{'✅ Aktiv' if PROXYCHECK_API_KEY else '❌ Deaktiviert'}\n"
            f"Temp Voice ──── "
            f"{'✅ Aktiv (' + str(len(temp_voice_channels)) + ' Kanäle)' if TEMP_VOICE_CHANNEL_ID else '❌ Deaktiviert'}\n"
            f"Weekly Review ─ "
            f"{'✅ Aktiv' if REVIEW_CHANNEL_ID else '❌ Deaktiviert'}\n"
            ### MUSIC MODULE START ###
            f"Musik-Modul ─── "
            f"{'✅ Aktiv (' + str(active_music) + ' aktiv)' if MUSIC_ENABLED else '❌ Deaktiviert'}\n"
            ### MUSIC MODULE END ###
            f"Tracking ────── ✅ Läuft"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Registriert", value=f"**{len(data['users'])}**", inline=True
    )
    embed.add_field(
        name="📅 Stream-Tage",
        value=f"**{len(data['streams'])}**",
        inline=True,
    )
    embed.add_field(
        name="📊 Heute erfasst", value=f"**{today_count}**", inline=True
    )
    if TEMP_VOICE_CHANNEL_ID:
        embed.add_field(
            name="🎙️ Aktive Temp-Kanäle",
            value=f"**{len(temp_voice_channels)}**",
            inline=True,
        )
    if REVIEW_CHANNEL_ID:
        embed.add_field(
            name="📅 Nächster Review",
            value=next_review,
            inline=True,
        )

    embed.add_field(
        name="🎰 Gambling",
        value=(
            f"**Kanal:** {'<#' + str(GAMBLING_CHANNEL_ID) + '>' if GAMBLING_CHANNEL_ID else 'Überall'}\n"
            "`/bet` `/coinflip` `/daily` `/balance`\n"
            "`/pay` `/coinleaderboard` `/gamblehelp`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎣 Angeln",
        value=(
            f"**Kanal:** {'<#' + str(FISHING_CHANNEL_ID) + '>' if FISHING_CHANNEL_ID else 'Überall'}\n"
            "`/fish` `/upgrade` `/buybait`\n"
            "`/fishstats` `/fishtop` `/shop`"
        ),
        inline=False,
    )
        
    if STREAMER_CHANNEL:
        embed.add_field(
            name="📺 Überwachter Kanal",
            value=(
                f"[{STREAMER_CHANNEL}]"
                f"(https://twitch.tv/{STREAMER_CHANNEL})"
            ),
            inline=False,
        )
    embed.set_footer(
        text="Combined Activity Tracker & AI Moderation System"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(
    name="aihelp",
    description="Zeigt alle verfügbaren Bot-Funktionen",
)
async def cmd_aihelp(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Bot-Hilfe",
        description="Alle verfügbaren Funktionen im Überblick",
        color=COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="📊 Activity Tracker",
        value=(
            "`/connect <twitch>` — Twitch-Account verknüpfen\n"
            "`/disconnect` — Verknüpfung entfernen\n"
            "`/activity @user` — Aktivität eines Users\n"
            "`/myactivity` — Deine eigene Aktivität\n"
            "`/compare @user_1 @user_2` — Zwei User vergleichen\n"
            "`/leaderboard` — Aktivitäts-Ranking\n"
            "`/status` — Bot-Status anzeigen"
        ),
        inline=False,
    )
    if ai_enabled:
        embed.add_field(
            name="🤖 KI-Features",
            value=(
                "**@Bot Frage** — Stelle eine Frage an die KI\n"
                "**🇩🇪🇺🇸🇫🇷...** — Reagiere mit Flagge zum Übersetzen\n"
                "**Auto-Moderation** — Toxische Nachrichten werden erkannt\n"
                "**Link-Analyse** — Verdächtige Links werden geprüft"
            ),
            inline=False,
        )
    embed.add_field(
        name="⏰ Reminder",
        value=(
            "`/remind <zeit> <nachricht>` — Reminder setzen\n"
            "**Formate:** `10s`, `5m`, `2h`, `1d`\n"
            "**Beispiel:** `/remind 10m Pizza aus dem Ofen`"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Twitch Mod Logger",
        value=(
            "Automatisches Logging von:\n"
            "🔨 Bans • ✅ Unbans\n"
            "⏱️ Timeouts • ✅ Untimeouts\n"
            "⚠️ Verwarnungen • 🗑️ Gelöschte Nachrichten"
        ),
        inline=False,
    )
    if TEMP_VOICE_CHANNEL_ID:
        embed.add_field(
            name="🎙️ Temp Voice Channels",
            value=(
                "Betrete den **Erstell-Kanal** um automatisch\n"
                "einen eigenen Voice-Kanal zu erhalten!\n\n"
                "**Als Kanal-Admin kannst du:**\n"
                "✏️ Kanal umbenennen\n"
                "👥 Nutzer-Limit setzen\n"
                "🎙️ Bitrate ändern\n"
                "🔒 Kanal sperren/entsperren\n"
                "➕ User einladen\n"
                "👢 User kicken\n"
                "👑 Admin übertragen"
            ),
            inline=False,
        )
    if REVIEW_CHANNEL_ID:
        embed.add_field(
            name="📅 Weekly Review",
            value=(
                "Jeden **Sonntag um 20:00 UTC** wird automatisch\n"
                "ein Wochen-Aktivitätsbericht gesendet.\n\n"
                "`/weeklyreview` — Manuell auslösen (nur Admins)"
            ),
            inline=False,
        )
    ### MUSIC MODULE START ###
    if MUSIC_ENABLED:
        embed.add_field(
            name="🎵 Musik",
            value=(
                "`/play <song/URL>` — Song abspielen/queuen\n"
                "`/search <titel>` — Suche & auswählen\n"
                "`/skip` — Song überspringen\n"
                "`/stop` — Stoppen & Kanal verlassen\n"
                "`/queue` — Warteschlange (mit Seiten)\n"
                "`/shuffle` — Queue mischen\n"
                "`/autoplay on/off` — Ähnliche Songs\n"
                "`/nowplaying` — Aktueller Song"
            ),
            inline=False,
        )
    ### MUSIC MODULE END ###
    if STREAMER_CHANNEL:
        embed.add_field(
            name="📺 Twitch Chat Commands",
            value=(
                "`!ping` — Bot-Test\n"
                "`!bot` — Bot-Info\n"
                "`!stats` — Deine Statistiken"
            ),
            inline=False,
        )
    embed.set_footer(text="Activity Tracker & AI Bot")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#            SLASH COMMANDS: TICKET SYSTEM
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(
    name="ticketpanel",
    description="🎫 Sendet das Ticket-Panel (nur Admins)",
)
@is_admin_user()
async def cmd_ticketpanel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 Support-Tickets",
        description=(
            "Hast du ein Problem, eine Frage oder ein Anliegen?\n\n"
            "Klicke auf den Button unten um ein **Ticket zu erstellen**.\n"
            "Unser Support-Team wird sich schnellstmöglich darum kümmern!\n\n"
            "**Bitte beachte:**\n"
            "• Erstelle nur ein Ticket pro Anliegen\n"
            "• Beschreibe dein Problem so genau wie möglich\n"
            "• Sei geduldig – wir helfen dir so schnell wir können"
        ),
        color=COLOR_TICKET,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text="Ticket System • Ein Ticket pro Anliegen")

    view = TicketOpenView()
    await interaction.response.send_message(embed=embed, view=view)


@d_bot.tree.command(
    name="closeticket",
    description="🔒 Schließt das aktuelle Ticket",
)
async def cmd_closeticket(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    ticket = open_tickets.get(channel_id)

    if not ticket:
        return await interaction.response.send_message(
            "❌ Dies ist kein Ticket-Kanal!", ephemeral=True
        )

    support_role = discord.utils.get(
        interaction.guild.roles, name=TICKET_SUPPORT_ROLE
    )
    is_support = (
        support_role in interaction.user.roles
        if support_role
        else interaction.user.guild_permissions.manage_channels
    )
    is_owner = interaction.user.id == ticket["user_id"]

    if not is_support and not is_owner:
        return await interaction.response.send_message(
            "❌ Nur Support oder der Ticket-Ersteller können das Ticket schließen!",
            ephemeral=True,
        )

    await interaction.response.send_message(
        "🔒 Ticket wird geschlossen...", ephemeral=True
    )
    await close_ticket(interaction.guild, channel_id, interaction.user)


@d_bot.tree.command(
    name="adduser",
    description="➕ Fügt einen User zum aktuellen Ticket hinzu",
)
@app_commands.describe(member="Der User der hinzugefügt werden soll")
async def cmd_adduser(interaction: discord.Interaction, member: discord.Member):
    channel_id = interaction.channel_id
    ticket = open_tickets.get(channel_id)

    if not ticket:
        return await interaction.response.send_message(
            "❌ Dies ist kein Ticket-Kanal!", ephemeral=True
        )

    support_role = discord.utils.get(
        interaction.guild.roles, name=TICKET_SUPPORT_ROLE
    )
    is_support = (
        support_role in interaction.user.roles
        if support_role
        else interaction.user.guild_permissions.manage_channels
    )

    if not is_support:
        return await interaction.response.send_message(
            f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann User hinzufügen!",
            ephemeral=True,
        )

    channel = interaction.guild.get_channel(channel_id)
    await channel.set_permissions(
        member,
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )

    embed = discord.Embed(
        title="➕ User hinzugefügt",
        description=f"{member.mention} wurde zum Ticket hinzugefügt von {interaction.user.mention}",
        color=COLOR_SUCCESS,
    )
    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(
    name="removeuser",
    description="➖ Entfernt einen User aus dem aktuellen Ticket",
)
@app_commands.describe(member="Der User der entfernt werden soll")
async def cmd_removeuser(interaction: discord.Interaction, member: discord.Member):
    channel_id = interaction.channel_id
    ticket = open_tickets.get(channel_id)

    if not ticket:
        return await interaction.response.send_message(
            "❌ Dies ist kein Ticket-Kanal!", ephemeral=True
        )

    support_role = discord.utils.get(
        interaction.guild.roles, name=TICKET_SUPPORT_ROLE
    )
    is_support = (
        support_role in interaction.user.roles
        if support_role
        else interaction.user.guild_permissions.manage_channels
    )

    if not is_support:
        return await interaction.response.send_message(
            f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann User entfernen!",
            ephemeral=True,
        )

    if member.id == ticket["user_id"]:
        return await interaction.response.send_message(
            "❌ Der Ticket-Ersteller kann nicht entfernt werden!",
            ephemeral=True,
        )

    channel = interaction.guild.get_channel(channel_id)
    await channel.set_permissions(member, overwrite=None)

    embed = discord.Embed(
        title="➖ User entfernt",
        description=f"{member.mention} wurde aus dem Ticket entfernt von {interaction.user.mention}",
        color=COLOR_ERROR,
    )
    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(
    name="tickets",
    description="📋 Zeigt alle offenen Tickets (nur Support)",
)
async def cmd_tickets(interaction: discord.Interaction):
    support_role = discord.utils.get(
        interaction.guild.roles, name=TICKET_SUPPORT_ROLE
    )
    is_support = (
        support_role in interaction.user.roles
        if support_role
        else interaction.user.guild_permissions.manage_channels
    )

    if not is_support:
        return await interaction.response.send_message(
            f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann alle Tickets sehen!",
            ephemeral=True,
        )

    if not open_tickets:
        return await interaction.response.send_message(
            "✅ Keine offenen Tickets!", ephemeral=True
        )

    lines = []
    for ch_id, ticket in open_tickets.items():
        try:
            channel = interaction.guild.get_channel(ch_id)
            ch_mention = channel.mention if channel else f"`{ch_id}`"
            user = interaction.guild.get_member(ticket.get("user_id") or 0)
            user_str = user.mention if user else f"`{ticket.get('user_id', '?')}`"
            claimed = ""
            if ticket.get("claimed_by"):
                claimer = interaction.guild.get_member(ticket["claimed_by"])
                claimed = f" • ✅ {claimer.display_name if claimer else '?'}"
            created_ts = int(
                datetime.datetime.fromisoformat(ticket["created_at"]).timestamp()
            )
        except Exception:
            continue  # kaputter Eintrag → überspringen statt Crashe
        lines.append(
            f"🎫 **#{int(ticket.get('ticket_id') or 0):04d}** {ch_mention}\n"
            f"╰ {user_str} • <t:{created_ts}:R>{claimed}\n"
            f"╰ *{ticket.get('betreff', '?')}*"
        )

    embed = discord.Embed(
        title=f"📋 Offene Tickets ({len(open_tickets)})",
        description="\n\n".join(lines),
        color=COLOR_TICKET,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#            SLASH COMMAND: /profile
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(
    name="profile",
    description="📊 Zeigt das vollständige Profil eines Users",
)
@app_commands.describe(member="Der User dessen Profil du sehen möchtest")
async def cmd_profile(
    interaction: discord.Interaction,
    member: discord.Member = None,
):
    if member is None:
        member = interaction.user

    await interaction.response.defer(ephemeral=True)

    data = load_data()
    uid = str(member.id)
    now = datetime.datetime.now(datetime.timezone.utc)
    today = datetime.date.today()

    # ── Basis Discord-Infos ──────────────────────────────
    account_created = member.created_at
    joined_at = member.joined_at
    account_age_days = (now - account_created).days
    join_age_days = (now - joined_at).days if joined_at else 0

    roles = [r for r in member.roles if r.name != "@everyone"]
    roles_str = (
        " ".join(r.mention for r in reversed(roles[:10]))
        if roles else "*Keine Rollen*"
    )

    # ── Activity Tracker Infos ───────────────────────────
    has_twitch = uid in data["users"]
    twitch_name = data["users"][uid]["twitch_name"] if has_twitch else None

    total_stream_days = len(data["streams"])
    total_present = 0
    current_streak = 0
    longest_streak = 0
    last_seen = "Noch nie"
    first_seen = "Unbekannt"
    grade = "—"
    grade_emoji = "❓"
    total_pct = 0.0
    rank = 0

    if has_twitch:
        total_present = sum(
            1 for d in data["streams"] if uid in data["streams"][d]
        )
        total_pct = (
            total_present / total_stream_days * 100
            if total_stream_days > 0 else 0
        )
        current_streak = get_current_streak(data, uid)
        longest_streak = get_longest_streak(data, uid)
        grade, grade_emoji, grade_color = get_activity_grade(total_pct)

        for date_str in sorted(data["streams"].keys(), reverse=True):
            if uid in data["streams"][date_str]:
                last_seen = date_str
                break
        for date_str in sorted(data["streams"].keys()):
            if uid in data["streams"][date_str]:
                first_seen = date_str
                break

        all_counts = {
            u: sum(1 for d in data["streams"] if u in data["streams"][d])
            for u in data["users"]
        }
        sorted_users = sorted(
            all_counts.items(), key=lambda x: x[1], reverse=True
        )
        rank = next(
            (i + 1 for i, (u, _) in enumerate(sorted_users) if u == uid),
            len(sorted_users),
        )
    else:
        grade_color = COLOR_INFO

    # ── Gambling / Coins ─────────────────────────────────
    balance = get_balance(member.id)
    bank = load_bank()
    uid_str = str(member.id)

    streak_val = bank.get(f"{uid_str}_daily_streak", 0)

    all_balances = {
        k: v for k, v in bank.items()
        if k.isdigit() and isinstance(v, int)
    }
    sorted_balances = sorted(
        all_balances.items(), key=lambda x: x[1], reverse=True
    )
    coin_rank = next(
        (i + 1 for i, (u, _) in enumerate(sorted_balances) if u == uid_str),
        len(sorted_balances),
    )

    # ── Angel-Daten ──────────────────────────────────────
    fishing_data = get_fishing_data(member.id)
    rod_key = fishing_data.get("rod", "basic")
    rod = FISHING_RODS[rod_key]
    fish_caught = fishing_data.get("total_caught", 0)
    fish_earned = fishing_data.get("total_earned", 0)
    legendary_caught = fishing_data.get("legendary_caught", 0)
    biggest_catch = fishing_data.get("biggest_catch_name", "Noch nichts")
    biggest_catch_val = fishing_data.get("biggest_catch", 0)

    # ── Ticket-Infos ─────────────────────────────────────
    user_tickets = [
        t for t in open_tickets.values()
        if t["user_id"] == member.id
    ]
    open_ticket_count = len(user_tickets)

    # ── Embed bauen ──────────────────────────────────────
    embed = discord.Embed(
        title=f"👤 Profil: {member.display_name}",
        color=grade_color,
        timestamp=now,
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    # Banner falls vorhanden
    if member.banner:
        embed.set_image(url=member.banner.url)

    # Discord Info
    embed.add_field(
        name="🏷️ Discord",
        value=(
            f"**Name:** {member.mention}\n"
            f"**Tag:** `{member.name}`\n"
            f"**ID:** `{member.id}`\n"
            f"**Bot:** {'✅ Ja' if member.bot else '❌ Nein'}"
        ),
        inline=True,
    )

    embed.add_field(
        name="📅 Dates",
        value=(
            f"**Account:** <t:{int(account_created.timestamp())}:D>\n"
            f"**Account-Alter:** {account_age_days} Tage\n"
            f"**Beigetreten:** <t:{int(joined_at.timestamp())}:D>\n"
            f"**Mitglied seit:** {join_age_days} Tage"
        ),
        inline=True,
    )

    embed.add_field(
        name="🎖️ Status",
        value=(
            f"**Status:** {str(member.status).capitalize()}\n"
            f"**Rollen:** {len(roles)}\n"
            f"**Boost:** {'✅' if member.premium_since else '❌'}"
        ),
        inline=True,
    )

    # Rollen
    embed.add_field(
        name=f"🏅 Rollen ({len(roles)})",
        value=roles_str[:1024],
        inline=False,
    )

    # Activity Tracker
    if has_twitch:
        activity_bar = make_progress_bar(total_present, total_stream_days, 10)
        flames = "🔥" * min(current_streak, 7) if current_streak > 0 else "—"
        embed.add_field(
            name="📺 Twitch Activity Tracker",
            value=(
                f"**Account:** [{twitch_name}](https://twitch.tv/{twitch_name})\n"
                f"**Rang:** {get_rank_emoji(rank)} von {len(data['users'])}\n"
                f"**Note:** {grade_emoji} **{grade}**\n"
                f"**Anwesend:** {total_present}/{total_stream_days} Tage "
                f"({total_pct:.0f}%)\n"
                f"{activity_bar}\n"
                f"**Streak:** {current_streak} Tage {flames}\n"
                f"**Max. Streak:** {longest_streak} Tage\n"
                f"**Zuletzt:** `{last_seen}`\n"
                f"**Dabei seit:** `{first_seen}`"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="📺 Twitch Activity Tracker",
            value="❌ Kein Twitch-Account verknüpft\n`/connect <twitch>` zum Verbinden",
            inline=False,
        )

    # Gambling
    embed.add_field(
        name="🪙 Gambling",
        value=(
            f"**Kontostand:** {balance:,} 🪙\n"
            f"**Coin-Rang:** #{coin_rank} von {len(all_balances)}\n"
            f"**Daily Streak:** {streak_val} Tage"
        ),
        inline=True,
    )

    # Angeln
    embed.add_field(
        name="🎣 Angeln",
        value=(
            f"**Angel:** {rod['name']}\n"
            f"**Gefangen:** {fish_caught:,}\n"
            f"**Verdient:** {fish_earned:,} 🪙\n"
            f"**Legendäre:** {legendary_caught}\n"
            f"**Bester Fang:** {biggest_catch} ({biggest_catch_val:,} 🪙)"
        ),
        inline=True,
    )

    # Tickets
    embed.add_field(
        name="🎫 Tickets",
        value=(
            f"**Offene Tickets:** {open_ticket_count}"
        ),
        inline=True,
    )

    embed.set_footer(
        text=f"Profil von {member.display_name} • {STREAMER_CHANNEL}",
        icon_url=member.display_avatar.url,
    )

    await interaction.followup.send(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: RUSSISCHES ROULETTE
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="rr", description="🔫 Starte Russisches Roulette! (Interaktiv)")
@app_commands.describe(einsatz="Einsatz pro Person (max 2.500)")
@is_gambling_channel()
async def cmd_rr(interaction: discord.Interaction, einsatz: int):
    if not await check_bet(interaction, einsatz):
        return
    if interaction.channel_id in active_rr_games:
        return await interaction.response.send_message(
            "❌ Bereits ein Spiel aktiv!", ephemeral=True
        )

    update_balance(interaction.user.id, -einsatz)
    active_rr_games[interaction.channel_id] = True
    view = RussianRouletteJoinView(interaction.user, einsatz, interaction.channel_id)

    embed = discord.Embed(
        title="🔫 Russisches Roulette!",
        description=(
            f"**{interaction.user.mention}** startet eine Runde!\n\n"
            f"💰 **Einsatz:** {einsatz:,} 🪙 pro Person\n"
            f"🔫 **Kammern:** {RR_CHAMBERS}\n"
            f"👥 **Spieler:** 1/{RR_MAX_PLAYERS}\n\n"
            f"**Spieler:**\n🔫 {interaction.user.mention}\n\n"
            f"**Aktionen im Spiel:**\n"
            f"🔫 Abdrücken – Normal schießen\n"
            f"🔄 Trommel drehen – Kammern mischen (musst trotzdem schießen!)\n"
            f"👉 Weitergeben – Nächster MUSS schießen\n"
            f"🎲 Doppelt – 2 Kammern prüfen, Bonus wenn du überlebst!\n\n"
            f"*Verlierer bekommen 60s Timeout!* 😈"
        ),
        color=COLOR_RR,
    )
    embed.set_footer(text=f"Min: {RR_MIN_PLAYERS} • Max: {RR_MAX_PLAYERS} • Host klickt ▶️")
    await interaction.response.send_message(embed=embed, view=view)

# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: MUSIK-BATTLE
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="musicbattle", description="🎵 Fordere jemanden zum Musik-Battle heraus!")
@app_commands.describe(
    gegner="Der User den du herausforderst",
    einsatz="Einsatz pro Person (max 2.500)",
)
@is_gambling_channel()
async def cmd_musicbattle(interaction: discord.Interaction, gegner: discord.Member, einsatz: int):
    if not MUSIC_ENABLED:
        return await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
    if gegner.id == interaction.user.id:
        return await interaction.response.send_message("❌ Du kannst dich nicht selbst herausfordern!", ephemeral=True)
    if gegner.bot:
        return await interaction.response.send_message("❌ Du kannst keinen Bot herausfordern!", ephemeral=True)
    if not await check_bet(interaction, einsatz):
        return
    if interaction.channel_id in active_battles:
        return await interaction.response.send_message(
            "❌ Es läuft bereits ein Musik-Battle in diesem Kanal!", ephemeral=True
        )

    update_balance(interaction.user.id, -einsatz)
    active_battles[interaction.channel_id] = True
    view = MusicBattleAcceptView(interaction.user, gegner, einsatz, interaction.channel_id)

    embed = discord.Embed(
        title="🎵 Musik-Battle Herausforderung!",
        description=(
            f"{interaction.user.mention} fordert {gegner.mention} zum **Musik-Battle** heraus!\n\n"
            f"💰 **Einsatz:** {einsatz:,} 🪙 pro Person\n"
            f"🏆 **Pot:** {einsatz * 2:,} 🪙\n\n"
            f"**So funktioniert's:**\n"
            f"1️⃣ Beide wählen einen Song\n"
            f"2️⃣ Songs werden präsentiert\n"
            f"3️⃣ Alle im Kanal stimmen ab\n"
            f"4️⃣ Gewinner bekommt den Pot!\n\n"
            f"🗳️ *Voter bekommen +10 🪙 Belohnung!*"
        ),
        color=COLOR_BATTLE,
    )
    embed.set_footer(text="Läuft in 60 Sekunden ab")
    await interaction.response.send_message(content=gegner.mention, embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: ABWESENHEITS-SYSTEM
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(
    name="absent",
    description="📋 Melde dich für einen Stream ab (Streak bleibt erhalten)",
)
@app_commands.describe(
    datum="Datum der Abwesenheit (TT.MM.JJJJ) — leer = heute",
    grund="Grund für die Abwesenheit",
)
@is_allowed_channel()
async def cmd_absent(
    interaction: discord.Interaction,
    grund: str = "Kein Grund angegeben",
    datum: str = None,
):
    if not any(role.name == MOD_ROLE_NAME for role in interaction.user.roles):
        return await interaction.response.send_message(
            f"❌ Du benötigst die Rolle **{MOD_ROLE_NAME}**.",
            ephemeral=True,
        )

    data = load_data()
    uid = str(interaction.user.id)

    if uid not in data["users"]:
        return await interaction.response.send_message(
            "❌ Du hast keinen verknüpften Twitch-Account!\nNutze `/connect` zuerst.",
            ephemeral=True,
        )

    if datum:
        try:
            parsed_date = datetime.datetime.strptime(datum, "%d.%m.%Y").date()
            date_str = str(parsed_date)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Ungültiges Datum! Format: **TT.MM.JJJJ** (z.B. 15.07.2025)",
                ephemeral=True,
            )
    else:
        date_str = str(datetime.date.today())
        parsed_date = datetime.date.today()

    # Prüfen ob in der Vergangenheit
    if parsed_date < datetime.date.today():
        return await interaction.response.send_message(
            "❌ Du kannst dich nicht rückwirkend abmelden!",
            ephemeral=True,
        )

    # Prüfen ob bereits abgemeldet
    if is_user_absent(uid, date_str):
        return await interaction.response.send_message(
            f"❌ Du bist bereits für **{datum or 'heute'}** abgemeldet!",
            ephemeral=True,
        )

    add_absence(uid, date_str, grund)

    # Streak-Schutz: User als "anwesend" markieren für diesen Tag
    if date_str not in data["streams"]:
        data["streams"][date_str] = []
    if uid not in data["streams"][date_str]:
        data["streams"][date_str].append(uid)
        save_data(data)

    day_name = WEEKDAY_SHORT[parsed_date.weekday()]
    embed = discord.Embed(
        title="📋 Abwesenheit eingetragen",
        description=(
            f"Du bist für **{parsed_date.strftime('%d.%m.%Y')}** ({day_name}) abgemeldet.\n\n"
            f"📝 **Grund:** {grund}\n"
            f"✅ **Streak wird geschützt!**\n\n"
            f"*Du wirst als anwesend markiert obwohl du nicht im Chat warst.*"
        ),
        color=COLOR_ABSENCE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="Abwesenheits-System • Nur für Mods")

    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Log senden
    log_embed = discord.Embed(
        title="📋 Abwesenheit gemeldet",
        color=COLOR_ABSENCE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    log_embed.add_field(name="👤 User", value=interaction.user.mention, inline=True)
    log_embed.add_field(name="📅 Datum", value=f"`{date_str}` ({day_name})", inline=True)
    log_embed.add_field(name="📝 Grund", value=grund, inline=False)
    log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await send_log(log_embed, d_bot)


@d_bot.tree.command(
    name="unabsent",
    description="📋 Entferne eine Abwesenheitsmeldung",
)
@app_commands.describe(datum="Datum der Abwesenheit (TT.MM.JJJJ) — leer = heute")
@is_allowed_channel()
async def cmd_unabsent(interaction: discord.Interaction, datum: str = None):
    if not any(role.name == MOD_ROLE_NAME for role in interaction.user.roles):
        return await interaction.response.send_message(
            f"❌ Du benötigst die Rolle **{MOD_ROLE_NAME}**.",
            ephemeral=True,
        )

    uid = str(interaction.user.id)

    if datum:
        try:
            parsed_date = datetime.datetime.strptime(datum, "%d.%m.%Y").date()
            date_str = str(parsed_date)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Ungültiges Datum! Format: **TT.MM.JJJJ**",
                ephemeral=True,
            )
    else:
        date_str = str(datetime.date.today())

    if not is_user_absent(uid, date_str):
        return await interaction.response.send_message(
            f"❌ Du bist nicht für **{datum or 'heute'}** abgemeldet!",
            ephemeral=True,
        )

    remove_absence(uid, date_str)

    embed = discord.Embed(
        title="✅ Abwesenheit entfernt",
        description=f"Deine Abmeldung für **{date_str}** wurde entfernt.",
        color=COLOR_SUCCESS,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(
    name="absences",
    description="📋 Zeigt deine anstehenden Abwesenheiten",
)
@is_allowed_channel()
async def cmd_absences(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    user_absences = get_user_absences(uid)

    today = str(datetime.date.today())
    upcoming = [a for a in user_absences if a["date"] >= today]

    if not upcoming:
        return await interaction.response.send_message(
            "✅ Du hast keine anstehenden Abwesenheiten!",
            ephemeral=True,
        )

    lines = []
    for a in upcoming[:15]:
        parsed = datetime.date.fromisoformat(a["date"])
        day_name = WEEKDAY_SHORT[parsed.weekday()]
        lines.append(
            f"📅 **{parsed.strftime('%d.%m.%Y')}** ({day_name})\n"
            f"╰ 📝 {a['reason']}"
        )

    embed = discord.Embed(
        title="📋 Deine Abwesenheiten",
        description="\n\n".join(lines),
        color=COLOR_ABSENCE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text=f"{len(upcoming)} anstehende Abmeldung(en)")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: RADIO
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="radio", description="📻 Starte einen Radiosender (Preset oder Suche)")
@app_commands.describe(sender="Name des Senders zum Suchen (leer = Preset-Liste)")
async def cmd_radio(interaction: discord.Interaction, sender: str = None):
    if not MUSIC_ENABLED:
        return await interaction.response.send_message(
            "❌ Musik-Modul deaktiviert.", ephemeral=True
        )
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message(
            "❌ Du musst in einem Voice-Kanal sein!", ephemeral=True
        )

    if sender is None:
        # Preset-Liste anzeigen
        embed = discord.Embed(
            title="📻 Radio – Sender auswählen",
            description="Wähle einen Sender aus dem Menü oder nutze `/radio <name>` zum Suchen!",
            color=COLOR_RADIO,
        )

        stations_list = "\n".join(
            f"{s['emoji']} **{s['name']}** — {s['description']}"
            for s in RADIO_STATIONS.values()
        )
        embed.add_field(name="📡 Preset-Sender", value=stations_list, inline=False)
        embed.set_footer(text="💡 Tipp: /radio Bayern 3 – sucht nach Bayern 3")

        view = RadioPresetSelectView(interaction.user)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    else:
        # Online-Suche
        await interaction.response.defer()

        stations = await search_radio_stations_list(sender, limit=10)

        if not stations:
            await interaction.followup.send(
                f"❌ Kein Radiosender für **{sender}** gefunden!\n"
                f"Versuche einen anderen Suchbegriff.",
                ephemeral=True,
            )
            return

        if len(stations) == 1:
            # Direkt abspielen
            state = get_music_state(interaction.guild_id, d_bot)
            await start_radio_stream(interaction, stations[0], state)
        else:
            # Auswahl anzeigen
            lines = []
            for i, station in enumerate(stations[:10], 1):
                country = station.get("country", "")
                bitrate = station.get("bitrate", 0)
                info = country
                if bitrate > 0:
                    info += f" • {bitrate}kbps" if info else f"{bitrate}kbps"
                lines.append(
                    f"`{i}.` **{station['name']}**"
                    + (f"\n╰ {info}" if info else "")
                )

            embed = discord.Embed(
                title=f"📻 Suchergebnisse: {sender}",
                description="\n".join(lines),
                color=COLOR_RADIO,
            )
            embed.set_footer(text="Wähle einen Sender aus dem Menü!")

            view = RadioSearchSelectView(interaction.user, stations)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@d_bot.tree.command(name="stations", description="📻 Zeigt alle Preset-Radiosender")
async def cmd_stations(interaction: discord.Interaction):
    lines = []
    for key, station in RADIO_STATIONS.items():
        lines.append(
            f"{station['emoji']} **{station['name']}**\n"
            f"╰ {station['description']}"
        )

    embed = discord.Embed(
        title="📻 Preset-Radiosender",
        description="\n\n".join(lines),
        color=COLOR_RADIO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="💡 Tipp",
        value=(
            "`/radio` — Preset-Sender auswählen\n"
            "`/radio Bayern 3` — Nach Sender suchen\n"
            "`/radio Jazz` — Genre suchen"
        ),
        inline=False,
    )
    embed.set_footer(text="📻 Radio System • Tausende Sender verfügbar!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: SONG QUIZ
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="songquiz", description="🎵 Starte ein Song-Quiz im Voice-Kanal!")
@app_commands.describe(runden="Anzahl der Runden (1-20)")
async def cmd_songquiz(interaction: discord.Interaction, runden: int = 5):
    if not MUSIC_ENABLED:
        return await interaction.response.send_message(
            "❌ Musik-Modul deaktiviert.", ephemeral=True
        )
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message(
            "❌ Du musst in einem Voice-Kanal sein!", ephemeral=True
        )
    if runden < 1 or runden > 20:
        return await interaction.response.send_message(
            "❌ Rundenanzahl muss zwischen 1 und 20 sein!", ephemeral=True
        )
    if interaction.channel_id in active_quizzes:
        return await interaction.response.send_message(
            "❌ Es läuft bereits ein Song-Quiz in diesem Kanal!", ephemeral=True
        )

    active_quizzes[interaction.channel_id] = True

    embed = discord.Embed(
        title="🎵 Song Quiz wird vorbereitet...",
        description=(
            f"**{runden} Runden** • **{SONG_QUIZ_DURATION}s** pro Song\n\n"
            f"🎧 Songs werden im Voice-Kanal abgespielt.\n"
            f"📝 Schreibt eure Antworten in diesen Chat!\n\n"
            f"**Punkte:**\n"
            f"🎯 Titel erraten: **{SONG_QUIZ_POINTS['title']}P**\n"
            f"🎤 Künstler erraten: **{SONG_QUIZ_POINTS['artist']}P**\n"
            f"⭐ Beides: **{SONG_QUIZ_POINTS['both']}P**\n\n"
            f"🏆 Top 3 bekommen Coins!"
        ),
        color=COLOR_QUIZ,
    )
    embed.set_footer(text="Startet in 5 Sekunden...")
    await interaction.response.send_message(embed=embed)

    await asyncio.sleep(5)

    await run_song_quiz(
        interaction.channel,
        interaction.guild,
        runden,
        interaction.user,
    )


@d_bot.tree.command(name="quizstop", description="🎵 Stoppt das aktuelle Song-Quiz")
async def cmd_quizstop(interaction: discord.Interaction):
    if interaction.channel_id not in active_quizzes:
        return await interaction.response.send_message(
            "❌ Kein Song-Quiz aktiv in diesem Kanal!", ephemeral=True
        )

    active_quizzes.pop(interaction.channel_id, None)

    state = get_music_state(interaction.guild_id, d_bot)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.stop()

    await interaction.response.send_message(
        embed=discord.Embed(
            title="⏹️ Song Quiz gestoppt!",
            description="Das Quiz wurde abgebrochen.",
            color=COLOR_ERROR,
        )
    )

# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: 4 GEWINNT
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="connect4", description="🟡🔴 Fordere jemanden zu 4 Gewinnt heraus!")
@app_commands.describe(
    gegner="Der User den du herausforderst",
    einsatz="Einsatz pro Person (max 2.500)",
)
@is_gambling_channel()
async def cmd_connect4(interaction: discord.Interaction, gegner: discord.Member, einsatz: int):
    if gegner.id == interaction.user.id:
        return await interaction.response.send_message("❌ Du kannst dich nicht selbst herausfordern!", ephemeral=True)
    if gegner.bot:
        return await interaction.response.send_message("❌ Du kannst keinen Bot herausfordern!", ephemeral=True)
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return

    update_balance(interaction.user.id, -einsatz)
    active_games[interaction.user.id] = "4 Gewinnt"
    view = Connect4AcceptView(interaction.user, gegner, einsatz)

    embed = discord.Embed(
        title="🟡🔴 4 Gewinnt – Herausforderung!",
        description=(
            f"{interaction.user.mention} fordert {gegner.mention} heraus!\n\n"
            f"💰 **Einsatz:** {einsatz:,} 🪙 pro Person\n"
            f"🏆 **Pot:** {einsatz * 2:,} 🪙 (5% Gebühr)\n\n"
            f"**Regeln:**\n"
            f"• Abwechselnd Steine in Spalten werfen\n"
            f"• Steine fallen automatisch nach unten\n"
            f"• 4 in einer Reihe (horizontal/vertikal/diagonal) gewinnt!\n\n"
            f"{gegner.mention}, nimmst du an?"
        ),
        color=COLOR_CONNECT4,
    )
    embed.set_footer(text="Läuft in 60 Sekunden ab")
    await interaction.response.send_message(content=gegner.mention, embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: TIC TAC TOE
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="tictactoe", description="❌⭕ Fordere jemanden zu Tic Tac Toe heraus!")
@app_commands.describe(
    gegner="Der User den du herausforderst",
    einsatz="Einsatz pro Person (max 2.500)",
)
@is_gambling_channel()
async def cmd_tictactoe(interaction: discord.Interaction, gegner: discord.Member, einsatz: int):
    if gegner.id == interaction.user.id:
        return await interaction.response.send_message("❌ Du kannst dich nicht selbst herausfordern!", ephemeral=True)
    if gegner.bot:
        return await interaction.response.send_message("❌ Du kannst keinen Bot herausfordern!", ephemeral=True)
    if not await check_active_game(interaction):
        return
    if not await check_bet(interaction, einsatz):
        return

    update_balance(interaction.user.id, -einsatz)
    active_games[interaction.user.id] = "Tic Tac Toe"
    view = TicTacToeAcceptView(interaction.user, gegner, einsatz)

    embed = discord.Embed(
        title="❌⭕ Tic Tac Toe – Herausforderung!",
        description=(
            f"{interaction.user.mention} fordert {gegner.mention} heraus!\n\n"
            f"💰 **Einsatz:** {einsatz:,} 🪙 pro Person\n"
            f"🏆 **Pot:** {einsatz * 2:,} 🪙 (5% Gebühr)\n\n"
            f"**Regeln:**\n"
            f"• Abwechselnd X und O setzen\n"
            f"• 3 in einer Reihe gewinnt!\n\n"
            f"{gegner.mention}, nimmst du an?"
        ),
        color=COLOR_TICTACTOE,
    )
    embed.set_footer(text="Läuft in 60 Sekunden ab")
    await interaction.response.send_message(content=gegner.mention, embed=embed, view=view)


# ══════════════════════════════════════════════════════════
#      SLASH COMMANDS: JOBS / MINIGAMES
# ══════════════════════════════════════════════════════════

# ── Generische Job-Funktion ──────────────────────────────

async def run_simple_job(
    interaction: discord.Interaction,
    job_key: str,
    job_name: str,
    job_emoji: str,
    items: list,
    tools: dict,
    cooldown_upgrades: dict,
    animations: list[list[str]],
):
    """Generische Funktion für einfache Sammel-Jobs."""
    user_id = interaction.user.id
    job_data = get_job_data(user_id, job_key)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cooldown = get_job_cooldown(job_data, cooldown_upgrades)

    last_time = job_data.get("last_action_time", 0)
    if now - last_time < cooldown:
        next_time = int(last_time + cooldown)
        return await interaction.response.send_message(
            f"⏳ Du kannst <t:{next_time}:R> wieder {job_name.lower()}!",
            ephemeral=True,
        )

    job_data["last_action_time"] = now
    save_job_data(user_id, job_key, job_data)

    tool_key = job_data.get("tool", list(tools.keys())[0])
    tool = tools[tool_key]

    # Animation
    frames = random.choice(animations)
    embed = discord.Embed(
        title=f"{job_emoji} {interaction.user.display_name} {job_name.lower()}t...",
        description=f"**Werkzeug:** {tool['name']}\n\n{frames[0]}",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed)
    for frame in frames[1:]:
        await asyncio.sleep(0.8)
        embed.description = f"**Werkzeug:** {tool['name']}\n\n{frame}"
        await interaction.edit_original_response(embed=embed)
    await asyncio.sleep(0.5)

    # Item bestimmen
    result = pick_job_item(items, tool_key, tools)
    rarity_color = FISH_RARITY_COLORS.get(result["rarity"], COLOR_INFO)
    rarity_name = FISH_RARITY_NAMES.get(result["rarity"], "")

    # Daten updaten
    job_data["total_earned"] = job_data.get("total_earned", 0) + result["value"]
    job_data["total_actions"] = job_data.get("total_actions", 0) + 1
    if result["value"] > job_data.get("best_value", 0):
        job_data["best_value"] = result["value"]
        job_data["best_name"] = result["name"]
    if result["rarity"] == "legendary":
        job_data["legendary_count"] = job_data.get("legendary_count", 0) + 1

    update_balance(user_id, result["value"])
    save_job_data(user_id, job_key, job_data)

    next_time = int(now + cooldown)

    if result["rarity"] == "legendary":
        title = f"🌟 LEGENDÄR! {result['emoji']} {result['name']}!"
    elif result["rarity"] == "epic":
        title = f"🟪 EPISCH! {result['emoji']} {result['name']}!"
    elif result["rarity"] == "rare":
        title = f"🟦 Selten! {result['emoji']} {result['name']}!"
    else:
        title = f"{result['emoji']} {result['name']} erhalten!"

    embed = discord.Embed(
        title=title,
        description=(
            f"💰 Wert: **{result['value']:,} 🪙**\n"
            f"📊 Seltenheit: {rarity_name}\n\n"
            f"⏱️ Nächster Versuch: <t:{next_time}:R>"
        ),
        color=rarity_color,
    )
    embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙 • {tool['name']}")
    await interaction.edit_original_response(embed=embed)


# ── Holzfällen ───────────────────────────────────────────

WOOD_ANIMATIONS = [
    ["🌳🪓", "🌳💨🪓", "🌳🪵💨", "🪵🪵✅"],
    ["🪓🌲", "🪓💥🌲", "🪓🌲💨", "🪵✅"],
    ["🌳🪓💪", "🌳💥", "🪵🪵", "🪵✨"],
]

@d_bot.tree.command(name="chop", description="🪓 Bäume fällen und Holz sammeln!")
@is_fishing_channel()
async def cmd_chop(interaction: discord.Interaction):
    await run_simple_job(
        interaction, "woodcutting", "Holzfällen", "🪓",
        WOODCUTTING_TREES, WOODCUTTING_AXES, WOODCUTTING_COOLDOWN_UPGRADES,
        WOOD_ANIMATIONS,
    )


# ── Bergbau ──────────────────────────────────────────────

MINE_ANIMATIONS = [
    ["⛏️🪨", "⛏️💥🪨", "⛏️💎💨", "💎✅"],
    ["🪨⛏️", "🪨💥⛏️", "✨🪨⛏️", "💎✨"],
    ["⛏️💪🪨", "💥🪨", "⛏️✨", "💎🎉"],
]

@d_bot.tree.command(name="mine", description="⛏️ Erze abbauen und Mineralien sammeln!")
@is_fishing_channel()
async def cmd_mine(interaction: discord.Interaction):
    await run_simple_job(
        interaction, "mining", "Bergbau", "⛏️",
        MINING_ORES, MINING_PICKAXES, MINING_COOLDOWN_UPGRADES,
        MINE_ANIMATIONS,
    )


# ── Jagen ────────────────────────────────────────────────

HUNT_ANIMATIONS = [
    ["🏹👀", "🏹🌿👀", "🏹💨🎯", "🎯✅"],
    ["👀🌲", "🏹🌲", "🏹💨", "🎯🦌"],
    ["🌿🏹", "🌿👀🏹", "💨🏹", "🎯✨"],
]

@d_bot.tree.command(name="hunt", description="🏹 Auf die Jagd gehen!")
@is_fishing_channel()
async def cmd_hunt(interaction: discord.Interaction):
    await run_simple_job(
        interaction, "hunting", "Jagen", "🏹",
        HUNTING_ANIMALS, HUNTING_BOWS, HUNTING_COOLDOWN_UPGRADES,
        HUNT_ANIMATIONS,
    )


# ── Schmieden ────────────────────────────────────────────

@d_bot.tree.command(name="smith", description="⚒️ Schmiede Gegenstände! (Reaktionsspiel)")
@is_fishing_channel()
async def cmd_smith(interaction: discord.Interaction):
    user_id = interaction.user.id
    job_data = get_job_data(user_id, "smithing")
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cooldown = get_job_cooldown(job_data, SMITHING_COOLDOWN_UPGRADES)

    last_time = job_data.get("last_action_time", 0)
    if now - last_time < cooldown:
        next_time = int(last_time + cooldown)
        return await interaction.response.send_message(
            f"⏳ Du kannst <t:{next_time}:R> wieder schmieden!", ephemeral=True
        )

    job_data["last_action_time"] = now
    save_job_data(user_id, "smithing", job_data)

    hammer_key = job_data.get("tool", "stone")
    hammer = SMITHING_HAMMERS[hammer_key]
    item = random.choice(SMITHING_ITEMS)

    view = SmithingView(user_id, item, hammer)
    embed = discord.Embed(
        title=f"⚒️ Schmieden – {item['emoji']} {item['name']}",
        description=(
            f"Klicke so schnell wie möglich auf **{view.target_emoji}**!\n\n"
            f"🔨 Schläge: **0/{view.required_hits}**\n"
            f"🛠️ Hammer: {hammer['name']}\n"
            f"⏱️ Schneller = bessere Qualität!\n\n"
            f"*Falscher Klick = -1 Schlag!*"
        ),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, view=view)


# ── Kochen ───────────────────────────────────────────────

@d_bot.tree.command(name="cook", description="🍳 Koche ein Gericht! (Rezept-Merkspiel)")
@is_fishing_channel()
async def cmd_cook(interaction: discord.Interaction):
    user_id = interaction.user.id
    job_data = get_job_data(user_id, "cooking")
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cooldown = get_job_cooldown(job_data, COOKING_COOLDOWN_UPGRADES)

    last_time = job_data.get("last_action_time", 0)
    if now - last_time < cooldown:
        next_time = int(last_time + cooldown)
        return await interaction.response.send_message(
            f"⏳ Du kannst <t:{next_time}:R> wieder kochen!", ephemeral=True
        )

    job_data["last_action_time"] = now
    save_job_data(user_id, "cooking", job_data)

    tool_key = job_data.get("tool", "basic")
    tool = COOKING_TOOLS[tool_key]
    recipe = random.choice(COOKING_RECIPES)

    # Rezept kurz anzeigen
    ingredients_str = " ".join(recipe["ingredients"])
    embed = discord.Embed(
        title=f"📖 Rezept: {recipe['emoji']} {recipe['name']}",
        description=(
            f"**Merke dir die Zutaten!**\n\n"
            f"📝 Zutaten: {ingredients_str}\n\n"
            f"🍳 Werkzeug: {tool['name']}\n"
            f"⏱️ Du hast **3 Sekunden** zum Merken!"
        ),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(3)

    view = CookingView(user_id, recipe, tool)
    embed = discord.Embed(
        title=f"🍳 Kochen – {recipe['emoji']} {recipe['name']}",
        description=(
            f"Wähle die richtigen Zutaten aus!\n\n"
            f"📝 Gewählt: *noch nichts*\n"
            f"📊 0/{len(recipe['ingredients'])} Zutaten\n\n"
            f"*Klicke ✅ Fertig wenn du alle hast!*"
        ),
        color=COLOR_INFO,
    )
    await interaction.edit_original_response(embed=embed, view=view)


# ── Job Upgrades ─────────────────────────────────────────

@d_bot.tree.command(name="jobupgrade", description="⬆️ Verbessere dein Werkzeug für einen Job!")
@app_commands.describe(job="Welchen Job upgraden?")
@app_commands.choices(job=[
    app_commands.Choice(name="🪓 Holzfällen", value="woodcutting"),
    app_commands.Choice(name="⛏️ Bergbau", value="mining"),
    app_commands.Choice(name="🏹 Jagen", value="hunting"),
    app_commands.Choice(name="⚒️ Schmieden", value="smithing"),
    app_commands.Choice(name="🍳 Kochen", value="cooking"),
])
@is_fishing_channel()
async def cmd_jobupgrade(interaction: discord.Interaction, job: str):
    user_id = interaction.user.id
    job_data = get_job_data(user_id, job)

    tools = {
        "woodcutting": WOODCUTTING_AXES,
        "mining": MINING_PICKAXES,
        "hunting": HUNTING_BOWS,
        "smithing": SMITHING_HAMMERS,
        "cooking": COOKING_TOOLS,
    }[job]

    tool_order = list(tools.keys())
    current_key = job_data.get("tool", tool_order[0])
    current_idx = tool_order.index(current_key)

    if current_idx >= len(tool_order) - 1:
        return await interaction.response.send_message(
            "🌟 Du hast bereits das beste Werkzeug!", ephemeral=True
        )

    next_key = tool_order[current_idx + 1]
    next_tool = tools[next_key]
    current_tool = tools[current_key]
    balance = get_balance(user_id)

    if balance < next_tool["cost"]:
        return await interaction.response.send_message(
            f"❌ **{next_tool['name']}** kostet **{next_tool['cost']:,} 🪙**\n"
            f"💰 Du hast: **{balance:,} 🪙**\n"
            f"❌ Dir fehlen: **{next_tool['cost'] - balance:,} 🪙**",
            ephemeral=True,
        )

    update_balance(user_id, -next_tool["cost"])
    job_data["tool"] = next_key
    save_job_data(user_id, job, job_data)

    embed = discord.Embed(
        title="⬆️ Werkzeug verbessert!",
        description=(
            f"**{current_tool['name']}** → **{next_tool['name']}**\n\n"
            f"📈 Wert-Bonus: ×{next_tool['bonus']}\n"
            f"💰 Bezahlt: **{next_tool['cost']:,} 🪙**"
        ),
        color=COLOR_SUCCESS,
    )
    embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="jobcooldown", description="⏱️ Verkürze den Cooldown eines Jobs!")
@app_commands.describe(job="Welchen Job upgraden?")
@app_commands.choices(job=[
    app_commands.Choice(name="🪓 Holzfällen", value="woodcutting"),
    app_commands.Choice(name="⛏️ Bergbau", value="mining"),
    app_commands.Choice(name="🏹 Jagen", value="hunting"),
    app_commands.Choice(name="⚒️ Schmieden", value="smithing"),
    app_commands.Choice(name="🍳 Kochen", value="cooking"),
])
@is_fishing_channel()
async def cmd_jobcooldown(interaction: discord.Interaction, job: str):
    user_id = interaction.user.id
    job_data = get_job_data(user_id, job)

    cd_upgrades = {
        "woodcutting": WOODCUTTING_COOLDOWN_UPGRADES,
        "mining": MINING_COOLDOWN_UPGRADES,
        "hunting": HUNTING_COOLDOWN_UPGRADES,
        "smithing": SMITHING_COOLDOWN_UPGRADES,
        "cooking": COOKING_COOLDOWN_UPGRADES,
    }[job]

    current_level = job_data.get("cooldown_level", 0)
    max_level = max(cd_upgrades.keys())

    if current_level >= max_level:
        return await interaction.response.send_message(
            "🌟 Maximales Cooldown-Level erreicht!", ephemeral=True
        )

    next_level = current_level + 1
    next_info = cd_upgrades[next_level]
    current_info = cd_upgrades[current_level]
    balance = get_balance(user_id)

    if balance < next_info["cost"]:
        return await interaction.response.send_message(
            f"❌ **{next_info['name']}** kostet **{next_info['cost']:,} 🪙**\n"
            f"💰 Du hast: **{balance:,} 🪙**",
            ephemeral=True,
        )

    update_balance(user_id, -next_info["cost"])
    job_data["cooldown_level"] = next_level
    save_job_data(user_id, job, job_data)

    embed = discord.Embed(
        title="⏱️ Cooldown verbessert!",
        description=(
            f"**{current_info['name']}** → **{next_info['name']}**\n\n"
            f"⏱️ Cooldown: {current_info['cooldown']}s → **{next_info['cooldown']}s**\n"
            f"💰 Bezahlt: **{next_info['cost']:,} 🪙**"
        ),
        color=COLOR_SUCCESS,
    )
    embed.set_footer(text=f"Kontostand: {get_balance(user_id):,} 🪙")
    await interaction.response.send_message(embed=embed)


@d_bot.tree.command(name="jobstats", description="📊 Zeigt deine Job-Statistiken")
@app_commands.describe(job="Welcher Job?")
@app_commands.choices(job=[
    app_commands.Choice(name="🪓 Holzfällen", value="woodcutting"),
    app_commands.Choice(name="⛏️ Bergbau", value="mining"),
    app_commands.Choice(name="🏹 Jagen", value="hunting"),
    app_commands.Choice(name="⚒️ Schmieden", value="smithing"),
    app_commands.Choice(name="🍳 Kochen", value="cooking"),
])
@is_fishing_channel()
async def cmd_jobstats(interaction: discord.Interaction, job: str):
    job_data = get_job_data(interaction.user.id, job)

    tools = {
        "woodcutting": WOODCUTTING_AXES,
        "mining": MINING_PICKAXES,
        "hunting": HUNTING_BOWS,
        "smithing": SMITHING_HAMMERS,
        "cooking": COOKING_TOOLS,
    }[job]
    cd_upgrades = {
        "woodcutting": WOODCUTTING_COOLDOWN_UPGRADES,
        "mining": MINING_COOLDOWN_UPGRADES,
        "hunting": HUNTING_COOLDOWN_UPGRADES,
        "smithing": SMITHING_COOLDOWN_UPGRADES,
        "cooking": COOKING_COOLDOWN_UPGRADES,
    }[job]
    job_names = {
        "woodcutting": "🪓 Holzfällen",
        "mining": "⛏️ Bergbau",
        "hunting": "🏹 Jagen",
        "smithing": "⚒️ Schmieden",
        "cooking": "🍳 Kochen",
    }

    tool_key = job_data.get("tool", list(tools.keys())[0])
    tool = tools[tool_key]
    cd_level = job_data.get("cooldown_level", 0)
    cd_info = cd_upgrades.get(cd_level, cd_upgrades[0])

    embed = discord.Embed(
        title=f"📊 {job_names[job]} – Statistiken",
        color=COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="🛠️ Werkzeug", value=tool["name"], inline=True)
    embed.add_field(name="⏱️ Cooldown", value=f"**{cd_info['cooldown']}s**", inline=True)
    embed.add_field(name="📊 Aktionen", value=f"**{job_data.get('total_actions', 0):,}**", inline=True)
    embed.add_field(name="💰 Verdient", value=f"**{job_data.get('total_earned', 0):,} 🪙**", inline=True)
    embed.add_field(
        name="🏆 Bester Fund",
        value=f"**{job_data.get('best_name', 'Noch nichts')}** ({job_data.get('best_value', 0):,} 🪙)",
        inline=True,
    )
    embed.add_field(name="🌟 Legendäre", value=f"**{job_data.get('legendary_count', 0)}**", inline=True)

    # Nächstes Upgrade
    tool_order = list(tools.keys())
    current_idx = tool_order.index(tool_key)
    if current_idx < len(tool_order) - 1:
        next_tool = tools[tool_order[current_idx + 1]]
        embed.add_field(
            name="⬆️ Nächstes Werkzeug",
            value=f"{next_tool['name']} — **{next_tool['cost']:,} 🪙**\n`/jobupgrade {job}`",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@d_bot.tree.command(name="jobs", description="📋 Zeigt alle verfügbaren Jobs")
@is_fishing_channel()
async def cmd_jobs(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📋 Verfügbare Jobs",
        description="Verdiene Coins durch Arbeiten! Alle Jobs haben Cooldowns und Upgrades.",
        color=COLOR_INFO,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="🪓 Holzfällen (`/chop`)",
        value="Fälle Bäume und sammle Holz.\n*Einfacher Sammel-Job*",
        inline=True,
    )
    embed.add_field(
        name="⛏️ Bergbau (`/mine`)",
        value="Baue Erze und Mineralien ab.\n*Einfacher Sammel-Job*",
        inline=True,
    )
    embed.add_field(
        name="🏹 Jagen (`/hunt`)",
        value="Geh auf die Jagd nach Tieren.\n*Einfacher Sammel-Job*",
        inline=True,
    )
    embed.add_field(
        name="⚒️ Schmieden (`/smith`)",
        value="Schmiede Gegenstände!\nKlicke schnell auf das richtige Symbol.\n*Reaktionsspiel – Skill = mehr Geld!*",
        inline=True,
    )
    embed.add_field(
        name="🍳 Kochen (`/cook`)",
        value="Koche Gerichte nach Rezept!\nMerke dir die Zutaten und wähle sie.\n*Gedächtnisspiel – Genauigkeit = mehr Geld!*",
        inline=True,
    )
    embed.add_field(
        name="🎣 Angeln (`/fish`)",
        value="Angel Fische aus dem Wasser.\n*Einfacher Sammel-Job*",
        inline=True,
    )
    embed.add_field(
        name="📊 Befehle",
        value=(
            "`/jobupgrade <job>` — Werkzeug verbessern\n"
            "`/jobcooldown <job>` — Cooldown verkürzen\n"
            "`/jobstats <job>` — Statistiken anzeigen"
        ),
        inline=False,
    )
    embed.set_footer(text="💡 Bessere Werkzeuge = höhere Belohnungen + seltene Funde!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#            SPIEL: PLINKO
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="plinko", description="🔴 Plinko – Die Kugel fällt durch die Pyramide!")
@app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)")
@is_gambling_channel()
async def cmd_plinko(interaction: discord.Interaction, einsatz: int):
    if not await check_bet(interaction, einsatz):
        return

    user_id = interaction.user.id
    update_balance(user_id, -einsatz)

    # Kugel-Pfad berechnen: Position 0-8 (9 Slots)
    pos = 4  # Start Mitte
    path = []

    for row in range(PLINKO_ROWS):
        direction = random.choice([-1, 1])
        new_pos = pos + direction
        new_pos = max(0, min(8, new_pos))
        path.append({"row": row, "pos": new_pos, "dir": direction})
        pos = new_pos

    final_slot = pos
    multiplier = PLINKO_MULTIPLIERS[final_slot]
    win = int(einsatz * multiplier)
    profit = win - einsatz

    def render_board(ball_row: int = -1, ball_pos: int = -1, finished: bool = False) -> str:
        lines = []

        for row in range(PLINKO_ROWS):
            pin_count = row + 3
            total_width = PLINKO_ROWS + 2
            padding = total_width - pin_count

            row_str = " " * padding
            for pin in range(pin_count):
                if row == ball_row:
                    # Berechne welcher Pin am nächsten zur Ball-Position ist
                    if pin_count > 1:
                        pin_slot = int(pin / (pin_count - 1) * 8)
                    else:
                        pin_slot = 4
                    if pin_slot == ball_pos:
                        row_str += "🔴"
                    else:
                        row_str += "⚪"
                else:
                    row_str += "⚪"
            lines.append(row_str)

        # Multiplikator-Reihe
        mult_str = ""
        for i, m in enumerate(PLINKO_MULTIPLIERS):
            if finished and i == final_slot:
                mult_str += "🔴"
            else:
                mult_str += PLINKO_COLORS.get(m, "⬜")
        lines.append(mult_str)

        return "\n".join(lines)

    def render_multipliers(finished: bool = False) -> str:
        parts = []
        for i, m in enumerate(PLINKO_MULTIPLIERS):
            if finished and i == final_slot:
                parts.append(f"**{m}**")
            else:
                parts.append(f"{m}")
        return "×" + " ".join(parts) + "×"

    # Start
    embed = discord.Embed(
        title="🔴 Plinko",
        description=(
            f"**Einsatz:** {einsatz:,} 🪙\n\n"
            f"```\n{render_board()}\n```\n"
            f"{render_multipliers()}"
        ),
        color=COLOR_INFO,
    )
    embed.set_footer(text="Die Kugel fällt...")
    await interaction.response.send_message(embed=embed)

    # Animation
    for step in range(PLINKO_ROWS):
        await asyncio.sleep(0.6)

        p = path[step]
        dir_arrow = "➡️" if p["dir"] == 1 else "⬅️"

        embed.description = (
            f"**Einsatz:** {einsatz:,} 🪙\n"
            f"Reihe {step + 1}/{PLINKO_ROWS} {dir_arrow}\n\n"
            f"```\n{render_board(ball_row=step, ball_pos=p['pos'])}\n```\n"
            f"{render_multipliers()}"
        )

        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            break

    # Finale
    await asyncio.sleep(0.8)

    update_balance(user_id, win)
    new_balance = get_balance(user_id)

    if multiplier >= 3.0:
        title = f"🎉 ×{multiplier} – GROSSER GEWINN!"
        color = COLOR_GOLD
    elif multiplier >= 1.5:
        title = f"✨ ×{multiplier} – Gewinn!"
        color = COLOR_SUCCESS
    elif multiplier >= 0.5:
        title = f"😐 ×{multiplier} – Fast nichts..."
        color = COLOR_WARNING
    else:
        title = f"💀 ×{multiplier} – Verlust!"
        color = COLOR_ERROR

    # Pfad visualisieren
    path_arrows = " ".join("➡️" if p["dir"] == 1 else "⬅️" for p in path)

    embed = discord.Embed(
        title=title,
        description=(
            f"```\n{render_board(finished=True)}\n```\n"
            f"{render_multipliers(finished=True)}\n\n"
            f"**Einsatz:** {einsatz:,} 🪙\n"
            f"**Multiplikator:** ×{multiplier}\n"
            f"**Auszahlung:** {win:,} 🪙 ({'+' if profit >= 0 else ''}{profit:,})\n\n"
            f"📍 {path_arrows}"
        ),
        color=color,
    )
    embed.set_footer(text=f"Kontostand: {new_balance:,} 🪙")
    await interaction.edit_original_response(embed=embed)
    
# ══════════════════════════════════════════════════════════
#                  ERROR HANDLER
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="userinfo", description="📋 Vollständige User-Akte (nur für Mods)")
@app_commands.describe(member="Der User dessen Akte erstellt werden soll")
async def cmd_userinfo(interaction: discord.Interaction, member: discord.Member = None):
    # Mod-Check
    if not any(role.name == MOD_ROLE_NAME for role in interaction.user.roles):
        return await interaction.response.send_message(
            f"❌ Du benötigst die Rolle **{MOD_ROLE_NAME}**.",
            ephemeral=True,
        )

    if member is None:
        member = interaction.user

    await interaction.response.defer(ephemeral=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    data = load_data()
    uid = str(member.id)

    # ── Discord-Infos ──────────────────────────────────────
    account_created = member.created_at
    joined_at = member.joined_at
    account_age = (now - account_created).days
    server_age = (now - joined_at).days if joined_at else 0
    roles = [r for r in member.roles if r.name != "@everyone"]
    roles_str = ", ".join(r.name for r in reversed(roles)) or "Keine"

    # ── Activity-Tracker ──────────────────────────────────
    has_twitch = uid in data["users"]
    twitch_name = data["users"][uid]["twitch_name"] if has_twitch else None
    stream_days = len(data["streams"])
    present = sum(1 for d in data["streams"] if uid in data["streams"][d])
    absent = stream_days - present
    pct = (present / stream_days * 100) if stream_days > 0 else 0
    current_streak = get_current_streak(data, uid) if has_twitch else 0
    longest_streak = get_longest_streak(data, uid) if has_twitch else 0
    last_seen = "Noch nie"
    first_seen = "Unbekannt"
    if has_twitch:
        for ds in sorted(data["streams"].keys(), reverse=True):
            if uid in data["streams"][ds]:
                last_seen = ds; break
        for ds in sorted(data["streams"].keys()):
            if uid in data["streams"][ds]:
                first_seen = ds; break
    grade, grade_emoji, grade_color = get_activity_grade(pct) if has_twitch else ("–", "❓", 0x30363d)

    # ── Gambling ──────────────────────────────────────────
    balance = get_balance(member.id)
    bank = load_bank()
    uid_str = str(member.id)
    daily_streak = bank.get(f"{uid_str}_daily_streak", 0)
    all_balances = {k: v for k, v in bank.items() if k.isdigit() and isinstance(v, int)}
    sorted_b = sorted(all_balances.items(), key=lambda x: x[1], reverse=True)
    coin_rank = next((i+1 for i,(u,_) in enumerate(sorted_b) if u == uid_str), len(sorted_b))

    # ── Angeln ────────────────────────────────────────────
    fd = get_fishing_data(member.id)
    rod = FISHING_RODS[fd.get("rod","basic")]

    # ── Tickets ───────────────────────────────────────────
    user_tickets = [t for t in open_tickets.values() if t["user_id"] == member.id]

    # ── Warnungen / Timeouts (aus Twitch Mod Log) ─────────
    stats = {}
    try:
        import json as _j
        sf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_stats.json")
        if os.path.exists(sf):
            with open(sf, "r", encoding="utf-8") as f:
                stats = _j.load(f)
    except Exception:
        pass

    # ── Voice-Zeit ────────────────────────────────────────
    voice_secs = stats.get("voice_time", {}).get(uid_str, 0)
    voice_msgs  = stats.get("messages", {}).get(uid_str, {}).get("total", 0)

    # ══════════════════════════════════════════════════════
    # Embed bauen
    # ══════════════════════════════════════════════════════
    embed = discord.Embed(
        title=f"📋 User-Akte: {member.display_name}",
        color=grade_color,
        timestamp=now,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    if member.banner:
        embed.set_image(url=member.banner.url)

    # Discord-Block
    discord_val = "\n".join([
        "**Name:** " + member.mention,
        "**Username:** `" + member.name + "`",
        "**ID:** `" + str(member.id) + "`",
        "**Bot:** " + ("Ja" if member.bot else "Nein"),
        "**Status:** " + str(member.status).capitalize(),
        "**Nitro:** " + ("✅" if member.premium_since else "❌"),
    ])
    embed.add_field(name="🏷️ Discord", value=discord_val, inline=True)

    nitro_since = f"<t:{int(member.premium_since.timestamp())}:D>" if member.premium_since else "–"
    ts_val = "\n".join([
        f"**Account erstellt:** <t:{int(account_created.timestamp())}:D>",
        f"**Account-Alter:** {account_age} Tage",
        f"**Server-Beitritt:** <t:{int(joined_at.timestamp())}:D>",
        f"**Auf Server seit:** {server_age} Tage",
        f"**Nitro seit:** {nitro_since}",
    ])
    embed.add_field(name="📅 Zeitstempel", value=ts_val, inline=True)
    embed.add_field(name=f"🏅 Rollen ({len(roles)})", value=roles_str[:1024], inline=False)

    twitch_link = f"[{twitch_name}](https://twitch.tv/{twitch_name})" if twitch_name else "Nicht verknüpft"
    tw_val = "\n".join([
        f"**Twitch:** {twitch_link}",
        f"**Note:** {grade_emoji} {grade}",
        f"**Anwesend:** {present}/{stream_days} Streams ({pct:.0f}%)",
        f"**Gefehlt:** {absent}",
        f"**Streak:** {current_streak} Tage",
        f"**Längste Streak:** {longest_streak} Tage",
        f"**Zuletzt gesehen:** `{last_seen}`",
        f"**Dabei seit:** `{first_seen}`",
    ])
    embed.add_field(name="📺 Twitch-Aktivität", value=tw_val, inline=True)

    voice_h = voice_secs // 3600
    voice_m = (voice_secs % 3600) // 60
    cv_val = "\n".join([
        f"**Nachrichten:** {voice_msgs:,}",
        f"**Voice-Zeit:** {voice_h}h {voice_m}m",
        f"**Coin-Kontostand:** {balance:,} 🪙",
        f"**Coin-Rang:** #{coin_rank} von {len(all_balances)}",
        f"**Daily-Streak:** {daily_streak} Tage",
    ])
    embed.add_field(name="📊 Chat & Voice", value=cv_val, inline=True)

    biggest = fd.get("biggest_catch_name", "–")
    biggest_val = fd.get("biggest_catch", 0)
    fish_val = "\n".join([
        f"**Angel:** {rod['name']}",
        f"**Gefangen:** {fd.get('total_caught', 0):,}",
        f"**Verdient:** {fd.get('total_earned', 0):,} 🪙",
        f"**Legendäre:** {fd.get('legendary_caught', 0)}",
        f"**Bester Fang:** {biggest} ({biggest_val:,} 🪙)",
        f"**Köder:** {fd.get('bait', 0)}",
    ])
    embed.add_field(name="🎣 Angeln", value=fish_val, inline=True)

    ticket_val = "\n".join([
        f"**Offene Tickets:** {len(user_tickets)}",
        f"**Mitglieder-Nr.:** #{member.guild.member_count}",
    ])
    embed.add_field(name="🎫 Tickets & Server", value=ticket_val, inline=True)

    # Permissions
    key_perms = []
    p = member.guild_permissions
    if p.administrator: key_perms.append("Administrator")
    if p.manage_guild:  key_perms.append("Server verwalten")
    if p.manage_channels: key_perms.append("Kanäle verwalten")
    if p.manage_roles:  key_perms.append("Rollen verwalten")
    if p.manage_messages: key_perms.append("Nachrichten verwalten")
    if p.kick_members:  key_perms.append("Mitglieder kicken")
    if p.ban_members:   key_perms.append("Mitglieder bannen")
    if p.mention_everyone: key_perms.append("@everyone erwähnen")
    embed.add_field(
        name="🔑 Berechtigungen",
        value=", ".join(key_perms) or "Keine besonderen",
        inline=False,
    )

    embed.set_footer(
        text=f"Angefragt von {interaction.user.display_name} · User-ID: {member.id}",
        icon_url=interaction.user.display_avatar.url,
    )

    # In konfigurierten Kanal senden falls gesetzt
    sent_to_channel = False
    if USERINFO_CHANNEL_ID:
        ch = interaction.guild.get_channel(USERINFO_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=embed)
                sent_to_channel = True
            except Exception as e:
                print(f"❌ [UserInfo] Kanal-Send Fehler: {e}")

    # Immer auch ephemeral an den Mod
    await interaction.followup.send(
        embed=embed,
        content=f"✅ Akte gesendet in <#{USERINFO_CHANNEL_ID}>" if sent_to_channel else None,
        ephemeral=True,
    )



# ══════════════════════════════════════════════════════════
#   KARAOKE-SYSTEM
#   pip install syncedlyrics gradio_client Pillow av numpy
# ══════════════════════════════════════════════════════════

# ── Karaoke Konfiguration ──────────────────────────────────
KARAOKE_HF_SPACE     = os.getenv("KARAOKE_HF_SPACE", "fffiloni/instant-vocal-remover")
KARAOKE_VIDEO_W      = 1280
KARAOKE_VIDEO_H      = 720
KARAOKE_FPS          = 30
KARAOKE_COUNTDOWN    = 10
KARAOKE_BG           = (10, 10, 20)
KARAOKE_TEXT         = (255, 255, 255)
KARAOKE_HIGHLIGHT    = (99, 102, 241)
KARAOKE_DIM          = (120, 120, 140)

# Aktive Karaoke-Sessions { guild_id: session_dict }
_karaoke_sessions: dict[int, dict] = {}


# ── LRC-Parser ────────────────────────────────────────────
def _parse_lrc(lrc_text: str) -> list[dict]:
    pat   = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)")
    lines = []
    for m in pat.finditer(lrc_text):
        ms  = (int(m.group(1)) * 60 + int(m.group(2))) * 1000
        raw = m.group(3)
        ms += int(raw) * (10 if len(raw) == 2 else 1)
        txt = m.group(4).strip()
        if txt:
            lines.append({"ms": ms, "text": txt})
    return sorted(lines, key=lambda l: l["ms"])


def _active_lrc_line(lyrics: list[dict], elapsed_ms: int) -> tuple[int, int]:
    cur = -1
    for i, l in enumerate(lyrics):
        if l["ms"] <= elapsed_ms:
            cur = i
        else:
            break
    return cur, (cur + 1 if cur + 1 < len(lyrics) else -1)


# ── Video-Frame-Rendering ──────────────────────────────────
def _karaoke_font(size: int):
    try:
        from PIL import ImageFont
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "arial.ttf",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()
    except Exception:
        return None


def _make_karaoke_frame(
    main_text:  str,
    sub_text:   str  = "",
    main_color: tuple = None,
    sub_color:  tuple = None,
    main_size:  int  = 80,
    sub_size:   int  = 44,
) -> bytes:
    try:
        from PIL import Image, ImageDraw
        import io as _io

        mc = main_color or KARAOKE_HIGHLIGHT
        sc = sub_color  or KARAOKE_DIM

        img  = Image.new("RGB", (KARAOKE_VIDEO_W, KARAOKE_VIDEO_H), color=KARAOKE_BG)
        draw = ImageDraw.Draw(img)

        # obere Akzentlinie
        for y in range(4):
            a = 1.0 - y / 4
            draw.line([(0, y), (KARAOKE_VIDEO_W, y)],
                      fill=tuple(int(c * a) for c in KARAOKE_HIGHLIGHT))

        # Haupttext
        fm = _karaoke_font(main_size)
        if fm:
            bb   = draw.textbbox((0, 0), main_text, font=fm)
            tw   = bb[2] - bb[0]
            th   = bb[3] - bb[1]
            x    = (KARAOKE_VIDEO_W - tw) // 2
            y    = (KARAOKE_VIDEO_H - th) // 2 - (sub_size + 24 if sub_text else 0)
            draw.text((x + 3, y + 3), main_text, font=fm, fill=(0, 0, 0))
            draw.text((x, y),         main_text, font=fm, fill=mc)
        else:
            draw.text((40, KARAOKE_VIDEO_H // 2 - 30), main_text, fill=mc)
            th = 60; y = KARAOKE_VIDEO_H // 2 - 30

        # Untertext
        if sub_text:
            fs = _karaoke_font(sub_size)
            if fs:
                bb2 = draw.textbbox((0, 0), sub_text, font=fs)
                x2  = (KARAOKE_VIDEO_W - (bb2[2] - bb2[0])) // 2
                draw.text((x2, y + th + 24), sub_text, font=fs, fill=sc)
            else:
                draw.text((40, y + th + 24), sub_text, fill=sc)

        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    except Exception as e:
        print(f"⚠️ [Karaoke] Frame-Rendering Fehler: {e}")
        return b""


def _encode_karaoke_frame_h264(jpeg_bytes: bytes) -> bytes:
    if not jpeg_bytes:
        return b""
    try:
        import av
        import numpy as np
        from PIL import Image
        import io as _io

        img   = Image.open(_io.BytesIO(jpeg_bytes)).convert("RGB")
        frame = av.VideoFrame.from_ndarray(
            np.array(img, dtype=np.uint8), format="rgb24"
        ).reformat(format="yuv420p")

        codec = av.CodecContext.create("libx264", "w")
        codec.width     = KARAOKE_VIDEO_W
        codec.height    = KARAOKE_VIDEO_H
        codec.pix_fmt   = "yuv420p"
        codec.framerate = KARAOKE_FPS
        codec.options   = {
            "preset": "ultrafast",
            "tune":   "zerolatency",
            "crf":    "28",
        }
        codec.open()
        pkts = list(codec.encode(frame)) + list(codec.encode(None))
        return b"".join(bytes(p) for p in pkts)

    except Exception:
        return jpeg_bytes   # JPEG als Fallback


async def _karaoke_send_frame(vc: discord.VoiceClient, data: bytes) -> None:
    if not data:
        return
    try:
        if hasattr(vc, "send_video_frame"):
            await vc.send_video_frame(data)
        elif hasattr(vc, "_video_connection") and vc._video_connection:
            vc._video_connection.send_frame(data)
    except Exception:
        pass


# ── Audio-Download ─────────────────────────────────────────
async def _karaoke_download_audio(query: str, out_dir: str) -> tuple[str, str]:
    import yt_dlp
    from pathlib import Path as _Path

    is_url = query.startswith("http://") or query.startswith("https://")
    ydl_opts = {
        "format":         "bestaudio/best",
        "outtmpl":        os.path.join(out_dir, "%(id)s.%(ext)s"),
        "quiet":          True,
        "no_warnings":    True,
        "noplaylist":     True,
        "postprocessors": [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "128",
        }],
    }

    loop = asyncio.get_event_loop()

    def _dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                query if is_url else f"ytsearch:{query}",
                download=True,
            )
            if "entries" in info:
                info = info["entries"][0]
            title = info.get("title", query)
            for f in _Path(out_dir).glob("*.mp3"):
                return str(f), title
            raise FileNotFoundError("Keine MP3-Datei nach Download gefunden")

    return await loop.run_in_executor(None, _dl)


# ── Vokal-Trennung via HuggingFace ────────────────────────
async def _karaoke_separate_vocals(mp3_path: str, out_dir: str) -> str:
    try:
        from gradio_client import Client, handle_file
        import shutil as _shutil

        loop = asyncio.get_event_loop()

        def _call():
            client = Client(KARAOKE_HF_SPACE)
            result = client.predict(handle_file(mp3_path), api_name="/predict")
            # Manche Spaces geben (vocals, instrumental) zurück
            if isinstance(result, (list, tuple)):
                instrumental = result[1] if len(result) > 1 else result[0]
            else:
                instrumental = result
            dest = os.path.join(out_dir, "instrumental.mp3")
            _shutil.copy(str(instrumental), dest)
            return dest

        return await asyncio.wait_for(
            loop.run_in_executor(None, _call),
            timeout=300,  # 5 Minuten max
        )

    except asyncio.TimeoutError:
        print("⚠️ [Karaoke] HuggingFace Timeout – nutze Original-Audio")
        return mp3_path
    except Exception as e:
        print(f"⚠️ [Karaoke] Vokal-Trennung fehlgeschlagen ({e}) – nutze Original-Audio")
        return mp3_path


# ── Lyrics ────────────────────────────────────────────────
async def _karaoke_fetch_lyrics(title: str) -> list[dict]:
    try:
        import syncedlyrics
        loop = asyncio.get_event_loop()
        lrc  = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: syncedlyrics.search(title)),
            timeout=15,
        )
        if lrc:
            return _parse_lrc(lrc)
    except asyncio.TimeoutError:
        print("⚠️ [Karaoke] Lyrics-Timeout")
    except Exception as e:
        print(f"⚠️ [Karaoke] Lyrics-Fehler: {e}")
    return []


# ── Haupt-Render-Schleife ─────────────────────────────────
async def _run_karaoke_session(
    session:     dict,
    interaction: discord.Interaction,
) -> None:
    import time as _time

    vc     = session["vc"]
    title  = session["title"]
    lyrics = session["lyrics"]
    instr  = session["instrumental"]
    ft     = 1.0 / KARAOKE_FPS

    # ── Phase 1: Countdown ─────────────────────────────────
    cd_start = _time.monotonic()
    for remaining in range(KARAOKE_COUNTDOWN, 0, -1):
        deadline = cd_start + (KARAOKE_COUNTDOWN - remaining + 1)
        while _time.monotonic() < deadline:
            if not session.get("running"):
                return
            frame = _make_karaoke_frame(
                str(remaining),
                sub_text=f"🎤  {title}",
                main_color=KARAOKE_HIGHLIGHT,
                main_size=220,
                sub_size=44,
            )
            await _karaoke_send_frame(vc, _encode_karaoke_frame_h264(frame))
            await asyncio.sleep(ft)

    # ── Phase 2: Audio + Lyrics ────────────────────────────
    if vc.is_playing():
        vc.stop()

    audio_source = discord.FFmpegOpusAudio(
        instr,
        before_options="-nostdin",
        options="-vn",
    )
    vc.play(audio_source)
    song_start = _time.monotonic()

    while session.get("running") and (vc.is_playing() or vc.is_paused()):
        elapsed_ms = int((_time.monotonic() - song_start) * 1000)

        if not lyrics:
            frame = _make_karaoke_frame(
                title, "🎤 Singe jetzt!",
                main_size=60, sub_size=40,
            )
        else:
            ci, ni = _active_lrc_line(lyrics, elapsed_ms)
            cur_text = lyrics[ci]["text"] if ci >= 0 else "♪ ♪ ♪"
            nxt_text = lyrics[ni]["text"] if ni >= 0 else ""
            frame    = _make_karaoke_frame(
                cur_text, nxt_text,
                main_color=KARAOKE_HIGHLIGHT if ci >= 0 else KARAOKE_DIM,
                main_size=80, sub_size=46,
            )

        await _karaoke_send_frame(vc, _encode_karaoke_frame_h264(frame))
        await asyncio.sleep(ft)

    # ── Ende ──────────────────────────────────────────────
    session["running"] = False
    if vc.is_playing():
        vc.stop()

    frame = _make_karaoke_frame(
        "🎤 Ende!", title,
        main_size=100, sub_size=44,
    )
    await _karaoke_send_frame(vc, _encode_karaoke_frame_h264(frame))
    await asyncio.sleep(2)


def _karaoke_cleanup(guild_id: int, tmpdir) -> None:
    try:
        if tmpdir:
            tmpdir.cleanup()
    except Exception as e:
        print(f"⚠️ [Karaoke] Cleanup-Fehler: {e}")
    _karaoke_sessions.pop(guild_id, None)


# ── /karaoke Command ──────────────────────────────────────
@d_bot.tree.command(
    name="karaoke",
    description="🎤 Karaoke starten – Songtitel oder YouTube-URL",
)
@app_commands.describe(
    song_oder_url="Songtitel (z.B. 'Die Ärzte - Westerland') oder YouTube-URL"
)
async def cmd_karaoke(
    interaction: discord.Interaction,
    song_oder_url: str,
) -> None:
    gid = interaction.guild_id

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message(
            "❌ Du musst zuerst einem Voice-Kanal beitreten!",
            ephemeral=True,
        )

    if gid in _karaoke_sessions and _karaoke_sessions[gid].get("running"):
        return await interaction.response.send_message(
            "❌ Karaoke läuft bereits! Nutze `/karaoke-stop` zum Beenden.",
            ephemeral=True,
        )

    await interaction.response.defer(thinking=True)
    user_vc = interaction.user.voice.channel

    import tempfile as _tempfile
    tmpdir = _tempfile.TemporaryDirectory(prefix="karaoke_")

    try:
        # 1) Audio herunterladen
        await interaction.followup.send(
            "⏳ **Schritt 1/3** — Lade Audio von YouTube herunter…",
            ephemeral=True,
        )
        try:
            mp3_path, title = await _karaoke_download_audio(song_oder_url, tmpdir.name)
        except Exception as e:
            tmpdir.cleanup()
            return await interaction.followup.send(
                f"❌ Song nicht gefunden: `{e}`", ephemeral=True
            )

        # 2) Vokal-Trennung
        await interaction.followup.send(
            f"🎵 **{title}** gefunden!\n"
            "⏳ **Schritt 2/3** — Trenne Gesangsstimme (kann 1–3 Min. dauern)…",
            ephemeral=True,
        )
        instrumental = await _karaoke_separate_vocals(mp3_path, tmpdir.name)

        # 3) Lyrics holen
        await interaction.followup.send(
            "⏳ **Schritt 3/3** — Lade Songtexte…",
            ephemeral=True,
        )
        lyrics = await _karaoke_fetch_lyrics(title)
        lyrics_info = (
            f"✅ {len(lyrics)} Zeilen geladen"
            if lyrics else
            "⚠️ Keine Lyrics gefunden – läuft ohne eingeblendeten Text"
        )

        # 4) Voice-Kanal beitreten
        existing_vc = interaction.guild.voice_client
        if existing_vc and existing_vc.is_connected():
            if existing_vc.channel.id != user_vc.id:
                await existing_vc.move_to(user_vc)
            vc = existing_vc
        else:
            try:
                vc = await user_vc.connect(self_deaf=True, self_video=True)
            except TypeError:
                vc = await user_vc.connect(self_deaf=True)

        # Session anlegen
        session = {
            "guild_id":     gid,
            "vc":           vc,
            "title":        title,
            "instrumental": instrumental,
            "lyrics":       lyrics,
            "tmpdir":       tmpdir,
            "running":      True,
            "task":         None,
        }
        _karaoke_sessions[gid] = session

        await interaction.followup.send(
            f"🎤 **Karaoke startet in {KARAOKE_COUNTDOWN} Sekunden!**\n"
            f"Titel: **{title}**\n"
            f"Lyrics: {lyrics_info}\n"
            f"Kanal: {user_vc.mention}",
        )

        # Karaoke-Task starten
        async def _wrapped():
            try:
                await _run_karaoke_session(session, interaction)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"❌ [Karaoke] Session-Fehler: {e}")
                try:
                    await interaction.followup.send(
                        f"❌ Karaoke-Fehler: `{e}`", ephemeral=True
                    )
                except Exception:
                    pass
            finally:
                try:
                    if vc.is_playing():
                        vc.stop()
                except Exception:
                    pass
                try:
                    await vc.disconnect(force=False)
                except Exception:
                    pass
                _karaoke_cleanup(gid, tmpdir)

        task = asyncio.create_task(_wrapped())
        session["task"] = task

    except Exception as e:
        tmpdir.cleanup()
        _karaoke_sessions.pop(gid, None)
        await interaction.followup.send(
            f"❌ Unbekannter Fehler: `{e}`", ephemeral=True
        )


# ── /karaoke-stop Command ─────────────────────────────────
@d_bot.tree.command(
    name="karaoke-stop",
    description="🛑 Laufendes Karaoke beenden",
)
async def cmd_karaoke_stop(interaction: discord.Interaction) -> None:
    gid     = interaction.guild_id
    session = _karaoke_sessions.get(gid)

    if not session or not session.get("running"):
        return await interaction.response.send_message(
            "❌ Kein Karaoke aktiv.", ephemeral=True
        )

    session["running"] = False
    task = session.get("task")
    if task and not task.done():
        task.cancel()

    vc = session.get("vc")
    if vc and vc.is_playing():
        vc.stop()

    await interaction.response.send_message("⏹️ Karaoke wurde gestoppt.")


@d_bot.event
async def on_error(event_method: str, /, *args, **kwargs):
    """Globale Fehlerfalle für Gateway-Events.

    Stellt sicher, dass ein Fehler in einem Event (on_message,
    on_voice_state_update, ...) den Bot NICHT crashen lässt und
    keine Fehler-Nachricht an User oder Server gesendet wird.
    """
    import traceback
    traceback.print_exc()
    print(f"❌ [Bot] Fehler im Event '{event_method}': "
          f"{type(kwargs.get('error', None)).__name__ if 'error' in kwargs else 'n/a'}")


@d_bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    # Doppelte Antworten vermeiden (CheckFailure wurde bereits beantwortet)
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ Befehl hier nicht verfügbar.", ephemeral=True
            )
        return

    import traceback
    traceback.print_exc()
    print(f"❌ Command-Fehler: {type(error).__name__}: {error}")
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ Ein Fehler ist aufgetreten. Bitte später erneut versuchen.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Ein Fehler ist aufgetreten. Bitte später erneut versuchen.",
                ephemeral=True,
            )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
#                   HAUPTPROGRAMM
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#   DASHBOARD – FLASK APP (läuft in separatem Thread)
# ══════════════════════════════════════════════════════════

_DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")

_flask_app = _Flask(
    "dashboard",
    template_folder=os.path.join(_DASHBOARD_DIR, "templates"),
    static_folder=os.path.join(_DASHBOARD_DIR, "static"),
)
_flask_app.secret_key = os.getenv("DASHBOARD_SECRET", "bygorgii_dashboard_secret_change_me")
_flask_app.jinja_env.globals.update(enumerate=enumerate, zip=zip, len=len, int=int, str=str, max=max)

_DASHBOARD_PORT    = int(os.getenv("DASHBOARD_PORT", "5000"))
_DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
_DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
_REDIRECT_URI_ENV  = os.getenv("DISCORD_REDIRECT_URI", "").strip()
_DISCORD_API       = "https://discord.com/api/v10"

# ── Stats-Hilfsfunktionen ──────────────────────────────────

def _load_stats() -> dict:
    try:
        with open(_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"messages":{},"voice_time":{},"voice_days":{},"daily_active":{},"gambling":{},"mod_actions":[],"deletions":[],"joins":[]}

def _get_summary(s: dict) -> dict:
    return {
        "total_messages":    sum(v.get("total",0) for v in s.get("messages",{}).values()),
        "total_voice_secs":  sum(s.get("voice_time",{}).values()),
        "total_mod_actions": len(s.get("mod_actions",[])),
        "total_deletions":   len(s.get("deletions",[])),
        "total_joins":       len(s.get("joins",[])),
    }

def _msg_lb(s, limit=15):
    items = [{"uid":k,"username":v.get("username",k),"total":v.get("total",0)} for k,v in s.get("messages",{}).items()]
    return sorted(items, key=lambda x: x["total"], reverse=True)[:limit]

def _voice_lb(s, limit=15):
    msg = s.get("messages",{})
    items = [{"uid":u,"username":msg.get(u,{}).get("username",f"User {u}"),"seconds":sec} for u,sec in s.get("voice_time",{}).items()]
    return sorted(items, key=lambda x: x["seconds"], reverse=True)[:limit]

def _daily_msgs(s, days=14):
    da = s.get("daily_active",{})
    return [{"date":(datetime.date.today()-datetime.timedelta(days=i)).isoformat(),"count":sum(da.get((datetime.date.today()-datetime.timedelta(days=i)).isoformat(),{}).values())} for i in range(days-1,-1,-1)]

def _daily_voice(s, days=14):
    vd = s.get("voice_days",{})
    result = []
    for i in range(days-1,-1,-1):
        d = (datetime.date.today()-datetime.timedelta(days=i)).isoformat()
        result.append({"date":d,"seconds":sum(ud.get(d,0) for ud in vd.values())})
    return result

def _gambling_lb(s, limit=10):
    items = [{"uid":k,"username":v.get("username",k),"wins":v.get("wins",0),"losses":v.get("losses",0),"profit":v.get("profit",0)} for k,v in s.get("gambling",{}).items()]
    return sorted(items, key=lambda x: x["profit"], reverse=True)[:limit]

def _get_redirect_uri():
    return _REDIRECT_URI_ENV or f"http://localhost:{_DASHBOARD_PORT}/auth/callback"

def _bh(): return {"Authorization": f"Bot {DISCORD_TOKEN}"}

def _dget(path, timeout=5):
    try:
        r = _requests.get(f"{_DISCORD_API}{path}", headers=_bh(), timeout=timeout)
        return r.json() if r.ok else None
    except Exception: return None

def _fetch_bot():
    d = _dget("/users/@me")
    if d:
        av = d.get("avatar")
        return {"id":d["id"],"username":d["username"],"avatar_url":(f"https://cdn.discordapp.com/avatars/{d['id']}/{av}.webp?size=128" if av else f"https://cdn.discordapp.com/embed/avatars/{int(d.get('discriminator','0'))%5}.png")}
    return {"id":"","username":"Bot","avatar_url":""}

def _fetch_guild():
    if not GUILD_ID: return {}
    d = _dget(f"/guilds/{GUILD_ID}?with_counts=true")
    if d:
        icon = d.get("icon")
        return {"id":d["id"],"name":d.get("name",""),"icon_url":(f"https://cdn.discordapp.com/icons/{d['id']}/{icon}.webp?size=128" if icon else ""),"member_count":d.get("approximate_member_count",0),"online_count":d.get("approximate_presence_count",0)}
    return {}

def _fetch_channels():
    if not GUILD_ID: return []
    return _dget(f"/guilds/{GUILD_ID}/channels") or []

def _fetch_members(limit=100):
    if not GUILD_ID: return []
    return _dget(f"/guilds/{GUILD_ID}/members?limit={limit}") or []

def _oauth_url():
    return (f"https://discord.com/api/oauth2/authorize?client_id={_DISCORD_CLIENT_ID}"
            f"&redirect_uri={quote(_get_redirect_uri(),safe='')}"+
            "&response_type=code&scope=identify")

def _exchange_code(code):
    r = _requests.post(f"{_DISCORD_API}/oauth2/token",
        data={"client_id":_DISCORD_CLIENT_ID,"client_secret":_DISCORD_CLIENT_SECRET,"grant_type":"authorization_code","code":code,"redirect_uri":_get_redirect_uri()},
        headers={"Content-Type":"application/x-www-form-urlencoded"}, timeout=10)
    if not r.ok: raise RuntimeError(f"{r.status_code} – {r.text}")
    return r.json()

def _discord_user(token):
    r = _requests.get(f"{_DISCORD_API}/users/@me", headers={"Authorization":f"Bearer {token}"}, timeout=5)
    r.raise_for_status(); return r.json()

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_EDITABLE = [
    ("GUILD_ID","Guild ID","text","core"),("ALLOWED_CHANNEL_ID","Allowed Channel","text","core"),
    ("LOG_CHANNEL_ID","Log Kanal","text","core"),("MOD_ROLE_NAME","Mod Rolle","text","core"),
    ("AUTO_ROLE_NAME","Auto-Rolle","text","core"),("ADMIN_USER_IDS","Admin User IDs","text","core"),
    ("TWITCH_LOG_CHANNEL_ID","Twitch Log Kanal","text","twitch"),("STREAMER_CHANNEL","Streamer Kanal","text","twitch"),
    ("STREAMER_USER_ID","Streamer User ID","text","twitch"),
    ("TEMP_VOICE_CHANNEL_ID","TempVC Trigger Kanal","text","voice"),("TEMP_VOICE_CATEGORY_ID","TempVC Kategorie","text","voice"),
    ("REVIEW_CHANNEL_ID","Review Kanal","text","review"),
    ("GAMBLING_CHANNEL_ID","Gambling Kanal","text","gambling"),("FISHING_CHANNEL_ID","Angel Kanal","text","gambling"),
    ("CRASH_CHEAT","Crash Cheat (Admins)","bool","gambling"),
    ("MUSIC_ENABLED","Musik Modul","bool","music"),("MUSIC_IDLE_TIMEOUT","Musik Idle Timeout (s)","number","music"),
    ("AI_MODEL_NAME","AI Modell","text","ai"),("PROXYCHECK_API_KEY","ProxyCheck API Key","text","ai"),
    ("TICKET_CATEGORY_ID","Ticket Kategorie","text","tickets"),("TICKET_LOG_CHANNEL_ID","Ticket Log Kanal","text","tickets"),
    ("TICKET_SUPPORT_ROLE","Support Rolle","text","tickets"),
]

def _load_env(): return {k: os.getenv(k,"") for k,*_ in _EDITABLE}

def _save_env(key, value):
    lines = open(_ENV_PATH,"r",encoding="utf-8").readlines() if os.path.exists(_ENV_PATH) else []
    found, new = False, []
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
            new.append(f"{key}={value}\n"); found = True
        else: new.append(line)
    if not found: new.append(f"{key}={value}\n")
    with open(_ENV_PATH,"w",encoding="utf-8") as f: f.writelines(new)
    os.environ[key] = value

def _require_admin(f):
    from functools import wraps
    @wraps(f)
    def dec(*a, **kw):
        u = _session.get("discord_user")
        if not u: return _redirect(_url_for("_login"))
        if int(u["id"]) not in ADMIN_USER_IDS: return _render("403.html"), 403
        return f(*a, **kw)
    return dec

@_flask_app.context_processor
def _ctx(): return {"admin_ids": list(ADMIN_USER_IDS), "redirect_uri": _get_redirect_uri()}

@_flask_app.route("/favicon.ico")
def _favicon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#6366f1"/><path d="M8 12h16M8 16h16M8 20h10" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg>'
    return _Response(svg, mimetype="image/svg+xml")

@_flask_app.route("/")
@_require_admin
def _index():
    s = _load_stats(); ch = _fetch_channels()
    return _render("index.html", bot=_fetch_bot(), guild=_fetch_guild(),
        vc_list=[c for c in ch if c.get("type")==2],
        now_playing=dashboard_state.now_playing, summary=_get_summary(s),
        recent_deletions=list(reversed(s.get("deletions",[])))[:10],
        recent_joins=list(reversed(s.get("joins",[])))[:10],
        recent_mod=list(reversed(s.get("mod_actions",[])))[:10])

@_flask_app.route("/stats")
@_require_admin
def _stats():
    s = _load_stats()
    return _render("stats.html", bot=_fetch_bot(), guild=_fetch_guild(), summary=_get_summary(s),
        msg_leaderboard=_msg_lb(s), voice_leaderboard=_voice_lb(s),
        daily_messages=_daily_msgs(s), daily_voice=_daily_voice(s), gambling_lb=_gambling_lb(s))

@_flask_app.route("/logs")
@_require_admin
def _logs():
    s = _load_stats()
    actions = s.get("mod_actions",[])
    mod_stats = {a: sum(1 for x in actions if x.get("action")==a) for a in set(x.get("action","") for x in actions)}
    return _render("logs.html", bot=_fetch_bot(), guild=_fetch_guild(),
        deletions=list(reversed(s.get("deletions",[])))[:50],
        joins=list(reversed(s.get("joins",[])))[:50],
        mod_actions=list(reversed(actions))[:100], mod_stats=mod_stats)

@_flask_app.route("/members")
@_require_admin
def _members():
    return _render("members.html", bot=_fetch_bot(), guild=_fetch_guild(), members=_fetch_members())

@_flask_app.route("/music")
@_require_admin
def _music():
    return _render("music.html", bot=_fetch_bot(), guild=_fetch_guild(), now_playing=dashboard_state.now_playing)

@_flask_app.route("/settings", methods=["GET","POST"])
@_require_admin
def _settings():
    saved, errors = False, []
    if _request.method == "POST":
        for key, label, typ, _ in _EDITABLE:
            val = "true" if typ=="bool" and _request.form.get(key+"_check") else ("false" if typ=="bool" else _request.form.get(key,"").strip())
            if typ=="number" and val:
                try: int(val)
                except ValueError: errors.append(f"{label}: Zahl erwartet"); continue
            try: _save_env(key, val)
            except Exception as e: errors.append(f"{key}: {e}")
        if not errors:
            saved = True
            _ws_broadcast("settings_changed", {})
    return _render("settings.html", bot=_fetch_bot(), guild=_fetch_guild(),
        settings=_load_env(), editable=_EDITABLE, saved=saved, errors=errors)

@_flask_app.route("/api/status")
@_require_admin
def _api_status():
    s = _load_stats()
    return _jsonify({"bot":_fetch_bot(),"guild":_fetch_guild(),"now_playing":dashboard_state.now_playing,"summary":_get_summary(s),"ts":datetime.datetime.now(datetime.timezone.utc).isoformat()})

@_flask_app.route("/api/settings", methods=["GET"])
@_require_admin
def _api_settings_get(): return _jsonify(_load_env())

@_flask_app.route("/api/settings", methods=["POST"])
@_require_admin
def _api_settings_post():
    data = _request.get_json(silent=True) or {}
    allowed = {k for k,*_ in _EDITABLE}; errors = []
    for key, val in data.items():
        if key not in allowed: errors.append(f"Unbekannt: {key}"); continue
        try: _save_env(key, str(val))
        except Exception as e: errors.append(str(e))
    if errors: return _jsonify({"ok":False,"errors":errors}), 400
    _ws_broadcast("settings_changed", {})
    return _jsonify({"ok":True})

@_flask_app.route("/login")
def _login():
    if not _DISCORD_CLIENT_ID or not _DISCORD_CLIENT_SECRET:
        return _render("login_no_oauth.html")
    return _render("login.html", oauth_url=_oauth_url(), redirect_uri=_get_redirect_uri(), error_msg=None)

@_flask_app.route("/auth/callback")
def _auth_callback():
    code = _request.args.get("code"); error = _request.args.get("error")
    if error:
        return _render("login.html", oauth_url=_oauth_url(), redirect_uri=_get_redirect_uri(),
            error_msg=f"{error}: {_request.args.get('error_description','')}"), 400
    if not code: return _redirect(_url_for("_login"))
    try:
        td = _exchange_code(code); user = _discord_user(td["access_token"])
        if int(user["id"]) not in ADMIN_USER_IDS:
            return _render("403.html"), 403
        _session["discord_user"] = user
        return _redirect(_url_for("_index"))
    except Exception as e:
        return _render("login.html", oauth_url=_oauth_url(), redirect_uri=_get_redirect_uri(), error_msg=str(e)), 400

@_flask_app.route("/logout")
def _logout(): _session.clear(); return _redirect(_url_for("_login"))

# SSE – Server-Sent Events (kein WebSocket nötig, funktioniert überall)
@_flask_app.route("/ws")
def _sse_stream():
    """Server-Sent Events Endpoint – Live-Updates für das Dashboard."""
    u = _session.get("discord_user")
    if not u or int(u["id"]) not in ADMIN_USER_IDS:
        return _Response("Unauthorized", status=401)

    def generate():
        import time as _time
        q = _ws_subscribe()
        # Initial-State sofort senden
        try:
            init = json.dumps({
                "type": "initial_state",
                "data": {"now_playing": dashboard_state.now_playing},
                "ts":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }, default=str)
            yield f"data: {init}\n\n"
        except Exception:
            pass
        try:
            while True:
                msg = None
                deadline = _time.time() + 20
                while _time.time() < deadline:
                    try: msg = q.get_nowait(); break
                    except _queue.Empty: _time.sleep(0.2)
                if msg:
                    yield f"data: {msg}\n\n"
                else:
                    ping = json.dumps({"type":"ping","ts":datetime.datetime.now(datetime.timezone.utc).isoformat()})
                    yield f"data: {ping}\n\n"
        except GeneratorExit:
            pass
        finally:
            _ws_unsubscribe(q)

    return _Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )

# ── Öffentliche Seiten (kein Login nötig) ────────────────────
@_flask_app.route("/nutzungsbedingungen")
@_flask_app.route("/tos")
def _tos():
    return _render("tos.html", now=datetime.datetime.now())

@_flask_app.route("/datenschutz")
@_flask_app.route("/privacy")
def _privacy():
    return _render("privacy.html", now=datetime.datetime.now())

@_flask_app.errorhandler(403)
def _e403(e): return _render("403.html"), 403
@_flask_app.errorhandler(404)
def _e404(e): return _render("404.html"), 404

def _start_dashboard():
    """Startet Flask in einem Daemon-Thread."""
    import logging, warnings
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)
    warnings.filterwarnings("ignore")
    _flask_app.run(host="0.0.0.0", port=_DASHBOARD_PORT, debug=False, threaded=True, use_reloader=False)


def check_system_requirements() -> None:
    """Prüft System-Voraussetzungen (wichtig auf dem Raspberry Pi 5!).

    Gibt klare Hinweise, wenn Musik-/Voice-Komponenten fehlen,
    statt dass der Bot erst im Betrieb stumm scheitert.
    """
    global FFMPEG_AVAILABLE

    print("")
    print("── System-Check ─────────────────────────────")

    # Python-Version
    v = sys.version_info
    print(f"  🐍 Python: {v.major}.{v.minor}.{v.micro}")

    # FFmpeg (erforderlich für ALLE Musik-Features)
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        FFMPEG_AVAILABLE = True
        print(f"  🎼 FFmpeg:  ✅ {ffmpeg_path}")
    else:
        FFMPEG_AVAILABLE = False
        print("  🎼 FFmpeg:  ❌ NICHT GEFUNDEN – Musik wird NICHT funktionieren!")
        print("     Raspberry Pi:  sudo apt update && sudo apt install -y ffmpeg")
        print("     Ubuntu/Debian: sudo apt update && sudo apt install -y ffmpeg")

    # PyNaCl (erforderlich für Discord-Voice)
    try:
        import nacl  # noqa: F401
        print("  🔐 PyNaCl:  ✅ (Voice-Verschlüsselung)")
    except ImportError:
        print("  🔐 PyNaCl:  ❌ fehlt – Voice-Verbindungen werden fehlschlagen!")
        print("     pip install pynacl")

    # yt-dlp (erforderlich zum Laden der Songs)
    if MUSIC_ENABLED and YDL:
        try:
            print(f"   yt-dlp:  ✅ Version {yt_dlp.version.__version__}")
        except Exception:
            print("  🎵 yt-dlp:  ✅ installiert")
    else:
        print("  🎵 yt-dlp:  ❌ fehlt oder Musik deaktiviert")

    print("──────────────────────────────────────────────")
    print("")


async def main():
    # System-Check VOR dem Start (FFmpeg, PyNaCl, yt-dlp, Python)
    check_system_requirements()

    print("")
    print("╔════════════════════════════════════════════════════════╗")
    print("║     🚀 KOMBINIERTER BOT WIRD GESTARTET...              ║")
    print("╠════════════════════════════════════════════════════════╣")
    print(
        f"║  📡 Discord Token: "
        f"{'✅ Geladen' if DISCORD_TOKEN else '❌ FEHLT'}"
        f"                          ║"
    )
    print(
        f"║  📺 Twitch Token:  "
        f"{'✅ Geladen' if TWITCH_TOKEN else '❌ FEHLT'}"
        f"                          ║"
    )
    print(
        f"║  🤖 OpenRouter:    "
        f"{'✅ ' + str(len(OPENROUTER_KEYS)) + ' Key(s)' if OPENROUTER_KEYS else '❌ FEHLT'}"
        f"                       ║"
    )
    print(
        f"║  🛡️ ProxyCheck:    "
        f"{'✅ Geladen' if PROXYCHECK_API_KEY else '❌ FEHLT'}"
        f"                          ║"
    )
    print(
        f"║  📋 Twitch Log:    "
        f"{'✅ ' + str(TWITCH_LOG_CHANNEL_ID) if TWITCH_LOG_CHANNEL_ID else '❌ FEHLT'}"
        f"                       ║"
    )
    print(
        f"║  🎙️ Temp Voice:    "
        f"{'✅ ' + str(TEMP_VOICE_CHANNEL_ID) if TEMP_VOICE_CHANNEL_ID else '❌ FEHLT'}"
        f"                       ║"
    )
    print(
        f"║  📅 Weekly Review:  "
        f"{'✅ ' + str(REVIEW_CHANNEL_ID) if REVIEW_CHANNEL_ID else '❌ FEHLT'}"
        f"                      ║"
    )
    ### MUSIC MODULE START ###
    _music_status = (
        "✅ Aktiv" if MUSIC_ENABLED else "❌ Deaktiviert"
    )
    if MUSIC_ENABLED and not FFMPEG_AVAILABLE:
        _music_status += " (⚠️ ohne FFmpeg!)"
    print(
        f"║  🎵 Musik-Modul:   "
        f"{_music_status:<25}║"
    )
    ### MUSIC MODULE END ###
    print(
        f"║  👑 Admin Users:    "
        f"{'✅ ' + str(len(ADMIN_USER_IDS)) + ' User' if ADMIN_USER_IDS else '❌ KEINE'}"
        f"                       ║"
    )
    print(f"║  📁 Datendatei:    {DATA_FILE:<30}     ║")
    print("╚════════════════════════════════════════════════════════╝")
    print("")

    # ── Dashboard-Thread starten ────────────────────────
    _dash_thread = threading.Thread(target=_start_dashboard, daemon=True)
    _dash_thread.start()
    print(f"🌐 Dashboard: http://localhost:{_DASHBOARD_PORT}")

    twitch_task = None
    if TWITCH_TOKEN and STREAMER_CHANNEL:
        twitch_bot = TwitchBot()
        twitch_task = asyncio.create_task(twitch_bot.start())

        def _twitch_task_done(task: asyncio.Task):
            # Fehler im Twitch-Task loggen statt sie still verschwinden
            # zu lassen (z. B. falscher Token → sonst kein Hinweis)
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                print(f"❌ [Twitch] Task-Fehler: {type(exc).__name__}: {exc}")

        twitch_task.add_done_callback(_twitch_task_done)
    else:
        print("⚠️ Twitch-Bot deaktiviert (fehlende Konfiguration)")

    try:
        await d_bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"❌ Kritischer Fehler: {e}")
    finally:
        print("\n🔄 Fahre herunter...")
        if review_scheduler.is_running():
            review_scheduler.cancel()
        if stream_live_tracker.is_running():
            stream_live_tracker.cancel()
        if clip_tracker.is_running():
            clip_tracker.cancel()
        ### MUSIC MODULE START ###
        if music_idle_checker.is_running():
            music_idle_checker.cancel()
        for state in music_states.values():
            await state.cleanup()
        ### MUSIC MODULE END ###
        if twitch_task:
            twitch_task.cancel()
            try:
                await twitch_task
            except (asyncio.CancelledError, Exception):
                pass
        if not d_bot.is_closed():
            await d_bot.close()
        print("✅ Alle Systeme gestoppt.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN fehlt in .env!")
        exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Programm beendet.")
