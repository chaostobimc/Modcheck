"""Zentrale, Thread-sichere Datenbank-Stores für alle Server-Module & dynamischen Kanal-Zuordnungen."""
from __future__ import annotations

import copy
import threading
from typing import Any, Callable, TypeVar

from bot_core.config import (
    BIRTHDAYS_FILE,
    COMMUNITY_FILE,
    COUNTING_FILE,
    GUILD_ID,
    LEVELS_FILE,
    MODERATION_FILE,
    ROLES_FILE,
    SERVER_CONFIG_FILE,
)
from bot_core.storage import _atomic_write_json, _read_json

T = TypeVar("T")


def _deep_merge_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    for k, v in defaults.items():
        if k not in target:
            target[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge_defaults(target[k], v)
    return target


class GuildJsonStore:
    """Thread-sicherer, RAM-gecachter & atomar persistierter Store pro Discord-Guild."""

    def __init__(self, file_path: str, default_guild_factory: Callable[[], dict[str, Any]]) -> None:
        self.file_path = file_path
        self._default_factory = default_guild_factory
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None

    def _ensure_loaded(self) -> dict[str, Any]:
        if self._cache is None:
            raw = _read_json(self.file_path, {"guilds": {}})
            if not isinstance(raw, dict) or not isinstance(raw.get("guilds"), dict):
                raw = {"guilds": {}}
            self._cache = raw
        return self._cache

    def _guild_ref_unlocked(self, guild_id: int) -> dict[str, Any]:
        root = self._ensure_loaded()
        gid_s = str(int(guild_id or GUILD_ID or 0))
        gdict = root["guilds"].get(gid_s)
        if not isinstance(gdict, dict):
            gdict = self._default_factory()
            root["guilds"][gid_s] = gdict
        else:
            _deep_merge_defaults(gdict, self._default_factory())
        return gdict

    def get_guild(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._guild_ref_unlocked(guild_id))

    def save_guild(self, guild_id: int, payload: dict[str, Any]) -> None:
        with self._lock:
            root = copy.deepcopy(self._ensure_loaded())
            gid_s = str(int(guild_id or GUILD_ID or 0))
            merged = _deep_merge_defaults(copy.deepcopy(payload), self._default_factory())
            root["guilds"][gid_s] = merged
            _atomic_write_json(self.file_path, root)
            self._cache = root

    def mutate(self, guild_id: int, mutator: Callable[[dict[str, Any]], T]) -> T:
        with self._lock:
            gdict = copy.deepcopy(self._guild_ref_unlocked(guild_id))
            result = mutator(gdict)
            self.save_guild(guild_id, gdict)
            return copy.deepcopy(result)

    def mutate_readonly_or_save(
        self, guild_id: int, fn: Callable[[dict[str, Any]], tuple[T, bool]]
    ) -> T:
        """Wie `mutate`, schreibt aber nur auf Disk wenn `dirty == True` zurückgegeben wird."""
        with self._lock:
            gdict = copy.deepcopy(self._guild_ref_unlocked(guild_id))
            res, dirty = fn(gdict)
            if dirty:
                self.save_guild(guild_id, gdict)
            return copy.deepcopy(res)

    def all_guilds(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            root = self._ensure_loaded()
            out: dict[int, dict[str, Any]] = {}
            for gid_s, val in root.get("guilds", {}).items():
                if gid_s.isdigit() and isinstance(val, dict):
                    gcopy = copy.deepcopy(val)
                    _deep_merge_defaults(gcopy, self._default_factory())
                    out[int(gid_s)] = gcopy
            return out


# ═══════════════════════════════════════════════════════════════════════════
# 1. SERVER CONFIG, WELCOME, AUTOMOD & AUDIT LOGS (server_config.json)
# ═══════════════════════════════════════════════════════════════════════════

def _default_server_config() -> dict[str, Any]:
    return {
        # Dynamische Überschreibungen für alle .env-Kanäle & Rollen (via /feature-status!)
        "channels": {},
        "roles": {},
        # Welcome / Goodbye / Boost / Counter
        "welcome": {
            "enabled": False,
            "channel_id": 0,
            "message": "Willkommen {user} auf **{server}**! Du bist Mitglied **#{membercount}** 🎉",
            "use_embed": True,
            "embed_title": "👋 Willkommen auf {server}!",
            "embed_color": 0x2ECC71,
            "banner_url": "",
            "dm_enabled": False,
            "dm_message": "Hey {username}, willkommen auf **{server}**! Viel Spaß in der Community 💜",
        },
        "goodbye": {
            "enabled": False,
            "channel_id": 0,
            "message": "👋 **{username}** hat den Server verlassen. Wir sind nun **{membercount}** Mitglieder.",
            "use_embed": True,
        },
        "boost": {
            "enabled": False,
            "channel_id": 0,
            "message": "🚀 Vielen Dank {user} für deinen **Server-Boost**! **{server}** hat jetzt **{boostcount}** Boosts! 💜",
            "coin_reward": 5000,
        },
        "stats_channels": [],  # Liste aus {"channel_id": int, "metric": "members"|"humans"|"bots"|"boosts"|"online", "template": str}
        # AutoMod
        "automod": {
            "enabled": False,
            "log_channel_id": 0,
            "exempt_channels": [],
            "exempt_roles": [],
            "rules": {
                "anti_invite": {"enabled": True, "action": "delete_warn"},
                "anti_link": {"enabled": False, "action": "delete"},
                "anti_spam": {"enabled": True, "action": "delete_timeout", "max_msgs": 6, "per_sec": 5},
                "anti_mass_mention": {"enabled": True, "action": "delete_timeout", "max_mentions": 5},
                "anti_caps": {"enabled": False, "action": "delete", "min_len": 15, "ratio": 0.75},
                "bad_words": {"enabled": True, "action": "delete_warn"},
            },
            "bad_words": [],
            "allowed_domains": [
                "twitch.tv",
                "clips.twitch.tv",
                "youtube.com",
                "youtu.be",
                "music.youtube.com",
                "open.spotify.com",
                "spotify.com",
                "tenor.com",
                "giphy.com",
                "discord.com",
                "discordapp.com",
                "imgur.com",
            ],
        },
        # Server Audit Logs (Carl-bot / ProBot Style)
        "logs": {
            "enabled": True,
            "message_log_channel_id": 0,
            "member_log_channel_id": 0,
            "voice_log_channel_id": 0,
            "mod_log_channel_id": 0,
            "ignored_channels": [],
        },
    }


server_cfg_store = GuildJsonStore(SERVER_CONFIG_FILE, _default_server_config)


def get_dynamic_channel_id(guild_id: int | None, key: str, env_default: int = 0) -> int:
    """Liest die per `/feature-status` gesetzte Kanal-ID oder fällt sauber auf `.env` zurück."""
    gid = int(guild_id or GUILD_ID or 0)
    if gid <= 0:
        return int(env_default or 0)
    cfg = server_cfg_store.get_guild(gid)
    ch_map = cfg.get("channels") or {}
    if key in ch_map and ch_map[key] is not None:
        return int(ch_map[key])
    return int(env_default or 0)


def set_dynamic_channel_id(guild_id: int, key: str, channel_id: int | None) -> None:
    def _upd(g: dict[str, Any]) -> None:
        if channel_id is None:
            g["channels"].pop(key, None)
        else:
            g["channels"][key] = int(channel_id)

    server_cfg_store.mutate(guild_id, _upd)


def get_dynamic_role_id(guild_id: int | None, key: str, env_default: int = 0) -> int:
    gid = int(guild_id or GUILD_ID or 0)
    if gid <= 0:
        return int(env_default or 0)
    cfg = server_cfg_store.get_guild(gid)
    r_map = cfg.get("roles") or {}
    if key in r_map and r_map[key] is not None:
        return int(r_map[key])
    return int(env_default or 0)


def set_dynamic_role_id(guild_id: int, key: str, role_id: int | None) -> None:
    def _upd(g: dict[str, Any]) -> None:
        if role_id is None:
            g["roles"].pop(key, None)
        else:
            g["roles"][key] = int(role_id)

    server_cfg_store.mutate(guild_id, _upd)


# ═══════════════════════════════════════════════════════════════════════════
# 2. BIRTHDAYS (birthdays.json)
# ═══════════════════════════════════════════════════════════════════════════

def _default_birthdays_guild() -> dict[str, Any]:
    return {
        "config": {
            "enabled": True,
            "role_id": 0,
            "channel_id": 0,
            "coin_reward": 2500,
            "message": "🎂 Alles Gute zum Geburtstag {user}! Wir wünschen dir einen großartigen Tag! 🎉🥳",
        },
        "users": {},  # uid_str -> {"day": int, "month": int, "year": int|None, "celebrated_year": int}
        "active_role_holders": [],  # list[int] – aktuell vergebene Tages-Geburtstagsrollen
    }


birthdays_store = GuildJsonStore(BIRTHDAYS_FILE, _default_birthdays_guild)


# ═══════════════════════════════════════════════════════════════════════════
# 3. COUNTING (counting.json)
# ═══════════════════════════════════════════════════════════════════════════

def _default_counting_guild() -> dict[str, Any]:
    return {
        "enabled": False,
        "channel_id": 0,
        "math_enabled": False,
        "numbers_only": False,
        "current": 0,
        "last_user_id": 0,
        "last_message_id": 0,
        "highscore": 0,
        "highscore_by": 0,
        "highscore_at": "",
        "total_counts": 0,
        "users": {},  # uid_str -> {"correct": int, "fails": int, "highest": int}
    }


counting_store = GuildJsonStore(COUNTING_FILE, _default_counting_guild)


# ═══════════════════════════════════════════════════════════════════════════
# 4. LEVELING – EXAKTE MEE6-MATHEMATIK (levels.json)
# ═══════════════════════════════════════════════════════════════════════════

def mee6_xp_for_next_level(level: int) -> int:
    """Exakte MEE6-Formel: XP nötig für den Aufstieg von `level` auf `level + 1`."""
    lv = max(0, int(level))
    return 5 * (lv * lv) + 50 * lv + 100


def mee6_total_xp_for_level(level: int) -> int:
    """Exakte kumulierte Gesamt-XP, um `level` in MEE6 zu erreichen: sum_{i=0..L-1} (5i^2 + 50i + 100)."""
    lv = max(0, int(level))
    if lv == 0:
        return 0
    return (5 * lv * (lv - 1) * (2 * lv - 1)) // 6 + 25 * lv * (lv - 1) + 100 * lv


def mee6_level_from_xp(total_xp: int) -> tuple[int, int, int]:
    """Berechnet `(level, xp_in_aktuellem_level, benoetigte_xp_fuer_naechstes_level)` exakt nach MEE6."""
    xp = max(0, int(total_xp))
    lo, hi = 0, 1
    while mee6_total_xp_for_level(hi) <= xp:
        hi *= 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mee6_total_xp_for_level(mid) <= xp:
            lo = mid
        else:
            hi = mid - 1
    into_level = xp - mee6_total_xp_for_level(lo)
    needed = mee6_xp_for_next_level(lo)
    return lo, into_level, needed


def _default_levels_guild() -> dict[str, Any]:
    return {
        "config": {
            "enabled": True,
            "announce_mode": "current",  # "current" | "channel" | "dm" | "off"
            "announce_channel_id": 0,
            "levelup_message": "🎉 Glückwunsch {user}, du bist auf **Level {level}** aufgestiegen!",
            "xp_rate": 1.0,
            "xp_min": 15,
            "xp_max": 25,
            "cooldown_sec": 60,
            "voice_xp_enabled": True,
            "voice_xp_per_min": 10,
            "coin_per_level": 250,
            "stack_roles": True,
            "no_xp_channels": [],
            "no_xp_roles": [],
        },
        "role_rewards": {},  # str(level) -> int(role_id)
        "users": {},  # uid_str -> {"xp": int, "messages": int, "voice_minutes": int}
    }


levels_store = GuildJsonStore(LEVELS_FILE, _default_levels_guild)


# ═══════════════════════════════════════════════════════════════════════════
# 5. ROLES, REACTION ROLES, AUTOROLE & STICKY ROLES (roles_config.json)
# ═══════════════════════════════════════════════════════════════════════════

def _default_roles_guild() -> dict[str, Any]:
    return {
        "panels": {},  # panel_id -> {"title", "description", "color", "mode", "max_roles", "style", "channel_id", "message_id", "items": [{"role_id", "label", "emoji", "description", "button_style"}]}
        "reaction_roles": {},  # str(message_id) -> {emoji_str: {"role_id": int, "channel_id": int, "mode": "toggle"|"verify"}}
        "autoroles": {
            "enabled": False,
            "humans": [],
            "bots": [],
            "wait_for_screening": True,
        },
        "sticky_roles": {
            "enabled": False,
            "saved": {},  # uid_str -> [role_id, ...]
        },
    }


roles_store = GuildJsonStore(ROLES_FILE, _default_roles_guild)


# ═══════════════════════════════════════════════════════════════════════════
# 6. MODERATION, CASES, WARNS & TEMPBANS (moderation.json)
# ═══════════════════════════════════════════════════════════════════════════

def _default_moderation_guild() -> dict[str, Any]:
    return {
        "next_case_id": 1,
        "cases": {},
        "warnings": {},
        "warn_escalation": {},
        "automod": {
            "enabled": False, "anti_invite": True, "anti_links": False,
            "anti_spam": True, "anti_mass_mention": True, "anti_caps": False,
            "bad_words": [], "allowed_domains": ["twitch.tv", "youtube.com", "youtu.be", "spotify.com", "tenor.com", "giphy.com"],
            "exempt_channels": [], "exempt_roles": [], "punishment": "delete_warn", "timeout_sec": 300,
        },
        "tempbans": {},  # uid_str -> {"unban_ts": float, "reason": str, "mod_id": int, "case_id": int}
    }


moderation_store = GuildJsonStore(MODERATION_FILE, _default_moderation_guild)


# ═══════════════════════════════════════════════════════════════════════════
# 7. COMMUNITY: GIVEAWAYS, POLLS, SUGGESTIONS, STARBOARD, TAGS, AFK (community.json)
# ═══════════════════════════════════════════════════════════════════════════

def _default_community_guild() -> dict[str, Any]:
    return {
        "giveaways": {},  # gid_key -> {"id", "channel_id", "message_id", "prize", "winners", "end_ts", "host_id", "required_role_id", "min_level", "entrants": [], "ended": bool}
        "polls": {},  # poll_id -> {"id", "channel_id", "message_id", "question", "options": [str], "votes": {opt_idx_str: [uid]}, "multi": bool, "end_ts": float|0, "ended": bool, "author_id": int}
        "suggestions": {
            "enabled": False,
            "channel_id": 0,
            "next_id": 1,
            "items": {},  # str(id) -> {"id", "author_id", "text", "message_id", "channel_id", "up": [uid], "down": [uid], "status": "open"|"approved"|"denied", "note": str}
        },
        "starboard": {
            "enabled": False,
            "channel_id": 0,
            "threshold": 3,
            "emoji": "⭐",
            "allow_self_star": False,
            "posts": {},  # str(orig_message_id) -> int(starboard_message_id)
        },
        "custom_commands": {},  # trigger -> response configuration
        "afk": {},  # uid_str -> {"reason": str, "since_ts": float}
    }


community_store = GuildJsonStore(COMMUNITY_FILE, _default_community_guild)
