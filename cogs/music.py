"""Music & Radio Cog: YouTube, Spotify, Live-Radio, Autoplay, Loop & Interaktive Controls."""
from __future__ import annotations

import asyncio
from collections import deque
import os
import random
import re
import threading
import time
from typing import Any
from urllib.parse import quote
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.config import (
    BASE_DIR,
    COLOR_ERROR,
    COLOR_GOLD,
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
from bot_core.storage import (
    clear_active_music_session,
    get_guild_music_prefs,
    get_guild_volume,
    get_user_favorites,
    load_active_music_sessions,
    remove_user_favorite,
    save_active_music_session,
    set_guild_music_prefs,
    set_guild_volume,
    toggle_user_favorite,
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
        "-nostdin -thread_queue_size 4096 "
        "-probesize 64k -analyzeduration 0 "
        "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
        "-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5 "
        f'-headers "{_YT_HEADERS}"'
    ),
    "options": "-vn -sn -dn -bufsize 8M",
}

RADIO_FFMPEG_OPTIONS = {
    "before_options": (
        "-nostdin -thread_queue_size 4096 "
        "-probesize 64k -analyzeduration 0 "
        "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
        "-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5"
    ),
    "options": "-vn -sn -dn -bufsize 8M",
}

BEST_AUDIO_FORMAT = (
    "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/"
    "bestaudio[protocol^=http][protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best"
)

STREAM_URL_MAX_AGE_SECS = 2700  # 45 Minuten Cache-Gültigkeit für vorab aufgelöste Stream-URLs
_ydl_thread_local = threading.local()

YDL_TIMEOUTS = {
    "socket_timeout": 15,
    "request_timeout": 15,
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


def _get_thread_ydl(cache_key: str, opts: dict[str, Any]) -> Any:
    """Wiederverwendet initialisierte YoutubeDL-Instanzen pro Worker-Thread (massiver Speed-Boost auf dem Pi)."""
    pool: dict[str, Any] | None = getattr(_ydl_thread_local, "pool", None)
    if pool is None:
        pool = {}
        _ydl_thread_local.pool = pool
    ydl = pool.get(cache_key)
    if ydl is None:
        ydl = yt_dlp.YoutubeDL(opts)
        pool[cache_key] = ydl
    return ydl


YTDL_SEARCH_OPTIONS = {
    "format": BEST_AUDIO_FORMAT,
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
    "format": BEST_AUDIO_FORMAT,
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
    None,
    {"extractor_args": {"youtube": {"player_client": ["ios"]}}},
    {"extractor_args": {"youtube": {"player_client": ["android"]}}},
    {"extractor_args": {"youtube": {"player_client": ["web"]}}},
]


class SongInfo:
    __slots__ = (
        "title",
        "url",
        "webpage_url",
        "duration",
        "thumbnail",
        "requester",
        "is_radio",
        "resolved_at",
    )

    def __init__(
        self,
        title: str,
        url: str,
        webpage_url: str,
        duration: int,
        thumbnail: str,
        requester: discord.abc.User | None,
        is_radio: bool = False,
        resolved_at: float = 0.0,
    ) -> None:
        self.title = title
        self.url = url
        self.webpage_url = webpage_url
        self.duration = int(duration) if duration else 0
        self.thumbnail = thumbnail or ""
        self.requester = requester
        self.is_radio = is_radio
        self.resolved_at = resolved_at or (time.monotonic() if (url and not is_radio) else 0.0)

    def has_fresh_stream_url(self) -> bool:
        if self.is_radio:
            return bool(self.url)
        return bool(
            self.url
            and self.resolved_at > 0
            and (time.monotonic() - self.resolved_at) < STREAM_URL_MAX_AGE_SECS
        )

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
        mprefs = get_guild_music_prefs(guild_id)
        self.autoplay: bool = bool(mprefs.get("autoplay", False))
        self.loop_mode: str = str(mprefs.get("loop_mode", "off"))
        self.volume_pct: int = get_guild_volume(guild_id)
        self.dj_user_id: int = 0
        self.last_voice_channel_id: int = 0
        self._intentional_disconnect: bool = False
        self._last_auto_reconnect_ts: float = 0.0
        self._prefetch_task: asyncio.Task[Any] | None = None
        self.history: deque[str] = deque(maxlen=15)
        self.is_loading: bool = False
        self._idle_seconds: int = 0
        self._empty_seconds: int = 0
        self._stale_current_seconds: int = 0
        self._lock = asyncio.Lock()
        self.consecutive_failures: int = 0

    def active_channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        """Gibt den aktuell verbundenen Voice-Kanal des Bots zurück (oder None)."""
        guild = self.bot.get_guild(self.guild_id)
        vc = (guild.voice_client if guild else None) or self.voice_client
        if vc is not None and vc.is_connected() and vc.channel is not None:
            return vc.channel
        if guild is not None and guild.me is not None and guild.me.voice and guild.me.voice.channel:
            return guild.me.voice.channel
        return None

    @staticmethod
    def _serialize_song(s: SongInfo) -> dict[str, Any]:
        return {
            "title": s.title,
            "url": s.url if s.is_radio else "",
            "webpage_url": s.webpage_url or s.url,
            "duration": int(s.duration or 0),
            "thumbnail": s.thumbnail or "",
            "is_radio": bool(s.is_radio),
        }

    @staticmethod
    def _deserialize_song(d: dict[str, Any]) -> SongInfo:
        is_rad = bool(d.get("is_radio", False))
        wurl = str(d.get("webpage_url") or d.get("url") or "")
        return SongInfo(
            title=str(d.get("title") or "Unbekannt"),
            url=str(d.get("url") or wurl) if is_rad else "",
            webpage_url=wurl,
            duration=int(d.get("duration") or 0),
            thumbnail=str(d.get("thumbnail") or ""),
            requester=None,
            is_radio=is_rad,
        )

    def persist_session(self) -> None:
        """Sichert den aktuellen Song/Radiosender, DJ & die Queue für nahtlosen Auto-Resume nach Neustarts."""
        vc_ch = self.active_channel()
        if vc_ch is None and self.last_voice_channel_id:
            guild = self.bot.get_guild(self.guild_id)
            if guild is not None:
                cand = guild.get_channel(self.last_voice_channel_id)
                if isinstance(cand, (discord.VoiceChannel, discord.StageChannel)):
                    vc_ch = cand
        if vc_ch is None or (self.current is None and not self.queue):
            clear_active_music_session(self.guild_id)
            return
        tc_id = getattr(self.text_channel, "id", 0) or 0
        save_active_music_session(
            self.guild_id,
            {
                "voice_channel_id": vc_ch.id,
                "text_channel_id": int(tc_id),
                "dj_user_id": int(self.dj_user_id or 0),
                "current": self._serialize_song(self.current) if self.current else None,
                "queue": [self._serialize_song(q) for q in list(self.queue)[:100]],
            },
        )

    def set_volume(self, pct: int) -> int:
        """Setzt die Lautstärke live (5%–200%) und speichert sie dauerhaft für diesen Server."""
        self.volume_pct = set_guild_volume(self.guild_id, pct)
        guild = self.bot.get_guild(self.guild_id)
        vc = self.voice_client or (guild.voice_client if guild else None)
        if vc is not None and getattr(vc, "source", None) is not None and hasattr(vc.source, "volume"):
            try:
                vc.source.volume = self.volume_pct / 100.0
            except Exception:
                pass
        return self.volume_pct

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

    async def cleanup(
        self,
        disconnect_voice: bool = True,
        clear_saved_session: bool = True,
    ) -> None:
        """Trennt die Voice-Verbindung zuverlässig & komplett lautlos (ohne Chat-Nachricht)."""
        self._intentional_disconnect = True
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None

        if clear_saved_session:
            clear_active_music_session(self.guild_id)
            self.dj_user_id = 0
            self.last_voice_channel_id = 0

        self.queue.clear()
        self.current = None
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


def _step_volume(current_pct: int, delta: int) -> int:
    """10%-Schritte mit Einrasten auf glatte 10er-Werte & garantiertem Stopp auf exakt 100%."""
    cur = max(5, min(200, int(current_pct)))
    if delta > 0:
        if cur < 100 < cur + delta:
            return 100
        nxt = ((cur // 10) + 1) * 10 if (cur % 10 != 0) else (cur + delta)
        return min(200, nxt)
    if cur + delta < 100 < cur:
        return 100
    nxt = (cur // 10) * 10 if (cur % 10 != 0) else (cur + delta)
    return max(10, nxt)


def _human_user_ids_in_channel(
    guild: discord.Guild | None,
    channel: discord.abc.Snowflake | None,
    bot_id: int,
) -> list[int]:
    """Gibt die User-IDs aller echten (nicht-Bot) Nutzer im Voice-Kanal zurück."""
    if guild is None or channel is None:
        return []
    fresh_ch = guild.get_channel(channel.id) or channel
    vstates = getattr(fresh_ch, "voice_states", None)
    if isinstance(vstates, dict) and vstates:
        ids: list[int] = []
        for uid in vstates:
            if uid == bot_id:
                continue
            member = guild.get_member(uid)
            if member is not None and member.bot:
                continue
            ids.append(int(uid))
        return ids
    return [
        int(m.id)
        for m in getattr(fresh_ch, "members", [])
        if not m.bot and m.id != bot_id
    ]


def _count_humans_in_channel(
    guild: discord.Guild | None,
    channel: discord.abc.Snowflake | None,
    bot_id: int,
) -> int:
    """Zählt echte (nicht-Bot) Nutzer im Voice-Kanal über Gateway voice_states + Member-Liste."""
    return len(_human_user_ids_in_channel(guild, channel, bot_id))


def claim_or_check_dj(
    state: GuildMusicState,
    user: discord.abc.User,
    user_vc: discord.abc.Snowflake | None,
    *,
    require_dj: bool,
) -> str | None:
    """
    Prüft oder vergibt die exklusive DJ-Kontrolle für die laufende Voice-Session:
    - Ist der Bot noch in keinem Voice-Kanal, erhält `user` als erste Person die Kontrolle (`dj_user_id`).
    - Ist der Bot bereits in einem Kanal aktiv, muss `user` im selben Voice-Kanal sein.
    - Bei `require_dj=True` (Buttons, Skip, Pause, Stop, Loop, Shuffle, Volume, Radio) darf NUR
      die Person steuern, die die Musik zuerst angemacht hat (sofern sie noch im Kanal ist).
    """
    guild = state.bot.get_guild(state.guild_id)
    bot_id = state.bot.user.id if state.bot.user else 0
    bot_ch = state.active_channel()

    if user_vc is None:
        return "❌ Du musst in einem Voice-Kanal sein!"

    if bot_ch is None:
        # Neue Session: Die Person, die zuerst Musik anmacht, bekommt alleinige Kontrolle!
        state.dj_user_id = int(user.id)
        return None

    if bot_ch.id != user_vc.id:
        return f"❌ Der Bot spielt bereits in <#{bot_ch.id}>! Komm in denselben Kanal."

    humans_in_vc = _human_user_ids_in_channel(guild, bot_ch, bot_id)
    if state.dj_user_id == 0 or (humans_in_vc and state.dj_user_id not in humans_in_vc):
        # Falls noch kein DJ gesetzt war oder der bisherige DJ den Kanal verlassen hat -> Übergabe!
        state.dj_user_id = int(user.id)
        state.persist_session()
        return None

    if require_dj and int(user.id) != state.dj_user_id:
        return (
            f"❌ Nur <@{state.dj_user_id}> kann den Musik-Player bedienen "
            "(hat die Musik in dieser Runde gestartet)!"
        )
    return None


async def ensure_voice_connected(
    state: GuildMusicState,
    target_channel: discord.VoiceChannel | discord.StageChannel,
) -> discord.VoiceClient:
    """Verbindet oder verschiebt den VoiceClient sauber (inkl. Sync mit guild.voice_client)."""
    guild = target_channel.guild
    state._intentional_disconnect = False
    state.last_voice_channel_id = target_channel.id
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

    vc = await target_channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
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


async def ytdl_extract(query: str, options: dict[str, Any], cache_tag: str = "search") -> dict[str, Any] | None:
    if not YTDLP_AVAILABLE or not runtime_settings.get("music_enabled", MUSIC_ENABLED):
        return None
    opts = {**YDL_TIMEOUTS, **_cookie_and_pot_opts(), **options}
    loop = asyncio.get_running_loop()

    def _run():
        key = f"{cache_tag}:{opts.get('extract_flat')}:{opts.get('playlistend')}"
        try:
            ydl = _get_thread_ydl(key, opts)
            return ydl.extract_info(query, download=False)
        except Exception:
            with yt_dlp.YoutubeDL(opts) as fresh_ydl:
                return fresh_ydl.extract_info(query, download=False)

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        print(f"⚠️ [Music] yt-dlp Extract-Fehler: {e}")
        return None


async def search_tracks(query: str, limit: int = 5) -> list[dict[str, Any]]:
    q = query if _is_url(query) else f"ytsearch{limit}:{query}"
    info = await ytdl_extract(q, YTDL_SEARCH_OPTIONS, cache_tag="flat_search")
    if not info:
        return []
    if "entries" in info:
        return [e for e in (info["entries"] or []) if e]
    return [info]


async def extract_playlist(url: str) -> list[dict[str, Any]]:
    info = await ytdl_extract(url, YTDL_PLAYLIST_OPTIONS, cache_tag="flat_playlist")
    if not info or "entries" not in info:
        return []
    return [e for e in (info["entries"] or []) if e]


def _resolve_with_client(
    target_query_or_url: str,
    extra_opts: dict[str, Any] | None = None,
    client_idx: int = 0,
) -> tuple[str | None, dict | None]:
    ydl_opts: dict[str, Any] = {
        "format": BEST_AUDIO_FORMAT,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch",
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

    cache_key = f"stream_resolve:{client_idx}"
    try:
        ydl = _get_thread_ydl(cache_key, ydl_opts)
        info = ydl.extract_info(target_query_or_url, download=False)
    except Exception:
        with yt_dlp.YoutubeDL(ydl_opts) as fresh_ydl:
            info = fresh_ydl.extract_info(target_query_or_url, download=False)

    if not info:
        return None, None
    if info.get("url") and "entries" not in info:
        return info["url"], info
    entries = info.get("entries") or []
    if entries and entries[0]:
        first = entries[0]
        if first.get("url"):
            return first["url"], first
        return None, first
    return info.get("url"), info


async def resolve_first_track_fast(
    query: str,
    requester: discord.abc.User | None,
) -> SongInfo | None:
    """
    Löst Suche + direkten Opus/M4A-Audio-Stream in EINEM einzigen Schritt auf.
    Spart beim Start eines neuen Songs einen kompletten zweiten yt-dlp-Aufruf (~50% schneller).
    """
    if not YTDLP_AVAILABLE or not runtime_settings.get("music_enabled", MUSIC_ENABLED):
        return None
    q = query if _is_url(query) else f"ytsearch1:{query}"
    loop = asyncio.get_running_loop()
    for idx, extra_opts in enumerate(YDL_PLAYER_CLIENT_FALLBACKS):
        try:
            stream_url, info = await loop.run_in_executor(
                None, _resolve_with_client, q, extra_opts, idx
            )
        except Exception:
            continue
        if info:
            ref = _get_reference_url(info, query)
            if ref or stream_url:
                return SongInfo(
                    title=str(info.get("title") or "Unbekannter Titel"),
                    url=stream_url or "",
                    webpage_url=ref or (query if _is_url(query) else ""),
                    duration=int(info.get("duration") or 0),
                    thumbnail=_entry_thumbnail(info),
                    requester=requester,
                    resolved_at=time.monotonic() if stream_url else 0.0,
                )
    return None


async def _prefetch_song_stream(song: SongInfo) -> None:
    """Löst im Hintergrund bereits den direkten Audio-Stream des nächsten Queue-Songs auf."""
    if song.is_radio or song.has_fresh_stream_url() or not song.webpage_url:
        return
    loop = asyncio.get_running_loop()
    for idx, extra_opts in enumerate(YDL_PLAYER_CLIENT_FALLBACKS):
        try:
            stream_url, info = await loop.run_in_executor(
                None, _resolve_with_client, song.webpage_url, extra_opts, idx
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        if stream_url:
            song.url = stream_url
            song.resolved_at = time.monotonic()
            if info:
                if not song.duration and info.get("duration"):
                    song.duration = int(info["duration"])
                if not song.thumbnail:
                    song.thumbnail = _entry_thumbnail(info)
            return


def schedule_next_prefetch(state: GuildMusicState) -> None:
    """Startet asynchrones Vorladen für den nächsten Song in `state.queue[0]`."""
    if not state.queue:
        return
    nxt = state.queue[0]
    if nxt.is_radio or nxt.has_fresh_stream_url() or not nxt.webpage_url:
        return
    if state._prefetch_task and not state._prefetch_task.done():
        return
    state._prefetch_task = asyncio.create_task(_prefetch_song_stream(nxt))


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

def _make_volume_bar(pct: int) -> str:
    icon = "🔇" if pct <= 10 else "🔉" if pct < 75 else "🔊"
    ratio = min(max(pct / 100.0, 0.0), 1.0)
    filled = round(ratio * 10)
    bar = "█" * filled + "░" * (10 - filled)
    boost = " `⚡BOOST`" if pct > 100 else ""
    return f"{icon} `{bar}` **{pct}%**{boost}"


def _create_audio_source(
    stream_url: str,
    base_ffmpeg_opts: dict[str, str],
    volume_pct: int,
) -> discord.AudioSource:
    vol_float = max(0.05, min(2.0, volume_pct / 100.0))
    raw = discord.FFmpegPCMAudio(stream_url, **base_ffmpeg_opts)
    if hasattr(discord, "PCMVolumeTransformer"):
        try:
            return discord.PCMVolumeTransformer(raw, volume=vol_float)
        except Exception:
            pass
    # Fallback über FFmpeg-Filter falls PCMVolumeTransformer auf einer Plattform nicht verfügbar ist
    fallback_opts = {
        "before_options": base_ffmpeg_opts.get("before_options", ""),
        "options": f"{base_ffmpeg_opts.get('options', '-vn')} -filter:a volume={vol_float:.2f}".strip(),
    }
    return discord.FFmpegPCMAudio(stream_url, **fallback_opts)


async def _try_start_ffmpeg_stream(
    state: GuildMusicState,
    song: SongInfo,
    stream_url: str,
    ffmpeg_opts: dict[str, str],
    after_cb,
    verify_seconds: float = 0.55,
) -> bool:
    """Startet FFmpeg und prüft kurz (~0.55s), ob kein sofortiger HTTP-403/Verbindungsabbruch auftritt."""
    if not state.voice_client or not state.voice_client.is_connected():
        return False

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
        source = _create_audio_source(stream_url, ffmpeg_opts, state.volume_pct)
        state.voice_client.play(source, after=_wrapped_after)
    except Exception:
        return False

    try:
        await asyncio.wait_for(done_flag.wait(), timeout=verify_seconds)
        # Innerhalb von verify_seconds beendet -> meist 403 Forbidden -> nächsten Client testen!
        if song.is_radio or song.duration == 0 or song.duration > 3 or early_error:
            return False
        # Ultrakurzer Soundeffekt (<= 3s) regulär beendet -> Callback nachreichen!
        after_cb(None)
        return True
    except asyncio.TimeoutError:
        done_flag.set()
        return True


async def _play_song_robust(
    state: GuildMusicState, song: SongInfo, after_cb
) -> bool:
    if not state.voice_client or not state.voice_client.is_connected():
        return False

    if song.is_radio and song.url:
        return await _try_start_ffmpeg_stream(
            state, song, song.url, RADIO_FFMPEG_OPTIONS, after_cb, verify_seconds=0.8
        )

    # 1) Blitzstart: Bereits vorab aufgelöste Stream-URL (von /play Direktauflösung oder Queue-Prefetch!)
    if song.has_fresh_stream_url():
        if await _try_start_ffmpeg_stream(
            state, song, song.url, FFMPEG_OPTIONS, after_cb, verify_seconds=0.55
        ):
            return True
        # Falls abgelaufen -> frisch auflösen
        song.url = ""
        song.resolved_at = 0.0

    # 2) Frische Auflösung mit optimierter Client-Reihenfolge
    loop = asyncio.get_running_loop()
    for idx, extra_opts in enumerate(YDL_PLAYER_CLIENT_FALLBACKS):
        if not state.voice_client or not state.voice_client.is_connected():
            return False
        try:
            stream_url, info = await loop.run_in_executor(
                None, _resolve_with_client, song.webpage_url, extra_opts, idx
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

        if await _try_start_ffmpeg_stream(
            state, song, stream_url, FFMPEG_OPTIONS, after_cb, verify_seconds=0.55
        ):
            song.url = stream_url
            song.resolved_at = time.monotonic()
            return True

    return False


async def _wait_or_recover_voice_connection(state: GuildMusicState) -> discord.VoiceClient | None:
    """
    Fängt kurze Discord-Voice-Gateway-Reconnects (1–5s) ab, ohne die Session sofort zu zerstören,
    und stellt die Verbindung bei Bedarf automatisch wieder her, solange Zuhörer im Kanal sind.
    """
    guild = state.bot.get_guild(state.guild_id)
    vc = (guild.voice_client if guild else None) or state.voice_client
    if vc is not None and vc.is_connected():
        state.voice_client = vc
        return vc

    if state._intentional_disconnect:
        return None

    # Bis zu 5 Sekunden auf laufenden internen discord.py Voice-Reconnect warten
    for _ in range(10):
        await asyncio.sleep(0.5)
        if state._intentional_disconnect:
            return None
        guild = state.bot.get_guild(state.guild_id)
        vc = (guild.voice_client if guild else None) or state.voice_client
        if vc is not None and vc.is_connected():
            state.voice_client = vc
            return vc

    # Falls weiterhin getrennt, aber Zuhörer noch im letzten Kanal warten -> sauber neu verbinden!
    if guild is not None and state.last_voice_channel_id:
        target = guild.get_channel(state.last_voice_channel_id)
        bot_id = state.bot.user.id if state.bot.user else 0
        if (
            isinstance(target, (discord.VoiceChannel, discord.StageChannel))
            and _count_humans_in_channel(guild, target, bot_id) > 0
            and (time.monotonic() - state._last_auto_reconnect_ts) > 15.0
        ):
            state._last_auto_reconnect_ts = time.monotonic()
            try:
                print(f"🔄 [Music] Voice-Verbindung in #{target.name} wird automatisch wiederhergestellt...")
                return await ensure_voice_connected(state, target)
            except Exception as e:
                print(f"⚠️ [Music] Auto-Reconnect fehlgeschlagen: {e}")
    return None


async def play_next(state: GuildMusicState, text_channel: discord.abc.Messageable | None = None) -> None:
    if text_channel is not None:
        state.text_channel = text_channel
    channel = state.text_channel

    async with state._lock:
        guild = state.bot.get_guild(state.guild_id)
        vc = await _wait_or_recover_voice_connection(state)

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
                clear_active_music_session(state.guild_id)
                await state.clear_np_buttons_silently()
                await state.restore_default_presence()
                # Falls inzwischen auch niemand mehr im Kanal ist -> sofort lautlos leaven!
                bot_id = state.bot.user.id if state.bot.user else 0
                if guild and _count_humans_in_channel(guild, vc.channel, bot_id) == 0:
                    await state.cleanup()
                return

            state.current = next_song
            state.reset_idle()
            state.persist_session()
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

        # Nächsten Queue-Song im Hintergrund vorladen -> 0s Ladezeit beim nächsten Lied!
        schedule_next_prefetch(state)

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
    if state.dj_user_id:
        embed.add_field(name="🎛️ Kontrolle (DJ)", value=f"<@{state.dj_user_id}>", inline=True)
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    loop_labels = {"off": "Aus", "track": "🔂 Song", "queue": "🔁 Queue"}
    status_str = "⏸️ Pausiert" if (state.voice_client and state.voice_client.is_paused()) else "▶️ Spielt"
    embed.set_footer(
        text=(
            f"{status_str} • 🔊 {state.volume_pct}% • Loop: {loop_labels.get(state.loop_mode, 'Aus')} • "
            f"Autoplay: {'✅ An' if state.autoplay else '❌ Aus'}"
        )
    )
    return embed


# ── Discord UI Views (Player Controls, Queue Pagination, Search, Radio, Favoriten) ───

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

        self.btn_vol_up.label = f"{self.state.volume_pct}% +"
        self.btn_autoplay.label = "Autoplay: An" if self.state.autoplay else "Autoplay: Aus"
        self.btn_autoplay.style = (
            discord.ButtonStyle.success if self.state.autoplay else discord.ButtonStyle.secondary
        )

    async def _check_vc(self, interaction: discord.Interaction) -> bool:
        if not self.state.voice_client or not self.state.voice_client.is_connected():
            await interaction.response.send_message("❌ Keine aktive Wiedergabe.", ephemeral=True)
            return False
        user_vc = (
            interaction.user.voice.channel
            if isinstance(interaction.user, discord.Member) and interaction.user.voice
            else None
        )
        err = claim_or_check_dj(self.state, interaction.user, user_vc, require_dj=True)
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return False
        return True

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
        set_guild_music_prefs(self.state.guild_id, loop_mode=self.state.loop_mode)
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
        self.state.persist_session()
        schedule_next_prefetch(self.state)
        await interaction.response.send_message(
            f"🔀 **{len(qlist)}** Songs wurden gemischt!", ephemeral=True, delete_after=5
        )

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
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

    @discord.ui.button(label="-", emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        self.state.set_volume(_step_volume(self.state.volume_pct, -10))
        self._sync_labels()
        await interaction.response.edit_message(embed=build_now_playing_embed(self.state), view=self)

    @discord.ui.button(label="100% +", emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        self.state.set_volume(_step_volume(self.state.volume_pct, +10))
        self._sync_labels()
        await interaction.response.edit_message(embed=build_now_playing_embed(self.state), view=self)

    @discord.ui.button(label="Favorit", emoji="⭐", style=discord.ButtonStyle.secondary, row=1)
    async def btn_favorite(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        song = self.state.current
        if not song:
            await interaction.response.send_message("❌ Es läuft gerade kein Song.", ephemeral=True)
            return
        fav_url = song.url if song.is_radio else (song.webpage_url or song.url)
        if not fav_url:
            await interaction.response.send_message("❌ Dieser Stream hat keine speicherbare URL.", ephemeral=True)
            return
        status, total = toggle_user_favorite(
            interaction.user.id,
            {
                "title": song.title,
                "url": fav_url,
                "duration": song.duration,
                "thumbnail": song.thumbnail,
                "is_radio": song.is_radio,
            },
        )
        if status == "added":
            await interaction.response.send_message(
                f"⭐ **{song.title}** wurde zu deinen Favoriten hinzugefügt! (`{total}/25` • `/fav`)",
                ephemeral=True,
            )
        elif status == "removed":
            await interaction.response.send_message(
                f"🗑️ **{song.title}** wurde aus deinen Favoriten entfernt. (`{total}/25`)",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Deine Favoritenliste ist voll (`25/25`). Nutze `/fav`, um alte Einträge zu löschen.",
                ephemeral=True,
            )

    @discord.ui.button(label="Autoplay: Aus", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def btn_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check_vc(interaction):
            return
        self.state.autoplay = not self.state.autoplay
        set_guild_music_prefs(self.state.guild_id, autoplay=self.state.autoplay)
        self._sync_labels()
        await interaction.response.edit_message(embed=build_now_playing_embed(self.state), view=self)


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

        state = get_music_state(interaction.guild_id, self.bot)
        vc_target = interaction.user.voice.channel
        err = claim_or_check_dj(
            state,
            interaction.user,
            vc_target,
            require_dj=bool(state.current and state.current.is_radio),
        )
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return

        await interaction.response.defer()
        entry = self.results[int(self.picker.values[0])]

        try:
            await ensure_voice_connected(state, vc_target)
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}", embed=None, view=None
            )
            return

        if state.current and state.current.is_radio:
            state.current = None
            if state.voice_client:
                state.voice_client.stop()
            await asyncio.sleep(0.2)

        song = SongInfo(
            title=entry.get("title", "Unbekannt"),
            url="",
            webpage_url=_get_reference_url(entry),
            duration=int(entry.get("duration") or 0),
            thumbnail=_entry_thumbnail(entry),
            requester=interaction.user,
        )
        state.queue.append(song)
        state.persist_session()
        await interaction.edit_original_response(
            content=f"✅ **{song.title}** ausgewählt!", embed=None, view=None
        )
        if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
            await play_next(state, interaction.channel)
        else:
            schedule_next_prefetch(state)


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
    err = claim_or_check_dj(state, interaction.user, user_vc, require_dj=True)
    if err is not None:
        if interaction.response.is_done():
            await interaction.followup.send(err, ephemeral=True)
        else:
            await interaction.response.send_message(err, ephemeral=True)
        return

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
        self._sessions_restored: bool = False

    async def cog_load(self) -> None:
        self.music_idle_checker.start()

    async def cog_unload(self) -> None:
        self.music_idle_checker.cancel()
        for state in list(music_states.values()):
            state.persist_session()
            await state.cleanup(clear_saved_session=False)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._sessions_restored:
            return
        self._sessions_restored = True
        sessions = load_active_music_sessions()
        if not sessions:
            return
        bot_id = self.bot.user.id if self.bot.user else 0
        for gid, sess in sessions.items():
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            vc_ch = guild.get_channel(int(sess.get("voice_channel_id") or 0))
            human_ids = _human_user_ids_in_channel(guild, vc_ch, bot_id) if isinstance(vc_ch, (discord.VoiceChannel, discord.StageChannel)) else []
            if not isinstance(vc_ch, (discord.VoiceChannel, discord.StageChannel)) or not human_ids:
                clear_active_music_session(gid)
                continue
            try:
                state = get_music_state(gid, self.bot)
                saved_dj = int(sess.get("dj_user_id") or 0)
                state.dj_user_id = saved_dj if saved_dj in human_ids else human_ids[0]
                await ensure_voice_connected(state, vc_ch)
                tc = guild.get_channel(int(sess.get("text_channel_id") or 0))
                if tc is not None:
                    state.text_channel = tc  # type: ignore[assignment]
                if sess.get("current"):
                    state.queue.append(GuildMusicState._deserialize_song(sess["current"]))
                for q_item in sess.get("queue") or []:
                    state.queue.append(GuildMusicState._deserialize_song(q_item))
                if state.queue:
                    print(f"🎵 [Music] Stelle unterbrochene Musik-Sitzung in Guild {gid} wieder her...")
                    await play_next(state, state.text_channel)
            except Exception as e:
                print(f"⚠️ [Music] Konnte Musik-Sitzung in Guild {gid} nicht wiederherstellen: {e}")
                clear_active_music_session(gid)

    def active_player_count(self) -> int:
        return sum(1 for vc in self.bot.voice_clients if vc.is_connected())

    async def _leave_if_still_empty(self, guild_id: int) -> None:
        """Prüft nach 3s Verzögerung erneut, ob der Bot allein im Voice-Kanal ist, und verlässt ihn lautlos."""
        await asyncio.sleep(3.0)
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

    async def _handle_unexpected_bot_voice_drop(
        self,
        guild_id: int,
        old_channel_id: int,
    ) -> None:
        """
        Behandelt unerwartete Voice-Trennungen (z. B. Discord Voice-Server-Wechsel oder UDP/DAVE-Glitch):
        - Wartet kurz auf discord.py's internen Reconnect.
        - Verbindet sich automatisch neu & spielt nahtlos weiter, falls Musik aktiv war und Zuhörer warten.
        - Bereinigt andernfalls sauber die Session (inkl. DJ-Reset für die nächste Person).
        """
        await asyncio.sleep(2.5)
        guild = self.bot.get_guild(guild_id)
        state = music_states.get(guild_id)
        if guild is None or state is None:
            return
        if state._intentional_disconnect:
            return

        # Hat discord.py die Verbindung bereits selbst wiederhergestellt?
        vc = guild.voice_client or state.voice_client
        me_ch = guild.me.voice.channel if (guild.me and guild.me.voice) else None
        if (vc is not None and vc.is_connected()) or me_ch is not None:
            if vc is not None:
                state.voice_client = vc
            return

        # Falls aktive Wiedergabe lief und noch Zuhörer im Kanal sind -> 1x sauber Auto-Reconnect!
        bot_id = self.bot.user.id if self.bot.user else 0
        target_ch = guild.get_channel(old_channel_id)
        has_music = state.current is not None or bool(state.queue)
        if (
            has_music
            and isinstance(target_ch, (discord.VoiceChannel, discord.StageChannel))
            and _count_humans_in_channel(guild, target_ch, bot_id) > 0
            and (time.monotonic() - state._last_auto_reconnect_ts) > 25.0
        ):
            state._last_auto_reconnect_ts = time.monotonic()
            try:
                print(
                    f"🔄 [Music] Unerwarteter Voice-Drop in #{target_ch.name} (Guild {guild_id}) "
                    "– verbinde automatisch neu..."
                )
                interrupted = state.current
                state.current = None
                if interrupted is not None and (not state.queue or state.queue[0] is not interrupted):
                    state.queue.appendleft(interrupted)
                await ensure_voice_connected(state, target_ch)
                if not state._lock.locked():
                    await play_next(state, state.text_channel)
                return
            except Exception as e:
                print(f"⚠️ [Music] Auto-Recovery nach Voice-Drop fehlgeschlagen: {e}")

        await state.cleanup(disconnect_voice=False)

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
                    if state._intentional_disconnect:
                        await state.cleanup(disconnect_voice=False)
                    else:
                        asyncio.create_task(
                            self._handle_unexpected_bot_voice_drop(member.guild.id, before.channel.id)
                        )
            elif after.channel is not None:
                state = get_music_state(member.guild.id, self.bot)
                state.last_voice_channel_id = after.channel.id
                if member.guild.voice_client is not None:
                    state.voice_client = member.guild.voice_client
                if _count_humans_in_channel(member.guild, after.channel, bot_id) == 0:
                    asyncio.create_task(self._leave_if_still_empty(member.guild.id))
            return

        # 2) Ein menschlicher User hat einen Kanal verlassen oder gewechselt -> sofort prüfen!
        if not member.bot and before.channel is not None:
            if after.channel is None or after.channel.id != before.channel.id:
                guild = member.guild
                state = music_states.get(guild.id)
                vc = guild.voice_client or (state.voice_client if state else None)
                bot_ch = (
                    (guild.me.voice.channel if guild.me and guild.me.voice else None)
                    or (vc.channel if vc else None)
                )
                if bot_ch is not None and bot_ch.id == before.channel.id:
                    remaining_humans = _human_user_ids_in_channel(guild, bot_ch, bot_id)
                    if not remaining_humans:
                        asyncio.create_task(self._leave_if_still_empty(guild.id))
                    elif state and state.dj_user_id == member.id:
                        # Der bisherige DJ hat den Kanal verlassen -> Kontrolle an nächsten Zuhörer übergeben
                        state.dj_user_id = remaining_humans[0]
                        state.persist_session()
                        if state.last_np_message and state.current:
                            try:
                                await state.last_np_message.edit(
                                    embed=build_now_playing_embed(state),
                                    view=MusicPlayerControlView(state),
                                )
                            except Exception:
                                pass

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

                # Während eines laufenden Voice-Reconnects nicht vorschnell abbrechen
                if vc is not None and not vc.is_connected() and not state._intentional_disconnect:
                    if (state.current is not None or state.queue) and state._idle_seconds < 10:
                        state.increment_idle(2)
                        continue

                # Fall A: Niemand (kein echter Mensch) im Voice-Kanal -> nach 2 Ticks (4s) lautlos leaven!
                human_ids = _human_user_ids_in_channel(guild, bot_ch, bot_id)
                if not human_ids:
                    state._empty_seconds += 2
                    if state._empty_seconds >= 4:
                        print(f"🎵 [Music] Niemand im Voice-Kanal (Guild {guild.id}) – verlasse Kanal lautlos.")
                        await state.cleanup()
                    continue

                state._empty_seconds = 0
                if state.dj_user_id == 0 or state.dj_user_id not in human_ids:
                    state.dj_user_id = human_ids[0]

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

        user_vc = interaction.user.voice.channel
        state = get_music_state(interaction.guild_id, self.bot)

        # DJ-Check: Erste Person bekommt die alleinige Steuerung; Kanalwechsel oder Radio-Abbruch durch Fremde blockiert!
        err = claim_or_check_dj(
            state,
            interaction.user,
            user_vc,
            require_dj=bool(state.current and state.current.is_radio),
        )
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return

        perms = user_vc.permissions_for(interaction.guild.me)
        if not perms.connect or not perms.speak:
            await interaction.response.send_message(
                f"❌ Keine Berechtigung (`Verbinden` & `Sprechen`) für **{user_vc.name}**!",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        state.is_loading = True
        try:
            try:
                await ensure_voice_connected(state, user_vc)
            except Exception as e:
                await interaction.followup.send(f"❌ Konnte dem Voice-Kanal nicht beitreten: {e}")
                return

            # Falls Radio lief -> beenden für reguläre Queue (nur DJ darf das, oben geprüft)
            if state.current and state.current.is_radio:
                state.current = None
                if state.voice_client:
                    state.voice_client.stop()
                await asyncio.sleep(0.2)

            state.reset_idle()
            idle_now = (
                state.voice_client is not None
                and not state.voice_client.is_playing()
                and not state.voice_client.is_paused()
                and state.current is None
                and not state.queue
            )

            # 1) Spotify
            if is_spotify_url(query):
                sp_tracks = await fetch_spotify_tracks(query)
                if not sp_tracks:
                    await interaction.followup.send("❌ Konnte keine Songs von Spotify laden.")
                    return
                if len(sp_tracks) == 1:
                    sp = sp_tracks[0]
                    if idle_now:
                        song = await resolve_first_track_fast(sp["search_query"], interaction.user)
                        if not song:
                            await interaction.followup.send(f"❌ Konnte **{sp['title']}** nicht auf YouTube finden.")
                            return
                        song.title = sp["title"]
                        if sp.get("duration"):
                            song.duration = int(sp["duration"])
                        if sp.get("thumbnail"):
                            song.thumbnail = sp["thumbnail"]
                    else:
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
                    state.persist_session()
                    if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                        msg = await interaction.followup.send(f"🟢 **{sp['title']}** startet...", wait=True)
                        try:
                            await msg.delete(delay=3)
                        except Exception:
                            pass
                        await play_next(state, interaction.channel)
                    else:
                        schedule_next_prefetch(state)
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

                # Spotify Playlist / Album (ersten Track sofort direkt auflösen, Rest asynchron)
                first = sp_tracks[0]
                if idle_now:
                    first_song = await resolve_first_track_fast(first["search_query"], interaction.user)
                    if first_song:
                        first_song.title = first["title"]
                        if first.get("duration"):
                            first_song.duration = int(first["duration"])
                        if first.get("thumbnail"):
                            first_song.thumbnail = first["thumbnail"]
                        state.queue.append(first_song)
                else:
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
                            schedule_next_prefetch(state)
                        await asyncio.sleep(0.2)
                    state.persist_session()

                asyncio.create_task(_queue_remaining())
                embed = discord.Embed(
                    title="🟢 Spotify Playlist/Album erkannt!",
                    description=f"**{len(sp_tracks)}** Songs werden zur Warteschlange hinzugefügt.",
                    color=COLOR_SPOTIFY,
                )
                await interaction.followup.send(embed=embed)
                if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                    await play_next(state, interaction.channel)
                else:
                    schedule_next_prefetch(state)
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
                state.persist_session()
                embed = discord.Embed(
                    title="📋 Playlist hinzugefügt",
                    description=f"**{added}** Songs zur Warteschlange hinzugefügt!",
                    color=COLOR_MUSIC,
                )
                await interaction.followup.send(embed=embed)
                if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                    await play_next(state, interaction.channel)
                else:
                    schedule_next_prefetch(state)
                return

            # 3) Einzelner Song / Direktlink
            if idle_now:
                # 1-Schritt-Schnellstart: Suche + direkter Opus/M4A-Stream in einem Aufruf!
                song = await resolve_first_track_fast(query, interaction.user)
                if not song:
                    await interaction.followup.send(f"❌ Keine Ergebnisse für: `{query}`")
                    return
            else:
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
            state.persist_session()

            if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
                msg = await interaction.followup.send(f"🎵 **{song.title}** startet...", wait=True)
                try:
                    await msg.delete(delay=3)
                except Exception:
                    pass
                await play_next(state, interaction.channel)
            else:
                schedule_next_prefetch(state)
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
        user_vc = (
            interaction.user.voice.channel
            if isinstance(interaction.user, discord.Member) and interaction.user.voice
            else None
        )
        err = claim_or_check_dj(state, interaction.user, user_vc, require_dj=True)
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
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
        user_vc = (
            interaction.user.voice.channel
            if isinstance(interaction.user, discord.Member) and interaction.user.voice
            else None
        )
        err = claim_or_check_dj(state, interaction.user, user_vc, require_dj=True)
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
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
        if state.active_channel() is not None:
            user_vc = (
                interaction.user.voice.channel
                if isinstance(interaction.user, discord.Member) and interaction.user.voice
                else None
            )
            err = claim_or_check_dj(state, interaction.user, user_vc, require_dj=True)
            if err is not None:
                await interaction.response.send_message(err, ephemeral=True)
                return
        if len(state.queue) < 2:
            await interaction.response.send_message("❌ Zu wenig Songs zum Mischen.", ephemeral=True)
            return
        qlist = list(state.queue)
        random.shuffle(qlist)
        state.queue = deque(qlist)
        state.persist_session()
        schedule_next_prefetch(state)
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
        if state.active_channel() is not None:
            user_vc = (
                interaction.user.voice.channel
                if isinstance(interaction.user, discord.Member) and interaction.user.voice
                else None
            )
            err = claim_or_check_dj(state, interaction.user, user_vc, require_dj=True)
            if err is not None:
                await interaction.response.send_message(err, ephemeral=True)
                return
        state.autoplay = mode == "on"
        set_guild_music_prefs(interaction.guild_id, autoplay=state.autoplay)
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

        state = get_music_state(interaction.guild_id, self.bot)
        err = claim_or_check_dj(state, interaction.user, interaction.user.voice.channel, require_dj=True)
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
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
    async def cmd_stations(self, interaction: discord.Interaction, ) -> None:
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

    # ── Slash Commands: Lautstärke & Favoriten ────────────────────────────

    @app_commands.command(
        name="volume",
        description="🔊 Ändert die Musik-Lautstärke live (5% – 200%) oder zeigt sie an",
    )
    @app_commands.describe(prozent="Gewünschte Lautstärke in Prozent (5–200, Standard: 100)")
    async def cmd_volume(
        self,
        interaction: discord.Interaction,
        prozent: app_commands.Range[int, 5, 200] | None = None,
    ) -> None:
        state = get_music_state(interaction.guild_id, self.bot)
        if prozent is None:
            embed = discord.Embed(
                title="🔊 Aktuelle Lautstärke",
                description=_make_volume_bar(state.volume_pct),
                color=COLOR_MUSIC,
            )
            embed.set_footer(text="Tipp: /volume <5–200> zum Anpassen • Einstellung wird gespeichert")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if state.active_channel() is not None:
            user_vc = (
                interaction.user.voice.channel
                if isinstance(interaction.user, discord.Member) and interaction.user.voice
                else None
            )
            err = claim_or_check_dj(state, interaction.user, user_vc, require_dj=True)
            if err is not None:
                await interaction.response.send_message(err, ephemeral=True)
                return

        new_pct = state.set_volume(prozent)
        if state.last_np_message is not None and state.current is not None:
            try:
                await state.last_np_message.edit(
                    embed=build_now_playing_embed(state),
                    view=MusicPlayerControlView(state),
                )
            except Exception:
                pass

        embed = discord.Embed(
            title="🔊 Lautstärke angepasst",
            description=_make_volume_bar(new_pct),
            color=COLOR_MUSIC,
        )
        embed.set_footer(text=f"Gesetzt von {interaction.user.display_name} • Für diesen Server gemerkt")
        await interaction.response.send_message(embed=embed, delete_after=10)

    @app_commands.command(
        name="fav",
        description="⭐ Öffnet deine persönlichen Musik- & Radio-Favoriten zum direkten Abspielen",
    )
    async def cmd_fav(self, interaction: discord.Interaction) -> None:
        favs = get_user_favorites(interaction.user.id)
        view = FavoritesView(interaction.user, self.bot, favs)
        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="favadd",
        description="⭐ Speichert den aktuellen Song oder eine Suche/URL in deinen Favoriten",
    )
    @app_commands.describe(query="Optional: Songtitel, YouTube- oder Spotify-URL (leer = aktueller Song)")
    async def cmd_favadd(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
    ) -> None:
        # 1) Ohne Query: aktuell laufenden Song / Radiosender speichern
        if not query or not query.strip():
            state = get_music_state(interaction.guild_id, self.bot)
            song = state.current
            if not song:
                await interaction.response.send_message(
                    "❌ Es läuft gerade kein Song! Übergib alternativ einen Titel/Link: `/favadd <song>`.",
                    ephemeral=True,
                )
                return
            fav_url = song.url if song.is_radio else (song.webpage_url or song.url)
            status, total = toggle_user_favorite(
                interaction.user.id,
                {
                    "title": song.title,
                    "url": fav_url,
                    "duration": song.duration,
                    "thumbnail": song.thumbnail,
                    "is_radio": song.is_radio,
                },
            )
            if status == "added":
                await interaction.response.send_message(
                    f"⭐ **{song.title}** zu deinen Favoriten hinzugefügt! (`{total}/25` • `/fav`)",
                    ephemeral=True,
                )
            elif status == "removed":
                await interaction.response.send_message(
                    f"🗑️ **{song.title}** war bereits gespeichert und wurde entfernt (`{total}/25`).",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Deine Favoritenliste ist voll (`25/25`). Öffne `/fav` zum Verwalten.",
                    ephemeral=True,
                )
            return

        # 2) Mit Query: auf Spotify / YouTube suchen und direkt speichern
        await interaction.response.defer(ephemeral=True)
        q = query.strip()
        title = ""
        url = ""
        dur = 0
        thumb = ""

        if is_spotify_url(q):
            sp_tracks = await fetch_spotify_tracks(q)
            if sp_tracks:
                sp = sp_tracks[0]
                res = await search_tracks(sp["search_query"], limit=1)
                if res:
                    e0 = res[0]
                    title = sp["title"]
                    url = _get_reference_url(e0)
                    dur = sp.get("duration") or int(e0.get("duration") or 0)
                    thumb = sp.get("thumbnail") or _entry_thumbnail(e0)
        else:
            res = await search_tracks(q, limit=1)
            if res:
                e0 = res[0]
                title = e0.get("title", "Unbekannt")
                url = _get_reference_url(e0, q)
                dur = int(e0.get("duration") or 0)
                thumb = _entry_thumbnail(e0)

        if not url:
            await interaction.followup.send(f"❌ Konnte keinen Song für `{q}` finden.", ephemeral=True)
            return

        from bot_core.storage import is_user_favorite
        if is_user_favorite(interaction.user.id, url):
            await interaction.followup.send(
                f"ℹ️ **{title}** befindet sich bereits in deinen Favoriten (`/fav`).",
                ephemeral=True,
            )
            return

        status, total = toggle_user_favorite(
            interaction.user.id,
            {"title": title, "url": url, "duration": dur, "thumbnail": thumb, "is_radio": False},
        )
        if status == "full":
            await interaction.followup.send(
                "❌ Deine Favoritenliste ist voll (`25/25`). Öffne `/fav`, um Platz zu schaffen.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="⭐ Favorit gespeichert!",
            description=f"[{title}]({url})",
            color=COLOR_GOLD,
        )
        m, s = divmod(dur, 60)
        embed.add_field(name="⏱️ Dauer", value=f"{m}:{s:02d}" if dur > 0 else "Live", inline=True)
        embed.add_field(name="📚 Gespeichert", value=f"`{total}/25`", inline=True)
        if thumb:
            embed.set_thumbnail(url=thumb)
        embed.set_footer(text="Nutze /fav zum Abspielen deiner gesamten Liste")
        await interaction.followup.send(embed=embed, ephemeral=True)


class FavoritesView(discord.ui.View):
    """Interaktives Favoriten-Panel: Einzelwahl, Alle abspielen, Shuffle & Löschen."""

    def __init__(
        self,
        user: discord.abc.User,
        bot: commands.Bot,
        favs: list[dict[str, Any]],
        delete_mode: bool = False,
    ) -> None:
        super().__init__(timeout=180)
        self.user = user
        self.bot = bot
        self.favs = favs
        self.delete_mode = delete_mode
        self._rebuild_items()

    @staticmethod
    def _fmt_dur(dur: int, is_radio: bool) -> str:
        if is_radio or dur <= 0:
            return "📻 LIVE"
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"⭐ Musik-Favoriten – {self.user.display_name}",
            color=COLOR_ERROR if self.delete_mode else COLOR_GOLD,
        )
        if not self.favs:
            embed.description = (
                "*Du hast noch keine Favoriten gespeichert.*\n\n"
                "💡 **So speicherst du Songs & Radiosender:**\n"
                "• Klicke beim Abspielen unter *Jetzt spielt* auf **`⭐ Favorit`**\n"
                "• Oder nutze jederzeit **`/favadd [Song / Link]`**"
            )
            embed.set_footer(text="0/25 Plätze belegt")
            return embed

        mode_hint = (
            "🗑️ **Lösch-Modus aktiv:** Wähle unten einen Eintrag aus, um ihn zu entfernen.\n\n"
            if self.delete_mode
            else ""
        )
        lines = []
        for idx, f in enumerate(self.favs[:25], 1):
            icon = "📻" if f.get("is_radio") else "🎵"
            dur_s = self._fmt_dur(int(f.get("duration") or 0), bool(f.get("is_radio")))
            title = f.get("title", "Unbekannt")
            url = f.get("url", "")
            link_str = f"[{title}]({url})" if url.startswith("http") and not f.get("is_radio") else f"**{title}**"
            lines.append(f"`{idx:02d}.` {icon} {link_str} — `{dur_s}`")

        embed.description = (mode_hint + "\n".join(lines))[:4000]
        embed.set_footer(text=f"{len(self.favs)}/25 Favoriten gespeichert • Klicke unten zum Abspielen")
        return embed

    def _rebuild_items(self) -> None:
        self.clear_items()
        if not self.favs:
            return

        options = []
        for idx, f in enumerate(self.favs[:25]):
            dur_s = self._fmt_dur(int(f.get("duration") or 0), bool(f.get("is_radio")))
            emoji = "🗑️" if self.delete_mode else ("📻" if f.get("is_radio") else "🎵")
            options.append(
                discord.SelectOption(
                    label=f"{idx + 1}. {str(f.get('title', 'Unbekannt'))[:85]}",
                    description=dur_s,
                    emoji=emoji,
                    value=str(idx),
                )
            )

        placeholder = (
            "🗑️ Favorit zum Löschen auswählen..."
            if self.delete_mode
            else "🎵 Favorit auswählen & abspielen..."
        )
        picker = discord.ui.Select(placeholder=placeholder, options=options, row=0)
        picker.callback = self._on_select
        self.add_item(picker)

        btn_all = discord.ui.Button(
            label="Alle in Queue",
            emoji="▶️",
            style=discord.ButtonStyle.success,
            disabled=self.delete_mode,
            row=1,
        )
        btn_all.callback = self._on_play_all
        self.add_item(btn_all)

        btn_shuf = discord.ui.Button(
            label="Gemischt starten",
            emoji="🔀",
            style=discord.ButtonStyle.primary,
            disabled=self.delete_mode or len(self.favs) < 2,
            row=1,
        )
        btn_shuf.callback = self._on_shuffle_all
        self.add_item(btn_shuf)

        btn_del = discord.ui.Button(
            label="Abbrechen" if self.delete_mode else "Verwalten / Löschen",
            emoji="↩️" if self.delete_mode else "🗑️",
            style=discord.ButtonStyle.secondary if self.delete_mode else discord.ButtonStyle.danger,
            row=1,
        )
        btn_del.callback = self._on_toggle_delete
        self.add_item(btn_del)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Nicht deine Favoritenliste!", ephemeral=True)
            return False
        return True

    async def _on_toggle_delete(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        self.delete_mode = not self.delete_mode
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        idx = int(interaction.data["values"][0])  # type: ignore[index]
        if self.delete_mode:
            removed = remove_user_favorite(self.user.id, idx)
            self.favs = get_user_favorites(self.user.id)
            if not self.favs:
                self.delete_mode = False
            self._rebuild_items()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            if removed:
                await interaction.followup.send(
                    f"🗑️ **{removed.get('title', 'Eintrag')}** wurde entfernt.",
                    ephemeral=True,
                )
            return

        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ Du musst in einem Voice-Kanal sein!", ephemeral=True)
            return
        if idx < 0 or idx >= len(self.favs):
            await interaction.response.send_message("❌ Ungültiger Eintrag.", ephemeral=True)
            return

        chosen = self.favs[idx]
        state = get_music_state(interaction.guild_id, self.bot)
        user_vc = interaction.user.voice.channel

        if chosen.get("is_radio"):
            err = claim_or_check_dj(state, interaction.user, user_vc, require_dj=True)
            if err is not None:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            station = {
                "name": str(chosen.get("title", "Radio")).removeprefix("📻 ").strip(),
                "url": chosen["url"],
                "favicon": chosen.get("thumbnail", ""),
            }
            await start_radio_stream(interaction, station, state)
            return

        err = claim_or_check_dj(
            state,
            interaction.user,
            user_vc,
            require_dj=bool(state.current and state.current.is_radio),
        )
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await ensure_voice_connected(state, user_vc)
        except Exception as e:
            await interaction.followup.send(f"❌ Konnte Voice-Kanal nicht beitreten: {e}", ephemeral=True)
            return

        if state.current and state.current.is_radio:
            state.current = None
            if state.voice_client:
                state.voice_client.stop()
            await asyncio.sleep(0.2)

        song = SongInfo(
            title=chosen.get("title", "Unbekannt"),
            url="",
            webpage_url=chosen["url"],
            duration=int(chosen.get("duration") or 0),
            thumbnail=chosen.get("thumbnail", ""),
            requester=interaction.user,
        )
        state.queue.append(song)
        state.persist_session()
        if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
            await play_next(state, interaction.channel)
            await interaction.followup.send(f"▶️ Starte Favorit: **{song.title}**", ephemeral=True)
        else:
            schedule_next_prefetch(state)
            await interaction.followup.send(
                f"➕ Favorit **{song.title}** auf Position `#{len(state.queue)}` eingereiht!",
                ephemeral=True,
            )

    async def _enqueue_all(self, interaction: discord.Interaction, shuffle: bool) -> None:
        if not await self._guard(interaction):
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ Du musst in einem Voice-Kanal sein!", ephemeral=True)
            return

        tracks = [f for f in self.favs if not f.get("is_radio") and f.get("url")]
        if not tracks:
            await interaction.response.send_message(
                "❌ Keine normalen Musik-Tracks in deinen Favoriten (Radiosender bitte einzeln auswählen).",
                ephemeral=True,
            )
            return

        user_vc = interaction.user.voice.channel
        state = get_music_state(interaction.guild_id, self.bot)
        err = claim_or_check_dj(
            state,
            interaction.user,
            user_vc,
            require_dj=bool(state.current and state.current.is_radio),
        )
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await ensure_voice_connected(state, user_vc)
        except Exception as e:
            await interaction.followup.send(f"❌ Konnte Voice-Kanal nicht beitreten: {e}", ephemeral=True)
            return

        if state.current and state.current.is_radio:
            state.current = None
            if state.voice_client:
                state.voice_client.stop()
            await asyncio.sleep(0.2)

        if shuffle:
            random.shuffle(tracks)

        for t in tracks:
            state.queue.append(
                SongInfo(
                    title=t.get("title", "Unbekannt"),
                    url="",
                    webpage_url=t["url"],
                    duration=int(t.get("duration") or 0),
                    thumbnail=t.get("thumbnail", ""),
                    requester=interaction.user,
                )
            )
        state.persist_session()

        if state.voice_client and not state.voice_client.is_playing() and not state.voice_client.is_paused() and state.current is None:
            await play_next(state, interaction.channel)
        else:
            schedule_next_prefetch(state)

        mode_txt = "gemischt " if shuffle else ""
        await interaction.followup.send(
            f"⭐ **{len(tracks)}** Favoriten wurden {mode_txt}zur Warteschlange hinzugefügt!",
            ephemeral=True,
        )

    async def _on_play_all(self, interaction: discord.Interaction) -> None:
        await self._enqueue_all(interaction, shuffle=False)

    async def _on_shuffle_all(self, interaction: discord.Interaction) -> None:
        await self._enqueue_all(interaction, shuffle=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
