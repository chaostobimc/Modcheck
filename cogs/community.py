"""Community Cog: Giveaways, Umfragen (Polls), Vorschläge, Starboard, Custom-Commands & AFK."""
from __future__ import annotations

import datetime
import random
import re
import time
import uuid
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.checks import has_mod_role, is_user_admin, parse_time
from bot_core.config import COLOR_ERROR, COLOR_GOLD, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING
from bot_core.server_store import community_store
from bot_core.storage import update_balance


# ── Persistente UI-Elemente (Giveaway & Poll) ────────────────────────────


class PersistentGiveawayButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"mc:gway:(?P<gway_id>[a-zA-Z0-9]+)",
):
    """Neustart-sicherer Teilnahme-Button für Giveaways."""

    def __init__(self, gway_id: str, count: int = 0, ended: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label=f"Teilnehmen ({count})" if not ended else f"Beendet ({count})",
                emoji="🎉",
                style=discord.ButtonStyle.primary if not ended else discord.ButtonStyle.secondary,
                disabled=ended,
                custom_id=f"mc:gway:{gway_id}",
            )
        )
        self.gway_id = gway_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> PersistentGiveawayButton:
        btn = item if isinstance(item, discord.ui.Button) else None
        ended = bool(btn.disabled) if btn else False
        return cls(gway_id=match.group("gway_id"), count=0, ended=ended)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        g = community_store.get_guild(interaction.guild.id)
        gw = (g.get("giveaways") or {}).get(self.gway_id)
        if not gw or gw.get("ended"):
            await interaction.response.send_message("⚠️ Dieses Gewinnspiel ist bereits beendet.", ephemeral=True)
            return

        req_rid = int(gw.get("required_role_id") or 0)
        if req_rid and all(r.id != req_rid for r in interaction.user.roles):
            await interaction.response.send_message(
                f"❌ Für dieses Gewinnspiel benötigst du die Rolle <@&{req_rid}>!", ephemeral=True
            )
            return

        uid = interaction.user.id

        def _tog(gd: dict[str, Any]) -> tuple[bool, int]:
            rec = gd["giveaways"][self.gway_id]
            entries: list[int] = rec.setdefault("entries", [])
            if uid in entries:
                entries.remove(uid)
                return False, len(entries)
            entries.append(uid)
            return True, len(entries)

        joined, total = community_store.mutate(interaction.guild.id, _tog)
        view = discord.ui.View(timeout=None)
        view.add_item(PersistentGiveawayButton(self.gway_id, count=total, ended=False))
        try:
            await interaction.message.edit(view=view)
        except Exception:
            pass
        await interaction.response.send_message(
            "🎉 **Du nimmst jetzt am Gewinnspiel teil!** *(Nochmal klicken zum Austragen)*"
            if joined
            else "➖ Du hast deine Teilnahme zurückgezogen.",
            ephemeral=True,
        )


def _render_poll_embed(poll: dict[str, Any]) -> discord.Embed:
    options: list[str] = poll.get("options") or []
    votes: dict[str, list[int]] = poll.get("votes") or {}
    total_votes = sum(len(v) for v in votes.values())
    num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

    lines: list[str] = []
    for idx, opt_text in enumerate(options):
        cnt = len(votes.get(str(idx), []))
        pct = (cnt / total_votes * 100.0) if total_votes > 0 else 0.0
        bars = round(pct / 100.0 * 12)
        bar_str = "🟦" * bars + "⬛" * (12 - bars)
        lines.append(f"{num_emojis[idx]} **{opt_text}**\n{bar_str} `{cnt}` (`{pct:.1f}%`)")

    status_line = ""
    if poll.get("ended"):
        status_line = "\n\n🔒 **Umfrage beendet**"
    elif poll.get("ends_at"):
        status_line = f"\n\n⏳ Endet <t:{int(poll['ends_at'])}:R>"

    embed = discord.Embed(
        title=f"📊 {poll.get('question', 'Umfrage')}",
        description="\n\n".join(lines) + status_line,
        color=COLOR_INFO if not poll.get("ended") else COLOR_WARNING,
    )
    mode_txt = "Mehrfachauswahl" if poll.get("multi") else "Einzelstimme"
    embed.set_footer(text=f"{total_votes} Stimmen abgegeben • {mode_txt}")
    return embed


class PersistentPollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"mc:poll:(?P<poll_id>[a-zA-Z0-9]+):(?P<opt_idx>\d)",
):
    """Neustart-sicherer Abstimmungs-Button für `/poll`."""

    def __init__(self, poll_id: str, opt_idx: int, label: str, disabled: bool = False) -> None:
        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        super().__init__(
            discord.ui.Button(
                label=label[:75],
                emoji=num_emojis[opt_idx] if opt_idx < 5 else "🗳️",
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
                custom_id=f"mc:poll:{poll_id}:{opt_idx}",
            )
        )
        self.poll_id = poll_id
        self.opt_idx = opt_idx

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> PersistentPollButton:
        btn = item if isinstance(item, discord.ui.Button) else None
        return cls(
            poll_id=match.group("poll_id"),
            opt_idx=int(match.group("opt_idx")),
            label=(btn.label if btn and btn.label else "Option"),
            disabled=bool(btn.disabled) if btn else False,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        uid = interaction.user.id

        def _vote(gd: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
            p = (gd.get("polls") or {}).get(self.poll_id)
            if not p or p.get("ended") or (p.get("ends_at") and int(p["ends_at"]) <= time.time()):
                return None, "🔒 Diese Umfrage ist bereits geschlossen."
            if self.opt_idx >= len(p.get("options") or []):
                return None, "❌ Diese Option existiert nicht mehr."
            votes: dict[str, list[int]] = p.setdefault("votes", {})
            key = str(self.opt_idx)
            cur_list = votes.setdefault(key, [])
            if uid in cur_list:
                cur_list.remove(uid)
                return dict(p), "➖ Stimme zurückgezogen."
            if not p.get("multi"):
                for k, vlist in votes.items():
                    if k != key and uid in vlist:
                        vlist.remove(uid)
            cur_list.append(uid)
            return dict(p), f"✅ Stimme gezählt für **{p['options'][self.opt_idx]}**!"

        updated_poll, msg_txt = community_store.mutate(interaction.guild.id, _vote)
        if not updated_poll:
            await interaction.response.send_message(msg_txt, ephemeral=True)
            return

        await interaction.response.edit_message(embed=_render_poll_embed(updated_poll))
        await interaction.followup.send(msg_txt, ephemeral=True)


# ── Slash-Command-Gruppen ────────────────────────────────────────────────


class GiveawayGroup(app_commands.Group):
    """Slash-Command-Gruppe `/giveaway ...`."""

    def __init__(self, cog: CommunityCog) -> None:
        super().__init__(name="giveaway", description="🎉 Gewinnspiele starten, vorzeitig beenden & neu auslosen")
        self.cog = cog

    @app_commands.command(name="start", description="🎉 Startet ein neues Giveaway mit Klick-Button (Mods/Admins)")
    @app_commands.describe(
        preis="Was gibt es zu gewinnen? (z. B. Discord Nitro, Steam-Key …)",
        dauer="Laufzeit (z. B. 30m, 6h, 2d, 1w)",
        gewinner="Anzahl Gewinner (1–20)",
        kanal="Ziel-Kanal (leer = aktueller Kanal)",
        coin_preis="Optional: Automatischer Coin-Gewinn pro Gewinner",
        benoetigte_rolle="Optional: Nur Mitglieder mit dieser Rolle dürfen teilnehmen",
    )
    async def cmd_start(
        self,
        interaction: discord.Interaction,
        preis: str,
        dauer: str,
        gewinner: app_commands.Range[int, 1, 20] = 1,
        kanal: discord.TextChannel | None = None,
        coin_preis: app_commands.Range[int, 0, 10000000] = 0,
        benoetigte_rolle: discord.Role | None = None,
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        secs = parse_time(dauer)
        if not secs or secs < 15:
            await interaction.response.send_message(
                "❌ Ungültige Dauer (z. B. `10m`, `2h`, `3d`).", ephemeral=True
            )
            return
        target_ch = kanal or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Bitte einen Textkanal angeben.", ephemeral=True)
            return

        gway_id = uuid.uuid4().hex[:8]
        ends_at = int(time.time() + secs)
        desc_lines = [
            f"🎁 **Preis:** **{preis}**" + (f" (+ `{coin_preis:,} 🪙`)" if coin_preis > 0 else ""),
            f"🏆 **Gewinner:** `{gewinner}`",
            f"⏳ **Endet:** <t:{ends_at}:R> (<t:{ends_at}:f>)",
            f"👑 **Veranstalter:** {interaction.user.mention}",
        ]
        if benoetigte_rolle:
            desc_lines.append(f"🎭 **Voraussetzung:** {benoetigte_rolle.mention}")
        desc_lines.append("\n👉 Klicke unten auf **🎉 Teilnehmen**, um im Lostopf zu landen!")

        embed = discord.Embed(
            title="🎉 GEWINNSPIEL / GIVEAWAY 🎉",
            description="\n".join(desc_lines),
            color=COLOR_GOLD,
        )
        embed.set_footer(text=f"Giveaway-ID: {gway_id}")

        view = discord.ui.View(timeout=None)
        view.add_item(PersistentGiveawayButton(gway_id, count=0, ended=False))
        msg = await target_ch.send(embed=embed, view=view)

        record = {
            "id": gway_id,
            "channel_id": target_ch.id,
            "message_id": msg.id,
            "host_id": interaction.user.id,
            "prize": preis,
            "coin_prize": int(coin_preis),
            "winners_count": int(gewinner),
            "required_role_id": benoetigte_rolle.id if benoetigte_rolle else 0,
            "ends_at": ends_at,
            "ended": False,
            "entries": [],
            "winners": [],
        }
        community_store.mutate(
            interaction.guild.id, lambda g: g.setdefault("giveaways", {}).__setitem__(gway_id, record)
        )
        await interaction.response.send_message(
            f"✅ Giveaway **`{gway_id}`** in {target_ch.mention} gestartet!", ephemeral=True
        )

    @app_commands.command(name="end", description="⏹️ Beendet ein laufendes Giveaway sofort & lost aus (Mods/Admins)")
    async def cmd_end(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        ok = await self.cog.finish_giveaway(interaction.guild, giveaway_id.strip())
        await interaction.response.send_message(
            "✅ Giveaway wurde beendet und ausgelost!" if ok else "❌ Aktives Giveaway nicht gefunden.",
            ephemeral=True,
        )

    @app_commands.command(name="reroll", description="🎲 Lost für ein beendetes Giveaway einen neuen Gewinner aus")
    async def cmd_reroll(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        gid = giveaway_id.strip()
        g = community_store.get_guild(interaction.guild.id)
        gw = (g.get("giveaways") or {}).get(gid)
        if not gw or not gw.get("entries"):
            await interaction.response.send_message(
                "❌ Giveaway nicht gefunden oder keine Teilnehmer vorhanden.", ephemeral=True
            )
            return
        pool = [int(u) for u in gw["entries"] if int(u) not in (gw.get("winners") or [])] or [
            int(u) for u in gw["entries"]
        ]
        new_winner = random.choice(pool)
        ch = interaction.guild.get_channel(int(gw["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            await ch.send(
                f"🎲 **Reroll für `{gw['prize']}`!** Neuer Gewinner: <@{new_winner}>! Herzlichen Glückwunsch! 🎉"
            )
        await interaction.response.send_message(f"✅ Neuer Gewinner ausgelost: <@{new_winner}>", ephemeral=True)

    @app_commands.command(name="list", description="📋 Zeigt alle aktiven Giveaways auf dem Server")
    async def cmd_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        g = community_store.get_guild(interaction.guild.id)
        active = [
            gw for gw in (g.get("giveaways") or {}).values() if isinstance(gw, dict) and not gw.get("ended")
        ]
        if not active:
            await interaction.response.send_message("ℹ️ Aktuell laufen keine Gewinnspiele.", ephemeral=True)
            return
        lines = [
            f"• **`{gw['id']}`** — **{gw['prize']}** (<#{gw['channel_id']}> • `{len(gw.get('entries', []))}` Teiln. • endet <t:{gw['ends_at']}:R>)"
            for gw in active
        ]
        await interaction.response.send_message(
            embed=discord.Embed(title="🎉 Laufende Giveaways", description="\n".join(lines), color=COLOR_GOLD),
            ephemeral=True,
        )


class SuggestGroup(app_commands.Group):
    """Slash-Command-Gruppe `/suggest ...`."""

    def __init__(self) -> None:
        super().__init__(name="suggest", description="💡 Community-Vorschläge einreichen & bewerten")

    @app_commands.command(name="send", description="💡 Reiche einen Vorschlag für den Server ein")
    @app_commands.describe(vorschlag="Deine Idee oder dein Verbesserungsvorschlag")
    async def cmd_send(self, interaction: discord.Interaction, vorschlag: str) -> None:
        if not interaction.guild:
            return
        g = community_store.get_guild(interaction.guild.id)
        scfg = g.get("suggestions") or {}
        if not scfg.get("enabled") or not scfg.get("channel_id"):
            await interaction.response.send_message(
                "ℹ️ Das Vorschlags-System ist noch nicht eingerichtet (`/feature-status` oder `/suggest setup`).",
                ephemeral=True,
            )
            return
        ch = interaction.guild.get_channel(int(scfg["channel_id"]))
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Vorschlags-Kanal nicht gefunden.", ephemeral=True)
            return

        def _alloc(gd: dict[str, Any]) -> int:
            s = gd["suggestions"]
            nid = int(s.get("next_id") or 1)
            s["next_id"] = nid + 1
            return nid

        sug_id = community_store.mutate(interaction.guild.id, _alloc)
        embed = discord.Embed(
            title=f"💡 Vorschlag #{sug_id}",
            description=vorschlag,
            color=COLOR_INFO,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="📊 Status", value="⏳ Offen zur Abstimmung", inline=False)
        msg = await ch.send(embed=embed)
        try:
            await msg.add_reaction("👍")
            await msg.add_reaction("👎")
            if scfg.get("auto_thread", True):
                await msg.create_thread(name=f"💡 Vorschlag #{sug_id}", auto_archive_duration=1440)
        except Exception:
            pass

        community_store.mutate(
            interaction.guild.id,
            lambda gd: gd["suggestions"]
            .setdefault("items", {})
            .__setitem__(
                str(sug_id),
                {
                    "id": sug_id,
                    "author_id": interaction.user.id,
                    "channel_id": ch.id,
                    "message_id": msg.id,
                    "text": vorschlag,
                    "status": "open",
                },
            ),
        )
        await interaction.response.send_message(
            f"✅ Dein **Vorschlag #{sug_id}** wurde in {ch.mention} veröffentlicht!", ephemeral=True
        )

    @app_commands.command(name="setup", description="⚙️ Richtet den Vorschlags-Kanal ein (Admins)")
    async def cmd_setup(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel,
        auto_thread: bool = True,
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        community_store.mutate(
            interaction.guild.id,
            lambda g: g["suggestions"].update(
                {"enabled": True, "channel_id": kanal.id, "auto_thread": bool(auto_thread)}
            ),
        )
        await interaction.response.send_message(
            f"✅ Vorschläge sind jetzt in {kanal.mention} aktiv!", ephemeral=True
        )

    async def _resolve(
        self, interaction: discord.Interaction, nummer: int, approved: bool, kommentar: str | None
    ) -> None:
        if not interaction.guild or not has_mod_role(interaction.user):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        g = community_store.get_guild(interaction.guild.id)
        item = ((g.get("suggestions") or {}).get("items") or {}).get(str(nummer))
        if not item:
            await interaction.response.send_message(f"❌ Vorschlag `#{nummer}` nicht gefunden.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(int(item["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            try:
                msg = await ch.fetch_message(int(item["message_id"]))
                if msg.embeds:
                    emb = msg.embeds[0]
                    emb.color = COLOR_SUCCESS if approved else COLOR_ERROR
                    emb.clear_fields()
                    state_str = (
                        f"✅ **Angenommen** von {interaction.user.mention}"
                        if approved
                        else f"❌ **Abgelehnt** von {interaction.user.mention}"
                    )
                    if kommentar:
                        state_str += f"\n💬 *{kommentar}*"
                    emb.add_field(name="📊 Status", value=state_str, inline=False)
                    await msg.edit(embed=emb)
            except Exception:
                pass
        community_store.mutate(
            interaction.guild.id,
            lambda gd: gd["suggestions"]["items"][str(nummer)].__setitem__(
                "status", "approved" if approved else "denied"
            ),
        )
        await interaction.response.send_message(
            f"✅ Vorschlag **#{nummer}** markiert als {'Angenommen' if approved else 'Abgelehnt'}.",
            ephemeral=True,
        )

    @app_commands.command(name="approve", description="✅ Nimmt einen Community-Vorschlag an (Mods/Admins)")
    async def cmd_approve(
        self, interaction: discord.Interaction, nummer: int, kommentar: str | None = None
    ) -> None:
        await self._resolve(interaction, nummer, True, kommentar)

    @app_commands.command(name="deny", description="❌ Lehnt einen Community-Vorschlag ab (Mods/Admins)")
    async def cmd_deny(
        self, interaction: discord.Interaction, nummer: int, kommentar: str | None = None
    ) -> None:
        await self._resolve(interaction, nummer, False, kommentar)


class StarboardGroup(app_commands.Group):
    """Slash-Command-Gruppe `/starboard ...`."""

    def __init__(self) -> None:
        super().__init__(name="starboard", description="⭐ Starboard (Hall of Fame für beliebte Nachrichten)")

    @app_commands.command(name="setup", description="⚙️ Konfiguriert das Starboard (Carl-bot Ersatz, Admins)")
    @app_commands.describe(
        kanal="Starboard-Kanal",
        sterne="Benötigte Reaktionen (Standard: 3)",
        emoji="Reaktions-Emoji (Standard: ⭐)",
        selbst_sterne="Dürfen Autoren ihre eigene Nachricht sternen?",
    )
    async def cmd_setup(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel,
        sterne: app_commands.Range[int, 1, 50] = 3,
        emoji: str = "⭐",
        selbst_sterne: bool = False,
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        community_store.mutate(
            interaction.guild.id,
            lambda g: g["starboard"].update(
                {
                    "enabled": True,
                    "channel_id": kanal.id,
                    "threshold": int(sterne),
                    "emoji": emoji.strip(),
                    "self_star": bool(selbst_sterne),
                }
            ),
        )
        await interaction.response.send_message(
            f"⭐ Starboard in {kanal.mention} aktiviert (ab **{sterne}× {emoji.strip()}**)!",
            ephemeral=True,
        )

    @app_commands.command(name="disable", description="⏸️ Deaktiviert das Starboard (Admins)")
    async def cmd_disable(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        community_store.mutate(interaction.guild.id, lambda g: g["starboard"].update({"enabled": False}))
        await interaction.response.send_message("⏸️ Starboard deaktiviert.", ephemeral=True)


class CustomCmdGroup(app_commands.Group):
    """Slash-Command-Gruppe `/customcmd ...` (Carl-bot/MEE6 Custom Commands & Tags)."""

    def __init__(self) -> None:
        super().__init__(name="customcmd", description="⚡ Eigene !Befehle / Text-Tags erstellen & verwalten")

    @app_commands.command(name="add", description="➕ Erstellt oder überschreibt einen eigenen !<name> Befehl (Admins)")
    @app_commands.describe(
        name="Auslöser ohne ! (z. B. social -> reagiert auf !social)",
        antwort="Antworttext ({user}, {username}, {server}, {channel}, {membercount})",
        als_embed="Antwort als schickes Embed senden?",
    )
    async def cmd_add(
        self,
        interaction: discord.Interaction,
        name: str,
        antwort: str,
        als_embed: bool = False,
    ) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        trig = name.strip().lower().lstrip("!/")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", trig):
            await interaction.response.send_message(
                "❌ Name darf nur aus Buchstaben, Zahlen, `-` und `_` bestehen.", ephemeral=True
            )
            return
        community_store.mutate(
            interaction.guild.id,
            lambda g: g.setdefault("custom_commands", {}).__setitem__(
                trig,
                {"response": antwort.replace("\\n", "\n"), "embed": bool(als_embed), "uses": 0},
            ),
        )
        await interaction.response.send_message(
            f"✅ Custom-Command **`!{trig}`** gespeichert!", ephemeral=True
        )

    @app_commands.command(name="remove", description="🗑️ Löscht einen eigenen !<name> Befehl (Admins)")
    async def cmd_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild or not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        trig = name.strip().lower().lstrip("!/")
        ok = community_store.mutate(
            interaction.guild.id, lambda g: g.get("custom_commands", {}).pop(trig, None) is not None
        )
        await interaction.response.send_message(
            f"🗑️ `!{trig}` gelöscht." if ok else f"❌ `!{trig}` existiert nicht.", ephemeral=True
        )

    @app_commands.command(name="list", description="📋 Zeigt alle eigenen !<name> Server-Befehle")
    async def cmd_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        cmds: dict[str, Any] = community_store.get_guild(interaction.guild.id).get("custom_commands") or {}
        if not cmds:
            await interaction.response.send_message("ℹ️ Noch keine eigenen `!Befehle` angelegt.", ephemeral=True)
            return
        lines = [
            f"• **`!{k}`** ({int(v.get('uses', 0))}× genutzt)"
            for k, v in sorted(cmds.items())
            if isinstance(v, dict)
        ]
        await interaction.response.send_message(
            embed=discord.Embed(title="⚡ Eigene Server-Befehle", description="\n".join(lines), color=COLOR_INFO)
        )


# ── Haupt-Cog ────────────────────────────────────────────────────────────


class CommunityCog(commands.Cog, name="Community"):
    """Giveaways, Polls, Vorschläge, Starboard, Custom-Commands & AFK."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.gway_group = GiveawayGroup(self)
        self.suggest_group = SuggestGroup()
        self.star_group = StarboardGroup()
        self.cc_group = CustomCmdGroup()
        self.bot.tree.add_command(self.gway_group)
        self.bot.tree.add_command(self.suggest_group)
        self.bot.tree.add_command(self.star_group)
        self.bot.tree.add_command(self.cc_group)

    async def cog_load(self) -> None:
        self.community_watcher.start()

    async def cog_unload(self) -> None:
        self.community_watcher.cancel()
        for grp in (self.gway_group, self.suggest_group, self.star_group, self.cc_group):
            self.bot.tree.remove_command(grp.name, type=grp.type)

    # ── Top-Level /poll & /afk ────────────────────────────────────────────

    @app_commands.command(name="poll", description="📊 Startet eine interaktive Live-Umfrage mit Balkenanzeige")
    @app_commands.describe(
        frage="Die Abstimmungsfrage",
        option_1="Antwortmöglichkeit 1",
        option_2="Antwortmöglichkeit 2",
        option_3="Optional: Antwort 3",
        option_4="Optional: Antwort 4",
        option_5="Optional: Antwort 5",
        dauer="Optional: Automatischer Schluss nach z. B. 1h, 12h, 2d",
        mehrfachauswahl="Mehrere Stimmen pro User erlauben?",
    )
    async def cmd_poll(
        self,
        interaction: discord.Interaction,
        frage: str,
        option_1: str,
        option_2: str,
        option_3: str | None = None,
        option_4: str | None = None,
        option_5: str | None = None,
        dauer: str | None = None,
        mehrfachauswahl: bool = False,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("❌ Nur in Server-Textkanälen verfügbar.", ephemeral=True)
            return
        opts = [o.strip() for o in (option_1, option_2, option_3, option_4, option_5) if o and o.strip()]
        secs = parse_time(dauer) if dauer else None
        ends_at = int(time.time() + secs) if secs else 0
        poll_id = uuid.uuid4().hex[:8]

        poll_rec: dict[str, Any] = {
            "id": poll_id,
            "channel_id": interaction.channel.id,
            "message_id": 0,
            "question": frage,
            "options": opts,
            "multi": bool(mehrfachauswahl),
            "ends_at": ends_at,
            "ended": False,
            "votes": {str(i): [] for i in range(len(opts))},
        }
        view = discord.ui.View(timeout=None)
        for idx, opt_lbl in enumerate(opts):
            view.add_item(PersistentPollButton(poll_id, idx, opt_lbl))

        await interaction.response.send_message(embed=_render_poll_embed(poll_rec), view=view)
        sent = await interaction.original_response()
        poll_rec["message_id"] = sent.id
        community_store.mutate(
            interaction.guild.id, lambda g: g.setdefault("polls", {}).__setitem__(poll_id, poll_rec)
        )

    @app_commands.command(name="afk", description="💤 Setzt dich auf AFK (antwortet automatisch bei Pings)")
    @app_commands.describe(grund="Warum bist du gerade AFK?")
    async def cmd_afk(
        self, interaction: discord.Interaction, grund: str = "Gerade nicht am Platz"
    ) -> None:
        if not interaction.guild:
            return
        community_store.mutate(
            interaction.guild.id,
            lambda g: g.setdefault("afk", {}).__setitem__(
                str(interaction.user.id), {"reason": grund[:150], "since": int(time.time())}
            ),
        )
        await interaction.response.send_message(
            f"💤 {interaction.user.mention} ist jetzt **AFK**: *{grund}*"
        )

    # ── Giveaway Auslosung & Timer ────────────────────────────────────────

    async def finish_giveaway(self, guild: discord.Guild, gway_id: str) -> bool:
        def _mark(gd: dict[str, Any]) -> dict[str, Any] | None:
            rec = (gd.get("giveaways") or {}).get(gway_id)
            if not rec or rec.get("ended"):
                return None
            rec["ended"] = True
            entries = [int(x) for x in (rec.get("entries") or [])]
            k = min(len(entries), max(1, int(rec.get("winners_count") or 1)))
            winners = random.sample(entries, k) if entries else []
            rec["winners"] = winners
            return dict(rec)

        gw = community_store.mutate(guild.id, _mark)
        if not gw:
            return False

        winners: list[int] = gw.get("winners") or []
        coin_prize = max(0, int(gw.get("coin_prize") or 0))
        if coin_prize > 0:
            for wid in winners:
                update_balance(wid, coin_prize)

        ch = guild.get_channel(int(gw["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            win_mentions = ", ".join(f"<@{w}>" for w in winners) if winners else "*Keine Teilnehmer*"
            end_emb = discord.Embed(
                title="🎉 GEWINNSPIEL BEENDET 🎉",
                description=(
                    f"🎁 **Preis:** **{gw['prize']}**\n"
                    f"🏆 **Gewinner:** {win_mentions}\n"
                    f"👥 **Teilnehmer:** `{len(gw.get('entries', []))}`"
                ),
                color=COLOR_SUCCESS if winners else COLOR_WARNING,
            )
            end_emb.set_footer(text=f"Giveaway-ID: {gway_id}")
            view = discord.ui.View(timeout=None)
            view.add_item(PersistentGiveawayButton(gway_id, count=len(gw.get("entries", [])), ended=True))
            try:
                msg = await ch.fetch_message(int(gw["message_id"]))
                await msg.edit(embed=end_emb, view=view)
            except Exception:
                pass
            if winners:
                coin_note = f" *(inkl. `+{coin_prize:,} 🪙`)*" if coin_prize > 0 else ""
                try:
                    await ch.send(
                        f"🎊 Herzlichen Glückwunsch {win_mentions}! Ihr habt **{gw['prize']}**{coin_note} gewonnen!"
                    )
                except Exception:
                    pass
        return True

    @tasks.loop(seconds=15)
    async def community_watcher(self) -> None:
        now = int(time.time())
        for guild in list(self.bot.guilds):
            g = community_store.get_guild(guild.id)
            for gid, gw in list((g.get("giveaways") or {}).items()):
                if isinstance(gw, dict) and not gw.get("ended") and int(gw.get("ends_at") or 0) <= now:
                    await self.finish_giveaway(guild, gid)

            for pid, poll in list((g.get("polls") or {}).items()):
                if (
                    isinstance(poll, dict)
                    and not poll.get("ended")
                    and int(poll.get("ends_at") or 0) > 0
                    and int(poll["ends_at"]) <= now
                ):
                    def _close_p(gd: dict[str, Any], p_key: str = pid) -> dict[str, Any] | None:
                        pr = (gd.get("polls") or {}).get(p_key)
                        if pr and not pr.get("ended"):
                            pr["ended"] = True
                            return dict(pr)
                        return None

                    closed = community_store.mutate(guild.id, _close_p)
                    if closed:
                        ch = guild.get_channel(int(closed["channel_id"]))
                        if isinstance(ch, discord.TextChannel):
                            try:
                                msg = await ch.fetch_message(int(closed["message_id"]))
                                view = discord.ui.View(timeout=None)
                                for idx, lbl in enumerate(closed.get("options", [])):
                                    view.add_item(PersistentPollButton(pid, idx, lbl, disabled=True))
                                await msg.edit(embed=_render_poll_embed(closed), view=view)
                            except Exception:
                                pass

    @community_watcher.before_loop
    async def _before_watcher(self) -> None:
        await self.bot.wait_until_ready()

    # ── Custom-Commands & AFK Listener ────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        g = community_store.get_guild(message.guild.id)

        # 1) AFK aufheben, sobald der User selbst wieder schreibt
        afk_map: dict[str, Any] = g.get("afk") or {}
        if str(message.author.id) in afk_map:
            community_store.mutate(message.guild.id, lambda gd: gd.get("afk", {}).pop(str(message.author.id), None))
            try:
                await message.channel.send(
                    f"👋 Willkommen zurück {message.author.mention}! Dein AFK-Status wurde aufgehoben.",
                    delete_after=7,
                )
            except Exception:
                pass

        # 2) AFK-Hinweis, wenn ein AFK-User gepingt wird
        if message.mentions:
            for mentioned in message.mentions[:3]:
                rec = afk_map.get(str(mentioned.id))
                if rec and mentioned.id != message.author.id:
                    try:
                        await message.reply(
                            f"💤 **{mentioned.display_name}** ist seit <t:{rec['since']}:R> AFK: *{rec['reason']}*",
                            mention_author=False,
                            delete_after=15,
                        )
                    except Exception:
                        pass

        # 3) Custom-Commands (!name)
        text = (message.content or "").strip()
        if text.startswith("!") and len(text) > 1:
            trig = text[1:].split()[0].lower()
            cc = (g.get("custom_commands") or {}).get(trig)
            if isinstance(cc, dict):
                community_store.mutate(
                    message.guild.id,
                    lambda gd: gd["custom_commands"][trig].__setitem__(
                        "uses", int(gd["custom_commands"][trig].get("uses") or 0) + 1
                    ),
                )
                out = (
                    str(cc.get("response") or "")
                    .replace("{user}", message.author.mention)
                    .replace("{username}", message.author.display_name)
                    .replace("{server}", message.guild.name)
                    .replace("{channel}", message.channel.mention)
                    .replace("{membercount}", str(message.guild.member_count or 0))
                )
                try:
                    if cc.get("embed"):
                        await message.channel.send(embed=discord.Embed(description=out, color=COLOR_INFO))
                    else:
                        await message.channel.send(out)
                except Exception:
                    pass

    # ── Starboard Listener ────────────────────────────────────────────────

    async def _sync_starboard(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id:
            return
        g = community_store.get_guild(payload.guild_id)
        sb = g.get("starboard") or {}
        if not sb.get("enabled") or not sb.get("channel_id"):
            return
        if str(payload.emoji) != str(sb.get("emoji") or "⭐"):
            return
        if payload.channel_id == int(sb["channel_id"]):
            return

        guild = self.bot.get_guild(payload.guild_id)
        src_ch = guild.get_channel(payload.channel_id) if guild else None
        sb_ch = guild.get_channel(int(sb["channel_id"])) if guild else None
        if not isinstance(src_ch, discord.TextChannel) or not isinstance(sb_ch, discord.TextChannel):
            return

        try:
            msg = await src_ch.fetch_message(payload.message_id)
        except Exception:
            return
        if msg.author.bot:
            return

        star_emoji = str(sb.get("emoji") or "⭐")
        star_count = 0
        for reac in msg.reactions:
            if str(reac.emoji) == star_emoji:
                star_count = reac.count
                if not sb.get("self_star", False):
                    async for u in reac.users(limit=100):
                        if u.id == msg.author.id:
                            star_count = max(0, star_count - 1)
                            break
                break

        threshold = max(1, int(sb.get("threshold") or 3))
        posted_map: dict[str, Any] = sb.get("posted") or {}
        sb_msg_id = posted_map.get(str(msg.id))

        if star_count < threshold:
            return

        header = f"{star_emoji} **{star_count}** • {src_ch.mention}"
        embed = discord.Embed(
            description=(msg.content or "") + f"\n\n🔗 [Zur Original-Nachricht]({msg.jump_url})",
            color=COLOR_GOLD,
            timestamp=msg.created_at,
        )
        embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
        if msg.attachments:
            first_img = next(
                (a.url for a in msg.attachments if (a.content_type or "").startswith("image/")), None
            )
            if first_img:
                embed.set_image(url=first_img)

        try:
            if sb_msg_id:
                existing = await sb_ch.fetch_message(int(sb_msg_id))
                await existing.edit(content=header, embed=embed)
            else:
                sent = await sb_ch.send(content=header, embed=embed)
                community_store.mutate(
                    guild.id,
                    lambda gd: gd["starboard"].setdefault("posted", {}).__setitem__(str(msg.id), sent.id),
                )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._sync_starboard(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._sync_starboard(payload)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommunityCog(bot))
