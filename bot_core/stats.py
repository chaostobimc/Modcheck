"""Persistenter Statistik-Sammler für Discord-Nachrichten, Voice-Zeit & Casino-Bilanzen."""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
from typing import Any

from bot_core.config import DE_TZ, STATS_FILE, today_berlin
from bot_core.storage import _atomic_write_json


class StatsTracker:
    """Speichert Discord-Nachrichten, Voice-Stunden & Gambling-Profite für /profile & /status."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._voice_sessions: dict[int, float] = {}
        self._last_save: float = 0.0
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        default = {
            "messages": {},
            "voice_time": {},
            "voice_days": {},
            "daily_active": {},
            "mod_actions": [],
            "gambling": {},
            "joins": [],
            "deletions": [],
        }
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k, v in default.items():
                    d.setdefault(k, v)
                return d
            except Exception as e:
                print(f"⚠️ Fehler beim Laden von {STATS_FILE}: {e}")
        return default

    def save(self, force: bool = True) -> None:
        now = time.time()
        if not force and (now - self._last_save) < 15.0:
            return
        with self._lock:
            try:
                _atomic_write_json(STATS_FILE, self._data)
                self._last_save = now
            except Exception as e:
                print(f"⚠️ Fehler beim Speichern von {STATS_FILE}: {e}")

    def flush(self) -> None:
        with self._lock:
            now = time.time()
            for user_id, start in list(self._voice_sessions.items()):
                duration = int(now - start)
                if duration >= 5:
                    uid = str(user_id)
                    cursor = start
                    while cursor < now:
                        cursor_dt = datetime.datetime.fromtimestamp(cursor, DE_TZ)
                        nxt = datetime.datetime.combine(
                            cursor_dt.date() + datetime.timedelta(days=1),
                            datetime.time.min,
                            tzinfo=DE_TZ,
                        ).timestamp()
                        chunk_end = min(now, nxt)
                        secs = int(chunk_end - cursor)
                        if secs > 0:
                            day = str(cursor_dt.date())
                            self._data["voice_time"][uid] = int(self._data["voice_time"].get(uid, 0)) + secs
                            vd = self._data["voice_days"].setdefault(uid, {})
                            vd[day] = int(vd.get(day, 0)) + secs
                        cursor = chunk_end
                    self._voice_sessions[user_id] = now
        self.save(force=True)

    def add_message(self, user_id: int, username: str, channel_id: int | None = None) -> None:
        today = str(today_berlin())
        uid = str(user_id)
        with self._lock:
            u = self._data["messages"].setdefault(
                uid, {"total": 0, "username": username, "daily": {}, "channels": {}}
            )
            u["username"] = username
            u["total"] = int(u.get("total", 0)) + 1
            daily = u.setdefault("daily", {})
            daily[today] = int(daily.get(today, 0)) + 1
            if channel_id is not None:
                channels = u.setdefault("channels", {})
                cid = str(channel_id)
                channels[cid] = int(channels.get(cid, 0)) + 1
            da = self._data["daily_active"].setdefault(today, {})
            da[uid] = int(da.get(uid, 0)) + 1
        self.save(force=False)

    def get_user_discord_stats(self, user_id: int) -> dict[str, int]:
        uid = str(user_id)
        with self._lock:
            m = self._data["messages"].get(uid, {})
            v_secs = int(self._data["voice_time"].get(uid, 0))
            g = self._data["gambling"].get(uid, {})
            return {
                "messages": int(m.get("total", 0)),
                "voice_seconds": v_secs,
                "gambling_wins": int(g.get("wins", 0)),
                "gambling_losses": int(g.get("losses", 0)),
                "gambling_profit": int(g.get("profit", g.get("net", 0))),
            }

    def voice_join(self, user_id: int) -> None:
        with self._lock:
            if user_id not in self._voice_sessions:
                self._voice_sessions[user_id] = time.time()

    def voice_leave(self, user_id: int, username: str) -> int:
        with self._lock:
            start = self._voice_sessions.pop(user_id, None)
            if start is None:
                return 0
            end = time.time()
            duration = int(end - start)
            if duration < 5:
                return 0
            uid = str(user_id)
            cursor = start
            while cursor < end:
                cursor_dt = datetime.datetime.fromtimestamp(cursor, DE_TZ)
                nxt = datetime.datetime.combine(
                    cursor_dt.date() + datetime.timedelta(days=1),
                    datetime.time.min,
                    tzinfo=DE_TZ,
                ).timestamp()
                chunk_end = min(end, nxt)
                secs = int(chunk_end - cursor)
                if secs > 0:
                    day = str(cursor_dt.date())
                    self._data["voice_time"][uid] = int(self._data["voice_time"].get(uid, 0)) + secs
                    vd = self._data["voice_days"].setdefault(uid, {})
                    vd[day] = int(vd.get(day, 0)) + secs
                cursor = chunk_end
            m = self._data["messages"].setdefault(uid, {"total": 0, "username": username, "daily": {}})
            m["username"] = username
            self.save(force=True)
            return duration

    def add_mod_action(
        self, action: str, target: str, moderator: str, reason: str = "", duration: int | None = None
    ) -> None:
        entry = {
            "action": action,
            "target": target,
            "moderator": moderator,
            "reason": reason or "",
            "duration": duration,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self._lock:
            self._data["mod_actions"].append(entry)
            if len(self._data["mod_actions"]) > 500:
                self._data["mod_actions"] = self._data["mod_actions"][-500:]
            self.save(force=True)

    def add_gambling_result(self, user_id: int, username: str, won: bool, amount: int) -> None:
        if amount <= 0:
            return
        uid = str(user_id)
        with self._lock:
            g = self._data["gambling"].setdefault(
                uid, {"username": username, "wins": 0, "losses": 0, "profit": 0}
            )
            g["username"] = username
            if "profit" not in g and "net" in g:
                g["profit"] = g.pop("net")
            if won:
                g["wins"] = int(g.get("wins", 0)) + 1
                g["profit"] = int(g.get("profit", 0)) + int(amount)
            else:
                g["losses"] = int(g.get("losses", 0)) + 1
                g["profit"] = int(g.get("profit", 0)) - int(amount)
            self.save(force=True)

    def record_gambling_net(self, user_id: int, username: str, net_delta: int) -> None:
        if net_delta > 0:
            self.add_gambling_result(user_id, username, True, net_delta)
        elif net_delta < 0:
            self.add_gambling_result(user_id, username, False, abs(net_delta))


class BotRuntime:
    """Laufzeit-Zustand (Uptime & aktive Casino-Runden)."""

    def __init__(self) -> None:
        self.active_games: dict[int, str] = {}
        self.bot_start_time: float = time.time()

    def uptime_seconds(self) -> int:
        return int(time.time() - self.bot_start_time)


stats_tracker = StatsTracker()
bot_runtime = BotRuntime()
