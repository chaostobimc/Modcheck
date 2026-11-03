"""Twitch Helix API, Stream-Metadaten, Kanalpunkte-Cache, Clips-Fetcher & TwitchBot."""
from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any
import aiohttp
import discord
from twitchio.ext import commands as t_commands

from bot_core.activity_helpers import (
    compute_activity_score,
    eligible_stream_days_for_user,
    get_activity_grade,
    get_current_streak,
    user_was_present,
)
from bot_core.config import (
    COINS_PER_CHANNEL_POINT,
    COLOR_INFO,
    COLOR_MOD_BAN,
    COLOR_MOD_DELETE,
    COLOR_MOD_TIMEOUT,
    COLOR_MOD_UNBAN,
    COLOR_MOD_UNTIMEOUT,
    COLOR_MOD_WARN,
    STREAMER_CHANNEL,
    STREAMER_OAUTH_TOKEN,
    STREAMER_USER_ID,
    TWITCH_CLIENT_ID,
    TWITCH_CLIENT_SECRET,
    TWITCH_LOG_CHANNEL_ID,
    TWITCH_TOKEN,
    now_berlin,
    today_berlin,
)
from bot_core.stats import stats_tracker
from bot_core.storage import load_data, save_data

_twitch_app_token: str | None = None
_twitch_token_expires: float = 0.0
_cached_rewards: list[dict[str, Any]] = []
_rewards_last_fetch: float = 0.0
_stream_live_cache: dict[str, Any] = {
    "live": False,
    "checked_at": 0.0,
    "broadcaster_id": "",
    "info": None,
}


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} Sekunde{'n' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} Minute{'n' if minutes != 1 else ''}"
    if seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}min" if minutes > 0 else f"{hours} Stunde{'n' if hours != 1 else ''}"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    return f"{days}T {hours}h" if hours > 0 else f"{days} Tag{'e' if days != 1 else ''}"


def build_twitch_mod_embed(
    action: str,
    moderator: str,
    target_user: str,
    reason: str | None = None,
    duration: int | None = None,
    deleted_message: str | None = None,
    channel: str | None = None,
) -> discord.Embed:
    now = datetime.datetime.now(datetime.timezone.utc)
    action_configs = {
        "ban": {
            "title": "🔨 User gebannt",
            "color": COLOR_MOD_BAN,
            "description": f"**{target_user}** wurde permanent gebannt.",
        },
        "timeout": {
            "title": "⏱️ User getimeoutet",
            "color": COLOR_MOD_TIMEOUT,
            "description": f"**{target_user}** wurde temporär getimeoutet.",
        },
        "warn": {
            "title": "⚠️ User verwarnt",
            "color": COLOR_MOD_WARN,
            "description": f"**{target_user}** wurde verwarnt.",
        },
        "delete": {
            "title": "🗑️ Nachricht gelöscht",
            "color": COLOR_MOD_DELETE,
            "description": f"Eine Nachricht von **{target_user}** wurde gelöscht.",
        },
        "unban": {
            "title": "✅ User entbannt",
            "color": COLOR_MOD_UNBAN,
            "description": f"**{target_user}** wurde entbannt.",
        },
        "untimeout": {
            "title": "✅ Timeout aufgehoben",
            "color": COLOR_MOD_UNTIMEOUT,
            "description": f"Der Timeout von **{target_user}** wurde aufgehoben.",
        },
    }
    cfg = action_configs.get(
        action,
        {
            "title": "📋 Moderations-Aktion",
            "color": COLOR_INFO,
            "description": f"Aktion an **{target_user}**.",
        },
    )

    embed = discord.Embed(
        title=cfg["title"],
        description=cfg["description"],
        color=cfg["color"],
        timestamp=now,
    )
    if channel:
        embed.add_field(
            name="📺 Kanal",
            value=f"[{channel}](https://twitch.tv/{channel})",
            inline=True,
        )
    embed.add_field(name="🛡️ Moderator", value=f"`{moderator}`", inline=True)
    embed.add_field(
        name="👤 User",
        value=f"[{target_user}](https://twitch.tv/{target_user})",
        inline=True,
    )
    if duration is not None and action == "timeout":
        embed.add_field(name="⏳ Dauer", value=f"**{format_duration(duration)}**", inline=True)

    embed.add_field(
        name="📝 Begründung",
        value=f"```{reason.strip()}```" if reason and reason.strip() else "*Keine Begründung angegeben*",
        inline=False,
    )
    if deleted_message:
        embed.add_field(
            name="💬 Gelöschte Nachricht",
            value=f"```{deleted_message[:500]}```",
            inline=False,
        )
    embed.set_footer(text=f"Twitch Mod Logger • {now.strftime('%d.%m.%Y um %H:%M:%S')} UTC")
    return embed


async def get_twitch_app_token() -> str | None:
    """Holt ein Twitch App-Access-Token oder fällt auf vorhandene User-OAuth-Tokens zurück."""
    global _twitch_app_token, _twitch_token_expires
    if not TWITCH_CLIENT_ID:
        return None
    if not TWITCH_CLIENT_SECRET:
        fallback = (STREAMER_OAUTH_TOKEN or TWITCH_TOKEN or "").replace("oauth:", "").strip()
        return fallback or None

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if _twitch_app_token and now < _twitch_token_expires - 60:
        return _twitch_app_token
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": TWITCH_CLIENT_ID,
                    "client_secret": TWITCH_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    _twitch_app_token = payload.get("access_token")
                    _twitch_token_expires = now + payload.get("expires_in", 3600)
                    return _twitch_app_token
    except Exception as e:
        print(f"⚠️ Twitch App-Token Fehler: {e}")

    fallback = (STREAMER_OAUTH_TOKEN or TWITCH_TOKEN or "").replace("oauth:", "").strip()
    return fallback or None


def _record_stream_metadata(stream_obj: dict[str, Any]) -> None:
    """Speichert Stream-Titel, Kategorien, Peak-Viewer & Dauer in data['stream_meta']."""
    today_s = str(today_berlin())
    now_de = now_berlin()
    data = load_data()
    data["streams"].setdefault(today_s, [])
    meta_map = data.setdefault("stream_meta", {})
    meta = meta_map.setdefault(
        today_s,
        {
            "title": "",
            "games": [],
            "peak_viewers": 0,
            "started_at": "",
            "last_seen_live": "",
            "duration_minutes": 0,
        },
    )

    title = (stream_obj.get("title") or "").strip()
    if title:
        meta["title"] = title[:140]

    game = (stream_obj.get("game_name") or "").strip()
    if game and game not in meta["games"]:
        meta["games"].append(game)

    viewers = int(stream_obj.get("viewer_count") or 0)
    if viewers > int(meta.get("peak_viewers") or 0):
        meta["peak_viewers"] = viewers

    started_at = stream_obj.get("started_at") or ""
    if started_at:
        meta["started_at"] = started_at
        try:
            start_dt = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            dur_min = max(1, int((datetime.datetime.now(datetime.timezone.utc) - start_dt).total_seconds() // 60))
            meta["duration_minutes"] = max(int(meta.get("duration_minutes") or 0), dur_min)
        except Exception:
            pass

    meta["last_seen_live"] = now_de.strftime("%H:%M")
    save_data(data, force=False)


async def check_streamer_live() -> bool:
    """Prüft adaptiv via Helix API, ob der Streamer live ist (25s Cache offline, 60s online)."""
    if not STREAMER_CHANNEL:
        return False
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    ttl = 60.0 if _stream_live_cache["live"] else 25.0
    if now - _stream_live_cache["checked_at"] < ttl:
        return bool(_stream_live_cache["live"])

    token = await get_twitch_app_token()
    if not token:
        # Fallback: Wenn heute bereits ein Stream aktiv erfasst wurde, nicht blockieren
        today_s = str(today_berlin())
        return today_s in load_data().get("streams", {})

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_login": STREAMER_CHANNEL},
                headers={
                    "Client-ID": TWITCH_CLIENT_ID,
                    "Authorization": f"Bearer {token}",
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    streams = payload.get("data", [])
                    is_live = len(streams) > 0
                    _stream_live_cache["live"] = is_live
                    _stream_live_cache["checked_at"] = now
                    if is_live:
                        s0 = streams[0]
                        _stream_live_cache["info"] = s0
                        if s0.get("user_id"):
                            _stream_live_cache["broadcaster_id"] = str(s0["user_id"])
                        _record_stream_metadata(s0)
                    else:
                        _stream_live_cache["info"] = None
                    return is_live
    except Exception as e:
        print(f"⚠️ Twitch Live-Check Fehler: {e}")
    return bool(_stream_live_cache["live"])


def is_stream_currently_live_cached() -> bool:
    return bool(_stream_live_cache.get("live", False))


def get_live_stream_info_cached() -> dict[str, Any] | None:
    return _stream_live_cache.get("info") if _stream_live_cache.get("live") else None


async def resolve_broadcaster_id() -> str | None:
    if STREAMER_USER_ID:
        return STREAMER_USER_ID
    if _stream_live_cache.get("broadcaster_id"):
        return _stream_live_cache["broadcaster_id"]
    if not STREAMER_CHANNEL:
        return None
    token = await get_twitch_app_token()
    if not token:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/users",
                params={"login": STREAMER_CHANNEL},
                headers={
                    "Client-ID": TWITCH_CLIENT_ID,
                    "Authorization": f"Bearer {token}",
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = (await resp.json()).get("data", [])
                    if data:
                        bid = str(data[0]["id"])
                        _stream_live_cache["broadcaster_id"] = bid
                        return bid
    except Exception:
        pass
    return None


_streamer_profile_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


async def fetch_streamer_profile() -> dict[str, Any] | None:
    """Lädt das Twitch-Profil des Streamers (Avatar, Anzeigename, Beschreibung) mit 6h Cache."""
    import time as _t

    now = _t.monotonic()
    if _streamer_profile_cache["data"] and (now - _streamer_profile_cache["fetched_at"]) < 21600:
        return _streamer_profile_cache["data"]
    if not STREAMER_CHANNEL or not TWITCH_CLIENT_ID:
        return None
    token = await get_twitch_app_token()
    if not token:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/users",
                params={"login": STREAMER_CHANNEL},
                headers={
                    "Client-ID": TWITCH_CLIENT_ID,
                    "Authorization": f"Bearer {token}",
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    rows = (await resp.json()).get("data", [])
                    if rows:
                        prof = rows[0]
                        _streamer_profile_cache["data"] = prof
                        _streamer_profile_cache["fetched_at"] = now
                        if prof.get("id"):
                            _stream_live_cache["broadcaster_id"] = str(prof["id"])
                        return prof
    except Exception:
        pass
    return _streamer_profile_cache["data"]


async def fetch_recent_clips(limit: int = 20) -> list[dict[str, Any]]:
    bid = await resolve_broadcaster_id()
    if not bid or not TWITCH_CLIENT_ID:
        return []
    token = await get_twitch_app_token()
    if not token:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/clips",
                params={"broadcaster_id": bid, "first": limit},
                headers={
                    "Client-ID": TWITCH_CLIENT_ID,
                    "Authorization": f"Bearer {token}",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("data", [])
    except Exception as e:
        print(f"⚠️ [ClipTracker] API Fehler: {e}")
    return []


async def fetch_channel_rewards() -> list[dict[str, Any]]:
    bid = await resolve_broadcaster_id()
    access_token = (STREAMER_OAUTH_TOKEN or TWITCH_TOKEN or "").replace("oauth:", "").strip()
    if not bid or not access_token or not TWITCH_CLIENT_ID:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/channel_points/custom_rewards",
                params={"broadcaster_id": bid},
                headers={
                    "Client-ID": TWITCH_CLIENT_ID,
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    raw_rewards = (await resp.json()).get("data", [])
                    rewards = []
                    for r in raw_rewards:
                        if not r.get("is_enabled", True) or r.get("is_paused", False):
                            continue
                        pt_cost = int(r.get("cost", 100) or 100)
                        rewards.append({
                            "id": r.get("id", ""),
                            "title": r.get("title", "Belohnung"),
                            "prompt": r.get("prompt", ""),
                            "twitch_cost": pt_cost,
                            "coin_cost": max(50, pt_cost * COINS_PER_CHANNEL_POINT),
                        })
                    return sorted(rewards, key=lambda x: x["coin_cost"])
    except Exception as e:
        print(f"⚠️ Fehler beim Abrufen der Kanalpunkte-Belohnungen: {e}")
    return []


async def get_cached_rewards(force_refresh: bool = False) -> list[dict[str, Any]]:
    global _cached_rewards, _rewards_last_fetch
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if force_refresh or not _cached_rewards or (now - _rewards_last_fetch > 300):
        fresh = await fetch_channel_rewards()
        if fresh:
            _cached_rewards = fresh
            _rewards_last_fetch = now
    return _cached_rewards


class TwitchBot(t_commands.Bot):
    """Twitch IRC & Moderations-Bot für präzises Live-Aktivitäts-Tracking & Mod-Logs."""

    def __init__(self, discord_bot: discord.Client) -> None:
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[STREAMER_CHANNEL] if STREAMER_CHANNEL else [],
        )
        self.discord_bot = discord_bot
        self._recent_messages: dict[str, str] = {}

    async def event_ready(self) -> None:
        print(f"🟣 Twitch-Bot verbunden als '{self.nick}' auf #{STREAMER_CHANNEL}")

    def _send_discord_mod_log(
        self,
        action: str,
        moderator: str,
        target: str,
        reason: str = "",
        duration: int | None = None,
        deleted_msg: str | None = None,
    ) -> None:
        stats_tracker.add_mod_action(action, target, moderator, reason, duration)
        if not TWITCH_LOG_CHANNEL_ID or not self.discord_bot:
            return
        embed = build_twitch_mod_embed(
            action=action,
            moderator=moderator,
            target_user=target,
            reason=reason,
            duration=duration,
            deleted_message=deleted_msg,
            channel=STREAMER_CHANNEL,
        )
        loop = self.discord_bot.loop
        if loop and loop.is_running():
            async def _post() -> None:
                ch = self.discord_bot.get_channel(TWITCH_LOG_CHANNEL_ID)
                if ch is None:
                    try:
                        ch = await self.discord_bot.fetch_channel(TWITCH_LOG_CHANNEL_ID)
                    except Exception:
                        return
                if ch:
                    await ch.send(embed=embed)

            asyncio.run_coroutine_threadsafe(_post(), loop)

    async def event_raw_data(self, data: str) -> None:
        """Erkennt Twitch IRC CLEARCHAT (Ban/Timeout) & CLEARMSG (Nachricht gelöscht)."""
        try:
            if "CLEARCHAT" in data:
                dur_m = re.search(r"ban-duration=(\d+)", data)
                target_m = re.search(r"CLEARCHAT #\S+ :(\S+)", data)
                reason_m = re.search(r"ban-reason=([^;\s]*)", data)
                reason = reason_m.group(1).replace("\\s", " ") if reason_m else ""
                if target_m:
                    target = target_m.group(1).strip()
                    last_msg = self._recent_messages.get(target.lower())
                    if dur_m:
                        self._send_discord_mod_log(
                            "timeout", "Twitch-Mod", target, reason=reason, duration=int(dur_m.group(1)), deleted_msg=last_msg
                        )
                    else:
                        self._send_discord_mod_log("ban", "Twitch-Mod", target, reason=reason, deleted_msg=last_msg)
            elif "CLEARMSG" in data:
                login_m = re.search(r"login=([^;\s]+)", data)
                msg_m = re.search(r"CLEARMSG #\S+ :(.*)$", data)
                if login_m:
                    target = login_m.group(1).strip()
                    deleted = msg_m.group(1).strip() if msg_m else self._recent_messages.get(target.lower(), "")
                    self._send_discord_mod_log("delete", "Twitch-Mod", target, deleted_msg=deleted)
        except Exception as e:
            print(f"⚠️ Twitch Raw-Event Parse-Fehler: {e}")

    async def event_message(self, message) -> None:
        if message.echo or not message.author:
            return

        author_name = message.author.name.lower()
        content = message.content or ""
        self._recent_messages[author_name] = content
        if len(self._recent_messages) > 500:
            oldest_key = next(iter(self._recent_messages))
            self._recent_messages.pop(oldest_key, None)

        try:
            is_live = await check_streamer_live()
            if is_live:
                data = load_data()
                matched_uid = next(
                    (
                        uid
                        for uid, uinfo in data.get("users", {}).items()
                        if uinfo.get("twitch_name", "").lower() == author_name
                    ),
                    None,
                )
                if matched_uid:
                    now_de = now_berlin()
                    today_s = str(now_de.date())
                    hour_idx = now_de.hour
                    slot_idx = now_de.hour * 12 + (now_de.minute // 5)  # 5-Minuten-Fenster (0..287)
                    ts_hm = now_de.strftime("%H:%M")

                    day_list = data["streams"].setdefault(today_s, [])
                    first_presence_today = matched_uid not in day_list
                    if first_presence_today:
                        day_list.append(matched_uid)

                    day_msgs = data["message_counts"].setdefault(today_s, {})
                    u_entry = day_msgs.setdefault(
                        matched_uid,
                        {"count": 0, "hours": {}, "slots": [], "first_ts": ts_hm, "last_ts": ts_hm},
                    )
                    hrs = u_entry.get("hours")
                    if isinstance(hrs, list):
                        hrs = {str(i): int(v) for i, v in enumerate(hrs[:24]) if int(v or 0) > 0}
                    elif not isinstance(hrs, dict):
                        hrs = {}
                    u_entry["hours"] = hrs

                    if not isinstance(u_entry.get("slots"), list):
                        u_entry["slots"] = []

                    h_key = str(hour_idx)
                    u_entry["count"] = int(u_entry.get("count", 0) or 0) + 1
                    u_entry["hours"][h_key] = int(u_entry["hours"].get(h_key, 0) or 0) + 1
                    if not u_entry.get("first_ts"):
                        u_entry["first_ts"] = ts_hm
                    u_entry["last_ts"] = ts_hm

                    new_slot = slot_idx not in u_entry["slots"]
                    if new_slot:
                        u_entry["slots"].append(slot_idx)
                        u_entry["slots"].sort()

                    # Sofort auf Disk speichern bei Erst-Anwesenheit oder neuem 5-Minuten-Slot, sonst gedrosselt
                    save_data(data, force=(first_presence_today or new_slot))
        except Exception as e:
            print(f"⚠️ Twitch Aktivitäts-Tracking Fehler: {e}")

        await self.handle_commands(message)

    @t_commands.command(name="ping")
    async def twitch_ping(self, ctx: t_commands.Context) -> None:
        await ctx.send(f"Pong! @{ctx.author.name} — Modcheck Bot läuft stabil!")

    @t_commands.command(name="bot")
    async def twitch_bot_info(self, ctx: t_commands.Context) -> None:
        await ctx.send(
            f"@{ctx.author.name} Modcheck Bot v2.1 — Präziser Stream-Aktivitäts-Tracker, Musik & Community-System!"
        )

    @t_commands.command(name="stats")
    async def twitch_stats(self, ctx: t_commands.Context) -> None:
        author = ctx.author.name.lower()
        data = load_data()
        matched_uid = next(
            (uid for uid, info in data.get("users", {}).items() if info.get("twitch_name", "").lower() == author),
            None,
        )
        if not matched_uid:
            await ctx.send(f"@{ctx.author.name} Dein Account ist noch nicht im Discord per /connect verknüpft!")
            return
        days = eligible_stream_days_for_user(data, matched_uid)
        present = sum(1 for d in days if user_was_present(data, matched_uid, d))
        streak = get_current_streak(data, matched_uid)
        pct = (present / len(days) * 100) if days else 0.0
        grade, _, _ = get_activity_grade(pct)
        score = compute_activity_score(data, matched_uid, days)
        await ctx.send(
            f"@{ctx.author.name} Streams: {present}/{len(days)} ({pct:.0f}% • Note {grade}) | "
            f"Score: {score:.0f}/100 | Streak: {streak}🔥"
        )
