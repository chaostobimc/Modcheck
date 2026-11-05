"""Leveling Cog: Exakte MEE6-XP-Formel, Chat- & Voice-XP, Level-Rollen, Coin-Belohnungen & MEE6-Import."""
from __future__ import annotations

import asyncio
import io
import json
import random
import time
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_core.checks import is_user_admin
from bot_core.config import COLOR_ERROR, COLOR_GOLD, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING
from bot_core.server_store import (
    levels_store,
    mee6_level_from_xp,
    mee6_total_xp_for_level,
    mee6_xp_for_next_level,
)
from bot_core.storage import update_balance


def _xp_progress_bar(current: int, needed: int, width: int = 16) -> str:
    ratio = max(0.0, min(1.0, current / max(1, needed)))
    filled = round(ratio * width)
    return "🟦" * filled + "⬛" * (width - filled) + f" `{ratio * 100:.1f}%`"


def get_user_level_snapshot(guild_id: int, user_id: int) -> dict[str, Any]:
    """Liefert Rang, Level & XP eines Users (auch für `/profile` & `/userinfo`)."""
    g = levels_store.get_guild(guild_id)
    users_map: dict[str, Any] = g.get("users") or {}
    rec = users_map.get(str(user_id)) or {}
    total_xp = int(rec.get("xp") or 0)
    lvl, in_lvl, needed = mee6_level_from_xp(total_xp)

    sorted_uids = sorted(
        ((int(k), int(v.get("xp") or 0)) for k, v in users_map.items() if k.isdigit() and isinstance(v, dict)),
        key=lambda item: item[1],
        reverse=True,
    )
    rank = 0
    for idx, (uid, xp_val) in enumerate(sorted_uids, 1):
        if uid == user_id and xp_val > 0:
            rank = idx
            break
    return {
        "xp": total_xp,
        "level": lvl,
        "xp_in_level": in_lvl,
        "xp_needed": needed,
        "rank": rank,
        "total_ranked": sum(1 for _, x in sorted_uids if x > 0),
        "messages": int(rec.get("messages") or 0),
        "voice_minutes": int(rec.get("voice_minutes") or 0),
    }


def _extract_mee6_players(payload: Any) -> list[dict[str, Any]]:
    """Normalisiert MEE6-API-Antworten, Export-Skript-Dateien und Modcheck-Backups."""
    players: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("players"), list):
            raw_list = payload["players"]
        elif isinstance(payload.get("users"), dict):
            for uid_s, rec in payload["users"].items():
                if isinstance(rec, dict):
                    players.append(
                        {
                            "id": str(uid_s),
                            "xp": rec.get("xp"),
                            "level": rec.get("level"),
                            "messages": rec.get("messages", 0),
                            "voice_minutes": rec.get("voice_minutes", 0),
                        }
                    )
            return players
        else:
            raw_list = []
    elif isinstance(payload, list):
        raw_list = payload
    else:
        return []

    for p in raw_list:
        if not isinstance(p, dict):
            continue
        uid_s = str(p.get("id") or p.get("user_id") or "").strip()
        if not uid_s.isdigit():
            continue
        total_xp = p.get("xp")
        det = p.get("detailed_xp")
        if total_xp is None and isinstance(det, list) and len(det) >= 3:
            total_xp = det[2]
        lvl = int(p.get("level") or 0)
        if total_xp is None:
            total_xp = mee6_total_xp_for_level(lvl)
        players.append(
            {
                "id": uid_s,
                "xp": max(0, int(total_xp)),
                "level": lvl,
                "messages": int(p.get("message_count") or p.get("messages") or 0),
                "voice_minutes": int(p.get("voice_minutes") or 0),
            }
        )
    return players


class LeaderboardPagerView(discord.ui.View):
    """Seitennavigation für `/levels leaderboard`."""

    def __init__(self, guild: discord.Guild, entries: list[tuple[int, dict[str, Any]]], author_id: int) -> None:
        super().__init__(timeout=180)
        self.guild = guild
        self.entries = entries
        self.author_id = author_id
        self.page = 0
        self.per_page = 10
        self.max_page = max(0, (len(entries) - 1) // self.per_page)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.btn_prev.disabled = self.page <= 0
        self.btn_next.disabled = self.page >= self.max_page

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        chunk = self.entries[start : start + self.per_page]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines: list[str] = []
        for idx, (uid, rec) in enumerate(chunk, start + 1):
            total_xp = int(rec.get("xp") or 0)
            lvl, in_lvl, needed = mee6_level_from_xp(total_xp)
            prefix = medals.get(idx, f"`#{idx:02d}`")
            msgs = int(rec.get("messages") or 0)
            vc_m = int(rec.get("voice_minutes") or 0)
            vc_part = f" • 🎙️ `{vc_m // 60}h`" if vc_m >= 60 else ""
            lines.append(
                f"{prefix} <@{uid}> — **Lvl. {lvl}** (`{total_xp:,} XP` • `{in_lvl}/{needed}` • 💬 `{msgs:,}`{vc_part})"
            )

        embed = discord.Embed(
            title=f"🏆 XP- & Level-Rangliste – {self.guild.name}",
            description="\n".join(lines) if lines else "Noch keine Einträge.",
            color=COLOR_GOLD,
        )
        embed.set_footer(
            text=f"Seite {self.page + 1}/{self.max_page + 1} • {len(self.entries)} aktive Mitglieder • Exakte MEE6-XP-Kurve"
        )
        return embed

    @discord.ui.button(label="◀ Zurück", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Nutze selbst `/levels leaderboard`.", ephemeral=True)
            return
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Weiter ▶", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Nutze selbst `/levels leaderboard`.", ephemeral=True)
            return
        self.page = min(self.max_page, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class LevelsGroup(app_commands.Group):
    """Slash-Command-Gruppe `/levels ...`."""

    def __init__(self, cog: LevelingCog) -> None:
        super().__init__(name="levels", description="📈 XP-Rangliste, Level-Rollen, Einstellungen & MEE6-Import")
        self.cog = cog

    @app_commands.command(name="leaderboard", description="🏆 Zeigt das Level- & XP-Leaderboard des Servers")
    @app_commands.describe(seite="Startseite (1 = Top 10)")
    async def cmd_leaderboard(
        self, interaction: discord.Interaction, seite: app_commands.Range[int, 1, 100] = 1
    ) -> None:
        if not interaction.guild:
            return
        g = levels_store.get_guild(interaction.guild.id)
        users_map: dict[str, Any] = g.get("users") or {}
        ranked = sorted(
            (
                (int(uid), rec)
                for uid, rec in users_map.items()
                if uid.isdigit() and isinstance(rec, dict) and int(rec.get("xp") or 0) > 0
            ),
            key=lambda x: int(x[1].get("xp") or 0),
            reverse=True,
        )
        if not ranked:
            await interaction.response.send_message("ℹ️ Noch keine XP auf diesem Server gesammelt!", ephemeral=True)
            return
        view = LeaderboardPagerView(interaction.guild, ranked, interaction.user.id)
        view.page = min(view.max_page, seite - 1)
        view._sync_buttons()
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    @app_commands.command(name="rewards", description="🎁 Zeigt alle Level-Rollen & Coin-Belohnungen")
    async def cmd_rewards(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        g = levels_store.get_guild(interaction.guild.id)
        rewards: dict[str, Any] = g.get("role_rewards") or {}
        cfg = g.get("config") or {}
        coin_per_lvl = int(cfg.get("coin_per_level") or 0)

        lines: list[str] = []
        for lvl_s, rdata in sorted(rewards.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            if isinstance(rdata, dict):
                rid = int(rdata.get("role_id") or 0)
                c_bonus = int(rdata.get("coins") or 0)
            else:
                rid = int(rdata or 0)
                c_bonus = 0
            bonus_str = f" + **`{c_bonus:,} 🪙`**" if c_bonus > 0 else ""
            lines.append(
                f"• **Level `{int(lvl_s):02d}`** (`{mee6_total_xp_for_level(int(lvl_s)):,} XP`) → <@&{rid}>{bonus_str}"
            )

        desc = "\n".join(lines) if lines else "*Noch keine Level-Rollen hinterlegt (`/levels reward-add`).*"
        if coin_per_lvl > 0:
            desc += f"\n\n🪙 **Zusätzlich pro Level-Up:** `Level × {coin_per_lvl:,} Coins`"
        embed = discord.Embed(
            title=f"🎁 Level-Belohnungen – {interaction.guild.name}",
            description=desc,
            color=COLOR_INFO,
        )
        mode_txt = "Stapelnd (alle behalten)" if cfg.get("stack_roles", True) else "Ersetzend (nur höchste Level-Rolle)"
        embed.set_footer(text=f"Rollen-Modus: {mode_txt}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reward-add", description="⚙️ Fügt eine Level-Rollen-Belohnung hinzu (Admins)")
    @app_commands.describe(
        level="Benötigtes Level (1–200)",
        rolle="Rolle, die ab diesem Level vergeben wird",
        coin_bonus="Einmaliger Coin-Bonus beim Erreichen dieses Levels",
    )
    async def cmd_reward_add(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 200],
        rolle: discord.Role,
        coin_bonus: app_commands.Range[int, 0, 1000000] = 0,
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        levels_store.mutate(
            interaction.guild.id,
            lambda g: g["role_rewards"].__setitem__(
                str(level), {"role_id": rolle.id, "coins": int(coin_bonus)}
            ),
        )
        await interaction.response.send_message(
            f"✅ Level-Belohnung gespeichert: **Level {level}** → {rolle.mention}"
            + (f" (+`{coin_bonus:,} 🪙`)" if coin_bonus else ""),
            ephemeral=True,
        )

    @app_commands.command(name="reward-remove", description="🗑️ Entfernt die Belohnung für ein Level (Admins)")
    async def cmd_reward_remove(
        self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 200]
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        existed = levels_store.mutate(
            interaction.guild.id, lambda g: g["role_rewards"].pop(str(level), None) is not None
        )
        msg = f"🗑️ Belohnung für **Level {level}** entfernt." if existed else f"ℹ️ Keine Belohnung für Level {level}."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="setup", description="⚙️ Konfiguriert das Level- & XP-System (Admins)")
    @app_commands.describe(
        aktiv="Leveling ein-/ausschalten",
        kanal="Kanal für Level-Up-Nachrichten (leer = aktueller Kanal / siehe modus)",
        modus="Wo sollen Level-Ups gesendet werden?",
        xp_multiplikator="Globaler XP-Multiplikator (z. B. 1.0 = MEE6-Standard, 2.0 = Double-XP)",
        voice_xp="Voice-XP pro Minute im Sprachkanal (0 = deaktiviert, Standard: 10)",
        rollen_stapeln="True = alle Level-Rollen behalten, False = nur höchste behalten",
        coins_pro_level="Coins pro erreichtem Level (z. B. 100 -> Lvl 10 gibt 1.000 Coins)",
    )
    @app_commands.choices(
        modus=[
            app_commands.Choice(name="Fester Level-Up-Kanal (oder aktueller Kanal)", value="channel"),
            app_commands.Choice(name="Immer im aktuellen Chat-Kanal", value="current"),
            app_commands.Choice(name="Per Direktnachricht (DM)", value="dm"),
            app_commands.Choice(name="Stumm (keine Nachricht, nur Rollen/Coins)", value="silent"),
        ]
    )
    async def cmd_setup(
        self,
        interaction: discord.Interaction,
        aktiv: bool | None = None,
        kanal: discord.TextChannel | None = None,
        modus: str | None = None,
        xp_multiplikator: app_commands.Range[float, 0.1, 10.0] | None = None,
        voice_xp: app_commands.Range[int, 0, 100] | None = None,
        rollen_stapeln: bool | None = None,
        coins_pro_level: app_commands.Range[int, 0, 50000] | None = None,
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> dict[str, Any]:
            cfg = g["config"]
            if aktiv is not None:
                cfg["enabled"] = bool(aktiv)
            if kanal is not None:
                cfg["announce_channel_id"] = kanal.id
            if modus is not None:
                cfg["announce_mode"] = modus
            if xp_multiplikator is not None:
                cfg["xp_rate"] = round(float(xp_multiplikator), 2)
            if voice_xp is not None:
                cfg["voice_xp_enabled"] = voice_xp > 0
                cfg["voice_xp_per_min"] = int(voice_xp)
            if rollen_stapeln is not None:
                cfg["stack_roles"] = bool(rollen_stapeln)
            if coins_pro_level is not None:
                cfg["coin_per_level"] = int(coins_pro_level)
            return cfg

        cfg = levels_store.mutate(interaction.guild.id, _upd)
        ch_str = f"<#{cfg['announce_channel_id']}>" if cfg.get("announce_channel_id") else "*(aktueller Kanal)*"
        embed = discord.Embed(
            title="⚙️ Level-System Einstellungen",
            description=(
                f"**Status:** {'✅ Aktiv' if cfg.get('enabled', True) else '⏸️ Deaktiviert'}\n"
                f"**Level-Up-Kanal:** {ch_str} (`{cfg.get('announce_mode', 'channel')}`)\n"
                f"**XP-Rate:** `{cfg.get('xp_rate', 1.0)}x` (`15–25 XP / 60s`)\n"
                f"**Voice-XP:** `{cfg.get('voice_xp_per_min', 10)} XP/Min.` ({'Aktiv' if cfg.get('voice_xp_enabled', True) else 'Aus'})\n"
                f"**Level-Rollen:** {'Stapelnd' if cfg.get('stack_roles', True) else 'Ersetzend'}\n"
                f"**Coin-Bonus pro Level:** `{cfg.get('coin_per_level', 100):,} 🪙`"
            ),
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="noxp-channel", description="⚙️ Schaltet XP in einem Kanal an oder aus (Admins)")
    async def cmd_noxp_channel(
        self, interaction: discord.Interaction, kanal: discord.TextChannel
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _toggle(g: dict[str, Any]) -> bool:
            lst: list[int] = g["config"].setdefault("no_xp_channels", [])
            if kanal.id in lst:
                lst.remove(kanal.id)
                return False
            lst.append(kanal.id)
            return True

        blocked = levels_store.mutate(interaction.guild.id, _toggle)
        state = "🚫 **Kein XP mehr**" if blocked else "✅ **XP wieder aktiv**"
        await interaction.response.send_message(f"{state} in {kanal.mention}.", ephemeral=True)

    @app_commands.command(name="set-level", description="⚙️ Setzt das Level eines Users exakt (MEE6-Formel, Admins)")
    async def cmd_set_level(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        level: app_commands.Range[int, 0, 500],
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        target_xp = mee6_total_xp_for_level(level)
        await self.cog.set_member_xp(interaction.guild, member, target_xp)
        await interaction.response.send_message(
            f"✅ {member.mention} wurde auf **Level {level}** (`{target_xp:,} XP`) gesetzt.",
            ephemeral=True,
        )

    @app_commands.command(name="set-xp", description="⚙️ Setzt die Gesamt-XP eines Users exakt (Admins)")
    async def cmd_set_xp(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        xp: app_commands.Range[int, 0, 50000000],
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        lvl = await self.cog.set_member_xp(interaction.guild, member, xp)
        await interaction.response.send_message(
            f"✅ {member.mention} hat jetzt **`{xp:,} XP`** (**Level {lvl}**).", ephemeral=True
        )

    @app_commands.command(name="add-xp", description="⚙️ Vergibt oder zieht XP bei einem User ab (Admins)")
    async def cmd_add_xp(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        betrag: app_commands.Range[int, -5000000, 5000000],
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        snap = get_user_level_snapshot(interaction.guild.id, member.id)
        new_xp = max(0, snap["xp"] + int(betrag))
        lvl = await self.cog.set_member_xp(interaction.guild, member, new_xp)
        await interaction.response.send_message(
            f"✅ XP angepasst (`{betrag:+,}`): {member.mention} steht jetzt bei **`{new_xp:,} XP`** (**Level {lvl}**).",
            ephemeral=True,
        )

    @app_commands.command(
        name="import-mee6",
        description="📥 Importiert Level & XP aus MEE6 (API oder JSON-Datei)",
    )
    @app_commands.describe(
        datei="Optional: JSON-Export von tools/export_mee6.py oder /levels export (leer = direkter MEE6-API-Abruf)",
        ueberschreiben="False = behält jeweils den höheren XP-Stand (empfohlen), True = überschreibt exakt",
        server_id="Optional: Abweichende MEE6-Server-ID für den API-Import",
    )
    async def cmd_import_mee6(
        self,
        interaction: discord.Interaction,
        datei: discord.Attachment | None = None,
        ueberschreiben: bool = False,
        server_id: str | None = None,
    ) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        players: list[dict[str, Any]] = []
        role_rewards_imported = 0

        if datei is not None:
            try:
                raw_bytes = await datei.read()
                payload = json.loads(raw_bytes.decode("utf-8"))
                players = _extract_mee6_players(payload)
                if isinstance(payload, dict) and isinstance(payload.get("role_rewards"), list):
                    def _imp_rewards(g: dict[str, Any]) -> int:
                        cnt = 0
                        for rw in payload["role_rewards"]:
                            rk = rw.get("rank") or rw.get("level")
                            role_obj = rw.get("role") or {}
                            rid = role_obj.get("id") if isinstance(role_obj, dict) else rw.get("role_id")
                            if rk and rid:
                                g["role_rewards"][str(int(rk))] = {"role_id": int(rid), "coins": 0}
                                cnt += 1
                        return cnt
                    role_rewards_imported = levels_store.mutate(interaction.guild.id, _imp_rewards)
            except Exception as e:
                await interaction.followup.send(f"❌ JSON-Datei konnte nicht gelesen werden: `{e}`", ephemeral=True)
                return
        else:
            target_gid = (server_id or str(interaction.guild.id)).strip()
            if not target_gid.isdigit():
                await interaction.followup.send("❌ Ungültige Server-ID.", ephemeral=True)
                return
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; ModcheckBot/3.0)",
                "Accept": "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                for page in range(0, 50):
                    url = f"https://mee6.xyz/api/plugins/levels/leaderboard/{target_gid}?limit=1000&page={page}"
                    try:
                        async with session.get(url) as resp:
                            if resp.status == 401 or resp.status == 403:
                                await interaction.followup.send(
                                    "⚠️ **MEE6-Leaderboard ist auf privat gestellt oder durch Cloudflare geschützt.**\n"
                                    "1. Aktiviere im MEE6-Dashboard kurz *„Make my server's leaderboard public“*, **oder**\n"
                                    f"2. Führe auf deinem PC `python tools/export_mee6.py {target_gid}` aus und lade die erzeugte `.json`-Datei hier mit `/levels import-mee6 datei:...` hoch!",
                                    ephemeral=True,
                                )
                                return
                            if resp.status == 404:
                                await interaction.followup.send(
                                    "❌ MEE6 hat für diese Server-ID kein Level-Leaderboard gefunden.",
                                    ephemeral=True,
                                )
                                return
                            if resp.status != 200:
                                break
                            data = await resp.json()
                    except Exception as e:
                        await interaction.followup.send(
                            f"❌ Fehler beim Abruf der MEE6-API: `{e}`\nNutze alternativ `python tools/export_mee6.py {target_gid}` + Datei-Upload.",
                            ephemeral=True,
                        )
                        return

                    if page == 0 and isinstance(data.get("role_rewards"), list):
                        def _save_rw(g: dict[str, Any]) -> int:
                            c = 0
                            for rw in data["role_rewards"]:
                                rk = rw.get("rank")
                                rid = (rw.get("role") or {}).get("id")
                                if rk and rid and str(rk) not in g["role_rewards"]:
                                    g["role_rewards"][str(int(rk))] = {"role_id": int(rid), "coins": 0}
                                    c += 1
                            return c
                        role_rewards_imported = levels_store.mutate(interaction.guild.id, _save_rw)

                    batch = _extract_mee6_players(data)
                    if not batch:
                        break
                    players.extend(batch)
                    if len(batch) < 1000:
                        break
                    await asyncio.sleep(0.4)

        if not players:
            await interaction.followup.send("⚠️ Keine Spieler-Einträge zum Importieren gefunden.", ephemeral=True)
            return

        def _apply_import(g: dict[str, Any]) -> tuple[int, int]:
            users_map = g["users"]
            imported_cnt = 0
            updated_cnt = 0
            for p in players:
                uid_s = p["id"]
                imp_xp = int(p["xp"])
                imp_msgs = int(p.get("messages") or 0)
                imp_vc = int(p.get("voice_minutes") or 0)
                cur = users_map.get(uid_s)
                if cur is None:
                    lvl, _, _ = mee6_level_from_xp(imp_xp)
                    users_map[uid_s] = {
                        "xp": imp_xp,
                        "level": lvl,
                        "messages": imp_msgs,
                        "voice_minutes": imp_vc,
                    }
                    imported_cnt += 1
                else:
                    final_xp = imp_xp if ueberschreiben else max(int(cur.get("xp") or 0), imp_xp)
                    lvl, _, _ = mee6_level_from_xp(final_xp)
                    cur["xp"] = final_xp
                    cur["level"] = lvl
                    cur["messages"] = imp_msgs if ueberschreiben else max(int(cur.get("messages") or 0), imp_msgs)
                    cur["voice_minutes"] = imp_vc if ueberschreiben else max(int(cur.get("voice_minutes") or 0), imp_vc)
                    updated_cnt += 1
            return imported_cnt, updated_cnt

        new_cnt, upd_cnt = levels_store.mutate(interaction.guild.id, _apply_import)

        # Level-Rollen für aktuell anwesende Mitglieder synchronisieren
        synced_roles = 0
        for p in players[:500]:
            mem = interaction.guild.get_member(int(p["id"]))
            if mem is not None:
                lvl = get_user_level_snapshot(interaction.guild.id, mem.id)["level"]
                if await self.cog.sync_member_level_roles(interaction.guild, mem, lvl):
                    synced_roles += 1

        embed = discord.Embed(
            title="✅ MEE6-Import erfolgreich abgeschlossen!",
            description=(
                f"• **Verarbeitete Spieler:** `{len(players):,}`\n"
                f"• **Neu angelegt:** `{new_cnt:,}`\n"
                f"• **Aktualisiert / zusammengeführt:** `{upd_cnt:,}`\n"
                f"• **Übernommene MEE6 Level-Rollen:** `{role_rewards_imported}`\n"
                f"• **Mitglieder-Rollen synchronisiert:** `{synced_roles}`\n\n"
                "Alle Level nutzen exakt die originale MEE6-Formel (`5·L² + 50·L + 100`)."
            ),
            color=COLOR_SUCCESS,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="export", description="📤 Exportiert alle Level- & XP-Daten als JSON-Backup (Admins)")
    async def cmd_export(self, interaction: discord.Interaction) -> None:
        if not is_user_admin(interaction.user) or not interaction.guild:
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        g = levels_store.get_guild(interaction.guild.id)
        raw = json.dumps(g, indent=2, ensure_ascii=False).encode("utf-8")
        file = discord.File(io.BytesIO(raw), filename=f"levels_backup_{interaction.guild.id}.json")
        await interaction.response.send_message(
            f"📦 Komplettes Level-Backup für **{interaction.guild.name}** (`{len(g.get('users', {}))}` User):",
            file=file,
            ephemeral=True,
        )


class LevelingCog(commands.Cog, name="Leveling"):
    """MEE6-kompatibles Chat- & Voice-Leveling mit Role- & Coin-Rewards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._xp_cooldowns: dict[tuple[int, int], float] = {}
        self.levels_group = LevelsGroup(self)
        self.bot.tree.add_command(self.levels_group)

    async def cog_load(self) -> None:
        self.voice_xp_loop.start()

    async def cog_unload(self) -> None:
        self.voice_xp_loop.cancel()
        self.bot.tree.remove_command(self.levels_group.name, type=self.levels_group.type)

    async def sync_member_level_roles(
        self, guild: discord.Guild, member: discord.Member, level: int
    ) -> bool:
        g = levels_store.get_guild(guild.id)
        rewards: dict[str, Any] = g.get("role_rewards") or {}
        if not rewards:
            return False
        stack = bool((g.get("config") or {}).get("stack_roles", True))

        earned_roles: list[tuple[int, discord.Role]] = []
        all_reward_roles: list[discord.Role] = []
        for lvl_s, rdata in rewards.items():
            if not lvl_s.isdigit():
                continue
            req_lvl = int(lvl_s)
            rid = int(rdata.get("role_id") or 0) if isinstance(rdata, dict) else int(rdata or 0)
            role = guild.get_role(rid)
            if role is None:
                continue
            all_reward_roles.append(role)
            if level >= req_lvl:
                earned_roles.append((req_lvl, role))

        if not earned_roles:
            return False

        earned_roles.sort(key=lambda x: x[0])
        desired: set[discord.Role] = (
            {r for _, r in earned_roles} if stack else {earned_roles[-1][1]}
        )

        to_add = [r for r in desired if r not in member.roles]
        to_remove = (
            [r for r in all_reward_roles if r not in desired and r in member.roles]
            if not stack
            else []
        )
        changed = False
        try:
            if to_add:
                await member.add_roles(*to_add, reason=f"Level {level} Belohnung")
                changed = True
            if to_remove:
                await member.remove_roles(*to_remove, reason=f"Level {level} Rollen-Upgrade")
                changed = True
        except Exception as e:
            print(f"⚠️ [Leveling] Konnte Level-Rolle für {member} nicht setzen: {e}")
        return changed

    async def set_member_xp(self, guild: discord.Guild, member: discord.Member, new_xp: int) -> int:
        lvl, _, _ = mee6_level_from_xp(new_xp)

        def _upd(g: dict[str, Any]) -> None:
            rec = g["users"].setdefault(
                str(member.id), {"xp": 0, "level": 0, "messages": 0, "voice_minutes": 0}
            )
            rec["xp"] = max(0, int(new_xp))
            rec["level"] = lvl

        levels_store.mutate(guild.id, _upd)
        await self.sync_member_level_roles(guild, member, lvl)
        return lvl

    async def _award_xp(
        self,
        guild: discord.Guild,
        member: discord.Member,
        xp_gain: int,
        source_channel: discord.abc.Messageable | None = None,
        is_voice: bool = False,
    ) -> None:
        if xp_gain <= 0:
            return

        def _mut(g: dict[str, Any]) -> tuple[int, int, int, dict[str, Any]]:
            rec = g["users"].setdefault(
                str(member.id), {"xp": 0, "level": 0, "messages": 0, "voice_minutes": 0}
            )
            old_xp = int(rec.get("xp") or 0)
            old_lvl, _, _ = mee6_level_from_xp(old_xp)
            new_xp = old_xp + xp_gain
            new_lvl, _, _ = mee6_level_from_xp(new_xp)
            rec["xp"] = new_xp
            rec["level"] = new_lvl
            if is_voice:
                rec["voice_minutes"] = int(rec.get("voice_minutes") or 0) + 1
            else:
                rec["messages"] = int(rec.get("messages") or 0) + 1
            return old_lvl, new_lvl, new_xp, dict(g)

        old_lvl, new_lvl, new_xp, g_snap = levels_store.mutate(guild.id, _mut)
        if new_lvl <= old_lvl:
            return

        await self.sync_member_level_roles(guild, member, new_lvl)

        cfg = g_snap.get("config") or {}
        rewards = g_snap.get("role_rewards") or {}
        coin_per_lvl = max(0, int(cfg.get("coin_per_level") or 0))
        total_coins = 0
        unlocked_roles: list[discord.Role] = []
        for lv in range(old_lvl + 1, new_lvl + 1):
            total_coins += coin_per_lvl * lv
            rw = rewards.get(str(lv))
            if isinstance(rw, dict):
                total_coins += max(0, int(rw.get("coins") or 0))
                r_obj = guild.get_role(int(rw.get("role_id") or 0))
                if r_obj:
                    unlocked_roles.append(r_obj)
            elif rw:
                r_obj = guild.get_role(int(rw))
                if r_obj:
                    unlocked_roles.append(r_obj)

        if total_coins > 0:
            update_balance(member.id, total_coins)

        mode = str(cfg.get("announce_mode") or "channel")
        if mode == "silent":
            return

        tpl = str(
            cfg.get("levelup_message")
            or "🎉 Glückwunsch {user}! Du hast **Level {level}** erreicht!"
        )
        msg_text = (
            tpl.replace("{user}", member.mention)
            .replace("{username}", member.display_name)
            .replace("{level}", str(new_lvl))
            .replace("{xp}", f"{new_xp:,}")
        )
        embed = discord.Embed(
            title=f"⬆️ Level Up! • Level {new_lvl}",
            description=msg_text,
            color=COLOR_GOLD,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if unlocked_roles:
            embed.add_field(
                name="👑 Neue Rolle freigeschaltet",
                value=", ".join(r.mention for r in unlocked_roles),
                inline=True,
            )
        if total_coins > 0:
            embed.add_field(name="🪙 Level-Prämie", value=f"**+{total_coins:,} Coins**", inline=True)
        embed.set_footer(text=f"Nächstes Level benötigt {mee6_xp_for_next_level(new_lvl):,} XP")

        target: discord.abc.Messageable | None = None
        if mode == "dm":
            target = member
        elif mode == "current":
            target = source_channel
        else:
            ann_id = int(cfg.get("announce_channel_id") or 0)
            target = guild.get_channel(ann_id) if ann_id else source_channel

        if target is not None:
            try:
                await target.send(content=member.mention if mode != "dm" else None, embed=embed)
            except Exception:
                pass

    @app_commands.command(name="rank", description="🏅 Zeigt deine Level- & XP-Karte (MEE6-Kurve)")
    @app_commands.describe(member="Optional: anderer User")
    async def cmd_rank(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        snap = get_user_level_snapshot(interaction.guild.id, target.id)
        g = levels_store.get_guild(interaction.guild.id)
        rewards: dict[str, Any] = g.get("role_rewards") or {}

        next_rw_txt = None
        for lvl_s in sorted((int(k) for k in rewards if k.isdigit())):
            if lvl_s > snap["level"]:
                rw = rewards[str(lvl_s)]
                rid = int(rw.get("role_id") if isinstance(rw, dict) else rw)
                missing_xp = max(0, mee6_total_xp_for_level(lvl_s) - snap["xp"])
                next_rw_txt = f"Level **{lvl_s}** → <@&{rid}> *(noch `{missing_xp:,} XP`)*"
                break

        rank_str = f"**#{snap['rank']}** / {snap['total_ranked']}" if snap["rank"] else "Noch unplatziert"
        embed = discord.Embed(
            title=f"🏅 Rang-Karte – {target.display_name}",
            color=target.color if target.color.value else COLOR_INFO,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="🏆 Server-Rang", value=rank_str, inline=True)
        embed.add_field(name="⭐ Level", value=f"**`{snap['level']}`**", inline=True)
        embed.add_field(name="✨ Gesamt-XP", value=f"`{snap['xp']:,} XP`", inline=True)
        embed.add_field(
            name=f"📊 Fortschritt zu Level {snap['level'] + 1} ({snap['xp_in_level']:,} / {snap['xp_needed']:,} XP)",
            value=_xp_progress_bar(snap["xp_in_level"], snap["xp_needed"]),
            inline=False,
        )
        vc_h, vc_m = divmod(snap["voice_minutes"], 60)
        embed.add_field(
            name="📈 Aktivität",
            value=f"💬 `{snap['messages']:,}` Nachrichten • 🎙️ `{vc_h}h {vc_m}m` Voice",
            inline=False,
        )
        if next_rw_txt:
            embed.add_field(name="🎯 Nächste Rollen-Belohnung", value=next_rw_txt, inline=False)
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        if message.content.startswith(("!", "/", "?")):
            return

        g = levels_store.get_guild(message.guild.id)
        cfg = g.get("config") or {}
        if not cfg.get("enabled", True):
            return
        if message.channel.id in (cfg.get("no_xp_channels") or []):
            return
        no_xp_roles = set(cfg.get("no_xp_roles") or [])
        if no_xp_roles and any(r.id in no_xp_roles for r in message.author.roles):
            return

        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        cooldown = max(5, int(cfg.get("cooldown_sec") or 60))
        if now - self._xp_cooldowns.get(key, 0.0) < cooldown:
            return
        self._xp_cooldowns[key] = now

        xp_min = int(cfg.get("xp_min") or 15)
        xp_max = max(xp_min, int(cfg.get("xp_max") or 25))
        rate = float(cfg.get("xp_rate") or 1.0)
        mult_roles: dict[str, Any] = cfg.get("multiplier_roles") or {}
        for r in message.author.roles:
            if str(r.id) in mult_roles:
                rate *= float(mult_roles[str(r.id)])

        gain = max(1, round(random.randint(xp_min, xp_max) * rate))
        await self._award_xp(message.guild, message.author, gain, source_channel=message.channel)

    @tasks.loop(minutes=1)
    async def voice_xp_loop(self) -> None:
        for guild in list(self.bot.guilds):
            g = levels_store.get_guild(guild.id)
            cfg = g.get("config") or {}
            if not cfg.get("enabled", True) or not cfg.get("voice_xp_enabled", True):
                continue
            per_min = int(cfg.get("voice_xp_per_min") or 0)
            if per_min <= 0:
                continue
            no_xp_channels = set(cfg.get("no_xp_channels") or [])
            afk_id = guild.afk_channel.id if guild.afk_channel else 0

            for vc in guild.voice_channels:
                if vc.id == afk_id or vc.id in no_xp_channels:
                    continue
                active_humans = [
                    m
                    for m in vc.members
                    if not m.bot and not (m.voice and (m.voice.self_deaf or m.voice.deaf))
                ]
                if len(active_humans) < 2:
                    continue
                gain = max(1, round(per_min * float(cfg.get("xp_rate") or 1.0)))
                for mem in active_humans:
                    try:
                        await self._award_xp(guild, mem, gain, source_channel=None, is_voice=True)
                    except Exception:
                        pass

    @voice_xp_loop.before_loop
    async def _before_voice_xp(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelingCog(bot))
