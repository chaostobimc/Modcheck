"""Tickets Cog: Support-Tickets mit Modal, Claim/Unclaim, Transcript & Logging."""
from __future__ import annotations

import asyncio
import datetime
import io
import re
import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import is_admin_user, is_user_admin
from bot_core.config import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_TICKET,
    COLOR_WARNING,
    LOG_CHANNEL_ID,
    TICKET_CATEGORY_ID,
    TICKET_LOG_CHANNEL_ID,
    TICKET_SUPPORT_ROLE,
)
from bot_core.storage import get_next_ticket_number, load_tickets, open_tickets, save_tickets


def _is_support(member: discord.Member) -> bool:
    if is_user_admin(member):
        return True
    role = discord.utils.get(member.guild.roles, name=TICKET_SUPPORT_ROLE)
    if role and role in member.roles:
        return True
    return member.guild_permissions.manage_channels


async def create_ticket_transcript(channel: discord.TextChannel) -> io.BytesIO:
    ticket = open_tickets.get(channel.id, {})
    tid = ticket.get("ticket_id", 0)
    lines = [
        "═" * 64,
        f"MODCHECK TICKET TRANSCRIPT — #{tid:04d} ({channel.name})",
        f"Betreff: {ticket.get('betreff', '–')}",
        f"Erstellt: {ticket.get('created_at', '–')}",
        f"Exportiert: {datetime.datetime.now(datetime.timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')}",
        "═" * 64,
        "",
    ]
    try:
        async for msg in channel.history(limit=500, oldest_first=True):
            ts = msg.created_at.strftime("%d.%m.%Y %H:%M:%S")
            body = msg.content or ""
            if msg.attachments:
                att_str = ", ".join(a.url for a in msg.attachments)
                body = f"{body} [Anhänge: {att_str}]".strip()
            if not body and msg.embeds:
                titles = [e.title for e in msg.embeds if e.title]
                body = f"[Embed: {', '.join(titles)}]" if titles else "[Embed]"
            lines.append(f"[{ts}] {msg.author.display_name} ({msg.author.id}): {body}")
    except Exception as e:
        lines.append(f"[Fehler beim Laden des Verlaufs: {e}]")

    return io.BytesIO("\n".join(lines).encode("utf-8"))


async def send_ticket_log(
    guild: discord.Guild,
    action: str,
    ticket: dict,
    actor: discord.abc.User,
    transcript_bytes: bytes | None = None,
) -> None:
    try:
        log_ch_id = TICKET_LOG_CHANNEL_ID or LOG_CHANNEL_ID
        if not log_ch_id:
            return
        log_channel = guild.get_channel(log_ch_id)
        if not log_channel:
            return

        tid = int(ticket.get("ticket_id") or 0)
        cfg = {
            "open": ("🎫 Ticket geöffnet", COLOR_SUCCESS),
            "close": ("🔒 Ticket geschlossen", COLOR_ERROR),
            "claim": ("🙋 Ticket übernommen", COLOR_INFO),
            "unclaim": ("🔓 Ticket freigegeben", COLOR_WARNING),
        }.get(action, ("📋 Ticket-Aktion", COLOR_INFO))

        creator = guild.get_member(ticket.get("user_id", 0))
        embed = discord.Embed(
            title=cfg[0],
            color=cfg[1],
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="🆔 Ticket", value=f"`#{tid:04d}`", inline=True)
        embed.add_field(
            name="👤 Ersteller",
            value=creator.mention if creator else f"<@{ticket.get('user_id', 0)}>",
            inline=True,
        )
        embed.add_field(name="🛡️ Akteur", value=actor.mention, inline=True)
        if ticket.get("betreff"):
            embed.add_field(name="📌 Betreff", value=ticket["betreff"][:200], inline=False)

        if transcript_bytes:
            file = discord.File(
                io.BytesIO(transcript_bytes),
                filename=f"ticket-{tid:04d}-transcript.txt",
            )
            await log_channel.send(embed=embed, file=file)
        else:
            await log_channel.send(embed=embed)
    except Exception as e:
        print(f"⚠️ [Ticket] Log-Fehler: {e}")


async def close_ticket(guild: discord.Guild, channel_id: int, closer: discord.Member) -> None:
    ticket = open_tickets.get(channel_id)
    if not ticket:
        return

    channel = guild.get_channel(channel_id)
    raw_bytes: bytes | None = None
    if channel:
        buf = await create_ticket_transcript(channel)
        raw_bytes = buf.getvalue()

    open_tickets.pop(channel_id, None)
    save_tickets()

    await send_ticket_log(guild, "close", ticket, closer, raw_bytes)

    creator = guild.get_member(ticket.get("user_id", 0))
    if creator and raw_bytes:
        try:
            tid = int(ticket.get("ticket_id") or 0)
            dm_embed = discord.Embed(
                title=f"🔒 Dein Ticket #{tid:04d} wurde geschlossen",
                description=(
                    f"**Server:** {guild.name}\n"
                    f"**Betreff:** {ticket.get('betreff', '–')}\n"
                    f"**Geschlossen von:** {closer.display_name}\n\n"
                    f"Im Anhang findest du das vollständige Transcript."
                ),
                color=COLOR_TICKET,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            await creator.send(
                embed=dm_embed,
                file=discord.File(
                    io.BytesIO(raw_bytes),
                    filename=f"ticket-{tid:04d}-transcript.txt",
                ),
            )
        except Exception:
            pass

    if channel:
        try:
            await channel.send(
                embed=discord.Embed(
                    title="🔒 Ticket geschlossen",
                    description="Transcript gespeichert. Dieser Kanal wird in **5 Sekunden** gelöscht...",
                    color=COLOR_ERROR,
                )
            )
            await asyncio.sleep(5)
            await channel.delete(reason=f"Ticket geschlossen von {closer.display_name}")
        except Exception as e:
            print(f"⚠️ [Ticket] Kanal konnte nicht gelöscht werden: {e}")


class TicketCreateModal(discord.ui.Modal, title="Support-Ticket erstellen"):
    betreff = discord.ui.TextInput(
        label="Betreff / Kurztitel",
        placeholder="z.B. Frage zur Verknüpfung / Bug-Report",
        min_length=3,
        max_length=100,
    )
    beschreibung = discord.ui.TextInput(
        label="Beschreibung deines Anliegens",
        style=discord.TextStyle.paragraph,
        placeholder="Beschreibe dein Anliegen bitte so genau wie möglich...",
        min_length=10,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        for ch_id, t in list(open_tickets.items()):
            if t.get("user_id") == interaction.user.id:
                existing_ch = interaction.guild.get_channel(ch_id)
                if existing_ch:
                    await interaction.response.send_message(
                        f"❌ Du hast bereits ein offenes Ticket: {existing_ch.mention}",
                        ephemeral=True,
                    )
                    return
                open_tickets.pop(ch_id, None)
                save_tickets()

        await interaction.response.defer(ephemeral=True)
        ticket_num = get_next_ticket_number()

        category = interaction.guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        if not isinstance(category, discord.CategoryChannel):
            category = None

        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            ),
        }
        support_role = discord.utils.get(interaction.guild.roles, name=TICKET_SUPPORT_ROLE)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )

        safe_user = re.sub(r"[^a-z0-9]", "", interaction.user.name.lower())[:12] or "user"
        channel = await interaction.guild.create_text_channel(
            name=f"ticket-{ticket_num:04d}-{safe_user}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket #{ticket_num:04d} von {interaction.user.display_name} | {self.betreff.value}",
            reason=f"Support-Ticket #{ticket_num:04d}",
        )

        ticket_info = {
            "user_id": interaction.user.id,
            "ticket_id": ticket_num,
            "status": "open",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "claimed_by": None,
            "betreff": self.betreff.value,
            "beschreibung": self.beschreibung.value,
        }
        open_tickets[channel.id] = ticket_info
        save_tickets()

        embed = discord.Embed(
            title=f"🎫 Ticket #{ticket_num:04d} – {self.betreff.value}",
            description=(
                f"Willkommen {interaction.user.mention}!\n"
                f"Unser Support-Team kümmert sich schnellstmöglich um dein Anliegen.\n\n"
                f"**📝 Beschreibung:**\n>>> {self.beschreibung.value}"
            ),
            color=COLOR_TICKET,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"Ticket #{ticket_num:04d} • Erstellt von {interaction.user.display_name}")

        ping = f"{interaction.user.mention}" + (f" {support_role.mention}" if support_role else "")
        await channel.send(content=ping, embed=embed, view=TicketControlView())
        await send_ticket_log(interaction.guild, "open", ticket_info, interaction.user)
        await interaction.followup.send(
            f"✅ Dein Ticket wurde erstellt: {channel.mention}", ephemeral=True
        )


class TicketOpenView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫 Ticket erstellen",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_open_btn",
    )
    async def btn_open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TicketCreateModal())


class TicketControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🙋 Übernehmen / Freigeben",
        style=discord.ButtonStyle.success,
        custom_id="ticket_claim_btn",
    )
    async def btn_claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        ticket = open_tickets.get(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("❌ Kein aktives Ticket.", ephemeral=True)
            return
        if not _is_support(interaction.user):
            await interaction.response.send_message(
                f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann Tickets übernehmen!", ephemeral=True
            )
            return

        current_claim = ticket.get("claimed_by")
        if current_claim == interaction.user.id:
            ticket["claimed_by"] = None
            save_tickets()
            await send_ticket_log(interaction.guild, "unclaim", ticket, interaction.user)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🔓 Ticket freigegeben",
                    description=f"{interaction.user.mention} hat das Ticket wieder freigegeben.",
                    color=COLOR_WARNING,
                )
            )
        elif current_claim is not None:
            await interaction.response.send_message(
                f"❌ Dieses Ticket wird bereits von <@{current_claim}> bearbeitet!",
                ephemeral=True,
            )
        else:
            ticket["claimed_by"] = interaction.user.id
            save_tickets()
            await send_ticket_log(interaction.guild, "claim", ticket, interaction.user)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🙋 Ticket übernommen",
                    description=f"{interaction.user.mention} kümmert sich jetzt um dein Anliegen!",
                    color=COLOR_SUCCESS,
                )
            )

    @discord.ui.button(
        label="🔒 Ticket schließen",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_btn",
    )
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        ticket = open_tickets.get(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("❌ Kein aktives Ticket.", ephemeral=True)
            return
        if not _is_support(interaction.user) and interaction.user.id != ticket.get("user_id"):
            await interaction.response.send_message(
                "❌ Nur Support oder der Ticket-Ersteller können das Ticket schließen!",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("🔒 Ticket wird geschlossen...", ephemeral=True)
        await close_ticket(interaction.guild, interaction.channel_id, interaction.user)


class TicketsCog(commands.Cog, name="Tickets"):
    """Support-Ticket-System mit persistenten Panels & Transcripts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        load_tickets()
        self.bot.add_view(TicketOpenView())
        self.bot.add_view(TicketControlView())

    @app_commands.command(name="ticketpanel", description="🎫 Sendet das Ticket-Panel (nur Admins)")
    @is_admin_user()
    async def cmd_ticketpanel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🎫 Support-Tickets",
            description=(
                "Hast du ein Problem, eine Frage oder ein Anliegen?\n\n"
                "Klicke auf den Button unten, um ein **privates Ticket zu erstellen**.\n"
                "Unser Support-Team kümmert sich schnellstmöglich darum!\n\n"
                "**Hinweise:**\n"
                "• Bitte nur ein Ticket pro Anliegen eröffnen\n"
                "• Beschreibe dein Anliegen so genau wie möglich\n"
                "• Nach Abschluss erhältst du automatisch ein Transcript per DM"
            ),
            color=COLOR_TICKET,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text="Ticket System • Ein Ticket pro Anliegen")
        await interaction.response.send_message(embed=embed, view=TicketOpenView())

    @app_commands.command(name="closeticket", description="🔒 Schließt das aktuelle Ticket")
    async def cmd_closeticket(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        ticket = open_tickets.get(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("❌ Dies ist kein Ticket-Kanal!", ephemeral=True)
            return
        if not _is_support(interaction.user) and interaction.user.id != ticket.get("user_id"):
            await interaction.response.send_message(
                "❌ Nur Support oder der Ticket-Ersteller können das Ticket schließen!",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("🔒 Ticket wird geschlossen...", ephemeral=True)
        await close_ticket(interaction.guild, interaction.channel_id, interaction.user)

    @app_commands.command(name="adduser", description="➕ Fügt einen User zum aktuellen Ticket hinzu")
    @app_commands.describe(member="Der User der hinzugefügt werden soll")
    async def cmd_adduser(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if interaction.channel_id not in open_tickets:
            await interaction.response.send_message("❌ Dies ist kein Ticket-Kanal!", ephemeral=True)
            return
        if not _is_support(interaction.user):
            await interaction.response.send_message(
                f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann User hinzufügen!", ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(interaction.channel_id)
        if isinstance(channel, discord.TextChannel):
            await channel.set_permissions(
                member, view_channel=True, send_messages=True, read_message_history=True
            )
        embed = discord.Embed(
            title="➕ User hinzugefügt",
            description=f"{member.mention} wurde von {interaction.user.mention} zum Ticket hinzugefügt.",
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="removeuser", description="➖ Entfernt einen User aus dem aktuellen Ticket")
    @app_commands.describe(member="Der User der entfernt werden soll")
    async def cmd_removeuser(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        ticket = open_tickets.get(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("❌ Dies ist kein Ticket-Kanal!", ephemeral=True)
            return
        if not _is_support(interaction.user):
            await interaction.response.send_message(
                f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann User entfernen!", ephemeral=True
            )
            return
        if member.id == ticket.get("user_id"):
            await interaction.response.send_message(
                "❌ Der Ticket-Ersteller kann nicht entfernt werden!", ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(interaction.channel_id)
        if isinstance(channel, discord.TextChannel):
            await channel.set_permissions(member, overwrite=None)
        embed = discord.Embed(
            title="➖ User entfernt",
            description=f"{member.mention} wurde von {interaction.user.mention} aus dem Ticket entfernt.",
            color=COLOR_ERROR,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="tickets", description="📋 Zeigt alle offenen Tickets (nur Support)")
    async def cmd_tickets(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        if not _is_support(interaction.user):
            await interaction.response.send_message(
                f"❌ Nur **{TICKET_SUPPORT_ROLE}** kann alle Tickets einsehen!", ephemeral=True
            )
            return

        # Verwaiste Tickets automatisch bereinigen
        stale = [cid for cid in open_tickets if interaction.guild.get_channel(cid) is None]
        for cid in stale:
            open_tickets.pop(cid, None)
        if stale:
            save_tickets()

        if not open_tickets:
            await interaction.response.send_message("✅ Keine offenen Tickets!", ephemeral=True)
            return

        lines = []
        for ch_id, t in sorted(open_tickets.items(), key=lambda x: x[1].get("ticket_id", 0)):
            tid = int(t.get("ticket_id") or 0)
            claimed = f"<@{t['claimed_by']}>" if t.get("claimed_by") else "*Offen*"
            created_disp = ""
            if t.get("created_at"):
                try:
                    dt = datetime.datetime.fromisoformat(t["created_at"])
                    created_disp = f" • <t:{int(dt.timestamp())}:R>"
                except Exception:
                    pass
            lines.append(
                f"🎫 **`#{tid:04d}`** <#{ch_id}> — **{t.get('betreff', 'Kein Betreff')}**\n"
                f"╰ 👤 <@{t.get('user_id', 0)}> • 🛡️ {claimed}{created_disp}"
            )

        embed = discord.Embed(
            title=f"📋 Offene Tickets ({len(open_tickets)})",
            description="\n\n".join(lines[:20]),
            color=COLOR_TICKET,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketsCog(bot))
