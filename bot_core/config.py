"""Zentrale Konfiguration, Konstanten und Spieldaten für Modcheck."""
from __future__ import annotations

import datetime
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ── Zeitzone (Europe/Berlin für korrekten Tageswechsel Sommer/Winterzeit) ──
DE_TZ = ZoneInfo("Europe/Berlin")


def now_berlin() -> datetime.datetime:
    return datetime.datetime.now(DE_TZ)


def today_berlin() -> datetime.date:
    return now_berlin().date()


# ── Dateien ─────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_file(env_name: str, default_filename: str) -> str:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return os.path.join(BASE_DIR, default_filename)
    if os.path.isabs(raw):
        return raw
    return os.path.join(BASE_DIR, raw)


DATA_FILE = _resolve_file("DATA_FILE", "data.json")
BANK_FILE = _resolve_file("GAMBLING_BANK_FILE", "bank.json")
VAULT_FILE = os.path.join(BASE_DIR, "vault.json")
TICKET_DATA_FILE = os.path.join(BASE_DIR, "tickets.json")
ABSENCE_FILE = os.path.join(BASE_DIR, "absences.json")
STATS_FILE = os.path.join(BASE_DIR, "dashboard_stats.json")
PREFS_FILE = os.path.join(BASE_DIR, "user_prefs.json")
ALERTS_FILE = os.path.join(BASE_DIR, "alerts_state.json")


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_int_set(name: str) -> set[int]:
    out: set[int] = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


# ── Discord & Rollen ────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = _env_int("GUILD_ID", 0)
ALLOWED_CHANNEL_ID = _env_int("ALLOWED_CHANNEL_ID", 0)
LOG_CHANNEL_ID = _env_int("LOG_CHANNEL_ID", 0)
MESSAGE_LOG_CHANNEL_ID = _env_int("MESSAGE_LOG_CHANNEL_ID", 0)
USERINFO_CHANNEL_ID = _env_int("USERINFO_CHANNEL_ID", 0)
CLIPS_CHANNEL_ID = _env_int("CLIPS_CHANNEL_ID", 0)
AUTO_ROLE_NAME = os.getenv("AUTO_ROLE_NAME", "Zuschauer").strip()

MOD_ROLE_NAME = os.getenv("MOD_ROLE_NAME", "Moderator").strip()
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "Admin").strip()
ADMIN_USER_IDS = _env_int_set("ADMIN_USER_IDS")

# ── Twitch ──────────────────────────────────────────────────────────────────
TWITCH_TOKEN = os.getenv("TWITCH_TOKEN", "").strip()
STREAMER_CHANNEL = os.getenv("STREAMER_CHANNEL", "").strip().lower()
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
TWITCH_BOT_ID = os.getenv("TWITCH_BOT_ID", "").strip()
STREAMER_USER_ID = (
    os.getenv("STREAMER_USER_ID", "").strip()
    or os.getenv("TWITCH_BROADCASTER_ID", "").strip()
)
STREAMER_OAUTH_TOKEN = (
    os.getenv("STREAMER_OAUTH_TOKEN", "").strip()
    or os.getenv("TWITCH_MOD_ACCESS_TOKEN", "").strip()
).replace("oauth:", "")
TWITCH_LOG_CHANNEL_ID = _env_int("TWITCH_LOG_CHANNEL_ID", 0)
REVIEW_CHANNEL_ID = _env_int("REVIEW_CHANNEL_ID", 0)

# ── Live-, YouTube- & Mod-Inaktivitäts-Alerts ───────────────────────────────
LIVE_ANNOUNCE_CHANNEL_ID = _env_int("LIVE_ANNOUNCE_CHANNEL_ID", 0)
LIVE_PING_ROLE_ID = _env_int("LIVE_PING_ROLE_ID", 0)
LIVE_PING_EVERYONE = os.getenv("LIVE_PING_EVERYONE", "false").strip().lower() in ("true", "1", "yes")

YOUTUBE_ANNOUNCE_CHANNEL_ID = _env_int("YOUTUBE_ANNOUNCE_CHANNEL_ID", 0)
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
YOUTUBE_PING_ROLE_ID = _env_int("YOUTUBE_PING_ROLE_ID", 0)
YOUTUBE_PING_EVERYONE = os.getenv("YOUTUBE_PING_EVERYONE", "false").strip().lower() in ("true", "1", "yes")

INACTIVITY_WARN_CHANNEL_ID = _env_int("INACTIVITY_WARN_CHANNEL_ID", 0)
INACTIVITY_WARN_STREAMS = _env_int("INACTIVITY_WARN_STREAMS", 5)

# ── Musik, Spotify & Radio ──────────────────────────────────────────────────
MUSIC_ENABLED = os.getenv("MUSIC_ENABLED", "true").strip().lower() in ("true", "1", "yes")
MUSIC_IDLE_TIMEOUT = _env_int("MUSIC_IDLE_TIMEOUT", 60)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "cookies.txt").strip()
YOUTUBE_POT_PROVIDER_URL = os.getenv("YOUTUBE_POT_PROVIDER_URL", "").strip()

# ── Casino & Jobs ───────────────────────────────────────────────────────────
GAMBLING_CHANNEL_ID = _env_int("GAMBLING_CHANNEL_ID", 0)
FISHING_CHANNEL_ID = _env_int("FISHING_CHANNEL_ID", 0)
CRASH_CHEAT = os.getenv("CRASH_CHEAT", "false").strip().lower() in ("true", "1", "yes")

# ── Temp Voice ──────────────────────────────────────────────────────────────
TEMP_VOICE_CHANNEL_ID = _env_int("TEMP_VOICE_CHANNEL_ID", 0)
TEMP_VOICE_CATEGORY_ID = _env_int("TEMP_VOICE_CATEGORY_ID", 0)
OWNER_RECOVERY_WINDOW = 600  # 10 Minuten

# ── Tickets ─────────────────────────────────────────────────────────────────
TICKET_CATEGORY_ID = _env_int("TICKET_CATEGORY_ID", 0)
TICKET_LOG_CHANNEL_ID = _env_int("TICKET_LOG_CHANNEL_ID", 0)
TICKET_SUPPORT_ROLE = os.getenv("TICKET_SUPPORT_ROLE", "Support").strip()

runtime_settings: dict[str, object] = {
    "auto_role_name": AUTO_ROLE_NAME,
    "music_idle_timeout": MUSIC_IDLE_TIMEOUT,
    "music_enabled": MUSIC_ENABLED,
}

# ── Farben ──────────────────────────────────────────────────────────────────
COLOR_SUCCESS = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_WARNING = 0xF39C12
COLOR_GOLD = 0xF1C40F
COLOR_INFO = 0x3498DB
COLOR_TWITCH = 0x9146FF
COLOR_MOD_BAN = 0xFF0000
COLOR_MOD_TIMEOUT = 0xFF6600
COLOR_MOD_WARN = 0xFFCC00
COLOR_MOD_DELETE = 0xFF4444
COLOR_MOD_UNBAN = 0x00FF88
COLOR_MOD_UNTIMEOUT = 0x00CCFF
COLOR_VOICE = 0x5865F2
COLOR_WEEKLY = 0x9B59B6
COLOR_MUSIC = 0x1DB954
COLOR_SPOTIFY = 0x1DB954
COLOR_TICKET = 0x2980B9
COLOR_PROFILE = 0x2B2D31
COLOR_RR = 0x8B0000
COLOR_ABSENCE = 0x95A5A6
COLOR_RADIO = 0x1ABC9C
COLOR_CONNECT4 = 0xFFC300
COLOR_TICTACTOE = 0x00BCD4
COLOR_YOUTUBE = 0xFF0000

# ── Kalender-Konstanten ─────────────────────────────────────────────────────
MONTH_NAMES = [
    "",
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
WEEKDAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# ── Casino & Economy Konstanten ─────────────────────────────────────────────
START_BALANCE = 1000
DAILY_REWARD = 250
MAX_BET = 2500

SLOT_SYMBOLS = [
    ("🍒", 30),
    ("🍋", 25),
    ("🍊", 20),
    ("🍇", 15),
    ("🔔", 7),
    ("💎", 2),
    ("7️⃣", 1),
]
SLOT_PAYOUTS = {
    "7️⃣": 25,
    "💎": 15,
    "🔔": 8,
    "🍇": 5,
    "🍊": 4,
    "🍋": 3,
    "🍒": 2,
}

CARD_SUITS = ["♠️", "♥️", "♦️", "♣️"]
CARD_RANKS = [
    ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6), ("7", 7),
    ("8", 8), ("9", 9), ("10", 10), ("B", 10), ("D", 10), ("K", 10), ("A", 11),
]

HORSES = [
    {"name": "Blitz", "emoji": "🐎"},
    {"name": "Donner", "emoji": "🏇"},
    {"name": "Sturmwind", "emoji": "🦄"},
    {"name": "Schatten", "emoji": "🐴"},
]

ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_BLACK = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}

PLINKO_ROWS = 8
PLINKO_MULTIPLIERS = [5.0, 2.5, 1.5, 0.5, 0.2, 0.5, 1.5, 2.5, 5.0]
PLINKO_COLORS = {
    5.0: "🟨",
    2.5: "🟩",
    1.5: "🟦",
    0.5: "🟧",
    0.2: "🟥",
}

RR_CHAMBERS = 6
RR_MIN_PLAYERS = 2
RR_MAX_PLAYERS = 8
RR_JOIN_TIMEOUT = 60

C4_ROWS = 6
C4_COLS = 7
C4_EMPTY = "⚫"
C4_RED = "🔴"
C4_YELLOW = "🟡"

TTT_EMPTY = "⬜"
TTT_X = "❌"
TTT_O = "⭕"

# ── Angeln (Fishing) & Shop Daten ───────────────────────────────────────────
FISH_LIST = [
    {"name": "Alter Schuh", "emoji": "👞", "min_val": 1, "max_val": 5, "weight": 80, "rarity": "junk"},
    {"name": "Plastiktüte", "emoji": "🛍️", "min_val": 1, "max_val": 3, "weight": 70, "rarity": "junk"},
    {"name": "Seegras", "emoji": "🌿", "min_val": 2, "max_val": 8, "weight": 70, "rarity": "junk"},
    {"name": "Rostige Dose", "emoji": "🥫", "min_val": 1, "max_val": 6, "weight": 60, "rarity": "junk"},
    {"name": "Sardine", "emoji": "🐟", "min_val": 10, "max_val": 25, "weight": 120, "rarity": "common"},
    {"name": "Hering", "emoji": "🐟", "min_val": 12, "max_val": 30, "weight": 110, "rarity": "common"},
    {"name": "Karpfen", "emoji": "🐟", "min_val": 15, "max_val": 35, "weight": 100, "rarity": "common"},
    {"name": "Barsch", "emoji": "🐟", "min_val": 15, "max_val": 40, "weight": 95, "rarity": "common"},
    {"name": "Forelle", "emoji": "🐠", "min_val": 35, "max_val": 75, "weight": 65, "rarity": "uncommon"},
    {"name": "Lachs", "emoji": "🐠", "min_val": 40, "max_val": 90, "weight": 55, "rarity": "uncommon"},
    {"name": "Kugelfisch", "emoji": "🐡", "min_val": 50, "max_val": 110, "weight": 45, "rarity": "uncommon"},
    {"name": "Krabbe", "emoji": "🦀", "min_val": 45, "max_val": 95, "weight": 50, "rarity": "uncommon"},
    {"name": "Thunfisch", "emoji": "🐟", "min_val": 90, "max_val": 180, "weight": 28, "rarity": "rare"},
    {"name": "Oktopus", "emoji": "🐙", "min_val": 100, "max_val": 210, "weight": 24, "rarity": "rare"},
    {"name": "Hummer", "emoji": "🦞", "min_val": 110, "max_val": 240, "weight": 20, "rarity": "rare"},
    {"name": "Schwertfisch", "emoji": "🗡️", "min_val": 130, "max_val": 280, "weight": 16, "rarity": "rare"},
    {"name": "Hai", "emoji": "🦈", "min_val": 250, "max_val": 500, "weight": 9, "rarity": "epic"},
    {"name": "Riesenkalmar", "emoji": "🦑", "min_val": 300, "max_val": 600, "weight": 7, "rarity": "epic"},
    {"name": "Schatztruhe", "emoji": "🧰", "min_val": 350, "max_val": 750, "weight": 5, "rarity": "epic"},
    {"name": "Goldener Karpfen", "emoji": "✨", "min_val": 800, "max_val": 1500, "weight": 2, "rarity": "legendary"},
    {"name": "Wal", "emoji": "🐋", "min_val": 1000, "max_val": 2000, "weight": 1, "rarity": "legendary"},
    {"name": "Mythischer Leviathan", "emoji": "🐉", "min_val": 1500, "max_val": 3000, "weight": 1, "rarity": "legendary"},
]

FISH_RARITY_COLORS = {
    "junk": 0x7F8C8D,
    "common": 0x95A5A6,
    "uncommon": 0x2ECC71,
    "rare": 0x3498DB,
    "epic": 0x9B59B6,
    "legendary": 0xF1C40F,
}

FISH_RARITY_NAMES = {
    "junk": "🗑️ Müll",
    "common": "⬜ Gewöhnlich",
    "uncommon": "🟩 Ungewöhnlich",
    "rare": "🟦 Selten",
    "epic": "🟪 Episch",
    "legendary": "🟨 Legendär",
}

FISHING_RODS = {
    "basic": {"name": "🪵 Holzrute", "cost": 0, "rare_bonus": 1.0, "value_bonus": 1.0, "junk_reduce": 1.0},
    "fiberglass": {"name": "🎣 Fiberglasrute", "cost": 750, "rare_bonus": 1.3, "value_bonus": 1.2, "junk_reduce": 0.8},
    "carbon": {"name": "⚡ Carbonrute", "cost": 2500, "rare_bonus": 1.7, "value_bonus": 1.45, "junk_reduce": 0.55},
    "titanium": {"name": "💎 Titanrute", "cost": 7500, "rare_bonus": 2.3, "value_bonus": 1.8, "junk_reduce": 0.35},
    "mythic": {"name": "🔱 Neptuns Dreizack", "cost": 20000, "rare_bonus": 3.2, "value_bonus": 2.3, "junk_reduce": 0.15},
}

COOLDOWN_UPGRADES = {
    0: {"name": "Stufe 0", "cooldown": 45, "cost": 0},
    1: {"name": "Stufe 1", "cooldown": 35, "cost": 500},
    2: {"name": "Stufe 2", "cooldown": 25, "cost": 1500},
    3: {"name": "Stufe 3", "cooldown": 18, "cost": 4000},
    4: {"name": "Stufe 4", "cooldown": 12, "cost": 10000},
}

BAIT_PACKS = {
    "small": {"name": "🪱 Kleines Köderpaket", "amount": 5, "cost": 100},
    "medium": {"name": "🪱 Mittleres Köderpaket", "amount": 15, "cost": 250},
    "large": {"name": "🪱 Großes Köderpaket", "amount": 40, "cost": 600},
}

COINS_PER_CHANNEL_POINT = 15

# ── Jobs / Minigames Daten ──────────────────────────────────────────────────
WOODCUTTING_TREES = [
    {"name": "Morscher Ast", "emoji": "🪵", "min_val": 5, "max_val": 12, "weight": 100, "rarity": "common"},
    {"name": "Birkenholz", "emoji": "🌳", "min_val": 12, "max_val": 28, "weight": 90, "rarity": "common"},
    {"name": "Fichtenholz", "emoji": "🌲", "min_val": 18, "max_val": 38, "weight": 75, "rarity": "common"},
    {"name": "Eichenholz", "emoji": "🌳", "min_val": 35, "max_val": 75, "weight": 50, "rarity": "uncommon"},
    {"name": "Kirschbaumholz", "emoji": "🌸", "min_val": 50, "max_val": 105, "weight": 35, "rarity": "uncommon"},
    {"name": "Mahagoni", "emoji": "🪵", "min_val": 95, "max_val": 190, "weight": 20, "rarity": "rare"},
    {"name": "Ebenholz", "emoji": "⬛", "min_val": 140, "max_val": 280, "weight": 12, "rarity": "rare"},
    {"name": "Uralte Mammutbaum-Rinde", "emoji": "🌲", "min_val": 280, "max_val": 550, "weight": 5, "rarity": "epic"},
    {"name": "Weltenbaum-Zweig", "emoji": "✨", "min_val": 850, "max_val": 1700, "weight": 1, "rarity": "legendary"},
]
WOODCUTTING_AXES = {
    "stone": {"name": "🪨 Steinaxt", "cost": 0, "bonus": 1.0, "rare_bonus": 1.0},
    "iron": {"name": "🪓 Eisenaxt", "cost": 800, "bonus": 1.25, "rare_bonus": 1.3},
    "steel": {"name": "⚙️ Stahlaxt", "cost": 2500, "bonus": 1.55, "rare_bonus": 1.7},
    "diamond": {"name": "💎 Diamantaxt", "cost": 7500, "bonus": 1.95, "rare_bonus": 2.3},
    "mythic": {"name": "🔥 Phönix-Axt", "cost": 20000, "bonus": 2.5, "rare_bonus": 3.2},
}
WOODCUTTING_COOLDOWN_UPGRADES = {
    0: {"name": "Stufe 0", "cooldown": 45, "cost": 0},
    1: {"name": "Stufe 1", "cooldown": 35, "cost": 500},
    2: {"name": "Stufe 2", "cooldown": 25, "cost": 1500},
    3: {"name": "Stufe 3", "cooldown": 18, "cost": 4000},
    4: {"name": "Stufe 4", "cooldown": 12, "cost": 10000},
}

MINING_ORES = [
    {"name": "Bruchstein", "emoji": "🪨", "min_val": 5, "max_val": 14, "weight": 100, "rarity": "common"},
    {"name": "Kohle", "emoji": "⚫", "min_val": 12, "max_val": 30, "weight": 85, "rarity": "common"},
    {"name": "Kupfererz", "emoji": "🟫", "min_val": 18, "max_val": 42, "weight": 70, "rarity": "common"},
    {"name": "Eisenerz", "emoji": "🔩", "min_val": 38, "max_val": 80, "weight": 48, "rarity": "uncommon"},
    {"name": "Silbererz", "emoji": "🪙", "min_val": 55, "max_val": 115, "weight": 32, "rarity": "uncommon"},
    {"name": "Golderz", "emoji": "🟨", "min_val": 105, "max_val": 210, "weight": 18, "rarity": "rare"},
    {"name": "Smaragd", "emoji": "💚", "min_val": 150, "max_val": 310, "weight": 11, "rarity": "rare"},
    {"name": "Diamant", "emoji": "💎", "min_val": 320, "max_val": 650, "weight": 5, "rarity": "epic"},
    {"name": "Sternenkristall", "emoji": "🌟", "min_val": 950, "max_val": 1900, "weight": 1, "rarity": "legendary"},
]
MINING_PICKAXES = {
    "wood": {"name": "🪵 Holzspitzhacke", "cost": 0, "bonus": 1.0, "rare_bonus": 1.0},
    "iron": {"name": "⛏️ Eisenspitzhacke", "cost": 800, "bonus": 1.25, "rare_bonus": 1.3},
    "steel": {"name": "⚙️ Stahlspitzhacke", "cost": 2500, "bonus": 1.55, "rare_bonus": 1.7},
    "diamond": {"name": "💎 Diamantspitzhacke", "cost": 7500, "bonus": 1.95, "rare_bonus": 2.3},
    "mythic": {"name": "⚡ Mjolnir-Bohrer", "cost": 20000, "bonus": 2.5, "rare_bonus": 3.2},
}
MINING_COOLDOWN_UPGRADES = dict(WOODCUTTING_COOLDOWN_UPGRADES)

HUNTING_ANIMALS = [
    {"name": "Kaninchen", "emoji": "🐇", "min_val": 8, "max_val": 20, "weight": 100, "rarity": "common"},
    {"name": "Fasan", "emoji": "🐦", "min_val": 14, "max_val": 32, "weight": 85, "rarity": "common"},
    {"name": "Fuchs", "emoji": "🦊", "min_val": 25, "max_val": 55, "weight": 60, "rarity": "uncommon"},
    {"name": "Wildschwein", "emoji": "🐗", "min_val": 45, "max_val": 95, "weight": 42, "rarity": "uncommon"},
    {"name": "Hirsch", "emoji": "🦌", "min_val": 95, "max_val": 195, "weight": 22, "rarity": "rare"},
    {"name": "Wolf", "emoji": "🐺", "min_val": 135, "max_val": 270, "weight": 13, "rarity": "rare"},
    {"name": "Braunbär", "emoji": "🐻", "min_val": 300, "max_val": 600, "weight": 5, "rarity": "epic"},
    {"name": "Weißer Hirsch", "emoji": "✨", "min_val": 900, "max_val": 1800, "weight": 1, "rarity": "legendary"},
]
HUNTING_BOWS = {
    "shortbow": {"name": "🏹 Kurzbogen", "cost": 0, "bonus": 1.0, "rare_bonus": 1.0},
    "longbow": {"name": "🏹 Langbogen", "cost": 800, "bonus": 1.25, "rare_bonus": 1.3},
    "crossbow": {"name": "🎯 Armbrust", "cost": 2500, "bonus": 1.55, "rare_bonus": 1.7},
    "compound": {"name": "⚙️ Compoundbogen", "cost": 7500, "bonus": 1.95, "rare_bonus": 2.3},
    "mythic": {"name": "🌟 Artemis' Bogen", "cost": 20000, "bonus": 2.5, "rare_bonus": 3.2},
}
HUNTING_COOLDOWN_UPGRADES = dict(WOODCUTTING_COOLDOWN_UPGRADES)

SMITHING_ITEMS = [
    {"name": "Eisendolch", "emoji": "🗡️", "base_val": 45, "hits": 3},
    {"name": "Ritterschwert", "emoji": "⚔️", "base_val": 65, "hits": 4},
    {"name": "Stahlschild", "emoji": "🛡️", "base_val": 75, "hits": 4},
    {"name": "Kriegshammer", "emoji": "🔨", "base_val": 95, "hits": 5},
    {"name": "Drachenrüstung", "emoji": "🐲", "base_val": 130, "hits": 5},
]
SMITHING_HAMMERS = {
    "stone": {"name": "🪨 Steinhammer", "cost": 0, "bonus": 1.0},
    "iron": {"name": "🔨 Eisenhammer", "cost": 800, "bonus": 1.3},
    "steel": {"name": "⚒️ Stahlhammer", "cost": 2500, "bonus": 1.65},
    "diamond": {"name": "💎 Runenhammer", "cost": 7500, "bonus": 2.1},
    "mythic": {"name": "🔥 Hephaistos' Hammer", "cost": 20000, "bonus": 2.7},
}
SMITHING_COOLDOWN_UPGRADES = {
    0: {"name": "Stufe 0", "cooldown": 60, "cost": 0},
    1: {"name": "Stufe 1", "cooldown": 45, "cost": 600},
    2: {"name": "Stufe 2", "cooldown": 35, "cost": 1800},
    3: {"name": "Stufe 3", "cooldown": 25, "cost": 4500},
    4: {"name": "Stufe 4", "cooldown": 15, "cost": 11000},
}

COOKING_ALL_INGREDIENTS = ["🍅", "🧀", "🥩", "🍄", "🧅", "🧄", "🥔", "🥕", "🌶️", "🥚", "🥓", "🌿"]
COOKING_RECIPES = [
    {"name": "Pizza Margherita", "emoji": "🍕", "ingredients": ["🍅", "🧀", "🌿"], "base_val": 50},
    {"name": "Burger Spezial", "emoji": "🍔", "ingredients": ["🥩", "🧀", "🧅", "🍅"], "base_val": 70},
    {"name": "Bauern-Omelett", "emoji": "🍳", "ingredients": ["🥚", "🥓", "🍄", "🧅"], "base_val": 75},
    {"name": "Scharfes Chili", "emoji": "🍲", "ingredients": ["🥩", "🌶️", "🍅", "🧄"], "base_val": 85},
    {"name": "Festtagsbraten", "emoji": "🍖", "ingredients": ["🥩", "🥔", "🥕", "🧄", "🌿"], "base_val": 120},
]
COOKING_TOOLS = {
    "basic": {"name": "🍳 Alte Pfanne", "cost": 0, "bonus": 1.0},
    "copper": {"name": "🥘 Kupferpfanne", "cost": 800, "bonus": 1.3},
    "cast_iron": {"name": "🫕 Gusseisentopf", "cost": 2500, "bonus": 1.65},
    "pro": {"name": "👨‍🍳 Profi-Küche", "cost": 7500, "bonus": 2.1},
    "mythic": {"name": "⭐ Sterne-Küche", "cost": 20000, "bonus": 2.7},
}
COOKING_COOLDOWN_UPGRADES = dict(SMITHING_COOLDOWN_UPGRADES)

# ── Radio-Presets ───────────────────────────────────────────────────────────
RADIO_STATIONS = {
    "1live": {
        "name": "1LIVE (WDR)",
        "emoji": "📻",
        "url": "https://wdr-1live-live.icecastssl.wdr.de/wdr/1live/live/mp3/128/stream.mp3",
        "description": "Pop, Charts & junge Musik",
        "search": "1LIVE",
    },
    "bigfm": {
        "name": "bigFM",
        "emoji": "🔥",
        "url": "https://streams.bigfm.de/bigfm-deutschland-128-mp3",
        "description": "Hip-Hop, Rap & Urban Beats",
        "search": "bigFM",
    },
    "swr3": {
        "name": "SWR3",
        "emoji": "🎵",
        "url": "https://liveradio.swr.de/sw282p3/swr3/play.mp3",
        "description": "Pop, Rock & aktuelle Hits",
        "search": "SWR3",
    },
    "bayern3": {
        "name": "BAYERN 3",
        "emoji": "🥨",
        "url": "https://dispatcher.rndfnk.com/br/br3/live/mp3/mid",
        "description": "Bayerns Hitradio Nummer 1",
        "search": "Bayern 3",
    },
    "sunshine": {
        "name": "Sunshine Live",
        "emoji": "🎧",
        "url": "https://stream.sunshine-live.de/live/mp3-192",
        "description": "Electronic Music, Techno & House",
        "search": "Sunshine Live",
    },
    "rockantenne": {
        "name": "ROCK ANTENNE",
        "emoji": "🎸",
        "url": "https://s1-webradio.rockantenne.de/rockantenne/stream/mp3",
        "description": "100% Rock Nonstop",
        "search": "Rock Antenne",
    },
    "lofi": {
        "name": "Lofi Chill Beats",
        "emoji": "☕",
        "url": "https://streams.ilovemusic.de/iloveradio17.mp3",
        "description": "Entspannte Lofi- & Study-Beats",
        "search": "Chillhop",
    },
    "ilovemusic": {
        "name": "I Love Radio",
        "emoji": "❤️",
        "url": "https://streams.ilovemusic.de/iloveradio1.mp3",
        "description": "Charts, Dance & Party-Hits",
        "search": "I Love Radio",
    },
}
