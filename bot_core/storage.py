"""Thread-sichere, atomare JSON-Persistenz für alle Bot-Datenbanken."""
from __future__ import annotations

import copy
import datetime
import json
import os
import random
import tempfile
import threading
import time
from typing import Any

from bot_core.config import (
    ABSENCE_FILE,
    ALERTS_FILE,
    BANK_FILE,
    DATA_FILE,
    INACTIVITY_WARN_CHANNEL_ID,
    INACTIVITY_WARN_STREAMS,
    LIVE_ANNOUNCE_CHANNEL_ID,
    LIVE_PING_EVERYONE,
    LIVE_PING_ROLE_ID,
    PREFS_FILE,
    START_BALANCE,
    TICKET_DATA_FILE,
    VAULT_FILE,
    YOUTUBE_ANNOUNCE_CHANNEL_ID,
    YOUTUBE_CHANNEL_ID,
    YOUTUBE_PING_EVERYONE,
    YOUTUBE_PING_ROLE_ID,
    today_berlin,
)


def _atomic_write_json(path: str, payload: Any) -> None:
    """Schreibt JSON atomar via Temp-Datei + fsync + os.replace."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return copy.deepcopy(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ Warnung beim Lesen von {path}: {e}")
        return copy.deepcopy(default)


# ═══════════════════════════════════════════════════════════════════════════
# 1. ACTIVITY & TWITCH TRACKING (data.json)
# ═══════════════════════════════════════════════════════════════════════════
_data_lock = threading.RLock()
_data_cache: dict[str, Any] | None = None
_data_dirty: bool = False
_data_last_flush: float = 0.0


def _ensure_data_schema(d: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(d, dict):
        d = {}
    d.setdefault("users", {})
    d.setdefault("streams", {})
    d.setdefault("message_counts", {})
    d.setdefault("stream_meta", {})
    return d


def load_data() -> dict[str, Any]:
    global _data_cache
    with _data_lock:
        if _data_cache is None:
            _data_cache = _ensure_data_schema(_read_json(DATA_FILE, {}))
        return copy.deepcopy(_data_cache)


def save_data(data: dict[str, Any], force: bool = True) -> None:
    global _data_cache, _data_dirty, _data_last_flush
    now = time.monotonic()
    with _data_lock:
        cleaned = _ensure_data_schema(copy.deepcopy(data))
        _data_cache = cleaned
        if force or (now - _data_last_flush) >= 20.0:
            _atomic_write_json(DATA_FILE, cleaned)
            _data_dirty = False
            _data_last_flush = now
        else:
            _data_dirty = True


def flush_data() -> None:
    """Schreibt ausstehende RAM-Änderungen garantiert auf die Festplatte."""
    global _data_dirty, _data_last_flush
    with _data_lock:
        if _data_dirty and _data_cache is not None:
            _atomic_write_json(DATA_FILE, _data_cache)
            _data_dirty = False
            _data_last_flush = time.monotonic()


def reload_data_from_disk() -> dict[str, Any]:
    """Für externe Prozesse/Dashboard: lädt frisch von der Festplatte."""
    global _data_cache
    with _data_lock:
        _data_cache = _ensure_data_schema(_read_json(DATA_FILE, {}))
        return copy.deepcopy(_data_cache)


# ═══════════════════════════════════════════════════════════════════════════
# 2. ECONOMY, FISHING & JOBS (bank.json)
# ═══════════════════════════════════════════════════════════════════════════
_bank_lock = threading.RLock()
_bank_cache: dict[str, Any] | None = None


def _get_bank_unlocked() -> dict[str, Any]:
    global _bank_cache
    if _bank_cache is None:
        raw = _read_json(BANK_FILE, {})
        _bank_cache = raw if isinstance(raw, dict) else {}
    return _bank_cache


def load_bank() -> dict[str, Any]:
    with _bank_lock:
        return copy.deepcopy(_get_bank_unlocked())


def save_bank(data: dict[str, Any]) -> None:
    global _bank_cache
    with _bank_lock:
        _bank_cache = copy.deepcopy(data)
        _atomic_write_json(BANK_FILE, _bank_cache)


def get_balance(user_id: int) -> int:
    with _bank_lock:
        bank = _get_bank_unlocked()
        uid = str(user_id)
        if uid not in bank:
            bank[uid] = START_BALANCE
            _atomic_write_json(BANK_FILE, bank)
        return int(bank.get(uid, START_BALANCE))


def update_balance(user_id: int, amount: int) -> int:
    with _bank_lock:
        bank = _get_bank_unlocked()
        uid = str(user_id)
        current = int(bank.get(uid, START_BALANCE))
        new_bal = max(0, current + int(amount))
        bank[uid] = new_bal
        _atomic_write_json(BANK_FILE, bank)
        return new_bal


def set_balance(user_id: int, amount: int) -> int:
    with _bank_lock:
        bank = _get_bank_unlocked()
        uid = str(user_id)
        bank[uid] = max(0, int(amount))
        _atomic_write_json(BANK_FILE, bank)
        return bank[uid]


def get_daily_last(user_id: int) -> str | None:
    with _bank_lock:
        bank = _get_bank_unlocked()
        return bank.get(f"{user_id}_daily")


def set_daily_last(user_id: int, date_str: str) -> None:
    with _bank_lock:
        bank = _get_bank_unlocked()
        bank[f"{user_id}_daily"] = date_str
        _atomic_write_json(BANK_FILE, bank)


def get_daily_streak(user_id: int) -> int:
    with _bank_lock:
        bank = _get_bank_unlocked()
        return int(bank.get(f"{user_id}_daily_streak", 0) or 0)


def set_daily_streak(user_id: int, streak: int) -> None:
    with _bank_lock:
        bank = _get_bank_unlocked()
        bank[f"{user_id}_daily_streak"] = max(0, int(streak))
        _atomic_write_json(BANK_FILE, bank)


def get_fishing_data(user_id: int) -> dict[str, Any]:
    with _bank_lock:
        bank = _get_bank_unlocked()
        key = f"{user_id}_fishing"
        if key not in bank or not isinstance(bank[key], dict):
            bank[key] = {
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
            _atomic_write_json(BANK_FILE, bank)
        return copy.deepcopy(bank[key])


def save_fishing_data(user_id: int, fishing_data: dict[str, Any]) -> None:
    with _bank_lock:
        bank = _get_bank_unlocked()
        bank[f"{user_id}_fishing"] = copy.deepcopy(fishing_data)
        _atomic_write_json(BANK_FILE, bank)


def get_job_data(user_id: int, job_name: str) -> dict[str, Any]:
    default_tools = {
        "woodcutting": "stone",
        "mining": "wood",
        "hunting": "shortbow",
        "smithing": "stone",
        "cooking": "basic",
    }
    with _bank_lock:
        bank = _get_bank_unlocked()
        key = f"{user_id}_job_{job_name}"
        if key not in bank or not isinstance(bank[key], dict):
            bank[key] = {
                "tool": default_tools.get(job_name, "basic"),
                "cooldown_level": 0,
                "total_earned": 0,
                "total_actions": 0,
                "best_value": 0,
                "best_name": "Noch nichts",
                "legendary_count": 0,
                "last_action_time": 0,
            }
            _atomic_write_json(BANK_FILE, bank)
        return copy.deepcopy(bank[key])


def save_job_data(user_id: int, job_name: str, job_data: dict[str, Any]) -> None:
    with _bank_lock:
        bank = _get_bank_unlocked()
        bank[f"{user_id}_job_{job_name}"] = copy.deepcopy(job_data)
        _atomic_write_json(BANK_FILE, bank)


# ═══════════════════════════════════════════════════════════════════════════
# 3. THE VAULT SAFE-KNACKER (vault.json)
# ═══════════════════════════════════════════════════════════════════════════
_vault_lock = threading.RLock()


def generate_vault_code() -> str:
    return f"{random.randint(0, 999):03d}"


def get_vault_data() -> dict[str, Any]:
    with _vault_lock:
        data = _read_json(VAULT_FILE, {})
        if not isinstance(data, dict) or "code" not in data:
            data = {
                "jackpot": 1000,
                "code": generate_vault_code(),
                "attempts": 0,
            }
            _atomic_write_json(VAULT_FILE, data)
        return data


def save_vault(data: dict[str, Any]) -> None:
    with _vault_lock:
        _atomic_write_json(VAULT_FILE, data)


load_vault = get_vault_data


# ═══════════════════════════════════════════════════════════════════════════
# 4. TICKET SYSTEM (tickets.json)
# ═══════════════════════════════════════════════════════════════════════════
_ticket_lock = threading.RLock()
open_tickets: dict[int, dict[str, Any]] = {}
ticket_counter: int = 0


def load_tickets() -> dict[int, dict[str, Any]]:
    global ticket_counter
    with _ticket_lock:
        raw = _read_json(TICKET_DATA_FILE, {"counter": 0, "tickets": {}})
        ticket_counter = int(raw.get("counter", 0) or 0)
        loaded: dict[int, dict[str, Any]] = {}
        for k, v in (raw.get("tickets") or {}).items():
            try:
                loaded[int(k)] = v
            except (ValueError, TypeError):
                continue
        open_tickets.clear()
        open_tickets.update(loaded)
        return open_tickets


def save_tickets() -> None:
    with _ticket_lock:
        payload = {
            "counter": ticket_counter,
            "tickets": {str(k): v for k, v in open_tickets.items()},
        }
        _atomic_write_json(TICKET_DATA_FILE, payload)


def get_next_ticket_number() -> int:
    global ticket_counter
    with _ticket_lock:
        ticket_counter += 1
        save_tickets()
        return ticket_counter


# ═══════════════════════════════════════════════════════════════════════════
# 5. ABWESENHEITS-SYSTEM (absences.json)
# ═══════════════════════════════════════════════════════════════════════════
_absence_lock = threading.RLock()


def load_absences() -> dict[str, Any]:
    with _absence_lock:
        raw = _read_json(ABSENCE_FILE, {"absences": {}})
        if not isinstance(raw, dict):
            raw = {"absences": {}}
        raw.setdefault("absences", {})
        return raw


def save_absences(data: dict[str, Any]) -> None:
    with _absence_lock:
        _atomic_write_json(ABSENCE_FILE, data)


def is_user_absent(uid: str, date_str: str) -> bool:
    with _absence_lock:
        data = load_absences()
        return date_str in data.get("absences", {}).get(str(uid), [])


def add_absence(uid: str, date_str: str, reason: str = "") -> None:
    with _absence_lock:
        data = load_absences()
        uid = str(uid)
        user_list = data["absences"].setdefault(uid, [])
        if date_str not in user_list:
            user_list.append(date_str)
            user_list.sort()
        reasons_key = f"{uid}_reasons"
        if not isinstance(data.get(reasons_key), dict):
            data[reasons_key] = {}
        data[reasons_key][date_str] = reason or "Kein Grund angegeben"
        save_absences(data)


def add_absence_range(
    uid: str,
    start_date: datetime.date,
    end_date: datetime.date,
    reason: str = "",
) -> list[str]:
    """Trägt einen mehrtägigen Abwesenheits-Zeitraum in einem Rutsch ein."""
    added: list[str] = []
    with _absence_lock:
        data = load_absences()
        uid = str(uid)
        user_list = data["absences"].setdefault(uid, [])
        reasons_key = f"{uid}_reasons"
        if not isinstance(data.get(reasons_key), dict):
            data[reasons_key] = {}
        cur = start_date
        while cur <= end_date:
            ds = str(cur)
            if ds not in user_list:
                user_list.append(ds)
                added.append(ds)
            data[reasons_key][ds] = reason or "Kein Grund angegeben"
            cur += datetime.timedelta(days=1)
        user_list.sort()
        save_absences(data)
    return added


def remove_absence(uid: str, date_str: str) -> bool:
    with _absence_lock:
        data = load_absences()
        uid = str(uid)
        user_list = data.get("absences", {}).get(uid, [])
        if date_str in user_list:
            user_list.remove(date_str)
            reasons_key = f"{uid}_reasons"
            if isinstance(data.get(reasons_key), dict):
                data[reasons_key].pop(date_str, None)
            save_absences(data)
            return True
        return False


def get_user_absences(uid: str) -> list[dict[str, str]]:
    with _absence_lock:
        data = load_absences()
        uid = str(uid)
        dates = sorted(data.get("absences", {}).get(uid, []))
        reasons = data.get(f"{uid}_reasons", {})
        if not isinstance(reasons, dict):
            reasons = {}
        return [
            {"date": d, "reason": reasons.get(d, "Kein Grund angegeben")}
            for d in dates
        ]


# ═══════════════════════════════════════════════════════════════════════════
# 6. USER- & SERVER-PRÄFERENZEN (user_prefs.json: Favoriten, Volume, Temp-VC)
# ═══════════════════════════════════════════════════════════════════════════
_prefs_lock = threading.RLock()
_prefs_cache: dict[str, Any] | None = None


def _get_prefs_unlocked() -> dict[str, Any]:
    global _prefs_cache
    if _prefs_cache is None:
        raw = _read_json(PREFS_FILE, {})
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("favorites", {})
        raw.setdefault("guild_volumes", {})
        raw.setdefault("temp_voice", {})
        _prefs_cache = raw
    return _prefs_cache


def _save_prefs_unlocked() -> None:
    if _prefs_cache is not None:
        _atomic_write_json(PREFS_FILE, _prefs_cache)


# ── Musik-Favoriten ────────────────────────────────────────────────────────

def get_user_favorites(user_id: int) -> list[dict[str, Any]]:
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        favs = prefs["favorites"].get(str(user_id), [])
        return copy.deepcopy(favs) if isinstance(favs, list) else []


def is_user_favorite(user_id: int, url: str) -> bool:
    norm = (url or "").strip()
    if not norm:
        return False
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        favs = prefs["favorites"].get(str(user_id), [])
        return any(isinstance(f, dict) and f.get("url") == norm for f in favs)


def toggle_user_favorite(
    user_id: int,
    item: dict[str, Any],
    max_items: int = 25,
) -> tuple[str, int]:
    """Fügt einen Favoriten hinzu oder entfernt ihn falls vorhanden.
    Rückgabe: (status: 'added' | 'removed' | 'full', anzahl_gesamt).
    """
    url = str(item.get("url") or "").strip()
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        uid = str(user_id)
        favs: list[dict[str, Any]] = prefs["favorites"].setdefault(uid, [])
        for idx, existing in enumerate(favs):
            if isinstance(existing, dict) and existing.get("url") == url and url:
                favs.pop(idx)
                _save_prefs_unlocked()
                return "removed", len(favs)
        if len(favs) >= max_items:
            return "full", len(favs)
        entry = {
            "title": str(item.get("title") or "Unbekannt")[:100],
            "url": url,
            "duration": int(item.get("duration") or 0),
            "thumbnail": str(item.get("thumbnail") or ""),
            "is_radio": bool(item.get("is_radio", False)),
            "added_at": str(today_berlin()),
        }
        favs.append(entry)
        _save_prefs_unlocked()
        return "added", len(favs)


def remove_user_favorite(user_id: int, index: int) -> dict[str, Any] | None:
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        favs = prefs["favorites"].get(str(user_id), [])
        if isinstance(favs, list) and 0 <= index < len(favs):
            removed = favs.pop(index)
            _save_prefs_unlocked()
            return copy.deepcopy(removed)
        return None


# ── Musik-Lautstärke (pro Server) ──────────────────────────────────────────

def get_guild_volume(guild_id: int) -> int:
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        val = int(prefs["guild_volumes"].get(str(guild_id), 100) or 100)
        return max(5, min(200, val))


def set_guild_volume(guild_id: int, percent: int) -> int:
    clamped = max(5, min(200, int(percent)))
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        prefs["guild_volumes"][str(guild_id)] = clamped
        _save_prefs_unlocked()
    return clamped


# ── Temp-Voice Gedächtnis (pro User) ───────────────────────────────────────

def get_temp_voice_prefs(user_id: int) -> dict[str, Any]:
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        entry = prefs["temp_voice"].get(str(user_id), {})
        return copy.deepcopy(entry) if isinstance(entry, dict) else {}


def update_temp_voice_prefs(user_id: int, **kwargs: Any) -> dict[str, Any]:
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        uid = str(user_id)
        entry = prefs["temp_voice"].setdefault(uid, {})
        if not isinstance(entry, dict):
            entry = {}
            prefs["temp_voice"][uid] = entry
        for k, v in kwargs.items():
            if v is not None:
                entry[k] = v
        _save_prefs_unlocked()
        return copy.deepcopy(entry)


def clear_temp_voice_prefs(user_id: int) -> None:
    with _prefs_lock:
        prefs = _get_prefs_unlocked()
        if prefs["temp_voice"].pop(str(user_id), None) is not None:
            _save_prefs_unlocked()


# ═══════════════════════════════════════════════════════════════════════════
# 7. ALERTS-KONFIGURATION & ZUSTAND (alerts_state.json)
# ═══════════════════════════════════════════════════════════════════════════
_alerts_lock = threading.RLock()
_alerts_cache: dict[str, Any] | None = None


def _ensure_alerts_schema(d: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(d, dict):
        d = {}
    cfg = d.setdefault("config", {})
    cfg.setdefault("live_channel_id", LIVE_ANNOUNCE_CHANNEL_ID)
    cfg.setdefault("live_ping_role_id", LIVE_PING_ROLE_ID)
    cfg.setdefault("live_ping_everyone", LIVE_PING_EVERYONE)

    cfg.setdefault("yt_channel_id_discord", YOUTUBE_ANNOUNCE_CHANNEL_ID)
    cfg.setdefault("yt_source", YOUTUBE_CHANNEL_ID)
    cfg.setdefault("yt_resolved_uc_id", "")
    cfg.setdefault("yt_channel_title", "")
    cfg.setdefault("yt_ping_role_id", YOUTUBE_PING_ROLE_ID)
    cfg.setdefault("yt_ping_everyone", YOUTUBE_PING_EVERYONE)

    cfg.setdefault("inactivity_channel_id", INACTIVITY_WARN_CHANNEL_ID)
    cfg.setdefault("inactivity_threshold", max(1, INACTIVITY_WARN_STREAMS))
    cfg.setdefault("inactivity_enabled", True)

    tw = d.setdefault("twitch_state", {})
    tw.setdefault("is_live", False)
    tw.setdefault("last_stream_id", "")
    tw.setdefault("last_announce_ts", 0.0)
    tw.setdefault("last_offline_ts", 0.0)
    tw.setdefault("active_message_id", 0)
    tw.setdefault("active_channel_id", 0)
    tw.setdefault("peak_viewers", 0)
    tw.setdefault("games", [])
    tw.setdefault("started_at", "")
    tw.setdefault("title", "")

    yt = d.setdefault("youtube_state", {})
    yt.setdefault("initialized_for_uc", "")
    yt.setdefault("seen_video_ids", [])

    ina = d.setdefault("inactivity_state", {})
    ina.setdefault("warned_streaks", {})
    return d


def load_alerts_state() -> dict[str, Any]:
    global _alerts_cache
    with _alerts_lock:
        if _alerts_cache is None:
            _alerts_cache = _ensure_alerts_schema(_read_json(ALERTS_FILE, {}))
        return copy.deepcopy(_alerts_cache)


def save_alerts_state(data: dict[str, Any]) -> None:
    global _alerts_cache
    with _alerts_lock:
        _alerts_cache = _ensure_alerts_schema(copy.deepcopy(data))
        _atomic_write_json(ALERTS_FILE, _alerts_cache)

