"""Präzise Aktivitäts-Analyse, Scores, Zeitfenster, Visualisierungen & interaktive Berichte."""
from __future__ import annotations

import asyncio
import calendar
import datetime
from typing import Any
import discord

from bot_core.config import (
    COLOR_ERROR,
    COLOR_GOLD,
    COLOR_TWITCH,
    COLOR_WEEKLY,
    MONTH_NAMES,
    STREAMER_CHANNEL,
    WEEKDAY_SHORT,
    today_berlin,
)
from bot_core.storage import get_user_absences, is_user_absent, load_data


def format_minutes_hm(minutes: int | float) -> str:
    mins = max(0, int(round(minutes)))
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m" if m else f"{h}h"


def fmt_iso_de(iso_date: str | None) -> str:
    if not iso_date or iso_date == "Nie":
        return "Nie"
    try:
        return datetime.date.fromisoformat(iso_date[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return iso_date


# ── Stream-Tage & fairer Tracking-Zeitraum pro User ────────────────────────

def is_real_stream_day(data: dict[str, Any], date_str: str) -> bool:
    """Ein Tag gilt als echter Stream-Tag, wenn Mods aktiv waren ODER der Stream mind. 10 Min. lief."""
    if data.get("streams", {}).get(date_str):
        return True
    meta = data.get("stream_meta", {}).get(date_str, {})
    return int(meta.get("duration_minutes") or 0) >= 10


def real_stream_days(data: dict[str, Any], up_to_today: bool = True) -> list[str]:
    today_s = str(today_berlin())
    all_keys = set(data.get("streams", {}).keys()) | set(data.get("stream_meta", {}).keys())
    days = [d for d in sorted(all_keys) if is_real_stream_day(data, d)]
    if up_to_today:
        days = [d for d in days if d <= today_s]
    return days


def get_user_tracking_start(data: dict[str, Any], uid: str) -> str:
    """Ermittelt das faire Startdatum des Trackings für einen User."""
    uinfo = data.get("users", {}).get(str(uid), {})
    explicit = (uinfo.get("connected_at") or "").strip()
    if explicit:
        return explicit

    seen_dates: list[str] = []
    for d_str, u_list in data.get("streams", {}).items():
        if str(uid) in u_list:
            seen_dates.append(d_str)
    for ab in get_user_absences(str(uid)):
        if ab.get("date"):
            seen_dates.append(ab["date"])

    if seen_dates:
        return min(seen_dates)
    days = real_stream_days(data)
    return days[0] if days else str(today_berlin())


def eligible_stream_days_for_user(
    data: dict[str, Any],
    uid: str,
    days: list[str] | None = None,
) -> list[str]:
    """Filtert Stream-Tage auf den Zeitraum ab Registrierung/Erst-Sichtung des Users."""
    base_days = days if days is not None else real_stream_days(data)
    start_s = get_user_tracking_start(data, uid)
    return [d for d in base_days if d >= start_s]


def user_was_present(data: dict[str, Any], uid: str, date_str: str) -> bool:
    """Anwesend im Stream ODER entschuldigt abgemeldet an einem echten Stream-Tag."""
    if not is_real_stream_day(data, date_str):
        return False
    if str(uid) in data.get("streams", {}).get(date_str, []):
        return True
    return is_user_absent(str(uid), date_str)


def user_was_live_in_chat(data: dict[str, Any], uid: str, date_str: str) -> bool:
    """War tatsächlich aktiv im Twitch-Chat (ohne Abwesenheits-Ersatz)."""
    return str(uid) in data.get("streams", {}).get(date_str, [])


# ── Visuelle Helfer, Noten & Streaks ───────────────────────────────────────

def make_progress_bar(value: int, total: int, length: int = 10) -> str:
    if total <= 0:
        return f"`{'░' * length}` **0%**"
    ratio = min(max(value / total, 0.0), 1.0)
    filled = round(ratio * length)
    bar = "█" * filled + "░" * (length - filled)
    return f"`{bar}` **{ratio * 100:.0f}%**"


def get_activity_grade(percentage: float) -> tuple[str, str, int]:
    if percentage >= 90:
        return "S+", "👑", 0xF1C40F
    if percentage >= 80:
        return "A", "🟢", 0x2ECC71
    if percentage >= 65:
        return "B", "🔵", 0x3498DB
    if percentage >= 50:
        return "C", "🟡", 0xF39C12
    if percentage >= 30:
        return "D", "🟠", 0xE67E22
    return "F", "🔴", 0xE74C3C


def get_rank_emoji(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"`#{rank}`")


def get_current_streak(data: dict[str, Any], uid: str) -> int:
    streak = 0
    for d in reversed(eligible_stream_days_for_user(data, uid)):
        if user_was_present(data, uid, d):
            streak += 1
        else:
            break
    return streak


def get_longest_streak(data: dict[str, Any], uid: str) -> int:
    longest = 0
    current = 0
    for d in eligible_stream_days_for_user(data, uid):
        if user_was_present(data, uid, d):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def get_days_since_last_seen(data: dict[str, Any], uid: str) -> tuple[str, int]:
    """Gibt (letztes_datum_iso, verpasste_streams_seitdem) zurück."""
    days = eligible_stream_days_for_user(data, uid)
    live_days = [d for d in days if user_was_live_in_chat(data, uid, d)]
    last_seen = live_days[-1] if live_days else "Nie"
    missed_unexcused = 0
    for d in reversed(days):
        if user_was_present(data, uid, d):
            break
        missed_unexcused += 1
    return last_seen, missed_unexcused


def _iter_hour_counts(hrs: Any) -> list[tuple[int, int]]:
    """Liest sowohl das bestehende Dict-Format {'15': 8} als auch Listen [0..23]."""
    out: list[tuple[int, int]] = []
    if isinstance(hrs, dict):
        for k, v in hrs.items():
            try:
                hi = int(k)
                val = int(v or 0)
                if 0 <= hi < 24 and val > 0:
                    out.append((hi, val))
            except (ValueError, TypeError):
                continue
    elif isinstance(hrs, list):
        for hi, v in enumerate(hrs[:24]):
            val = int(v or 0)
            if val > 0:
                out.append((hi, val))
    return out


def _estimate_day_active_minutes(u_info: dict[str, Any]) -> int:
    """Berechnet aktive Minuten über 5-Minuten-Slots (oder schätzt fair für Altdaten)."""
    slots = u_info.get("slots")
    if isinstance(slots, list) and slots:
        return len(slots) * 5
    hour_pairs = _iter_hour_counts(u_info.get("hours"))
    active_h = len(hour_pairs)
    cnt = int(u_info.get("count", 0) or 0)
    if active_h == 0 and cnt > 0:
        return 10
    return min(active_h * 30, max(active_h * 12, min(cnt * 2, active_h * 40)))


def get_period_detailed_stats(
    data: dict[str, Any],
    uid: str,
    days: list[str],
) -> dict[str, Any]:
    """Aggregiert Nachrichten, echte Präsenzzeit, 24h-Verteilung & Wochentags-Quoten."""
    uid = str(uid)
    msg_counts = data.get("message_counts", {})
    total_messages = 0
    days_with_msgs = 0
    active_minutes_total = 0
    hours_agg = [0] * 24
    active_hours_total = 0
    weekday_streams = [0] * 7
    weekday_present = [0] * 7
    recent_spans: list[str] = []

    day_set = set(days)
    for d_str in sorted(day_set):
        try:
            wd = datetime.date.fromisoformat(d_str).weekday()
            weekday_streams[wd] += 1
            if user_was_present(data, uid, d_str):
                weekday_present[wd] += 1
        except ValueError:
            pass

        u_info = msg_counts.get(d_str, {}).get(uid)
        if not u_info:
            continue
        cnt = int(u_info.get("count", 0) or 0)
        if cnt > 0:
            total_messages += cnt
            days_with_msgs += 1
            active_minutes_total += _estimate_day_active_minutes(u_info)
            f_ts = u_info.get("first_ts")
            l_ts = u_info.get("last_ts")
            if f_ts and l_ts:
                recent_spans.append(f"`{fmt_iso_de(d_str)[:6]}` {f_ts}–{l_ts} ({cnt} 💬)")

        for h_idx, val in _iter_hour_counts(u_info.get("hours")):
            hours_agg[h_idx] += val
            active_hours_total += 1

    peak_hour = max(range(24), key=lambda h: hours_agg[h]) if any(hours_agg) else 0
    return {
        "total_messages": total_messages,
        "days_with_msgs": days_with_msgs,
        "avg_per_stream": (total_messages / days_with_msgs) if days_with_msgs else 0.0,
        "avg_per_hour": (total_messages / active_hours_total) if active_hours_total else 0.0,
        "active_minutes_total": active_minutes_total,
        "avg_active_minutes": (active_minutes_total / days_with_msgs) if days_with_msgs else 0.0,
        "peak_hour": peak_hour,
        "peak_hour_count": hours_agg[peak_hour],
        "hours_agg": hours_agg,
        "weekday_streams": weekday_streams,
        "weekday_present": weekday_present,
        "recent_spans": recent_spans[-5:],
    }


def _period_end_streak(data: dict[str, Any], uid: str, elig: list[str]) -> int:
    streak = 0
    for d in reversed(elig):
        if user_was_present(data, uid, d):
            streak += 1
        else:
            break
    return streak


def compute_activity_score(
    data: dict[str, Any],
    uid: str,
    days: list[str] | None = None,
) -> float:
    """Berechnet einen fairen 0–100 Aktivitäts-Score (Anwesenheit + Präsenzdauer + Konstanz)."""
    elig = days if days is not None else eligible_stream_days_for_user(data, uid)
    if not elig:
        return 0.0
    present = sum(1 for d in elig if user_was_present(data, uid, d))
    pct = (present / len(elig)) * 100.0

    det = get_period_detailed_stats(data, uid, elig)
    msg_counts = data.get("message_counts", {})
    tracked_msg_streams = sum(
        1 for d in elig if d in msg_counts and not (not user_was_live_in_chat(data, uid, d) and is_user_absent(uid, d))
    )
    denom = max(1, tracked_msg_streams) if tracked_msg_streams > 0 else max(1, det["days_with_msgs"])

    avg_active_min = det["active_minutes_total"] / denom if tracked_msg_streams > 0 else det["avg_active_minutes"]
    avg_msgs = det["total_messages"] / denom if tracked_msg_streams > 0 else det["avg_per_stream"]

    # Bis zu 65 Punkte für Anwesenheitsquote
    attendance_pts = min(65.0, pct * 0.65)
    # Bis zu 15 Punkte für aktive Präsenzdauer pro Stream (voll ab Ø 60 Min./Stream)
    presence_pts = min(15.0, (avg_active_min / 60.0) * 15.0)
    # Bis zu 10 Punkte für aktive Chat-Beteiligung (voll ab Ø 25 Nachrichten/Stream, kein Spam-Vorteil!)
    chat_pts = min(10.0, (avg_msgs / 25.0) * 10.0)
    # Bis zu 10 Punkte für laufende Streak im Zeitraum (voll ab 7 Streams in Folge)
    streak = _period_end_streak(data, uid, elig)
    streak_pts = min(10.0, streak * 1.45)

    return round(min(100.0, attendance_pts + presence_pts + chat_pts + streak_pts), 1)


def make_hourly_sparkline(hours_agg: list[int]) -> str:
    """Erstellt eine kompakte 24-Stunden-Aktivitätsgrafik für Discord."""
    blocks = "·▁▂▃▄▅▆▇█"
    mx = max(hours_agg) if hours_agg else 0
    if mx <= 0:
        return "`Keine Stunden-Daten im gewählten Zeitraum`"
    chars = []
    for val in hours_agg[:24]:
        if val <= 0:
            chars.append("·")
        else:
            idx = max(1, min(8, round((val / mx) * 8)))
            chars.append(blocks[idx])
    line1 = "`00    06    12    18   23`"
    line2 = "`" + "".join(chars) + "`"
    return f"{line1}\n{line2}"


def make_weekday_summary(weekday_present: list[int], weekday_streams: list[int]) -> str:
    lines = []
    for idx in range(7):
        s_cnt = weekday_streams[idx]
        p_cnt = weekday_present[idx]
        if s_cnt == 0:
            lines.append(f"`{WEEKDAY_SHORT[idx]}` — *kein Stream*")
        else:
            pct = round(p_cnt / s_cnt * 100)
            bar_len = round((p_cnt / s_cnt) * 6)
            bar = "▰" * bar_len + "▱" * (6 - bar_len)
            lines.append(f"`{WEEKDAY_SHORT[idx]}` `{bar}` **{p_cnt}/{s_cnt}** ({pct}%)")
    return "\n".join(lines)


def make_streak_calendar(data: dict[str, Any], uid: str, year: int, month: int) -> str:
    cal = calendar.monthcalendar(year, month)
    today = today_berlin()
    start_s = get_user_tracking_start(data, uid)
    lines = ["`Mo Di Mi Do Fr Sa So`"]
    for week in cal:
        cells = []
        for day_num in week:
            if day_num == 0:
                cells.append("  ")
                continue
            d_obj = datetime.date(year, month, day_num)
            d_str = str(d_obj)
            if d_obj > today:
                cells.append("· ")
            elif is_real_stream_day(data, d_str):
                if user_was_live_in_chat(data, uid, d_str):
                    cells.append("✓ ")
                elif is_user_absent(uid, d_str):
                    cells.append("○ ")
                elif d_str < start_s:
                    cells.append("- ")
                else:
                    cells.append("✗ ")
            else:
                cells.append("- ")
        lines.append("`" + " ".join(c.strip().ljust(2) for c in cells).rstrip() + "`")
    lines.append("-# `✓` anwesend • `○` abgemeldet • `✗` gefehlt • `-` kein Stream")
    return "\n".join(lines)


# ── Monats-, Vergleichs- & Trend-Berechnungen ──────────────────────────────

def get_month_activity_stats(data: dict[str, Any], uid: str, year: int, month: int) -> dict[str, Any]:
    prefix = f"{year:04d}-{month:02d}-"
    all_month_days = [d for d in real_stream_days(data) if d.startswith(prefix)]
    elig_days = eligible_stream_days_for_user(data, uid, all_month_days)
    # Falls der User im Monat schon vor connected_at aktiv war, auch diese Tage mitzählen
    effective_days = sorted(set(elig_days) | {d for d in all_month_days if user_was_present(data, uid, d)})

    stream_days = len(effective_days)
    live_present = sum(1 for d in effective_days if user_was_live_in_chat(data, uid, d))
    excused = sum(
        1 for d in effective_days
        if not user_was_live_in_chat(data, uid, d) and is_user_absent(uid, d)
    )
    present = live_present + excused
    absent = max(0, stream_days - present)
    pct = (present / stream_days * 100.0) if stream_days > 0 else 0.0
    grade, grade_emoji, grade_color = get_activity_grade(pct)
    det = get_period_detailed_stats(data, uid, effective_days)
    score = compute_activity_score(data, uid, effective_days) if stream_days > 0 else 0.0

    return {
        "stream_days": stream_days,
        "all_month_streams": len(all_month_days),
        "present": present,
        "live_present": live_present,
        "excused": excused,
        "absent": absent,
        "pct": pct,
        "score": score,
        "grade": grade,
        "grade_emoji": grade_emoji,
        "grade_color": grade_color,
        "messages": det,
    }


def get_month_trend_str(data: dict[str, Any], uid: str, year: int, month: int) -> str:
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    prev = get_month_activity_stats(data, uid, prev_y, prev_m)
    cur = get_month_activity_stats(data, uid, year, month)
    if prev["stream_days"] == 0 or cur["stream_days"] == 0:
        return "➖ *Kein Vergleichsmonat*"
    diff = cur["pct"] - prev["pct"]
    if diff >= 2.0:
        return f"📈 **+{diff:.1f}%** ggü. {MONTH_NAMES[prev_m][:3]}"
    if diff <= -2.0:
        return f"📉 **{diff:.1f}%** ggü. {MONTH_NAMES[prev_m][:3]}"
    return f"➖ **Stabil** ({diff:+.1f}% ggü. {MONTH_NAMES[prev_m][:3]})"


def get_period_ranking(
    data: dict[str, Any],
    days: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Erstellt ein vollständiges Ranking aller registrierten Mods für beliebige Zeiträume."""
    base_days = days if days is not None else real_stream_days(data)
    rows: list[dict[str, Any]] = []
    for uid, uinfo in data.get("users", {}).items():
        elig = eligible_stream_days_for_user(data, uid, base_days)
        eff = sorted(set(elig) | {d for d in base_days if user_was_present(data, uid, d)})
        s_cnt = len(eff)
        present = sum(1 for d in eff if user_was_present(data, uid, d))
        live_p = sum(1 for d in eff if user_was_live_in_chat(data, uid, d))
        excused = max(0, present - live_p)
        pct = (present / s_cnt * 100.0) if s_cnt > 0 else 0.0
        det = get_period_detailed_stats(data, uid, eff)
        score = compute_activity_score(data, uid, eff) if s_cnt > 0 else 0.0
        grade, g_emoji, g_color = get_activity_grade(pct)
        rows.append({
            "uid": uid,
            "display_name": uinfo.get("display_name", "Unbekannt"),
            "twitch_name": uinfo.get("twitch_name", "?"),
            "present": present,
            "live_present": live_p,
            "excused": excused,
            "stream_days": s_cnt,
            "pct": pct,
            "score": score,
            "grade": grade,
            "grade_emoji": g_emoji,
            "grade_color": g_color,
            "messages": det["total_messages"],
            "active_minutes": det["active_minutes_total"],
            "streak": get_current_streak(data, uid),
        })

    rows.sort(
        key=lambda r: (r["score"], r["pct"], r["present"], r["active_minutes"], r["messages"]),
        reverse=True,
    )
    for idx, r in enumerate(rows, 1):
        r["rank"] = idx
    return rows


def get_user_compare_stats(data: dict[str, Any], uid: str) -> dict[str, Any]:
    elig_days = eligible_stream_days_for_user(data, uid)
    total_days = len(elig_days)
    present = sum(1 for d in elig_days if user_was_present(data, uid, d))
    pct = (present / total_days * 100.0) if total_days > 0 else 0.0
    grade, grade_emoji, grade_color = get_activity_grade(pct)
    det = get_period_detailed_stats(data, uid, elig_days)
    score = compute_activity_score(data, uid, elig_days)

    ranking = get_period_ranking(data)
    rank = next((r["rank"] for r in ranking if r["uid"] == uid), len(ranking) + 1)
    last_seen, missed_since = get_days_since_last_seen(data, uid)

    return {
        "twitch": data["users"].get(uid, {}).get("twitch_name", "?"),
        "total_days": total_days,
        "present": present,
        "pct": pct,
        "score": score,
        "rank": rank,
        "current_streak": get_current_streak(data, uid),
        "longest_streak": get_longest_streak(data, uid),
        "grade": grade,
        "grade_emoji": grade_emoji,
        "grade_color": grade_color,
        "messages": det["total_messages"],
        "active_minutes": det["active_minutes_total"],
        "last_seen": fmt_iso_de(last_seen),
        "missed_since": missed_since,
    }


# ── Embed-Builder & Interaktives Monats-/Detail-/Jahres-Menü ───────────────

def build_activity_month_embed(
    data: dict[str, Any],
    member: discord.abc.User,
    monat: int,
    jahr: int,
) -> discord.Embed:
    uid = str(member.id)
    user_info = data["users"][uid]
    twitch_name = user_info.get("twitch_name", "?")
    stats = get_month_activity_stats(data, uid, jahr, monat)
    msg = stats["messages"]
    month_name = MONTH_NAMES[monat]

    month_prefix = f"{jahr:04d}-{monat:02d}-"
    month_days_all = [d for d in real_stream_days(data) if d.startswith(month_prefix)]
    month_ranking = get_period_ranking(data, month_days_all)
    rank = next((r["rank"] for r in month_ranking if r["uid"] == uid), 1)

    current_streak = get_current_streak(data, uid)
    longest_streak = get_longest_streak(data, uid)
    trend_str = get_month_trend_str(data, uid, jahr, monat)
    last_seen, _ = get_days_since_last_seen(data, uid)

    embed = discord.Embed(
        title=f"📊 Aktivitätsbericht – {member.display_name}",
        description=(
            f"**Zeitraum:** {month_name} {jahr} • {trend_str}\n"
            f"**Twitch:** [{twitch_name}](https://twitch.tv/{twitch_name}) • **Zuletzt aktiv:** `{fmt_iso_de(last_seen)}`"
        ),
        color=stats["grade_color"],
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name=f"{stats['grade_emoji']} Bewertung",
        value=f"**Note {stats['grade']}** ({stats['pct']:.1f}%)\n🎯 Score: **{stats['score']:.0f}/100**",
        inline=True,
    )
    embed.add_field(
        name="🏅 Monats-Rang",
        value=f"**#{rank}** von {len(month_ranking)}\n🔥 Streak: **{current_streak}** *(Max {longest_streak})*",
        inline=True,
    )
    embed.add_field(
        name="⏱️ Aktive Präsenz",
        value=(
            f"🕒 **{format_minutes_hm(msg['active_minutes_total'])}** gesamt\n"
            f"📊 Ø **{format_minutes_hm(msg['avg_active_minutes'])}** / Stream"
        ),
        inline=True,
    )

    excused_note = f"\n📋 Abgemeldet: **{stats['excused']}**" if stats["excused"] else ""
    embed.add_field(
        name=f"📺 Streams ({month_name})",
        value=(
            f"✅ Live da: **{stats['live_present']}** / {stats['stream_days']}"
            f"{excused_note}\n"
            f"❌ Unentschuldigt: **{stats['absent']}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="💬 Chat-Aktivität",
        value=(
            f"📝 **{msg['total_messages']:,}** Nachrichten\n"
            f"📊 Ø **{msg['avg_per_stream']:.0f}** / Stream\n"
            f"🕐 Peak: **{msg['peak_hour']:02d}:00** Uhr"
        ),
        inline=True,
    )

    elig_all = eligible_stream_days_for_user(data, uid)
    all_present = sum(1 for d in elig_all if user_was_present(data, uid, d))
    all_pct = (all_present / len(elig_all) * 100.0) if elig_all else 0.0
    embed.add_field(
        name="🌐 Gesamtbilanz",
        value=(
            f"**{all_present}/{len(elig_all)}** Streams ({all_pct:.0f}%)\n"
            f"-# Seit {fmt_iso_de(get_user_tracking_start(data, uid))}"
        ),
        inline=True,
    )

    embed.add_field(
        name="📈 Monats-Quote",
        value=make_progress_bar(stats["present"], stats["stream_days"], 12),
        inline=False,
    )
    embed.add_field(
        name=f"🗓️ Stream-Kalender – {month_name} {jahr}",
        value=make_streak_calendar(data, uid, jahr, monat),
        inline=False,
    )
    embed.set_footer(text=f"Activity Tracker v2.1 • Kanal: {STREAMER_CHANNEL or 'Nicht gesetzt'}")
    return embed


def build_activity_details_embed(
    data: dict[str, Any],
    member: discord.abc.User,
    monat: int,
    jahr: int,
) -> discord.Embed:
    uid = str(member.id)
    month_name = MONTH_NAMES[monat]
    prefix = f"{jahr:04d}-{monat:02d}-"
    days = eligible_stream_days_for_user(
        data, uid, [d for d in real_stream_days(data) if d.startswith(prefix)]
    )
    det = get_period_detailed_stats(data, uid, days)

    embed = discord.Embed(
        title=f"🕐 Zeit- & Wochentags-Analyse – {member.display_name}",
        description=f"**Zeitraum:** {month_name} {jahr} ({len(days)} relevante Stream-Tage)",
        color=COLOR_TWITCH,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="📊 24-Stunden-Aktivitätsprofil (Europe/Berlin)",
        value=make_hourly_sparkline(det["hours_agg"]),
        inline=False,
    )
    embed.add_field(
        name="📅 Anwesenheit nach Wochentagen",
        value=make_weekday_summary(det["weekday_present"], det["weekday_streams"]),
        inline=False,
    )
    if det["recent_spans"]:
        embed.add_field(
            name="⏱️ Letzte erfasste Stream-Schichten (Erste–Letzte Nachricht)",
            value="\n".join(det["recent_spans"]),
            inline=False,
        )
    embed.set_footer(text="Nutze die Buttons unten, um zur Kalenderansicht zurückzukehren")
    return embed


def build_activity_year_embed(
    data: dict[str, Any],
    member: discord.abc.User,
    jahr: int,
) -> discord.Embed:
    uid = str(member.id)
    year_days = eligible_stream_days_for_user(
        data, uid, [d for d in real_stream_days(data) if d.startswith(f"{jahr:04d}-")]
    )
    present = sum(1 for d in year_days if user_was_present(data, uid, d))
    pct = (present / len(year_days) * 100.0) if year_days else 0.0
    grade, g_emoji, g_color = get_activity_grade(pct)
    score = compute_activity_score(data, uid, year_days)
    det = get_period_detailed_stats(data, uid, year_days)

    m_lines = []
    for m in range(1, 13):
        ms = get_month_activity_stats(data, uid, jahr, m)
        if ms["stream_days"] == 0:
            continue
        mini = make_progress_bar(ms["present"], ms["stream_days"], 6)
        m_lines.append(
            f"`{MONTH_NAMES[m][:3]}` {mini} — **{ms['present']}/{ms['stream_days']}** "
            f"• {format_minutes_hm(ms['messages']['active_minutes_total'])}"
        )

    embed = discord.Embed(
        title=f"🎆 Jahresübersicht {jahr} – {member.display_name}",
        description=(
            f"**Note:** {g_emoji} **{grade}** ({pct:.1f}%) • **Score:** `{score:.0f}/100`\n"
            f"**Streams:** `{present}/{len(year_days)}` • "
            f"**Präsenzzeit:** `{format_minutes_hm(det['active_minutes_total'])}` • "
            f"**Nachrichten:** `{det['total_messages']:,}`"
        ),
        color=g_color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="📅 Monatsverlauf",
        value="\n".join(m_lines) if m_lines else "*Noch keine Streams in diesem Jahr erfasst.*",
        inline=False,
    )
    return embed


class ActivityReportView(discord.ui.View):
    """Interaktive Steuerung für /activity & /myactivity (Monate blättern, Zeiten & Jahresansicht)."""

    def __init__(self, invoker_id: int, target: discord.abc.User, monat: int, jahr: int) -> None:
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.target = target
        self.monat = monat
        self.jahr = jahr
        self.mode = "month"  # "month" | "details" | "year"
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        today = today_berlin()
        is_current_or_future = (self.jahr > today.year) or (
            self.jahr == today.year and self.monat >= today.month
        )
        self.btn_next.disabled = is_current_or_future
        self.btn_details.label = "🗓️ Kalender" if self.mode == "details" else "🕐 Zeiten & Tage"
        self.btn_year.style = (
            discord.ButtonStyle.success if self.mode == "year" else discord.ButtonStyle.secondary
        )

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Nicht deine Ansicht!", ephemeral=True)
            return False
        return True

    async def _render(self, interaction: discord.Interaction) -> None:
        data = load_data()
        self._sync_buttons()
        if self.mode == "details":
            embed = build_activity_details_embed(data, self.target, self.monat, self.jahr)
        elif self.mode == "year":
            embed = build_activity_year_embed(data, self.target, self.jahr)
        else:
            embed = build_activity_month_embed(data, self.target, self.monat, self.jahr)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        if self.mode == "year":
            self.jahr = max(2020, self.jahr - 1)
        else:
            if self.monat == 1:
                self.monat = 12
                self.jahr -= 1
            else:
                self.monat -= 1
        await self._render(interaction)

    @discord.ui.button(label="Heute", emoji="📅", style=discord.ButtonStyle.primary, row=0)
    async def btn_today(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        today = today_berlin()
        self.monat = today.month
        self.jahr = today.year
        self.mode = "month"
        await self._render(interaction)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        if self.mode == "year":
            self.jahr = min(today_berlin().year, self.jahr + 1)
        else:
            if self.monat == 12:
                self.monat = 1
                self.jahr += 1
            else:
                self.monat += 1
        await self._render(interaction)

    @discord.ui.button(label="🕐 Zeiten & Tage", style=discord.ButtonStyle.secondary, row=0)
    async def btn_details(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        self.mode = "month" if self.mode == "details" else "details"
        await self._render(interaction)

    @discord.ui.button(label="🎆 Jahresübersicht", style=discord.ButtonStyle.secondary, row=0)
    async def btn_year(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        self.mode = "month" if self.mode == "year" else "year"
        await self._render(interaction)


async def build_and_send_activity(
    interaction: discord.Interaction,
    member: discord.abc.User,
    monat: int,
    jahr: int,
) -> None:
    if monat < 1 or monat > 12:
        await interaction.followup.send("❌ Ungültiger Monat (1–12).", ephemeral=True)
        return
    if jahr < 2020 or jahr > 2100:
        await interaction.followup.send("❌ Ungültiges Jahr.", ephemeral=True)
        return

    data = load_data()
    uid = str(member.id)
    if uid not in data["users"]:
        embed = discord.Embed(
            title="❌ Nicht registriert",
            description=f"{member.mention} hat noch keinen Twitch-Account mit `/connect` verknüpft.",
            color=COLOR_ERROR,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    embed = build_activity_month_embed(data, member, monat, jahr)
    view = ActivityReportView(interaction.user.id, member, monat, jahr)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ── Automatische Monats- & Jahresberichte ──────────────────────────────────

async def send_monthly_review(
    channel: discord.abc.Messageable,
    guild: discord.Guild,
    year: int | None = None,
    month: int | None = None,
) -> None:
    data = load_data()
    if not data["users"]:
        await channel.send("❌ Keine User registriert.")
        return

    today = today_berlin()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    month_name = MONTH_NAMES[month]
    days_in_month = calendar.monthrange(year, month)[1]
    now = datetime.datetime.now(datetime.timezone.utc)
    month_prefix = f"{year:04d}-{month:02d}-"
    month_streams = [d for d in real_stream_days(data) if d.startswith(month_prefix)]

    header = discord.Embed(
        title=f"📅 Monatsbericht – {month_name} {year}",
        description=(
            f"Auswertung aller registrierten Stream-Moderatoren für **{month_name} {year}**\n"
            f"Zeitraum: `01.{month:02d}.{year}` – `{days_in_month}.{month:02d}.{year}` • "
            f"**{len(month_streams)}** Streams erfasst"
        ),
        color=COLOR_WEEKLY,
        timestamp=now,
    )
    header.set_footer(text=f"Activity Tracker v2.1 • {STREAMER_CHANNEL}")
    await channel.send(embed=header)

    user_stats = {
        uid: get_month_activity_stats(data, uid, year, month)
        for uid in data["users"]
    }
    sorted_users = sorted(
        user_stats.items(),
        key=lambda x: (
            x[1]["score"],
            x[1]["pct"],
            x[1]["present"],
            x[1]["messages"]["active_minutes_total"],
        ),
        reverse=True,
    )

    for rank, (uid, stats) in enumerate(sorted_users, 1):
        user_info = data["users"][uid]
        twitch_name = user_info.get("twitch_name", "?")
        display_name = user_info.get("display_name", "Unbekannt")
        member = guild.get_member(int(uid)) if guild else None
        mention = member.mention if member else f"**{display_name}**"
        avatar_url = member.display_avatar.url if member else None

        msg = stats["messages"]
        bar = make_progress_bar(stats["present"], stats["stream_days"], 10)
        current_streak = get_current_streak(data, uid)
        trend_str = get_month_trend_str(data, uid, year, month)

        embed = discord.Embed(
            title=f"{get_rank_emoji(rank)} {display_name}",
            description=trend_str,
            color=stats["grade_color"],
            timestamp=now,
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(
            name="👤 Moderator",
            value=f"{mention}\n[{twitch_name}](https://twitch.tv/{twitch_name})",
            inline=True,
        )
        embed.add_field(
            name=f"{stats['grade_emoji']} Bewertung",
            value=f"**Note {stats['grade']}**\n🎯 `{stats['score']:.0f}/100` Pkt.",
            inline=True,
        )
        embed.add_field(
            name="🔥 Streak & Zeit",
            value=f"**{current_streak}** Streams\n⏱️ `{format_minutes_hm(msg['active_minutes_total'])}` aktiv",
            inline=True,
        )
        embed.add_field(
            name="📺 Streams",
            value=(
                f"✅ **{stats['present']}** / {stats['stream_days']}\n"
                f"❌ {stats['absent']} gefehlt"
                + (f" ({stats['excused']} entsch.)" if stats["excused"] else "")
            ),
            inline=True,
        )
        embed.add_field(
            name="💬 Nachrichten",
            value=(
                f"📝 **{msg['total_messages']:,}** gesamt\n"
                f"📊 Ø **{msg['avg_per_stream']:.0f}** / Stream"
            ),
            inline=True,
        )
        embed.add_field(
            name="🕐 Peak-Stunde",
            value=f"**{msg['peak_hour']:02d}:00** Uhr\n({msg['peak_hour_count']} 💬)",
            inline=True,
        )
        embed.add_field(name="📈 Anwesenheits-Quote", value=bar, inline=False)
        embed.add_field(
            name=f"🗓️ Kalender – {month_name}",
            value=make_streak_calendar(data, uid, year, month),
            inline=False,
        )
        embed.set_footer(text=f"Rang #{rank} von {len(sorted_users)} • {month_name} {year}")
        await channel.send(embed=embed)
        await asyncio.sleep(0.5)

    lb_lines = []
    for rank, (uid, stats) in enumerate(sorted_users, 1):
        display_name = data["users"][uid].get("display_name", "?")
        member = guild.get_member(int(uid)) if guild else None
        mention = member.mention if member else f"**{display_name}**"
        msg = stats["messages"]
        prefix_medal = get_rank_emoji(rank)
        lb_lines.append(
            f"{prefix_medal} {mention} — **{stats['score']:.0f} Pkt.** "
            f"({stats['present']}/{stats['stream_days']} • {stats['pct']:.0f}% • "
            f"⏱️ {format_minutes_hm(msg['active_minutes_total'])} • 💬 {msg['total_messages']:,})"
        )

    total_msgs = sum(s["messages"]["total_messages"] for s in user_stats.values())
    total_mins = sum(s["messages"]["active_minutes_total"] for s in user_stats.values())
    avg_pct = sum(s["pct"] for s in user_stats.values()) / max(1, len(user_stats))

    lb_embed = discord.Embed(
        title=f"🏆 Monats-Leaderboard – {month_name} {year}",
        description="\n".join(lb_lines) or "Keine Daten",
        color=COLOR_GOLD,
        timestamp=now,
    )
    lb_embed.add_field(
        name="📊 Team-Zusammenfassung",
        value=(
            f"📺 **Streams im Monat:** `{len(month_streams)}`\n"
            f"👥 **Aktive Mods:** `{len(data['users'])}`\n"
            f"⏱️ **Mod-Präsenz gesamt:** `{format_minutes_hm(total_mins)}`\n"
            f"💬 **Nachrichten gesamt:** `{total_msgs:,}`\n"
            f"📈 **Ø Team-Quote:** `{avg_pct:.0f}%`"
        ),
        inline=False,
    )
    await channel.send(embed=lb_embed)


async def send_yearly_review(
    channel: discord.abc.Messageable,
    guild: discord.Guild,
    year: int | None = None,
) -> None:
    data = load_data()
    if not data["users"]:
        await channel.send("❌ Keine User registriert.")
        return

    if year is None:
        year = today_berlin().year

    now = datetime.datetime.now(datetime.timezone.utc)
    year_streams_all = [d for d in real_stream_days(data) if d.startswith(f"{year:04d}-")]
    header = discord.Embed(
        title=f"🎆 Jahresbericht {year}",
        description=(
            f"**Das große Jahres-Review des Mod-Teams!**\n"
            f"Zeitraum: `01.01.{year}` – `31.12.{year}` • **{len(year_streams_all)}** Streams"
        ),
        color=COLOR_GOLD,
        timestamp=now,
    )
    await channel.send(embed=header)

    user_year_stats = {}
    for uid in data["users"]:
        elig_year = eligible_stream_days_for_user(data, uid, year_streams_all)
        eff_year = sorted(set(elig_year) | {d for d in year_streams_all if user_was_present(data, uid, d)})
        present = sum(1 for d in eff_year if user_was_present(data, uid, d))
        s_cnt = len(eff_year)
        pct = (present / s_cnt * 100.0) if s_cnt > 0 else 0.0
        grade, g_emoji, g_color = get_activity_grade(pct)
        det = get_period_detailed_stats(data, uid, eff_year)
        score = compute_activity_score(data, uid, eff_year) if s_cnt > 0 else 0.0
        monthly = [get_month_activity_stats(data, uid, year, m) for m in range(1, 13)]

        user_year_stats[uid] = {
            "present": present,
            "stream_days": s_cnt,
            "absent": max(0, s_cnt - present),
            "pct": pct,
            "score": score,
            "grade": grade,
            "grade_emoji": g_emoji,
            "grade_color": g_color,
            "messages": det,
            "monthly": monthly,
            "longest_streak": get_longest_streak(data, uid),
        }

    sorted_users = sorted(
        user_year_stats.items(),
        key=lambda x: (x[1]["score"], x[1]["pct"], x[1]["present"]),
        reverse=True,
    )

    for rank, (uid, stats) in enumerate(sorted_users, 1):
        user_info = data["users"][uid]
        display_name = user_info.get("display_name", "?")
        twitch_name = user_info.get("twitch_name", "?")
        member = guild.get_member(int(uid)) if guild else None
        mention = member.mention if member else f"**{display_name}**"
        avatar_url = member.display_avatar.url if member else None
        msg = stats["messages"]

        month_bars = []
        for m in range(1, 13):
            m_stats = stats["monthly"][m - 1]
            if m_stats["stream_days"] > 0:
                p = m_stats["present"] / m_stats["stream_days"]
                bar_char = "█" if p >= 0.8 else "▓" if p >= 0.5 else "░" if p > 0 else "·"
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
            name="👤 Moderator",
            value=f"{mention}\n[{twitch_name}](https://twitch.tv/{twitch_name})",
            inline=True,
        )
        embed.add_field(
            name=f"{stats['grade_emoji']} Jahres-Bewertung",
            value=f"**Note {stats['grade']}**\n🎯 `{stats['score']:.0f}/100` Pkt.",
            inline=True,
        )
        embed.add_field(
            name="🏆 Rekord & Zeit",
            value=(
                f"🔥 **{stats['longest_streak']}** Streams\n"
                f"⏱️ `{format_minutes_hm(msg['active_minutes_total'])}` aktiv"
            ),
            inline=True,
        )
        embed.add_field(
            name="📺 Streams",
            value=(
                f"✅ **{stats['present']}** / {stats['stream_days']}\n"
                f"❌ **{stats['absent']}** gefehlt ({stats['pct']:.0f}%)"
            ),
            inline=True,
        )
        embed.add_field(
            name="💬 Nachrichten",
            value=(
                f"📝 **{msg['total_messages']:,}** gesamt\n"
                f"📊 Ø **{msg['avg_per_stream']:.0f}** / Stream"
            ),
            inline=True,
        )
        embed.add_field(
            name="🕐 Peak-Zeit",
            value=f"**{msg['peak_hour']:02d}:00** Uhr",
            inline=True,
        )
        embed.add_field(
            name="📈 Jahres-Quote",
            value=make_progress_bar(stats["present"], stats["stream_days"], 12),
            inline=False,
        )
        embed.add_field(
            name="📊 Monats-Verlauf",
            value=f"{chart_labels}\n{chart}\n╰ `█` ≥80% • `▓` ≥50% • `░` <50% • `·` kein Stream",
            inline=False,
        )
        embed.set_footer(text=f"Rang #{rank} • Jahresbericht {year}")
        await channel.send(embed=embed)
        await asyncio.sleep(0.5)

    lb_lines = []
    for rank, (uid, stats) in enumerate(sorted_users[:15], 1):
        display_name = data["users"][uid].get("display_name", "?")
        member = guild.get_member(int(uid)) if guild else None
        mention = member.mention if member else f"**{display_name}**"
        lb_lines.append(
            f"{get_rank_emoji(rank)} {mention} — **{stats['score']:.0f} Pkt.** "
            f"({stats['present']}/{stats['stream_days']} • {stats['pct']:.0f}% • "
            f"⏱️ {format_minutes_hm(stats['messages']['active_minutes_total'])})"
        )

    lb_embed = discord.Embed(
        title=f"🏆 Jahres-Leaderboard {year}",
        description="\n".join(lb_lines) or "Keine Daten",
        color=COLOR_GOLD,
        timestamp=now,
    )
    await channel.send(embed=lb_embed)
