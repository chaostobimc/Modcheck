"""Moderation Cog (Carl-bot, ProBot & MEE6 Ersatz): Cases, Warns + Eskalation, Tempban, Purge, Lock & Embeds."""
from __future__ import annotations

import datetime
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.checks import has_mod_role, is_user_admin, parse_time
from bot_core.config import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    LOG_CHANNEL_ID,
)
from bot_core.server_store import get_dynamic_channel_id, moderation_store, server_cfg_store


def get_active_warn_count(guild_id: int, user_id: int) -> int:
    g = moderation_store.get_guild(guild_id)
    warns = (g.get("warnings") or {}).get(str(user_id)) or []
    return len(warns)


async def log_mod_case(
    guild: discord.Guild,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str,
    duration_str: str | None = None,
) -> int:
    """Erstellt einen fortlaufenden Mod-Case und postet ihn in den Mod-Log-Kanal."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _create(g: dict[str, Any]) -> int:
        cid = int(g.get("next_case_id") or 1)
        g["next_case_id"] = cid + 1
        g.setdefault("cases", {})[str(cid)] = {
            "case_id": cid,
            "action": action,
            "target_id": target.id,
            "target_tag": str(target),
            "mod_id": moderator.id,
            "mod_tag": str(moderator),
            "reason": reason,
            "duration": duration_str,
            "created_at": now_iso,
        }
        return cid

    case_id = moderation_store.mutate(guild.id, _create)

    scfg = server_cfg_store.get_guild(guild.id)
    mod_log_id = (
        int(((scfg.get("logs") or {}).get("mod") or {}).get("channel_id") or 0)
        or int(((scfg.get("logs") or {}).get("server") or {}).get("channel_id") or 0)
        or get_dynamic_channel_id(guild.id, "log_channel_id", LOG_CHANNEL_ID)
    )
    if mod_log_id:
        ch = guild.get_channel(mod_log_id)
        if isinstance(ch, discord.TextChannel):
            color_map = {
                "WARN": COLOR_WARNING,
                "TIMEOUT": COLOR_WARNING,
                "KICK": COLOR_ERROR,
                "BAN": COLOR_ERROR,
                "TEMPBAN": COLOR_ERROR,
                "UNBAN": COLOR_SUCCESS,
                "UNTIMEOUT": COLOR_SUCCESS,
            }
            embed = discord.Embed(
                title=f"🛡️ Case #{case_id} • {action}",
                color=color_map.get(action.upper(), COLOR_INFO),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            embed.add_field(name="👤 User", value=f"{target.mention} (`{target.id}`)", inline=True)
            embed.add_field(name="👮 Moderator", value=f"{moderator.mention}", inline=True)
            if duration_str:
                embed.add_field(name="⏱️ Dauer", value=f"`{duration_str}`", inline=True)
            embed.add_field(name="📝 Grund", value=reason or "Kein Grund angegeben", inline=False)
            try:
                await ch.send(embed=embed)
            except Exception:
                pass
    return case_id


class EmbedBuilderModal(discord.ui.Modal):
    """Interaktives Modal zum Erstellen oder Bearbeiten von Custom-Embeds."""

    titel = discord.ui.TextInput(
        label="Embed-Titel",
        placeholder="z. B. 📜 Server-Regeln",
        required=True,
        max_length=256,
    )
    beschreibung = discord.ui.TextInput(
        label="Text / Beschreibung (Markdown unterstützt)",
        style=discord.TextStyle.paragraph,
        placeholder="Dein Text hier …",
        required=True,
        max_length=4000,
    )
    farbe = discord.ui.TextInput(
        label="Hex-Farbe (optional, z. B. #5865F2)",
        placeholder="#5865F2",
        required=False,
        max_length=10,
    )
    bild_url = discord.ui.TextInput(
        label="Bild-/Banner-URL (optional)",
        placeholder="https://...",
        required=False,
        max_length=500,
    )
    footer = discord.ui.TextInput(
        label="Footer-Text (optional)",
        placeholder="Stand: 2026",
        required=False,
        max_length=200,
    )

    def __init__(
        self,
        target_channel: discord.TextChannel,
        edit_message: discord.Message | None = None,
    ) -> None:
        super().__init__(
            title="🎨 Embed bearbeiten" if edit_message else "🎨 Neues Embed senden"
        )
        self.target_channel = target_channel
        self.edit_message = edit_message
        if edit_message and edit_message.embeds:
            emb0 = edit_message.embeds[0]
            if emb0.title:
                self.titel.default = emb0.title
            if emb0.description:
                self.beschreibung.default = emb0.description
            if emb0.color:
                self.farbe.default = f"#{emb0.color.value:06X}"
            if emb0.image and emb0.image.url:
                self.bild_url.default = emb0.image.url
            if emb0.footer and emb0.footer.text:
                self.footer.default = emb0.footer.text

    async def on_submit(self, interaction: discord.Interaction) -> None:
        col = COLOR_INFO
        raw_hex = (self.farbe.value or "").strip().lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{6}", raw_hex):
            col = int(raw_hex, 16)

        embed = discord.Embed(
            title=self.titel.value.strip(),
            description=self.beschreibung.value,
            color=col,
        )
        if self.bild_url.value and self.bild_url.value.strip().startswith(("http://", "https://")):
            embed.set_image(url=self.bild_url.value.strip())
        if self.footer.value and self.footer.value.strip():
            embed.set_footer(text=self.footer.value.strip())

        try:
            if self.edit_message:
                await self.edit_message.edit(embed=embed)
                await interaction.response.send_message("✅ Embed aktualisiert!", ephemeral=True)
            else:
                msg = await self.target_channel.send(embed=embed)
                await interaction.response.send_message(
                    f"✅ Embed in {self.target_channel.mention} gesendet (`ID: {msg.id}`)!",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler beim Senden: `{e}`", ephemeral=True)


class ModGroup(app_commands.Group):
    """Slash-Command-Gruppe `/mod ...` (max. 25 Subcommands)."""

    def __init__(self, cog: ModerationCog) -> None:
        super().__init__(
            name="mod",
            description="🛡️ Moderation: Warns, Timeouts, Bans, Purge, Lock, Cases & Embed-Builder",
        )
        self.cog = cog

    @staticmethod
    def _can_act_on(actor: discord.Member, target: discord.Member) -> str | None:
        if target.id == actor.id:
            return "❌ Du kannst diese Aktion nicht auf dich selbst anwenden."
        if target.id == actor.guild.owner_id:
            return "❌ Der Server-Eigentümer kann nicht moderiert werden."
        if actor.id != actor.guild.owner_id and target.top_role >= actor.top_role:
            return "❌ Dieser User hat eine gleichwertige oder höhere Rolle als du."
        return None

    @app_commands.command(name="warn", description="⚠️ Verwarnt einen User (inkl. automatischer Eskalation)")
    @app_commands.describe(member="Zu verwarnender User", grund="Grund der Verwarnung")
    async def cmd_warn(
        self, interaction: discord.Interaction, member: discord.Member, grund: str
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Moderations-Berechtigung.", ephemeral=True)
            return
        if err := self._can_act_on(interaction.user, member):
            await interaction.response.send_message(err, ephemeral=True)
            return

        case_id = await log_mod_case(interaction.guild, "WARN", member, interaction.user, grund)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        def _add_w(g: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            wlist = g.setdefault("warnings", {}).setdefault(str(member.id), [])
            wid = (max((int(x.get("id") or 0) for x in wlist), default=0)) + 1
            wlist.append(
                {
                    "id": wid,
                    "case_id": case_id,
                    "mod_id": interaction.user.id,
                    "reason": grund,
                    "created_at": now_iso,
                }
            )
            return len(wlist), dict(g.get("warn_escalation") or {})

        warn_total, esc_map = moderation_store.mutate(interaction.guild.id, _add_w)

        try:
            await member.send(
                f"⚠️ Du wurdest auf **{interaction.guild.name}** verwarnt (Warnung #{warn_total}):\n> {grund}"
            )
        except Exception:
            pass

        esc_note = ""
        rule = esc_map.get(str(warn_total))
        if isinstance(rule, dict):
            act = str(rule.get("action") or "")
            dur = int(rule.get("duration_sec") or 3600)
            try:
                if act == "timeout":
                    until = discord.utils.utcnow() + datetime.timedelta(seconds=dur)
                    await member.timeout(until, reason=f"Auto-Eskalation ({warn_total} Verwarnungen)")
                    esc_note = f"\n⚡ **Auto-Strafe ausgelöst:** Timeout für `{dur // 60} Min.`!"
                elif act == "kick":
                    await member.kick(reason=f"Auto-Eskalation ({warn_total} Verwarnungen)")
                    esc_note = "\n⚡ **Auto-Strafe ausgelöst:** Vom Server gekickt!"
                elif act == "ban":
                    await member.ban(reason=f"Auto-Eskalation ({warn_total} Verwarnungen)")
                    esc_note = "\n⚡ **Auto-Strafe ausgelöst:** Permanent gebannt!"
            except Exception:
                pass

        await interaction.response.send_message(
            f"⚠️ {member.mention} wurde verwarnt (**Case #{case_id}** • Aktive Verwarnungen: **`{warn_total}`**).\n"
            f"📝 **Grund:** {grund}{esc_note}"
        )

    @app_commands.command(name="warnings", description="📋 Zeigt alle Verwarnungen eines Users")
    async def cmd_warnings(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        g = moderation_store.get_guild(interaction.guild.id)
        wlist = (g.get("warnings") or {}).get(str(member.id)) or []
        if not wlist:
            await interaction.response.send_message(
                f"✅ {member.mention} hat aktuell **0** Verwarnungen.", ephemeral=True
            )
            return
        lines = [
            f"• **Warn #{w['id']}** (Case #{w.get('case_id', '?')}) — <@{w.get('mod_id')}>: *{w.get('reason')}* (`{str(w.get('created_at', ''))[:10]}`)"
            for w in wlist
        ]
        embed = discord.Embed(
            title=f"⚠️ Verwarnungen – {member.display_name} ({len(wlist)})",
            description="\n".join(lines[:20]),
            color=COLOR_WARNING,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="delwarn", description="🗑️ Löscht eine einzelne Verwarnung (Mods/Admins)")
    async def cmd_delwarn(
        self, interaction: discord.Interaction, member: discord.Member, warn_id: int
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        def _rem(g: dict[str, Any]) -> bool:
            wlist = (g.get("warnings") or {}).get(str(member.id)) or []
            before = len(wlist)
            wlist = [w for w in wlist if int(w.get("id") or 0) != warn_id]
            g.setdefault("warnings", {})[str(member.id)] = wlist
            return len(wlist) < before

        ok = moderation_store.mutate(interaction.guild.id, _rem)
        await interaction.response.send_message(
            f"🗑️ Verwarnung **#{warn_id}** von {member.mention} gelöscht."
            if ok
            else "❌ Warn-ID nicht gefunden.",
            ephemeral=True,
        )

    @app_commands.command(name="clearwarns", description="🧹 Löscht alle Verwarnungen eines Users (Admins)")
    async def cmd_clearwarns(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        moderation_store.mutate(interaction.guild.id, lambda g: g.get("warnings", {}).pop(str(member.id), None))
        await interaction.response.send_message(
            f"🧹 Alle Verwarnungen von {member.mention} wurden zurückgesetzt.", ephemeral=True
        )

    @app_commands.command(
        name="escalation-set",
        description="⚙️ Konfiguriert automatische Strafen ab X Verwarnungen (Admins)",
    )
    @app_commands.choices(
        aktion=[
            app_commands.Choice(name="⏳ Timeout", value="timeout"),
            app_commands.Choice(name="👢 Kick", value="kick"),
            app_commands.Choice(name="🔨 Permanenter Ban", value="ban"),
            app_commands.Choice(name="🗑️ Regel entfernen", value="remove"),
        ]
    )
    @app_commands.describe(
        verwarnungen="Anzahl Verwarnungen (z. B. 3)",
        aktion="Automatische Strafe",
        dauer="Dauer bei Timeout (z. B. 1h, 12h, 3d)",
    )
    async def cmd_escalation_set(
        self,
        interaction: discord.Interaction,
        verwarnungen: app_commands.Range[int, 1, 25],
        aktion: str,
        dauer: str = "1h",
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        if aktion == "remove":
            moderation_store.mutate(
                interaction.guild.id, lambda g: g["warn_escalation"].pop(str(verwarnungen), None)
            )
            await interaction.response.send_message(
                f"🗑️ Eskalations-Regel für **{verwarnungen}** Verwarnungen entfernt.", ephemeral=True
            )
            return
        secs = parse_time(dauer) or 3600
        moderation_store.mutate(
            interaction.guild.id,
            lambda g: g["warn_escalation"].__setitem__(
                str(verwarnungen), {"action": aktion, "duration_sec": secs}
            ),
        )
        await interaction.response.send_message(
            f"✅ Ab **{verwarnungen}** Verwarnungen greift automatisch: **`{aktion.upper()}`**"
            + (f" (`{dauer}`)" if aktion == "timeout" else ""),
            ephemeral=True,
        )

    @app_commands.command(name="timeout", description="⏳ Versetzt einen User in den Timeout / Mute (Mods)")
    @app_commands.describe(member="Ziel-User", dauer="Dauer (z. B. 10m, 2h, 3d, max. 28d)", grund="Grund")
    async def cmd_timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        dauer: str,
        grund: str = "Kein Grund angegeben",
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if err := self._can_act_on(interaction.user, member):
            await interaction.response.send_message(err, ephemeral=True)
            return
        secs = parse_time(dauer)
        if not secs or secs < 10 or secs > 28 * 86400:
            await interaction.response.send_message(
                "❌ Ungültige Dauer! Beispiele: `10m`, `2h`, `1d`, `7d` (max. 28 Tage).", ephemeral=True
            )
            return
        try:
            await member.timeout(
                discord.utils.utcnow() + datetime.timedelta(seconds=secs),
                reason=f"{interaction.user}: {grund}",
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ Mir fehlen die Timeout-Rechte für diesen User.", ephemeral=True)
            return
        cid = await log_mod_case(interaction.guild, "TIMEOUT", member, interaction.user, grund, dauer)
        await interaction.response.send_message(
            f"⏳ {member.mention} erhielt einen Timeout für **`{dauer}`** (**Case #{cid}**).\n📝 **Grund:** {grund}"
        )

    @app_commands.command(name="untimeout", description="🔊 Hebt den Timeout eines Users sofort auf (Mods)")
    async def cmd_untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        grund: str = "Timeout aufgehoben",
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        try:
            await member.timeout(None, reason=f"{interaction.user}: {grund}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        cid = await log_mod_case(interaction.guild, "UNTIMEOUT", member, interaction.user, grund)
        await interaction.response.send_message(
            f"🔊 Timeout von {member.mention} aufgehoben (**Case #{cid}**)."
        )

    @app_commands.command(name="kick", description="👢 Kickt ein Mitglied vom Server (Mods)")
    async def cmd_kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        grund: str = "Kein Grund angegeben",
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if err := self._can_act_on(interaction.user, member):
            await interaction.response.send_message(err, ephemeral=True)
            return
        try:
            await member.kick(reason=f"{interaction.user}: {grund}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Mir fehlen Kick-Rechte für diesen User.", ephemeral=True)
            return
        cid = await log_mod_case(interaction.guild, "KICK", member, interaction.user, grund)
        await interaction.response.send_message(
            f"👢 **{member}** wurde vom Server gekickt (**Case #{cid}**).\n📝 **Grund:** {grund}"
        )

    @app_commands.command(name="ban", description="🔨 Bannt einen User permanent vom Server (Mods/Admins)")
    async def cmd_ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        grund: str = "Kein Grund angegeben",
        nachrichten_loeschen_tage: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        mem = interaction.guild.get_member(user.id)
        if mem and (err := self._can_act_on(interaction.user, mem)):
            await interaction.response.send_message(err, ephemeral=True)
            return
        try:
            await interaction.guild.ban(
                user,
                reason=f"{interaction.user}: {grund}",
                delete_message_days=nachrichten_loeschen_tage,
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ Mir fehlen Ban-Rechte.", ephemeral=True)
            return
        cid = await log_mod_case(interaction.guild, "BAN", user, interaction.user, grund)
        await interaction.response.send_message(
            f"🔨 **{user}** wurde gebannt (**Case #{cid}**).\n📝 **Grund:** {grund}"
        )

    @app_commands.command(
        name="tempban",
        description="⏱️🔨 Bannt einen User zeitlich begrenzt mit automatischem Unban (Mods/Admins)",
    )
    @app_commands.describe(user="Ziel-User", dauer="z. B. 12h, 3d, 2w", grund="Grund")
    async def cmd_tempban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        dauer: str,
        grund: str = "Kein Grund angegeben",
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        secs = parse_time(dauer)
        if not secs or secs < 60:
            await interaction.response.send_message(
                "❌ Ungültige Dauer (z. B. `1h`, `3d`, `2w`).", ephemeral=True
            )
            return
        mem = interaction.guild.get_member(user.id)
        if mem and (err := self._can_act_on(interaction.user, mem)):
            await interaction.response.send_message(err, ephemeral=True)
            return
        try:
            await interaction.guild.ban(user, reason=f"Tempban ({dauer}) von {interaction.user}: {grund}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Mir fehlen Ban-Rechte.", ephemeral=True)
            return

        expires_at = time.time() + secs
        moderation_store.mutate(
            interaction.guild.id,
            lambda g: g.setdefault("tempbans", {}).__setitem__(
                str(user.id), {"expires_at": expires_at, "reason": grund}
            ),
        )
        cid = await log_mod_case(interaction.guild, "TEMPBAN", user, interaction.user, grund, dauer)
        await interaction.response.send_message(
            f"⏱️🔨 **{user}** wurde für **`{dauer}`** gebannt (**Case #{cid}**, Entbannung: <t:{int(expires_at)}:R>)."
        )

    @app_commands.command(name="unban", description="🔓 Entbannt einen User per User-ID (Mods/Admins)")
    async def cmd_unban(
        self, interaction: discord.Interaction, user_id: str, grund: str = "Manuell entbannt"
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if not user_id.strip().isdigit():
            await interaction.response.send_message("❌ Bitte eine gültige numerische User-ID angeben.", ephemeral=True)
            return
        uid = int(user_id.strip())
        try:
            user_obj = await self.cog.bot.fetch_user(uid)
            await interaction.guild.unban(user_obj, reason=f"{interaction.user}: {grund}")
        except Exception as e:
            await interaction.response.send_message(f"❌ Entbannen fehlgeschlagen: `{e}`", ephemeral=True)
            return
        moderation_store.mutate(
            interaction.guild.id, lambda g: g.get("tempbans", {}).pop(str(uid), None)
        )
        cid = await log_mod_case(interaction.guild, "UNBAN", user_obj, interaction.user, grund)
        await interaction.response.send_message(f"🔓 **{user_obj}** wurde entbannt (**Case #{cid}**).")

    @app_commands.command(
        name="purge",
        description="🧹 Löscht bis zu 200 Nachrichten mit optionalen Filtern (Carl-bot/ProBot Purge)",
    )
    @app_commands.describe(
        anzahl="Anzahl zu prüfender Nachrichten (1–200)",
        von_user="Nur Nachrichten dieses Users löschen",
        nur_bots="Nur Bot-Nachrichten löschen",
        enthaelt="Nur Nachrichten löschen, die diesen Text enthalten",
        nur_links="Nur Nachrichten mit Links löschen",
    )
    async def cmd_purge(
        self,
        interaction: discord.Interaction,
        anzahl: app_commands.Range[int, 1, 200],
        von_user: discord.User | None = None,
        nur_bots: bool = False,
        enthaelt: str | None = None,
        nur_links: bool = False,
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("❌ Nur in Textkanälen verfügbar.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        def _check(m: discord.Message) -> bool:
            if m.pinned:
                return False
            if von_user and m.author.id != von_user.id:
                return False
            if nur_bots and not m.author.bot:
                return False
            if enthaelt and enthaelt.lower() not in (m.content or "").lower():
                return False
            if nur_links and "http://" not in (m.content or "") and "https://" not in (m.content or ""):
                return False
            return True

        try:
            deleted = await interaction.channel.purge(limit=anzahl, check=_check)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler beim Löschen: `{e}`", ephemeral=True)
            return
        await log_mod_case(
            interaction.guild,
            "PURGE",
            interaction.user,
            interaction.user,
            f"{len(deleted)} Nachrichten in #{interaction.channel.name} gelöscht",
        )
        await interaction.followup.send(
            f"🧹 **{len(deleted)}** Nachrichten in {interaction.channel.mention} gelöscht.",
            ephemeral=True,
        )

    @app_commands.command(name="lock", description="🔒 Sperrt einen Textkanal für @everyone (Mods)")
    async def cmd_lock(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel | None = None,
        grund: str = "Kanal vorübergehend gesperrt",
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        ch = kanal or interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow, reason=grund)
        await ch.send(embed=discord.Embed(description=f"🔒 **Kanal gesperrt:** {grund}", color=COLOR_ERROR))
        await interaction.response.send_message(f"🔒 {ch.mention} gesperrt.", ephemeral=True)

    @app_commands.command(name="unlock", description="🔓 Entsperrt einen Textkanal wieder (Mods)")
    async def cmd_unlock(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        ch = kanal or interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.send_messages = None
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow, reason="Kanal entsperrt")
        await ch.send(embed=discord.Embed(description="🔓 **Kanal wurde wieder freigegeben!**", color=COLOR_SUCCESS))
        await interaction.response.send_message(f"🔓 {ch.mention} entsperrt.", ephemeral=True)

    @app_commands.command(name="slowmode", description="🐢 Setzt den Slowmode eines Kanals in Sekunden (0 = aus)")
    async def cmd_slowmode(
        self,
        interaction: discord.Interaction,
        sekunden: app_commands.Range[int, 0, 21600],
        kanal: discord.TextChannel | None = None,
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        ch = kanal or interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return
        await ch.edit(slowmode_delay=sekunden)
        msg = f"🐢 Slowmode in {ch.mention} auf **`{sekunden}s`** gesetzt." if sekunden else f"✅ Slowmode in {ch.mention} deaktiviert."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="nick", description="🏷️ Ändert oder entfernt den Server-Nickname eines Users (Mods)")
    async def cmd_nick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        neuer_name: str | None = None,
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        try:
            await member.edit(nick=neuer_name or None, reason=f"Geändert von {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Mir fehlen die Rechte zum Ändern dieses Nicknames.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🏷️ Nickname von {member.mention} {'auf **' + neuer_name + '** gesetzt' if neuer_name else 'zurückgesetzt'}.",
            ephemeral=True,
        )

    @app_commands.command(name="case", description="🔎 Zeigt Details zu einem Moderations-Case (oder ändert den Grund)")
    async def cmd_case(
        self,
        interaction: discord.Interaction,
        case_id: int,
        neuer_grund: str | None = None,
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if neuer_grund:
            moderation_store.mutate(
                interaction.guild.id,
                lambda g: g.get("cases", {}).get(str(case_id), {}).__setitem__("reason", neuer_grund),
            )
        g = moderation_store.get_guild(interaction.guild.id)
        c = (g.get("cases") or {}).get(str(case_id))
        if not c:
            await interaction.response.send_message(f"❌ Case `#{case_id}` nicht gefunden.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"🗂️ Case #{c['case_id']} • {c['action']}",
            color=COLOR_INFO,
        )
        embed.add_field(name="👤 Ziel", value=f"<@{c['target_id']}> (`{c['target_tag']}`)", inline=True)
        embed.add_field(name="👮 Moderator", value=f"<@{c['mod_id']}>", inline=True)
        if c.get("duration"):
            embed.add_field(name="⏱️ Dauer", value=f"`{c['duration']}`", inline=True)
        embed.add_field(name="📝 Grund", value=str(c.get("reason") or "-"), inline=False)
        embed.set_footer(text=f"Erstellt: {str(c.get('created_at', ''))[:19].replace('T', ' ')} UTC")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="history", description="📜 Zeigt die komplette Moderations-Akte eines Users (Mods)")
    async def cmd_history(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        g = moderation_store.get_guild(interaction.guild.id)
        cases = [
            c
            for c in (g.get("cases") or {}).values()
            if isinstance(c, dict) and int(c.get("target_id") or 0) == user.id
        ]
        cases.sort(key=lambda x: int(x.get("case_id") or 0), reverse=True)
        if not cases:
            await interaction.response.send_message(
                f"✅ Keine Moderations-Einträge für **{user}** vorhanden.", ephemeral=True
            )
            return
        lines = [
            f"• **`#{c['case_id']}` {c['action']}** (`{str(c.get('created_at', ''))[:10]}`) — {str(c.get('reason', ''))[:80]}"
            for c in cases[:15]
        ]
        embed = discord.Embed(
            title=f"📜 Moderations-Akte – {user} ({len(cases)} Einträge)",
            description="\n".join(lines),
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="embed-send", description="🎨 Öffnet ein Formular zum Senden eines Custom-Embeds (Admins)")
    async def cmd_embed_send(
        self, interaction: discord.Interaction, kanal: discord.TextChannel | None = None
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        target_ch = kanal or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Bitte einen Textkanal wählen.", ephemeral=True)
            return
        await interaction.response.send_modal(EmbedBuilderModal(target_ch))

    @app_commands.command(name="embed-edit", description="✏️ Bearbeitet ein bestehendes Bot-Embed per Nachrichten-ID (Admins)")
    async def cmd_embed_edit(
        self,
        interaction: discord.Interaction,
        nachrichten_id: str,
        kanal: discord.TextChannel | None = None,
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        ch = kanal or interaction.channel
        if not isinstance(ch, discord.TextChannel) or not nachrichten_id.strip().isdigit():
            await interaction.response.send_message("❌ Ungültiger Kanal oder Nachrichten-ID.", ephemeral=True)
            return
        try:
            msg = await ch.fetch_message(int(nachrichten_id.strip()))
        except Exception:
            await interaction.response.send_message("❌ Nachricht nicht gefunden.", ephemeral=True)
            return
        await interaction.response.send_modal(EmbedBuilderModal(ch, edit_message=msg))


class ModerationCog(commands.Cog, name="Moderation"):
    """Moderations-Suite inkl. automatischer Tempban-Ablaufkontrolle."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.mod_group = ModGroup(self)
        self.bot.tree.add_command(self.mod_group)

    async def cog_load(self) -> None:
        self.tempban_watcher.start()

    async def cog_unload(self) -> None:
        self.tempban_watcher.cancel()
        self.bot.tree.remove_command(self.mod_group.name, type=self.mod_group.type)

    @tasks.loop(minutes=1)
    async def tempban_watcher(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            g = moderation_store.get_guild(guild.id)
            tb_map: dict[str, Any] = g.get("tempbans") or {}
            expired_uids = [
                int(uid)
                for uid, rec in tb_map.items()
                if uid.isdigit() and isinstance(rec, dict) and float(rec.get("expires_at") or 0) <= now
            ]
            for uid in expired_uids:
                try:
                    u_obj = discord.Object(id=uid)
                    await guild.unban(u_obj, reason="⏱️ Tempban automatisch abgelaufen")
                except discord.NotFound:
                    pass  # Already unbanned; no retry needed.
                except discord.HTTPException as exc:
                    print(f"⚠️ [Moderation] Tempban {uid} konnte nicht aufgehoben werden: {exc}")
                    continue
                # Do not discard a newly scheduled ban while this request was in flight.
                def _remove_expired(gd: dict[str, Any]) -> None:
                    current = gd.get("tempbans", {}).get(str(uid))
                    if current == tb_map.get(str(uid)):
                        gd["tempbans"].pop(str(uid), None)
                moderation_store.mutate(guild.id, _remove_expired)
                if self.bot.user:
                    try:
                        target = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                        await log_mod_case(guild, "UNBAN", target, self.bot.user, "Tempban automatisch abgelaufen")
                    except discord.HTTPException:
                        pass

    @tempban_watcher.before_loop
    async def _before_tb(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
