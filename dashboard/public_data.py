"""Öffentliche Hub-Daten aus data.json + dashboard_stats.json."""
from __future__ import annotations

import calendar
import datetime
import json
import os
from typing import Any

try:
    from zoneinfo import ZoneInfo
    DE_TZ = ZoneInfo("Europe/Berlin")
except Exception:
    DE_TZ = datetime.timezone(datetime.timedelta(hours=1))

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

MONTH_NAMES = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
WEEKDAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def today_local() -> datetime.date:
    return datetime.datetime.now(DE_TZ).date()


def today_iso() -> str:
    return str(today_local())


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        d = datetime.date.fromisoformat(iso[:10])
        return d.strftime("%d.%m.%Y")
    except ValueError:
        return iso


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_activity_data() -> dict:
    data = _load_json(os.path.join(_ROOT, os.getenv("DATA_FILE", "data.json")), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("users", {})
    data.setdefault("streams", {})
    data.setdefault("message_counts", {})
    return data


def load_stats() -> dict:
    stats = _load_json(os.path.join(_ROOT, "dashboard_stats.json"), {})
    if not isinstance(stats, dict):
        stats = {}
    for key, default in (
        ("messages", {}), ("voice_time", {}), ("voice_days", {}),
        ("daily_active", {}), ("gambling", {}), ("mod_actions", []),
        ("deletions", []), ("joins", []),
    ):
        stats.setdefault(key, default)
    return stats


def load_absences() -> dict:
    data = _load_json(os.path.join(_ROOT, "absences.json"), {"absences": {}})
    if not isinstance(data, dict):
        data = {"absences": {}}
    data.setdefault("absences", {})
    return data


def is_real_stream_day(streams: dict, date_str: str) -> bool:
    lst = streams.get(date_str)
    return isinstance(lst, list) and len(lst) > 0


def is_user_absent(uid: str, date_str: str, absences: dict | None = None) -> bool:
    absences = absences if absences is not None else load_absences()
    dates = absences.get("absences", {}).get(str(uid), [])
    return date_str in dates


def is_present(streams: dict, uid: str, date_str: str, absences: dict | None = None) -> bool:
    lst = streams.get(date_str) or []
    uid_s = str(uid)
    if uid_s in lst or uid in lst:
        return True
    return is_user_absent(uid_s, date_str, absences)


def activity_grade(percentage: float) -> str:
    if percentage >= 90:
        return "S+"
    if percentage >= 75:
        return "A"
    if percentage >= 60:
        return "B"
    if percentage >= 40:
        return "C"
    if percentage >= 20:
        return "D"
    return "F"


def current_streak(streams: dict, uid: str, absences: dict | None = None) -> int:
    uid_s = str(uid)
    today = today_iso()
    dates = sorted((d for d, lst in streams.items() if lst), reverse=True)
    streak = 0
    for date_str in dates:
        present = is_present(streams, uid_s, date_str, absences)
        if date_str == today and not present:
            continue
        if present:
            streak += 1
        else:
            break
    return streak


def longest_streak(streams: dict, uid: str, absences: dict | None = None) -> int:
    uid_s = str(uid)
    dates = sorted(d for d, lst in streams.items() if lst)
    longest = current = 0
    for date_str in dates:
        if is_present(streams, uid_s, date_str, absences):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def fmt_voice(seconds: int) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, _s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _month_heatmap(streams: dict, uid: str, year: int, month: int, absences: dict) -> list[dict]:
    today = today_local()
    days_in_month = calendar.monthrange(year, month)[1]
    first_wd = datetime.date(year, month, 1).weekday()
    cells: list[dict] = [{"empty": True} for _ in range(first_wd)]
    for day in range(1, days_in_month + 1):
        d = datetime.date(year, month, day)
        date_str = str(d)
        future = d > today
        real = is_real_stream_day(streams, date_str)
        present = is_present(streams, uid, date_str, absences) if real else False
        absent_excused = is_user_absent(uid, date_str, absences)
        if future:
            state = "future"
        elif present and absent_excused:
            state = "excused"
        elif present:
            state = "present"
        elif real:
            state = "absent"
        else:
            state = "off"
        cells.append({
            "day": day,
            "date": date_str,
            "state": state,
            "today": date_str == str(today),
        })
    return cells


def build_public_payload(guild: dict | None = None, now_playing: dict | None = None) -> dict:
    data = load_activity_data()
    stats = load_stats()
    absences = load_absences()
    streams = data.get("streams", {})
    users = data.get("users", {})
    msg_counts = data.get("message_counts", {})
    today = today_local()
    today_s = str(today)
    year, month = today.year, today.month
    last_month = month - 1 or 12
    last_month_year = year if month > 1 else year - 1

    name_of = {
        uid: (info.get("display_name") or info.get("twitch_name") or uid)
        for uid, info in users.items()
    }

    real_days = [d for d, lst in streams.items() if lst]
    real_days_sorted = sorted(real_days)
    total_stream_days = len(real_days)
    this_month_days = [d for d in real_days if d.startswith(f"{year}-{month:02d}-")]
    last_month_days = [d for d in real_days if d.startswith(f"{last_month_year}-{last_month:02d}-")]

    live_guess = bool(streams.get(today_s))

    hour_totals = [0] * 24
    weekday_counts = [0] * 7
    for d in real_days:
        try:
            weekday_counts[datetime.date.fromisoformat(d).weekday()] += 1
        except ValueError:
            pass

    roster = []
    for uid, info in users.items():
        present_days = sum(1 for d in real_days if is_present(streams, uid, d, absences))
        pct = (present_days / total_stream_days * 100) if total_stream_days else 0
        last_seen = next((d for d in reversed(real_days_sorted) if is_present(streams, uid, d, absences)), None)
        first_seen = next((d for d in real_days_sorted if is_present(streams, uid, d, absences)), None)

        twitch_msgs = 0
        hours = [0] * 24
        for day, day_data in msg_counts.items():
            if uid in day_data:
                twitch_msgs += int(day_data[uid].get("count", 0) or 0)
                for h, c in (day_data[uid].get("hours") or {}).items():
                    try:
                        hi = int(h)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= hi <= 23:
                        hours[hi] += int(c)
                        hour_totals[hi] += int(c)

        month_present = sum(1 for d in this_month_days if is_present(streams, uid, d, absences))
        month_excused = sum(1 for d in this_month_days if is_user_absent(uid, d, absences))
        discord_msgs = int((stats.get("messages") or {}).get(uid, {}).get("total", 0) or 0)
        voice_secs = int((stats.get("voice_time") or {}).get(uid, 0) or 0)
        peak_hour = hours.index(max(hours)) if any(hours) else None
        avg_twitch = round(twitch_msgs / present_days, 1) if present_days else 0

        roster.append({
            "uid": uid,
            "display_name": name_of[uid],
            "twitch_name": info.get("twitch_name", ""),
            "present": present_days,
            "absent": max(0, total_stream_days - present_days),
            "pct": round(pct, 1),
            "grade": activity_grade(pct),
            "streak": current_streak(streams, uid, absences),
            "longest_streak": longest_streak(streams, uid, absences),
            "last_seen": last_seen,
            "last_seen_fmt": fmt_date(last_seen),
            "first_seen": first_seen,
            "first_seen_fmt": fmt_date(first_seen),
            "twitch_messages": twitch_msgs,
            "avg_twitch": avg_twitch,
            "discord_messages": discord_msgs,
            "voice_seconds": voice_secs,
            "voice_label": fmt_voice(voice_secs),
            "peak_hour": peak_hour,
            "hours": hours,
            "month_present": month_present,
            "month_total": len(this_month_days),
            "month_excused": month_excused,
            "today": is_present(streams, uid, today_s, absences) if today_s in streams else False,
            "heatmap": _month_heatmap(streams, uid, year, month, absences),
        })

    roster.sort(key=lambda x: (x["present"], x["twitch_messages"], x["pct"]), reverse=True)
    for i, row in enumerate(roster, 1):
        row["rank"] = i

    weeks = []
    start = today - datetime.timedelta(days=today.weekday() + 15 * 7)
    for w in range(16):
        week = []
        for i in range(7):
            d = start + datetime.timedelta(days=w * 7 + i)
            ds = str(d)
            count = len(streams.get(ds) or [])
            week.append({
                "date": ds,
                "label": d.strftime("%d.%m"),
                "count": count,
                "stream": bool(streams.get(ds)),
                "future": d > today,
                "today": ds == today_s,
            })
        weeks.append(week)

    monthly = []
    for m in range(1, 13):
        days_m = [d for d in real_days if d.startswith(f"{year}-{m:02d}-")]
        monthly.append({
            "month": m,
            "name": MONTH_NAMES[m][:3],
            "full": MONTH_NAMES[m],
            "streams": len(days_m),
            "msgs": sum(
                int(u.get("count", 0) or 0)
                for day, users_d in msg_counts.items() if day.startswith(f"{year}-{m:02d}-")
                for u in users_d.values()
            ),
        })

    daily_msgs = []
    da = stats.get("daily_active") or {}
    for i in range(29, -1, -1):
        d = str(today - datetime.timedelta(days=i))
        daily_msgs.append({"date": d[5:], "full": fmt_date(d), "count": sum((da.get(d) or {}).values())})

    daily_voice = []
    vd = stats.get("voice_days") or {}
    for i in range(29, -1, -1):
        d = str(today - datetime.timedelta(days=i))
        secs = sum(ud.get(d, 0) for ud in vd.values())
        daily_voice.append({"date": d[5:], "full": fmt_date(d), "seconds": secs, "label": fmt_voice(secs)})

    msg_lb = sorted(
        [
            {"uid": k, "username": v.get("username", k), "total": v.get("total", 0)}
            for k, v in (stats.get("messages") or {}).items()
        ],
        key=lambda x: x["total"],
        reverse=True,
    )[:15]

    voice_lb = sorted(
        [
            {
                "uid": uid,
                "username": (stats.get("messages") or {}).get(uid, {}).get("username", name_of.get(uid, uid)),
                "seconds": secs,
                "label": fmt_voice(secs),
            }
            for uid, secs in (stats.get("voice_time") or {}).items()
        ],
        key=lambda x: x["seconds"],
        reverse=True,
    )[:15]

    recent_streams = []
    for ds in reversed(real_days_sorted[-20:]):
        ids = streams.get(ds) or []
        try:
            wd = WEEKDAY_SHORT[datetime.date.fromisoformat(ds).weekday()]
        except ValueError:
            wd = ""
        recent_streams.append({
            "date": ds,
            "date_fmt": fmt_date(ds),
            "weekday": wd,
            "count": len(ids),
            "names": [name_of.get(str(u), str(u)) for u in ids],
        })

    today_names = [name_of.get(str(u), str(u)) for u in (streams.get(today_s) or [])]
    total_twitch_msgs = sum(int(u.get("count", 0) or 0) for day in msg_counts.values() for u in day.values())
    avg_mods = round(sum(len(streams[d]) for d in real_days) / total_stream_days, 1) if total_stream_days else 0

    joins = []
    for j in reversed(stats.get("joins") or []):
        joins.append({
            "name": j.get("name", "?"),
            "age_days": j.get("age_days", 0),
            "ts": j.get("ts", ""),
            "ts_fmt": fmt_date((j.get("ts") or "")[:10]),
            "avatar": j.get("avatar") or "",
        })
        if len(joins) >= 12:
            break

    guild = guild or {}
    first_stream = fmt_date(real_days_sorted[0]) if real_days_sorted else "—"
    last_stream = fmt_date(real_days_sorted[-1]) if real_days_sorted else "—"

    return {
        "generated_at": datetime.datetime.now(DE_TZ).strftime("%d.%m.%Y %H:%M"),
        "today": today_s,
        "today_fmt": fmt_date(today_s),
        "month_name": MONTH_NAMES[month],
        "year": year,
        "live": live_guess,
        "streamer": os.getenv("STREAMER_CHANNEL", "").lower(),
        "invite": os.getenv("DISCORD_INVITE_URL", ""),
        "guild": {
            "name": guild.get("name") or "ByGorgii",
            "icon_url": guild.get("icon_url") or "",
            "member_count": guild.get("member_count") or 0,
            "online_count": guild.get("online_count") or 0,
        },
        "now_playing": now_playing,
        "kpis": {
            "mods": len(users),
            "stream_days": total_stream_days,
            "month_streams": len(this_month_days),
            "last_month_streams": len(last_month_days),
            "today_present": len(streams.get(today_s) or []),
            "twitch_messages": total_twitch_msgs,
            "discord_messages": sum(v.get("total", 0) for v in (stats.get("messages") or {}).values()),
            "voice_seconds": sum((stats.get("voice_time") or {}).values()),
            "voice_label": fmt_voice(sum((stats.get("voice_time") or {}).values())),
            "joins": len(stats.get("joins") or []),
            "avg_mods": avg_mods,
            "first_stream": first_stream,
            "last_stream": last_stream,
        },
        "today_names": today_names,
        "roster": roster,
        "weeks": weeks,
        "weekdays": WEEKDAY_SHORT,
        "weekday_counts": weekday_counts,
        "hours": hour_totals,
        "monthly": monthly,
        "daily_messages": daily_msgs,
        "daily_voice": daily_voice,
        "msg_leaderboard": msg_lb,
        "voice_leaderboard": voice_lb,
        "recent_streams": recent_streams,
        "recent_joins": joins,
    }
