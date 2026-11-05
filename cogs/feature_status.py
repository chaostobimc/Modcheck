"""Übersichtliches, serverbezogenes Setup-Center für Modcheck."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import is_user_admin
from bot_core.config import (
    ADMIN_ROLE_NAME, ALLOWED_CHANNEL_ID, CLIPS_CHANNEL_ID, COLOR_INFO,
    FISHING_CHANNEL_ID, GAMBLING_CHANNEL_ID, LOG_CHANNEL_ID,
    MESSAGE_LOG_CHANNEL_ID, MOD_ROLE_NAME, REVIEW_CHANNEL_ID,
    TEMP_VOICE_CATEGORY_ID, TEMP_VOICE_CHANNEL_ID, TICKET_CATEGORY_ID,
    TICKET_LOG_CHANNEL_ID, TICKET_SUPPORT_ROLE, TWITCH_LOG_CHANNEL_ID,
    USERINFO_CHANNEL_ID, WELCOME_CHANNEL_ID, runtime_settings,
)
from bot_core.server_store import (
    birthdays_store, community_store, counting_store, get_dynamic_channel_id,
    get_dynamic_role_id, levels_store, moderation_store, roles_store,
    server_cfg_store, set_dynamic_channel_id, set_dynamic_role_id,
)
from bot_core.storage import load_alerts_state, save_alerts_state
from cogs.welcome import STAT_COUNTERS, CounterSetupError


@dataclass(frozen=True)
class Field:
    label: str
    kind: str                         # text, voice, category, role
    store: str                        # dynamic_channel, dynamic_role, alerts, or a guild store
    path: tuple[str, ...]
    fallback: int = 0
    fallback_name: str = ""
    note: str = ""


def field(label: str, kind: str, store: str, *path: str, fallback: int = 0,
          fallback_name: str = "", note: str = "") -> Field:
    return Field(label, kind, store, path, fallback, fallback_name, note)


# Each option is connected to the same storage used by the corresponding feature.
MODULE_SPECS: dict[str, dict[str, Any]] = {
    "birthdays": {"title": "🎂 Geburtstage", "category": "community", "desc": "Geburtstagsnachrichten und Tagesrolle.", "fields": {
        "channel": field("Glückwunsch-Kanal", "text", "birthdays", "config", "channel_id"),
        "role": field("Geburtstagsrolle", "role", "birthdays", "config", "role_id")},
        "toggle": ("birthdays", "config", "enabled"), "commands": "`/birthday setup` für Nachricht und Coins · `/birthday set` für Einträge"},
    "counting": {"title": "🔢 Counting", "category": "community", "desc": "Gemeinsam abwechselnd zählen.", "fields": {
        "channel": field("Counting-Kanal", "text", "counting", "channel_id")},
        "toggle": ("counting", "enabled"), "commands": "`/counting setup` für Mathe, Zahlenpflicht und Startwert"},
    "leveling": {"title": "📈 Level & XP", "category": "community", "desc": "Chat- und Voice-XP mit Levelbelohnungen.", "fields": {
        "channel": field("Level-Up-Kanal", "text", "levels", "config", "announce_channel_id", note="Auswahl schaltet den festen Kanal ein; Zurücksetzen nutzt den aktuellen Chat.")},
        "toggle": ("levels", "config", "enabled"), "commands": "`/levels setup` für XP und Voice · `/levels reward-add` für Rollen · `/levels import-mee6`"},
    "suggestions": {"title": "💡 Vorschläge", "category": "community", "desc": "Kanal für Community-Vorschläge.", "fields": {
        "channel": field("Vorschlags-Kanal", "text", "community", "suggestions", "channel_id")},
        "toggle": ("community", "suggestions", "enabled"), "commands": "`/suggest setup` · `/suggest send`"},
    "starboard": {"title": "⭐ Starboard", "category": "community", "desc": "Beliebte Nachrichten sammeln.", "fields": {
        "channel": field("Starboard-Kanal", "text", "community", "starboard", "channel_id")},
        "toggle": ("community", "starboard", "enabled"), "commands": "`/starboard setup` für Emoji und Mindestanzahl"},
    "giveaways": {"title": "🎉 Giveaways", "category": "community", "desc": "Gewinnspiele werden einzeln im gewünschten Kanal erstellt.", "commands": "`/giveaway start` enthält Kanal, Dauer, Preis und Teilnahme-Rolle."},
    "polls": {"title": "📊 Umfragen", "category": "community", "desc": "Umfragen erscheinen im Kanal, in dem der Befehl verwendet wird.", "commands": "`/poll` enthält Frage, Optionen und Dauer."},
    "customcmd": {"title": "⚡ Eigene Befehle & AFK", "category": "community", "desc": "Individuelle Chat-Befehle und AFK-Status.", "commands": "`/customcmd add` · `/customcmd list` · `/afk`"},
    "welcome": {"title": "👋 Willkommen", "category": "server", "desc": "Nachricht für neue Mitglieder.", "fields": {
        "channel": field("Willkommens-Kanal", "text", "server", "welcome", "channel_id", fallback=WELCOME_CHANNEL_ID)},
        "toggle": ("server", "welcome", "enabled"), "commands": "`/welcome setup-join` für Text und Banner · `/welcome setup-dm` für DMs"},
    "goodbye": {"title": "🚪 Abschied", "category": "server", "desc": "Nachricht beim Verlassen des Servers.", "fields": {
        "channel": field("Abschieds-Kanal", "text", "server", "goodbye", "channel_id")},
        "toggle": ("server", "goodbye", "enabled"), "commands": "`/welcome setup-leave` für den Nachrichtentext"},
    "boost": {"title": "🚀 Server-Boost", "category": "server", "desc": "Boost-Nachricht und Coin-Belohnung.", "fields": {
        "channel": field("Boost-Kanal", "text", "server", "boost", "channel_id", note="Ohne eigenen Kanal wird der Willkommens-Kanal verwendet.")},
        "toggle": ("server", "boost", "enabled"), "commands": "`/welcome setup-boost` für Text und Coins"},
    "autorole": {"title": "🎭 Automatische Rollen", "category": "server", "desc": "Rollen für neue Mitglieder und Bots. Die Auswahl fügt eine Rolle hinzu; einzelne Rollen können über die Befehle entfernt werden.", "fields": {
        "primary": field("Auto-Rolle für Mitglieder", "role", "roles_list", "autoroles", "humans"),
        "bot": field("Auto-Rolle für Bots", "role", "roles_list", "autoroles", "bots")},
        "toggle": ("roles", "sticky_roles", "enabled"), "toggle_label": "Sticky-Rollen", "commands": "`/roles autorole-human` · `/roles autorole-bot` · `/roles sticky`"},
    "role_panels": {"title": "🎨 Rollenpanels", "category": "server", "desc": "Panels und Reaction-Rollen werden jeweils für eine konkrete Nachricht erstellt.", "commands": "`/roles panel-create` · `/roles panel-add-role` · `/roles reaction-add`"},
    "temp_voice": {"title": "🎙️ Temp-Voice", "category": "server", "desc": "Sprachkanal zum Erstellen und optionale Zielkategorie.", "fields": {
        "primary": field("Erstell-Sprachkanal", "voice", "dynamic_channel", "temp_voice_channel_id", fallback=TEMP_VOICE_CHANNEL_ID),
        "category": field("Zielkategorie", "category", "dynamic_channel", "temp_voice_category_id", fallback=TEMP_VOICE_CATEGORY_ID)}},
    "tickets": {"title": "🎫 Tickets", "category": "server", "desc": "Kategorie, Transkript-Kanal und Support-Rolle für neue Tickets.", "fields": {
        "primary": field("Ticket-Log-Kanal", "text", "dynamic_channel", "ticket_log_channel_id", fallback=TICKET_LOG_CHANNEL_ID),
        "category": field("Ticket-Kategorie", "category", "dynamic_channel", "ticket_category_id", fallback=TICKET_CATEGORY_ID),
        "support": field("Support-Rolle", "role", "dynamic_role", "ticket_support_role_id", fallback_name=TICKET_SUPPORT_ROLE, note="Gilt für neue Tickets; bestehende Kanalrechte werden nicht geändert.")},
        "commands": "`/ticketpanel` erstellt das Ticket-Panel."},
    "stats": {"title": "📊 Statistik-Zähler", "category": "server", "desc": "Erstelle und verwalte gesperrte Sprachkanäle für Discord-Mitglieder, Twitch-Follower und weitere Statistiken direkt hier im Menü.", "commands": "Alternativ: `/welcome counter-create` · `/welcome counter-remove`"},
    "logs_all": {"title": "📋 Audit-Logs", "category": "moderation", "desc": "Einzelne Log-Kanäle überschreiben den Standardkanal. Zurücksetzen eines Einzelkanals nutzt wieder den Standard.", "fields": {
        "primary": field("Standard-Log-Kanal", "text", "dynamic_channel", "log_channel_id", fallback=LOG_CHANNEL_ID),
        "message": field("Nachrichten-Log", "text", "log", "message", fallback=MESSAGE_LOG_CHANNEL_ID),
        "member": field("Mitglieder-Log", "text", "log", "member"),
        "voice": field("Voice-Log", "text", "log", "voice"),
        "mod": field("Moderations-Log", "text", "log", "mod")}, "commands": "`/logs ignore-channel` für Ausnahmen"},
    "automod": {"title": "🤖 AutoMod", "category": "moderation", "desc": "Automatischen Schutz ein- oder ausschalten.", "toggle": ("moderation", "automod", "enabled"), "commands": "`/automod toggle` für einzelne Filter · `/automod punishment` · `/automod status`"},
    "staff_roles": {"title": "👮 Teamrollen", "category": "moderation", "desc": "Admin- und Moderatorrolle für Bot-Befehle; Discord-Administratoren behalten Zugriff.", "fields": {
        "primary": field("Moderatorrolle", "role", "dynamic_role", "mod_role_id", fallback_name=MOD_ROLE_NAME),
        "admin": field("Adminrolle", "role", "dynamic_role", "admin_role_id", fallback_name=ADMIN_ROLE_NAME)}},
    "userinfo": {"title": "📁 User-Akten", "category": "moderation", "desc": "Optionales Archiv für `/userinfo`-Akten.", "fields": {
        "channel": field("Userinfo-Archiv", "text", "dynamic_channel", "userinfo_channel_id", fallback=USERINFO_CHANNEL_ID)}},
    "moderation": {"title": "🛡️ Moderation", "category": "moderation", "desc": "Verwarnungen, Timeouts, Bans und Fälle.", "commands": "`/mod warn` · `/mod escalation-set` · `/mod history`"},
    "economy_channels": {"title": "🎰 Casino", "category": "channels", "desc": "Optionaler Kanal für Casino-Befehle; ohne Kanal überall erlaubt.", "fields": {
        "primary": field("Casino-Kanal", "text", "dynamic_channel", "gambling_channel_id", fallback=GAMBLING_CHANNEL_ID)}},
    "fishing_channel": {"title": "🎣 Angeln & Jobs", "category": "channels", "desc": "Optionaler Kanal für Angeln und Jobs; ohne Kanal überall erlaubt.", "fields": {
        "primary": field("Angel- und Job-Kanal", "text", "dynamic_channel", "fishing_channel_id", fallback=FISHING_CHANNEL_ID)}},
    "commands_channel": {"title": "🤖 Bot-Befehle", "category": "channels", "desc": "Optionaler Kanal für Befehle mit Kanalbeschränkung; ohne Kanal überall erlaubt.", "fields": {
        "primary": field("Bot-Befehls-Kanal", "text", "dynamic_channel", "allowed_channel_id", fallback=ALLOWED_CHANNEL_ID)}},
    "music": {"title": "🎵 Musik", "category": "channels", "desc": "Der Player nutzt den Sprachkanal des Nutzers; es gibt keinen festen Musikkanal.", "commands": "`/play` · `/autoplay` · `/radio`"},
    "live_alert": {"title": "🟣 Twitch Live-Alert", "category": "twitch", "desc": "Kanal und Ping-Rolle für Live-Ankündigungen.", "fields": {
        "channel": field("Live-Alert-Kanal", "text", "alerts", "live_channel_id"),
        "role": field("Live-Ping-Rolle", "role", "alerts", "live_ping_role_id")},
        "commands": "`/setlivealert` für @everyone · `/testalert` zum Testen", "global_note": True},
    "youtube_alert": {"title": "🔴 YouTube-Alert", "category": "twitch", "desc": "Kanal und Ping-Rolle für YouTube-Ankündigungen.", "fields": {
        "channel": field("YouTube-Alert-Kanal", "text", "alerts", "yt_channel_id_discord"),
        "role": field("YouTube-Ping-Rolle", "role", "alerts", "yt_ping_role_id")},
        "commands": "Die YouTube-Quelle zusätzlich mit `/setyoutubealert` konfigurieren.", "global_note": True},
    "inactivity": {"title": "⚠️ Inaktivitäts-Warnungen", "category": "twitch", "desc": "Zielkanal für Warnungen an das Team.", "fields": {
        "channel": field("Warn-Kanal", "text", "alerts", "inactivity_channel_id", note="Ohne eigenen Kanal wird Review/Log als Fallback verwendet.")},
        "commands": "`/setinactivity` für Schwellwert und Aktivierung", "global_note": True},
    "reviews": {"title": "📅 Stream-Reviews", "category": "twitch", "desc": "Kanal für Monats- und Jahresberichte. Automatische Berichte nutzen den konfigurierten Hauptserver bzw. den ersten verfügbaren Review-Kanal; manuelle Berichte nutzen diesen Server.", "fields": {
        "primary": field("Review-Kanal", "text", "dynamic_channel", "review_channel_id", fallback=REVIEW_CHANNEL_ID)}},
    "clips": {"title": "🎬 Twitch-Clips", "category": "twitch", "desc": "Kanal für neue Clips des überwachten Twitch-Kanals.", "fields": {
        "channel": field("Clips-Kanal", "text", "dynamic_channel", "clips_channel_id", fallback=CLIPS_CHANNEL_ID)}},
    "twitch_log": {"title": "🟣 Twitch-Mod-Log", "category": "twitch", "desc": "Kanal für Moderationsaktionen aus dem Twitch-Chat.", "fields": {
        "channel": field("Twitch-Mod-Log-Kanal", "text", "dynamic_channel", "twitch_log_channel_id", fallback=TWITCH_LOG_CHANNEL_ID)}},
}

CATEGORIES = {
    "community": "🎉 Community",
    "server": "🏠 Server & Rollen",
    "moderation": "🛡️ Moderation & Logs",
    "channels": "🎮 Spiele & Befehle",
    "twitch": "📺 Twitch & YouTube",
}


def _store(name: str):
    return {"server": server_cfg_store, "birthdays": birthdays_store, "counting": counting_store,
            "levels": levels_store, "roles": roles_store, "roles_list": roles_store,
            "moderation": moderation_store, "community": community_store}[name]


def _nested_get(data: dict[str, Any], path: tuple[str, ...], default: Any = 0) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key, default)
    return data


def _nested_set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        data = data.setdefault(key, {})
    data[path[-1]] = value


def _field_value(guild: discord.Guild, f: Field) -> int:
    if f.store == "dynamic_channel":
        return get_dynamic_channel_id(guild.id, f.path[0], f.fallback)
    if f.store == "dynamic_role":
        value = get_dynamic_role_id(guild.id, f.path[0])
        if not value and f.fallback_name:
            role = discord.utils.get(guild.roles, name=f.fallback_name)
            return role.id if role else 0
        return value
    if f.store == "alerts":
        return int(_nested_get(load_alerts_state().get("config", {}), f.path) or 0)
    if f.store == "log":
        logs = server_cfg_store.get_guild(guild.id).get("logs", {})
        specific = logs.get(f.path[0]) or {}
        return int(specific.get("channel_id") or f.fallback or get_dynamic_channel_id(guild.id, "log_channel_id", LOG_CHANNEL_ID))
    value = _nested_get(_store(f.store).get_guild(guild.id), f.path)
    if f.store == "roles_list":
        return int(value[0]) if isinstance(value, list) and value else 0
    if f.store == "server" and f.path == ("welcome", "channel_id") and not value:
        return get_dynamic_channel_id(guild.id, "welcome_channel_id", f.fallback)
    if f.store == "server" and f.path == ("boost", "channel_id") and not value:
        return get_dynamic_channel_id(guild.id, "welcome_channel_id", WELCOME_CHANNEL_ID)
    return int(value or 0)


def _set_field(guild: discord.Guild, f: Field, value: int) -> None:
    if f.store == "dynamic_channel":
        set_dynamic_channel_id(guild.id, f.path[0], value)
    elif f.store == "dynamic_role":
        # Reset restores the configured role name fallback, just like the checks do.
        set_dynamic_role_id(guild.id, f.path[0], value or None)
    elif f.store == "alerts":
        state = load_alerts_state()
        _nested_set(state["config"], f.path, value)
        save_alerts_state(state)
    elif f.store == "log":
        def update(g: dict[str, Any]) -> None:
            logs = g.setdefault("logs", {})
            keys = ("mod", "server") if f.path[0] == "mod" else (f.path[0],)
            for key in keys:
                if value:
                    logs[key] = {"enabled": True, "channel_id": value}
                else:
                    logs.pop(key, None)
        server_cfg_store.mutate(guild.id, update)
    else:
        def update(g: dict[str, Any]) -> None:
            if f.store == "roles_list":
                existing = list(_nested_get(g, f.path, []))
                if value and value not in existing:
                    existing.append(value)
                _nested_set(g, f.path, existing if value else [])
                if value:
                    g["autoroles"]["enabled"] = True
            else:
                _nested_set(g, f.path, value)
            if f.store == "levels" and f.path[-1] == "announce_channel_id":
                g["config"]["announce_mode"] = "channel" if value else "current"
            if f.store == "server" and f.path == ("welcome", "channel_id"):
                g.setdefault("channels", {})["welcome_channel_id"] = value
        _store(f.store).mutate(guild.id, update)


def _display_field(guild: discord.Guild, f: Field) -> str:
    if f.store == "roles_list":
        autoroles = roles_store.get_guild(guild.id).get("autoroles") or {}
        values = autoroles.get(f.path[-1]) or []
        if autoroles.get("enabled") and values:
            shown = ", ".join(guild.get_role(int(rid)).mention if guild.get_role(int(rid)) else f"⚠️ `{rid}`" for rid in values[:10])
            return shown + (f" und {len(values) - 10} weitere" if len(values) > 10 else "")
        if not autoroles.get("enabled") and f.path[-1] == "humans":
            fallback = discord.utils.get(guild.roles, name=str(runtime_settings.get("auto_role_name") or ""))
            if fallback:
                return f"{fallback.mention} (Standardrolle)"
        return "Nicht gesetzt"
    value = _field_value(guild, f)
    if not value:
        return "Nicht gesetzt"
    obj = guild.get_role(value) if f.kind == "role" else guild.get_channel(value)
    return obj.mention if obj else f"⚠️ Nicht gefunden (`{value}`)"


def _toggle_state(guild: discord.Guild, spec: dict[str, Any]) -> bool | None:
    toggle = spec.get("toggle")
    if not toggle:
        return None
    return bool(_nested_get(_store(toggle[0]).get_guild(guild.id), tuple(toggle[1:]), False))


def build_feature_status_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title="🎛️ Modcheck · Setup-Center", color=COLOR_INFO,
        description="**1. Bereich wählen** → **2. Funktion wählen** → **3. Einstellung auswählen**\n"
                    "Kanäle und Rollen werden sofort gespeichert. Weitere Optionen stehen bei der jeweiligen Funktion.")
    for category, title in CATEGORIES.items():
        names = [spec["title"] for spec in MODULE_SPECS.values() if spec["category"] == category]
        embed.add_field(name=title, value=" · ".join(names), inline=False)
    embed.set_footer(text="Wähle unten einen Bereich. Nur du kannst dieses Menü bedienen.")
    return embed


class CategorySelect(discord.ui.Select):
    def __init__(self, owner: FeatureSetupView):
        super().__init__(placeholder="1. Bereich auswählen …", options=[discord.SelectOption(label=label, value=key) for key, label in CATEGORIES.items()], row=0)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        view = FeatureSetupView(interaction.guild, self.owner.author_id, category=self.values[0])
        await interaction.response.edit_message(embed=view.build_current_embed(), view=view)


class ModuleSelectDropdown(discord.ui.Select):
    def __init__(self, owner: FeatureSetupView):
        options = [discord.SelectOption(label=spec["title"], value=key, default=key == owner.module_key)
                   for key, spec in MODULE_SPECS.items() if spec["category"] == owner.category]
        super().__init__(placeholder="2. Funktion auswählen …", options=options, row=0)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        view = FeatureSetupView(interaction.guild, self.owner.author_id, module_key=self.values[0])
        await interaction.response.edit_message(embed=view.build_current_embed(), view=view)


class FieldSelect(discord.ui.Select):
    def __init__(self, owner: FeatureSetupView):
        fields = MODULE_SPECS[owner.module_key]["fields"]
        options = [discord.SelectOption(label=f.label, value=key, default=key == owner.field_key) for key, f in fields.items()]
        super().__init__(placeholder="3. Einstellung auswählen …", options=options, row=1)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        view = FeatureSetupView(interaction.guild, self.owner.author_id, module_key=self.owner.module_key, field_key=self.values[0])
        await interaction.response.edit_message(embed=view.build_current_embed(), view=view)


class DynamicChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, owner: FeatureSetupView, f: Field):
        types = {"text": discord.ChannelType.text, "voice": discord.ChannelType.voice, "category": discord.ChannelType.category}
        super().__init__(channel_types=[types[f.kind]], placeholder=f"{f.label} auswählen …", row=2)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner.apply_channel(interaction.guild, self.values[0].id, self.owner.field_key)
        self.owner.last_notice = "Einstellung gespeichert."
        await interaction.response.edit_message(embed=self.owner.build_current_embed(), view=self.owner)


class DynamicRolePicker(discord.ui.RoleSelect):
    def __init__(self, owner: FeatureSetupView, f: Field):
        super().__init__(placeholder=f"{f.label} auswählen …", row=2)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner.apply_role(interaction.guild, self.values[0].id, self.owner.field_key)
        self.owner.last_notice = "Einstellung gespeichert."
        await interaction.response.edit_message(embed=self.owner.build_current_embed(), view=self.owner)


class FeatureSetupView(discord.ui.View):
    def __init__(self, guild: discord.Guild, author_id: int, module_key: str | None = None,
                 *, category: str | None = None, field_key: str | None = None) -> None:
        super().__init__(timeout=600)
        self.guild, self.author_id, self.module_key = guild, author_id, module_key
        self.category = MODULE_SPECS[module_key]["category"] if module_key else category
        fields = MODULE_SPECS[module_key].get("fields", {}) if module_key else {}
        self.field_key = field_key or (next(iter(fields)) if len(fields) == 1 else None)
        field_key = self.field_key
        self.last_notice: str | None = None
        if not self.category:
            self.add_item(CategorySelect(self))
            self._button("Aktualisieren", self._refresh, 4)
            return
        self.add_item(ModuleSelectDropdown(self))
        if module_key:
            spec = MODULE_SPECS[module_key]
            fields = spec.get("fields", {})
            if fields:
                self.add_item(FieldSelect(self))
                if field_key in fields:
                    f = fields[field_key]
                    self.add_item(DynamicRolePicker(self, f) if f.kind == "role" else DynamicChannelPicker(self, f))
                    self._button("Alle Rollen entfernen" if f.store == "roles_list" else "Auswahl zurücksetzen", self._clear, 3)
            if module_key == "stats":
                self._button("➕ Zähler erstellen", self._stats_create, 3, discord.ButtonStyle.success)
                self._button("🗂️ Zähler verwalten", self._stats_manage, 3)
            state = _toggle_state(guild, spec)
            if state is not None:
                label = spec.get("toggle_label", "Funktion") + (" deaktivieren" if state else " aktivieren")
                self._button(label, self._toggle, 3, discord.ButtonStyle.danger if state else discord.ButtonStyle.success)
        if module_key:
            self._button("← Funktionen", self._back_modules, 4)
        self._button("← Bereiche", self._back, 4)

    def _button(self, label: str, callback, row: int, style=discord.ButtonStyle.secondary) -> None:
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild and interaction.guild.id == self.guild.id and interaction.user.id == self.author_id and is_user_admin(interaction.user):
            return True
        await interaction.response.send_message("❌ Dieses Setup-Menü ist nur für den Administrator zugänglich, der es geöffnet hat.", ephemeral=True)
        return False

    def build_current_embed(self) -> discord.Embed:
        if not self.category:
            return build_feature_status_embed(self.guild)
        if not self.module_key:
            embed = discord.Embed(title=CATEGORIES[self.category], color=COLOR_INFO,
                                  description="Wähle unten eine Funktion aus. Dort siehst du ihre Einstellungen und passenden Befehle.")
            lines = []
            for key, spec in MODULE_SPECS.items():
                if spec["category"] != self.category:
                    continue
                state = _toggle_state(self.guild, spec)
                if state is not None:
                    state_text = (spec.get("toggle_label", "Funktion") + (": an" if state else ": aus"))
                    marker = "🟢" if state else "⚪"
                elif key == "stats":
                    marker, state_text = "⚙️", "Im Menü einrichten"
                elif spec.get("fields"):
                    marker, state_text = "⚙️", "Einstellungen verfügbar"
                else:
                    marker, state_text = "ℹ️", "Einrichtung per Befehl"
                lines.append(f"{marker} **{spec['title']}** — {state_text}")
            embed.add_field(name="Funktionen & Status", value="\n".join(lines), inline=False)
            return embed
        spec = MODULE_SPECS[self.module_key]
        embed = discord.Embed(title=spec["title"], description=spec["desc"], color=COLOR_INFO)
        state = _toggle_state(self.guild, spec)
        if state is not None:
            embed.add_field(name=spec.get("toggle_label", "Status"), value="🟢 Aktiviert" if state else "⚪ Deaktiviert", inline=False)
        fields = spec.get("fields", {})
        if fields:
            lines = [f"**{f.label}:** {_display_field(self.guild, f)}" for f in fields.values()]
            embed.add_field(name="Aktuelle Einstellungen", value="\n".join(lines), inline=False)
            if self.field_key in fields:
                f = fields[self.field_key]
                text = f"Wähle unten {'eine Rolle' if f.kind == 'role' else 'einen Kanal'} für **{f.label}**."
                if f.note:
                    text += "\n" + f.note
                embed.add_field(name="Ausgewählte Einstellung", value=text, inline=False)
            else:
                embed.add_field(name="Ändern", value="Wähle zuerst unten eine Einstellung aus.", inline=False)
        if self.module_key == "stats":
            counters = server_cfg_store.get_guild(self.guild.id).get("stats_channels") or []
            lines = []
            for counter in counters[:15]:
                cid = int(counter.get("channel_id") or 0)
                channel = self.guild.get_channel(cid)
                kind = STAT_COUNTERS.get(counter.get("type"), ("Statistik", ""))[0]
                lines.append(f"• **{kind}** — {channel.mention if channel else f'⚠️ Kanal nicht gefunden (`{cid}`)'}")
            if len(counters) > 15:
                lines.append(f"… und {len(counters) - 15} weitere (unter Verwalten).")
            embed.add_field(name="Eingerichtete Zähler", value="\n".join(lines) or "Noch keine Zähler eingerichtet.", inline=False)
            embed.add_field(name="Erstellen", value="Auf **Zähler erstellen** klicken, Typ und optional eine Kategorie wählen. Der Kanal wird automatisch angelegt und alle 10 Minuten aktualisiert.", inline=False)
        if spec.get("global_note"):
            embed.add_field(name="Hinweis", value="Diese Twitch-/YouTube-Alert-Einstellungen gelten botweit, nicht pro Server.", inline=False)
        if spec.get("commands"):
            embed.add_field(name="Weitere Einstellungen", value=spec["commands"], inline=False)
        if self.last_notice:
            embed.add_field(name="✅ Gespeichert", value=self.last_notice, inline=False)
        embed.set_footer(text="Zurücksetzen entfernt die Auswahl bzw. stellt den Standard wieder her.")
        return embed

    def _apply(self, guild: discord.Guild, value: int, slot: str, kind: str) -> str:
        fields = MODULE_SPECS[self.module_key].get("fields", {})
        f = fields.get(slot)
        if not f and slot == "primary" and len(fields) == 1:
            f = next(iter(fields.values()))
        if not f or (kind == "role") != (f.kind == "role"):
            raise ValueError("Ungültige Einstellung")
        _set_field(guild, f, value)
        return "Einstellung gespeichert."

    def apply_channel(self, guild: discord.Guild, ch_id: int, slot: str) -> str:
        return self._apply(guild, ch_id, slot, "channel")

    def apply_role(self, guild: discord.Guild, role_id: int, slot: str) -> str:
        return self._apply(guild, role_id, slot, "role")

    async def _clear(self, interaction: discord.Interaction) -> None:
        f = MODULE_SPECS[self.module_key]["fields"][self.field_key]
        _set_field(interaction.guild, f, 0)
        self.last_notice = f"{f.label} zurückgesetzt. Die aktuelle Einstellung steht oben."
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        spec = MODULE_SPECS[self.module_key]
        toggle = spec["toggle"]
        def update(g: dict[str, Any]) -> bool:
            path = tuple(toggle[1:])
            value = not bool(_nested_get(g, path, False))
            _nested_set(g, path, value)
            return value
        state = _store(toggle[0]).mutate(interaction.guild.id, update)
        view = FeatureSetupView(interaction.guild, self.author_id, self.module_key, field_key=self.field_key)
        view.last_notice = f"{spec.get('toggle_label', 'Funktion')} {'aktiviert' if state else 'deaktiviert'}."
        await interaction.response.edit_message(embed=view.build_current_embed(), view=view)

    async def _stats_create(self, interaction: discord.Interaction) -> None:
        view = StatsCreateView(interaction.guild, self.author_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _stats_manage(self, interaction: discord.Interaction) -> None:
        view = StatsManageView(interaction.guild, self.author_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _back_modules(self, interaction: discord.Interaction) -> None:
        view = FeatureSetupView(interaction.guild, self.author_id, category=self.category)
        await interaction.response.edit_message(embed=view.build_current_embed(), view=view)

    async def _back(self, interaction: discord.Interaction) -> None:
        view = FeatureSetupView(interaction.guild, self.author_id)
        await interaction.response.edit_message(embed=view.build_current_embed(), view=view)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.build_current_embed(), view=self)


class StatsView(discord.ui.View):
    """Privates, erneut auf aktuelle Admin-Rechte prüfendes Statistik-Menü."""
    def __init__(self, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=600)
        self.guild, self.author_id = guild, author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.guild and interaction.guild.id == self.guild.id
                and interaction.user.id == self.author_id and is_user_admin(interaction.user)):
            return True
        await interaction.response.send_message("❌ Keine Berechtigung für dieses Menü.", ephemeral=True)
        return False

    def button(self, label: str, callback, row: int, style=discord.ButtonStyle.secondary, *, disabled=False):
        item = discord.ui.Button(label=label, row=row, style=style, disabled=disabled)
        item.callback = callback
        self.add_item(item)

    async def back(self, interaction: discord.Interaction) -> None:
        view = FeatureSetupView(interaction.guild, self.author_id, module_key="stats")
        await interaction.response.edit_message(embed=view.build_current_embed(), view=view)


class StatsMetricSelect(discord.ui.Select):
    def __init__(self, parent: StatsCreateView):
        options = [discord.SelectOption(label=label, value=key, default=key == parent.metric)
                   for key, (label, _) in STAT_COUNTERS.items()]
        super().__init__(placeholder="1. Statistik auswählen …", options=options, row=0)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        view = StatsCreateView(interaction.guild, self.parent_view.author_id,
                               metric=self.values[0], category_id=self.parent_view.category_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class StatsCategorySelect(discord.ui.ChannelSelect):
    def __init__(self, parent: StatsCreateView):
        super().__init__(placeholder="2. Kategorie wählen (optional) …", channel_types=[discord.ChannelType.category], row=1)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        view = StatsCreateView(interaction.guild, self.parent_view.author_id,
                               metric=self.parent_view.metric, category_id=self.values[0].id, template=self.parent_view.template)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class StatsCreateView(StatsView):
    def __init__(self, guild: discord.Guild, author_id: int, *, metric: str | None = None,
                 category_id: int | None = None, template: str | None = None) -> None:
        super().__init__(guild, author_id)
        self.metric, self.category_id, self.template = metric, category_id, template
        self.add_item(StatsMetricSelect(self))
        self.add_item(StatsCategorySelect(self))
        self.button("➕ Kanal erstellen", self.create, 2, discord.ButtonStyle.success, disabled=not metric)
        self.button("Keine Kategorie", self.clear_category, 2)
        self.button("✏️ Namen anpassen", self.edit_template, 2, disabled=not metric)
        self.button("← Statistik", self.back, 3)

    def build_embed(self) -> discord.Embed:
        category = self.guild.get_channel(self.category_id) if self.category_id else None
        metric = STAT_COUNTERS[self.metric][0] if self.metric in STAT_COUNTERS else "Noch nicht ausgewählt"
        category_text = category.mention if category else ("⚠️ Kategorie nicht gefunden" if self.category_id else "Keine (oberste Ebene)")
        embed = discord.Embed(title="➕ Statistik-Kanal erstellen", color=COLOR_INFO,
            description="Wähle eine Statistik und optional eine Kategorie. Mit **Kanal erstellen** legt der Bot einen gesperrten Sprachkanal an.")
        embed.add_field(name="Statistik", value=metric, inline=True)
        embed.add_field(name="Kategorie", value=category_text, inline=True)
        if self.metric in STAT_COUNTERS:
            template = self.template or STAT_COUNTERS[self.metric][1]
            embed.add_field(name="Beispiel für den Kanalnamen", value=discord.utils.escape_markdown(template.replace("{count}", "123")), inline=False)
        embed.add_field(name="Hinweise", value="Der Bot benötigt **Kanäle verwalten**. Twitch-Follower verwenden den konfigurierten Twitch-Kanal; Client-ID und gültiger Twitch-Zugang müssen eingerichtet sein.", inline=False)
        return embed

    async def clear_category(self, interaction: discord.Interaction) -> None:
        view = StatsCreateView(interaction.guild, self.author_id, metric=self.metric, template=self.template)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def edit_template(self, interaction: discord.Interaction) -> None:
        if self.metric not in STAT_COUNTERS:
            await interaction.response.send_message("❌ Bitte zuerst einen Statistik-Typ wählen.", ephemeral=True)
            return
        await interaction.response.send_modal(StatsNameModal(self))

    async def create(self, interaction: discord.Interaction) -> None:
        if self.metric not in STAT_COUNTERS:
            await interaction.response.send_message("❌ Bitte zuerst einen Statistik-Typ wählen.", ephemeral=True)
            return
        category = interaction.guild.get_channel(self.category_id) if self.category_id else None
        if self.category_id and not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("❌ Die gewählte Kategorie existiert nicht mehr. Bitte erneut auswählen.", ephemeral=True)
            return
        cog = interaction.client.get_cog("Welcome")
        if cog is None:
            await interaction.response.send_message("❌ Das Welcome-Modul ist nicht geladen.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            channel = await cog.create_stat_counter(interaction.guild, self.metric, category, self.template)
        except CounterSetupError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        view = FeatureSetupView(interaction.guild, self.author_id, module_key="stats")
        view.last_notice = f"Statistik-Kanal {channel.mention} wurde erstellt."
        await interaction.edit_original_response(embed=view.build_current_embed(), view=view)


class StatsNameModal(discord.ui.Modal, title="Statistik-Kanalname"):
    def __init__(self, parent: StatsCreateView) -> None:
        super().__init__()
        self.parent_view = parent
        self.template = discord.ui.TextInput(
            label="Kanalname mit {count}", placeholder="z. B. 👥 Mitglieder: {count}",
            default=parent.template or STAT_COUNTERS[parent.metric][1], max_length=100,
        )
        self.add_item(self.template)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (not interaction.guild or interaction.guild.id != self.parent_view.guild.id
                or interaction.user.id != self.parent_view.author_id or not is_user_admin(interaction.user)):
            await interaction.response.send_message("❌ Keine Berechtigung für dieses Menü.", ephemeral=True)
            return
        template = str(self.template.value).strip()
        if "{count}" not in template:
            await interaction.response.send_message("❌ Der Kanalname muss `{count}` enthalten.", ephemeral=True)
            return
        view = StatsCreateView(interaction.guild, self.parent_view.author_id,
                               metric=self.parent_view.metric, category_id=self.parent_view.category_id,
                               template=template)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


def _stats_records(guild: discord.Guild) -> list[dict[str, Any]]:
    records, seen = [], set()
    for counter in server_cfg_store.get_guild(guild.id).get("stats_channels") or []:
        if not isinstance(counter, dict) or not str(counter.get("channel_id") or "").isdigit():
            continue
        cid = int(counter["channel_id"])
        if cid and cid not in seen:
            records.append(counter)
            seen.add(cid)
    return records


class StatsChannelSelect(discord.ui.Select):
    def __init__(self, parent: StatsManageView, counters: list[dict[str, Any]]):
        options = []
        for counter in counters:
            cid = int(counter["channel_id"])
            ch = parent.guild.get_channel(cid)
            kind = STAT_COUNTERS.get(counter.get("type"), ("Statistik", ""))[0]
            name = getattr(ch, "name", "Kanal fehlt")
            options.append(discord.SelectOption(label=f"{kind} · {name}"[:100], value=str(cid),
                                                default=cid == parent.selected_id))
        super().__init__(placeholder="Zähler zum Verwalten auswählen …", options=options, row=0)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        view = StatsManageView(interaction.guild, self.parent_view.author_id,
                               page=self.parent_view.page, selected_id=int(self.values[0]))
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class StatsManageView(StatsView):
    def __init__(self, guild: discord.Guild, author_id: int, *, page: int = 0, selected_id: int | None = None):
        super().__init__(guild, author_id)
        self.counters = _stats_records(guild)
        self.page = min(max(0, page), max(0, (len(self.counters) - 1) // 25))
        self.selected_id = selected_id
        page_items = self.counters[self.page * 25:(self.page + 1) * 25]
        if page_items:
            self.add_item(StatsChannelSelect(self, page_items))
            self.button("🗑️ Ausgewählten Zähler entfernen", self.confirm, 1, discord.ButtonStyle.danger, disabled=not selected_id)
        if len(self.counters) > 25:
            self.button("← Vorherige", self.previous, 2, disabled=self.page == 0)
            self.button("Nächste →", self.next, 2, disabled=(self.page + 1) * 25 >= len(self.counters))
        self.button("← Statistik", self.back, 3)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🗂️ Statistik-Zähler verwalten", color=COLOR_INFO,
            description="Wähle einen verwalteten Zähler aus. Entfernen löscht nach Bestätigung auch den zugehörigen Discord-Kanal.")
        embed.add_field(name="Zähler", value=f"{len(self.counters)} eingerichtet" if self.counters else "Keine Zähler eingerichtet.", inline=False)
        if self.selected_id:
            channel = self.guild.get_channel(self.selected_id)
            embed.add_field(name="Ausgewählt", value=channel.mention if channel else f"Kanal nicht gefunden (`{self.selected_id}`)", inline=False)
        return embed

    async def previous(self, interaction: discord.Interaction) -> None:
        view = StatsManageView(interaction.guild, self.author_id, page=self.page - 1)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def next(self, interaction: discord.Interaction) -> None:
        view = StatsManageView(interaction.guild, self.author_id, page=self.page + 1)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if not self.selected_id or not any(int(c["channel_id"]) == self.selected_id for c in _stats_records(interaction.guild)):
            await interaction.response.send_message("❌ Der Zähler ist nicht mehr eingerichtet.", ephemeral=True)
            return
        view = StatsDeleteConfirmView(interaction.guild, self.author_id, self.selected_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class StatsDeleteConfirmView(StatsView):
    def __init__(self, guild: discord.Guild, author_id: int, channel_id: int):
        super().__init__(guild, author_id)
        self.channel_id = channel_id
        self.button("Ja, Kanal löschen", self.delete, 0, discord.ButtonStyle.danger)
        self.button("Abbrechen", self.cancel, 0)

    def build_embed(self) -> discord.Embed:
        ch = self.guild.get_channel(self.channel_id)
        name = ch.mention if ch else f"fehlender Kanal `{self.channel_id}`"
        return discord.Embed(title="⚠️ Statistik-Kanal entfernen?", description=f"**{name}** wird gelöscht und aus der Statistik entfernt. Diese Aktion kann nicht rückgängig gemacht werden.", color=discord.Color.orange())

    async def delete(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Welcome")
        if cog is None:
            await interaction.response.send_message("❌ Das Welcome-Modul ist nicht geladen.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await cog.remove_stat_counter(interaction.guild, self.channel_id)
        except CounterSetupError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        view = StatsManageView(interaction.guild, self.author_id)
        await interaction.edit_original_response(embed=view.build_embed(), view=view)
        await interaction.followup.send("✅ Statistik-Kanal entfernt.", ephemeral=True)

    async def cancel(self, interaction: discord.Interaction) -> None:
        view = StatsManageView(interaction.guild, self.author_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class FeatureStatusCog(commands.Cog, name="FeatureStatus"):
    @app_commands.command(name="feature-status", description="🎛️ Übersichtliches Setup-Center für Bot-Funktionen (Admins)")
    async def cmd_feature_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur Administratoren können das Setup-Center öffnen.", ephemeral=True)
            return
        view = FeatureSetupView(interaction.guild, interaction.user.id)
        await interaction.response.send_message(embed=view.build_current_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeatureStatusCog(bot))
