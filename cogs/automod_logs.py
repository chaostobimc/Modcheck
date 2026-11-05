"""AutoMod & Audit-Logs Cog (Carl-bot, ProBot & MEE6 Ersatz): Vollständiger AutoMod + 4-Kanal Event-Logging."""
from __future__ import annotations

import collections
import datetime
import re
import time
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import has_mod_role, is_user_admin
from bot_core.config import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    LOG_CHANNEL_ID,
    MESSAGE_LOG_CHANNEL_ID,
)
from bot_core.server_store import get_dynamic_channel_id, moderation_store, server_cfg_store
from cogs.moderation import log_mod_case

_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discordapp\.com/invite|discord\.com/invite)/([a-zA-Z0-9-]+)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _resolve_log_channel(guild: discord.Guild, category: str) -> tuple[discord.TextChannel | None, set[int]]:
    """Ermittelt den konfigurierten Log-Kanal für `message`, `member`, `voice` oder `server`."""
    g = server_cfg_store.get_guild(guild.id)
    logs = g.get("logs") or {}
    ignored = {int(x) for x in (logs.get("ignored_channels") or []) if str(x).isdigit()}

    cat_cfg = logs.get(category) or {}
    if cat_cfg.get("enabled") and cat_cfg.get("channel_id"):
        ch = guild.get_channel(int(cat_cfg["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            return ch, ignored

    if category == "message" and MESSAGE_LOG_CHANNEL_ID:
        ch = guild.get_channel(int(MESSAGE_LOG_CHANNEL_ID))
        if isinstance(ch, discord.TextChannel):
            return ch, ignored

    fallback_id = get_dynamic_channel_id(guild.id, "log_channel_id", LOG_CHANNEL_ID)
    if fallback_id:
        ch = guild.get_channel(fallback_id)
        if isinstance(ch, discord.TextChannel):
            return ch, ignored
    return None, ignored


class AutoModGroup(app_commands.Group):
    """Slash-Command-Gruppe `/automod ...`."""

    def __init__(self) -> None:
        super().__init__(
            name="automod",
            description="🤖 AutoMod: Anti-Invite, Anti-Link, Anti-Spam, Mass-Mentions, Caps & Wortfilter",
        )

    @app_commands.command(name="status", description="📊 Zeigt die aktuelle AutoMod-Konfiguration (Admins)")
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        am = moderation_store.get_guild(interaction.guild.id).get("automod") or {}
        b = lambda key: "✅ Aktiv" if am.get(key) else "⏸️ Aus"
        embed = discord.Embed(
            title=f"🤖 AutoMod-Status – {interaction.guild.name}",
            description=f"**Hauptschalter:** {'🟢 **AKTIV**' if am.get('enabled') else '🔴 **DEAKTIVIERT**'}",
            color=COLOR_SUCCESS if am.get("enabled") else COLOR_WARNING,
        )
        embed.add_field(
            name="🛡️ Filter",
            value=(
                f"• **Anti-Discord-Invites:** {b('anti_invite')}\n"
                f"• **Anti-Links:** {b('anti_links')} (`{len(am.get('allowed_domains', []))}` erlaubte Domains)\n"
                f"• **Anti-Spam:** {b('anti_spam')} (`{am.get('spam_max_msgs', 6)}` Nachr. / `{am.get('spam_window_sec', 5)}s`)\n"
                f"• **Anti-Mass-Mentions:** {b('anti_mass_mention')} (ab `{am.get('max_mentions', 5)}` Pings)\n"
                f"• **Anti-Caps:** {b('anti_caps')} (ab `{am.get('caps_percent', 75)}%` Großbuchstaben)\n"
                f"• **Bad-Word-Filter:** `{len(am.get('bad_words', []))}` gesperrte Wörter"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚖️ Sanktion bei Verstoß",
            value=f"`{am.get('punishment', 'delete_warn')}` (Timeout: `{am.get('timeout_sec', 300)}s`)",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="toggle", description="⚙️ Schaltet AutoMod oder einzelne Filter ein/aus (Admins)")
    @app_commands.choices(
        modul=[
            app_commands.Choice(name="🟢 Hauptschalter (AutoMod gesamt)", value="enabled"),
            app_commands.Choice(name="🔗 Anti-Discord-Invites", value="anti_invite"),
            app_commands.Choice(name="🌐 Anti-Links (außer Whitelist)", value="anti_links"),
            app_commands.Choice(name="⚡ Anti-Spam (Nachrichten-Flut)", value="anti_spam"),
            app_commands.Choice(name="📣 Anti-Mass-Mentions", value="anti_mass_mention"),
            app_commands.Choice(name="🔠 Anti-Caps (Schreien)", value="anti_caps"),
        ]
    )
    async def cmd_toggle(
        self, interaction: discord.Interaction, modul: str, aktiv: bool
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> None:
            g["automod"][modul] = bool(aktiv)
            if modul != "enabled" and aktiv:
                g["automod"]["enabled"] = True

        moderation_store.mutate(interaction.guild.id, _upd)
        await interaction.response.send_message(
            f"✅ AutoMod-Modul **`{modul}`** ist jetzt **{'Aktiviert' if aktiv else 'Deaktiviert'}**.",
            ephemeral=True,
        )

    @app_commands.command(name="punishment", description="⚖️ Legt die automatische Strafe bei AutoMod-Treffern fest")
    @app_commands.choices(
        aktion=[
            app_commands.Choice(name="🗑️ Nur Nachricht löschen + Hinweis", value="delete_only"),
            app_commands.Choice(name="⚠️ Löschen + Verwarnung eintragen", value="delete_warn"),
            app_commands.Choice(name="⏳ Löschen + automatischer Timeout", value="timeout"),
        ]
    )
    async def cmd_punishment(
        self,
        interaction: discord.Interaction,
        aktion: str,
        timeout_sekunden: app_commands.Range[int, 30, 86400] = 300,
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        moderation_store.mutate(
            interaction.guild.id,
            lambda g: g["automod"].update({"punishment": aktion, "timeout_sec": int(timeout_sekunden)}),
        )
        await interaction.response.send_message(
            f"✅ AutoMod-Strafe auf **`{aktion}`** gesetzt.", ephemeral=True
        )

    @app_commands.command(name="badword-add", description="🚫 Fügt ein verbotenes Wort zum Wortfilter hinzu (Admins)")
    async def cmd_badword_add(self, interaction: discord.Interaction, wort: str) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        w = wort.strip().lower()

        def _add(g: dict[str, Any]) -> int:
            g["automod"]["enabled"] = True
            bw: list[str] = g["automod"].setdefault("bad_words", [])
            if w and w not in bw:
                bw.append(w)
            return len(bw)

        total = moderation_store.mutate(interaction.guild.id, _add)
        await interaction.response.send_message(
            f"🚫 Wort `||{w}||` gesperrt (`{total}` Wörter auf der Blacklist).", ephemeral=True
        )

    @app_commands.command(name="badword-remove", description="✅ Entfernt ein Wort aus dem Wortfilter (Admins)")
    async def cmd_badword_remove(self, interaction: discord.Interaction, wort: str) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        w = wort.strip().lower()
        moderation_store.mutate(
            interaction.guild.id,
            lambda g: g["automod"].__setitem__(
                "bad_words", [x for x in g["automod"].get("bad_words", []) if x != w]
            ),
        )
        await interaction.response.send_message(f"✅ Wort `{w}` aus dem Filter entfernt.", ephemeral=True)

    @app_commands.command(name="link-whitelist", description="🌐 Fügt eine Domain zur Link-Whitelist hinzu / entfernt sie")
    async def cmd_link_whitelist(self, interaction: discord.Interaction, domain: str) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        d = domain.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]

        def _tog(g: dict[str, Any]) -> bool:
            lst: list[str] = g["automod"].setdefault("allowed_domains", [])
            if d in lst:
                lst.remove(d)
                return False
            lst.append(d)
            return True

        added = moderation_store.mutate(interaction.guild.id, _tog)
        await interaction.response.send_message(
            f"{'✅ Erlaubt' if added else '➖ Entfernt'}: Domain `{d}`.", ephemeral=True
        )

    @app_commands.command(name="exempt-channel", description="🛡️ Nimmt einen Kanal vom AutoMod aus (oder hebt es auf)")
    async def cmd_exempt_channel(
        self, interaction: discord.Interaction, kanal: discord.TextChannel
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _tog(g: dict[str, Any]) -> bool:
            lst: list[int] = g["automod"].setdefault("exempt_channels", [])
            if kanal.id in lst:
                lst.remove(kanal.id)
                return False
            lst.append(kanal.id)
            return True

        ex = moderation_store.mutate(interaction.guild.id, _tog)
        await interaction.response.send_message(
            f"{'🛡️ Ausgenommen' if ex else '🔍 Wieder überwacht'}: {kanal.mention}.", ephemeral=True
        )


class LogsGroup(app_commands.Group):
    """Slash-Command-Gruppe `/logs ...`."""

    def __init__(self) -> None:
        super().__init__(
            name="logs",
            description="📋 Server-Audit-Logs (Nachrichten, Mitglieder, Voice & Moderation)",
        )

    @app_commands.command(name="status", description="📋 Zeigt alle konfigurierten Log-Kanäle (Admins)")
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        g = server_cfg_store.get_guild(interaction.guild.id)
        logs = g.get("logs") or {}
        fallback = get_dynamic_channel_id(interaction.guild.id, "log_channel_id", LOG_CHANNEL_ID)
        fb_txt = f"<#{fallback}>" if fallback else "*(nicht gesetzt)*"

        def _fmt(cat: str) -> str:
            c = logs.get(cat) or {}
            if c.get("enabled") and c.get("channel_id"):
                return f"✅ <#{c['channel_id']}>"
            return f"↩️ Fallback ({fb_txt})" if fallback else "⚪ Nicht gesetzt"

        embed = discord.Embed(
            title=f"📋 Audit-Logs Übersicht – {interaction.guild.name}",
            description=(
                f"• 💬 **Nachrichten-Logs (Edit/Delete/Bulk):** {_fmt('message')}\n"
                f"• 👥 **Mitglieder-Logs (Join/Leave/Rollen/Nick):** {_fmt('member')}\n"
                f"• 🎙️ **Voice-Logs (Join/Leave/Switch):** {_fmt('voice')}\n"
                f"• 🛡️ **Moderations- & Server-Logs:** {_fmt('mod')}\n"
                f"• 🌐 **Globaler Standard-Log:** {fb_txt}"
            ),
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set-channel", description="⚙️ Weist einer Log-Kategorie (oder allen) einen Kanal zu")
    @app_commands.choices(
        kategorie=[
            app_commands.Choice(name="🌐 Alle Log-Arten (Einzelner Sammel-Kanal)", value="all"),
            app_commands.Choice(name="💬 Nachrichten (Edit, Delete, Bulk)", value="message"),
            app_commands.Choice(name="👥 Mitglieder (Join, Leave, Rollen, Nick)", value="member"),
            app_commands.Choice(name="🎙️ Sprachkanäle (Join, Leave, Move)", value="voice"),
            app_commands.Choice(name="🛡️ Moderation & Server-Events", value="mod"),
        ]
    )
    async def cmd_set_channel(
        self, interaction: discord.Interaction, kategorie: str, kanal: discord.TextChannel
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> None:
            logs = g.setdefault("logs", {})
            if kategorie == "all":
                g.setdefault("channels", {})["log_channel_id"] = kanal.id
                for k in ("message", "member", "voice", "server", "mod"):
                    logs[k] = {"enabled": True, "channel_id": kanal.id}
            else:
                logs[kategorie] = {"enabled": True, "channel_id": kanal.id}
                if kategorie == "mod":
                    logs["server"] = {"enabled": True, "channel_id": kanal.id}

        server_cfg_store.mutate(interaction.guild.id, _upd)
        await interaction.response.send_message(
            f"✅ Log-Kanal für **`{kategorie}`** auf {kanal.mention} gesetzt!", ephemeral=True
        )

    @app_commands.command(name="ignore-channel", description="🙈 Ignoriert einen Kanal in den Nachrichten-Logs (Toggle)")
    async def cmd_ignore_channel(
        self, interaction: discord.Interaction, kanal: discord.TextChannel
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _tog(g: dict[str, Any]) -> bool:
            ign = g.setdefault("logs", {}).setdefault("ignored_channels", [])
            if kanal.id in ign:
                ign.remove(kanal.id)
                return False
            ign.append(kanal.id)
            return True

        ignored = server_cfg_store.mutate(interaction.guild.id, _tog)
        await interaction.response.send_message(
            f"{'🙈 Ignoriert' if ignored else '👁️ Wieder geloggt'}: {kanal.mention}.",
            ephemeral=True,
        )


class AutoModLogsCog(commands.Cog, name="AutoModLogs"):
    """Echtzeit-AutoMod & detailliertes 4-Kanal Server-Logging."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._spam_buckets: dict[tuple[int, int], collections.deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=20)
        )
        self.automod_group = AutoModGroup()
        self.logs_group = LogsGroup()
        self.bot.tree.add_command(self.automod_group)
        self.bot.tree.add_command(self.logs_group)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.automod_group.name, type=self.automod_group.type)
        self.bot.tree.remove_command(self.logs_group.name, type=self.logs_group.type)

    # ── AutoMod Prüfung ───────────────────────────────────────────────────

    async def _check_automod(self, message: discord.Message) -> str | None:
        assert message.guild is not None and isinstance(message.author, discord.Member)
        am = moderation_store.get_guild(message.guild.id).get("automod") or {}
        if not am.get("enabled"):
            return None
        if has_mod_role(message.author):
            return None
        if message.channel.id in (am.get("exempt_channels") or []):
            return None
        ex_roles = set(am.get("exempt_roles") or [])
        if ex_roles and any(r.id in ex_roles for r in message.author.roles):
            return None

        text = message.content or ""

        # 1) Anti-Spam
        if am.get("anti_spam", True):
            now = time.monotonic()
            win = max(2, int(am.get("spam_window_sec") or 5))
            max_m = max(3, int(am.get("spam_max_msgs") or 6))
            dq = self._spam_buckets[(message.guild.id, message.author.id)]
            dq.append(now)
            while dq and now - dq[0] > win:
                dq.popleft()
            if len(dq) >= max_m:
                dq.clear()
                return f"Anti-Spam ({max_m} Nachrichten in {win}s)"

        # 2) Anti-Discord-Invites
        if am.get("anti_invite") and text:
            m_inv = _INVITE_RE.search(text)
            if m_inv:
                code = m_inv.group(1)
                own_codes = set()
                try:
                    if message.guild.vanity_url_code:
                        own_codes.add(message.guild.vanity_url_code.lower())
                except Exception:
                    pass
                if code.lower() not in own_codes:
                    return "Unerlaubter Discord-Einladungslink"

        # 3) Anti-Links (mit Domain-Whitelist)
        if am.get("anti_links") and text:
            allowed = {str(d).lower() for d in (am.get("allowed_domains") or [])}
            for raw_url in _URL_RE.findall(text):
                host = (urlparse(raw_url).hostname or "").lower().removeprefix("www.")
                if host and not any(host == dom or host.endswith("." + dom) for dom in allowed):
                    return f"Unerlaubter Link (`{host}`)"

        # 4) Anti-Mass-Mentions
        if am.get("anti_mass_mention", True):
            mentions_total = len(message.mentions) + len(message.role_mentions)
            max_men = max(3, int(am.get("max_mentions") or 5))
            if mentions_total >= max_men:
                return f"Massen-Erwähnung ({mentions_total} Pings)"

        # 5) Anti-Caps
        if am.get("anti_caps") and len(text) >= int(am.get("caps_min_len") or 12):
            letters = [ch for ch in text if ch.isalpha()]
            if len(letters) >= int(am.get("caps_min_len") or 12):
                ratio = sum(1 for ch in letters if ch.isupper()) / len(letters) * 100.0
                if ratio >= float(am.get("caps_percent") or 75):
                    return f"Übermäßige Großschreibung ({ratio:.0f}% CAPS)"

        # 6) Bad-Words
        bad_words = [str(w).lower() for w in (am.get("bad_words") or []) if w]
        if bad_words and text:
            lowered = text.lower()
            for bw in bad_words:
                if bw in lowered:
                    return "Gesperrter Ausdruck (Wortfilter)"

        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        violation = await self._check_automod(message)
        if not violation:
            return

        try:
            await message.delete()
        except Exception:
            pass

        am = moderation_store.get_guild(message.guild.id).get("automod") or {}
        punishment = str(am.get("punishment") or "delete_warn")
        extra_info = ""

        if punishment == "timeout":
            dur = max(30, int(am.get("timeout_sec") or 300))
            try:
                await message.author.timeout(
                    discord.utils.utcnow() + datetime.timedelta(seconds=dur),
                    reason=f"AutoMod: {violation}",
                )
                extra_info = f" • Timeout `{dur}s`"
            except Exception:
                pass
            if self.bot.user:
                await log_mod_case(
                    message.guild, "AUTOMOD-TIMEOUT", message.author, self.bot.user, violation, f"{dur}s"
                )
        elif punishment == "delete_warn" and self.bot.user:
            case_id = await log_mod_case(message.guild, "AUTOMOD-WARN", message.author, self.bot.user, violation)
            def _record_warning(g: dict[str, Any]) -> None:
                warnings = g.setdefault("warnings", {}).setdefault(str(message.author.id), [])
                warnings.append({
                    "id": max((int(w.get("id") or 0) for w in warnings), default=0) + 1,
                    "case_id": case_id, "mod_id": self.bot.user.id,
                    "reason": violation, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
            moderation_store.mutate(message.guild.id, _record_warning)

        warn_emb = discord.Embed(
            description=f"🤖 {message.author.mention}, deine Nachricht wurde entfernt (**{violation}**{extra_info}).",
            color=COLOR_WARNING,
        )
        try:
            await message.channel.send(embed=warn_emb, delete_after=8)
        except Exception:
            pass

    # ── Audit-Logging: Nachrichten ────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        log_ch, ignored = _resolve_log_channel(message.guild, "message")
        if not log_ch or message.channel.id in ignored or message.channel.id == log_ch.id:
            return

        embed = discord.Embed(
            title="🗑️ Nachricht gelöscht",
            color=COLOR_ERROR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="👤 Autor", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
        embed.add_field(name="📍 Kanal", value=message.channel.mention, inline=True)
        content = (message.content or "").strip() or "*[Kein Text / nur Anhang]*"
        embed.add_field(name="💬 Inhalt", value=content[:1020], inline=False)
        if message.attachments:
            att_list = "\n".join(a.filename for a in message.attachments[:5])
            embed.add_field(name="📎 Anhänge", value=att_list[:500], inline=False)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or not messages[0].guild:
            return
        guild = messages[0].guild
        log_ch, ignored = _resolve_log_channel(guild, "message")
        if not log_ch or messages[0].channel.id in ignored:
            return
        embed = discord.Embed(
            title="🧹 Massen-Löschung (Bulk Delete)",
            description=f"**{len(messages)}** Nachrichten in {messages[0].channel.mention} gelöscht.",
            color=COLOR_WARNING,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        log_ch, ignored = _resolve_log_channel(after.guild, "message")
        if not log_ch or after.channel.id in ignored:
            return
        embed = discord.Embed(
            title="✏️ Nachricht bearbeitet",
            description=f"[Zur Nachricht springen]({after.jump_url})",
            color=COLOR_WARNING,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        embed.add_field(name="👤 Autor", value=after.author.mention, inline=True)
        embed.add_field(name="📍 Kanal", value=after.channel.mention, inline=True)
        embed.add_field(name="Vorher", value=(before.content or "*leer*")[:1020], inline=False)
        embed.add_field(name="Nachher", value=(after.content or "*leer*")[:1020], inline=False)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

    # ── Audit-Logging: Mitglieder & Rollen ────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        log_ch, _ = _resolve_log_channel(member.guild, "member")
        if not log_ch:
            return
        age_days = (datetime.datetime.now(datetime.timezone.utc) - member.created_at).days
        warn_new = " ⚠️ *(Sehr junger Account!)*" if age_days < 7 else ""
        embed = discord.Embed(
            title="📥 Mitglied beigetreten",
            description=(
                f"{member.mention} (`{member}` • `{member.id}`)\n"
                f"📅 **Account erstellt:** <t:{int(member.created_at.timestamp())}:R>{warn_new}"
            ),
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        log_ch, _ = _resolve_log_channel(member.guild, "member")
        if not log_ch:
            return
        roles = [r.mention for r in member.roles if r != member.guild.default_role]
        embed = discord.Embed(
            title="📤 Mitglied hat den Server verlassen",
            description=f"**{member}** (`{member.id}`)",
            color=COLOR_ERROR,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if roles:
            embed.add_field(name="🎭 Bisherige Rollen", value=", ".join(roles)[:1020], inline=False)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        log_ch, _ = _resolve_log_channel(after.guild, "member")
        if not log_ch:
            return
        if before.nick != after.nick:
            embed = discord.Embed(
                title="🏷️ Nickname geändert",
                description=f"{after.mention}: `{before.nick or before.name}` ➡️ `{after.nick or after.name}`",
                color=COLOR_INFO,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            try:
                await log_ch.send(embed=embed)
            except Exception:
                pass

        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            parts = []
            if added:
                parts.append("➕ **Hinzugefügt:** " + ", ".join(r.mention for r in added))
            if removed:
                parts.append("➖ **Entfernt:** " + ", ".join(r.mention for r in removed))
            embed = discord.Embed(
                title="🎭 Rollen-Änderung",
                description=f"{after.mention}\n" + "\n".join(parts),
                color=COLOR_INFO,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            try:
                await log_ch.send(embed=embed)
            except Exception:
                pass

    # ── Audit-Logging: Voice-Kanäle ───────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot or before.channel == after.channel:
            return
        log_ch, _ = _resolve_log_channel(member.guild, "voice")
        if not log_ch:
            return
        if before.channel is None and after.channel is not None:
            desc = f"🎙️ {member.mention} ist **{after.channel.mention}** beigetreten."
            col = COLOR_SUCCESS
        elif before.channel is not None and after.channel is None:
            desc = f"🔇 {member.mention} hat **{before.channel.mention}** verlassen."
            col = COLOR_ERROR
        else:
            desc = f"🔀 {member.mention} wechselte von **{before.channel.mention}** ➡️ **{after.channel.mention}**."
            col = COLOR_INFO
        embed = discord.Embed(
            description=desc,
            color=col,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoModLogsCog(bot))
