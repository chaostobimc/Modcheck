"""Music & Radio Cog: YouTube, Spotify, Live-Radio, Autoplay, Loop & Interaktive Controls."""
from __future__ import annotations

import asyncio
from collections import deque
import os
import random
import re
from typing import Any
from urllib.parse import quote
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.config import (
    BASE_DIR,
    COLOR_ERROR,
    COLOR_MUSIC,
    COLOR_RADIO,
    COLOR_SPOTIFY,
    MUSIC_ENABLED,
    RADIO_STATIONS,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    STREAMER_CHANNEL,
    YOUTUBE_COOKIES_FILE,
    YOUTUBE_POT_PROVIDER_URL,
    runtime_settings,
)

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None  # type: ignore
    YTDLP_AVAILABLE = False

try:
    from spotdl import Spotdl
    SPOTDL_AVAILABLE = bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)
except Exception:
    Spotdl = None  # type: ignore
    SPOTDL_AVAILABLE = False

_spotdl_instance = None


def _get_spotdl():
    global _spotdl_instance
    if _spotdl_instance is None and SPOTDL_AVAILABLE and Spotdl is not None:
        try:
            _spotdl_instance = Spotdl(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                no_cache=True,
            )
        except Exception as e:
            print(f"⚠️ [Spotify] Spotdl Init-Fehler: {e}")
    return _spotdl_instance


_YT_HEADERS = (
    "Referer: https://www.youtube.com/\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36\r\n"
)

FFMPEG_OPTIONS = {
    "before_options": (
        "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        f'-headers "{_YT_HEADERS}"'
    ),
    "options": "-vn",
}

RADIO_FFMPEG_OPTIONS = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YDL_TIMEOUTS = {
    "socket_timeout": 20,
    "request_timeout": 20,
    "extractor_retries": 2,
}


def _cookie_and_pot_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if YOUTUBE_COOKIES_FILE:
        cpath = (
            YOUTUBE_COOKIES_FILE
            if os.path.isabs(YOUTUBE_COOKIES_FILE)
            else os.path.join(BASE_DIR, YOUTUBE_COOKIES_FILE)
        )
        if os.path.isfile(cpath) and os.path.getsize(cpath) > 0:
            opts["cookiefile"] = cpath
    if YOUTUBE_POT_PROVIDER_URL:
        opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {
            "base_url": [YOUTUBE_POT_PROVIDER_URL]
        }
    return opts


YTDL_SEARCH_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": True,
    "cachedir": False,
    **YDL_TIMEOUTS,
}

YTDL_PLAYLIST_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extract_flat": True,
    "playlistend": 50,
    "cachedir": False,
    **YDL_TIMEOUTS,
}

YDL_PLAYER_CLIENT_FALLBACKS: list[dict[str, Any] | None] = [
    {"extractor_args": {"youtube": {"player_client": ["tv"]}}},
    {"extractor_args": {"youtube": {"player_client": ["android"]}}},
    {"extractor_args": {"youtube": {"player_client": ["ios"]}}},
    {"extractor_args": {"youtube": {"player_client": ["web"]}}},
    None,
]


class SongInfo:
    __slots__ = ("title", "url", "webpage_url", "duration", "thumbnail", "requester", "is_radio")

    def __init__(
        self,
        title: str,
        url: str,
        webpage_url: str,
        duration: int,
        thumbnail: str,
        requester: discord.abc.User | None,
        is_radio: bool = False,
    ) -> None:
        self.title = title
        self.url = url
        self.webpage_url = webpage_url
        self.duration = int(duration) if duration else 0
        self.thumbnail = thumbnail or ""
        self.requester = requester
        self.is_radio = is_radio

    @property
    def duration_str(self) -> str:
        if self.is_radio or self.duration <= 0:
            return "🔴 LIVE"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "webpage_url": self.webpage_url,
            "duration": self.duration,
            "duration_str": self.duration_str,
            "thumbnail": self.thumbnail,
            "requester": getattr(self.requester, "display_name", "Autoplay") if self.requester else "Autoplay",
            "is_radio": self.is_radio,
        }


class GuildMusicState:
    def __init__(self, guild_id: int, bot: commands.Bot) -> None:
        self.guild_id = guild_id
        self.bot = bot
        self.queue: deque[SongInfo] = deque()
        self.current: SongInfo | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.last_np_message: discord.Message | None = None
        self.autoplay: bool = False
        self.loop_mode: str = "off"  # "off" | "track" | "queue"
        self.history: deque[str] = deque(maxlen=15)
        self.is_loading: bool = False
        self._idle_seconds: int = 0
        self._empty_seconds: int = 0
        self._stale_current_seconds: int = 0
        self._lock = asyncio.Lock()
        self.consecutive_failures: int = 0

    async def restore_default_presence(self) -> None:
        try:
            default_act = discord.Activity(
                type=discord.ActivityType.watching,
                name=f"twitch.tv/{STREAMER_CHANNEL} • /help" if STREAMER_CHANNEL else "/help",
            )
            await self.bot.change_presence(activity=default_act)
        except Exception:
            pass

    async def clear_np_buttons_silently(self) -> None:
        msg = self.last_np_message
        self.last_np_message = None
        if msg is not None:
            try:
                await msg.edit(view=None)
            except Exception:
                pass

    async def cleanup(self, disconnect_voice: bool = True) -> None:
        """Trennt die Voice-Verbindung zuverlässig & komplett lautlos (ohne Chat-Nachricht)."""
        self.queue.clear()
        self.current = None
        self.loop_mode = "off"
        self.is_loading = False
        self._idle_seconds = 0
        self._empty_seconds = 0
        self._stale_current_seconds = 0
        self.history.clear()
        await self.clear_np_buttons_silently()

        guild = self.bot.get_guild(self.guild_id)
        seen_vcs: list[discord.VoiceClient] = []
        for candidate in (self.voice_client, guild.voice_client if guild else None):
            if candidate is not None and candidate not in seen_vcs:
                seen_vcs.append(candidate)

        for vc in seen_vcs:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass
            if disconnect_voice:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass

        # Falls der Bot auf dem Discord-Gateway noch im Kanal hängt (z. B. nach Neustart / Voice-Drop)
        if disconnect_voice and guild is not None:
            try:
                me = guild.me
                if me is not None and me.voice is not None and me.voice.channel is not None:
                    await guild.change_voice_state(channel=None)
            except Exception:
                pass

        self.voice_client = None
        await self.restore_default_presence()

    def reset_idle(self) -> None:
        self._idle_seconds = 0
        self._stale_current_seconds = 0

    def increment_idle(self, seconds: int = 1) -> None:
        self._idle_seconds += seconds

    @property
    def is_idle_timeout(self) -> bool:
        timeout = int(runtime_settings.get("music_idle_timeout") or 60)
        return self._idle_seconds >= max(5, timeout)


music_states: dict[int, GuildMusicState] = {}


def get_music_state(guild_id: int, bot: commands.Bot) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id, bot)
    return music_states[guild_id]


def _count_humans_in_channel(
    guild: discord.Guild | None,
    channel: discord.abc.Snowflake | None,
    bot_id: int,
) -> int:
    """Zählt echte (nicht-Bot) Nutzer im Voice-Kanal über Gateway voice_states + Member-Liste."""
    if guild is None or channel is None:
        return 0
    fresh_ch = guild.get_channel(channel.id) or channel
    vstates = getattr(fresh_ch, "voice_states", None)
    if isinstance(vstates, dict) and vstates:
        humans = 0
        for uid in vstates:
            if uid == bot_id:
                continue
            member = guild.get_member(uid)
            if member is not None and member.bot:
                continue
            humans += 1
        return humans
    return sum(
        1
        for m in getattr(fresh_ch, "members", [])
        if not m.bot and m.id != bot_id
    )


async def ensure_voice_connected(
    state: GuildMusicState,
    target_channel: discord.VoiceChannel | discord.StageChannel,
) -> discord.VoiceClient:
    """Verbindet oder verschiebt den VoiceClient sauber (inkl. Sync mit guild.voice_client)."""
    guild = target_channel.guild
    existing: discord.VoiceClient | None = guild.voice_client or state.voice_client
    if existing is not None:
        if existing.is_connected():
            if existing.channel is None or existing.channel.id != target_channel.id:
                await existing.move_to(target_channel)
            state.voice_client = existing
            state.reset_idle()
            return existing
        try:
            await existing.disconnect(force=True)
        except Exception:
            pass

    vc = await target_channel.connect(self_deaf=True)
    state.voice_client = vc
    state.reset_idle()
    return vc


# ── YouTube Helpers ────────────────────────────────────────────────────────

def _is_url(q: str) -> bool:
    return q.startswith(("http://", "https://"))


def _is_playlist_url(q: str) -> bool:
    return _is_url(q) and ("list=" in q or "/playlist" in q)


def _entry_thumbnail(entry: dict[str, Any]) -> str:
    if entry.get("thumbnail"):
        return str(entry["thumbnail"])
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        return str(thumbs[-1].get("url", ""))
    vid = entry.get("id")
    if vid and len(str(vid)) == 11:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return ""


def _get_reference_url(entry: dict[str, Any], fallback_query: str = "") -> str:
    for key in ("webpage_url", "original_url", "url"):
        val = entry.get(key)
        if isinstance(val, str) and val.startswith("http") and "googlevideo.com" not in val:
            return val
    vid = entry.get("id")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return fallback_query if _is_url(fallback_query) else ""


async def ytdl_extract(query: str, options: dict[str, Any]) -> dict[str, Any] | None:
    if not YTDLP_AVAILABLE or not runtime_settings.get("music_enabled", MUSIC_ENABLED):
        return None
    opts = {**YDL_TIMEOUTS, **_cookie_and_pot_opts(), **options}
    loop = asyncio.get_running_loop()

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        print(f"⚠️ [Music] yt-dlp Extract-Fehler: {e}")
        return None


async def search_tracks(query: str, limit: int = 5) -> list[dict[str, Any]]:
    q = query if _is_url(query) else f"ytsearch{limit}:{query}"
    info = await ytdl_extract(q, YTDL_SEARCH_OPTIONS)
    if not info:
        return []
    if "entries" in info:
        return [e for e in (info["entries"] or []) if e]
    return [info]


async def extract_playlist(url: str) -> list[dict[str, Any]]:
    info = await ytdl_extract(url, YTDL_PLAYLIST_OPTIONS)
    if not info or "entries" not in info:
        return []
    return [e for e in (info["entries"] or []) if e]


def _resolve_with_client(webpage_url: str, extra_opts: dict[str, Any] | None = None) -> tuple[str | None, dict | None]:
    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "source_address": "0.0.0.0",
        "extract_flat": False,
        "nocheckcertificate": True,
        "cachedir": False,
        **YDL_TIMEOUTS,
        **_cookie_and_pot_opts(),
    }
    if extra_opts:
        merged_ext = dict(ydl_opts.get("extractor_args", {}))
        for k, v in extra_opts.get("extractor_args", {}).items():
            merged_ext[k] = v
        ydl_opts.update(extra_opts)
        if merged_ext:
            ydl_opts["extractor_args"] = merged_ext

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
    if not info:
        return None, None
    if info.get("url"):
        return info["url"], info
    entries = info.get("entries") or []
    if entries and entries[0] and entries[0].get("url"):
        return entries[0]["url"], entries[0]
    return None, info


async def _find_autoplay_song(state: GuildMusicState, base_song: SongInfo) -> SongInfo | None:
    clean_title = re.sub(r"\(.*?\)|\[.*?\]", "", base_song.title).strip()
    results = await search_tracks(f"{clean_title} official audio", limit=8)
    for entry in results:
        ref = _get_reference_url(entry)
        if not ref or ref in state.history or ref == base_song.webpage_url:
            continue
        return SongInfo(
            title=entry.get("title", "Autoplay Track"),
            url="",
            webpage_url=ref,
            duration=int(entry.get("duration") or 0),
            thumbnail=_entry_thumbnail(entry),
            requester=None,
        )
    return None


# ── Spotify Helpers ────────────────────────────────────────────────────────

def is_spotify_url(query: str) -> bool:
    return "open.spotify.com/" in query or query.startswith("spotify:")


async def fetch_spotify_tracks(url: str) -> list[dict[str, Any]]:
    spotdl_client = _get_spotdl()
    loop = asyncio.get_running_loop()
    if spotdl_client is not None:
        try:
            songs = await loop.run_in_executor(None, spotdl_client.search, [url])
            out = []
            for s in (songs or [])[:50]:
                artist = getattr(s, "artist", "") or ""
                name = getattr(s, "name", "") or "Unbekannt"
                full = f"{artist} - {name}".strip(" -")
                out.append({
                    "title": full,
                    "search_query": full,
                    "duration": int(getattr(s, "duration", 0) or 0),
                    "thumbnail": getattr(s, "cover_url", "") or "",
                })
            if out:
                return out
        except Exception as e:
            print(f"⚠️ [Spotify] spotdl Fallback auf Web-Embed: {e}")

    # Leichter oEmbed / HTML Fallback ohne API-Keys
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://open.spotify.com/oembed",
                params={"url": url},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title", "").strip()
                    thumb = data.get("thumbnail_url", "")
                    if title:
                        return [{
                            "title": title,
                            "search_query": title,
                            "duration": 0,
                            "thumbnail": thumb,
                        }]
    except Exception as e:
        print(f"⚠️ [Spotify] oEmbed Fehler: {e}")
    return []


# ── Radio Helpers ──────────────────────────────────────────────────────────

RADIO_API_HOSTS = [
    "https://de1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
]


async def search_radio_stations_list(query: str, limit: int = 10) -> list[dict[str, Any]]:
    encoded = quote(query.strip())
    headers = {"User-Agent": "ModcheckDiscordBot/2.0"}
    for host in RADIO_API_HOSTS:
        url = (
            f"{host}/json/stations/byname/{encoded}"
            f"?limit={limit}&hidebroken=true&order=clickcount&reverse=true"
        )
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        raw = await resp.json()
                        stations = []
                        seen = set()
                        for s in raw:
                            stream_url = (s.get("url_resolved") or s.get("url") or "").strip()
                            name = (s.get("name") or "").strip()
                            if not stream_url or not name or name.lower() in seen:
                                continue
                            seen.add(name.lower())
                            stations.append({
                                "name": name[:80],
                                "url": stream_url,
                                "country": s.get("country", ""),
                                "bitrate": int(s.get("bitrate") or 0),
                                "favicon": s.get("favicon", ""),
                            })
                        return stations[:limit]
        except Exception:
            continue
    return []


# ── Playback Engine ────────────────────────────────────────────────────────

async def _play_song_robust(
    state: GuildMusicState, song: SongInfo, after_cb
) -> bool:
    if not state.voice_client or not state.voice_client.is_connected():
        return False

    if song.is_radio and song.url:
        try:
            source = discord.FFmpegPCMAudio(song.url, **RADIO_FFMPEG_OPTIONS)
            state.voice_client.play(source, after=after_cb)
            await asyncio.sleep(1.5)
            return state.voice_client.is_playing() or state.voice_client.is_paused()
        except Exception as e:
            print(f"❌ [Radio] Stream-Fehler: {e}")
            return False

    loop = asyncio.get_running_loop()
    for extra_opts in YDL_PLAYER_CLIENT_FALLBACKS:
        if not state.voice_client or not state.voice_client.is_connected():
            return False
        try:
            stream_url, info = await loop.run_in_executor(
                None, _resolve_with_client, song.webpage_url, extra_opts
            )
        except Exception:
            continue
        if not stream_url:
            continue

        if info:
            if not song.duration and info.get("duration"):
                song.duration = int(info["duration"])
            if not song.thumbnail:
                song.thumbnail = _entry_thumbnail(info)

        early_error: list[Exception] = []
        done_flag = asyncio.Event()

        def _wrapped_after(err: Exception | None) -> None:
            if not done_flag.is_set():
                if err:
                    early_error.append(err)
                done_flag.set()
                return
            after_cb(err)

        try:
            source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
            state.voice_client.play(source, after=_wrapped_after)
        except Exception:
            continue

        try:
            await asyncio.wait_for(done_flag.wait(), timeout=2.2)
            # Innerhalb von 2.2s beendet -> meist 403 Forbidden -> nächsten Client testen!
            if song.duration == 0 or song.duration > 4 or early_error:
                continue
            # Sehr kurzer Soundeffekt (<= 4s) regulär beendet -> Callback nachreichen!
            song.url = stream_url
            after_cb(None)
            return True
        except asyncio.TimeoutError:
            done_flag.set()
            song.url = stream_url
            return True

    return False


async def play_next(state: GuildMusicState, text_channel: discord.abc.Messageable | None = None) -> None:
    if text_channel is not None:
        state.text_channel = text_channel
    channel = state.text_channel

    async with state._lock:
        guild = state.bot.get_guild(state.guild_id)
        vc = state.voice_client or (guild.voice_client if guild else None)
        if vc is not None and state.voice_client is not vc:
            state.voice_client = vc

        if not vc or not vc.is_connected():
            await state.cleanup()
            return

        state.is_loading = True
        try:
            prev_song = state.current
            next_song: SongInfo | None = None

            if prev_song and not prev_song.is_radio:
                if state.loop_mode == "track":
                    next_song = prev_song
                elif state.loop_mode == "queue":
                    state.queue.append(prev_song)

            if next_song is None and state.queue:
                next_song = state.queue.popleft()

            if next_song is None and state.autoplay and prev_song and not prev_song.is_radio:
                next_song = await _find_autoplay_song(state, prev_song)

            if next_song is None:
                state.current = None
                await state.clear_np_buttons_silently()
                await state.restore_default_presence()
                # Falls inzwischen auch niemand mehr im Kanal ist -> sofort lautlos leaven!
                bot_id = state.bot.user.id if state.bot.user else 0
                if guild and _count_humans_in_channel(guild, vc.channel, bot_id) == 0:
                    await state.cleanup()
                return

            state.current = next_song
            state.reset_idle()
            if next_song.webpage_url:
                state.history.append(next_song.webpage_url)

            bot_loop = state.bot.loop

            def _after_playback(err: Exception | None) -> None:
                if err:
                    print(f"⚠️ [Music] Playback beendet mit Fehler: {err}")
                asyncio.run_coroutine_threadsafe(play_next(state), bot_loop)

            ok = await _play_song_robust(state, next_song, _after_playback)
        finally:
            state.is_loading = False
        if not ok:
            state.consecutive_failures += 1
            state.current = None
            if channel and state.consecutive_failures <= 3:
                try:
                    await channel.send(
                        f"⚠️ Konnte **{next_song.title}** nicht abspielen – überspringe...",
                        delete_after=8,
                    )
                except Exception:
                    pass
            if state.consecutive_failures >= 5:
                state.consecutive_failures = 0
                if channel:
                    try:
                        await channel.send(
                            "❌ Zu viele fehlerhafte Streams in Folge – Wiedergabe pausiert.",
                            delete_after=15,
                        )
                    except Exception:
                        pass
                return
            asyncio.create_task(play_next(state, channel))
            return

        state.consecutive_failures = 0

        try:
            await state.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=next_song.title[:120],
                )
            )
        except Exception:
            pass

        if channel and state.loop_mode != "track":
            await state.clear_np_buttons_silently()
            embed = build_now_playing_embed(state)
            view = MusicPlayerControlView(state)
            try:
                state.last_np_message = await channel.send(embed=embed, view=view)
            except Exception:
                pass


def build_now_playing_embed(state: GuildMusicState) -> discord.Embed:
    song = state.current
    if not song:
        return discord.Embed(title="⏹️ Keine aktive Wiedergabe", color=COLOR_MUSIC)

    title_prefix = "📻 Radio spielt" if song.is_radio else "🎶 Jetzt spielt"
    desc = (
        f"**{song.title}**"
        if not song.webpage_url or not song.webpage_url.startswith("http")
        else f"[{song.title}]({song.webpage_url})"
    )
    embed = discord.Embed(
        title=title_prefix,
        description=desc,
        color=COLOR_RADIO if song.is_radio else COLOR_MUSIC,
    )
    req_str = song.requester.mention if song.requester else "🔄 Autoplay"
    embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
    embed.add_field(name="👤 Angefragt von", value=req_str, inline=True)
    embed.add_field(name="📋 Queue", value=f"{len(state.queue)} Songs", inline=True)
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    loop_labels = {"off": "Aus", "track": "🔂 Song", "queue": "🔁 Queue"}
    status_str = "⏸️ Pausiert" if (state.voice_client and state.voice_client.is_paused()) else "▶️ Spielt"
    embed.set_footer(
        text=(
            f"{status_str} • Loop: {loop_labels.get(state.loop_mode, 'Aus')} • "
            f"Autoplay: {'✅ An' if state.autoplay else '❌ Aus'}"
        )
    )
    return embed


# ── Discord UI Views (Player Controls, Queue Pagination, Search, Radio) ───

class MusicPlayerControlView(discord.ui.View):
    def __init__(self, state: GuildMusicState) -> None:
        super().__init__(timeout=600)
        self.state = state
        self._sync_labels()
        if state.current and state.current.webpage_url.startswith("http") and not state.current.is_radio:
            self.add_item(
                discord.ui.Button(
                    label="Öffnen",
                    emoji="🔗",
                    style=discord.ButtonStyle.link,
                    url=state.current.webpage_url,
                    row=1,
                )
            )

    def _sync_labels(self) -> None:
        paused = bool(self.state.voice_client and self.state.voice_client.is_paused())
        self.btn_pause.label = "Weiter" if paused else "Pause"
        self.btn_pause.emoji = "▶️" if paused else "⏸️"
        self.btn_pause.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary

        loop_map = {
            "off": ("Loop: Aus", discord.ButtonStyle.secondary),
            "track": ("Loop: Song", discord.ButtonStyle.primary),
            "queue": ("Loop: Queue", discord.ButtonStyle.success),
        }
        lbl, st = loop_map.get(self.state.loop_mode, loop_map["off"])
        self.btn_loop.label = lbl
        self.btn_loop.style = st

        self.btn_autoplay.label = "Autoplay: An" if self.state.autoplay else "Autoplay: Aus"
        self.btn_autoplay.style = (
            discord.ButtonStyle.success if self.state.autoplay else discord.ButtonStyle.secondary
        )

    async def _check_vc(self, interaction: discord.Interaction) -> bool:
        if not self.state.voice_client or not self.state.voice_client.is_connected():
            await interaction.response.send_message("❌ Keine aktive Wiedergabe.", ephemeral=True)
            return False
        if (
            isinstance(interaction.user, discord.Member)
            and interaction.user.voice
            and interaction.user.voice.channel == self.state.voice_client.channel
        ):
            return True
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "❌ Du musst im selben Voice-Kanal sein, um den Player zu steuern!", ephemeral=True
        )
        return False

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        vc = self.state.voice_client
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        self._sync_labels()
        await interaction.response.edit_message(embed=build_now_playing_embed(self.state), view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        if self.state.loop_mode == "track":
            self.state.loop_mode = "off"
        if self.state.voice_client:
            self.state.voice_client.stop()
        await interaction.response.send_message("⏭️ Song übersprungen!", ephemeral=True, delete_after=4)

    @discord.ui.button(label="Loop: Aus", emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def btn_loop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        order = ["off", "track", "queue"]
        self.state.loop_mode = order[(order.index(self.state.loop_mode) + 1) % len(order)]
        self._sync_labels()
        await interaction.response.edit_message(embed=build_now_playing_embed(self.state), view=self)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def btn_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        if len(self.state.queue) < 2:
            await interaction.response.send_message("❌ Zu wenig Songs in der Queue.", ephemeral=True)
            return
        qlist = list(self.state.queue)
        random.shuffle(qlist)
        self.state.queue = deque(qlist)
        await interaction.response.send_message(
            f"🔀 **{len(qlist)}** Songs wurden gemischt!", ephemeral=True, delete_after=5
        )

    @discord.ui.button(label="Autoplay: Aus", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def btn_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        self.state.autoplay = not self.state.autoplay
        self._sync_labels()
        await interaction.response.edit_message(embed=build_now_playing_embed(self.state), view=self)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        await self.state.cleanup()
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⏹️ Wiedergabe beendet",
                description=f"Gestoppt von {interaction.user.mention}.",
                color=COLOR_ERROR,
            ),
            view=None,
        )


class QueuePaginationView(discord.ui.View):
    PER_PAGE = 10

    def __init__(self, state: GuildMusicState, page: int = 0) -> None:
        super().__init__(timeout=120)
        self.state = state
        self.page = page
        self._sync()

    def _max_page(self) -> int:
        return max(0, (len(self.state.queue) - 1) // self.PER_PAGE)

    def _sync(self) -> None:
        mp = self._max_page()
        self.page = max(0, min(self.page, mp))
        self.btn_prev.disabled = self.page <= 0
        self.btn_next.disabled = self.page >= mp

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="📋 Musik-Warteschlange", color=COLOR_MUSIC)
        if self.state.current:
            c = self.state.current
            embed.add_field(
                name="🎶 Spielt gerade",
                value=f"**{c.title}** (`{c.duration_str}`)",
                inline=False,
            )
        qlist = list(self.state.queue)
        if not qlist:
            embed.description = "*Die Warteschlange ist leer.*"
        else:
            start = self.page * self.PER_PAGE
            chunk = qlist[start : start + self.PER_PAGE]
            lines = [
                f"`{start + idx}.` **{s.title}** — `{s.duration_str}`"
                for idx, s in enumerate(chunk, 1)
            ]
            embed.add_field(
                name=f"⏭️ Als Nächstes ({len(qlist)} Songs)",
                value="\n".join(lines),
                inline=False,
            )
        embed.set_footer(
            text=f"Seite {self.page + 1}/{self._max_page() + 1} • Autoplay: {'An' if self.state.autoplay else 'Aus'}"
        )
        return embed

    @discord.ui.button(label="◀️ Zurück", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Weiter ▶️", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class SearchResultSelectView(discord.ui.View):
    def __init__(self, results: list[dict[str, Any]], user: discord.abc.User, bot: commands.Bot) -> None:
        super().__init__(timeout=60)
        self.results = results
        self.user = user
        self.bot = bot
        options = []
        for idx, entry in enumerate(results[:10]):
            title = (entry.get("title") or "Unbekannt")[:95]
            dur = int(entry.get("duration") or 0)
            m, s = divmod(dur, 60)
            dur_str = f"{m}:{s:02d}" if dur > 0 else "Live/Unbekannt"
            options.append(discord.SelectOption(label=f"{idx + 1}. {title}", description=dur_str, value=str(idx)))
        self.picker = discord.ui.Select(placeholder="🎵 Song auswählen...", options=options)
        self.picker.callback = self._on_select
        self.add_item(self.picker)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Nicht deine Suche!", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ Du musst in einem Voice-Kanal sein!", ephemeral=True)
            return

        await interaction.response.defer()
        entry = self.results[int(self.picker.values[0])]
        state = get_music_state(interaction.guild_id, self.bot)
        vc_target = interaction.user.voice.channel

        try:
            await ensure_voice_connected(state, vc_target)
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}", embed=None, view=None
            )
            return

        song = SongInfo(
            title=entry.get("title", "Unbekannt"),
            url="",
            webpage_url=_get_reference_url(entry),
            duration=int(entry.get("duration") or 0),
            thumbnail=_entry_thumbnail(entry),
            requester=interaction.user,
        )
        state.queue.append(song)
        await interaction.edit_original_response(
            content=f"✅ **{song.title}** ausgewählt!", embed=None, view=None
        )
        if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
            await play_next(state, interaction.channel)


async def start_radio_stream(
    interaction: discord.Interaction,
    station: dict[str, Any],
    state: GuildMusicState,
) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
        msg = "❌ Du musst in einem Voice-Kanal sein!"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    user_vc = interaction.user.voice.channel
    try:
        await ensure_voice_connected(state, user_vc)
    except Exception as e:
        err_msg = f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}"
        if interaction.response.is_done():
            await interaction.followup.send(err_msg, ephemeral=True)
        else:
            await interaction.response.send_message(err_msg, ephemeral=True)
        return

    state.queue.clear()
    state.autoplay = False
    state.loop_mode = "off"
    if state.voice_client.is_playing() or state.voice_client.is_paused():
        state.current = None
        state.voice_client.stop()
        await asyncio.sleep(0.3)

    radio_song = SongInfo(
        title=f"📻 {station['name']}",
        url=station["url"],
        webpage_url=station["url"],
        duration=0,
        thumbnail=station.get("favicon", ""),
        requester=interaction.user,
        is_radio=True,
    )
    state.queue.appendleft(radio_song)
    await play_next(state, interaction.channel)

    reply_text = f"📻 Starte Livestream: **{station['name']}**"
    if interaction.response.is_done():
        await interaction.followup.send(reply_text, ephemeral=True)
    else:
        await interaction.response.send_message(reply_text, ephemeral=True)


class RadioPresetSelectView(discord.ui.View):
    def __init__(self, user: discord.abc.User, bot: commands.Bot) -> None:
        super().__init__(timeout=60)
        self.user = user
        self.bot = bot
        options = [
            discord.SelectOption(
                label=s["name"],
                description=s["description"][:95],
                emoji=s["emoji"],
                value=key,
            )
            for key, s in RADIO_STATIONS.items()
        ]
        self.picker = discord.ui.Select(placeholder="📻 Preset-Sender auswählen...", options=options)
        self.picker.callback = self._on_pick
        self.add_item(self.picker)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Nicht dein Menü!", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        station = RADIO_STATIONS[self.picker.values[0]]
        state = get_music_state(interaction.guild_id, self.bot)
        await start_radio_stream(interaction, station, state)


class RadioSearchSelectView(discord.ui.View):
    def __init__(self, user: discord.abc.User, stations: list[dict[str, Any]], bot: commands.Bot) -> None:
        super().__init__(timeout=60)
        self.user = user
        self.stations = stations
        self.bot = bot
        options = []
        for idx, st in enumerate(stations[:10]):
            desc_parts = []
            if st.get("country"):
                desc_parts.append(st["country"])
            if st.get("bitrate"):
                desc_parts.append(f"{st['bitrate']} kbps")
            options.append(
                discord.SelectOption(
                    label=f"{idx + 1}. {st['name'][:85]}",
                    description=" • ".join(desc_parts)[:95] or "Live-Stream",
                    value=str(idx),
                )
            )
        self.picker = discord.ui.Select(placeholder="📻 Gefundenen Sender starten...", options=options)
        self.picker.callback = self._on_pick
        self.add_item(self.picker)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Nicht dein Menü!", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        station = self.stations[int(self.picker.values[0])]
        state = get_music_state(interaction.guild_id, self.bot)
        await start_radio_stream(interaction, station, state)


# ═══════════════════════════════════════════════════════════════════════════
# MUSIC COG
# ═══════════════════════════════════════════════════════════════════════════

class MusicCog(commands.Cog, name="Music"):
    """YouTube-, Spotify- & Radio-Musik-Bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.music_idle_checker.start()

    async def cog_unload(self) -> None:
        self.music_idle_checker.cancel()
        for state in list(music_states.values()):
            await state.cleanup()

    def active_player_count(self) -> int:
        return sum(1 for vc in self.bot.voice_clients if vc.is_connected())

    async def _leave_if_still_empty(self, guild_id: int) -> None:
        """Prüft nach 2s Verzögerung erneut, ob der Bot allein im Voice-Kanal ist, und verlässt ihn lautlos."""
        await asyncio.sleep(2.0)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        state = get_music_state(guild_id, self.bot)
        vc = guild.voice_client or state.voice_client
        bot_ch = (
            (guild.me.voice.channel if guild.me and guild.me.voice else None)
            or (vc.channel if vc else None)
        )
        if bot_ch is None and vc is None:
            return
        bot_id = self.bot.user.id if self.bot.user else 0
        if _count_humans_in_channel(guild, bot_ch, bot_id) == 0:
            print(f"🎵 [Music] Kanal leer in Guild {guild_id} – verlasse Voice-Kanal lautlos.")
            await state.cleanup()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        bot_id = self.bot.user.id if self.bot.user else 0

        # 1) Der Bot selbst hat den Kanal gewechselt oder wurde getrennt
        if bot_id and member.id == bot_id:
            if before.channel is not None and after.channel is None:
                state = music_states.get(member.guild.id)
                if state:
                    await state.cleanup(disconnect_voice=False)
            elif after.channel is not None:
                state = get_music_state(member.guild.id, self.bot)
                if member.guild.voice_client is not None:
                    state.voice_client = member.guild.voice_client
                if _count_humans_in_channel(member.guild, after.channel, bot_id) == 0:
                    asyncio.create_task(self._leave_if_still_empty(member.guild.id))
            return

        # 2) Ein menschlicher User hat einen Kanal verlassen oder gewechselt -> sofort prüfen!
        if not member.bot and before.channel is not None:
            if after.channel is None or after.channel.id != before.channel.id:
                guild = member.guild
                vc = guild.voice_client or (
                    music_states[guild.id].voice_client if guild.id in music_states else None
                )
                bot_ch = (
                    (guild.me.voice.channel if guild.me and guild.me.voice else None)
                    or (vc.channel if vc else None)
                )
                if bot_ch is not None and bot_ch.id == before.channel.id:
                    if _count_humans_in_channel(guild, bot_ch, bot_id) == 0:
                        asyncio.create_task(self._leave_if_still_empty(guild.id))

    # ── Idle Checker Task (verlässt den Kanal bei Leere / Inaktivität komplett lautlos!) ──

    @tasks.loop(seconds=2)
    async def music_idle_checker(self) -> None:
        try:
            bot_id = self.bot.user.id if self.bot.user else 0
            checked_guild_ids: set[int] = set()

            guilds_to_check: list[discord.Guild] = list(self.bot.guilds)
            for gid in list(music_states.keys()):
                g = self.bot.get_guild(gid)
                if g and g not in guilds_to_check:
                    guilds_to_check.append(g)

            for guild in guilds_to_check:
                if guild.id in checked_guild_ids:
                    continue
                checked_guild_ids.add(guild.id)

                existing_state = music_states.get(guild.id)
                vc = guild.voice_client or (existing_state.voice_client if existing_state else None)
                bot_ch = (
                    (guild.me.voice.channel if guild.me and guild.me.voice else None)
                    or (vc.channel if vc else None)
                )

                # Bot ist in dieser Guild in keinem Voice-Kanal
                if vc is None and bot_ch is None:
                    continue

                state = get_music_state(guild.id, self.bot)
                if vc is not None and state.voice_client is not vc:
                    state.voice_client = vc

                # Fall A: Niemand (kein echter Mensch) im Voice-Kanal -> sofort (nach 2s) leaven!
                humans = _count_humans_in_channel(guild, bot_ch, bot_id)
                if humans == 0:
                    state._empty_seconds += 2
                    if state._empty_seconds >= 2:
                        print(f"🎵 [Music] Niemand im Voice-Kanal (Guild {guild.id}) – verlasse Kanal lautlos.")
                        await state.cleanup()
                    continue

                state._empty_seconds = 0

                # Fall B: Leute sind im Kanal – prüfen ob Musik läuft oder geladen wird
                is_playing_or_paused = bool(vc and (vc.is_playing() or vc.is_paused()))
                if is_playing_or_paused or state.is_loading:
                    state.reset_idle()
                    continue

                # Falls noch Songs in der Queue warten, Wiedergabe anstoßen
                if vc is not None and vc.is_connected() and state.queue:
                    state.reset_idle()
                    asyncio.create_task(play_next(state))
                    continue

                # Falls state.current noch hing, obwohl weder Audio läuft noch geladen wird -> nach 4s bereinigen
                if state.current is not None:
                    state._stale_current_seconds += 2
                    if state._stale_current_seconds >= 4:
                        state.current = None
                    else:
                        continue

                # Weder Musik noch Queue noch Ladevorgang aktiv -> Idle-Timer hochzählen
                state.increment_idle(2)
                # Bei verwaistem Gateway-Voice-State ohne aktiven VoiceClient nach 6s trennen, sonst nach Timeout
                if (vc is None and state._idle_seconds >= 6) or state.is_idle_timeout:
                    print(f"🎵 [Music] Keine Musik aktiv (Guild {guild.id}) – verlasse Kanal lautlos.")
                    await state.cleanup()
        except Exception as e:
            print(f"⚠️ [Music] Idle-Checker Fehler: {e}")

    @music_idle_checker.before_loop
    async def _before_idle(self) -> None:
        await self.bot.wait_until_ready()

    # ── Slash Commands: Music ─────────────────────────────────────────────

    @app_commands.command(
        name="play",
        description="🎵 Spielt einen Song oder eine Playlist ab (YouTube + Spotify)",
    )
    @app_commands.describe(query="Songtitel, YouTube-URL, Spotify-URL oder Playlist")
    async def cmd_play(self, interaction: discord.Interaction, query: str) -> None:
        if not runtime_settings.get("music_enabled", MUSIC_ENABLED) or not YTDLP_AVAILABLE:
            await interaction.response.send_message("❌ Musik-Modul ist deaktiviert.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ Du musst in einem Voice-Kanal sein!", ephemeral=True)
            return

        await interaction.response.defer()
        user_vc = interaction.user.voice.channel
        state = get_music_state(interaction.guild_id, self.bot)

        perms = user_vc.permissions_for(interaction.guild.me)
        if not perms.connect or not perms.speak:
            await interaction.followup.send(
                f"❌ Keine Berechtigung (`Verbinden` & `Sprechen`) für **{user_vc.name}**!"
            )
            return

        state.is_loading = True
        try:
            try:
                await ensure_voice_connected(state, user_vc)
            except Exception as e:
                await interaction.followup.send(f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}")
                return

            # Falls Radio lief -> beenden für reguläre Queue
            if state.current and state.current.is_radio:
                state.current = None
                if state.voice_client:
                    state.voice_client.stop()
                await asyncio.sleep(0.2)

            state.reset_idle()

            # 1) Spotify
            if is_spotify_url(query):
                sp_tracks = await fetch_spotify_tracks(query)
                if not sp_tracks:
                    await interaction.followup.send("❌ Konnte keine Songs von Spotify laden.")
                    return
                if len(sp_tracks) == 1:
                    sp = sp_tracks[0]
                    results = await search_tracks(sp["search_query"], limit=1)
                    if not results:
                        await interaction.followup.send(f"❌ Konnte **{sp['title']}** nicht auf YouTube finden.")
                        return
                    entry = results[0]
                    song = SongInfo(
                        title=sp["title"],
                        url="",
                        webpage_url=_get_reference_url(entry),
                        duration=sp.get("duration") or int(entry.get("duration") or 0),
                        thumbnail=sp.get("thumbnail") or _entry_thumbnail(entry),
                        requester=interaction.user,
                    )
                    state.queue.append(song)
                    if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                        msg = await interaction.followup.send(f"🟢 **{sp['title']}** wird geladen...", wait=True)
                        try:
                            await msg.delete(delay=4)
                        except Exception:
                            pass
                        await play_next(state, interaction.channel)
                    else:
                        embed = discord.Embed(
                            title="➕ Spotify → Warteschlange",
                            description=f"🟢 **{song.title}**",
                            color=COLOR_SPOTIFY,
                        )
                        embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
                        embed.add_field(name="📋 Position", value=f"#{len(state.queue)}", inline=True)
                        if song.thumbnail:
                            embed.set_thumbnail(url=song.thumbnail)
                        await interaction.followup.send(embed=embed)
                    return

                # Spotify Playlist / Album (ersten Track sofort auflösen, Rest asynchron)
                first = sp_tracks[0]
                first_res = await search_tracks(first["search_query"], limit=1)
                if first_res:
                    e0 = first_res[0]
                    state.queue.append(
                        SongInfo(
                            first["title"], "", _get_reference_url(e0),
                            first.get("duration") or int(e0.get("duration") or 0),
                            first.get("thumbnail") or _entry_thumbnail(e0),
                            interaction.user,
                        )
                    )

                async def _queue_remaining() -> None:
                    for sp in sp_tracks[1:]:
                        res = await search_tracks(sp["search_query"], limit=1)
                        if res:
                            en = res[0]
                            state.queue.append(
                                SongInfo(
                                    sp["title"], "", _get_reference_url(en),
                                    sp.get("duration") or int(en.get("duration") or 0),
                                    sp.get("thumbnail") or _entry_thumbnail(en),
                                    interaction.user,
                                )
                            )
                        await asyncio.sleep(0.2)

                asyncio.create_task(_queue_remaining())
                embed = discord.Embed(
                    title="🟢 Spotify Playlist/Album erkannt!",
                    description=f"**{len(sp_tracks)}** Songs werden zur Warteschlange hinzugefügt.",
                    color=COLOR_SPOTIFY,
                )
                await interaction.followup.send(embed=embed)
                if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                    await play_next(state, interaction.channel)
                return

            # 2) YouTube Playlist
            if _is_playlist_url(query):
                entries = await extract_playlist(query)
                if not entries:
                    await interaction.followup.send("❌ Konnte keine Songs aus der Playlist laden.")
                    return
                added = 0
                for entry in entries:
                    ref = _get_reference_url(entry)
                    if not ref:
                        continue
                    state.queue.append(
                        SongInfo(
                            entry.get("title", "Unbekannt"),
                            "",
                            ref,
                            int(entry.get("duration") or 0),
                            _entry_thumbnail(entry),
                            interaction.user,
                        )
                    )
                    added += 1
                embed = discord.Embed(
                    title="📋 Playlist hinzugefügt",
                    description=f"**{added}** Songs zur Warteschlange hinzugefügt!",
                    color=COLOR_MUSIC,
                )
                await interaction.followup.send(embed=embed)
                if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                    await play_next(state, interaction.channel)
                return

            # 3) Einzelner Song / Direktlink
            results = await search_tracks(query, limit=1)
            if not results:
                await interaction.followup.send(f"❌ Keine Ergebnisse für: `{query}`")
                return
            entry = results[0]
            song = SongInfo(
                title=entry.get("title", "Unbekannt"),
                url="",
                webpage_url=_get_reference_url(entry, query),
                duration=int(entry.get("duration") or 0),
                thumbnail=_entry_thumbnail(entry),
                requester=interaction.user,
            )
            state.queue.append(song)

            if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                msg = await interaction.followup.send(f"🎵 **{song.title}** wird geladen...", wait=True)
                try:
                    await msg.delete(delay=4)
                except Exception:
                    pass
                await play_next(state, interaction.channel)
            else:
                embed = discord.Embed(
                    title="➕ Zur Warteschlange hinzugefügt",
                    description=f"[{song.title}]({song.webpage_url})",
                    color=COLOR_MUSIC,
                )
                embed.add_field(name="⏱️ Dauer", value=song.duration_str, inline=True)
                embed.add_field(name="📋 Position", value=f"#{len(state.queue)}", inline=True)
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                await interaction.followup.send(embed=embed)
        finally:
            state.is_loading = False

    @app_commands.command(name="search", description="🎵 Suche nach Songs und wähle einen aus")
    @app_commands.describe(query="Songtitel zum Suchen")
    async def cmd_search(self, interaction: discord.Interaction, query: str) -> None:
        if not runtime_settings.get("music_enabled", MUSIC_ENABLED) or not YTDLP_AVAILABLE:
            await interaction.response.send_message("❌ Musik-Modul ist deaktiviert.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        results = await search_tracks(query, limit=10)
        if not results:
            await interaction.followup.send(f"❌ Keine Ergebnisse für: `{query}`", ephemeral=True)
            return

        lines = []
        for idx, entry in enumerate(results[:10], 1):
            dur = int(entry.get("duration") or 0)
            m, s = divmod(dur, 60)
            dur_str = f"{m}:{s:02d}" if dur > 0 else "Live"
            lines.append(f"`{idx}.` **{entry.get('title', 'Unbekannt')}** — `{dur_str}`")

        embed = discord.Embed(
            title=f"🔍 Suchergebnisse für: {query}",
            description="\n".join(lines),
            color=COLOR_MUSIC,
        )
        embed.set_footer(text="Wähle einen Song aus dem Dropdown-Menü")
        view = SearchResultSelectView(results[:10], interaction.user, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="skip", description="🎵 Überspringt den aktuellen Song")
    async def cmd_skip(self, interaction: discord.Interaction) -> None:
        state = get_music_state(interaction.guild_id, self.bot)
        if not state.voice_client or (not state.voice_client.is_playing() and not state.voice_client.is_paused()):
            await interaction.response.send_message("❌ Es wird gerade nichts abgespielt.", ephemeral=True)
            return
        if state.loop_mode == "track":
            state.loop_mode = "off"
        state.voice_client.stop()
        await interaction.response.send_message("⏭️ Song übersprungen!", delete_after=5)

    @app_commands.command(name="stop", description="🎵 Stoppt die Musik und leert die Warteschlange")
    async def cmd_stop(self, interaction: discord.Interaction) -> None:
        state = get_music_state(interaction.guild_id, self.bot)
        if not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.send_message("❌ Ich bin in keinem Voice-Kanal.", ephemeral=True)
            return
        await state.cleanup()
        await interaction.response.send_message(
            "⏹️ Musik gestoppt, Warteschlange geleert und Kanal verlassen.",
            delete_after=10,
        )

    @app_commands.command(name="queue", description="🎵 Zeigt die aktuelle Warteschlange")
    async def cmd_queue(self, interaction: discord.Interaction) -> None:
        state = get_music_state(interaction.guild_id, self.bot)
        view = QueuePaginationView(state, page=0)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @app_commands.command(name="shuffle", description="🎵 Mischt die Warteschlange zufällig")
    async def cmd_shuffle(self, interaction: discord.Interaction) -> None:
        state = get_music_state(interaction.guild_id, self.bot)
        if len(state.queue) < 2:
            await interaction.response.send_message("❌ Zu wenig Songs zum Mischen.", ephemeral=True)
            return
        qlist = list(state.queue)
        random.shuffle(qlist)
        state.queue = deque(qlist)
        await interaction.response.send_message(f"🔀 **{len(qlist)}** Songs wurden gemischt!", delete_after=10)

    @app_commands.command(
        name="autoplay",
        description="🎵 Automatisch ähnliche Songs spielen wenn die Queue leer ist",
    )
    @app_commands.describe(mode="Autoplay ein- oder ausschalten")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="An", value="on"),
            app_commands.Choice(name="Aus", value="off"),
        ]
    )
    async def cmd_autoplay(self, interaction: discord.Interaction, mode: str) -> None:
        state = get_music_state(interaction.guild_id, self.bot)
        state.autoplay = mode == "on"
        if state.autoplay:
            await interaction.response.send_message(
                "🔄 **Autoplay aktiviert!** Ähnliche Songs werden automatisch gespielt.", delete_after=10
            )
        else:
            await interaction.response.send_message("⏹️ **Autoplay deaktiviert.**", delete_after=10)

    @app_commands.command(
        name="nowplaying",
        description="🎵 Zeigt den aktuellen Song inkl. interaktiver Steuerung",
    )
    async def cmd_nowplaying(self, interaction: discord.Interaction) -> None:
        state = get_music_state(interaction.guild_id, self.bot)
        if not state.current:
            await interaction.response.send_message("❌ Es wird gerade nichts abgespielt.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=build_now_playing_embed(state),
            view=MusicPlayerControlView(state),
            ephemeral=True,
        )

    # ── Slash Commands: Radio ─────────────────────────────────────────────

    @app_commands.command(name="radio", description="📻 Starte einen Radiosender (Preset oder weltweite Suche)")
    @app_commands.describe(sender="Optional: Name des Senders oder Genres (leer = Preset-Liste)")
    async def cmd_radio(
        self, interaction: discord.Interaction, sender: str | None = None
    ) -> None:
        if not runtime_settings.get("music_enabled", MUSIC_ENABLED):
            await interaction.response.send_message("❌ Musik-Modul deaktiviert.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ Du musst in einem Voice-Kanal sein!", ephemeral=True)
            return

        if sender is None:
            stations_list = "\n".join(
                f"{s['emoji']} **{s['name']}** — {s['description']}"
                for s in RADIO_STATIONS.values()
            )
            embed = discord.Embed(
                title="📻 Radio – Sender auswählen",
                description="Wähle einen Sender aus dem Menü oder nutze `/radio <name>` zur weltweiten Suche!",
                color=COLOR_RADIO,
            )
            embed.add_field(name="📡 Preset-Sender", value=stations_list, inline=False)
            embed.set_footer(text="💡 Tipp: /radio Bayern 3 oder /radio Lofi")
            await interaction.response.send_message(
                embed=embed, view=RadioPresetSelectView(interaction.user, self.bot), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        stations = await search_radio_stations_list(sender, limit=10)
        if not stations:
            await interaction.followup.send(
                f"❌ Kein Radiosender für **{sender}** gefunden!", ephemeral=True
            )
            return

        if len(stations) == 1:
            state = get_music_state(interaction.guild_id, self.bot)
            await start_radio_stream(interaction, stations[0], state)
            return

        lines = []
        for idx, st in enumerate(stations[:10], 1):
            meta = " • ".join(
                p for p in (st.get("country", ""), f"{st['bitrate']}kbps" if st.get("bitrate") else "") if p
            )
            lines.append(f"`{idx}.` **{st['name']}**" + (f"\n╰ {meta}" if meta else ""))

        embed = discord.Embed(
            title=f"📻 Suchergebnisse: {sender}",
            description="\n".join(lines),
            color=COLOR_RADIO,
        )
        embed.set_footer(text="Wähle einen Sender aus dem Dropdown-Menü!")
        await interaction.followup.send(
            embed=embed, view=RadioSearchSelectView(interaction.user, stations, self.bot), ephemeral=True
        )

    @app_commands.command(name="stations", description="📻 Zeigt alle Preset-Radiosender")
    async def cmd_stations(self, interaction: discord.Interaction) -> None:
        lines = [
            f"{s['emoji']} **{s['name']}**\n╰ {s['description']}"
            for s in RADIO_STATIONS.values()
        ]
        embed = discord.Embed(
            title="📻 Preset-Radiosender",
            description="\n\n".join(lines),
            color=COLOR_RADIO,
        )
        embed.add_field(
            name="💡 Bedienung",
            value=(
                "`/radio` — Aus Preset-Sendern wählen\n"
                "`/radio <Name>` — Weltweit nach Radiosendern suchen\n"
                "`/stop` — Radio beenden"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
