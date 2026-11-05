"""Birthday Cog: Geburtstags-Kalender, 24h-Tagesrolle, Glückwunsch-Embed & Coin-Geschenk."""
from __future__ import annotations

import calendar
import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.checks import has_mod_role, is_user_admin
from bot_core.config import (
    COLOR_ERROR,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_SUCCESS,
    MONTH_NAMES,
    today_berlin,
)
from bot_core.server_store import birthdays_store
from bot_core.storage import update_balance


def _zodiac_sign(day: int, month: int) -> str:
    zodiacs = [
        ((1, 20), "♑ Steinbock"),
        ((2, 19), "♒ Wassermann"),
        ((3, 20), "♓ Fische"),
        ((4, 20), "♈ Widder"),
        ((5, 21), "♉ Stier"),
        ((6, 21), "♊ Zwillinge"),
        ((7, 22), "♋ Krebs"),
        ((8, 23), "♌ Löwe"),
        ((9, 23), "♍ Jungfrau"),
        ((10, 23), "♎ Waage"),
        ((11, 22), "♏ Skorpion"),
        ((12, 21), "♐ Schütze"),
        ((12, 31), "♑ Steinbock"),
    ]
    for (m_end, d_end), label in zodiacs:
        if (month, day) <= (m_end, d_end):
            return label
    return "♑ Steinbock"


def _is_birthday_today(day: int, month: int, today: datetime.date) -> bool:
    if month == today.month and day == today.day:
        return True
    # 29. Februar in Nicht-Schaltjahren am 28. Februar feiern
    if month == 2 and day == 29 and not calendar.isleap(today.year):
        return today.month == 2 and today.day == 28
    return False


def _next_birthday_date(day: int, month: int, today: datetime.date) -> datetime.date:
    for y in (today.year, today.year + 1):
        d_eff = 28 if (month == 2 and day == 29 and not calendar.isleap(y)) else day
        cand = datetime.date(y, month, d_eff)
        if cand >= today:
            return cand
    return datetime.date(today.year + 1, month, 28)


def get_user_birthday_info(guild_id: int, user_id: int) -> dict[str, Any] | None:
    gdata = birthdays_store.get_guild(guild_id)
    entry = (gdata.get("users") or {}).get(str(user_id))
    return dict(entry) if isinstance(entry, dict) else None


class BirthdayGroup(app_commands.Group):
    """Slash-Command-Gruppe `/birthday ...`."""

    def __init__(self, cog: BirthdaysCog) -> None:
        super().__init__(name="birthday", description="🎂 Geburtstage eintragen, anzeigen & verwalten")
        self.cog = cog

    @app_commands.command(name="set", description="🎂 Trage deinen Geburtstag ein")
    @app_commands.describe(
        tag="Tag (1–31)",
        monat="Monat (1–12)",
        jahr="Optional: Geburtsjahr (z. B. 2003 – zeigt dein Alter an)",
    )
    async def cmd_set(
        self,
        interaction: discord.Interaction,
        tag: app_commands.Range[int, 1, 31],
        monat: app_commands.Range[int, 1, 12],
        jahr: app_commands.Range[int, 1930, 2022] | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Nur auf einem Server nutzbar.", ephemeral=True)
            return
        check_year = jahr if jahr is not None else 2024  # 2024 erlaubt auch den 29.02.
        try:
            datetime.date(check_year, monat, tag)
        except ValueError:
            await interaction.response.send_message(
                f"❌ Das Datum `{tag:02d}.{monat:02d}.` existiert nicht!", ephemeral=True
            )
            return

        uid_s = str(interaction.user.id)

        def _save(g: dict[str, Any]) -> None:
            prev = g["users"].get(uid_s) or {}
            g["users"][uid_s] = {
                "day": int(tag),
                "month": int(monat),
                "year": int(jahr) if jahr else None,
                "celebrated_year": int(prev.get("celebrated_year") or 0),
            }

        birthdays_store.mutate(interaction.guild.id, _save)
        today = today_berlin()
        nxt = _next_birthday_date(tag, monat, today)
        days_left = (nxt - today).days
        year_str = f"{jahr}" if jahr else "*(ohne Jahr)*"

        embed = discord.Embed(
            title="🎂 Geburtstag gespeichert!",
            description=(
                f"Dein Geburtstag wurde auf den **{tag:02d}. {MONTH_NAMES[monat]}** {year_str} gesetzt.\n"
                f"**Sternzeichen:** {_zodiac_sign(tag, monat)}\n"
                f"**Countdown:** {'🎉 **HEUTE!**' if days_left == 0 else f'in **{days_left}** Tagen'}"
            ),
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.cog.process_guild_birthdays(interaction.guild)

    @app_commands.command(name="remove", description="🗑️ Löscht deinen gespeicherten Geburtstag")
    async def cmd_remove(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        uid_s = str(interaction.user.id)
        removed = birthdays_store.mutate(
            interaction.guild.id, lambda g: g["users"].pop(uid_s, None) is not None
        )
        if not removed:
            await interaction.response.send_message("ℹ️ Du hast keinen Geburtstag eingetragen.", ephemeral=True)
            return
        await interaction.response.send_message("🗑️ Dein Geburtstag wurde entfernt.", ephemeral=True)
        await self.cog.process_guild_birthdays(interaction.guild)

    @app_commands.command(name="show", description="🎂 Zeigt den Geburtstag eines Users & den Countdown")
    @app_commands.describe(member="Optional: anderer User")
    async def cmd_show(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        info = get_user_birthday_info(interaction.guild.id, target.id)
        if not info:
            await interaction.response.send_message(
                f"ℹ️ **{target.display_name}** hat noch keinen Geburtstag eingetragen (`/birthday set`).",
                ephemeral=True,
            )
            return
        d, m, y = int(info["day"]), int(info["month"]), info.get("year")
        today = today_berlin()
        nxt = _next_birthday_date(d, m, today)
        days_left = (nxt - today).days
        age_txt = f" • wird **{nxt.year - int(y)}**" if y else ""
        when_txt = "🎉 **HEUTE!**" if days_left == 0 else f"in **{days_left}** Tagen (`{nxt.strftime('%d.%m.%Y')}`)"

        embed = discord.Embed(
            title=f"🎂 Geburtstag – {target.display_name}",
            color=COLOR_GOLD,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name="📅 Datum",
            value=f"**{d:02d}. {MONTH_NAMES[m]}**" + (f" {y}" if y else ""),
            inline=True,
        )
        embed.add_field(name="✨ Sternzeichen", value=_zodiac_sign(d, m), inline=True)
        embed.add_field(name="⏳ Nächster Geburtstag", value=f"{when_txt}{age_txt}", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="upcoming", description="📅 Zeigt die nächsten anstehenden Geburtstage auf dem Server")
    async def cmd_upcoming(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        gdata = birthdays_store.get_guild(interaction.guild.id)
        users_map: dict[str, Any] = gdata.get("users") or {}
        today = today_berlin()

        upcoming_list: list[tuple[int, datetime.date, int, dict[str, Any]]] = []
        for uid_s, entry in users_map.items():
            if not uid_s.isdigit() or not isinstance(entry, dict):
                continue
            uid = int(uid_s)
            member = interaction.guild.get_member(uid)
            if member is None:
                continue
            d, m = int(entry.get("day", 1)), int(entry.get("month", 1))
            nxt = _next_birthday_date(d, m, today)
            upcoming_list.append(((nxt - today).days, nxt, uid, entry))

        if not upcoming_list:
            await interaction.response.send_message(
                "ℹ️ Noch keine Geburtstage eingetragen! Nutze `/birthday set`.", ephemeral=True
            )
            return

        upcoming_list.sort(key=lambda x: (x[0], x[2]))
        lines: list[str] = []
        for days_left, nxt, uid, entry in upcoming_list[:12]:
            d, m, y = int(entry["day"]), int(entry["month"]), entry.get("year")
            age_part = f" *(wird {nxt.year - int(y)})*" if y else ""
            badge = "🎂 **HEUTE!**" if days_left == 0 else ("🎈 **Morgen!**" if days_left == 1 else f"in `{days_left}d`")
            lines.append(f"• **`{d:02d}.{m:02d}.`** — <@{uid}>{age_part} — {badge}")

        embed = discord.Embed(
            title=f"🎂 Nächste Geburtstage – {interaction.guild.name}",
            description="\n".join(lines),
            color=COLOR_GOLD,
        )
        embed.set_footer(text=f"{len(upcoming_list)} Geburtstage gespeichert • /birthday set zum Eintragen")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="admin-set", description="🛡️ Setzt den Geburtstag eines Users (Mods/Admins)")
    @app_commands.describe(member="Ziel-User", tag="Tag (1–31)", monat="Monat (1–12)", jahr="Optionales Geburtsjahr")
    async def cmd_admin_set(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tag: app_commands.Range[int, 1, 31],
        monat: app_commands.Range[int, 1, 12],
        jahr: app_commands.Range[int, 1930, 2022] | None = None,
    ) -> None:
        if not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        try:
            datetime.date(jahr or 2024, monat, tag)
        except ValueError:
            await interaction.response.send_message("❌ Ungültiges Kalenderdatum.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> None:
            prev = g["users"].get(str(member.id)) or {}
            g["users"][str(member.id)] = {
                "day": int(tag),
                "month": int(monat),
                "year": int(jahr) if jahr else None,
                "celebrated_year": int(prev.get("celebrated_year") or 0),
            }

        birthdays_store.mutate(interaction.guild.id, _upd)
        await interaction.response.send_message(
            f"✅ Geburtstag für {member.mention} auf **{tag:02d}.{monat:02d}.{jahr or ''}** gesetzt.",
            ephemeral=True,
        )
        await self.cog.process_guild_birthdays(interaction.guild)

    @app_commands.command(name="admin-remove", description="🛡️ Entfernt den Geburtstag eines Users (Mods/Admins)")
    async def cmd_admin_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        birthdays_store.mutate(interaction.guild.id, lambda g: g["users"].pop(str(member.id), None))
        await interaction.response.send_message(
            f"🗑️ Geburtstag von {member.mention} entfernt.", ephemeral=True
        )
        await self.cog.process_guild_birthdays(interaction.guild)

    @app_commands.command(
        name="setup",
        description="⚙️ Konfiguriert Geburtstags-Rolle, Glückwunsch-Kanal & Coin-Geschenk (Admins)",
    )
    @app_commands.describe(
        rolle="Rolle, die Geburtstagskinder für 24 Stunden erhalten",
        kanal="Kanal für die automatische Glückwunsch-Nachricht",
        coin_bonus="Coin-Geschenk zum Geburtstag (0 = kein Bonus, Standard: 2500)",
        aktiv="Birthday-System ein- oder ausschalten",
        nachricht="Optionaler Text ({user}, {username}, {age} als Platzhalter)",
    )
    async def cmd_setup(
        self,
        interaction: discord.Interaction,
        rolle: discord.Role | None = None,
        kanal: discord.TextChannel | None = None,
        coin_bonus: app_commands.Range[int, 0, 1000000] | None = None,
        aktiv: bool | None = None,
        nachricht: str | None = None,
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> dict[str, Any]:
            cfg = g["config"]
            if rolle is not None:
                cfg["role_id"] = rolle.id
            if kanal is not None:
                cfg["channel_id"] = kanal.id
            if coin_bonus is not None:
                cfg["coin_reward"] = int(coin_bonus)
            if aktiv is not None:
                cfg["enabled"] = bool(aktiv)
            if nachricht is not None and nachricht.strip():
                cfg["message"] = nachricht.strip()
            return cfg

        cfg = birthdays_store.mutate(interaction.guild.id, _upd)
        r_disp = f"<@&{cfg['role_id']}>" if cfg.get("role_id") else "*(nicht gesetzt)*"
        c_disp = f"<#{cfg['channel_id']}>" if cfg.get("channel_id") else "*(nicht gesetzt)*"
        embed = discord.Embed(
            title="⚙️ Birthday-System konfiguriert",
            description=(
                f"**Status:** {'✅ Aktiv' if cfg.get('enabled', True) else '⏸️ Deaktiviert'}\n"
                f"**Geburtstags-Rolle (24h):** {r_disp}\n"
                f"**Glückwunsch-Kanal:** {c_disp}\n"
                f"**Coin-Geschenk:** `{cfg.get('coin_reward', 2500):,} 🪙`\n"
                f"**Nachricht:** {cfg.get('message', '')}"
            ),
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.cog.process_guild_birthdays(interaction.guild)


class BirthdaysCog(commands.Cog, name="Birthdays"):
    """Automatische Tages-Geburtstagsrolle, Glückwunsch-Nachricht & Coin-Geschenk."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bday_group = BirthdayGroup(self)
        self.bot.tree.add_command(self.bday_group)

    async def cog_load(self) -> None:
        self.birthday_checker.start()

    async def cog_unload(self) -> None:
        self.birthday_checker.cancel()
        self.bot.tree.remove_command(self.bday_group.name, type=self.bday_group.type)

    async def process_guild_birthdays(self, guild: discord.Guild) -> None:
        gdata = birthdays_store.get_guild(guild.id)
        cfg = gdata.get("config") or {}
        if not cfg.get("enabled", True):
            return

        today = today_berlin()
        role_id = int(cfg.get("role_id") or 0)
        bday_role = guild.get_role(role_id) if role_id else None
        ch_id = int(cfg.get("channel_id") or 0)
        announce_ch = guild.get_channel(ch_id) if ch_id else None
        coin_reward = max(0, int(cfg.get("coin_reward") or 0))
        users_map: dict[str, Any] = gdata.get("users") or {}

        todays_user_ids: set[int] = set()
        to_celebrate: list[tuple[discord.Member, dict[str, Any]]] = []

        for uid_s, entry in users_map.items():
            if not uid_s.isdigit() or not isinstance(entry, dict):
                continue
            uid = int(uid_s)
            d, m = int(entry.get("day") or 0), int(entry.get("month") or 0)
            if not (1 <= m <= 12 and 1 <= d <= 31):
                continue
            if _is_birthday_today(d, m, today):
                todays_user_ids.add(uid)
                member = guild.get_member(uid)
                if member is not None:
                    if bday_role is not None and bday_role not in member.roles:
                        try:
                            await member.add_roles(bday_role, reason="🎂 Geburtstag (24h Tages-Rolle)")
                        except Exception as e:
                            print(f"⚠️ [Birthday] Konnte Rolle nicht an {member} vergeben: {e}")
                    if int(entry.get("celebrated_year") or 0) != today.year:
                        to_celebrate.append((member, entry))

        # Abgelaufene Geburtstagsrollen von gestern zuverlässig entfernen
        if bday_role is not None:
            prev_holders = {int(x) for x in (gdata.get("active_role_holders") or []) if str(x).isdigit()}
            check_members: dict[int, discord.Member] = {m.id: m for m in bday_role.members}
            for hid in prev_holders:
                m_obj = guild.get_member(hid)
                if m_obj is not None and bday_role in m_obj.roles:
                    check_members[hid] = m_obj
            for mid, m_obj in check_members.items():
                if mid not in todays_user_ids:
                    try:
                        await m_obj.remove_roles(bday_role, reason="🎂 Geburtstag vorbei – Tages-Rolle entfernt")
                    except Exception as e:
                        print(f"⚠️ [Birthday] Konnte Rolle nicht von {m_obj} entfernen: {e}")

        celebrated_ids: list[int] = []
        for member, entry in to_celebrate:
            celebrated_ids.append(member.id)
            new_balance = None
            if coin_reward > 0:
                new_balance = update_balance(member.id, coin_reward)

            if isinstance(announce_ch, discord.TextChannel):
                birth_year = entry.get("year")
                age_val = (today.year - int(birth_year)) if birth_year else None
                raw_msg = str(
                    cfg.get("message")
                    or "🎂 Alles Gute zum Geburtstag {user}! Wir wünschen dir einen großartigen Tag! 🎉🥳"
                )
                text = (
                    raw_msg.replace("{user}", member.mention)
                    .replace("{username}", member.display_name)
                    .replace("{age}", str(age_val) if age_val else "")
                )
                embed = discord.Embed(
                    title="🎂🎉 Happy Birthday! 🎉🎂",
                    description=text,
                    color=COLOR_GOLD,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                if age_val and age_val > 0:
                    embed.add_field(name="🎈 Alter", value=f"**{age_val}. Geburtstag**", inline=True)
                if bday_role is not None:
                    embed.add_field(name="👑 Tages-Rolle", value=bday_role.mention, inline=True)
                if coin_reward > 0 and new_balance is not None:
                    embed.add_field(
                        name="🎁 Geburtstags-Geschenk",
                        value=f"**+{coin_reward:,} 🪙** *(Kontostand: {new_balance:,} 🪙)*",
                        inline=False,
                    )
                try:
                    await announce_ch.send(content=member.mention, embed=embed)
                except Exception as e:
                    print(f"⚠️ [Birthday] Konnte Glückwunsch nicht senden: {e}")

        def _commit_state(g: dict[str, Any]) -> None:
            g["active_role_holders"] = sorted(todays_user_ids)
            for cid in celebrated_ids:
                if str(cid) in g["users"]:
                    g["users"][str(cid)]["celebrated_year"] = today.year

        birthdays_store.mutate(guild.id, _commit_state)

    @tasks.loop(minutes=5)
    async def birthday_checker(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self.process_guild_birthdays(guild)
            except Exception as e:
                print(f"⚠️ [Birthday] Loop-Fehler in Guild {guild.id}: {e}")

    @birthday_checker.before_loop
    async def _before_bday(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BirthdaysCog(bot))
