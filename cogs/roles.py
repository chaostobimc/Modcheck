"""Roles Cog (Carl-bot & ProBot Ersatz): Button- & Dropdown-Panels, Reaction-Roles, Auto-Role & Sticky-Roles."""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import is_user_admin
from bot_core.config import COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING, runtime_settings
from bot_core.server_store import roles_store


def _parse_hex_color(hex_str: str | None, default: int = COLOR_INFO) -> int:
    if not hex_str:
        return default
    cleaned = hex_str.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", cleaned):
        return int(cleaned, 16)
    return default


class PersistentRoleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"mc:rolebtn:(?P<panel_id>[a-zA-Z0-9]+):(?P<role_id>\d+)",
):
    """Neustart-sicherer Rollen-Button für beliebig viele Panels."""

    def __init__(
        self,
        panel_id: str,
        role_id: int,
        label: str = "Rolle",
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                emoji=emoji or None,
                style=style,
                custom_id=f"mc:rolebtn:{panel_id}:{role_id}",
            )
        )
        self.panel_id = panel_id
        self.role_id = role_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> PersistentRoleButton:
        btn = item if isinstance(item, discord.ui.Button) else None
        return cls(
            panel_id=match.group("panel_id"),
            role_id=int(match.group("role_id")),
            label=(btn.label if btn and btn.label else "Rolle"),
            emoji=(str(btn.emoji) if btn and btn.emoji else None),
            style=(btn.style if btn else discord.ButtonStyle.secondary),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        g = roles_store.get_guild(interaction.guild.id)
        panel = (g.get("panels") or {}).get(self.panel_id)
        if not panel:
            await interaction.response.send_message("⚠️ Dieses Rollen-Panel existiert nicht mehr.", ephemeral=True)
            return

        if self.role_id not in {int(it.get("role_id") or 0) for it in panel.get("items", [])}:
            await interaction.response.send_message("⚠️ Diese Rolle gehört nicht mehr zum Panel.", ephemeral=True)
            return
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message("❌ Diese Rolle existiert nicht mehr.", ephemeral=True)
            return

        mode = str(panel.get("mode") or "toggle")
        member = interaction.user
        has_role = role in member.roles

        try:
            if mode == "verify":
                if has_role:
                    await interaction.response.send_message(
                        f"ℹ️ Du besitzt {role.mention} bereits.", ephemeral=True
                    )
                    return
                await member.add_roles(role, reason=f"Role-Panel ({self.panel_id}) Verify")
                await interaction.response.send_message(f"✅ {role.mention} erhalten!", ephemeral=True)
                return

            if mode == "drop":
                if not has_role:
                    await interaction.response.send_message(
                        f"ℹ️ Du hast {role.mention} aktuell nicht.", ephemeral=True
                    )
                    return
                await member.remove_roles(role, reason=f"Role-Panel ({self.panel_id}) Drop")
                await interaction.response.send_message(f"➖ {role.mention} abgelegt.", ephemeral=True)
                return

            if has_role:
                await member.remove_roles(role, reason=f"Role-Panel ({self.panel_id}) Toggle")
                await interaction.response.send_message(f"➖ {role.mention} entfernt.", ephemeral=True)
            else:
                removed_others: list[discord.Role] = []
                if mode == "unique":
                    panel_rids = {int(it.get("role_id") or 0) for it in (panel.get("items") or [])}
                    removed_others = [
                        r for r in member.roles if r.id in panel_rids and r.id != role.id
                    ]
                    if removed_others:
                        await member.remove_roles(*removed_others, reason=f"Role-Panel ({self.panel_id}) Unique")
                await member.add_roles(role, reason=f"Role-Panel ({self.panel_id}) Add")
                extra = (
                    f" *(ersetzt {', '.join(r.mention for r in removed_others)})*"
                    if removed_others
                    else ""
                )
                await interaction.response.send_message(f"✅ {role.mention} vergeben!{extra}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Mir fehlen die Rollen-Rechte (meine Bot-Rolle muss über der Ziel-Rolle stehen!).",
                ephemeral=True,
            )


class PersistentRoleSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"mc:rolesel:(?P<panel_id>[a-zA-Z0-9]+)",
):
    """Neustart-sicheres Dropdown-Menü für Rollen-Panels."""

    def __init__(self, panel_id: str, options: list[discord.SelectOption], max_values: int = 1) -> None:
        safe_opts = options or [discord.SelectOption(label="Noch keine Rollen hinterlegt", value="0")]
        super().__init__(
            discord.ui.Select(
                placeholder="🎭 Rollen auswählen …",
                min_values=0,
                max_values=max(1, min(len(safe_opts), max_values)),
                options=safe_opts[:25],
                custom_id=f"mc:rolesel:{panel_id}",
            )
        )
        self.panel_id = panel_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> PersistentRoleSelect:
        sel = item if isinstance(item, discord.ui.Select) else None
        opts = list(sel.options) if sel else []
        max_v = sel.max_values if sel else 1
        return cls(panel_id=match.group("panel_id"), options=opts, max_values=max_v)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        g = roles_store.get_guild(interaction.guild.id)
        panel = (g.get("panels") or {}).get(self.panel_id)
        if not panel:
            await interaction.response.send_message("⚠️ Dieses Rollen-Panel existiert nicht mehr.", ephemeral=True)
            return

        selected_ids = {int(v) for v in self.item.values if v.isdigit() and int(v) > 0}
        panel_ids = {int(it.get("role_id") or 0) for it in (panel.get("items") or []) if it.get("role_id")}
        mode = str(panel.get("mode") or "toggle")
        member = interaction.user

        to_add: list[discord.Role] = []
        to_remove: list[discord.Role] = []
        for rid in panel_ids:
            r_obj = interaction.guild.get_role(rid)
            if r_obj is None:
                continue
            if rid in selected_ids and r_obj not in member.roles:
                to_add.append(r_obj)
            elif rid not in selected_ids and r_obj in member.roles and mode != "verify":
                to_remove.append(r_obj)

        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason=f"Dropdown Role-Panel ({self.panel_id})")
            if to_add:
                await member.add_roles(*to_add, reason=f"Dropdown Role-Panel ({self.panel_id})")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Mir fehlen die Rollen-Rechte (Bot-Rolle muss über den Ziel-Rollen stehen).",
                ephemeral=True,
            )
            return

        parts: list[str] = []
        if to_add:
            parts.append("✅ Hinzugefügt: " + ", ".join(r.mention for r in to_add))
        if to_remove:
            parts.append("➖ Entfernt: " + ", ".join(r.mention for r in to_remove))
        if not parts:
            parts.append("ℹ️ Deine Rollen sind unverändert.")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)


def _build_panel_render(
    guild: discord.Guild, panel_id: str, panel: dict[str, Any]
) -> tuple[discord.Embed, discord.ui.View]:
    items = panel.get("items") or []
    mode_labels = {
        "toggle": "🔄 Frei wählbar (An/Aus)",
        "unique": "1️⃣ Exklusiv (nur 1 Rolle gleichzeitig)",
        "verify": "✅ Verifizierung (nur hinzufügen)",
        "drop": "➖ Nur ablegen",
    }
    desc = str(panel.get("description") or "Wähle unten deine gewünschten Rollen aus:")
    if items:
        role_lines = []
        for it in items:
            em = f"{it.get('emoji')} " if it.get("emoji") else "• "
            sub = f" — *{it.get('desc')}*" if it.get("desc") else ""
            role_lines.append(f"{em}<@&{it['role_id']}>{sub}")
        desc += "\n\n" + "\n".join(role_lines)

    embed = discord.Embed(
        title=str(panel.get("title") or "🎭 Rollen-Auswahl"),
        description=desc,
        color=int(panel.get("color") or COLOR_INFO),
    )
    embed.set_footer(
        text=f"Panel-ID: {panel_id} • {mode_labels.get(str(panel.get('mode', 'toggle')), 'Rollen-Panel')}"
    )

    view = discord.ui.View(timeout=None)
    ui_type = str(panel.get("ui_type") or "buttons")
    if ui_type == "select" and items:
        options: list[discord.SelectOption] = []
        for it in items[:25]:
            r_obj = guild.get_role(int(it["role_id"]))
            lbl = str(it.get("label") or (r_obj.name if r_obj else f"Rolle {it['role_id']}"))[:100]
            options.append(
                discord.SelectOption(
                    label=lbl,
                    value=str(it["role_id"]),
                    emoji=it.get("emoji") or None,
                    description=(str(it["desc"])[:100] if it.get("desc") else None),
                )
            )
        max_v = 1 if panel.get("mode") == "unique" else len(options)
        view.add_item(PersistentRoleSelect(panel_id, options, max_values=max_v))
    else:
        style_map = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
        }
        st = style_map.get(str(panel.get("button_style") or "secondary"), discord.ButtonStyle.secondary)
        for it in items[:25]:
            r_obj = guild.get_role(int(it["role_id"]))
            lbl = str(it.get("label") or (r_obj.name if r_obj else "Rolle"))[:80]
            view.add_item(
                PersistentRoleButton(
                    panel_id=panel_id,
                    role_id=int(it["role_id"]),
                    label=lbl,
                    emoji=it.get("emoji") or None,
                    style=st,
                )
            )
    return embed, view


class RolesGroup(app_commands.Group):
    """Slash-Command-Gruppe `/roles ...`."""

    def __init__(self, cog: RolesCog) -> None:
        super().__init__(
            name="roles",
            description="🎭 Self-Role-Panels, Reaction-Roles, Auto-Role & Sticky-Roles (Carl-bot Ersatz)",
        )
        self.cog = cog

    @app_commands.command(
        name="panel-create",
        description="⚙️ Erstellt ein neues Button- oder Dropdown-Rollenpanel (Admins)",
    )
    @app_commands.describe(
        titel="Überschrift des Rollen-Panels",
        beschreibung="Beschreibungstext im Panel",
        kanal="Ziel-Kanal (leer = aktueller Kanal)",
        modus="Verhalten beim Anklicken",
        typ="Buttons oder Dropdown-Menü",
        farbe="Hex-Farbe (z. B. #5865F2)",
    )
    @app_commands.choices(
        modus=[
            app_commands.Choice(name="Normal (An- & Abwählen)", value="toggle"),
            app_commands.Choice(name="Exklusiv / Unique (nur 1 Rolle gleichzeitig)", value="unique"),
            app_commands.Choice(name="Verifizierung (nur hinzufügen)", value="verify"),
            app_commands.Choice(name="Nur entfernen (Drop)", value="drop"),
        ],
        typ=[
            app_commands.Choice(name="Buttons (Klick-Buttons)", value="buttons"),
            app_commands.Choice(name="Dropdown-Auswahlmenü", value="select"),
        ],
    )
    async def cmd_panel_create(
        self,
        interaction: discord.Interaction,
        titel: str,
        beschreibung: str = "Klicke unten, um deine Rollen auszuwählen:",
        kanal: discord.TextChannel | None = None,
        modus: str = "toggle",
        typ: str = "buttons",
        farbe: str | None = None,
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        target_ch = kanal or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Bitte einen Textkanal angeben.", ephemeral=True)
            return

        panel_id = uuid.uuid4().hex[:8]
        panel_data: dict[str, Any] = {
            "channel_id": target_ch.id,
            "message_id": 0,
            "title": titel,
            "description": beschreibung.replace("\\n", "\n"),
            "mode": modus,
            "ui_type": typ,
            "color": _parse_hex_color(farbe, COLOR_INFO),
            "button_style": "success" if modus == "verify" else "secondary",
            "items": [],
        }
        embed, view = _build_panel_render(interaction.guild, panel_id, panel_data)
        msg = await target_ch.send(embed=embed, view=view)
        panel_data["message_id"] = msg.id

        roles_store.mutate(interaction.guild.id, lambda g: g["panels"].__setitem__(panel_id, panel_data))
        await interaction.response.send_message(
            f"✅ Rollen-Panel **`{panel_id}`** in {target_ch.mention} erstellt!\n"
            f"👉 Füge jetzt Rollen hinzu mit: `/roles panel-add-role panel_id:{panel_id} rolle:@Rolle`",
            ephemeral=True,
        )

    @app_commands.command(name="panel-add-role", description="➕ Fügt einem Rollen-Panel eine Rolle hinzu (Admins)")
    @app_commands.describe(
        panel_id="Die 8-stellige Panel-ID (steht im Footer des Panels oder unter /roles panel-list)",
        rolle="Die hinzuzufügende Rolle",
        label="Optionaler Button-/Menü-Text (leer = Rollenname)",
        emoji="Optionales Emoji (z. B. 🎮)",
        beschreibung="Kurze Zusatzinfo zur Rolle",
    )
    async def cmd_panel_add_role(
        self,
        interaction: discord.Interaction,
        panel_id: str,
        rolle: discord.Role,
        label: str | None = None,
        emoji: str | None = None,
        beschreibung: str | None = None,
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        pid = panel_id.strip()

        def _add(g: dict[str, Any]) -> dict[str, Any] | None:
            p = g["panels"].get(pid)
            if not p:
                return None
            items: list[dict[str, Any]] = p.setdefault("items", [])
            items = [x for x in items if int(x.get("role_id") or 0) != rolle.id]
            if len(items) >= 25:
                return p
            items.append(
                {
                    "role_id": rolle.id,
                    "label": (label or rolle.name)[:80],
                    "emoji": emoji.strip() if emoji else None,
                    "desc": beschreibung.strip()[:100] if beschreibung else None,
                }
            )
            p["items"] = items
            return dict(p)

        updated = roles_store.mutate(interaction.guild.id, _add)
        if not updated:
            await interaction.response.send_message(f"❌ Panel `{pid}` nicht gefunden.", ephemeral=True)
            return

        await self.cog.refresh_panel_message(interaction.guild, pid, updated)
        await interaction.response.send_message(
            f"✅ {rolle.mention} wurde zum Panel **`{pid}`** hinzugefügt!", ephemeral=True
        )

    @app_commands.command(name="panel-remove-role", description="➖ Entfernt eine Rolle aus einem Panel (Admins)")
    async def cmd_panel_remove_role(
        self, interaction: discord.Interaction, panel_id: str, rolle: discord.Role
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        pid = panel_id.strip()

        def _rem(g: dict[str, Any]) -> dict[str, Any] | None:
            p = g["panels"].get(pid)
            if not p:
                return None
            p["items"] = [x for x in (p.get("items") or []) if int(x.get("role_id") or 0) != rolle.id]
            return dict(p)

        updated = roles_store.mutate(interaction.guild.id, _rem)
        if not updated:
            await interaction.response.send_message(f"❌ Panel `{pid}` nicht gefunden.", ephemeral=True)
            return
        await self.cog.refresh_panel_message(interaction.guild, pid, updated)
        await interaction.response.send_message(
            f"🗑️ {rolle.mention} aus Panel **`{pid}`** entfernt.", ephemeral=True
        )

    @app_commands.command(name="panel-list", description="📋 Listet alle aktiven Rollen-Panels auf (Admins)")
    async def cmd_panel_list(self, interaction: discord.Interaction) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        g = roles_store.get_guild(interaction.guild.id)
        panels: dict[str, Any] = g.get("panels") or {}
        if not panels:
            await interaction.response.send_message("ℹ️ Noch keine Rollen-Panels vorhanden.", ephemeral=True)
            return
        lines = [
            f"• **`{pid}`** — **{p.get('title')}** (<#{p.get('channel_id')}> • `{len(p.get('items', []))}` Rollen • `{p.get('mode')}`)"
            for pid, p in panels.items()
        ]
        embed = discord.Embed(
            title="🎭 Aktive Rollen-Panels",
            description="\n".join(lines),
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="panel-delete", description="🗑️ Löscht ein Rollen-Panel (Admins)")
    async def cmd_panel_delete(self, interaction: discord.Interaction, panel_id: str) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        pid = panel_id.strip()
        removed = roles_store.mutate(interaction.guild.id, lambda g: g["panels"].pop(pid, None))
        if not removed:
            await interaction.response.send_message(f"❌ Panel `{pid}` nicht gefunden.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(int(removed.get("channel_id") or 0))
        if isinstance(ch, discord.TextChannel) and removed.get("message_id"):
            try:
                msg = await ch.fetch_message(int(removed["message_id"]))
                await msg.delete()
            except Exception:
                pass
        await interaction.response.send_message(f"🗑️ Panel **`{pid}`** gelöscht.", ephemeral=True)

    @app_commands.command(
        name="reaction-add",
        description="✨ Klassische Emoji-Reaction-Role auf beliebiger Nachricht hinzufügen (Carl-bot Stil)",
    )
    @app_commands.describe(
        nachrichten_id="ID der Ziel-Nachricht",
        emoji="Emoji für die Reaktion (z. B. ✅ oder 🎮)",
        rolle="Rolle, die vergeben werden soll",
        kanal="Kanal der Nachricht (leer = aktueller Kanal)",
        modus="toggle = An/Aus, verify = nur hinzufügen, unique = nur 1 Reaktion pro Nachricht",
    )
    @app_commands.choices(
        modus=[
            app_commands.Choice(name="Normal (Toggle)", value="toggle"),
            app_commands.Choice(name="Exklusiv (Unique)", value="unique"),
            app_commands.Choice(name="Verifizierung (nur hinzufügen)", value="verify"),
        ]
    )
    async def cmd_reaction_add(
        self,
        interaction: discord.Interaction,
        nachrichten_id: str,
        emoji: str,
        rolle: discord.Role,
        kanal: discord.TextChannel | None = None,
        modus: str = "toggle",
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        if not nachrichten_id.strip().isdigit():
            await interaction.response.send_message("❌ Ungültige Nachrichten-ID.", ephemeral=True)
            return
        target_ch = kanal or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Ungültiger Textkanal.", ephemeral=True)
            return
        try:
            msg = await target_ch.fetch_message(int(nachrichten_id.strip()))
            await msg.add_reaction(emoji.strip())
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Konnte Nachricht/Emoji nicht verarbeiten: `{e}`", ephemeral=True
            )
            return

        em_key = emoji.strip()

        def _save_rr(g: dict[str, Any]) -> None:
            rr = g["reaction_roles"].setdefault(
                str(msg.id), {"channel_id": target_ch.id, "mode": modus, "emojis": {}}
            )
            rr["mode"] = modus
            rr["emojis"][em_key] = rolle.id

        roles_store.mutate(interaction.guild.id, _save_rr)
        await interaction.response.send_message(
            f"✅ Reaction-Role aktiv: {em_key} auf Nachricht `{msg.id}` → {rolle.mention} (`{modus}`).",
            ephemeral=True,
        )

    @app_commands.command(name="reaction-remove", description="🗑️ Entfernt eine Emoji-Reaction-Role (Admins)")
    async def cmd_reaction_remove(
        self, interaction: discord.Interaction, nachrichten_id: str, emoji: str
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _del(g: dict[str, Any]) -> bool:
            rr = g["reaction_roles"].get(nachrichten_id.strip())
            if not rr:
                return False
            existed = rr.get("emojis", {}).pop(emoji.strip(), None) is not None
            if not rr.get("emojis"):
                g["reaction_roles"].pop(nachrichten_id.strip(), None)
            return existed

        ok = roles_store.mutate(interaction.guild.id, _del)
        await interaction.response.send_message(
            "🗑️ Reaction-Role entfernt." if ok else "ℹ️ Keine passende Reaction-Role gefunden.",
            ephemeral=True,
        )

    @app_commands.command(
        name="autorole-human",
        description="👋 Schaltet eine automatische Beitritts-Rolle für neue Mitglieder an/aus (Admins)",
    )
    async def cmd_autorole_human(
        self, interaction: discord.Interaction, rolle: discord.Role
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _toggle(g: dict[str, Any]) -> bool:
            ar = g["autoroles"]
            ar["enabled"] = True
            humans: list[int] = ar.setdefault("humans", [])
            if rolle.id in humans:
                humans.remove(rolle.id)
                return False
            humans.append(rolle.id)
            return True

        added = roles_store.mutate(interaction.guild.id, _toggle)
        await interaction.response.send_message(
            f"{'✅ Hinzugefügt' if added else '➖ Entfernt'}: Auto-Role für Menschen → {rolle.mention}",
            ephemeral=True,
        )

    @app_commands.command(
        name="autorole-bot",
        description="🤖 Schaltet eine automatische Beitritts-Rolle für Bots an/aus (Admins)",
    )
    async def cmd_autorole_bot(self, interaction: discord.Interaction, rolle: discord.Role) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _toggle(g: dict[str, Any]) -> bool:
            ar = g["autoroles"]
            ar["enabled"] = True
            bots: list[int] = ar.setdefault("bots", [])
            if rolle.id in bots:
                bots.remove(rolle.id)
                return False
            bots.append(rolle.id)
            return True

        added = roles_store.mutate(interaction.guild.id, _toggle)
        await interaction.response.send_message(
            f"{'✅ Hinzugefügt' if added else '➖ Entfernt'}: Auto-Role für Bots → {rolle.mention}",
            ephemeral=True,
        )

    @app_commands.command(
        name="sticky",
        description="🧲 Sticky-Roles aktivieren/deaktivieren (stellt Rollen bei Wiedereintritt wieder her)",
    )
    async def cmd_sticky(self, interaction: discord.Interaction, aktiv: bool) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        roles_store.mutate(interaction.guild.id, lambda g: g["sticky_roles"].update({"enabled": bool(aktiv)}))
        await interaction.response.send_message(
            f"🧲 Sticky-Roles sind jetzt **{'✅ Aktiviert' if aktiv else '⏸️ Deaktiviert'}**.",
            ephemeral=True,
        )

    @app_commands.command(
        name="give-all",
        description="⚡ Vergibt eine Rolle an alle Server-Mitglieder (Carl-bot Mass-Role, Admins)",
    )
    async def cmd_give_all(
        self, interaction: discord.Interaction, rolle: discord.Role, nur_menschen: bool = True
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        targets = [
            m
            for m in interaction.guild.members
            if rolle not in m.roles and (not nur_menschen or not m.bot)
        ]
        await interaction.response.send_message(
            f"⏳ Vergebe {rolle.mention} an **{len(targets)}** Mitglieder …", ephemeral=True
        )
        ok = 0
        for mem in targets:
            try:
                await mem.add_roles(rolle, reason=f"Mass-Role von {interaction.user}")
                ok += 1
                await asyncio.sleep(0.25)
            except Exception:
                pass
        await interaction.followup.send(
            f"✅ Fertig! {rolle.mention} wurde an **{ok}/{len(targets)}** Mitglieder vergeben.",
            ephemeral=True,
        )


class RolesCog(commands.Cog, name="Roles"):
    """Verwaltet Self-Role-Panels, Reaction-Roles, Auto-Roles & Sticky-Roles."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.roles_group = RolesGroup(self)
        self.bot.tree.add_command(self.roles_group)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.roles_group.name, type=self.roles_group.type)

    async def refresh_panel_message(
        self, guild: discord.Guild, panel_id: str, panel: dict[str, Any]
    ) -> None:
        ch = guild.get_channel(int(panel.get("channel_id") or 0))
        mid = int(panel.get("message_id") or 0)
        if not isinstance(ch, discord.TextChannel) or not mid:
            return
        embed, view = _build_panel_render(guild, panel_id, panel)
        try:
            msg = await ch.fetch_message(mid)
            await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"⚠️ [Roles] Konnte Panel {panel_id} nicht aktualisieren: {e}")

    async def _assign_join_roles(self, member: discord.Member) -> None:
        g = roles_store.get_guild(member.guild.id)
        to_assign: list[discord.Role] = []

        ar = g.get("autoroles") or {}
        if ar.get("enabled"):
            rids = ar.get("bots", []) if member.bot else ar.get("humans", [])
            for rid in rids:
                r_obj = member.guild.get_role(int(rid))
                if r_obj and r_obj not in member.roles:
                    to_assign.append(r_obj)
        elif not member.bot:
            legacy_name = str(runtime_settings.get("auto_role_name") or "").strip()
            if legacy_name:
                r_legacy = discord.utils.get(member.guild.roles, name=legacy_name)
                if r_legacy and r_legacy not in member.roles:
                    to_assign.append(r_legacy)

        sticky = g.get("sticky_roles") or {}
        if sticky.get("enabled") and not member.bot:
            saved_rids = (sticky.get("saved") or {}).get(str(member.id), [])
            bot_top = member.guild.me.top_role if member.guild.me else None
            for rid in saved_rids:
                r_obj = member.guild.get_role(int(rid))
                if (
                    r_obj
                    and not r_obj.managed
                    and r_obj != member.guild.default_role
                    and (bot_top is None or r_obj < bot_top)
                    and r_obj not in to_assign
                    and r_obj not in member.roles
                ):
                    to_assign.append(r_obj)

        if to_assign:
            try:
                await member.add_roles(*to_assign, reason="Auto-Role / Sticky-Roles")
            except Exception as e:
                print(f"⚠️ [Roles] Auto/Sticky-Role Fehler für {member}: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        g = roles_store.get_guild(member.guild.id)
        wait_screening = bool((g.get("autoroles") or {}).get("wait_for_screening", True))
        if member.pending and wait_screening and not member.bot:
            return
        await self._assign_join_roles(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.pending and not after.pending:
            await self._assign_join_roles(after)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        g = roles_store.get_guild(member.guild.id)
        if not (g.get("sticky_roles") or {}).get("enabled"):
            return
        role_ids = [
            r.id
            for r in member.roles
            if r != member.guild.default_role and not r.managed
        ]
        roles_store.mutate(
            member.guild.id,
            lambda gd: gd["sticky_roles"].setdefault("saved", {}).__setitem__(str(member.id), role_ids),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or payload.user_id == (self.bot.user.id if self.bot.user else 0):
            return
        g = roles_store.get_guild(payload.guild_id)
        rr = (g.get("reaction_roles") or {}).get(str(payload.message_id))
        if not rr:
            return
        em_str = str(payload.emoji)
        rid = (rr.get("emojis") or {}).get(em_str)
        if not rid:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        role = guild.get_role(int(rid)) if guild else None
        if not guild or not member or member.bot or not role:
            return

        try:
            if rr.get("mode") == "unique":
                other_rids = {int(v) for k, v in (rr.get("emojis") or {}).items() if k != em_str}
                rem = [r for r in member.roles if r.id in other_rids]
                if rem:
                    await member.remove_roles(*rem, reason="Reaction-Role Unique")
            if role not in member.roles:
                await member.add_roles(role, reason="Reaction-Role Add")
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or payload.user_id == (self.bot.user.id if self.bot.user else 0):
            return
        g = roles_store.get_guild(payload.guild_id)
        rr = (g.get("reaction_roles") or {}).get(str(payload.message_id))
        if not rr or rr.get("mode") == "verify":
            return
        rid = (rr.get("emojis") or {}).get(str(payload.emoji))
        if not rid:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        role = guild.get_role(int(rid)) if guild else None
        if member and role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Reaction-Role Remove")
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolesCog(bot))
