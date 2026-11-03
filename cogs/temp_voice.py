"""Temp-Voice Cog: Dynamische Sprach-/Textkanäle mit UserSelect-Menüs & Owner-Recovery."""
from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any
import discord
from discord.ext import commands

from bot_core.config import (
    ADMIN_ROLE_NAME,
    COLOR_VOICE,
    OWNER_RECOVERY_WINDOW,
    TEMP_VOICE_CATEGORY_ID,
    TEMP_VOICE_CHANNEL_ID,
)
from bot_core.stats import stats_tracker

temp_voice_channels: dict[int, dict[str, Any]] = {}
_owner_recovery: dict[int, dict[str, Any]] = {}


def _is_only_audio_state_change(before: discord.VoiceState, after: discord.VoiceState) -> bool:
    if before.channel is None or after.channel is None:
        return False
    if before.channel.id != after.channel.id:
        return False
    return (
        before.self_mute != after.self_mute
        or before.self_deaf != after.self_deaf
        or before.mute != after.mute
        or before.deaf != after.deaf
        or before.self_stream != after.self_stream
        or before.self_video != after.self_video
    )


def _record_owner_left(channel_id: int, owner_id: int) -> None:
    _owner_recovery[channel_id] = {
        "original_owner_id": owner_id,
        "left_at": datetime.datetime.now(datetime.timezone.utc),
    }


def _check_owner_recovery(channel_id: int, member_id: int) -> bool:
    rec = _owner_recovery.get(channel_id)
    if not rec or rec["original_owner_id"] != member_id:
        return False
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - rec["left_at"]).total_seconds()
    if elapsed <= OWNER_RECOVERY_WINDOW:
        return True
    _owner_recovery.pop(channel_id, None)
    return False


def _clear_owner_recovery(channel_id: int) -> None:
    _owner_recovery.pop(channel_id, None)


def _slugify_channel_name(raw: str) -> str:
    safe = re.sub(
        r"[^a-z0-9\-]",
        "-",
        raw.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace(" ", "-"),
    )
    return re.sub(r"-+", "-", safe).strip("-") or "kanal"


def build_vc_control_embed(guild: discord.Guild, voice_channel_id: int) -> discord.Embed:
    vc_data = temp_voice_channels.get(voice_channel_id, {})
    voice_channel = guild.get_channel(voice_channel_id)

    owner_id = vc_data.get("owner_id", 0)
    owner = guild.get_member(owner_id)
    owner_str = owner.mention if owner else f"<@{owner_id}>"
    locked = vc_data.get("locked", False)
    allowed_users = vc_data.get("allowed_users", set())

    current_limit = voice_channel.user_limit if voice_channel else vc_data.get("limit", 0)
    limit_str = str(current_limit) if current_limit > 0 else "∞"
    member_count = len(voice_channel.members) if voice_channel else 0
    bitrate_str = f"`{voice_channel.bitrate // 1000} kbps`" if voice_channel else "Unbekannt"
    channel_name = voice_channel.name if voice_channel else "Unbekannt"

    if voice_channel and voice_channel.members:
        member_list = "\n".join(
            f"{'👑 ' if m.id == owner_id else '👤 '}{m.display_name}"
            for m in voice_channel.members
        )
    else:
        member_list = "*Niemand im Kanal*"

    embed = discord.Embed(
        title="🎙️ Kanal-Einstellungen",
        description=(
            "Hier kannst du deinen Voice-Kanal verwalten.\n"
            "Nur der **Kanal-Admin** kann die Buttons benutzen."
        ),
        color=COLOR_VOICE,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="📛 Kanalname", value=f"`{channel_name}`", inline=False)
    embed.add_field(name="👑 Admin", value=owner_str, inline=True)
    embed.add_field(name="🔒 Status", value="Gesperrt 🔒" if locked else "Offen 🔓", inline=True)
    embed.add_field(name="👥 Nutzer", value=f"`{member_count}` / `{limit_str}`", inline=True)
    embed.add_field(name="🎙️ Bitrate", value=bitrate_str, inline=True)
    embed.add_field(
        name="📋 Whitelist",
        value=f"`{len(allowed_users)}` User" if allowed_users else "Nicht aktiv",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="🧑‍🤝‍🧑 Im Kanal", value=member_list, inline=False)
    embed.set_footer(text="Temp Voice System • Wird gelöscht wenn alle raus sind")
    return embed


async def update_temp_voice_panel(guild: discord.Guild, voice_channel_id: int) -> None:
    vc_data = temp_voice_channels.get(voice_channel_id)
    if not vc_data:
        return
    text_channel = guild.get_channel(vc_data.get("text_channel_id", 0))
    control_message_id = vc_data.get("control_message_id", 0)
    if not text_channel or not control_message_id:
        return
    try:
        msg = await text_channel.fetch_message(control_message_id)
        await msg.edit(
            embed=build_vc_control_embed(guild, voice_channel_id),
            view=VoiceChannelControlView(vc_data["owner_id"], voice_channel_id),
        )
    except discord.NotFound:
        pass
    except Exception as e:
        print(f"⚠️ [TempVC] Panel-Update Fehler: {e}")


# ── Modals & Native User-Select Views ──────────────────────────────────────

class RenameChannelModal(discord.ui.Modal, title="Kanal umbenennen"):
    new_name = discord.ui.TextInput(
        label="Neuer Kanalname",
        placeholder="z.B. Gaming Lounge",
        min_length=1,
        max_length=100,
    )

    def __init__(self, voice_channel_id: int) -> None:
        super().__init__()
        self.voice_channel_id = voice_channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        vc = interaction.guild.get_channel(self.voice_channel_id)
        if not isinstance(vc, discord.VoiceChannel):
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        try:
            await vc.edit(name=self.new_name.value)
            vc_data = temp_voice_channels.get(self.voice_channel_id)
            if vc_data:
                tc = interaction.guild.get_channel(vc_data.get("text_channel_id", 0))
                if isinstance(tc, discord.TextChannel):
                    await tc.edit(name=f"💬{_slugify_channel_name(self.new_name.value)}")
            await interaction.response.send_message(
                f"✅ Kanal wurde in **{self.new_name.value}** umbenannt!", ephemeral=True
            )
            await update_temp_voice_panel(interaction.guild, self.voice_channel_id)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class SetLimitModal(discord.ui.Modal, title="Nutzer-Limit setzen"):
    limit = discord.ui.TextInput(
        label="Maximale Nutzerzahl (0 = kein Limit)",
        placeholder="z.B. 5",
        min_length=1,
        max_length=2,
    )

    def __init__(self, voice_channel_id: int) -> None:
        super().__init__()
        self.voice_channel_id = voice_channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        try:
            val = int(self.limit.value)
            if not (0 <= val <= 99):
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "❌ Bitte eine Zahl zwischen 0 und 99 eingeben.", ephemeral=True
            )
            return
        vc = interaction.guild.get_channel(self.voice_channel_id)
        if not isinstance(vc, discord.VoiceChannel):
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        try:
            await vc.edit(user_limit=val)
            if self.voice_channel_id in temp_voice_channels:
                temp_voice_channels[self.voice_channel_id]["limit"] = val
            await interaction.response.send_message(
                f"✅ Nutzer-Limit auf **{val if val > 0 else 'kein Limit'}** gesetzt!",
                ephemeral=True,
            )
            await update_temp_voice_panel(interaction.guild, self.voice_channel_id)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class SetBitrateModal(discord.ui.Modal, title="Bitrate ändern"):
    bitrate = discord.ui.TextInput(
        label="Bitrate in kbps (8 - 96)",
        placeholder="z.B. 64",
        min_length=1,
        max_length=3,
    )

    def __init__(self, voice_channel_id: int) -> None:
        super().__init__()
        self.voice_channel_id = voice_channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        max_kbps = int(interaction.guild.bitrate_limit // 1000) if interaction.guild else 96
        try:
            br = int(self.bitrate.value)
            if not (8 <= br <= max_kbps):
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                f"❌ Bitte eine Zahl zwischen 8 und {max_kbps} kbps eingeben.", ephemeral=True
            )
            return
        vc = interaction.guild.get_channel(self.voice_channel_id)
        if not isinstance(vc, discord.VoiceChannel):
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        try:
            await vc.edit(bitrate=br * 1000)
            await interaction.response.send_message(
                f"✅ Bitrate auf **{br} kbps** gesetzt!", ephemeral=True
            )
            await update_temp_voice_panel(interaction.guild, self.voice_channel_id)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class InviteUserSelectView(discord.ui.View):
    def __init__(self, voice_channel_id: int) -> None:
        super().__init__(timeout=45)
        self.voice_channel_id = voice_channel_id

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="User zum Einladen auswählen...",
        min_values=1,
        max_values=1,
    )
    async def select_user(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ) -> None:
        if not interaction.guild:
            return
        member = interaction.guild.get_member(select.values[0].id)
        vc = interaction.guild.get_channel(self.voice_channel_id)
        if not member or not isinstance(vc, discord.VoiceChannel):
            await interaction.response.edit_message(content="❌ User oder Kanal nicht gefunden.", view=None)
            return
        try:
            await vc.set_permissions(member, connect=True, view_channel=True)
            vc_data = temp_voice_channels.get(self.voice_channel_id)
            if vc_data:
                vc_data["allowed_users"].add(member.id)
                tc = interaction.guild.get_channel(vc_data.get("text_channel_id", 0))
                if isinstance(tc, discord.TextChannel):
                    await tc.set_permissions(member, read_messages=True, send_messages=True)
            await interaction.response.edit_message(
                content=f"✅ **{member.display_name}** wurde eingeladen!", view=None
            )
            await update_temp_voice_panel(interaction.guild, self.voice_channel_id)
            try:
                await member.send(
                    f"🎙️ **{interaction.user.display_name}** hat dich in den Voice-Kanal **{vc.name}** eingeladen!"
                )
            except Exception:
                pass
        except Exception as e:
            await interaction.response.edit_message(content=f"❌ Fehler: {e}", view=None)


class TransferOwnerSelectView(discord.ui.View):
    def __init__(self, voice_channel_id: int, members: list[discord.Member]) -> None:
        super().__init__(timeout=45)
        self.voice_channel_id = voice_channel_id
        options = [
            discord.SelectOption(label=m.display_name, description=f"@{m.name}", value=str(m.id))
            for m in members[:25]
        ]
        self.picker = discord.ui.Select(placeholder="Neuen Kanal-Admin wählen...", options=options)
        self.picker.callback = self._on_pick
        self.add_item(self.picker)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        new_owner = interaction.guild.get_member(int(self.picker.values[0]))
        vc = interaction.guild.get_channel(self.voice_channel_id)
        vc_data = temp_voice_channels.get(self.voice_channel_id)
        if not new_owner or not isinstance(vc, discord.VoiceChannel) or not vc_data:
            await interaction.response.edit_message(content="❌ User oder Kanal nicht gefunden.", view=None)
            return

        old_owner = interaction.guild.get_member(vc_data["owner_id"])
        try:
            await vc.set_permissions(
                new_owner,
                manage_channels=True, connect=True, speak=True,
                move_members=True, mute_members=True, deafen_members=True,
            )
            if old_owner and old_owner.id != new_owner.id:
                await vc.set_permissions(
                    old_owner,
                    manage_channels=False, move_members=False,
                    mute_members=False, deafen_members=False,
                    connect=True, speak=True,
                )
            tc = interaction.guild.get_channel(vc_data.get("text_channel_id", 0))
            if isinstance(tc, discord.TextChannel):
                await tc.set_permissions(new_owner, read_messages=True, send_messages=True, manage_messages=True)
                if old_owner and old_owner.id != new_owner.id:
                    await tc.set_permissions(old_owner, read_messages=True, send_messages=True, manage_messages=False)

            vc_data["owner_id"] = new_owner.id
            await interaction.response.edit_message(
                content=f"👑 Admin-Rechte wurden an **{new_owner.display_name}** übertragen!", view=None
            )
            await update_temp_voice_panel(interaction.guild, self.voice_channel_id)
        except Exception as e:
            await interaction.response.edit_message(content=f"❌ Fehler: {e}", view=None)


class KickUserSelectView(discord.ui.View):
    def __init__(self, voice_channel_id: int, kickable: list[discord.Member]) -> None:
        super().__init__(timeout=30)
        self.voice_channel_id = voice_channel_id
        options = [
            discord.SelectOption(label=m.display_name, description=f"@{m.name}", value=str(m.id))
            for m in kickable[:25]
        ]
        self.select = discord.ui.Select(placeholder="User zum Kicken auswählen...", options=options)
        self.select.callback = self._on_kick
        self.add_item(self.select)

    async def _on_kick(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        member = interaction.guild.get_member(int(self.select.values[0]))
        vc = interaction.guild.get_channel(self.voice_channel_id)
        if not member or not isinstance(vc, discord.VoiceChannel) or member not in vc.members:
            await interaction.response.edit_message(content="❌ User nicht mehr im Kanal.", view=None)
            return
        try:
            await member.move_to(None, reason="Temp VC: Vom Kanal-Admin gekickt")
            await vc.set_permissions(member, connect=False)
            await interaction.response.edit_message(
                content=f"👢 **{member.display_name}** wurde aus dem Kanal gekickt!", view=None
            )
            await update_temp_voice_panel(interaction.guild, self.voice_channel_id)
        except Exception as e:
            await interaction.response.edit_message(content=f"❌ Fehler: {e}", view=None)


class VoiceChannelControlView(discord.ui.View):
    def __init__(self, owner_id: int, voice_channel_id: int) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.voice_channel_id = voice_channel_id

        vc_data = temp_voice_channels.get(voice_channel_id)
        if vc_data and vc_data.get("locked"):
            self.btn_lock.label = "🔓 Entsperren"
            self.btn_lock.style = discord.ButtonStyle.success
        else:
            self.btn_lock.label = "🔒 Sperren"
            self.btn_lock.style = discord.ButtonStyle.danger

    def _resolve_vc_id(self, interaction: discord.Interaction) -> int:
        if self.voice_channel_id:
            return self.voice_channel_id
        for vid, data in temp_voice_channels.items():
            if data.get("text_channel_id") == interaction.channel_id:
                return vid
        return 0

    async def _owner_check(self, interaction: discord.Interaction) -> int:
        vid = self._resolve_vc_id(interaction)
        vc_data = temp_voice_channels.get(vid)
        if not vid or not vc_data:
            await interaction.response.send_message(
                "❌ Dieser temporäre Kanal existiert nicht mehr.", ephemeral=True
            )
            return 0
        actual_owner = vc_data.get("owner_id", self.owner_id)
        if interaction.user.id != actual_owner:
            await interaction.response.send_message(
                "❌ Nur der **Kanal-Admin** kann diese Einstellung ändern!", ephemeral=True
            )
            return 0
        return vid

    @discord.ui.button(label="✏️ Umbenennen", style=discord.ButtonStyle.primary, row=0, custom_id="vc_rename_btn")
    async def btn_rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vid = await self._owner_check(interaction)
        if vid:
            await interaction.response.send_modal(RenameChannelModal(vid))

    @discord.ui.button(label="👥 Limit", style=discord.ButtonStyle.primary, row=0, custom_id="vc_limit_btn")
    async def btn_limit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vid = await self._owner_check(interaction)
        if vid:
            await interaction.response.send_modal(SetLimitModal(vid))

    @discord.ui.button(label="🎙️ Bitrate", style=discord.ButtonStyle.primary, row=0, custom_id="vc_bitrate_btn")
    async def btn_bitrate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vid = await self._owner_check(interaction)
        if vid:
            await interaction.response.send_modal(SetBitrateModal(vid))

    @discord.ui.button(label="🔒 Sperren", style=discord.ButtonStyle.danger, row=1, custom_id="vc_lock_btn")
    async def btn_lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vid = await self._owner_check(interaction)
        if not vid or not interaction.guild:
            return
        vc_data = temp_voice_channels.get(vid)
        vc = interaction.guild.get_channel(vid)
        if not vc_data or not isinstance(vc, discord.VoiceChannel):
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        try:
            if vc_data["locked"]:
                await vc.set_permissions(interaction.guild.default_role, connect=None)
                vc_data["locked"] = False
                msg = "🔓 Kanal wurde **entsperrt**."
            else:
                await vc.set_permissions(interaction.guild.default_role, connect=False)
                vc_data["locked"] = True
                msg = "🔒 Kanal wurde **gesperrt**."
            await interaction.response.send_message(msg, ephemeral=True)
            await update_temp_voice_panel(interaction.guild, vid)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)

    @discord.ui.button(label="➕ Einladen", style=discord.ButtonStyle.success, row=1, custom_id="vc_invite_btn")
    async def btn_invite(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vid = await self._owner_check(interaction)
        if vid:
            await interaction.response.send_message(
                "➕ Wähle den User aus, den du einladen möchtest:",
                view=InviteUserSelectView(vid),
                ephemeral=True,
            )

    @discord.ui.button(label="👢 Kicken", style=discord.ButtonStyle.danger, row=1, custom_id="vc_kick_btn")
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vid = await self._owner_check(interaction)
        if not vid or not interaction.guild:
            return
        vc = interaction.guild.get_channel(vid)
        vc_data = temp_voice_channels.get(vid)
        if not isinstance(vc, discord.VoiceChannel) or not vc_data:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        kickable = [m for m in vc.members if m.id != vc_data["owner_id"] and not m.bot]
        if not kickable:
            await interaction.response.send_message(
                "❌ Es ist niemand im Kanal, den du kicken könntest.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "👢 Wähle den User zum Kicken:",
            view=KickUserSelectView(vid, kickable),
            ephemeral=True,
        )

    @discord.ui.button(label="👑 Admin übertragen", style=discord.ButtonStyle.secondary, row=2, custom_id="vc_transfer_btn")
    async def btn_transfer(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vid = await self._owner_check(interaction)
        if not vid or not interaction.guild:
            return
        vc = interaction.guild.get_channel(vid)
        vc_data = temp_voice_channels.get(vid)
        if not isinstance(vc, discord.VoiceChannel) or not vc_data:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        candidates = [m for m in vc.members if m.id != vc_data["owner_id"] and not m.bot]
        if not candidates:
            await interaction.response.send_message(
                "❌ Keine anderen Mitglieder im Kanal vorhanden.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "👑 Wähle den neuen Kanal-Admin:",
            view=TransferOwnerSelectView(vid, candidates),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
# TEMP VOICE COG
# ═══════════════════════════════════════════════════════════════════════════

class TempVoiceCog(commands.Cog, name="TempVoice"):
    """Temporäre Voice-Kanäle & globales Voice-Zeit-Tracking."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.temp_voice_channels = temp_voice_channels

    async def cog_load(self) -> None:
        self.bot.add_view(VoiceChannelControlView(0, 0))

    async def _create_temp_vc(self, member: discord.Member, guild: discord.Guild) -> None:
        try:
            category = guild.get_channel(TEMP_VOICE_CATEGORY_ID) if TEMP_VOICE_CATEGORY_ID else None
            if not isinstance(category, discord.CategoryChannel):
                trigger = guild.get_channel(TEMP_VOICE_CHANNEL_ID)
                category = trigger.category if trigger else None

            channel_name = f"🎙️ {member.display_name}'s Kanal"
            overwrites_vc: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(connect=True, view_channel=True),
                member: discord.PermissionOverwrite(
                    connect=True, speak=True, view_channel=True, manage_channels=True,
                    move_members=True, mute_members=True, deafen_members=True, stream=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    connect=True, view_channel=True, manage_channels=True,
                    move_members=True, mute_members=True,
                ),
            }
            admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
            if admin_role:
                overwrites_vc[admin_role] = discord.PermissionOverwrite(
                    connect=True, view_channel=True, move_members=True
                )

            vc = await guild.create_voice_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites_vc,
                reason=f"Temp Voice: {member.display_name}",
            )

            overwrites_tc: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                member: discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, manage_messages=True,
                    embed_links=True, attach_files=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, manage_messages=True, embed_links=True,
                ),
            }
            if admin_role:
                overwrites_tc[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            tc = await guild.create_text_channel(
                name=f"💬{_slugify_channel_name(member.display_name)}s-kanal",
                category=category,
                overwrites=overwrites_tc,
                topic=f"Privater Kanal von {member.display_name} | VC-ID: {vc.id}",
                reason=f"Temp Voice Text-Kanal: {member.display_name}",
            )

            temp_voice_channels[vc.id] = {
                "owner_id": member.id,
                "original_owner_id": member.id,
                "text_channel_id": tc.id,
                "control_message_id": 0,
                "allowed_users": set(),
                "locked": False,
                "limit": 0,
            }

            try:
                await member.move_to(vc)
            except discord.HTTPException:
                pass

            msg = await tc.send(
                content=(
                    f"👋 Willkommen {member.mention}!\n"
                    f"Dies ist dein privater Kanal. Nutze die Buttons unten zur Verwaltung."
                ),
                embed=build_vc_control_embed(guild, vc.id),
                view=VoiceChannelControlView(member.id, vc.id),
            )
            temp_voice_channels[vc.id]["control_message_id"] = msg.id
            try:
                await msg.pin()
            except discord.HTTPException:
                pass
        except Exception as e:
            print(f"❌ [TempVC] Fehler bei Kanalerstellung: {e}")

    async def _delete_temp_vc(self, voice_channel_id: int, guild: discord.Guild) -> None:
        vc_data = temp_voice_channels.pop(voice_channel_id, None)
        if not vc_data:
            return

        vc = guild.get_channel(voice_channel_id)
        if vc:
            try:
                await vc.delete(reason="Temp Voice: Kanal ist leer")
            except Exception:
                pass
        tc = guild.get_channel(vc_data.get("text_channel_id", 0))
        if tc:
            try:
                await tc.delete(reason="Temp Voice: Kanal ist leer")
            except Exception:
                pass

    async def _handle_temp_vc_leave(self, member: discord.Member, channel_id: int) -> None:
        await asyncio.sleep(1)
        guild = member.guild
        try:
            vc = await guild.fetch_channel(channel_id)
        except Exception:
            vc = guild.get_channel(channel_id)

        if vc is None or not isinstance(vc, discord.VoiceChannel):
            temp_voice_channels.pop(channel_id, None)
            return

        human_members = [m for m in vc.members if not m.bot]
        if len(vc.members) == 0 or len(human_members) == 0:
            _clear_owner_recovery(channel_id)
            await self._delete_temp_vc(channel_id, guild)
            return

        vc_data = temp_voice_channels.get(channel_id)
        if vc_data and member.id == vc_data["owner_id"] and human_members:
            _record_owner_left(channel_id, member.id)
            new_owner = human_members[0]
            try:
                await vc.set_permissions(
                    new_owner,
                    manage_channels=True, connect=True, speak=True,
                    move_members=True, mute_members=True, deafen_members=True,
                )
                await vc.set_permissions(member, overwrite=None)
                tc = guild.get_channel(vc_data.get("text_channel_id", 0))
                if isinstance(tc, discord.TextChannel):
                    await tc.set_permissions(new_owner, read_messages=True, send_messages=True, manage_messages=True)
                    await tc.set_permissions(member, overwrite=None)
                    await tc.send(
                        f"👑 **{new_owner.mention}** ist jetzt Kanal-Admin!\n"
                        f"-# Der vorherige Admin kann innerhalb von 10 Min. zurückkehren, um den Rang zurückzuerhalten."
                    )
                vc_data["owner_id"] = new_owner.id
            except Exception as e:
                print(f"⚠️ [TempVC] Ownership-Transfer Fehler: {e}")

        await update_temp_voice_panel(guild, channel_id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if _is_only_audio_state_change(before, after):
            return

        if not member.bot:
            changed = (
                (before.channel is None and after.channel is not None)
                or (before.channel is not None and after.channel is None)
                or (
                    before.channel is not None
                    and after.channel is not None
                    and before.channel.id != after.channel.id
                )
            )
            if changed:
                try:
                    if before.channel is not None:
                        stats_tracker.voice_leave(member.id, member.display_name)
                    if after.channel is not None:
                        stats_tracker.voice_join(member.id)
                except Exception:
                    pass

        if not TEMP_VOICE_CHANNEL_ID:
            return

        # 1) Zuerst alten Temp-Kanal bereinigen (auch bei direktem Wechsel in anderen Kanal!)
        if (
            before.channel is not None
            and before.channel.id in temp_voice_channels
            and (after.channel is None or after.channel.id != before.channel.id)
        ):
            asyncio.create_task(self._handle_temp_vc_leave(member, before.channel.id))

        # 2) Beitritt in Trigger-Kanal → Neuen Temp-VC erstellen
        if after.channel is not None and after.channel.id == TEMP_VOICE_CHANNEL_ID:
            await self._create_temp_vc(member, member.guild)
            return

        # 3) Beitritt in bestehenden Temp-VC → Owner-Recovery & Textkanal-Zugriff
        if (
            after.channel is not None
            and after.channel.id in temp_voice_channels
            and after.channel.id != TEMP_VOICE_CHANNEL_ID
        ):
            cid = after.channel.id
            vc_data = temp_voice_channels.get(cid)
            if vc_data:
                tc = member.guild.get_channel(vc_data.get("text_channel_id", 0))
                if isinstance(tc, discord.TextChannel) and not member.bot:
                    try:
                        await tc.set_permissions(member, read_messages=True, send_messages=True)
                    except Exception:
                        pass

                if _check_owner_recovery(cid, member.id):
                    _clear_owner_recovery(cid)
                    cur_owner_id = vc_data.get("owner_id")
                    cur_owner = member.guild.get_member(cur_owner_id) if cur_owner_id else None
                    vc = member.guild.get_channel(cid)
                    if isinstance(vc, discord.VoiceChannel):
                        try:
                            await vc.set_permissions(
                                member,
                                manage_channels=True, connect=True, speak=True,
                                move_members=True, mute_members=True, deafen_members=True,
                            )
                            if cur_owner and cur_owner.id != member.id:
                                await vc.set_permissions(
                                    cur_owner,
                                    manage_channels=False, move_members=False,
                                    mute_members=False, deafen_members=False,
                                    connect=True, speak=True,
                                )
                            if isinstance(tc, discord.TextChannel):
                                await tc.set_permissions(member, read_messages=True, send_messages=True, manage_messages=True)
                                if cur_owner and cur_owner.id != member.id:
                                    await tc.set_permissions(cur_owner, read_messages=True, send_messages=True, manage_messages=False)
                                await tc.send(
                                    f"👑 **{member.mention}** ist zurückgekehrt und hat den Kanal-Admin-Rang zurückerhalten!"
                                )
                            vc_data["owner_id"] = member.id
                        except Exception as e:
                            print(f"⚠️ [TempVC] Owner-Recovery Fehler: {e}")

            await update_temp_voice_panel(member.guild, cid)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TempVoiceCog(bot))
