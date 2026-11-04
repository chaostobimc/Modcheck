"""Modcheck v2.1 – Modularer Discord- & Twitch-Bot.

Startpunkt für lokales Ausführen sowie für den Raspberry Pi 5 systemd-Dienst:
    python main.py
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import shutil
import subprocess
import sys
import traceback


def _parse_ver(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in v.split(".")[:3]:
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _ensure_compatible_voice_deps() -> None:
    """Stellt vor `import discord` sicher, dass discord.py 2.7.x mit davey 0.1.7 läuft (DAVE E2EE Pflicht)."""
    try:
        dpy_s = importlib.metadata.version("discord.py")
    except Exception:
        dpy_s = "0.0.0"
    try:
        davey_s: str | None = importlib.metadata.version("davey")
    except Exception:
        davey_s = None

    dpy_v = _parse_ver(dpy_s)
    davey_v = _parse_ver(davey_s) if davey_s else (0, 0, 0)
    if dpy_v < (2, 7, 1) or davey_s is None or davey_v < (0, 1, 7) or (dpy_v < (2, 8, 0) and davey_v >= (0, 2, 0)):
        print(
            f"🔧 [Voice-AutoFix] Repariere DAVE-Abhängigkeiten (discord.py={dpy_s}, davey={davey_s}) "
            "-> discord.py[voice]>=2.7.1,<2.8.0 + davey>=0.1.7,<0.2.0 ..."
        )
        try:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "discord.py[voice]>=2.7.1,<2.8.0",
                    "davey>=0.1.7,<0.2.0",
                ]
            )
            print("  ✅ [Voice-AutoFix] davey 0.1.7 erfolgreich installiert.")
        except Exception as exc:
            print(f"  ⚠️ [Voice-AutoFix] Konnte Pakete nicht automatisch anpassen: {exc}")


_ensure_compatible_voice_deps()

import discord
from discord import app_commands
from discord.ext import commands

from bot_core.config import (
    ADMIN_USER_IDS,
    DATA_FILE,
    DISCORD_TOKEN,
    GUILD_ID,
    REVIEW_CHANNEL_ID,
    STREAMER_CHANNEL,
    TEMP_VOICE_CHANNEL_ID,
    TWITCH_LOG_CHANNEL_ID,
    TWITCH_TOKEN,
    runtime_settings,
)
from bot_core.stats import stats_tracker
from bot_core.storage import flush_data
from bot_core.twitch_service import TwitchBot
from cogs import COG_EXTENSIONS


def check_system_requirements() -> bool:
    """Prüft wichtige Laufzeitkomponenten (insb. FFmpeg, PyNaCl & yt-dlp auf dem Raspberry Pi 5)."""
    print("\n── System-Check (Modcheck v2.1) ─────────────")
    v = sys.version_info
    print(f"  🐍 Python:  {v.major}.{v.minor}.{v.micro}")

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        print(f"  🎼 FFmpeg:  ✅ {ffmpeg_path}")
    else:
        print("  🎼 FFmpeg:  ❌ NICHT GEFUNDEN – Musik/Radio benötigt: sudo apt install -y ffmpeg")

    if importlib.util.find_spec("nacl") is not None:
        print("  🔐 PyNaCl:  ✅ Voice-Verschlüsselung bereit")
    else:
        print("  🔐 PyNaCl:  ❌ fehlt – pip install PyNaCl")

    try:
        davey_ver = importlib.metadata.version("davey")
        has_dave = getattr(getattr(discord, "voice_state", None), "has_dave", None)
        print(f"  🛡️ DAVE:    ✅ discord.py {discord.__version__} + davey {davey_ver} (has_dave={has_dave})")
    except Exception:
        print(f"  🛡️ DAVE:    ❌ davey fehlt (discord.py {discord.__version__})")

    try:
        import yt_dlp
        print(f"  🎵 yt-dlp:  ✅ {yt_dlp.version.__version__}")
    except Exception:
        print("  🎵 yt-dlp:  ❌ nicht installiert")

    print("──────────────────────────────────────────────\n")
    return bool(ffmpeg_path)


class ModcheckBot(commands.Bot):
    """Zentrale Bot-Instanz – lädt alle Cogs & synchronisiert den Slash-Command-Tree."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.presences = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        loaded = 0
        for ext in COG_EXTENSIONS:
            try:
                await self.load_extension(ext)
                loaded += 1
                print(f"  🧩 Cog geladen: {ext}")
            except Exception as e:
                print(f"  ❌ Fehler beim Laden von {ext}: {e}")
                traceback.print_exc()

        print(f"  📦 {loaded}/{len(COG_EXTENSIONS)} Cogs aktiv ({len(self.tree.get_commands())} Slash-Commands).")

        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"  ✅ {len(synced)} Slash-Commands sofort für Guild {GUILD_ID} synchronisiert.")
            else:
                synced = await self.tree.sync()
                print(f"  ✅ {len(synced)} globale Slash-Commands synchronisiert.")
        except Exception as e:
            print(f"  ⚠️ Slash-Command-Sync fehlgeschlagen: {e}")

    async def on_ready(self) -> None:
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"twitch.tv/{STREAMER_CHANNEL} • /help" if STREAMER_CHANNEL else "/help",
        )
        try:
            await self.change_presence(activity=activity)
        except Exception:
            pass

        print("\n══════════════════════════════════════════════")
        print(f"  ✅ Discord-Bot online: {self.user} (ID: {self.user.id if self.user else '?'})")
        print(f"  📡 Verbundene Server:  {len(self.guilds)}")
        print("══════════════════════════════════════════════\n")

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Du kannst diesen Befehl hier nicht ausführen.", ephemeral=True
                )
            return

        print(f"❌ [CommandError] /{interaction.command.name if interaction.command else '?'}: {error}")
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = "❌ Ein unerwarteter Fehler ist aufgetreten. Bitte versuche es gleich erneut."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def run_bot() -> None:
    ffmpeg_ok = check_system_requirements()
    music_info = "✅ Aktiv" if runtime_settings.get("music_enabled", True) else "❌ Deaktiviert"
    if runtime_settings.get("music_enabled", True) and not ffmpeg_ok:
        music_info += " (⚠️ FFmpeg fehlt)"

    print("╔════════════════════════════════════════════════════════╗")
    print("║            🚀 MODCHECK v2.1 WIRD GESTARTET             ║")
    print("╠════════════════════════════════════════════════════════╣")
    print(f"║  📡 Discord Token:   {'✅ Geladen' if DISCORD_TOKEN else '❌ FEHLT':<32}  ║")
    print(f"║  🟣 Twitch Token:    {'✅ Geladen' if TWITCH_TOKEN else '⚠️ Nicht gesetzt':<32}  ║")
    print(f"║  📋 Twitch Mod-Log:  {str(TWITCH_LOG_CHANNEL_ID or 'Deaktiviert'):<32}  ║")
    print(f"║  🎙️ Temp-Voice:      {str(TEMP_VOICE_CHANNEL_ID or 'Deaktiviert'):<32}  ║")
    print(f"║  📅 Review-Kanal:    {str(REVIEW_CHANNEL_ID or 'Deaktiviert'):<32}  ║")
    print(f"║  🎵 Musik & Radio:   {music_info:<32}  ║")
    print(f"║  👑 Admins:          {f'{len(ADMIN_USER_IDS)} konfiguriert':<32}  ║")
    print(f"║  📁 Datendatei:      {DATA_FILE[-32:]:<32}  ║")
    print("╚════════════════════════════════════════════════════════╝\n")

    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN fehlt in der .env-Datei! Bot wird beendet.")
        return

    bot = ModcheckBot()
    twitch_bot: TwitchBot | None = None
    twitch_task: asyncio.Task | None = None
    if TWITCH_TOKEN and STREAMER_CHANNEL:
        twitch_bot = TwitchBot(bot)
        twitch_task = asyncio.create_task(twitch_bot.start(), name="modcheck-twitch")

        def _on_twitch_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                print(f"❌ [Twitch] Task-Fehler: {t.exception()}")

        twitch_task.add_done_callback(_on_twitch_done)
    else:
        print("ℹ️ Twitch-Tracking übersprungen (TWITCH_TOKEN oder STREAMER_CHANNEL leer).")

    try:
        await bot.start(DISCORD_TOKEN)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\n🔄 Fahre Modcheck sauber herunter...")
        if twitch_bot:
            try:
                await twitch_bot.close()
            except Exception:
                pass
        if twitch_task and not twitch_task.done():
            twitch_task.cancel()
        flush_data()
        stats_tracker.flush()
        if not bot.is_closed():
            await bot.close()
        print("✅ Alle Daten gespeichert. Auf Wiedersehen!")


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
