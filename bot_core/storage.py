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
    BANK_FILE,
    DATA_FILE,
    START_BALANCE,
    TICKET_DATA_FILE,
    VAULT_FILE,
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
