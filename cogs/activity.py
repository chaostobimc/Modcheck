"""Activity Cog v2.1: Präzises Stream-Tracking, Team-Status, Stream-Historie, Leaderboards & Abwesenheiten."""
from __future__ import annotations

import calendar
import datetime
from typing import Any
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.activity_helpers import (
    build_and_send_activity,
    fmt_iso_de,
    format_minutes_hm,
    get_days_since_last_seen,
    get_month_activity_stats,
    get_period_ranking,
    get_rank_emoji,
    get_user_compare_stats,
    make_progress_bar,
    real_stream_days,
    send_monthly_review,
    send_yearly_review,
    user_was_live_in_chat,
)
from bot_core.checks import has_mod_role, is_admin_user, is_allowed_channel, send_log
from bot_core.config import (
    CLIPS_CHANNEL_ID,
    COLOR_ABSENCE,
    COLOR_ERROR,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_TWITCH,
    COLOR_WARNING,
    MOD_ROLE_NAME,
    MONTH_NAMES,
    REVIEW_CHANNEL_ID,
    STREAMER_CHANNEL,
    WEEKDAY_SHORT,
    now_berlin,
    today_berlin,
)
from bot_core.storage import (
    add_absence,
    add_absence_range,
    get_clip_tracker_state,
    get_user_absences,
    is_review_already_sent,
    is_user_absent,
    load_data,
    mark_review_already_sent,
    remove_absence,
    save_clip_tracker_state,
    save_data,
)
from bot_core.twitch_service import (
    check_streamer_live,
    fetch_recent_clips,
    get_live_stream_info_cached,
    is_stream_currently_live_cached,
)


def parse_flexible_date(raw: str | None, default: datetime.date) -> datetime.date | None:
    """Erkennt 'heute', 'morgen', 'TT.MM.JJJJ', 'TT.MM.' oder 'JJJJ-MM-TT'."""
    if not raw or not raw.strip():
        return default
    s = raw.strip().lower()
    if s in ("heute", "today"):
        return default
    if s in ("morgen", "tomorrow"):
        return default + datetime.timedelta(days=1)
    if s in ("übermorgen", "uebermorgen"):
        return default + datetime.timedelta(days=2)

    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # Kurzform z.B. "15.11" oder "15.11."
    cleaned = s.rstrip(".")
    try:
        dt = datetime.datetime.strptime(f"{cleaned}.{default.year}", "%d.%m.%Y").date()
        if dt < default:
            dt = datetime.date(default.year + 1, dt.month, dt.day)
        return dt
    except ValueError:
        return None


# ── Interaktive Views für Leaderboard, Stream-Historie & Abmeldungen ───────

class LeaderboardView(discord.ui.View):
    """Zeitraum-Filter für /leaderboard (Monat, 30 Tage, Jahr, Gesamt)."""

    def __init__(self, invoker_id: int, scope: str = "month") -> None:
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.scope = scope
        self._sync_styles()

    def _sync_styles(self) -> None:
        mapping = {
            "month": self.btn_month,
            "30d": self.btn_30d,
            "year": self.btn_year,
            "all": self.btn_all,
        }
        for key, btn in mapping.items():
            btn.style = discord.ButtonStyle.primary if key == self.scope else discord.ButtonStyle.secondary

    def build_embed(self) -> discord.Embed:
        data = load_data()
        today = today_berlin()
        all_days = real_stream_days(data)

        if self.scope == "month":
            prefix = f"{today.year:04d}-{today.month:02d}-"
            days = [d for d in all_days if d.startswith(prefix)]
            label = f"{MONTH_NAMES[today.month]} {today.year}"
        elif self.scope == "30d":
            cutoff = str(today - datetime.timedelta(days=30))
            days = [d for d in all_days if d >= cutoff]
            label = "Letzte 30 Tage"
        elif self.scope == "year":
            days = [d for d in all_days if d.startswith(f"{today.year:04d}-")]
            label = f"Jahr {today.year}"
        else:
            days = all_days
            label = "Gesamtzeit (All-Time)"

        ranking = get_period_ranking(data, days)
        lines: list[str] = []
        for r in ranking[:15]:
            medal = get_rank_emoji(r["rank"])
            streak_txt = f" • 🔥{r['streak']}" if r["streak"] > 0 else ""
            if r["rank"] <= 3:
                bar = make_progress_bar(r["present"], r["stream_days"], 8)
                lines.append(
                    f"{medal} **{r['display_name']}** (`{r['twitch_name']}`) — "
                    f"**{r['score']:.0f} Pkt.** ({r['grade_emoji']} {r['grade']})\n"
                    f"╰ {r['present']}/{r['stream_days']} Streams • {bar} • "
                    f"⏱️ {format_minutes_hm(r['active_minutes'])} • 💬 {r['messages']:,}{streak_txt}"
                )
            else:
                lines.append(
                    f"{medal} **{r['display_name']}** (`{r['twitch_name']}`) — "
                    f"**{r['score']:.0f} Pkt.** • {r['present']}/{r['stream_days']} ({r['pct']:.0f}%) • "
                    f"⏱️ {format_minutes_hm(r['active_minutes'])}{streak_txt}"
                )

        embed = discord.Embed(
            title=f"🏆 Aktivitäts-Ranking — {label}",
            description="\n\n".join(lines) if lines else "*Keine Daten für diesen Zeitraum vorhanden.*",
            color=COLOR_GOLD,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(
            text=f"📅 {len(days)} Stream-Tage im Zeitraum • Sortiert nach Aktivitäts-Score (0–100)"
        )
        return embed

    async def _switch(self, interaction: discord.Interaction, scope: str) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Nicht deine Ansicht!", ephemeral=True)
            return
        self.scope = scope
        self._sync_styles()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Dieser Monat", emoji="📅", row=0)
    async def btn_month(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._switch(interaction, "month")

    @discord.ui.button(label="30 Tage", emoji="📆", row=0)
    async def btn_30d(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._switch(interaction, "30d")

    @discord.ui.button(label="Dieses Jahr", emoji="🎆", row=0)
    async def btn_year(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._switch(interaction, "year")

    @discord.ui.button(label="Gesamt", emoji="🏆", row=0)
    async def btn_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._switch(interaction, "all")


class StreamHistoryView(discord.ui.View):
    """Seitennavigation für /streams."""

    PER_PAGE = 6

    def __init__(self, invoker_id: int, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.page = page
        self._sync()

    def _get_days(self) -> tuple[dict[str, Any], list[str]]:
        data = load_data()
        days = list(reversed(real_stream_days(data)))
        return data, days

    def _sync(self) -> None:
        _, days = self._get_days()
        max_page = max(0, (len(days) - 1) // self.PER_PAGE)
        self.page = min(self.page, max_page)
        self.btn_prev.disabled = self.page <= 0
        self.btn_next.disabled = self.page >= max_page

    def build_embed(self) -> discord.Embed:
        data, days = self._get_days()
        total = len(days)
        max_page = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        chunk = days[self.page * self.PER_PAGE : (self.page + 1) * self.PER_PAGE]
        users = data.get("users", {})
        meta_map = data.get("stream_meta", {})

        embed = discord.Embed(
            title=f"📺 Stream-Historie ({STREAMER_CHANNEL or 'Kanal'})",
            description=f"Zeigt die erfassten Stream-Tage inkl. Team-Anwesenheit *(Seite {self.page + 1}/{max_page})*",
            color=COLOR_TWITCH,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        if not chunk:
            embed.description = "*Noch keine Streams erfasst.*"
            return embed

        for d_str in chunk:
            try:
                d_obj = datetime.date.fromisoformat(d_str)
                wd = WEEKDAY_SHORT[d_obj.weekday()]
                date_label = f"{d_obj.strftime('%d.%m.%Y')} ({wd})"
            except ValueError:
                date_label = d_str

            meta = meta_map.get(d_str, {})
            live_uids = [u for u in users if user_was_live_in_chat(data, u, d_str)]
            exc_uids = [u for u in users if u not in live_uids and is_user_absent(u, d_str)]

            live_names = [users[u].get("display_name", "?") for u in live_uids]
            exc_names = [users[u].get("display_name", "?") for u in exc_uids]

            meta_bits = []
            if meta.get("duration_minutes"):
                meta_bits.append(f"⏱️ {format_minutes_hm(meta['duration_minutes'])}")
            if meta.get("peak_viewers"):
                meta_bits.append(f"👁️ Peak {meta['peak_viewers']:,}")
            if meta.get("games"):
                meta_bits.append("🎮 " + ", ".join(meta["games"][:2]))

            lines = []
            if meta.get("title"):
                lines.append(f"*{meta['title'][:80]}*")
            if meta_bits:
                lines.append(" • ".join(meta_bits))
            lines.append(
                f"✅ **Anwesend ({len(live_names)}/{len(users)}):** "
                + (", ".join(live_names) if live_names else "*Niemand*")
            )
            if exc_names:
                lines.append(f"📋 **Entschuldigt:** {', '.join(exc_names)}")

            embed.add_field(name=f"📅 {date_label}", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Insgesamt {total} erfasste Stream-Tage")
        return embed

    @discord.ui.button(label="◀️ Neuere", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Nicht deine Ansicht!", ephemeral=True)
            return
        self.page = max(0, self.page - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Ältere ▶️", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ Nicht deine Ansicht!", ephemeral=True)
            return
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class AbsenceManageView(discord.ui.View):
    """1-Klick-Stornierung für anstehende Abwesenheiten in /absences."""

    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self._rebuild()

    def _upcoming(self) -> list[dict[str, str]]:
        today_s = str(today_berlin())
        return [a for a in get_user_absences(str(self.user_id)) if a["date"] >= today_s]

    def _rebuild(self) -> None:
        self.clear_items()
        upcoming = self._upcoming()
        if not upcoming:
            return
        options = []
        for a in upcoming[:25]:
            d_obj = datetime.date.fromisoformat(a["date"])
            wd = WEEKDAY_SHORT[d_obj.weekday()]
            options.append(
                discord.SelectOption(
                    label=f"{d_obj.strftime('%d.%m.%Y')} ({wd})",
                    description=a["reason"][:90],
                    value=a["date"],
                )
            )
        sel = discord.ui.Select(
            placeholder="🗑️ Abmeldung auswählen, um sie aufzuheben...",
            options=options,
            min_values=1,
            max_values=min(len(options), 10),
        )
        sel.callback = self._on_cancel
        self.add_item(sel)

    def build_embed(self) -> discord.Embed:
        upcoming = self._upcoming()
        if not upcoming:
            return discord.Embed(
                title="📋 Deine Abwesenheiten",
                description="✅ Du hast aktuell keine anstehenden Abwesenheiten!",
                color=COLOR_SUCCESS,
            )
        lines = []
        for a in upcoming[:20]:
            parsed = datetime.date.fromisoformat(a["date"])
            day_name = WEEKDAY_SHORT[parsed.weekday()]
            lines.append(f"📅 **{parsed.strftime('%d.%m.%Y')}** ({day_name}) — 📝 {a['reason']}")

        embed = discord.Embed(
            title="📋 Deine anstehenden Abwesenheiten",
            description="\n".join(lines),
            color=COLOR_ABSENCE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text=f"{len(upcoming)} anstehende Abmeldung(en) • Unten auswählen zum Stornieren")
        return embed

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Nicht dein Menü!", ephemeral=True)
            return
        selected_dates = interaction.data.get("values", [])
        removed = sum(1 for ds in selected_dates if remove_absence(str(self.user_id), ds))
        self._rebuild()
        embed = self.build_embed()
        embed.set_author(name=f"✅ {removed} Abmeldung(en) entfernt")
        await interaction.response.edit_message(embed=embed, view=self if self.children else None)


# ═══════════════════════════════════════════════════════════════════════════
# ACTIVITY COG
# ═══════════════════════════════════════════════════════════════════════════

class ActivityCog(commands.Cog, name="Activity"):
    """Twitch-Aktivitäts-Tracking, Team-Übersicht, Abmeldungen, Reviews & Clip-Posts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._stream_was_live: bool = False
        init_flag, saved_clips = get_clip_tracker_state()
        self._clips_initialized: bool = init_flag
        self._known_clip_ids: set[str] = saved_clips

    async def cog_load(self) -> None:
        self.review_scheduler.start()
        self.stream_live_tracker.start()
        self.clip_tracker.start()

    async def cog_unload(self) -> None:
        self.review_scheduler.cancel()
        self.stream_live_tracker.cancel()
        self.clip_tracker.cancel()

    # ── Background Loops ──────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def review_scheduler(self) -> None:
        if not REVIEW_CHANNEL_ID:
            return
        now_de = now_berlin()
        today = now_de.date()
        last_day = calendar.monthrange(today.year, today.month)[1]

        if today.day == last_day and now_de.hour == 22 and now_de.minute == 0:
            key = f"m-{today.year}-{today.month}"
            if not is_review_already_sent(key):
                channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
                if channel and getattr(channel, "guild", None):
                    mark_review_already_sent(key)
                    try:
                        await send_monthly_review(channel, channel.guild, today.year, today.month)
                    except Exception as e:
                        print(f"❌ [Review] Monatsbericht Fehler: {e}")

        if today.month == 12 and today.day == 1 and now_de.hour == 22 and now_de.minute == 0:
            key = f"y-{today.year}"
            if not is_review_already_sent(key):
                channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
                if channel and getattr(channel, "guild", None):
                    mark_review_already_sent(key)
                    try:
                        await send_yearly_review(channel, channel.guild, today.year)
                    except Exception as e:
                        print(f"❌ [Review] Jahresbericht Fehler: {e}")

    @review_scheduler.before_loop
    async def _before_reviews(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=2)
    async def stream_live_tracker(self) -> None:
        """Prüft alle 2 Minuten den Twitch-Livestatus & pflegt Stream-Metadaten."""
        if not STREAMER_CHANNEL:
            return
        is_live = await check_streamer_live()
        if is_live:
            today_s = str(today_berlin())
            data = load_data()
            if today_s not in data["streams"]:
                data["streams"][today_s] = []
                save_data(data, force=True)
            if not self._stream_was_live:
                self._stream_was_live = True
                print(f"🟢 [StreamTracker] {STREAMER_CHANNEL} ist LIVE ({today_s})")
        elif self._stream_was_live:
            self._stream_was_live = False
            print(f"🔴 [StreamTracker] {STREAMER_CHANNEL} ist OFFLINE")

    @stream_live_tracker.before_loop
    async def _before_stream_tracker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=2)
    async def clip_tracker(self) -> None:
        if not CLIPS_CHANNEL_ID or not STREAMER_CHANNEL:
            return
        clips = await fetch_recent_clips(limit=20)
        if not clips:
            return

        if not self._clips_initialized:
            for c in clips:
                if c.get("id"):
                    self._known_clip_ids.add(c["id"])
            self._clips_initialized = True
            save_clip_tracker_state(True, self._known_clip_ids)
            return

        new_clips = [c for c in clips if c.get("id") and c["id"] not in self._known_clip_ids]
        for clip in reversed(new_clips):
            self._known_clip_ids.add(clip["id"])
            save_clip_tracker_state(True, self._known_clip_ids)
            clip_title = clip.get("title", "Neuer Clip")
            clip_url = clip.get("url", "")
            creator = clip.get("creator_name", "Unbekannt")
            thumb = (clip.get("thumbnail_url") or "").replace("%{width}", "1280").replace("%{height}", "720")
            duration = clip.get("duration", 0)
            views = clip.get("view_count", 0)
            created_at = clip.get("created_at", "")
            try:
                created_dt = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                created_disp = f"<t:{int(created_dt.timestamp())}:R>"
            except Exception:
                created_disp = created_at[:10] or "Gerade eben"

            embed = discord.Embed(
                title=f"🎬 {clip_title}",
                url=clip_url,
                color=COLOR_TWITCH,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            embed.add_field(
                name="👤 Erstellt von",
                value=f"[{creator}](https://twitch.tv/{creator.lower()})",
                inline=True,
            )
            embed.add_field(name="🕐 Erstellt", value=created_disp, inline=True)
            embed.add_field(name="⏱️ Länge", value=f"{duration:.0f}s", inline=True)
            if views > 0:
                embed.add_field(name="👁️ Views", value=f"{views:,}", inline=True)
            if thumb:
                embed.set_image(url=thumb)
            embed.set_footer(text=f"Twitch Clips • {STREAMER_CHANNEL}")

            for guild in self.bot.guilds:
                ch = guild.get_channel(CLIPS_CHANNEL_ID)
                if ch:
                    try:
                        await ch.send(content=f"🎬 **Neuer Clip!** [{clip_title}]({clip_url})", embed=embed)
                    except Exception as e:
                        print(f"⚠️ [ClipTracker] Senden fehlgeschlagen: {e}")
                    break

    @clip_tracker.before_loop
    async def _before_clips(self) -> None:
        await self.bot.wait_until_ready()

    # ── Slash Commands: Verknüpfung & Einzelberichte ──────────────────────

    @app_commands.command(name="connect", description="🔗 Verknüpfe deinen Twitch-Account mit dem Aktivitäts-Tracker")
    @app_commands.describe(twitch_name="Dein exakter Twitch-Benutzername")
    @is_allowed_channel()
    async def cmd_connect(self, interaction: discord.Interaction, twitch_name: str) -> None:
        if not has_mod_role(interaction.user):
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description=f"Du benötigst die Rolle **{MOD_ROLE_NAME}**.",
                color=COLOR_ERROR,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        clean_name = twitch_name.strip().lstrip("@").lower()
        if len(clean_name) < 2 or " " in clean_name:
            await interaction.response.send_message("❌ Ungültiger Twitch-Benutzername.", ephemeral=True)
            return

        data = load_data()
        user_id = str(interaction.user.id)
        for uid, info in data["users"].items():
            if info.get("twitch_name", "").lower() == clean_name and uid != user_id:
                embed = discord.Embed(
                    title="❌ Bereits vergeben",
                    description=f"`{clean_name}` ist bereits mit einem anderen Discord-Account verknüpft.",
                    color=COLOR_ERROR,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        existing = data["users"].get(user_id, {})
        connected_at = existing.get("connected_at") or str(today_berlin())
        data["users"][user_id] = {
            "twitch_name": clean_name,
            "display_name": interaction.user.display_name,
            "connected_at": connected_at,
        }
        save_data(data, force=True)

        embed = discord.Embed(
            title="🔗 Erfolgreich verknüpft!",
            description=(
                f">>> **Discord:** {interaction.user.mention}\n"
                f"**Twitch:** [{clean_name}](https://twitch.tv/{clean_name})\n"
                f"**Tracking ab:** `{fmt_iso_de(connected_at)}`\n\n"
                f"✅ Anwesenheit, Präsenzdauer & Chat-Aktivität werden ab sofort präzise erfasst!"
            ),
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="disconnect", description="🔓 Entferne deine Twitch-Verknüpfung")
    @is_allowed_channel()
    async def cmd_disconnect(self, interaction: discord.Interaction) -> None:
        data = load_data()
        user_id = str(interaction.user.id)
        if user_id not in data["users"]:
            await interaction.response.send_message("❌ Kein Account verknüpft.", ephemeral=True)
            return
        old_name = data["users"][user_id].get("twitch_name", "?")
        del data["users"][user_id]
        save_data(data, force=True)

        embed = discord.Embed(
            title="🔓 Verknüpfung entfernt",
            description=f"Account **{old_name}** wurde vom Aktivitäts-Tracking getrennt.",
            color=COLOR_ERROR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="activity", description="📊 Zeigt den interaktiven Aktivitätsbericht eines Users")
    @app_commands.describe(
        member="Der User dessen Aktivität du sehen willst",
        monat="Monat (1-12) — Standard: aktueller Monat",
        jahr="Jahr — Standard: aktuelles Jahr",
    )
    @is_allowed_channel()
    async def cmd_activity(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        monat: int | None = None,
        jahr: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        today = today_berlin()
        await build_and_send_activity(
            interaction,
            member,
            monat if monat is not None else today.month,
            jahr if jahr is not None else today.year,
        )

    @app_commands.command(name="myactivity", description="📊 Zeigt deine eigene Aktivität (mit Monats- & Zeit-Navigation)")
    @is_allowed_channel()
    async def cmd_myactivity(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        today = today_berlin()
        await build_and_send_activity(interaction, interaction.user, today.month, today.year)

    @app_commands.command(name="compare", description="⚔️ Vergleiche die Aktivität zweier Mods in 7 Kategorien")
    @app_commands.describe(user_1="Erster User", user_2="Zweiter User")
    @is_allowed_channel()
    async def cmd_compare(
        self,
        interaction: discord.Interaction,
        user_1: discord.Member,
        user_2: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        uid1, uid2 = str(user_1.id), str(user_2.id)

        missing = [u.mention for u, uid in ((user_1, uid1), (user_2, uid2)) if uid not in data["users"]]
        if missing:
            await interaction.followup.send(f"❌ Nicht registriert: {', '.join(missing)}", ephemeral=True)
            return
        if uid1 == uid2:
            await interaction.followup.send(
                "❌ Du kannst keinen User mit sich selbst vergleichen!", ephemeral=True
            )
            return

        s1 = get_user_compare_stats(data, uid1)
        s2 = get_user_compare_stats(data, uid2)

        def wa(v1: float, v2: float) -> tuple[str, str]:
            if v1 > v2:
                return "◀️", ""
            if v2 > v1:
                return "", "▶️"
            return "🤝", "🤝"

        asc = wa(s1["score"], s2["score"])
        aq = wa(s1["pct"], s2["pct"])
        ap = wa(s1["present"], s2["present"])
        atm = wa(s1["active_minutes"], s2["active_minutes"])
        ast = wa(s1["current_streak"], s2["current_streak"])
        al = wa(s1["longest_streak"], s2["longest_streak"])
        am = wa(s1["messages"], s2["messages"])

        checks = [
            (s1["score"], s2["score"]),
            (s1["pct"], s2["pct"]),
            (s1["present"], s2["present"]),
            (s1["active_minutes"], s2["active_minutes"]),
            (s1["current_streak"], s2["current_streak"]),
            (s1["longest_streak"], s2["longest_streak"]),
            (s1["messages"], s2["messages"]),
        ]
        score1 = sum(1 for a, b in checks if a > b)
        score2 = sum(1 for a, b in checks if b > a)

        if score1 > score2:
            winner_text = f"🏆 **{user_1.display_name}** gewinnt! (**{score1}:{score2}** Kategorien)"
            color = 0x3498DB
        elif score2 > score1:
            winner_text = f"🏆 **{user_2.display_name}** gewinnt! (**{score2}:{score1}** Kategorien)"
            color = 0xE74C3C
        else:
            winner_text = f"🤝 **Unentschieden!** (**{score1}:{score2}**)"
            color = COLOR_GOLD

        embed = discord.Embed(
            title="⚔️ Direktvergleich der Stream-Aktivität",
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(
            name=f"📊 {user_1.display_name}",
            value=(
                f"🟣 [{s1['twitch']}](https://twitch.tv/{s1['twitch']})\n"
                f"🎯 Score: **{s1['score']:.0f}/100** {asc[0]}\n"
                f"📈 Quote: **{s1['pct']:.1f}%** ({s1['grade_emoji']} {s1['grade']}) {aq[0]}\n"
                f"✅ Streams: **{s1['present']}/{s1['total_days']}** {ap[0]}\n"
                f"⏱️ Präsenz: **{format_minutes_hm(s1['active_minutes'])}** {atm[0]}\n"
                f"🔥 Streak: **{s1['current_streak']}** *(Max {s1['longest_streak']})* {ast[0]}{al[0]}\n"
                f"💬 Chat: **{s1['messages']:,}** {am[0]}\n"
                f"🕐 Zuletzt: `{s1['last_seen']}`"
            ),
            inline=True,
        )
        embed.add_field(name="⚡", value="**VS**", inline=True)
        embed.add_field(
            name=f"📊 {user_2.display_name}",
            value=(
                f"🟣 [{s2['twitch']}](https://twitch.tv/{s2['twitch']})\n"
                f"{asc[1]} 🎯 Score: **{s2['score']:.0f}/100**\n"
                f"{aq[1]} 📈 Quote: **{s2['pct']:.1f}%** ({s2['grade_emoji']} {s2['grade']})\n"
                f"{ap[1]} ✅ Streams: **{s2['present']}/{s2['total_days']}**\n"
                f"{atm[1]} ⏱️ Präsenz: **{format_minutes_hm(s2['active_minutes'])}**\n"
                f"{ast[1]}{al[1]} 🔥 Streak: **{s2['current_streak']}** *(Max {s2['longest_streak']})*\n"
                f"{am[1]} 💬 Chat: **{s2['messages']:,}**\n"
                f"🕐 Zuletzt: `{s2['last_seen']}`"
            ),
            inline=True,
        )
        embed.add_field(name="🎯 Gesamtergebnis", value=winner_text, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="🏆 Interaktives Aktivitäts-Ranking (Monat, 30 Tage, Jahr, Gesamt)")
    @is_allowed_channel()
    async def cmd_leaderboard(self, interaction: discord.Interaction) -> None:
        data = load_data()
        if not data["users"]:
            await interaction.response.send_message("❌ Keine User registriert.", ephemeral=True)
            return
        view = LeaderboardView(interaction.user.id, scope="month")
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    # ── Neue Team- & Stream-Übersichts-Commands ───────────────────────────

    @app_commands.command(
        name="teamstatus",
        description="🛡️ Echtzeit-Überblick über alle Mods (Live-Präsenz, Monats-Quoten & Abmeldungen)",
    )
    @is_allowed_channel()
    async def cmd_teamstatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await check_streamer_live()
        data = load_data()
        users = data.get("users", {})
        if not users:
            await interaction.followup.send("❌ Noch keine Mods mit `/connect` registriert.", ephemeral=True)
            return

        today = today_berlin()
        today_s = str(today)
        is_live = is_stream_currently_live_cached()
        live_info = get_live_stream_info_cached()

        if is_live and live_info:
            game = live_info.get("game_name") or "Unbekannt"
            viewers = int(live_info.get("viewer_count") or 0)
            live_banner = (
                f"🟢 **LIVE auf Twitch:** [{STREAMER_CHANNEL}](https://twitch.tv/{STREAMER_CHANNEL}) — "
                f"**{game}** (`{viewers:,}` Viewer)"
            )
        else:
            live_banner = f"⚫ **{STREAMER_CHANNEL or 'Streamer'}** ist aktuell offline."

        rows = []
        for uid, uinfo in users.items():
            ms = get_month_activity_stats(data, uid, today.year, today.month)
            last_seen, missed_since = get_days_since_last_seen(data, uid)
            active_today = user_was_live_in_chat(data, uid, today_s)
            absent_today = is_user_absent(uid, today_s)

            if active_today:
                badge = "🟢 Heute da"
            elif absent_today:
                badge = "📋 Abgemeldet"
            elif missed_since >= 3:
                badge = f"⚠️ {missed_since}x gefehlt"
            else:
                badge = f"⚪ Zuletzt {fmt_iso_de(last_seen)[:6]}"

            rows.append((
                ms["score"],
                f"{ms['grade_emoji']} **{uinfo.get('display_name', '?')}** (`{uinfo.get('twitch_name', '?')}`) — {badge}\n"
                f"╰ Monat: **{ms['present']}/{ms['stream_days']}** ({ms['pct']:.0f}% • Note **{ms['grade']}**) • "
                f"🎯 `{ms['score']:.0f}` • ⏱️ `{format_minutes_hm(ms['messages']['active_minutes_total'])}`",
            ))

        rows.sort(key=lambda x: x[0], reverse=True)
        embed = discord.Embed(
            title=f"🛡️ Mod-Team Status — {MONTH_NAMES[today.month]} {today.year}",
            description=f"{live_banner}\n\n" + "\n\n".join(line for _, line in rows[:18]),
            color=COLOR_SUCCESS if is_live else COLOR_INFO,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text=f"{len(users)} registrierte Moderatoren")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="streams",
        description="📺 Zeigt die Historie der letzten Streams inkl. Mod-Anwesenheit & Kategorien",
    )
    @is_allowed_channel()
    async def cmd_streams(self, interaction: discord.Interaction) -> None:
        view = StreamHistoryView(interaction.user.id, page=0)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @app_commands.command(
        name="checkinactive",
        description="🔍 Findet Mods mit niedriger Monatsquote oder mehreren verpassten Streams",
    )
    @app_commands.describe(min_quote="Warnschwelle in Prozent (Standard: 50)")
    @is_allowed_channel()
    async def cmd_checkinactive(self, interaction: discord.Interaction, min_quote: int = 50) -> None:
        if not has_mod_role(interaction.user):
            await interaction.response.send_message(
                f"❌ Du benötigst die Rolle **{MOD_ROLE_NAME}**.", ephemeral=True
            )
            return

        data = load_data()
        today = today_berlin()
        flagged = []
        for uid, uinfo in data.get("users", {}).items():
            ms = get_month_activity_stats(data, uid, today.year, today.month)
            last_seen, missed_since = get_days_since_last_seen(data, uid)
            if (ms["stream_days"] >= 2 and ms["pct"] < min_quote) or missed_since >= 3:
                flagged.append((
                    ms["pct"],
                    f"⚠️ **{uinfo.get('display_name', '?')}** (`{uinfo.get('twitch_name', '?')}`)\n"
                    f"╰ Monatsquote: **{ms['pct']:.0f}%** ({ms['present']}/{ms['stream_days']}) • "
                    f"Unentschuldigt in Folge: **{missed_since}** • Zuletzt da: `{fmt_iso_de(last_seen)}`",
                ))

        flagged.sort(key=lambda x: x[0])
        if not flagged:
            embed = discord.Embed(
                title="✅ Alle Moderatoren im grünen Bereich!",
                description=f"Kein registrierter Mod liegt aktuell unter **{min_quote}%** oder hat ≥3 Streams unentschuldigt verpasst.",
                color=COLOR_SUCCESS,
            )
        else:
            embed = discord.Embed(
                title=f"🔍 Inaktivitäts-Check (< {min_quote}% oder ≥3 verpasste Streams)",
                description="\n\n".join(line for _, line in flagged),
                color=COLOR_WARNING,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Manuelle Monats- & Jahresberichte ─────────────────────────────────

    @app_commands.command(name="monthlyreview", description="📅 Sendet den Monatsbericht manuell (nur Admins)")
    @app_commands.describe(monat="Monat (1-12)", jahr="Jahr")
    @is_admin_user()
    async def cmd_monthlyreview(
        self,
        interaction: discord.Interaction,
        monat: int | None = None,
        jahr: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        today = today_berlin()
        m = monat if monat is not None else today.month
        y = jahr if jahr is not None else today.year
        if m < 1 or m > 12:
            await interaction.followup.send("❌ Ungültiger Monat (1–12).", ephemeral=True)
            return
        review_channel = self.bot.get_channel(REVIEW_CHANNEL_ID) or interaction.channel
        await send_monthly_review(review_channel, interaction.guild, y, m)
        await interaction.followup.send(
            f"✅ Monatsbericht für **{MONTH_NAMES[m]} {y}** gesendet!", ephemeral=True
        )

    @app_commands.command(name="yearlyreview", description="🎆 Sendet den Jahresbericht manuell (nur Admins)")
    @app_commands.describe(jahr="Jahr — Standard: aktuelles Jahr")
    @is_admin_user()
    async def cmd_yearlyreview(
        self, interaction: discord.Interaction, jahr: int | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        y = jahr if jahr is not None else today_berlin().year
        review_channel = self.bot.get_channel(REVIEW_CHANNEL_ID) or interaction.channel
        await send_yearly_review(review_channel, interaction.guild, y)
        await interaction.followup.send(f"✅ Jahresbericht für **{y}** gesendet!", ephemeral=True)

    # ── Slash Commands: Abwesenheiten ─────────────────────────────────────

    @app_commands.command(
        name="absent",
        description="📋 Melde dich für einen Tag oder Zeitraum ab (Streak & Quote geschützt)",
    )
    @app_commands.describe(
        grund="Grund für die Abwesenheit",
        datum="Startdatum (heute, morgen oder TT.MM.JJJJ) — leer = heute",
        bis_datum="Optionales Enddatum (TT.MM.JJJJ) für mehrtägige Abwesenheit",
    )
    @is_allowed_channel()
    async def cmd_absent(
        self,
        interaction: discord.Interaction,
        grund: str = "Kein Grund angegeben",
        datum: str | None = None,
        bis_datum: str | None = None,
    ) -> None:
        if not has_mod_role(interaction.user):
            await interaction.response.send_message(
                f"❌ Du benötigst die Rolle **{MOD_ROLE_NAME}**.", ephemeral=True
            )
            return

        data = load_data()
        uid = str(interaction.user.id)
        if uid not in data["users"]:
            await interaction.response.send_message(
                "❌ Du hast keinen verknüpften Twitch-Account! Nutze `/connect` zuerst.",
                ephemeral=True,
            )
            return

        today = today_berlin()
        start_d = parse_flexible_date(datum, today)
        end_d = parse_flexible_date(bis_datum, start_d) if (start_d and bis_datum) else start_d
        if start_d is None or end_d is None:
            await interaction.response.send_message(
                "❌ Ungültiges Datum! Nutze `heute`, `morgen`, `TT.MM.` oder `TT.MM.JJJJ`.",
                ephemeral=True,
            )
            return

        if start_d < today:
            await interaction.response.send_message(
                "❌ Du kannst dich nicht rückwirkend abmelden!", ephemeral=True
            )
            return
        if end_d < start_d:
            await interaction.response.send_message(
                "❌ `bis_datum` darf nicht vor dem Startdatum liegen!", ephemeral=True
            )
            return
        if (end_d - start_d).days > 60:
            await interaction.response.send_message(
                "❌ Maximal 60 Tage am Stück pro Abmeldung möglich.", ephemeral=True
            )
            return

        if start_d == end_d:
            ds = str(start_d)
            if is_user_absent(uid, ds):
                await interaction.response.send_message(
                    f"❌ Du bist bereits für **{start_d.strftime('%d.%m.%Y')}** abgemeldet!",
                    ephemeral=True,
                )
                return
            add_absence(uid, ds, grund)
            period_label = f"**{start_d.strftime('%d.%m.%Y')}** ({WEEKDAY_SHORT[start_d.weekday()]})"
        else:
            added = add_absence_range(uid, start_d, end_d, grund)
            period_label = (
                f"**{start_d.strftime('%d.%m.%Y')}** bis **{end_d.strftime('%d.%m.%Y')}** "
                f"({len(added)} Tage)"
            )

        embed = discord.Embed(
            title="📋 Abwesenheit eingetragen",
            description=(
                f"Zeitraum: {period_label}\n"
                f"📝 **Grund:** {grund}\n"
                f"✅ **Deine Streak & Anwesenheitsquote bleiben währenddessen geschützt!**"
            ),
            color=COLOR_ABSENCE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        log_embed = discord.Embed(
            title="📋 Abwesenheit gemeldet",
            color=COLOR_ABSENCE,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.add_field(name="👤 User", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="📅 Zeitraum", value=period_label, inline=True)
        log_embed.add_field(name="📝 Grund", value=grund, inline=False)
        await send_log(log_embed, self.bot)

    @app_commands.command(name="unabsent", description="📋 Entferne eine Abwesenheitsmeldung")
    @app_commands.describe(datum="Datum der Abwesenheit (heute, morgen oder TT.MM.JJJJ) — leer = heute")
    @is_allowed_channel()
    async def cmd_unabsent(
        self, interaction: discord.Interaction, datum: str | None = None
    ) -> None:
        if not has_mod_role(interaction.user):
            await interaction.response.send_message(
                f"❌ Du benötigst die Rolle **{MOD_ROLE_NAME}**.", ephemeral=True
            )
            return

        uid = str(interaction.user.id)
        parsed = parse_flexible_date(datum, today_berlin())
        if parsed is None:
            await interaction.response.send_message(
                "❌ Ungültiges Datum! Format: `heute`, `morgen` oder `TT.MM.JJJJ`", ephemeral=True
            )
            return

        date_str = str(parsed)
        if not remove_absence(uid, date_str):
            await interaction.response.send_message(
                f"❌ Du bist nicht für **{parsed.strftime('%d.%m.%Y')}** abgemeldet!", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="✅ Abwesenheit entfernt",
            description=f"Deine Abmeldung für **{parsed.strftime('%d.%m.%Y')}** wurde aufgehoben.",
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="absences",
        description="📋 Zeigt deine anstehenden Abwesenheiten (inkl. 1-Klick-Stornierung)",
    )
    @is_allowed_channel()
    async def cmd_absences(self, interaction: discord.Interaction) -> None:
        view = AbsenceManageView(interaction.user.id)
        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view if view.children else None,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ActivityCog(bot))
