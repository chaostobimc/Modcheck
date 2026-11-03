"""General Cog: /help, /status, /remind, /profile, /userinfo & Moderations-/Join-Events."""
from __future__ import annotations

import asyncio
import calendar
import datetime
import resource
import discord
from discord import app_commands
from discord.ext import commands

from bot_core.activity_helpers import (
    compute_activity_score,
    eligible_stream_days_for_user,
    get_activity_grade,
    get_current_streak,
    get_longest_streak,
    make_progress_bar,
    real_stream_days,
    user_was_present,
)
from bot_core.checks import has_mod_role, is_allowed_channel, parse_time, send_log
from bot_core.config import (
    COLOR_ERROR,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_PROFILE,
    COLOR_SUCCESS,
    COLOR_WARNING,
    DE_TZ,
    FISHING_CHANNEL_ID,
    FISHING_RODS,
    GAMBLING_CHANNEL_ID,
    MESSAGE_LOG_CHANNEL_ID,
    MOD_ROLE_NAME,
    MUSIC_ENABLED,
    REVIEW_CHANNEL_ID,
    STREAMER_CHANNEL,
    TEMP_VOICE_CHANNEL_ID,
    TWITCH_LOG_CHANNEL_ID,
    USERINFO_CHANNEL_ID,
    now_berlin,
    runtime_settings,
    today_berlin,
)
from bot_core.stats import bot_runtime, stats_tracker
from bot_core.storage import (
    get_balance,
    get_fishing_data,
    get_job_data,
    load_bank,
    load_data,
    open_tickets,
)
from bot_core.twitch_service import is_stream_currently_live_cached


# ═══════════════════════════════════════════════════════════════════════════
# INTERAKTIVES /help MENÜ
# ═══════════════════════════════════════════════════════════════════════════

def _build_help_embed(category: str) -> discord.Embed:
    now = datetime.datetime.now(datetime.timezone.utc)
    if category == "activity":
        embed = discord.Embed(
            title="📊 Activity Tracker & Abwesenheiten",
            description="Twitch-Stream-Anwesenheit, Streaks, Vergleiche & Abmeldungen.",
            color=COLOR_INFO,
            timestamp=now,
        )
        embed.add_field(
            name="🔗 Verknüpfung & Einzel-Statistiken",
            value=(
                "`/connect <twitch>` — Twitch-Account verknüpfen\n"
                "`/disconnect` — Twitch-Verknüpfung trennen\n"
                "`/myactivity` — Interaktiver eigener Monats-/Zeit-/Jahresbericht\n"
                "`/activity <user> [monat] [jahr]` — Interaktiver Bericht eines Users\n"
                "`/compare <user_1> <user_2>` — Direktvergleich in 7 Kategorien\n"
                "`/leaderboard` — Ranking (Monat / 30 Tage / Jahr / Gesamt)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️ Team-Überblick, Stream-Historie & Abmeldungen",
            value=(
                "`/teamstatus` — Live-Übersicht aller Mods & Stream-Status\n"
                "`/streams` — Historie der letzten Streams & wer da war\n"
                "`/checkinactive [min_quote]` — Warnt bei inaktiven Mods\n"
                "`/absent [grund] [datum] [bis_datum]` — Für Tag/Zeitraum abmelden\n"
                "`/absences` — Anstehende Abmeldungen anzeigen & 1-Klick-Storno\n"
                "`/unabsent [datum]` — Abmeldung aufheben\n"
                "`/monthlyreview` / `/yearlyreview` — Manuelle Berichte (Admins)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🟣 Twitch-Chat Commands",
            value="`!ping` • `!bot` • `!stats`",
            inline=False,
        )
        return embed

    if category == "music":
        embed = discord.Embed(
            title="🎵 Musik-Bot & 📻 Live-Radio",
            description="YouTube, Playlists, Spotify-Links & Tausende Radiosender.",
            color=0x1DB954,
            timestamp=now,
        )
        embed.add_field(
            name="🎶 Musik-Player",
            value=(
                "`/play <song/URL>` — YouTube- oder Spotify-Track/-Playlist abspielen\n"
                "`/search <titel>` — Top-10 Ergebnisse durchsuchen & auswählen\n"
                "`/nowplaying` — Aktueller Song + interaktive Player-Buttons\n"
                "`/queue` — Warteschlange mit Seitennavigation\n"
                "`/skip` — Aktuellen Song überspringen\n"
                "`/shuffle` — Warteschlange mischen\n"
                "`/autoplay <on/off>` — Automatisch passende Folgesongs spielen\n"
                "`/stop` — Musik stoppen & Kanal verlassen"
            ),
            inline=False,
        )
        embed.add_field(
            name="📻 Radio",
            value=(
                "`/radio` — Aus beliebten Preset-Sendern wählen\n"
                "`/radio <sender>` — Weltweit nach Radiosendern/Genres suchen\n"
                "`/stations` — Alle Preset-Sender im Überblick"
            ),
            inline=False,
        )
        return embed

    if category == "casino":
        embed = discord.Embed(
            title="🎰 Casino, Economy & Multiplayer",
            description="Coins verdienen, handeln und in Solo- oder PvP-Spielen einsetzen.",
            color=COLOR_GOLD,
            timestamp=now,
        )
        embed.add_field(
            name="💰 Economy",
            value=(
                "`/balance [user]` — Kontostand prüfen\n"
                "`/daily` — Tägliche Belohnung (+ Streak-Bonus)\n"
                "`/pay <user> <betrag>` — Coins überweisen\n"
                "`/coinleaderboard` — Reichste User auf dem Server\n"
                "`/gamblehelp` — Spielregeln & Quoten"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎲 Solo-Casino",
            value=(
                "`/bet <einsatz>` — Slot-Machine\n"
                "`/coinflip <einsatz> <wahl>` — Kopf oder Zahl\n"
                "`/blackjack <einsatz>` — Blackjack (Hit, Stand, Double)\n"
                "`/mines <einsatz> [minen]` — Minenfeld mit Cashout\n"
                "`/crash <einsatz>` — Steigender Multiplikator\n"
                "`/towers <einsatz> [schwierigkeit]` — Tower-Climb\n"
                "`/highlow <einsatz>` — Höher oder Niedriger\n"
                "`/roulette <einsatz> <wette>` — Roulette\n"
                "`/horserace <einsatz> <pferd>` — Pferderennen\n"
                "`/plinko <einsatz>` — Plinko-Pyramide\n"
                "`/vault <code>` & `/vaultinfo` — Knacke den 3-stelligen Tresor-Jackpot"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚔️ PvP & Multiplayer",
            value=(
                "`/duel <gegner> <einsatz>` — 1v1 Würfel-Duell\n"
                "`/connect4 <gegner> <einsatz>` — 4 Gewinnt um Coins\n"
                "`/tictactoe <gegner> <einsatz>` — Tic-Tac-Toe um Coins\n"
                "`/rr <einsatz>` — Interaktives Russisches Roulette (2–8 Spieler)"
            ),
            inline=False,
        )
        return embed

    if category == "jobs":
        embed = discord.Embed(
            title="🎣 Angeln, Jobs & Kanalpunkte-Shop",
            description="Sichere Einnahmequellen mit Werkzeug- & Cooldown-Upgrades.",
            color=COLOR_SUCCESS,
            timestamp=now,
        )
        embed.add_field(
            name="🎣 Angeln",
            value=(
                "`/fish` — Angeln gehen\n"
                "`/fishstats` — Fangbuch & Ruten-Status\n"
                "`/fishtop` — Beste Angler\n"
                "`/upgrade` — Bessere Angelrute kaufen\n"
                "`/upgradecooldown` — Angel-Cooldown verkürzen\n"
                "`/buybait <paket>` — Köderpakete kaufen"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚒️ 5 Berufe & Minigames",
            value=(
                "`/jobs` — Übersicht aller Berufe\n"
                "`/chop` (Holzfällen) • `/mine` (Bergbau) • `/hunt` (Jagen)\n"
                "`/smith` — Schmieden (Reaktions-Minispiel)\n"
                "`/cook` — Kochen (Rezept-Merkspiel)\n"
                "`/jobupgrade <job>` • `/jobcooldown <job>` • `/jobstats <job>`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛒 Shop & Twitch-Rewards",
            value=(
                "`/shop` — Verfügbare Twitch-Kanalpunkte-Belohnungen ansehen\n"
                "`/redeem` — Coins gegen echte Stream-Rewards einlösen"
            ),
            inline=False,
        )
        return embed

    if category == "community":
        embed = discord.Embed(
            title="🎫 Support-Tickets, Temp-Voice & Tools",
            description="Server-Verwaltung, private Sprachkanäle, Profile & Reminder.",
            color=0x5865F2,
            timestamp=now,
        )
        embed.add_field(
            name="🎫 Ticket-System",
            value=(
                "`/ticketpanel` — Interaktives Ticket-Panel senden (Admins)\n"
                "`/closeticket` — Aktuelles Ticket inkl. Transcript schließen\n"
                "`/adduser <user>` / `/removeuser <user>` — User im Ticket verwalten\n"
                "`/tickets` — Alle offenen Tickets auflisten (Support)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎙️ Temp-Voice",
            value=(
                "Tritt dem **Erstell-Kanal** bei, um automatisch einen eigenen Sprach- & Textkanal zu erhalten.\n"
                "Über das Panel kannst du Name, Limit, Bitrate, Sperre, Einladungen, Kicks & Admin-Rechte steuern."
            ),
            inline=False,
        )
        embed.add_field(
            name="🛠️ Profile, Akten & Reminder",
            value=(
                "`/profile [user]` — Community-Profilkarte anzeigen\n"
                "`/userinfo [user]` — Vollständige Moderator-Akte\n"
                "`/remind <zeit> <text>` — Erinnerung setzen (`10s`, `5m`, `2h`, `1d`)\n"
                "`/status` — System- & Modulstatus prüfen"
            ),
            inline=False,
        )
        return embed

    # Default: Übersicht
    embed = discord.Embed(
        title="📚 Modcheck Bot – Hilfe & Befehlsübersicht",
        description=(
            "Willkommen beim **Modcheck Bot**! Wähle unten im Dropdown-Menü eine Kategorie aus, "
            "um alle zugehörigen Slash-Commands im Detail zu sehen."
        ),
        color=COLOR_INFO,
        timestamp=now,
    )
    embed.add_field(
        name="📊 Activity & Twitch",
        value="Stream-Tracking, Streaks, Monatsberichte & Abwesenheiten",
        inline=True,
    )
    embed.add_field(
        name="🎵 Musik & Radio",
        value="YouTube, Spotify-Support, Autoplay & Live-Radiosender",
        inline=True,
    )
    embed.add_field(
        name="🎰 Casino & PvP",
        value="Slots, Blackjack, Mines, Crash, Plinko, 4 Gewinnt, RR uvm.",
        inline=True,
    )
    embed.add_field(
        name="🎣 Angeln & Jobs",
        value="Angeln + 5 Berufe mit Upgrades & Twitch-Reward-Shop",
        inline=True,
    )
    embed.add_field(
        name="🎫 Tickets & Voice",
        value="Support-Tickets mit Transcript & dynamische Temp-Voice-Räume",
        inline=True,
    )
    embed.add_field(
        name="🌐 Web-Dashboard",
        value="Öffentlicher Community-Hub & Echtzeit-Admin-Panel",
        inline=True,
    )
    embed.set_footer(text="Wähle unten eine Kategorie für alle Befehle • Modcheck v2.0")
    return embed


class HelpCategoryView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id

    @discord.ui.select(
        placeholder="📂 Kategorie auswählen...",
        options=[
            discord.SelectOption(label="Übersicht", value="overview", emoji="🏠", description="Startseite aller Module"),
            discord.SelectOption(label="Activity & Twitch", value="activity", emoji="📊", description="Streaks, Berichte & Abwesenheiten"),
            discord.SelectOption(label="Musik & Radio", value="music", emoji="🎵", description="YouTube, Spotify & Radiosender"),
            discord.SelectOption(label="Casino & Multiplayer", value="casino", emoji="🎰", description="Economy, Solo-Games & PvP"),
            discord.SelectOption(label="Angeln, Jobs & Shop", value="jobs", emoji="🎣", description="Berufe, Upgrades & Kanalpunkte"),
            discord.SelectOption(label="Tickets, Temp-Voice & Tools", value="community", emoji="🎫", description="Support, Sprachkanäle, Profile & Reminder"),
        ],
    )
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Nutze `/help` für dein eigenes Menü.", ephemeral=True)
            return
        cat = select.values[0]
        await interaction.response.edit_message(embed=_build_help_embed(cat), view=self)


# ═══════════════════════════════════════════════════════════════════════════
# GENERAL COG
# ═══════════════════════════════════════════════════════════════════════════

class GeneralCog(commands.Cog, name="General"):
    """Allgemeine Befehle, Profile, Reminder & Moderations-Logs."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Events: Message Tracking, Edit/Delete Logs & Member Join ──────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        try:
            stats_tracker.add_message(
                message.author.id,
                message.author.display_name,
                message.channel.id,
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        auto_role_name = str(runtime_settings.get("auto_role_name") or "").strip()
        if auto_role_name:
            try:
                role = discord.utils.get(member.guild.roles, name=auto_role_name)
                if role:
                    await member.add_roles(role, reason="Modcheck Auto-Rolle")
            except Exception as e:
                print(f"⚠️ Auto-Rolle konnte nicht vergeben werden: {e}")

        now = datetime.datetime.now(datetime.timezone.utc)
        age_days = max(0, (now - member.created_at).days)
        suspicious = age_days < 7

        embed = discord.Embed(
            title="⚠️ Neuer Account beigetreten" if suspicious else "👋 Neues Mitglied",
            description=f"{member.mention} (`{member.name}`) ist dem Server beigetreten.",
            color=COLOR_WARNING if suspicious else COLOR_SUCCESS,
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="📅 Account erstellt",
            value=f"<t:{int(member.created_at.timestamp())}:R> ({age_days} Tage alt)",
            inline=True,
        )
        embed.add_field(
            name="👥 Mitgliederzahl",
            value=f"**{member.guild.member_count}**",
            inline=True,
        )
        await send_log(embed, self.bot)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        content = (message.content or "").strip()
        attachments = [a.url for a in message.attachments]
        if not content and not attachments:
            return

        target_ch_id = MESSAGE_LOG_CHANNEL_ID
        if not target_ch_id:
            return
        log_ch = message.guild.get_channel(target_ch_id)
        if not log_ch:
            return

        embed = discord.Embed(
            title="🗑️ Nachricht gelöscht",
            color=COLOR_ERROR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_author(name=f"{message.author.display_name} ({message.author.name})", icon_url=message.author.display_avatar.url)
        embed.add_field(name="📍 Kanal", value=message.channel.mention, inline=True)
        embed.add_field(name="🆔 User-ID", value=f"`{message.author.id}`", inline=True)
        if content:
            embed.add_field(name="💬 Inhalt", value=content[:1020], inline=False)
        if attachments:
            embed.add_field(name="📎 Anhänge", value="\n".join(attachments)[:1020], inline=False)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot:
            return
        if (before.content or "") == (after.content or ""):
            return
        if not MESSAGE_LOG_CHANNEL_ID:
            return
        log_ch = after.guild.get_channel(MESSAGE_LOG_CHANNEL_ID)
        if not log_ch:
            return

        embed = discord.Embed(
            title="✏️ Nachricht bearbeitet",
            url=after.jump_url,
            color=COLOR_WARNING,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_author(name=f"{after.author.display_name} ({after.author.name})", icon_url=after.author.display_avatar.url)
        embed.add_field(name="📍 Kanal", value=after.channel.mention, inline=True)
        embed.add_field(name="🔗 Link", value=f"[Zur Nachricht]({after.jump_url})", inline=True)
        embed.add_field(name="Vorher", value=(before.content or "*leer*")[:1020], inline=False)
        embed.add_field(name="Nachher", value=(after.content or "*leer*")[:1020], inline=False)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

    # ── Slash Commands ────────────────────────────────────────────────────

    @app_commands.command(name="help", description="📚 Interaktive Übersicht aller Bot-Befehle")
    async def cmd_help(self, interaction: discord.Interaction) -> None:
        view = HelpCategoryView(interaction.user.id)
        await interaction.response.send_message(
            embed=_build_help_embed("overview"),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="remind", description="⏰ Setzt einen Reminder (z.B. /remind 10m Pizza)")
    @app_commands.describe(
        zeit="Zeitangabe (z.B. 10s, 5m, 2h, 1d)",
        nachricht="Woran soll ich dich erinnern?",
    )
    async def cmd_remind(self, interaction: discord.Interaction, zeit: str, nachricht: str) -> None:
        seconds = parse_time(zeit)
        if seconds is None:
            await interaction.response.send_message(
                "❌ Ungültiges Zeitformat!\n**Erlaubte Formate:** `10s`, `5m`, `2h`, `1d`\n"
                "**Beispiel:** `/remind 10m Pizza aus dem Ofen holen`",
                ephemeral=True,
            )
            return
        if seconds < 5:
            await interaction.response.send_message(
                "❌ Der Reminder muss mindestens 5 Sekunden in der Zukunft liegen.",
                ephemeral=True,
            )
            return
        if seconds > 86400 * 7:
            await interaction.response.send_message(
                "❌ Der Reminder darf maximal 7 Tage in der Zukunft liegen.",
                ephemeral=True,
            )
            return

        remind_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
        remind_ts = int(remind_at.timestamp())
        await interaction.response.send_message(
            f"✅ **Reminder gesetzt!**\n📝 **Nachricht:** {nachricht}\n"
            f"⏰ **Erinnerung:** <t:{remind_ts}:R> (<t:{remind_ts}:T>)",
            ephemeral=True,
        )

        async def _deliver() -> None:
            await asyncio.sleep(seconds)
            try:
                if interaction.channel:
                    await interaction.channel.send(
                        f"⏰ {interaction.user.mention} **Dein Reminder:**\n{nachricht}"
                    )
                    return
            except Exception:
                pass
            try:
                dm = await interaction.user.create_dm()
                await dm.send(f"⏰ **Dein Reminder:**\n{nachricht}")
            except Exception:
                pass

        asyncio.create_task(_deliver())

    @app_commands.command(name="status", description="🤖 Zeigt den aktuellen System- & Modulstatus des Bots")
    @is_allowed_channel()
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        data = load_data()
        today_s = str(today_berlin())
        today_count = len(data["streams"].get(today_s, []))

        next_review = "Deaktiviert"
        if REVIEW_CHANNEL_ID:
            now_de = now_berlin()
            td = now_de.date()
            last_day = calendar.monthrange(td.year, td.month)[1]
            nxt = datetime.datetime(td.year, td.month, last_day, 22, 0, tzinfo=DE_TZ)
            if now_de >= nxt:
                if td.month == 12:
                    nxt = datetime.datetime(td.year + 1, 1, 31, 22, 0, tzinfo=DE_TZ)
                else:
                    nm = td.month + 1
                    ld = calendar.monthrange(td.year, nm)[1]
                    nxt = datetime.datetime(td.year, nm, ld, 22, 0, tzinfo=DE_TZ)
            next_review = f"<t:{int(nxt.timestamp())}:R>"

        music_cog = self.bot.get_cog("Music")
        active_music = music_cog.active_player_count() if music_cog else 0
        tempvc_cog = self.bot.get_cog("TempVoice")
        active_tempvcs = len(tempvc_cog.temp_voice_channels) if tempvc_cog else 0

        uptime_s = bot_runtime.uptime_seconds()
        up_h, rem = divmod(uptime_s, 3600)
        up_d, up_h = divmod(up_h, 24)
        up_m = rem // 60
        uptime_str = f"{up_d}d {up_h}h {up_m}m" if up_d else f"{up_h}h {up_m}m"

        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        ping_ms = round(self.bot.latency * 1000)
        is_live = is_stream_currently_live_cached()

        embed = discord.Embed(
            title="🤖 Modcheck System-Status",
            color=COLOR_INFO,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name="📡 Module",
            value=(
                f"Discord Gateway ─ ✅ Online (`{ping_ms} ms`)\n"
                f"Twitch Tracker ── {'🟢 LIVE' if is_live else ('✅ Bereit' if STREAMER_CHANNEL else '❌ Deaktiviert')}\n"
                f"Twitch Mod Log ── {'✅ Aktiv' if TWITCH_LOG_CHANNEL_ID else '❌ Deaktiviert'}\n"
                f"Musik & Radio ─── {'✅ Aktiv (' + str(active_music) + ' Streams)' if MUSIC_ENABLED else '❌ Deaktiviert'}\n"
                f"Temp Voice ────── {'✅ Aktiv (' + str(active_tempvcs) + ' Kanäle)' if TEMP_VOICE_CHANNEL_ID else '❌ Deaktiviert'}\n"
                f"Support-Tickets ─ ✅ `{len(open_tickets)}` offen\n"
                f"Monthly Review ── {'✅ Aktiv' if REVIEW_CHANNEL_ID else '❌ Deaktiviert'}"
            ),
            inline=False,
        )
        embed.add_field(name="⏱️ Uptime", value=f"`{uptime_str}`", inline=True)
        embed.add_field(name="🧠 Speicher", value=f"`{rss_mb:.0f} MB`", inline=True)
        embed.add_field(name="👥 Verknüpft", value=f"**{len(data['users'])}** Mods", inline=True)
        embed.add_field(name="📅 Stream-Tage", value=f"**{len(real_stream_days(data))}**", inline=True)
        embed.add_field(name="📊 Heute aktiv", value=f"**{today_count}**", inline=True)
        embed.add_field(name="🗓️ Nächster Review", value=next_review, inline=True)

        embed.add_field(
            name="🎰 Kanäle",
            value=(
                f"Casino: {'<#' + str(GAMBLING_CHANNEL_ID) + '>' if GAMBLING_CHANNEL_ID else 'Überall'} • "
                f"Angeln/Jobs: {'<#' + str(FISHING_CHANNEL_ID) + '>' if FISHING_CHANNEL_ID else 'Überall'}"
            ),
            inline=False,
        )
        if STREAMER_CHANNEL:
            embed.add_field(
                name="📺 Überwachter Kanal",
                value=f"[{STREAMER_CHANNEL}](https://twitch.tv/{STREAMER_CHANNEL})",
                inline=False,
            )
        embed.set_footer(text="Modcheck v2.0 • Nutze /help für alle Befehle")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="profile", description="🪪 Zeigt deine Community-Profilkarte (oder die eines Users)")
    @app_commands.describe(member="Optional: anderer Server-User")
    async def cmd_profile(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        target = member or interaction.user
        data = load_data()
        uid = str(target.id)
        balance = get_balance(target.id)
        fd = get_fishing_data(target.id)
        rod = FISHING_RODS.get(fd.get("rod", "basic"), FISHING_RODS["basic"])

        voice_secs = stats_tracker._data.get("voice_time", {}).get(uid, 0)
        msg_total = stats_tracker._data.get("messages", {}).get(uid, {}).get("total", 0)
        vh, vm = voice_secs // 3600, (voice_secs % 3600) // 60

        total_job_earned = sum(
            get_job_data(target.id, j).get("total_earned", 0)
            for j in ("woodcutting", "mining", "hunting", "smithing", "cooking")
        )

        embed = discord.Embed(
            title=f"🪪 Profil – {target.display_name}",
            color=COLOR_PROFILE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(name="💰 Kontostand", value=f"**{balance:,} 🪙**", inline=True)
        embed.add_field(name="💬 Nachrichten", value=f"**{msg_total:,}**", inline=True)
        embed.add_field(name="🎙️ Voice-Zeit", value=f"**{vh}h {vm}m**", inline=True)

        embed.add_field(
            name="🎣 Angeln & Berufe",
            value=(
                f"Rute: {rod['name']}\n"
                f"Gefangen: **{fd.get('total_caught', 0):,}** Fische\n"
                f"Job-Verdienst: **{total_job_earned + fd.get('total_earned', 0):,} 🪙**"
            ),
            inline=False,
        )

        if uid in data["users"]:
            tname = data["users"][uid].get("twitch_name", "?")
            days = eligible_stream_days_for_user(data, uid)
            present = sum(1 for d in days if user_was_present(data, uid, d))
            pct = (present / len(days) * 100) if days else 0.0
            grade, g_emoji, _ = get_activity_grade(pct)
            streak = get_current_streak(data, uid)
            score = compute_activity_score(data, uid, days)
            embed.add_field(
                name="🟣 Twitch-Aktivität",
                value=(
                    f"Account: [{tname}](https://twitch.tv/{tname})\n"
                    f"Note: **{g_emoji} {grade}** • Score: **{score:.0f}/100** • Streak: **{streak}**🔥\n"
                    f"Streams: {make_progress_bar(present, len(days), 10)}"
                ),
                inline=False,
            )

        if isinstance(target, discord.Member) and target.joined_at:
            embed.set_footer(
                text=f"Auf dem Server seit {target.joined_at.strftime('%d.%m.%Y')}"
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="📋 Vollständige User-Akte (nur für Mods)")
    @app_commands.describe(member="Der User dessen Akte erstellt werden soll")
    async def cmd_userinfo(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        if not has_mod_role(interaction.user):
            await interaction.response.send_message(
                f"❌ Du benötigst die Rolle **{MOD_ROLE_NAME}**.",
                ephemeral=True,
            )
            return

        target = member or interaction.user
        await interaction.response.defer(ephemeral=True)

        now = datetime.datetime.now(datetime.timezone.utc)
        data = load_data()
        uid = str(target.id)

        account_created = target.created_at
        joined_at = target.joined_at
        account_age = (now - account_created).days
        server_age = (now - joined_at).days if joined_at else 0
        roles = [r for r in target.roles if r.name != "@everyone"]
        roles_str = ", ".join(r.name for r in reversed(roles)) or "Keine"

        has_twitch = uid in data["users"]
        twitch_name = data["users"][uid]["twitch_name"] if has_twitch else None
        days = eligible_stream_days_for_user(data, uid) if has_twitch else []
        stream_days = len(days)
        present = sum(1 for d in days if user_was_present(data, uid, d)) if has_twitch else 0
        absent = max(0, stream_days - present)
        pct = (present / stream_days * 100) if (stream_days > 0 and has_twitch) else 0.0
        act_score = compute_activity_score(data, uid, days) if has_twitch else 0.0
        current_streak = get_current_streak(data, uid) if has_twitch else 0
        longest_streak = get_longest_streak(data, uid) if has_twitch else 0

        last_seen = "Noch nie"
        first_seen = "Unbekannt"
        if has_twitch:
            seen_days = [ds for ds in days if uid in data["streams"].get(ds, [])]
            if seen_days:
                first_seen = seen_days[0]
                last_seen = seen_days[-1]

        grade, grade_emoji, grade_color = (
            get_activity_grade(pct) if has_twitch else ("–", "❓", 0x30363D)
        )

        balance = get_balance(target.id)
        bank = load_bank()
        all_balances = {k: v for k, v in bank.items() if k.isdigit() and isinstance(v, int)}
        sorted_b = sorted(all_balances.items(), key=lambda x: x[1], reverse=True)
        coin_rank = next((i + 1 for i, (u, _) in enumerate(sorted_b) if u == uid), max(1, len(sorted_b)))

        fd = get_fishing_data(target.id)
        rod = FISHING_RODS.get(fd.get("rod", "basic"), FISHING_RODS["basic"])
        user_tickets = [t for t in open_tickets.values() if t.get("user_id") == target.id]

        voice_secs = stats_tracker._data.get("voice_time", {}).get(uid, 0)
        voice_msgs = stats_tracker._data.get("messages", {}).get(uid, {}).get("total", 0)

        embed = discord.Embed(
            title=f"📋 User-Akte: {target.display_name}",
            color=grade_color,
            timestamp=now,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        if target.banner:
            embed.set_image(url=target.banner.url)

        embed.add_field(
            name="🏷️ Discord",
            value="\n".join([
                f"**Name:** {target.mention}",
                f"**Username:** `{target.name}`",
                f"**ID:** `{target.id}`",
                f"**Bot:** {'Ja' if target.bot else 'Nein'}",
                f"**Nitro:** {'✅' if target.premium_since else '❌'}",
            ]),
            inline=True,
        )
        joined_disp = f"<t:{int(joined_at.timestamp())}:D>" if joined_at else "–"
        nitro_since = f"<t:{int(target.premium_since.timestamp())}:D>" if target.premium_since else "–"
        embed.add_field(
            name="📅 Zeitstempel",
            value="\n".join([
                f"**Erstellt:** <t:{int(account_created.timestamp())}:D> ({account_age}T)",
                f"**Beitritt:** {joined_disp} ({server_age}T)",
                f"**Nitro seit:** {nitro_since}",
            ]),
            inline=True,
        )
        embed.add_field(name=f"🏅 Rollen ({len(roles)})", value=roles_str[:1024], inline=False)

        twitch_link = f"[{twitch_name}](https://twitch.tv/{twitch_name})" if twitch_name else "Nicht verknüpft"
        embed.add_field(
            name="📺 Twitch-Aktivität",
            value="\n".join([
                f"**Twitch:** {twitch_link}",
                f"**Note:** {grade_emoji} {grade} (`{act_score:.0f}/100` Pkt.)",
                f"**Anwesend:** {present}/{stream_days} ({pct:.0f}%)",
                f"**Gefehlt:** {absent}",
                f"**Streak:** {current_streak} (Max: {longest_streak})",
                f"**Zuletzt:** `{last_seen}` • **Seit:** `{first_seen}`",
            ]),
            inline=True,
        )

        voice_h, voice_m = voice_secs // 3600, (voice_secs % 3600) // 60
        embed.add_field(
            name="📊 Chat, Voice & Coins",
            value="\n".join([
                f"**Nachrichten:** {voice_msgs:,}",
                f"**Voice-Zeit:** {voice_h}h {voice_m}m",
                f"**Coins:** {balance:,} 🪙 (#{coin_rank})",
                f"**Offene Tickets:** {len(user_tickets)}",
            ]),
            inline=True,
        )
        embed.add_field(
            name="🎣 Angeln",
            value="\n".join([
                f"**Angel:** {rod['name']}",
                f"**Gefangen:** {fd.get('total_caught', 0):,}",
                f"**Verdient:** {fd.get('total_earned', 0):,} 🪙",
                f"**Legendäre:** {fd.get('legendary_caught', 0)}",
            ]),
            inline=True,
        )
        embed.set_footer(text=f"Angefordert von {interaction.user.display_name}")

        if USERINFO_CHANNEL_ID and interaction.guild:
            archive_ch = interaction.guild.get_channel(USERINFO_CHANNEL_ID)
            if archive_ch:
                try:
                    await archive_ch.send(embed=embed)
                except Exception:
                    pass

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GeneralCog(bot))
