"""Welcome & Server-Stats Cog: Begrüßung, DM, Goodbye, Server-Boost-Belohnung & Live-Voice-Counter."""
from __future__ import annotations

import asyncio
import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.checks import is_user_admin
from bot_core.config import (
    COLOR_ERROR,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_SUCCESS,
    WELCOME_CHANNEL_ID,
)
from bot_core.server_store import get_dynamic_channel_id, server_cfg_store
from bot_core.storage import update_balance
from bot_core.twitch_service import fetch_twitch_follower_count


STAT_COUNTERS = {
    "all": ("Discord Mitglieder (inkl. Bots)", "👥 Discord Mitglieder: {count}"),
    "members": ("Discord Mitglieder (ohne Bots)", "👥 Mitglieder: {count}"),
    "twitch_followers": ("Twitch Follower", "🟣 Twitch Follower: {count}"),
    "bots": ("Discord Bots", "🤖 Bots: {count}"),
    "boosts": ("Discord Boosts", "💎 Boosts: {count}"),
    "online": ("Discord Online-Mitglieder", "🟢 Online: {count}"),
}


class CounterSetupError(Exception):
    """Sichere, nutzerverständliche Fehlermeldung bei der Zähler-Einrichtung."""



def _format_welcome_text(template: str, member: discord.Member) -> str:
    return (
        template.replace("{user}", member.mention)
        .replace("{username}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{membercount}", str(member.guild.member_count or len(member.guild.members)))
        .replace("{boostcount}", str(member.guild.premium_subscription_count or 0))
    )


class WelcomeGroup(app_commands.Group):
    """Slash-Command-Gruppe `/welcome ...`."""

    def __init__(self, cog: WelcomeCog) -> None:
        super().__init__(
            name="welcome",
            description="👋 Willkommen, Goodbye, Boost-Alerts & Server-Statistik-Kanäle",
        )
        self.cog = cog

    @app_commands.command(name="setup-join", description="👋 Konfiguriert die Begrüßungsnachricht im Kanal (Admins)")
    @app_commands.describe(
        kanal="Begrüßungs-Kanal",
        aktiv="Begrüßungen ein-/ausschalten",
        nachricht="Text ({user}, {username}, {server}, {membercount})",
        titel="Embed-Titel",
        banner_url="Optionale Bild-/GIF-URL unten im Willkommens-Embed",
    )
    async def cmd_setup_join(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel | None = None,
        aktiv: bool = True,
        nachricht: str | None = None,
        titel: str | None = None,
        banner_url: str | None = None,
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> dict[str, Any]:
            w = g["welcome"]
            w["enabled"] = bool(aktiv)
            if kanal is not None:
                w["channel_id"] = kanal.id
                g.setdefault("channels", {})["welcome_channel_id"] = kanal.id
            if nachricht is not None:
                w["message"] = nachricht.replace("\\n", "\n")
            if titel is not None:
                w["embed_title"] = titel
            if banner_url is not None:
                w["banner_url"] = banner_url.strip()
            return dict(w)

        w = server_cfg_store.mutate(interaction.guild.id, _upd)
        ch_id = w.get("channel_id") or get_dynamic_channel_id(
            interaction.guild.id, "welcome_channel_id", WELCOME_CHANNEL_ID
        )
        await interaction.response.send_message(
            f"✅ Begrüßung aktualisiert ({'Aktiv' if w['enabled'] else 'Aus'} • Kanal: {f'<#{ch_id}>' if ch_id else 'nicht gesetzt'}).",
            ephemeral=True,
        )

    @app_commands.command(name="setup-dm", description="✉️ Konfiguriert eine automatische Willkommens-DM (Admins)")
    async def cmd_setup_dm(
        self, interaction: discord.Interaction, aktiv: bool, nachricht: str | None = None
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> None:
            g["welcome"]["dm_enabled"] = bool(aktiv)
            if nachricht:
                g["welcome"]["dm_message"] = nachricht.replace("\\n", "\n")

        server_cfg_store.mutate(interaction.guild.id, _upd)
        await interaction.response.send_message(
            f"✅ Willkommens-DM ist jetzt **{'Aktiv' if aktiv else 'Deaktiviert'}**.",
            ephemeral=True,
        )

    @app_commands.command(name="setup-leave", description="🚪 Konfiguriert Verlassens-/Goodbye-Nachrichten (Admins)")
    async def cmd_setup_leave(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel | None = None,
        aktiv: bool = True,
        nachricht: str | None = None,
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> dict[str, Any]:
            gb = g["goodbye"]
            gb["enabled"] = bool(aktiv)
            if kanal is not None:
                gb["channel_id"] = kanal.id
            if nachricht is not None:
                gb["message"] = nachricht.replace("\\n", "\n")
            return dict(gb)

        gb = server_cfg_store.mutate(interaction.guild.id, _upd)
        await interaction.response.send_message(
            f"✅ Goodbye-Nachricht gespeichert ({'Aktiv' if gb['enabled'] else 'Aus'}).",
            ephemeral=True,
        )

    @app_commands.command(name="setup-boost", description="💎 Konfiguriert Server-Boost-Dank & Coin-Prämie (Admins)")
    async def cmd_setup_boost(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel | None = None,
        aktiv: bool = True,
        coin_bonus: app_commands.Range[int, 0, 1000000] = 5000,
        nachricht: str | None = None,
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> None:
            b = g["boost"]
            b["enabled"] = bool(aktiv)
            b["coin_reward"] = int(coin_bonus)
            if kanal is not None:
                b["channel_id"] = kanal.id
            if nachricht is not None:
                b["message"] = nachricht.replace("\\n", "\n")

        server_cfg_store.mutate(interaction.guild.id, _upd)
        await interaction.response.send_message(
            f"✅ Boost-Dank konfiguriert (`+{coin_bonus:,} 🪙`).", ephemeral=True
        )

    @app_commands.command(name="test", description="🧪 Sendet eine Test-Vorschau für Join, Leave oder Boost (Admins)")
    @app_commands.choices(
        typ=[
            app_commands.Choice(name="👋 Join / Willkommen", value="join"),
            app_commands.Choice(name="🚪 Leave / Goodbye", value="leave"),
            app_commands.Choice(name="💎 Server-Boost", value="boost"),
        ]
    )
    async def cmd_test(self, interaction: discord.Interaction, typ: str = "join") -> None:
        if not is_user_admin(interaction.user) or not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        if typ == "join":
            await self.cog.send_welcome(interaction.user, fallback_channel=interaction.channel)
        elif typ == "leave":
            await self.cog.send_goodbye(interaction.user, fallback_channel=interaction.channel)
        else:
            await self.cog.send_boost(interaction.user, fallback_channel=interaction.channel, award_coins=False)
        await interaction.response.send_message(f"✅ Test-Nachricht (`{typ}`) gesendet!", ephemeral=True)

    @app_commands.command(
        name="counter-create",
        description="📊 Erstellt einen Statistik-Sprachkanal (Admins)",
    )
    @app_commands.choices(
        typ=[app_commands.Choice(name=label, value=key) for key, (label, _) in STAT_COUNTERS.items()]
    )
    @app_commands.describe(
        typ="Welche Zahl soll angezeigt werden?",
        vorlage="Kanalname mit {count} als Platzhalter",
        kategorie="Optionale Kategorie für den neuen Kanal",
    )
    async def cmd_counter_create(
        self, interaction: discord.Interaction, typ: str, vorlage: str | None = None,
        kategorie: discord.CategoryChannel | None = None,
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await self.cog.create_stat_counter(interaction.guild, typ, kategorie, vorlage)
        except CounterSetupError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Statistik-Kanal {channel.mention} erstellt. Aktualisierung etwa alle 10 Minuten.", ephemeral=True
        )

    @app_commands.command(name="counter-remove", description="🗑️ Entfernt einen verwalteten Statistik-Sprachkanal (Admins)")
    async def cmd_counter_remove(
        self, interaction: discord.Interaction, kanal: discord.VoiceChannel
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog.remove_stat_counter(interaction.guild, kanal.id)
        except CounterSetupError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send("🗑️ Statistik-Kanal entfernt.", ephemeral=True)


class WelcomeCog(commands.Cog, name="Welcome"):
    """Begrüßungen, Verabschiedungen, Boost-Belohnungen & Statistik-Kanäle."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._counter_locks: dict[int, asyncio.Lock] = {}
        self.welcome_group = WelcomeGroup(self)
        self.bot.tree.add_command(self.welcome_group)

    async def cog_load(self) -> None:
        self.stats_counter_loop.start()

    async def cog_unload(self) -> None:
        self.stats_counter_loop.cancel()
        self.bot.tree.remove_command(self.welcome_group.name, type=self.welcome_group.type)

    @staticmethod
    def compute_stat_value(guild: discord.Guild, stat_type: str) -> int:
        if stat_type == "all":
            return guild.member_count or len(guild.members)
        if stat_type == "bots":
            return sum(1 for m in guild.members if m.bot)
        if stat_type == "boosts":
            return int(guild.premium_subscription_count or 0)
        if stat_type == "online":
            return sum(1 for m in guild.members if not m.bot and m.status != discord.Status.offline)
        return sum(1 for m in guild.members if not m.bot)

    async def create_stat_counter(
        self, guild: discord.Guild, stat_type: str,
        category: discord.CategoryChannel | None = None, template: str | None = None,
    ) -> discord.VoiceChannel:
        if stat_type not in STAT_COUNTERS:
            raise CounterSetupError("Unbekannter Statistik-Typ.")
        if template is not None and ("{count}" not in template or len(template) > 100):
            raise CounterSetupError("Die Vorlage muss {count} enthalten und höchstens 100 Zeichen lang sein.")
        tpl = template or STAT_COUNTERS[stat_type][1]
        lock = self._counter_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            counters = server_cfg_store.get_guild(guild.id).get("stats_channels") or []
            if any(c.get("type") == stat_type and guild.get_channel(int(c.get("channel_id") or 0)) for c in counters):
                raise CounterSetupError("Für diesen Typ existiert bereits ein Statistik-Kanal. Entferne ihn zuerst, wenn du ihn neu erstellen möchtest.")
            if stat_type == "twitch_followers":
                value = await fetch_twitch_follower_count()
                if value is None:
                    raise CounterSetupError(
                        "Twitch-Followerzahl nicht abrufbar. Prüfe STREAMER_CHANNEL bzw. STREAMER_USER_ID, "
                        "TWITCH_CLIENT_ID und ein gültiges Twitch-Token bzw. TWITCH_CLIENT_SECRET. "
                        "Der Kanal wurde nicht erstellt; versuche es erneut."
                    )
            else:
                value = self.compute_stat_value(guild, stat_type)
            try:
                channel = await guild.create_voice_channel(
                    name=tpl.replace("{count}", str(value))[:100], category=category,
                    overwrites={guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True)},
                    reason="Modcheck Statistik-Zähler",
                )
            except discord.Forbidden as exc:
                raise CounterSetupError("Mir fehlt die Berechtigung, in dieser Kategorie Kanäle zu verwalten.") from exc
            except discord.HTTPException as exc:
                raise CounterSetupError("Discord konnte den Statistik-Kanal nicht erstellen.") from exc
            try:
                server_cfg_store.mutate(guild.id, lambda g: g.setdefault("stats_channels", []).append(
                    {"channel_id": channel.id, "type": stat_type, "template": tpl}
                ))
            except Exception as exc:
                try:
                    await channel.delete(reason="Statistik-Zähler konnte nicht gespeichert werden")
                except discord.HTTPException:
                    raise CounterSetupError(
                        f"Der Zähler konnte nicht gespeichert oder automatisch gelöscht werden. Bitte entferne {channel.mention} manuell und prüfe die Bot-Logs."
                    ) from exc
                raise CounterSetupError("Der Kanal konnte nicht gespeichert werden und wurde wieder entfernt. Bitte prüfe die Bot-Logs.") from exc
            return channel

    async def remove_stat_counter(self, guild: discord.Guild, channel_id: int) -> None:
        lock = self._counter_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            counters = server_cfg_store.get_guild(guild.id).get("stats_channels") or []
            if not any(int(c.get("channel_id") or 0) == channel_id for c in counters):
                raise CounterSetupError("Dieser Kanal ist kein verwalteter Statistik-Kanal.")
            channel = guild.get_channel(channel_id)
            if channel is not None:
                try:
                    await channel.delete(reason="Modcheck Statistik-Kanal entfernt")
                except discord.NotFound:
                    pass
                except discord.Forbidden as exc:
                    raise CounterSetupError("Mir fehlt die Berechtigung, diesen Kanal zu löschen.") from exc
                except discord.HTTPException as exc:
                    raise CounterSetupError("Discord konnte den Kanal nicht löschen. Bitte erneut versuchen.") from exc
            try:
                server_cfg_store.mutate(guild.id, lambda g: g.__setitem__("stats_channels", [
                    c for c in (g.get("stats_channels") or []) if int(c.get("channel_id") or 0) != channel_id
                ]))
            except Exception as exc:
                raise CounterSetupError("Der Kanal wurde entfernt, aber die Statistik-Einstellung konnte nicht gespeichert werden. Bitte erneut versuchen und die Bot-Logs prüfen.") from exc

    async def send_welcome(
        self,
        member: discord.Member,
        fallback_channel: discord.abc.Messageable | None = None,
    ) -> None:
        g = server_cfg_store.get_guild(member.guild.id)
        w = g.get("welcome") or {}
        if not w.get("enabled", True) and fallback_channel is None:
            return

        ch_id = int(w.get("channel_id") or 0) or get_dynamic_channel_id(
            member.guild.id, "welcome_channel_id", WELCOME_CHANNEL_ID
        )
        channel = member.guild.get_channel(ch_id) if ch_id else fallback_channel
        if channel is not None:
            desc = _format_welcome_text(
                str(
                    w.get("message")
                    or "Willkommen auf **{server}**, {user}! Du bist unser **{membercount}.** Mitglied! 🎉"
                ),
                member,
            )
            embed = discord.Embed(
                title=str(w.get("embed_title") or "👋 Herzlich Willkommen!"),
                description=desc,
                color=int(w.get("embed_color") or COLOR_SUCCESS),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            if w.get("banner_url"):
                embed.set_image(url=str(w["banner_url"]))
            embed.set_footer(text=f"Mitglied #{member.guild.member_count} • {member.guild.name}")
            try:
                await channel.send(content=member.mention, embed=embed)
            except Exception as e:
                print(f"⚠️ [Welcome] Fehler beim Senden der Begrüßung: {e}")

        if w.get("dm_enabled") and w.get("dm_message") and not member.bot:
            try:
                await member.send(_format_welcome_text(str(w["dm_message"]), member))
            except Exception:
                pass

    async def send_goodbye(
        self,
        member: discord.Member,
        fallback_channel: discord.abc.Messageable | None = None,
    ) -> None:
        g = server_cfg_store.get_guild(member.guild.id)
        gb = g.get("goodbye") or {}
        if not gb.get("enabled") and fallback_channel is None:
            return
        ch_id = int(gb.get("channel_id") or 0)
        channel = member.guild.get_channel(ch_id) if ch_id else fallback_channel
        if channel is None:
            return
        desc = _format_welcome_text(
            str(gb.get("message") or "**{username}** hat den Server verlassen. Auf Wiedersehen! 👋"),
            member,
        )
        embed = discord.Embed(
            title="🚪 Mitglied hat den Server verlassen",
            description=desc,
            color=COLOR_ERROR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    async def send_boost(
        self,
        member: discord.Member,
        fallback_channel: discord.abc.Messageable | None = None,
        award_coins: bool = True,
    ) -> None:
        g = server_cfg_store.get_guild(member.guild.id)
        b = g.get("boost") or {}
        if not b.get("enabled", True) and fallback_channel is None:
            return
        coins = max(0, int(b.get("coin_reward") or 0))
        if award_coins and coins > 0:
            update_balance(member.id, coins)
        ch_id = int(b.get("channel_id") or 0) or get_dynamic_channel_id(
            member.guild.id, "welcome_channel_id", WELCOME_CHANNEL_ID
        )
        channel = member.guild.get_channel(ch_id) if ch_id else fallback_channel
        if channel is None:
            return
        desc = _format_welcome_text(
            str(b.get("message") or "💎 Vielen Dank {user} für deinen **Server-Boost**! 🚀"),
            member,
        )
        if coins > 0:
            desc += f"\n🎁 Als Dankeschön erhältst du **`+{coins:,} 🪙`**!"
        embed = discord.Embed(
            title="💎 Neuer Server-Boost!",
            description=desc,
            color=0xF47FFF,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(content=member.mention, embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not member.bot:
            await self.send_welcome(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not member.bot:
            await self.send_goodbye(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.premium_since is None and after.premium_since is not None:
            await self.send_boost(after, award_coins=True)

    @tasks.loop(minutes=10)
    async def stats_counter_loop(self) -> None:
        guilds = list(self.bot.guilds)
        configs = [(guild, server_cfg_store.get_guild(guild.id).get("stats_channels") or []) for guild in guilds]
        needs_twitch = any(item.get("type") == "twitch_followers" for _, items in configs for item in items)
        # Ein API-Abruf für alle Server; bei Fehlern bleibt der letzte Kanalname erhalten.
        followers = await fetch_twitch_follower_count() if needs_twitch else None
        for guild, counters in configs:
            for item in counters:
                cid = int(item.get("channel_id") or 0)
                vc = guild.get_channel(cid)
                if not isinstance(vc, discord.VoiceChannel):
                    continue
                stat_type = str(item.get("type") or "members")
                if stat_type == "twitch_followers":
                    if followers is None:
                        continue
                    val = followers
                else:
                    val = self.compute_stat_value(guild, stat_type)
                desired = str(item.get("template") or "👥 {count}").replace("{count}", str(val))
                if vc.name != desired:
                    try:
                        await vc.edit(name=desired[:100])
                    except discord.HTTPException as exc:
                        print(f"⚠️ [Statistik] Kanal {cid} konnte nicht aktualisiert werden: {exc}")

    @stats_counter_loop.before_loop
    async def _before_stats_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
