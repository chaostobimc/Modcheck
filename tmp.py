import os
import random
import discord
from discord.ext import commands, tasks
from discord import app_commands
from twitchio.ext import commands as t_commands
from dotenv import load_dotenv
from openai import OpenAI
import asyncio
import json
import datetime
import calendar
import re
import aiohttp
from collections import deque
from urllib.parse import urlparse


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

# Weekly Review
WEEKLY_REVIEW_CHANNEL_ID = int(os.getenv("WEEKLY_REVIEW_CHANNEL_ID", "0"))
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

# Ignorierte Kanäle für Automod
IGNORED_CHANNELS_RAW = os.getenv("IGNORED_CHANNELS", "")
IGNORED_CHANNELS = set()
if IGNORED_CHANNELS_RAW.strip():
    try:
        IGNORED_CHANNELS = set(
            int(ch_id.strip())
            for ch_id in IGNORED_CHANNELS_RAW.split(",")
            if ch_id.strip()
        )
    except ValueError:
        print("⚠️ Warnung: IGNORED_CHANNELS enthält ungültige Werte!")


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

### MUSIC MODULE START ###
COLOR_MUSIC = 0xFF1DB8
QUEUE_PAGE_SIZE = 10
### MUSIC MODULE END ###

AI_FOOTER = (
    "\n\n-# 🛈 Ich bin eine KI und kann Fehler machen. "
    "Bitte überprüfe wichtige Informationen."
)
MOD_FOOTER = (
    "\n\n-# 🛈 Ich bin eine KI und kann Fehler machen. "
    "Bitte wende dich bei Einwänden gegen diese Entscheidung "
    "direkt an einen Moderator."
)

URL_PATTERN = re.compile(
    r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|'
    r'(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    r'|(?:www\.)[a-zA-Z0-9-]+\.[a-zA-Z]{2,}'
    r'|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?'
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
            if uid in data["streams"][date_str]:
                present_days += 1
            else:
                absent_days += 1
    return {
        "stream_days": stream_days,
        "present": present_days,
        "absent": absent_days,
    }


def get_current_streak(data: dict, uid: str) -> int:
    sorted_dates = sorted(data["streams"].keys(), reverse=True)
    streak = 0
    for date_str in sorted_dates:
        if uid in data["streams"][date_str]:
            streak += 1
        else:
            break
    return streak


def get_longest_streak(data: dict, uid: str) -> int:
    sorted_dates = sorted(data["streams"].keys())
    longest = 0
    current = 0
    for date_str in sorted_dates:
        if uid in data["streams"][date_str]:
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

channel_histories = {}

# Globale Referenz auf den Discord Bot für Twitch-Events
discord_bot_ref = None


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


async def analyze_link(message: discord.Message, url: str, bot):
    if not ai_enabled or not key_rotator:
        return {"status": "SAFE"}
    try:
        parsed = urlparse(
            url if url.startswith('http') else f'http://{url}'
        )
        domain = parsed.netloc or parsed.path.split('/')[0]
        prompt = (
            f"Du bist ein Sicherheitsexperte für Phishing- und "
            f"Scam-Erkennung. Analysiere die folgende Domain.\n\n"
            f"Prüfe auf:\n"
            f'1. Tippfehler bekannter Domains (z.B. "steeam" statt "steam")\n'
            f"2. Verdächtige TLDs (.tk, .ml, .ga, .cf, .gq)\n"
            f"3. Verdächtige Subdomains oder übermäßig lange Domains\n"
            f"4. Homoglyphen-Attacken (ähnlich aussehende Zeichen)\n"
            f"5. URL-Shortener ohne erkennbares Ziel\n\n"
            f"Domain: {domain}\n"
            f"Vollständige URL: {url}\n\n"
            f"Antworte NUR im JSON-Format: "
            f'{{"status": "SAFE" oder "SUSPICIOUS", '
            f'"reason": "Kurze Begründung auf Deutsch", '
            f'"confidence": "niedrig/mittel/hoch", '
            f'"user_message": "Kurze freundliche Nachricht an den User"}}\n\n'
            f"Bei bekannten legitimen Domains (discord.com, youtube.com, "
            f"github.com, twitch.tv, etc.) antworte immer mit SAFE."
        )
        response = await key_rotator.call_with_rotation(
            lambda c: c.chat.completions.create(
                model=MODERATION_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                extra_headers={"X-Title": "Discord Link Analyzer"},
            )
        )
        if (
            not response
            or not response.choices
            or not response.choices[0].message.content
        ):
            return {"status": "SAFE"}
        result = json.loads(response.choices[0].message.content)
        user_message = result.get("user_message", "")
        if result.get("status") == "SUSPICIOUS":
            user_message = (
                user_message or "Dieser Link ist verdächtig! Sei vorsichtig."
            )
        else:
            user_message = (
                user_message or "Dieser Link scheint sicher zu sein."
            )
        try:
            await message.channel.send(
                f"{message.author.mention} {user_message}",
                delete_after=15,
            )
        except Exception as e:
            print(f"Fehler beim Senden der Link-Warnung: {e}")
        if result.get("status") == "SUSPICIOUS":
            embed = discord.Embed(
                title="🔗 Verdächtiger Link erkannt",
                description=(
                    "Die KI hat einen potenziell gefährlichen "
                    "Link identifiziert"
                ),
                color=COLOR_WARNING,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            embed.add_field(
                name="👤 Gepostet von",
                value=f"{message.author.mention} ({message.author.name})",
                inline=True,
            )
            embed.add_field(
                name="📍 Kanal", value=message.channel.mention, inline=True
            )
            embed.add_field(
                name="🆔 User-ID", value=str(message.author.id), inline=True
            )
            embed.add_field(name="🔗 URL", value=f"`{url}`", inline=False)
            embed.add_field(name="🌐 Domain", value=f"`{domain}`", inline=True)
            embed.add_field(
                name="⚖️ KI-Analyse",
                value=result.get("reason", "Keine Angabe"),
                inline=False,
            )
            embed.add_field(
                name="📊 Vertrauensstufe",
                value=result.get("confidence", "unbekannt").upper(),
                inline=True,
            )
            embed.add_field(
                name="💬 Nachricht",
                value=f"[Zum Post springen]({message.jump_url})",
                inline=False,
            )
            embed.set_thumbnail(url=message.author.display_avatar.url)
            embed.set_footer(text="⚠️ Link wurde NICHT automatisch gelöscht")
            await send_log(embed, bot)
        return result
    except json.JSONDecodeError as e:
        print(f"JSON-Fehler bei Link-Analyse: {e}")
        return {"status": "SAFE"}
    except Exception as e:
        print(f"Fehler bei Link-Analyse: {e}")
        return {"status": "SAFE"}


async def moderate_message(message: discord.Message) -> dict:
    if not ai_enabled or not key_rotator:
        return {"status": "CLEAN"}
    if message.channel.id in IGNORED_CHANNELS:
        return {"status": "CLEAN"}
    channel_id = message.channel.id
    history = list(channel_histories.get(channel_id, []))
    context_str = "\n".join(
        [f"{m['author']}: {m['content']}" for m in history]
    )
    prompt = (
        f"Du bist ein hochmoderner Discord-Moderations-Assistent. "
        f"Deine Aufgabe ist es, Nachrichten auf Toxizität, Beleidigungen, "
        f"Hassrede und Umgehungsversuche von Filtern zu prüfen.\n\n"
        f"Regeln:\n"
        f"1. Erkenne direkte Beleidigungen sowie indirekte/versteckte "
        f"Toxizität in Konversationen.\n"
        f"2. Erkenne Umgehungsversuche (z.B. Leetspeak) nur wenn das "
        f"Wort auch eine Beleidigung ist.\n"
        f"3. Antworte NUR im JSON-Format: "
        f'{{"status": "CLEAN" oder "TOXIC", '
        f'"reason": "Kurze Begründung (Deutsch)"}}\n'
        f"4. 'TOXIC' nur bei echtem Fehlverhalten. Sarkasmus ist erlaubt.\n\n"
        f"Kontext der letzten Nachrichten:\n{context_str}\n\n"
        f"Aktuelle Nachricht von {message.author.display_name}:\n"
        f"{message.content}"
    )
    try:
        response = await key_rotator.call_with_rotation(
            lambda c: c.chat.completions.create(
                model=MODERATION_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                extra_headers={"X-Title": "Discord AI Mod"},
            )
        )
        if (
            not response
            or not response.choices
            or not response.choices[0].message.content
        ):
            return {"status": "CLEAN"}
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Moderationsfehler: {e}")
        return {"status": "CLEAN"}


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
    async with message.channel.typing():
        try:
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
            print(f"KI-Antwort Fehler: {e}")
            await message.reply("Ein interner Fehler ist aufgetreten.")


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
                f"Voice: {channel_name}"
            ),
            reason=f"Temp Voice Text-Kanal: {member.display_name}",
        )

        temp_voice_channels[voice_channel.id] = {
            "owner_id": member.id,
            "text_channel_id": text_channel.id,
            "control_message_id": 0,
            "allowed_users": set(),
            "locked": False,
            "limit": 0,
        }

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


async def send_weekly_review(channel: discord.TextChannel, guild: discord.Guild):
    """Sendet den kompletten wöchentlichen Review in den Kanal."""
    data = load_data()
    if not data["users"]:
        await channel.send("❌ Keine User registriert – kein Weekly Review möglich.")
        return

    monday, sunday = get_week_dates()
    today = datetime.date.today()
    now = datetime.datetime.now(datetime.timezone.utc)

    week_str = f"{monday.strftime('%d.%m.%Y')} – {sunday.strftime('%d.%m.%Y')}"

    # Header Embed
    header_embed = discord.Embed(
        title="📅 Wöchentlicher Aktivitätsbericht",
        description=(
            f"**Kalenderwoche {monday.isocalendar()[1]}** • {week_str}\n\n"
            f"Hier ist der Überblick der Aktivität aller registrierten User für diese Woche."
        ),
        color=COLOR_WEEKLY,
        timestamp=now,
    )
    header_embed.set_footer(
        text=f"Tracking basiert auf Chat-Aktivität • {STREAMER_CHANNEL}"
    )
    await channel.send(embed=header_embed)

    # Statistiken für alle User berechnen
    user_stats = {}
    for uid in data["users"]:
        stats = get_weekly_user_stats(data, uid, monday, sunday)
        user_stats[uid] = stats

    # Nach Anwesenheit sortieren
    sorted_users = sorted(
        user_stats.items(),
        key=lambda x: (x[1]["present"], x[1]["pct"]),
        reverse=True,
    )

    # Einzelne User-Embeds senden
    for rank, (uid, stats) in enumerate(sorted_users, 1):
        user_info = data["users"][uid]
        twitch_name = user_info.get("twitch_name", "?")
        display_name = user_info.get("display_name", "Unbekannt")

        member = guild.get_member(int(uid))
        avatar_url = member.display_avatar.url if member else None
        mention = member.mention if member else f"**{display_name}**"

        bar = make_progress_bar(stats["present"], stats["stream_days"], 10)
        day_line = build_weekly_day_line(stats["day_details"])
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
            name=f"{stats['grade_emoji']} Wochen-Note",
            value=f"**{stats['grade']}**",
            inline=True,
        )
        embed.add_field(
            name="⚡ Streak",
            value=f"**{current_streak}** Tage\n{flames}",
            inline=True,
        )
        embed.add_field(
            name="📺 Streams diese Woche",
            value=f"**{stats['stream_days']}**",
            inline=True,
        )
        embed.add_field(
            name="✅ Anwesend",
            value=f"**{stats['present']}**",
            inline=True,
        )
        embed.add_field(
            name="❌ Gefehlt",
            value=f"**{stats['absent']}**",
            inline=True,
        )
        embed.add_field(
            name="📈 Wochen-Quote",
            value=bar,
            inline=False,
        )
        embed.add_field(
            name="🗓️ Tagesübersicht",
            value=day_line,
            inline=False,
        )

        embed.set_footer(
            text=f"Rang #{rank} von {len(sorted_users)} • KW {monday.isocalendar()[1]}"
        )

        await channel.send(embed=embed)
        await asyncio.sleep(0.5)

    # ── Leaderboard Embed am Ende ────────────────────────

    leaderboard_lines = []
    for rank, (uid, stats) in enumerate(sorted_users, 1):
        display_name = data["users"][uid].get("display_name", "Unbekannt")
        twitch_name = data["users"][uid].get("twitch_name", "?")
        member = guild.get_member(int(uid))
        mention = member.mention if member else f"**{display_name}**"

        if rank <= 3:
            medal = ["🥇", "🥈", "🥉"][rank - 1]
            pct_str = f"{stats['pct']:.0f}%"
            bar_small = make_progress_bar(stats["present"], stats["stream_days"], 8)
            leaderboard_lines.append(
                f"{medal} {mention} (`{twitch_name}`)\n"
                f"╰ {stats['present']}/{stats['stream_days']} Tage "
                f"({pct_str}) — {bar_small}"
            )
        else:
            pct_str = f"{stats['pct']:.0f}%"
            leaderboard_lines.append(
                f"`#{rank}` {mention} (`{twitch_name}`) — "
                f"{stats['present']}/{stats['stream_days']} Tage ({pct_str})"
            )

    total_week_streams = 0
    for i in range(7):
        day = monday + datetime.timedelta(days=i)
        if day > today:
            break
        if str(day) in data["streams"]:
            total_week_streams += 1

    total_participations = sum(s["present"] for s in user_stats.values())
    avg_pct = (
        sum(s["pct"] for s in user_stats.values()) / len(user_stats)
        if user_stats else 0
    )

    lb_embed = discord.Embed(
        title="🏆 Wochen-Leaderboard",
        description="\n\n".join(leaderboard_lines),
        color=COLOR_GOLD,
        timestamp=now,
    )
    lb_embed.add_field(
        name="📊 Wochen-Zusammenfassung",
        value=(
            f"📺 **Streams diese Woche:** `{total_week_streams}`\n"
            f"👥 **Registrierte User:** `{len(data['users'])}`\n"
            f"✅ **Gesamt-Teilnahmen:** `{total_participations}`\n"
            f"📈 **Durchschnittliche Quote:** `{avg_pct:.0f}%`"
        ),
        inline=False,
    )
    lb_embed.set_footer(text=f"📅 KW {monday.isocalendar()[1]} • {week_str}")

    await channel.send(embed=lb_embed)

    print(
        f"✅ [Weekly Review] Review für KW {monday.isocalendar()[1]} "
        f"gesendet ({len(sorted_users)} User)"
    )
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

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -af "loudnorm=I=-16:TP=-1.5:LRA=11" -b:a 128k',
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
}

YTDL_SEARCH_OPTIONS = {
    **YTDL_FORMAT_OPTIONS,
    'noplaylist': True,
    'extract_flat': True,
    'default_search': 'ytsearch5',
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

    def reset_idle(self):
        self._idle_seconds = 0

    def increment_idle(self):
        self._idle_seconds += 1

    @property
    def is_idle_timeout(self) -> bool:
        return self._idle_seconds >= MUSIC_IDLE_TIMEOUT


music_states: dict[int, GuildMusicState] = {}


def get_music_state(guild_id: int, bot) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id, bot)
    return music_states[guild_id]


async def ytdl_extract(query: str, options: dict) -> dict | None:
    if not MUSIC_ENABLED:
        return None
    loop = asyncio.get_event_loop()
    try:
        def _extract():
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(query, download=False)
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        print(f"❌ [Music] yt-dlp Fehler: {e}")
        return None


async def resolve_song_url(song: SongInfo) -> str | None:
    try:
        info = await ytdl_extract(song.webpage_url, YTDL_FORMAT_OPTIONS)
        if info and 'url' in info:
            return info['url']
        if info and 'entries' in info:
            for entry in info['entries']:
                if entry and 'url' in entry:
                    return entry['url']
    except Exception as e:
        print(f"❌ [Music] URL-Resolve Fehler: {e}")
    return None


async def search_tracks(query: str) -> list[dict]:
    info = await ytdl_extract(query, YTDL_SEARCH_OPTIONS)
    if not info:
        return []
    if 'entries' in info:
        return [e for e in info['entries'] if e]
    return [info] if info.get('url') or info.get('webpage_url') else []


async def extract_playlist(url: str) -> list[dict]:
    """Extrahiert alle Tracks einer YouTube-Playlist."""
    info = await ytdl_extract(url, YTDL_PLAYLIST_OPTIONS)
    if not info:
        return []
    if 'entries' in info:
        return [e for e in info['entries'] if e]
    return [info] if info.get('url') or info.get('webpage_url') else []


async def get_related_track(video_url: str) -> dict | None:
    try:
        video_id = _extract_video_id(video_url)
        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        opts = {**YTDL_FORMAT_OPTIONS, 'noplaylist': False, 'extract_flat': True, 'playlistend': 5}
        info = await ytdl_extract(mix_url, opts)
        if info and 'entries' in info:
            entries = [e for e in info['entries'] if e and e.get('url') != video_url]
            if entries:
                return random.choice(entries[:5])
    except Exception as e:
        print(f"⚠️ [Music] Autoplay-Suche fehlgeschlagen: {e}")
    try:
        fallback_info = await ytdl_extract(f"ytsearch3:{video_url} mix", YTDL_SEARCH_OPTIONS)
        if fallback_info and 'entries' in fallback_info:
            entries = [e for e in fallback_info['entries'] if e]
            if entries:
                return random.choice(entries)
    except Exception:
        pass
    return None


def _extract_video_id(url: str) -> str:
    patterns = [r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})', r'(?:embed/)([a-zA-Z0-9_-]{11})']
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url


def _is_url(query: str) -> bool:
    return query.startswith(('http://', 'https://', 'www.'))


def _is_playlist_url(query: str) -> bool:
    return _is_url(query) and ('list=' in query or '/playlist' in query)


async def play_next(state: GuildMusicState, text_channel: discord.TextChannel = None):
    async with state._lock:
        if not state.voice_client or not state.voice_client.is_connected():
            return

        if not state.queue:
            if state.autoplay and state.history:
                last_url = state.history[-1]
                related = await get_related_track(last_url)
                if related:
                    title = related.get('title', 'Unbekannt')
                    webpage_url = related.get('url') or related.get('webpage_url', '')
                    duration = int(related.get('duration', 0)) if related.get('duration') else 0
                    thumbnail = related.get('thumbnail', '')
                    bot_member = state.voice_client.guild.me
                    autoplay_song = SongInfo(title, '', webpage_url, duration, thumbnail, bot_member)
                    state.queue.append(autoplay_song)
                    if text_channel:
                        embed = discord.Embed(
                            title="🔄 Autoplay",
                            description=f"[{title}]({webpage_url})",
                            color=COLOR_MUSIC,
                        )
                        embed.set_footer(text="Basierend auf deinen letzten Songs")
                        try:
                            await text_channel.send(embed=embed, delete_after=30)
                        except Exception:
                            pass
                else:
                    state.current = None
                    return
            else:
                state.current = None
                return

        song = state.queue.popleft()
        state.current = song
        state.reset_idle()

        stream_url = await resolve_song_url(song)
        if not stream_url:
            if text_channel:
                try:
                    await text_channel.send(
                        f"❌ Konnte **{song.title}** nicht abspielen. Überspringe...",
                        delete_after=10,
                    )
                except Exception:
                    pass
            await play_next(state, text_channel)
            return

        try:
            source = discord.FFmpegOpusAudio(stream_url, **FFMPEG_OPTIONS)
        except Exception as e:
            print(f"❌ [Music] FFmpeg Fehler: {e}")
            if text_channel:
                try:
                    await text_channel.send(
                        f"❌ FFmpeg-Fehler bei **{song.title}**. Überspringe...",
                        delete_after=10,
                    )
                except Exception:
                    pass
            await play_next(state, text_channel)
            return

        if song.webpage_url:
            state.history.append(song.webpage_url)

        def after_playing(error):
            if error:
                print(f"❌ [Music] Playback-Fehler: {error}")
            asyncio.run_coroutine_threadsafe(
                play_next(state, text_channel), state.bot.loop
            )

        try:
            state.voice_client.play(source, after=after_playing)
        except Exception as e:
            print(f"❌ [Music] Play-Fehler: {e}")
            await play_next(state, text_channel)
            return

        if text_channel:
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
            try:
                await text_channel.send(
                    embed=embed,
                    delete_after=song.duration + 5 if song.duration > 0 else 60,
                )
            except Exception as e:
                print(f"⚠️ [Music] Embed senden fehlgeschlagen: {e}")


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
            embed.add_field(
                name="🎶 Jetzt spielt",
                value=f"[{self.state.current.title}]({self.state.current.webpage_url}) — `{self.state.current.duration_str}`\n{req}",
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
                lines.append(f"`{i}.` **{title_short}** — `{song.duration_str}` — {req}")
            total_duration = int(sum(s.duration for s in queue_list))
            total_min = total_duration // 60
            total_sec = total_duration % 60
            embed.add_field(
                name=f"📋 Songs {start + 1}–{end} von {total} (Gesamt: {total_min}:{total_sec:02d})",
                value="\n".join(lines),
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
        webpage_url = entry.get('url') or entry.get('webpage_url', '')
        duration = int(entry.get('duration', 0)) if entry.get('duration') else 0
        thumbnail = entry.get('thumbnail', '')
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
        print(f"💬 [Twitch] {message.author.name}: {message.content}")
        data = load_data()
        today = str(datetime.date.today())
        for discord_id, info in data["users"].items():
            if info.get("twitch_name", "").lower() == chatter:
                if today not in data["streams"]:
                    data["streams"][today] = []
                if discord_id not in data["streams"][today]:
                    data["streams"][today].append(discord_id)
                    save_data(data)
                    print(
                        f"  ⭐ {message.author.name} → erfasst für {today}"
                    )
                break
        await self.handle_commands(message)

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
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print("✅ Discord: Slash-Commands synchronisiert")


d_bot = DiscordBot()


# ══════════════════════════════════════════════════════════
#         WEEKLY REVIEW – SCHEDULED TASK
# ══════════════════════════════════════════════════════════

@tasks.loop(minutes=1)
async def weekly_review_scheduler():
    """Prüft jede Minute ob es Sonntag 20:00 UTC ist und sendet den Review."""
    if not WEEKLY_REVIEW_CHANNEL_ID:
        return

    now = datetime.datetime.now(datetime.timezone.utc)

    # Sonntag = weekday() == 6, Uhrzeit 20:00
    if now.weekday() == 6 and now.hour == 20 and now.minute == 0:
        try:
            channel = d_bot.get_channel(WEEKLY_REVIEW_CHANNEL_ID)
            if not channel:
                print(f"⚠️ [Weekly] Kanal {WEEKLY_REVIEW_CHANNEL_ID} nicht gefunden!")
                return

            guild = channel.guild
            await send_weekly_review(channel, guild)
        except Exception as e:
            print(f"❌ [Weekly] Fehler beim automatischen Review: {e}")


@weekly_review_scheduler.before_loop
async def before_weekly_scheduler():
    await d_bot.wait_until_ready()
    print("✅ [Weekly] Scheduler gestartet – wartet auf Sonntag 20:00 UTC")


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

        # Idle-Timer (nur wenn nichts spielt)
        if not state.voice_client.is_playing() and not state.voice_client.is_paused():
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
        f"  📅 Weekly Review: "
        f"{'✅ Kanal-ID: ' + str(WEEKLY_REVIEW_CHANNEL_ID) if WEEKLY_REVIEW_CHANNEL_ID else '❌ Deaktiviert'}"
    )
    ### MUSIC MODULE START ###
    print(
        f"  🎵 Musik-Modul: "
        f"{'✅ Aktiv' if MUSIC_ENABLED else '❌ Deaktiviert'}"
    )
    ### MUSIC MODULE END ###
    print(f"  👑 Admin Users: {len(ADMIN_USER_IDS)} konfiguriert")
    if PROXYCHECK_API_KEY:
        print("  🛡️ ProxyCheck.io: ✅ Aktiv")
    if IGNORED_CHANNELS:
        print(f"  🚫 Ignorierte Kanäle: {IGNORED_CHANNELS}")
    print("═══════════════════════════════════")
    print("")

    # Weekly Scheduler starten
    if WEEKLY_REVIEW_CHANNEL_ID and not weekly_review_scheduler.is_running():
        weekly_review_scheduler.start()

    ### MUSIC MODULE START ###
    if MUSIC_ENABLED and not music_idle_checker.is_running():
        music_idle_checker.start()
    ### MUSIC MODULE END ###


@d_bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """
    Verwaltet Temp-Voice-Kanäle:
    - Erstellt Kanal wenn jemand dem Trigger-Kanal beitritt
    - Löscht Kanal wenn er leer wird
    """
    if not TEMP_VOICE_CHANNEL_ID:
        return

    # ── User betritt Trigger-Kanal ────────────────────────
    if (
        after.channel is not None
        and after.channel.id == TEMP_VOICE_CHANNEL_ID
    ):
        await create_temp_voice_channel(member, member.guild)
        return

    # ── User betritt einen existierenden Temp-Kanal ──────
    if (
        after.channel is not None
        and after.channel.id in temp_voice_channels
        and after.channel.id != TEMP_VOICE_CHANNEL_ID
    ):
        await update_temp_voice_panel(member.guild, after.channel.id)

    # ── User verlässt einen Temp-Kanal ───────────────────
    if before.channel is not None and before.channel.id in temp_voice_channels:
        channel_id = before.channel.id

        await asyncio.sleep(1)

        voice_channel = member.guild.get_channel(channel_id)
        if voice_channel is None:
            temp_voice_channels.pop(channel_id, None)
            return

        # Wenn Kanal leer ist → löschen
        if len(voice_channel.members) == 0:
            await delete_temp_voice_channel(channel_id, member.guild)
            return

        # Wenn Owner gegangen ist → neuen Owner bestimmen
        vc_data = temp_voice_channels.get(channel_id)
        if vc_data and member.id == vc_data["owner_id"]:
            remaining = voice_channel.members
            if remaining:
                new_owner = remaining[0]
                old_owner = member

                try:
                    await voice_channel.set_permissions(
                        new_owner,
                        manage_channels=True,
                        connect=True,
                        speak=True,
                        move_members=True,
                        mute_members=True,
                        deafen_members=True,
                    )
                    await voice_channel.set_permissions(
                        old_owner,
                        overwrite=None,
                    )

                    text_ch = member.guild.get_channel(
                        vc_data.get("text_channel_id", 0)
                    )
                    if text_ch:
                        await text_ch.set_permissions(
                            new_owner,
                            read_messages=True,
                            send_messages=True,
                            manage_messages=True,
                        )
                        await text_ch.set_permissions(
                            old_owner,
                            overwrite=None,
                        )
                        await text_ch.send(
                            f"👑 **{new_owner.mention}** ist jetzt der "
                            f"neue Admin dieses Kanals!\n"
                            f"-# Der vorherige Admin hat den Kanal verlassen."
                        )

                    vc_data["owner_id"] = new_owner.id

                    print(
                        f"👑 [TempVC] Ownership: "
                        f"{old_owner.display_name} → "
                        f"{new_owner.display_name}"
                    )
                except Exception as e:
                    print(
                        f"❌ [TempVC] Fehler bei Ownership-Transfer: {e}"
                    )

        # Panel aktualisieren
        await update_temp_voice_panel(member.guild, channel_id)


@d_bot.event
async def on_member_join(member: discord.Member):
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
    except Exception as e:
        print(f"Fehler in on_member_join: {e}")


@d_bot.event
async def on_message(message: discord.Message):
    if message.author == d_bot.user:
        return
    if ai_enabled:
        urls = URL_PATTERN.findall(message.content)
        if urls:
            for url in urls:
                await analyze_link(message, url, d_bot)
    if ai_enabled:
        mod_result = await moderate_message(message)
        if mod_result.get("status") == "TOXIC":
            try:
                deleted_content = message.content
                deleted_author = message.author
                deleted_channel = message.channel
                await message.delete()
                warning = await message.channel.send(
                    f"⚠️ {message.author.mention}, deine Nachricht "
                    f"wurde entfernt.\n"
                    f"**Grund:** {mod_result.get('reason')}{MOD_FOOTER}"
                )
                embed = discord.Embed(
                    title="🤖 AI-Automod: Nachricht gelöscht",
                    description=(
                        "Eine toxische Nachricht wurde automatisch entfernt"
                    ),
                    color=COLOR_ERROR,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
                embed.add_field(
                    name="👤 Autor",
                    value=(
                        f"{deleted_author.mention} ({deleted_author.name})"
                    ),
                    inline=True,
                )
                embed.add_field(
                    name="📍 Kanal",
                    value=deleted_channel.mention,
                    inline=True,
                )
                embed.add_field(
                    name="🆔 User-ID",
                    value=str(deleted_author.id),
                    inline=True,
                )
                embed.add_field(
                    name="💬 Gelöschte Nachricht",
                    value=f"```{deleted_content[:1000]}```",
                    inline=False,
                )
                embed.add_field(
                    name="⚖️ KI-Begründung",
                    value=mod_result.get("reason", "Keine Angabe"),
                    inline=False,
                )
                embed.set_thumbnail(url=deleted_author.display_avatar.url)
                embed.set_footer(text="AI-Moderation")
                await send_log(embed, d_bot)
                await asyncio.sleep(10)
                await warning.delete()
                return
            except discord.Forbidden:
                print("Fehlende Berechtigung zum Löschen von Nachrichten.")
            except Exception as e:
                print(f"Fehler beim Löschen: {e}")
    channel_id = message.channel.id
    if channel_id not in channel_histories:
        channel_histories[channel_id] = deque(maxlen=5)
    channel_histories[channel_id].append({
        "author": message.author.display_name,
        "content": message.content,
    })
    if d_bot.user.mentioned_in(message) and ai_enabled:
        user_input = (
            message.content
            .replace(f'<@{d_bot.user.id}>', '')
            .replace(f'<@!{d_bot.user.id}>', '')
            .strip()
        )
        if not user_input:
            await message.reply("Wie kann ich helfen?")
        else:
            await process_ai_reply(message, user_input)
    await d_bot.process_commands(message)


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
#                    CHANNEL CHECK
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
    name="weeklyreview",
    description="Sendet den wöchentlichen Aktivitätsbericht manuell (nur Admins)",
)
@is_admin_user()
async def cmd_weeklyreview(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        review_channel = d_bot.get_channel(WEEKLY_REVIEW_CHANNEL_ID)
        if not review_channel:
            review_channel = interaction.channel

        await send_weekly_review(review_channel, interaction.guild)
        await interaction.followup.send(
            f"✅ Weekly Review wurde in {review_channel.mention} gesendet!",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


### MUSIC MODULE START ###
# ══════════════════════════════════════════════════════════
#            SLASH COMMANDS: MUSIK-MODUL
# ══════════════════════════════════════════════════════════

@d_bot.tree.command(name="play", description="🎵 Spielt einen Song oder eine Playlist ab")
@app_commands.describe(query="Songtitel oder YouTube-URL/Playlist-URL")
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

    # Playlist-URL erkennen
    if _is_playlist_url(query):
        playlist_entries = await extract_playlist(query)
        if not playlist_entries:
            await interaction.followup.send("❌ Konnte keine Songs aus der Playlist laden.")
            return
        songs_added = 0
        for entry in playlist_entries:
            title = entry.get('title', 'Unbekannt')
            webpage_url = entry.get('url') or entry.get('webpage_url', '')
            duration = int(entry.get('duration', 0)) if entry.get('duration') else 0
            thumbnail = entry.get('thumbnail', '')
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

    # Einzelne URL oder Suche – nur bestes Ergebnis
    results = await search_tracks(query)
    if not results:
        await interaction.followup.send(f"❌ Keine Ergebnisse für: `{query}`")
        return

    entry = results[0]
    title = entry.get('title', 'Unbekannt')
    webpage_url = entry.get('url') or entry.get('webpage_url', '')
    duration = int(entry.get('duration', 0)) if entry.get('duration') else 0
    thumbnail = entry.get('thumbnail', '')

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

    results = await search_tracks(query)
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
    if WEEKLY_REVIEW_CHANNEL_ID:
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
            f"{'✅ Aktiv' if WEEKLY_REVIEW_CHANNEL_ID else '❌ Deaktiviert'}\n"
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
    if WEEKLY_REVIEW_CHANNEL_ID:
        embed.add_field(
            name="📅 Nächster Review",
            value=next_review,
            inline=True,
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
    if WEEKLY_REVIEW_CHANNEL_ID:
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
#                  ERROR HANDLER
# ══════════════════════════════════════════════════════════

@d_bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ Befehl hier nicht verfügbar.", ephemeral=True
            )
    else:
        print(f"❌ Command-Fehler: {type(error).__name__}: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Ein Fehler ist aufgetreten.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ Ein Fehler ist aufgetreten.", ephemeral=True
                )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════
#                   HAUPTPROGRAMM
# ══════════════════════════════════════════════════════════

async def main():
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
        f"{'✅ ' + str(WEEKLY_REVIEW_CHANNEL_ID) if WEEKLY_REVIEW_CHANNEL_ID else '❌ FEHLT'}"
        f"                      ║"
    )
    ### MUSIC MODULE START ###
    print(
        f"║  🎵 Musik-Modul:   "
        f"{'✅ Aktiv' if MUSIC_ENABLED else '❌ Deaktiviert'}"
        f"                          ║"
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

    twitch_task = None
    if TWITCH_TOKEN and STREAMER_CHANNEL:
        twitch_bot = TwitchBot()
        twitch_task = asyncio.create_task(twitch_bot.start())
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
        if weekly_review_scheduler.is_running():
            weekly_review_scheduler.cancel()
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
            except asyncio.CancelledError:
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