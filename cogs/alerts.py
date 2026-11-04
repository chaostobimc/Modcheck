"""Cog: Automatische Twitch-Live-Posts, YouTube-Upload-Alerts & Mod-Inaktivitäts-Warnungen."""
from __future__ import annotations

import asyncio
import datetime
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.activity_helpers import (
    eligible_stream_days_for_user,
    fmt_iso_de,
    get_activity_grade,
    get_month_activity_stats,
    get_user_compare_stats,
    user_was_live_in_chat,
)
from bot_core.checks import is_admin_user
from bot_core.config import (
    COLOR_ABSENCE,
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_TWITCH,
    COLOR_WARNING,
    COLOR_YOUTUBE,
    LOG_CHANNEL_ID,
    REVIEW_CHANNEL_ID,
    STREAMER_CHANNEL,
    now_berlin,
    today_berlin,
)
from bot_core.storage import (
    is_user_absent,
    load_alerts_state,
    load_data,
    save_alerts_state,
)
from bot_core.twitch_service import (
    check_streamer_live,
    fetch_streamer_profile,
    get_live_stream_info_cached,
)

_UC_RE = re.compile(r"\b(UC[A-Za-z0-9_-]{22})\b")
_YT_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
_YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Cookie": "CONSENT=YES+cb; SOCS=CAISHAgBEhJnd3NfMjAyNDAxMDEtMF9SQzIaAmRlIAEaBgiA_LyuBg",
}


# ═══════════════════════════════════════════════════════════════════════════
# YOUTUBE HELFER (Keyless: @Handle / URL -> UC-ID + Atom RSS Parser)
# ═══════════════════════════════════════════════════════════════════════════

async def resolve_youtube_channel(raw_source: str) -> tuple[str | None, str]:
    """Löst eine UC-ID, @Handle oder YouTube-URL in (uc_channel_id, channel_title) auf."""
    src = (raw_source or "").strip()
    if not src:
        return None, ""

    # 1) Direkte UC-ID vorhanden?
    direct = _UC_RE.search(src)
    if direct:
        uc_id = direct.group(1)
        entries, title = await fetch_youtube_feed(uc_id)
        return uc_id, title

    # 2) Aus Handle oder URL eine aufrufbare Kanal-URL bauen
    if src.startswith("http://") or src.startswith("https://"):
        url = src
    elif src.startswith("@"):
        url = f"https://www.youtube.com/{src}"
    else:
        url = f"https://www.youtube.com/@{src}"

    try:
        async with aiohttp.ClientSession(headers=_YT_HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="ignore")
                    for pattern in (
                        r'rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"',
                        r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
                        r'"browseId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
                        r'channel_id=(UC[A-Za-z0-9_-]{22})',
                    ):
                        m = re.search(pattern, html)
                        if m:
                            uc_id = m.group(1)
                            _, title = await fetch_youtube_feed(uc_id)
                            return uc_id, title
    except Exception:
        pass

    # 3) Fallback über yt-dlp falls installiert
    try:
        import yt_dlp

        def _extract_uc() -> tuple[str | None, str]:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "playlist_items": "0",
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
                cid = info.get("channel_id") or info.get("uploader_id") or ""
                if isinstance(cid, str) and _UC_RE.fullmatch(cid):
                    return cid, str(info.get("channel") or info.get("uploader") or "")
            return None, ""

        uc_id, fallback_title = await asyncio.to_thread(_extract_uc)
        if uc_id:
            _, feed_title = await fetch_youtube_feed(uc_id)
            return uc_id, (feed_title or fallback_title)
    except Exception:
        pass

    return None, ""


async def fetch_youtube_feed(uc_id: str) -> tuple[list[dict[str, Any]], str]:
    """Liest die neuesten Uploads eines YouTube-Kanals über den offiziellen Atom-Feed (älteste zuerst)."""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={uc_id}"
    try:
        async with aiohttp.ClientSession(headers=_YT_HEADERS) as session:
            async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    return [], ""
                xml_text = await resp.text(errors="ignore")
    except Exception:
        return [], ""

    return parse_youtube_atom_xml(xml_text)


def parse_youtube_atom_xml(xml_text: str) -> tuple[list[dict[str, Any]], str]:
    """Parst den YouTube Atom-XML-Feed in eine Liste von Video-Dicts (chronologisch aufsteigend)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], ""

    channel_title = (root.findtext("atom:title", default="", namespaces=_YT_ATOM_NS) or "").strip()
    items: list[dict[str, Any]] = []

    for entry in root.findall("atom:entry", _YT_ATOM_NS):
        vid_id = (entry.findtext("yt:videoId", default="", namespaces=_YT_ATOM_NS) or "").strip()
        if not vid_id:
            continue
        title = (entry.findtext("atom:title", default="Neues Video", namespaces=_YT_ATOM_NS) or "").strip()
        published_raw = (entry.findtext("atom:published", default="", namespaces=_YT_ATOM_NS) or "").strip()
        author_name = (
            entry.findtext("atom:author/atom:name", default=channel_title, namespaces=_YT_ATOM_NS)
            or channel_title
        ).strip()

        link_url = f"https://www.youtube.com/watch?v={vid_id}"
        for link_el in entry.findall("atom:link", _YT_ATOM_NS):
            if link_el.attrib.get("rel") == "alternate" and link_el.attrib.get("href"):
                link_url = link_el.attrib["href"]
                break

        is_short = "/shorts/" in link_url
        thumb_url = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
        media_thumb = entry.find("media:group/media:thumbnail", _YT_ATOM_NS)
        if media_thumb is not None and media_thumb.attrib.get("url"):
            thumb_url = media_thumb.attrib["url"]

        pub_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        if published_raw:
            try:
                dt = datetime.datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
                pub_ts = int(dt.timestamp())
            except ValueError:
                pass

        items.append(
            {
                "video_id": vid_id,
                "title": title,
                "url": link_url,
                "channel_title": author_name or channel_title,
                "published_ts": pub_ts,
                "thumbnail": thumb_url,
                "is_short": is_short,
            }
        )

    # Atom-Feed liefert neueste zuerst -> umdrehen, damit ältere ungesehene zuerst gepostet werden
    items.reverse()
    return items, channel_title


# ═══════════════════════════════════════════════════════════════════════════
# EMBED- & VIEW-BUILDER (Twitch Live, Stream Offline, YouTube, Inaktivität)
# ═══════════════════════════════════════════════════════════════════════════

def _make_link_view(label: str, url: str, emoji: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url, emoji=emoji))
    return view


def _format_ping_text(ping_everyone: bool, ping_role_id: int) -> str:
    parts: list[str] = []
    if ping_everyone:
        parts.append("@everyone")
    if ping_role_id:
        parts.append(f"<@&{ping_role_id}>")
    return " ".join(parts)


def _playable_youtube_url(video: dict[str, Any]) -> str:
    """Gibt eine watch?v=-URL zurück, damit Discord den Player direkt in derselben Nachricht einbettet."""
    vid_id = (video.get("video_id") or "").strip()
    if vid_id:
        return f"https://www.youtube.com/watch?v={vid_id}"
    return str(video.get("url") or "")


def build_twitch_live_message(
    stream_info: dict[str, Any],
    profile: dict[str, Any] | None = None,
    ping_text: str = "",
) -> str:
    """Baut die komplette Twitch-Live-Ankündigung in EINER Nachricht inkl. nativem Discord-Stream-Embed."""
    channel_login = (STREAMER_CHANNEL or stream_info.get("user_login") or "twitch").lower()
    display_name = (
        (profile or {}).get("display_name")
        or stream_info.get("user_name")
        or STREAMER_CHANNEL
        or "Streamer"
    )
    stream_url = f"https://www.twitch.tv/{channel_login}"

    title_str = (stream_info.get("title") or "Live auf Twitch!").strip()
    game_name = (stream_info.get("game_name") or "Just Chatting").strip()
    viewers = int(stream_info.get("viewer_count") or 0)
    viewers_str = f"{viewers:,}".replace(",", ".")

    started_raw = stream_info.get("started_at") or ""
    started_val = "gerade eben"
    if started_raw:
        try:
            dt = datetime.datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
            started_val = f"<t:{int(dt.timestamp())}:R>"
        except ValueError:
            pass

    lines: list[str] = []
    if ping_text.strip():
        lines.append(ping_text.strip())
    lines.append(f"### 🔴 **{display_name} ist JETZT LIVE!**")
    lines.append(f"**{title_str}**")
    lines.append(
        f"🎮 **{game_name}** • 👁️ **{viewers_str}** Zuschauer • 🕐 Gestartet {started_val}"
    )
    lines.append(stream_url)
    return "\n".join(lines)


def build_youtube_upload_message(
    video: dict[str, Any],
    ping_text: str = "",
) -> str:
    """Baut die komplette YouTube-Ankündigung in EINER Nachricht (ohne Typ-Angabe) inkl. nativem Player-Embed."""
    ch_name = (video.get("channel_title") or "YouTube").strip()
    title_str = (video.get("title") or "Neuer Upload").strip()
    pub_ts = int(video.get("published_ts") or time.time())
    playable_url = _playable_youtube_url(video)

    lines: list[str] = []
    if ping_text.strip():
        lines.append(ping_text.strip())
    lines.append(f"### 🔴 **Neuer Upload von {ch_name}!**")
    lines.append(f"**{title_str}** • 🕐 <t:{pub_ts}:R>")
    lines.append(playable_url)
    return "\n".join(lines)


def build_twitch_offline_embed(
    tw_state: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> discord.Embed:
    channel_login = (STREAMER_CHANNEL or "twitch").lower()
    display_name = (profile or {}).get("display_name") or STREAMER_CHANNEL or "Streamer"
    avatar_url = (profile or {}).get("profile_image_url") or ""
    offline_img = (profile or {}).get("offline_image_url") or ""
    stream_url = f"https://www.twitch.tv/{channel_login}"

    duration_str = "Unbekannt"
    started_raw = tw_state.get("started_at") or ""
    if started_raw:
        try:
            dt_start = datetime.datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
            mins = max(1, int((datetime.datetime.now(datetime.timezone.utc) - dt_start).total_seconds() // 60))
            h, m = divmod(mins, 60)
            duration_str = f"{h}h {m:02d}m" if h else f"{m}m"
        except ValueError:
            pass

    games = tw_state.get("games") or []
    games_str = ", ".join(games[:4]) if games else "—"
    peak = int(tw_state.get("peak_viewers") or 0)
    last_title = tw_state.get("title") or "Stream beendet"

    embed = discord.Embed(
        title=f"⚫ {display_name} ist jetzt offline",
        url=stream_url,
        description=f"*{last_title}*\n\n💜 **Danke fürs Einschalten und Chatten!**",
        color=COLOR_ABSENCE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if avatar_url:
        embed.set_author(name=f"{display_name} • Stream beendet", url=stream_url, icon_url=avatar_url)
        embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="⏱️ Dauer", value=f"`{duration_str}`", inline=True)
    embed.add_field(name="👁️ Peak-Zuschauer", value=f"**{peak:,}**".replace(",", "."), inline=True)
    embed.add_field(name="🎮 Gespielt", value=games_str[:1024], inline=False)
    if offline_img:
        embed.set_image(url=offline_img)
    embed.set_footer(text=f"Stream-Zusammenfassung • twitch.tv/{channel_login}")
    return embed


def compute_unexcused_streaks(
    data: dict[str, Any],
    include_today: bool = False,
) -> list[dict[str, Any]]:
    """Berechnet für alle verknüpften Mods die aktuelle Serie unentschuldigter Fehl-Streams."""
    today_s = str(today_berlin())
    results: list[dict[str, Any]] = []

    for uid, twitch_name in data.get("users", {}).items():
        elig = eligible_stream_days_for_user(data, uid)
        if not include_today:
            elig = [d for d in elig if d < today_s]
        if not elig:
            results.append(
                {
                    "uid": uid,
                    "twitch_name": twitch_name,
                    "missed_unexcused": 0,
                    "last_live": "Noch kein Stream",
                }
            )
            continue

        live_days = [d for d in elig if user_was_live_in_chat(data, uid, d)]
        last_live = live_days[-1] if live_days else "Nie"

        missed = 0
        for d in reversed(elig):
            if user_was_live_in_chat(data, uid, d):
                break
            if is_user_absent(uid, d):
                # Entschuldigte Tage zählen nicht als Fehltritt, löschen aber auch keine alten unentschuldigten Fehltage
                continue
            missed += 1

        results.append(
            {
                "uid": uid,
                "twitch_name": twitch_name,
                "missed_unexcused": missed,
                "last_live": last_live,
            }
        )

    results.sort(key=lambda r: r["missed_unexcused"], reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# ALERTS COG
# ═══════════════════════════════════════════════════════════════════════════

class AlertsCog(commands.Cog, name="Alerts"):
    """Automatische Twitch-Live-, YouTube-Upload- & Mod-Inaktivitäts-Benachrichtigungen."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._offline_detected_at: float = 0.0
        self._last_live_card_edit: float = 0.0
        self._last_daily_inactivity_date: str = ""

    async def cog_load(self) -> None:
        self.twitch_live_alert_loop.start()
        self.youtube_feed_loop.start()
        self.inactivity_daily_loop.start()

    async def cog_unload(self) -> None:
        for loop in (
            self.twitch_live_alert_loop,
            self.youtube_feed_loop,
            self.inactivity_daily_loop,
        ):
            if loop.is_running():
                loop.cancel()

    def _resolve_text_channel(self, channel_id: int) -> discord.TextChannel | None:
        if not channel_id:
            return None
        ch = self.bot.get_channel(channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    # ── 1. TWITCH „JETZT LIVE“-POST LOOP (alle 60s) ───────────────────────

    @tasks.loop(seconds=60)
    async def twitch_live_alert_loop(self) -> None:
        if not STREAMER_CHANNEL:
            return
        try:
            is_live = await check_streamer_live()
            info = get_live_stream_info_cached()
            state = load_alerts_state()
            cfg = state["config"]
            tw = state["twitch_state"]
            now_ts = time.time()

            if is_live and info:
                self._offline_detected_at = 0.0
                stream_id = str(info.get("id") or "")
                viewers = int(info.get("viewer_count") or 0)
                game_name = (info.get("game_name") or "").strip()
                title_str = (info.get("title") or "").strip()

                same_stream = bool(stream_id and stream_id == tw.get("last_stream_id"))
                recent_reconnect = (
                    float(tw.get("last_offline_ts") or 0) > 0
                    and (now_ts - float(tw.get("last_offline_ts") or 0)) < 1200
                )

                if not tw.get("is_live") and not same_stream and not recent_reconnect:
                    # Brandneuer Stream gestartet -> Live-Announcement senden!
                    tw["is_live"] = True
                    tw["last_stream_id"] = stream_id
                    tw["last_announce_ts"] = now_ts
                    tw["peak_viewers"] = viewers
                    tw["games"] = [game_name] if game_name else []
                    tw["started_at"] = str(info.get("started_at") or "")
                    tw["title"] = title_str

                    target_ch = self._resolve_text_channel(int(cfg.get("live_channel_id") or 0))
                    if target_ch is not None:
                        profile = await fetch_streamer_profile()
                        ping_txt = _format_ping_text(
                            bool(cfg.get("live_ping_everyone")),
                            int(cfg.get("live_ping_role_id") or 0),
                        )
                        stream_url = f"https://www.twitch.tv/{STREAMER_CHANNEL.lower()}"
                        # Alles in EINER einzigen Nachricht (ohne embed=..., damit Discord das echte Stream-Embed anzeigt!)
                        msg = await target_ch.send(
                            content=build_twitch_live_message(info, profile, ping_text=ping_txt),
                            view=_make_link_view("Jetzt auf Twitch einschalten", stream_url, "🟣"),
                            allowed_mentions=discord.AllowedMentions(everyone=True, roles=True),
                        )
                        tw["active_message_id"] = msg.id
                        tw["active_player_message_id"] = 0
                        tw["active_channel_id"] = target_ch.id
                        self._last_live_card_edit = now_ts
                        print(f"🔴 Twitch Live-Post gesendet in #{target_ch.name} (Stream-ID: {stream_id})")

                    save_alerts_state(state)
                    return

                # Laufender Stream (oder kurzer Reconnect) -> Stats pflegen & Nachricht alle 5 Min frisch halten
                tw["is_live"] = True
                if stream_id:
                    tw["last_stream_id"] = stream_id
                tw["peak_viewers"] = max(int(tw.get("peak_viewers") or 0), viewers)
                if game_name and game_name not in tw["games"]:
                    tw["games"].append(game_name)
                if title_str:
                    tw["title"] = title_str
                save_alerts_state(state)

                if (
                    tw.get("active_message_id")
                    and tw.get("active_channel_id")
                    and (now_ts - self._last_live_card_edit) >= 300
                ):
                    active_ch = self._resolve_text_channel(int(tw["active_channel_id"]))
                    if active_ch is not None:
                        try:
                            msg = await active_ch.fetch_message(int(tw["active_message_id"]))
                            profile = await fetch_streamer_profile()
                            ping_txt = _format_ping_text(
                                bool(cfg.get("live_ping_everyone")),
                                int(cfg.get("live_ping_role_id") or 0),
                            )
                            stream_url = f"https://www.twitch.tv/{STREAMER_CHANNEL.lower()}"
                            await msg.edit(
                                content=build_twitch_live_message(info, profile, ping_text=ping_txt),
                                view=_make_link_view("Jetzt auf Twitch einschalten", stream_url, "🟣"),
                            )
                            self._last_live_card_edit = now_ts
                        except Exception:
                            pass

            else:
                # Aktuell nicht live -> 3 Minuten Karenzzeit gegen kurze OBS-Reconnects
                if tw.get("is_live"):
                    if self._offline_detected_at == 0.0:
                        self._offline_detected_at = now_ts
                        return
                    if (now_ts - self._offline_detected_at) < 180:
                        return

                    self._offline_detected_at = 0.0
                    tw["is_live"] = False
                    tw["last_offline_ts"] = now_ts

                    if tw.get("active_channel_id"):
                        active_ch = self._resolve_text_channel(int(tw["active_channel_id"]))
                        if active_ch is not None:
                            if tw.get("active_message_id"):
                                try:
                                    msg = await active_ch.fetch_message(int(tw["active_message_id"]))
                                    profile = await fetch_streamer_profile()
                                    stream_url = f"https://www.twitch.tv/{STREAMER_CHANNEL.lower()}"
                                    await msg.edit(
                                        content="",
                                        embed=build_twitch_offline_embed(tw, profile),
                                        view=_make_link_view("Kanal & VODs auf Twitch", stream_url, "📺"),
                                    )
                                except Exception:
                                    pass
                            if tw.get("active_player_message_id"):
                                try:
                                    pmsg = await active_ch.fetch_message(int(tw["active_player_message_id"]))
                                    await pmsg.delete()
                                except Exception:
                                    pass
                    tw["active_message_id"] = 0
                    tw["active_player_message_id"] = 0
                    tw["active_channel_id"] = 0
                    save_alerts_state(state)

                    # Direkt nach Stream-Ende prüfen, ob ein Mod unentschuldigt inaktiv ist
                    await self.run_inactivity_check(include_today=True, force_report=False)
        except Exception as e:
            print(f"⚠️ Fehler im Twitch-Live-Alert-Loop: {e}")

    @twitch_live_alert_loop.before_loop
    async def _before_twitch_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── 2. YOUTUBE-UPLOAD-ALERT LOOP (alle 3 Minuten) ─────────────────────

    @tasks.loop(minutes=3)
    async def youtube_feed_loop(self) -> None:
        try:
            state = load_alerts_state()
            cfg = state["config"]
            yt = state["youtube_state"]

            target_ch_id = int(cfg.get("yt_channel_id_discord") or 0)
            yt_source = (cfg.get("yt_source") or "").strip()
            if not target_ch_id or not yt_source:
                return

            uc_id = (cfg.get("yt_resolved_uc_id") or "").strip()
            if not uc_id:
                resolved_uc, resolved_title = await resolve_youtube_channel(yt_source)
                if not resolved_uc:
                    return
                uc_id = resolved_uc
                cfg["yt_resolved_uc_id"] = uc_id
                if resolved_title:
                    cfg["yt_channel_title"] = resolved_title
                save_alerts_state(state)

            entries, feed_title = await fetch_youtube_feed(uc_id)
            if not entries:
                return
            if feed_title and feed_title != cfg.get("yt_channel_title"):
                cfg["yt_channel_title"] = feed_title

            seen: list[str] = list(yt.get("seen_video_ids") or [])

            # Beim ersten Start für diesen Kanal: bestehende Videos lautlos als gesehen markieren
            if yt.get("initialized_for_uc") != uc_id:
                yt["initialized_for_uc"] = uc_id
                for e in entries:
                    if e["video_id"] not in seen:
                        seen.append(e["video_id"])
                yt["seen_video_ids"] = seen[-200:]
                save_alerts_state(state)
                print(
                    f"📺 YouTube-Feed initialisiert für {feed_title or uc_id} "
                    f"({len(entries)} bestehende Videos gemerkt)."
                )
                return

            new_videos = [e for e in entries if e["video_id"] not in seen]
            if not new_videos:
                return

            target_ch = self._resolve_text_channel(target_ch_id)
            ping_txt = _format_ping_text(
                bool(cfg.get("yt_ping_everyone")),
                int(cfg.get("yt_ping_role_id") or 0),
            )

            for vid in new_videos:
                seen.append(vid["video_id"])
                if target_ch is not None:
                    # Alles in EINER einzigen Nachricht (ohne Typ-Angabe & mit direkt abspielbarem YouTube-Embed)
                    await target_ch.send(
                        content=build_youtube_upload_message(vid, ping_text=ping_txt),
                        view=_make_link_view("Auf YouTube anschauen", vid["url"], "▶️"),
                        allowed_mentions=discord.AllowedMentions(everyone=True, roles=True),
                    )
                    print(f"🔴 YouTube-Upload angekündigt: {vid['title']} ({vid['video_id']})")

            yt["seen_video_ids"] = seen[-200:]
            save_alerts_state(state)
        except Exception as e:
            print(f"⚠️ Fehler im YouTube-Feed-Loop: {e}")

    @youtube_feed_loop.before_loop
    async def _before_youtube_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── 3. UNENTSCHULDIGTE MOD-INAKTIVITÄTS-WARNUNG ───────────────────────

    @tasks.loop(minutes=15)
    async def inactivity_daily_loop(self) -> None:
        now_de = now_berlin()
        today_s = str(now_de.date())
        if now_de.hour == 22 and self._last_daily_inactivity_date != today_s:
            self._last_daily_inactivity_date = today_s
            tw_live = bool(load_alerts_state()["twitch_state"].get("is_live"))
            await self.run_inactivity_check(include_today=not tw_live, force_report=False)

    @inactivity_daily_loop.before_loop
    async def _before_inactivity_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def run_inactivity_check(
        self,
        include_today: bool = False,
        force_report: bool = False,
    ) -> discord.Embed | None:
        """Prüft alle Mods auf unentschuldigte Fehlserien und warnt Admins bei Überschreitung."""
        state = load_alerts_state()
        cfg = state["config"]
        if not cfg.get("inactivity_enabled", True) and not force_report:
            return None

        threshold = max(1, int(cfg.get("inactivity_threshold") or 5))
        warn_ch_id = (
            int(cfg.get("inactivity_channel_id") or 0)
            or REVIEW_CHANNEL_ID
            or LOG_CHANNEL_ID
        )
        warned_map: dict[str, int] = state["inactivity_state"].setdefault("warned_streaks", {})

        data = load_data()
        streaks = compute_unexcused_streaks(data, include_today=include_today)
        now_de = now_berlin()

        triggered: list[dict[str, Any]] = []
        changed = False

        for row in streaks:
            uid = row["uid"]
            missed = row["missed_unexcused"]
            prev_warned = int(warned_map.get(uid, 0) or 0)

            if missed < threshold:
                if prev_warned != 0:
                    warned_map[uid] = 0
                    changed = True
                continue

            milestone = (missed // threshold) * threshold
            if force_report or milestone > prev_warned:
                triggered.append(row)
                if not force_report and milestone > prev_warned:
                    warned_map[uid] = milestone
                    changed = True

        if changed:
            save_alerts_state(state)

        if not triggered:
            return None

        lines: list[str] = []
        for row in triggered:
            uid = row["uid"]
            missed = row["missed_unexcused"]
            last_str = fmt_iso_de(row["last_live"]) if row["last_live"] not in ("Nie", "Noch kein Stream") else row["last_live"]
            m_stats = get_month_activity_stats(data, uid, now_de.year, now_de.month)
            grade_ltr, grade_em, _ = get_activity_grade(m_stats["pct"])
            comp = get_user_compare_stats(data, uid)

            severity = "🚨" if missed >= threshold * 2 else "⚠️"
            lines.append(
                f"{severity} <@{uid}> (`{row['twitch_name']}`)\n"
                f"╰ **{missed} Streams in Folge** ohne `/absent` verpasst • "
                f"Zuletzt im Chat: `{last_str}`\n"
                f"╰ Monatsquote: **{m_stats['pct']:.0f}%** ({grade_em} `{grade_ltr}`) • "
                f"Gesamt-Score: `{comp['score']}/100`"
            )

        embed = discord.Embed(
            title="⚠️ Mod-Inaktivitäts-Warnung (Unentschuldigt)",
            description=(
                f"Folgende Moderatoren haben mindestens **{threshold} Streams am Stück** "
                f"ohne eingetragene `/absent`-Abmeldung gefehlt:\n\n"
                + "\n\n".join(lines)
            )[:4000],
            color=COLOR_ERROR if any(r["missed_unexcused"] >= threshold * 2 for r in triggered) else COLOR_WARNING,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(
            text=f"Schwelle: {threshold} Streams ohne /absent • Einstellbar per /setinactivity"
        )

        if not force_report:
            target_ch = self._resolve_text_channel(warn_ch_id)
            if target_ch is not None:
                await target_ch.send(embed=embed)
                print(f"⚠️ Mod-Inaktivitäts-Warnung gesendet ({len(triggered)} Mods betroffen).")

        return embed

    # ═══════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS (Admin-Konfiguration & Sofort-Tests)
    # ═══════════════════════════════════════════════════════════════════════

    @app_commands.command(
        name="alerts",
        description="🔔 Zeigt den Status von Twitch Live-Post, YouTube-Uploads & Inaktivitäts-Warnung",
    )
    @is_admin_user()
    async def cmd_alerts(self, interaction: discord.Interaction) -> None:
        state = load_alerts_state()
        cfg = state["config"]
        tw = state["twitch_state"]
        yt = state["youtube_state"]

        live_ch = f"<#{cfg['live_channel_id']}>" if cfg.get("live_channel_id") else "❌ *Nicht gesetzt* (`/setlivealert`)"
        live_ping = (
            _format_ping_text(bool(cfg.get("live_ping_everyone")), int(cfg.get("live_ping_role_id") or 0))
            or "Kein Ping"
        )

        yt_ch = f"<#{cfg['yt_channel_id_discord']}>" if cfg.get("yt_channel_id_discord") else "❌ *Nicht gesetzt* (`/setyoutubealert`)"
        yt_src = cfg.get("yt_channel_title") or cfg.get("yt_source") or "Nicht gesetzt"
        yt_uc = cfg.get("yt_resolved_uc_id") or "—"
        yt_ping = (
            _format_ping_text(bool(cfg.get("yt_ping_everyone")), int(cfg.get("yt_ping_role_id") or 0))
            or "Kein Ping"
        )

        ina_ch_id = int(cfg.get("inactivity_channel_id") or 0) or REVIEW_CHANNEL_ID or LOG_CHANNEL_ID
        ina_ch = f"<#{ina_ch_id}>" if ina_ch_id else "❌ *Kein Zielkanal*"
        ina_status = "✅ Aktiv" if cfg.get("inactivity_enabled", True) else "⏸️ Pausiert"

        embed = discord.Embed(
            title="🔔 Automatische Benachrichtigungen & Warnungen",
            color=COLOR_INFO,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name="🟣 Twitch „JETZT LIVE“-Post",
            value=(
                f"**Discord-Kanal:** {live_ch}\n"
                f"**Streamer:** `{STREAMER_CHANNEL or 'Nicht in .env gesetzt'}` "
                f"({'🟢 LIVE' if tw.get('is_live') else '⚫ Offline'})\n"
                f"**Ping:** {live_ping}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔴 YouTube-Upload-Ankündigung",
            value=(
                f"**Discord-Kanal:** {yt_ch}\n"
                f"**YouTube-Kanal:** `{yt_src}` (`{yt_uc}`)\n"
                f"**Bekannte Uploads:** `{len(yt.get('seen_video_ids') or [])}` • **Ping:** {yt_ping}"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚠️ Unentschuldigte Mod-Inaktivität",
            value=(
                f"**Status:** {ina_status}\n"
                f"**Ziel-Kanal:** {ina_ch}\n"
                f"**Warnschwelle:** Ab **{cfg.get('inactivity_threshold', 5)} Streams** am Stück ohne `/absent`"
            ),
            inline=False,
        )
        embed.set_footer(text="Befehle: /setlivealert • /setyoutubealert • /setinactivity • /testalert")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setlivealert",
        description="🟣 Richtet den automatischen Twitch „JETZT LIVE“-Post ein (nur Admins)",
    )
    @app_commands.describe(
        kanal="Discord-Kanal für die Live-Ankündigung (leer lassen zum Deaktivieren)",
        ping_rolle="Optional: Rolle die beim Livegehen gepingt werden soll",
        ping_everyone="Optional: @everyone beim Livegehen pingen?",
    )
    @is_admin_user()
    async def cmd_setlivealert(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel | None = None,
        ping_rolle: discord.Role | None = None,
        ping_everyone: bool = False,
    ) -> None:
        state = load_alerts_state()
        cfg = state["config"]
        cfg["live_channel_id"] = kanal.id if kanal else 0
        cfg["live_ping_role_id"] = ping_rolle.id if ping_rolle else 0
        cfg["live_ping_everyone"] = bool(ping_everyone)
        save_alerts_state(state)

        if not kanal:
            await interaction.response.send_message(
                "⏸️ Automatischer **Twitch Live-Post** wurde deaktiviert.", ephemeral=True
            )
            return

        ping_str = _format_ping_text(ping_everyone, cfg["live_ping_role_id"]) or "Kein Ping"
        embed = discord.Embed(
            title="🟣 Twitch Live-Alert gespeichert!",
            description=(
                f"Sobald **`{STREAMER_CHANNEL or 'Streamer'}`** auf Twitch live geht, postet der Bot automatisch "
                f"in {kanal.mention} (inkl. Live-Update & Offline-Zusammenfassung)."
            ),
            color=COLOR_TWITCH,
        )
        embed.add_field(name="📍 Kanal", value=kanal.mention, inline=True)
        embed.add_field(name="🔔 Ping", value=ping_str, inline=True)
        embed.set_footer(text="Tipp: Nutze /testalert zum sofortigen Testen des Designs!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setyoutubealert",
        description="🔴 Richtet automatische YouTube-Upload-Ankündigungen ein (nur Admins)",
    )
    @app_commands.describe(
        kanal="Discord-Kanal für neue YouTube-Uploads (leer = deaktivieren)",
        youtube_kanal="@Handle, Kanal-URL oder UC-ID (z.B. @Chaostobi oder https://youtube.com/@...)",
        ping_rolle="Optional: Rolle die bei neuen Uploads gepingt wird",
        ping_everyone="Optional: @everyone bei neuen Uploads pingen?",
    )
    @is_admin_user()
    async def cmd_setyoutubealert(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel | None = None,
        youtube_kanal: str | None = None,
        ping_rolle: discord.Role | None = None,
        ping_everyone: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        state = load_alerts_state()
        cfg = state["config"]
        yt = state["youtube_state"]

        if not kanal:
            cfg["yt_channel_id_discord"] = 0
            save_alerts_state(state)
            await interaction.followup.send(
                "⏸️ Automatische **YouTube-Upload-Ankündigung** wurde deaktiviert.",
                ephemeral=True,
            )
            return

        src = (youtube_kanal or cfg.get("yt_source") or "").strip()
        if not src:
            await interaction.followup.send(
                "❌ Bitte gib beim ersten Einrichten auch `youtube_kanal` an (z. B. `@Handle`, Kanal-Link oder `UC...`).",
                ephemeral=True,
            )
            return

        uc_id, ch_title = await resolve_youtube_channel(src)
        if not uc_id:
            await interaction.followup.send(
                f"❌ Konnte keinen YouTube-Kanal für `{src}` auflösen.\n"
                f"💡 **Tipp:** Übergib direkt die Kanal-URL (`https://www.youtube.com/@...`) oder die `UC...`-Kanal-ID.",
                ephemeral=True,
            )
            return

        # Bestehende Uploads direkt initialisieren, damit nur zukünftige Uploads gepostet werden
        entries, feed_title = await fetch_youtube_feed(uc_id)
        final_title = feed_title or ch_title or src

        cfg["yt_channel_id_discord"] = kanal.id
        cfg["yt_source"] = src
        cfg["yt_resolved_uc_id"] = uc_id
        cfg["yt_channel_title"] = final_title
        cfg["yt_ping_role_id"] = ping_rolle.id if ping_rolle else 0
        cfg["yt_ping_everyone"] = bool(ping_everyone)

        yt["initialized_for_uc"] = uc_id
        yt["seen_video_ids"] = [e["video_id"] for e in entries][-200:]
        save_alerts_state(state)

        ping_str = _format_ping_text(ping_everyone, cfg["yt_ping_role_id"]) or "Kein Ping"
        embed = discord.Embed(
            title="🔴 YouTube Upload-Alert aktiviert!",
            description=(
                f"Neue Uploads von **{final_title}** werden ab sofort automatisch "
                f"in {kanal.mention} angekündigt."
            ),
            color=COLOR_YOUTUBE,
        )
        embed.add_field(name="📺 YouTube-Kanal", value=f"**{final_title}**\n`{uc_id}`", inline=True)
        embed.add_field(name="📍 Discord-Kanal", value=kanal.mention, inline=True)
        embed.add_field(name="🔔 Ping", value=ping_str, inline=True)
        embed.set_footer(
            text=f"{len(entries)} vorhandene Uploads gemerkt (kein Altlasten-Spam) • /testalert für Vorschau"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setinactivity",
        description="⚠️ Konfiguriert die automatische Mod-Inaktivitäts-Warnung (nur Admins)",
    )
    @app_commands.describe(
        streams_in_folge="Ab wie vielen unentschuldigt verpassten Streams am Stück gewarnt wird (Standard: 5)",
        kanal="Ziel-Kanal für die Admin-Warnung (Standard: Review- oder Log-Kanal)",
        aktiv="Automatische Warnung aktivieren oder pausieren",
    )
    @is_admin_user()
    async def cmd_setinactivity(
        self,
        interaction: discord.Interaction,
        streams_in_folge: app_commands.Range[int, 2, 30] = 5,
        kanal: discord.TextChannel | None = None,
        aktiv: bool = True,
    ) -> None:
        state = load_alerts_state()
        cfg = state["config"]
        cfg["inactivity_threshold"] = int(streams_in_folge)
        if kanal is not None:
            cfg["inactivity_channel_id"] = kanal.id
        cfg["inactivity_enabled"] = bool(aktiv)
        save_alerts_state(state)

        eff_ch = int(cfg.get("inactivity_channel_id") or 0) or REVIEW_CHANNEL_ID or LOG_CHANNEL_ID
        ch_str = f"<#{eff_ch}>" if eff_ch else "Nicht gesetzt"

        embed = discord.Embed(
            title="⚠️ Inaktivitäts-Warnung aktualisiert",
            color=COLOR_SUCCESS if aktiv else COLOR_ABSENCE,
        )
        embed.add_field(name="📡 Status", value="✅ Aktiv" if aktiv else "⏸️ Pausiert", inline=True)
        embed.add_field(name="🎯 Schwelle", value=f"**{streams_in_folge} Streams** ohne `/absent`", inline=True)
        embed.add_field(name="📍 Warn-Kanal", value=ch_str, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="testalert",
        description="🧪 Sendet eine Vorschau für Twitch-Live, YouTube-Upload oder Inaktivitäts-Warnung",
    )
    @app_commands.describe(
        typ="Welchen Alert möchtest du testen?",
        im_kanal_posten="True = 1:1 als eine Nachricht im Kanal posten (ohne Ping)",
    )
    @app_commands.choices(
        typ=[
            app_commands.Choice(name="🟣 Twitch „JETZT LIVE“-Post", value="twitch"),
            app_commands.Choice(name="🔴 YouTube-Upload-Ankündigung (neuester Upload)", value="youtube"),
            app_commands.Choice(name="⚠️ Mod-Inaktivitäts-Prüfung (aktueller Stand)", value="inactivity"),
        ]
    )
    @is_admin_user()
    async def cmd_testalert(
        self,
        interaction: discord.Interaction,
        typ: str,
        im_kanal_posten: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        state = load_alerts_state()
        cfg = state["config"]

        if typ == "twitch":
            await check_streamer_live()
            info = get_live_stream_info_cached()
            profile = await fetch_streamer_profile()
            fake_or_real = info or {
                "user_login": STREAMER_CHANNEL or "streamer",
                "user_name": (profile or {}).get("display_name") or STREAMER_CHANNEL or "Streamer",
                "title": "🔥 Beispiel-Stream: Modcheck Live-Alert Vorschau!",
                "game_name": "Just Chatting",
                "viewer_count": 42,
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "thumbnail_url": "",
            }
            stream_url = f"https://www.twitch.tv/{(STREAMER_CHANNEL or 'twitch').lower()}"
            msg_text = build_twitch_live_message(fake_or_real, profile)
            if im_kanal_posten and isinstance(interaction.channel, discord.abc.Messageable):
                await interaction.channel.send(
                    content=msg_text,
                    view=_make_link_view("Jetzt auf Twitch einschalten", stream_url, "🟣"),
                )
                await interaction.followup.send(
                    "✅ **Twitch-Test-Post** wurde als eine Nachricht im Kanal gesendet *(ohne Ping)*!",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    content=msg_text,
                    view=_make_link_view("Jetzt auf Twitch einschalten", stream_url, "🟣"),
                    ephemeral=True,
                )
            return

        if typ == "youtube":
            uc_id = (cfg.get("yt_resolved_uc_id") or "").strip()
            if not uc_id and cfg.get("yt_source"):
                uc_id, _ = await resolve_youtube_channel(cfg["yt_source"])
            if not uc_id:
                await interaction.followup.send(
                    "❌ Noch kein YouTube-Kanal eingerichtet! Nutze zuerst `/setyoutubealert`.",
                    ephemeral=True,
                )
                return
            entries, _ = await fetch_youtube_feed(uc_id)
            if not entries:
                await interaction.followup.send(
                    f"❌ Der Feed für `{uc_id}` enthält aktuell keine öffentlichen Uploads.",
                    ephemeral=True,
                )
                return
            latest = entries[-1]
            msg_text = build_youtube_upload_message(latest)
            if im_kanal_posten and isinstance(interaction.channel, discord.abc.Messageable):
                await interaction.channel.send(
                    content=msg_text,
                    view=_make_link_view("Auf YouTube anschauen", latest["url"], "▶️"),
                )
                await interaction.followup.send(
                    "✅ **YouTube-Test-Post** wurde als eine Nachricht im Kanal gesendet *(ohne Ping)*!",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    content=msg_text,
                    view=_make_link_view("Auf YouTube anschauen", latest["url"], "▶️"),
                    ephemeral=True,
                )
            return

        # Inaktivitäts-Check Vorschau
        embed = await self.run_inactivity_check(include_today=False, force_report=True)
        if embed is None:
            threshold = cfg.get("inactivity_threshold", 5)
            await interaction.followup.send(
                f"✅ **Alles im grünen Bereich!** Aktuell hat kein einziger Mod "
                f"**{threshold}+ Streams in Folge** unentschuldigt verpasst.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                content="🧪 **Aktuelle Auswertung der unentschuldigten Fehlserien (Vorschau):**",
                embed=embed,
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AlertsCog(bot))
