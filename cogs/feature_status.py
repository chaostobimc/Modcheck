"""Feature-Status & Interaktives Admin-Kontrollzentrum (`/feature-status`).

Zeigt auf einen Blick, welche Bot-Funktionen aktiv oder unkonfiguriert sind, und erlaubt das Zuweisen
aller Kanäle & Rollen direkt per Dropdown in Discord — komplett ohne `.env`-Bearbeitung!
"""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import is_user_admin
from bot_core.config import (
    ADMIN_ROLE_NAME,
    ALLOWED_CHANNEL_ID,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_SUCCESS,
    FISHING_CHANNEL_ID,
    GAMBLING_CHANNEL_ID,
    LOG_CHANNEL_ID,
    MOD_ROLE_NAME,
    REVIEW_CHANNEL_ID,
    STREAMER_CHANNEL,
    TEMP_VOICE_CATEGORY_ID,
    TEMP_VOICE_CHANNEL_ID,
    TICKET_CATEGORY_ID,
    TICKET_LOG_CHANNEL_ID,
    WELCOME_CHANNEL_ID,
)
from bot_core.server_store import (
    birthdays_store,
    community_store,
    counting_store,
    get_dynamic_channel_id,
    get_dynamic_role_id,
    levels_store,
    moderation_store,
    roles_store,
    server_cfg_store,
    set_dynamic_channel_id,
    set_dynamic_role_id,
)


def _ch_badge(guild: discord.Guild, ch_id: int | None) -> str:
    if not ch_id:
        return "⚪ *Nicht gesetzt*"
    ch = guild.get_channel(int(ch_id))
    return ch.mention if ch else f"⚠️ *Gelöscht (`{ch_id}`)*"


def _role_badge(guild: discord.Guild, role_id: int | None, fallback_name: str = "") -> str:
    if role_id:
        r = guild.get_role(int(role_id))
        if r:
            return r.mention
    if fallback_name:
        r = discord.utils.get(guild.roles, name=fallback_name)
        if r:
            return f"{r.mention} *(.env)*"
    return "⚪ *Nicht gesetzt*"


def build_feature_status_embed(guild: discord.Guild) -> discord.Embed:
    """Erstellt die komplette Live-Übersicht aller Modcheck-Module für den Server."""
    scfg = server_cfg_store.get_guild(guild.id)
    bday = birthdays_store.get_guild(guild.id)
    cnt = counting_store.get_guild(guild.id)
    lvl = levels_store.get_guild(guild.id)
    rol = roles_store.get_guild(guild.id)
    mod = moderation_store.get_guild(guild.id)
    com = community_store.get_guild(guild.id)

    # 1. Geburtstage & Counting
    b_cfg = bday.get("config") or {}
    b_ready = bool(b_cfg.get("enabled", True) and (b_cfg.get("channel_id") or b_cfg.get("role_id")))
    b_dot = "🟢" if b_ready else ("🟡" if b_cfg.get("enabled", True) else "🔴")
    b_line = (
        f"{b_dot} **🎂 Geburtstage:** Kanal {_ch_badge(guild, b_cfg.get('channel_id'))} • "
        f"Rolle {_role_badge(guild, b_cfg.get('role_id'))} • `{len(bday.get('users', {}))}` Einträge"
    )

    c_ready = bool(cnt.get("enabled") and cnt.get("channel_id"))
    c_dot = "🟢" if c_ready else "⚪"
    c_line = (
        f"{c_dot} **🔢 Counting:** {_ch_badge(guild, cnt.get('channel_id'))} • "
        f"Stand `{cnt.get('current', 0):,}` • Rekord 🏆 `{cnt.get('highscore', 0):,}`"
    )

    # 2. Leveling, Rollen & Welcome
    l_cfg = lvl.get("config") or {}
    l_dot = "🟢" if l_cfg.get("enabled", True) else "🔴"
    l_ch = _ch_badge(guild, l_cfg.get("announce_channel_id")) if l_cfg.get("announce_channel_id") else "*Aktueller Chat*"
    l_line = (
        f"{l_dot} **📈 Leveling (MEE6):** {l_ch} • `{len(lvl.get('users', {}))}` User • "
        f"`{len(lvl.get('role_rewards', {}))}` Level-Rollen"
    )

    ar = rol.get("autoroles") or {}
    ar_humans = ar.get("humans") or []
    ar_disp = _role_badge(guild, ar_humans[0]) if ar_humans else "⚪ *Aus*"
    sticky_on = bool((rol.get("sticky_roles") or {}).get("enabled"))
    r_line = (
        f"🟢 **🎭 Rollen-System:** `{len(rol.get('panels', {}))}` Panels • "
        f"`{len(rol.get('reaction_roles', {}))}` Reaction-Msgs • Auto-Role: {ar_disp} • "
        f"Sticky: {'✅' if sticky_on else '⚪'}"
    )

    w_cfg = scfg.get("welcome") or {}
    w_ch_id = int(w_cfg.get("channel_id") or 0) or get_dynamic_channel_id(
        guild.id, "welcome_channel_id", WELCOME_CHANNEL_ID
    )
    w_dot = "🟢" if (w_cfg.get("enabled", True) and w_ch_id) else "⚪"
    gb_cfg = scfg.get("goodbye") or {}
    w_line = (
        f"{w_dot} **👋 Welcome / Leave / Boost:** Join {_ch_badge(guild, w_ch_id)} • "
        f"Leave {_ch_badge(guild, gb_cfg.get('channel_id')) if gb_cfg.get('enabled') else '⚪ *Aus*'} • "
        f"`{len(scfg.get('stats_channels', []))}` Stats-Counter"
    )

    # 3. Moderation, AutoMod & Logs
    am = mod.get("automod") or {}
    am_dot = "🟢" if am.get("enabled") else "⚪"
    log_main = get_dynamic_channel_id(guild.id, "log_channel_id", LOG_CHANNEL_ID)
    logs_cfg = scfg.get("logs") or {}
    active_logs = sum(1 for k in ("message", "member", "voice", "mod") if (logs_cfg.get(k) or {}).get("channel_id"))
    log_dot = "🟢" if (log_main or active_logs > 0) else "⚪"
    m_line = (
        f"🟢 **🛡️ Moderation:** `{max(0, int(mod.get('next_case_id', 1)) - 1)}` Cases • "
        f"Team: Admin {_role_badge(guild, get_dynamic_role_id(guild.id, 'admin_role_id'), ADMIN_ROLE_NAME)} / "
        f"Mod {_role_badge(guild, get_dynamic_role_id(guild.id, 'mod_role_id'), MOD_ROLE_NAME)}\n"
        f"{am_dot} **🤖 AutoMod:** {'Aktiv' if am.get('enabled') else 'Deaktiviert'} • "
        f"{log_dot} **📋 Audit-Logs:** Standard {_ch_badge(guild, log_main)} (`{active_logs}/4` Spezial-Kanäle)"
    )

    # 4. Community Suite
    sug = com.get("suggestions") or {}
    sb = com.get("starboard") or {}
    active_gw = sum(1 for x in (com.get("giveaways") or {}).values() if isinstance(x, dict) and not x.get("ended"))
    com_line = (
        f"{'🟢' if sug.get('enabled') and sug.get('channel_id') else '⚪'} **💡 Vorschläge:** {_ch_badge(guild, sug.get('channel_id'))} • "
        f"{'🟢' if sb.get('enabled') and sb.get('channel_id') else '⚪'} **⭐ Starboard:** {_ch_badge(guild, sb.get('channel_id'))}\n"
        f"🟢 **🎉 Community-Tools:** `{active_gw}` aktive Giveaways • `{len(com.get('custom_commands', {}))}` `!Befehle` • `/poll` & `/afk` bereit"
    )

    # 5. Bestehende Modcheck Kern-Module (Voice, Tickets, Economy, Musik, Stream)
    tv_ch = get_dynamic_channel_id(guild.id, "temp_voice_channel_id", TEMP_VOICE_CHANNEL_ID)
    t_cat = get_dynamic_channel_id(guild.id, "ticket_category_id", TICKET_CATEGORY_ID)
    t_log = get_dynamic_channel_id(guild.id, "ticket_log_channel_id", TICKET_LOG_CHANNEL_ID)
    gamb_ch = get_dynamic_channel_id(guild.id, "gambling_channel_id", GAMBLING_CHANNEL_ID)
    fish_ch = get_dynamic_channel_id(guild.id, "fishing_channel_id", FISHING_CHANNEL_ID)
    cmd_ch = get_dynamic_channel_id(guild.id, "allowed_channel_id", ALLOWED_CHANNEL_ID)
    rev_ch = get_dynamic_channel_id(guild.id, "review_channel_id", REVIEW_CHANNEL_ID)

    core_line = (
        f"{'🟢' if tv_ch else '⚪'} **🎙️ Temp-Voice Trigger:** {_ch_badge(guild, tv_ch)}\n"
        f"{'🟢' if t_cat or t_log else '🟡'} **🎫 Ticket-System:** Kategorie {_ch_badge(guild, t_cat)} • Log {_ch_badge(guild, t_log)}\n"
        f"🟢 **🎰 Economy & Spiele:** Befehle {_ch_badge(guild, cmd_ch)} • Casino {_ch_badge(guild, gamb_ch)} • Angeln {_ch_badge(guild, fish_ch)}\n"
        f"🟢 **🎵 Musik + Autoplay v2.0:** Aktiv • **📊 Stream-Reviews:** {_ch_badge(guild, rev_ch)} (`#{STREAMER_CHANNEL}`)"
    )

    embed = discord.Embed(
        title=f"🎛️ Modcheck Feature-Status & Setup-Center – {guild.name}",
        description=(
            "Hier siehst du live, welche Module aktiv sind (`🟢`), noch keinen Kanal haben (`⚪`) oder pausieren (`🔴`).\n"
            "👉 **Wähle unten im Menü einfach ein Modul aus**, um Kanäle, Kategorien oder Rollen direkt per Klick zuzuweisen — **ganz ohne `.env`!**"
        ),
        color=COLOR_GOLD,
    )
    embed.add_field(name="🎂 Geburtstage & 🔢 Counting", value=f"{b_line}\n{c_line}", inline=False)
    embed.add_field(name="📈 Leveling, 🎭 Rollen & 👋 Welcome", value=f"{l_line}\n{r_line}\n{w_line}", inline=False)
    embed.add_field(name="🛡️ Moderation, 🤖 AutoMod & 📋 Logs", value=m_line, inline=False)
    embed.add_field(name="🎉 Community-Suite", value=com_line, inline=False)
    embed.add_field(name="⚡ Modcheck Core (Voice, Tickets, Casino, Musik & Stream)", value=core_line, inline=False)
    embed.set_footer(text="Änderungen werden sofort in den Modul-Datendateien gespeichert — kein Neustart nötig!")
    return embed


# ── Modul-Detail-Konfiguratoren (ChannelSelect / RoleSelect / Toggles) ───


MODULE_SPECS: dict[str, dict[str, Any]] = {
    "birthdays": {
        "title": "🎂 Geburtstags-System einrichten",
        "desc": "Wähle den Kanal für automatische Glückwünsche und die 24h-Geburtstags-Rolle.",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "📢 Glückwunsch-Kanal auswählen …",
        "role_placeholder": "👑 24h-Geburtstags-Rolle auswählen …",
        "has_toggle": True,
    },
    "counting": {
        "title": "🔢 Counting-Spiel einrichten",
        "desc": "Wähle den Kanal, in dem gemeinsam ab `1` aufwärts gezählt wird (Fehler = Reset auf `0`).",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "🔢 Counting-Kanal auswählen …",
        "has_toggle": True,
    },
    "leveling": {
        "title": "📈 Level- & XP-System (MEE6) einrichten",
        "desc": "Wähle den Kanal für Level-Up-Nachrichten (oder lasse ihn frei für aktuellen Chat). Tipp: `/levels import-mee6` übernimmt alle MEE6-Level!",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "⬆️ Level-Up-Kanal auswählen …",
        "has_toggle": True,
    },
    "welcome": {
        "title": "👋 Begrüßung & Boost-Nachrichten",
        "desc": "Wähle den Kanal für Willkommens-Embeds & Server-Boost-Belohnungen.",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "👋 Willkommens-Kanal auswählen …",
        "has_toggle": True,
    },
    "goodbye": {
        "title": "🚪 Goodbye / Verlassens-Nachrichten",
        "desc": "Wähle den Kanal, in dem angezeigt wird, wenn jemand den Server verlässt.",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "🚪 Goodbye-Kanal auswählen …",
        "has_toggle": True,
    },
    "logs_all": {
        "title": "📋 Server- & Moderations-Logs (Sammel-Kanal)",
        "desc": "Setzt den Haupt-Log-Kanal für gelöschte/bearbeitete Nachrichten, Beitritte, Voice & Moderation. (Einzelne Kanäle je Typ sind zusätzlich per `/logs set-channel` möglich.)",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "📋 Haupt-Log-Kanal auswählen …",
        "has_toggle": False,
    },
    "automod": {
        "title": "🤖 AutoMod-Schutz konfigurieren",
        "desc": "Schalte den AutoMod-Schutz (Anti-Spam, Anti-Mass-Mention, Anti-Invite, Bad-Words) direkt scharf oder aus.",
        "has_toggle": True,
    },
    "autorole": {
        "title": "🎭 Auto-Role & Sticky-Roles",
        "desc": "Wähle die Rolle, die jedes neue Mitglied automatisch beim Beitritt erhält, und schalte Sticky-Roles an/aus.",
        "role_placeholder": "🎭 Automatische Beitritts-Rolle auswählen …",
        "has_toggle": True,
    },
    "suggestions": {
        "title": "💡 Vorschlags-Kanal (`/suggest`)",
        "desc": "Wähle den Kanal, in dem Community-Vorschläge mit 👍/👎 Abstimmung landen.",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "💡 Vorschlags-Kanal auswählen …",
        "has_toggle": True,
    },
    "starboard": {
        "title": "⭐ Starboard einrichten",
        "desc": "Wähle den Hall-of-Fame-Kanal für Nachrichten ab 3× ⭐ Reaktionen.",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "⭐ Starboard-Kanal auswählen …",
        "has_toggle": True,
    },
    "temp_voice": {
        "title": "🎙️ Temp-Voice (Join-to-Create) einrichten",
        "desc": "Wähle den Sprachkanal, dem User beitreten, um automatisch ihren eigenen Temp-Channel zu erstellen!",
        "channel_types": [discord.ChannelType.voice],
        "channel_placeholder": "➕ Join-to-Create Sprachkanal auswählen …",
        "extra_category_placeholder": "📁 Optional: Ziel-Kategorie für Temp-Channels …",
        "has_toggle": False,
    },
    "tickets": {
        "title": "🎫 Ticket-System Kanäle",
        "desc": "Wähle den Log-Kanal für Ticket-Transkripte und die Kategorie, in der neue Tickets geöffnet werden.",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "📋 Ticket-Log-Kanal auswählen …",
        "extra_category_placeholder": "📁 Kategorie für offene Tickets auswählen …",
        "has_toggle": False,
    },
    "economy_channels": {
        "title": "🎰 Casino-, Angel- & Bot-Befehls-Kanal",
        "desc": "Lege fest, in welchem Kanal Casino/Gambling (`/slots`, `/blackjack`) genutzt werden darf (oder drücke Zurücksetzen für freie Kanalwahl).",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "🎰 Gambling-/Casino-Kanal auswählen …",
        "has_clear_channel": True,
        "has_toggle": False,
    },
    "fishing_channel": {
        "title": "🎣 Angel-Kanal (`/fish`) & Review-Kanal",
        "desc": "Wähle den Kanal für `/fish` (oder leer lassen = überall erlaubt).",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "🎣 Angel-Kanal auswählen …",
        "has_clear_channel": True,
        "has_toggle": False,
    },
    "reviews": {
        "title": "📊 Monats- & Jahres-Review-Kanal",
        "desc": "Wähle den Kanal für automatische Monats- & Jahresrückblicke.",
        "channel_types": [discord.ChannelType.text],
        "channel_placeholder": "📊 Review-Kanal auswählen …",
        "has_toggle": False,
    },
    "staff_roles": {
        "title": "👮 Admin- & Moderator-Rolle festlegen",
        "desc": "Wähle die Server-Moderator-Rolle für `/mod`, `/giveaway` & Ticket-Support (Admin-Rechte haben zusätzlich alle Discord-Administratoren).",
        "role_placeholder": "👮 Moderator-Rolle auswählen …",
        "extra_admin_role": True,
        "has_toggle": False,
    },
}


class ModuleSelectDropdown(discord.ui.Select):
    """Haupt-Dropdown zum Auswählen eines beliebigen Features im `/feature-status` Center."""

    def __init__(self, current_module: str | None = None) -> None:
        options = [
            discord.SelectOption(label="🎂 Geburtstage (Kanal & 24h-Rolle)", value="birthdays", emoji="🎂"),
            discord.SelectOption(label="🔢 Counting-Spiel (Zähl-Kanal)", value="counting", emoji="🔢"),
            discord.SelectOption(label="📈 Level- & XP-System (MEE6)", value="leveling", emoji="📈"),
            discord.SelectOption(label="👋 Willkommen & Boost-Alerts", value="welcome", emoji="👋"),
            discord.SelectOption(label="🚪 Goodbye / Verlassen-Kanal", value="goodbye", emoji="🚪"),
            discord.SelectOption(label="📋 Audit- & Mod-Logs Kanal", value="logs_all", emoji="📋"),
            discord.SelectOption(label="🤖 AutoMod-Schutz (An/Aus & Filter)", value="automod", emoji="🤖"),
            discord.SelectOption(label="🎭 Auto-Role & Sticky-Roles", value="autorole", emoji="🎭"),
            discord.SelectOption(label="💡 Vorschläge (/suggest Kanal)", value="suggestions", emoji="💡"),
            discord.SelectOption(label="⭐ Starboard (Hall of Fame)", value="starboard", emoji="⭐"),
            discord.SelectOption(label="🎙️ Temp-Voice (Join-to-Create Kanal)", value="temp_voice", emoji="🎙️"),
            discord.SelectOption(label="🎫 Ticket-System (Kategorie & Log)", value="tickets", emoji="🎫"),
            discord.SelectOption(label="🎰 Casino / Gambling-Kanal", value="economy_channels", emoji="🎰"),
            discord.SelectOption(label="🎣 Angel-Kanal (/fish)", value="fishing_channel", emoji="🎣"),
            discord.SelectOption(label="📊 Monats-/Jahres-Review Kanal", value="reviews", emoji="📊"),
            discord.SelectOption(label="👮 Admin- & Moderator-Rollen", value="staff_roles", emoji="👮"),
        ]
        for opt in options:
            if opt.value == current_module:
                opt.default = True
        super().__init__(
            placeholder="⚙️ Modul zum Einrichten / Ändern auswählen …",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        mod_key = self.values[0]
        new_view = FeatureSetupView(interaction.guild, interaction.user.id, module_key=mod_key)
        await interaction.response.edit_message(
            embed=new_view.build_current_embed(),
            view=new_view,
        )


class DynamicChannelPicker(discord.ui.ChannelSelect):
    def __init__(
        self,
        parent_view: FeatureSetupView,
        channel_types: list[discord.ChannelType],
        placeholder: str,
        slot: str = "primary",
        row: int = 1,
    ) -> None:
        super().__init__(
            channel_types=channel_types,
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            row=row,
        )
        self.parent_view = parent_view
        self.slot = slot

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        picked = self.values[0]
        note = self.parent_view.apply_channel(interaction.guild, picked.id, self.slot)
        self.parent_view.last_notice = note
        await interaction.response.edit_message(
            embed=self.parent_view.build_current_embed(),
            view=self.parent_view,
        )


class DynamicRolePicker(discord.ui.RoleSelect):
    def __init__(
        self, parent_view: FeatureSetupView, placeholder: str, slot: str = "primary", row: int = 2
    ) -> None:
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=row)
        self.parent_view = parent_view
        self.slot = slot

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        picked = self.values[0]
        note = self.parent_view.apply_role(interaction.guild, picked.id, self.slot)
        self.parent_view.last_notice = note
        await interaction.response.edit_message(
            embed=self.parent_view.build_current_embed(),
            view=self.parent_view,
        )


class FeatureSetupView(discord.ui.View):
    """Interaktives Setup-Center für `/feature-status`."""

    def __init__(
        self, guild: discord.Guild, author_id: int, module_key: str | None = None
    ) -> None:
        super().__init__(timeout=600)
        self.guild = guild
        self.author_id = author_id
        self.module_key = module_key
        self.last_notice: str | None = None

        self.add_item(ModuleSelectDropdown(current_module=module_key))

        if module_key and module_key in MODULE_SPECS:
            spec = MODULE_SPECS[module_key]
            next_row = 1
            if "channel_placeholder" in spec:
                self.add_item(
                    DynamicChannelPicker(
                        self,
                        spec.get("channel_types", [discord.ChannelType.text]),
                        spec["channel_placeholder"],
                        slot="primary",
                        row=next_row,
                    )
                )
                next_row += 1
            if "extra_category_placeholder" in spec:
                self.add_item(
                    DynamicChannelPicker(
                        self,
                        [discord.ChannelType.category],
                        spec["extra_category_placeholder"],
                        slot="category",
                        row=next_row,
                    )
                )
                next_row += 1
            if "role_placeholder" in spec:
                self.add_item(
                    DynamicRolePicker(self, spec["role_placeholder"], slot="primary", row=next_row)
                )
                next_row += 1
            if spec.get("extra_admin_role"):
                self.add_item(
                    DynamicRolePicker(
                        self, "👑 Admin-Rolle auswählen …", slot="admin", row=next_row
                    )
                )
                next_row += 1

            # Action Row mit Toggle / Zurück zur Gesamtübersicht
            if spec.get("has_toggle"):
                btn_tog = discord.ui.Button(
                    label="🟢 Aktivieren / 🔴 Deaktivieren",
                    style=discord.ButtonStyle.primary,
                    row=4,
                )
                btn_tog.callback = self._on_toggle
                self.add_item(btn_tog)

            if module_key == "automod":
                btn_preset = discord.ui.Button(
                    label="🛡️ Empfohlene Filter aktivieren",
                    style=discord.ButtonStyle.success,
                    row=4,
                )
                btn_preset.callback = self._on_automod_preset
                self.add_item(btn_preset)

            if spec.get("has_clear_channel"):
                btn_clr = discord.ui.Button(
                    label="🔓 Kanal-Bindung aufheben (überall erlauben)",
                    style=discord.ButtonStyle.secondary,
                    row=4,
                )
                btn_clr.callback = self._on_clear_channel
                self.add_item(btn_clr)

            btn_back = discord.ui.Button(
                label="📊 Zur Gesamt-Übersicht",
                style=discord.ButtonStyle.secondary,
                row=4,
            )
            btn_back.callback = self._on_back
            self.add_item(btn_back)
        else:
            btn_refresh = discord.ui.Button(
                label="🔄 Status aktualisieren",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            btn_refresh.callback = self._on_refresh
            self.add_item(btn_refresh)

    def build_current_embed(self) -> discord.Embed:
        base = build_feature_status_embed(self.guild)
        if not self.module_key or self.module_key not in MODULE_SPECS:
            return base
        spec = MODULE_SPECS[self.module_key]
        notice_block = f"\n\n✅ **{self.last_notice}**" if self.last_notice else ""
        base.description = (
            f"### ⚙️ Ausgewählt: {spec['title']}\n"
            f"{spec['desc']}{notice_block}\n"
            "──────────────────────────────"
        )
        base.color = COLOR_SUCCESS if self.last_notice else COLOR_INFO
        return base

    def apply_channel(self, guild: discord.Guild, ch_id: int, slot: str) -> str:
        mk = self.module_key
        if mk == "birthdays":
            birthdays_store.mutate(
                guild.id, lambda g: g["config"].update({"enabled": True, "channel_id": ch_id})
            )
            return f"Geburtstags-Glückwünsche gehen jetzt in <#{ch_id}>!"
        if mk == "counting":
            counting_store.mutate(
                guild.id, lambda g: g.update({"enabled": True, "channel_id": ch_id})
            )
            return f"Counting ist jetzt in <#{ch_id}> aktiv!"
        if mk == "leveling":
            levels_store.mutate(
                guild.id, lambda g: g["config"].update({"enabled": True, "announce_channel_id": ch_id, "announce_mode": "channel"})
            )
            return f"Level-Up-Nachrichten erscheinen jetzt in <#{ch_id}>!"
        if mk == "welcome":
            set_dynamic_channel_id(guild.id, "welcome_channel_id", ch_id)
            server_cfg_store.mutate(
                guild.id,
                lambda g: (
                    g["welcome"].update({"enabled": True, "channel_id": ch_id}),
                    g["boost"].update({"channel_id": ch_id}),
                ),
            )
            return f"Willkommens- & Boost-Nachrichten gehen jetzt in <#{ch_id}>!"
        if mk == "goodbye":
            server_cfg_store.mutate(
                guild.id, lambda g: g["goodbye"].update({"enabled": True, "channel_id": ch_id})
            )
            return f"Goodbye-Nachrichten gehen jetzt in <#{ch_id}>!"
        if mk == "logs_all":
            def _set_logs(g: dict[str, Any]) -> None:
                g.setdefault("channels", {})["log_channel_id"] = ch_id
                logs = g.setdefault("logs", {})
                for k in ("message", "member", "voice", "server", "mod"):
                    logs[k] = {"enabled": True, "channel_id": ch_id}

            server_cfg_store.mutate(guild.id, _set_logs)
            return f"Alle Audit- & Mod-Logs laufen jetzt in <#{ch_id}>!"
        if mk == "suggestions":
            community_store.mutate(
                guild.id, lambda g: g["suggestions"].update({"enabled": True, "channel_id": ch_id})
            )
            return f"Community-Vorschläge (`/suggest send`) landen jetzt in <#{ch_id}>!"
        if mk == "starboard":
            community_store.mutate(
                guild.id, lambda g: g["starboard"].update({"enabled": True, "channel_id": ch_id})
            )
            return f"Starboard ist jetzt in <#{ch_id}> aktiv!"
        if mk == "temp_voice":
            if slot == "category":
                set_dynamic_channel_id(guild.id, "temp_voice_category_id", ch_id)
                return f"Temp-Voice-Kategorie gesetzt auf <#{ch_id}>!"
            set_dynamic_channel_id(guild.id, "temp_voice_channel_id", ch_id)
            return f"Temp-Voice Join-to-Create Trigger ist jetzt <#{ch_id}>!"
        if mk == "tickets":
            if slot == "category":
                set_dynamic_channel_id(guild.id, "ticket_category_id", ch_id)
                return f"Neue Tickets werden jetzt in der Kategorie <#{ch_id}> erstellt!"
            set_dynamic_channel_id(guild.id, "ticket_log_channel_id", ch_id)
            return f"Ticket-Logs & Transkripte gehen jetzt in <#{ch_id}>!"
        if mk == "economy_channels":
            set_dynamic_channel_id(guild.id, "gambling_channel_id", ch_id)
            return f"Casino-/Gambling-Befehle sind jetzt auf <#{ch_id}> gesetzt!"
        if mk == "fishing_channel":
            set_dynamic_channel_id(guild.id, "fishing_channel_id", ch_id)
            return f"Angel-Kanal (`/fish`) auf <#{ch_id}> gesetzt!"
        if mk == "reviews":
            set_dynamic_channel_id(guild.id, "review_channel_id", ch_id)
            return f"Monats- & Jahresreviews gehen jetzt in <#{ch_id}>!"
        return "Kanal gespeichert."

    def apply_role(self, guild: discord.Guild, role_id: int, slot: str) -> str:
        mk = self.module_key
        if mk == "birthdays":
            birthdays_store.mutate(
                guild.id, lambda g: g["config"].update({"enabled": True, "role_id": role_id})
            )
            return f"24h-Geburtstags-Rolle gesetzt auf <@&{role_id}>!"
        if mk == "autorole":
            roles_store.mutate(
                guild.id,
                lambda g: g["autoroles"].update({"enabled": True, "humans": [role_id]}),
            )
            return f"Neue Mitglieder erhalten automatisch <@&{role_id}>!"
        if mk == "staff_roles":
            if slot == "admin":
                set_dynamic_role_id(guild.id, "admin_role_id", role_id)
                return f"Admin-Rolle gesetzt auf <@&{role_id}>!"
            set_dynamic_role_id(guild.id, "mod_role_id", role_id)
            return f"Moderator-Rolle gesetzt auf <@&{role_id}>!"
        return "Rolle gespeichert."

    async def _on_toggle(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id or not interaction.guild:
            return
        mk = self.module_key
        gid = interaction.guild.id
        new_state = True
        if mk == "birthdays":
            new_state = birthdays_store.mutate(
                gid,
                lambda g: (
                    g["config"].__setitem__("enabled", not g["config"].get("enabled", True)),
                    g["config"]["enabled"],
                )[1],
            )
        elif mk == "counting":
            new_state = counting_store.mutate(
                gid, lambda g: (g.__setitem__("enabled", not g.get("enabled")), g["enabled"])[1]
            )
        elif mk == "leveling":
            new_state = levels_store.mutate(
                gid,
                lambda g: (
                    g["config"].__setitem__("enabled", not g["config"].get("enabled", True)),
                    g["config"]["enabled"],
                )[1],
            )
        elif mk == "welcome":
            new_state = server_cfg_store.mutate(
                gid,
                lambda g: (
                    g["welcome"].__setitem__("enabled", not g["welcome"].get("enabled", True)),
                    g["welcome"]["enabled"],
                )[1],
            )
        elif mk == "goodbye":
            new_state = server_cfg_store.mutate(
                gid,
                lambda g: (
                    g["goodbye"].__setitem__("enabled", not g["goodbye"].get("enabled")),
                    g["goodbye"]["enabled"],
                )[1],
            )
        elif mk == "automod":
            new_state = moderation_store.mutate(
                gid,
                lambda g: (
                    g["automod"].__setitem__("enabled", not g["automod"].get("enabled")),
                    g["automod"]["enabled"],
                )[1],
            )
        elif mk == "autorole":
            new_state = roles_store.mutate(
                gid,
                lambda g: (
                    g["sticky_roles"].__setitem__("enabled", not g["sticky_roles"].get("enabled")),
                    g["sticky_roles"]["enabled"],
                )[1],
            )
            self.last_notice = f"Sticky-Roles sind jetzt {'AKTIV' if new_state else 'DEAKTIVIERT'}!"
            await interaction.response.edit_message(embed=self.build_current_embed(), view=self)
            return
        elif mk == "suggestions":
            new_state = community_store.mutate(
                gid,
                lambda g: (
                    g["suggestions"].__setitem__("enabled", not g["suggestions"].get("enabled")),
                    g["suggestions"]["enabled"],
                )[1],
            )
        elif mk == "starboard":
            new_state = community_store.mutate(
                gid,
                lambda g: (
                    g["starboard"].__setitem__("enabled", not g["starboard"].get("enabled")),
                    g["starboard"]["enabled"],
                )[1],
            )
        self.last_notice = f"Modul ist jetzt {'🟢 AKTIVIERT' if new_state else '🔴 DEAKTIVIERT'}!"
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)

    async def _on_automod_preset(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id or not interaction.guild:
            return
        moderation_store.mutate(
            interaction.guild.id,
            lambda g: g["automod"].update(
                {
                    "enabled": True,
                    "anti_invite": True,
                    "anti_spam": True,
                    "anti_mass_mention": True,
                }
            ),
        )
        self.last_notice = "AutoMod aktiviert (Anti-Invite, Anti-Spam & Anti-Mass-Mention sind scharf)!"
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)

    async def _on_clear_channel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id or not interaction.guild:
            return
        if self.module_key == "economy_channels":
            set_dynamic_channel_id(interaction.guild.id, "gambling_channel_id", 0)
            self.last_notice = "Casino-Kanalbindung aufgehoben (überall nutzbar)."
        elif self.module_key == "fishing_channel":
            set_dynamic_channel_id(interaction.guild.id, "fishing_channel_id", 0)
            self.last_notice = "Angel-Kanalbindung aufgehoben (überall nutzbar)."
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id or not interaction.guild:
            return
        overview = FeatureSetupView(interaction.guild, self.author_id, module_key=None)
        await interaction.response.edit_message(
            embed=overview.build_current_embed(), view=overview
        )

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id or not interaction.guild:
            return
        await interaction.response.edit_message(
            embed=build_feature_status_embed(interaction.guild), view=self
        )


class FeatureStatusCog(commands.Cog, name="FeatureStatus"):
    """Stellt `/feature-status` bereit."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="feature-status",
        description="🎛️ Zeigt alle aktiven/inaktiven Bot-Features & lässt Kanäle/Rollen direkt per Klick setzen (Admins)",
    )
    async def cmd_feature_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Nur Administratoren können das Feature- & Setup-Center öffnen.",
                ephemeral=True,
            )
            return
        view = FeatureSetupView(interaction.guild, interaction.user.id, module_key=None)
        await interaction.response.send_message(
            embed=view.build_current_embed(), view=view, ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeatureStatusCog(bot))
