"""Modcheck v2.1 – Modularer Discord- & Twitch-Bot.

Startpunkt für lokales Ausführen sowie für den Raspberry Pi 5 systemd-Dienst:
    python main.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import traceback

import discord
from discord import app_commands
from discord.ext import commands

# Umgeht den discord.py 2.7.x DAVE-Bug (#10549: grüner Redekreis ohne Ton) und nutzt den bewährten Transport-Modus
try:
    import discord.voice_state as _dvs
    _dvs.has_dave = False
except Exception:
    pass

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
