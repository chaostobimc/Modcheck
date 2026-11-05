"""Counting Cog: Abwechselndes Zählen im Kanal mit Highscore, Reset bei Fehlern & Lösch-Schutz."""
from __future__ import annotations

import ast
import datetime
import operator
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import is_user_admin
from bot_core.config import COLOR_ERROR, COLOR_GOLD, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING
from bot_core.server_store import counting_store

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
}

_PURE_INT_RE = re.compile(r"^\s*([+-]?\d+)\s*$")
_MATH_START_RE = re.compile(r"^\s*[\d(][0-9\s+\-*/().]*$")


def _eval_ast_node(node: ast.AST, depth: int = 0) -> float:
    if depth > 8:
        raise ValueError("Expression too deep")
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        val = _eval_ast_node(node.operand, depth + 1)
        return val if isinstance(node.op, ast.UAdd) else -val
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        left = _eval_ast_node(node.left, depth + 1)
        right = _eval_ast_node(node.right, depth + 1)
        if abs(left) > 10**9 or abs(right) > 10**9:
            raise ValueError("Overflow")
        return float(_SAFE_OPS[type(node.op)](left, right))
    raise ValueError("Unsupported syntax")


def parse_counting_message(content: str, allow_math: bool) -> int | None:
    """Liest die gezählte Ganzzahl aus einer Nachricht (oder None, falls es kein Zahlenversuch ist)."""
    text = (content or "").strip()
    if not text:
        return None
    first_token = text.split()[0]
    m_int = _PURE_INT_RE.match(text) or _PURE_INT_RE.match(first_token)
    if m_int and (not allow_math or _PURE_INT_RE.match(text)):
        return int(m_int.group(1)) if len(m_int.group(1)) <= 100 else None
    if allow_math and _MATH_START_RE.match(text) and len(text) <= 60:
        try:
            tree = ast.parse(text, mode="eval")
            val = _eval_ast_node(tree)
            if val.is_integer():
                return int(val)
        except Exception:
            pass
    if m_int and len(m_int.group(1)) <= 100:
        return int(m_int.group(1))
    return None


class CountingGroup(app_commands.Group):
    """Slash-Command-Gruppe `/counting ...`."""

    def __init__(self) -> None:
        super().__init__(name="counting", description="🔢 Counting-Spiel, Highscores & Einstellungen")

    @app_commands.command(
        name="setup",
        description="⚙️ Richtet den Counting-Kanal ein (Admins)",
    )
    @app_commands.describe(
        kanal="Kanal, in dem gemeinsam gezählt wird",
        mathe_erlaubt="Auch Rechenaufgaben wie '40+2' als Zahl akzeptieren? (Standard: Nein)",
        nur_zahlen="Nicht-Zahlen-Chatnachrichten im Counting-Kanal automatisch löschen? (Standard: Ja)",
        start_zahl="Optional: Aktuellen Zählerstand übernehmen (z. B. beim Umstieg vom alten Counting-Bot)",
    )
    async def cmd_setup(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel,
        mathe_erlaubt: bool = False,
        nur_zahlen: bool = True,
        start_zahl: app_commands.Range[int, 0, 10000000] | None = None,
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> dict[str, Any]:
            g["enabled"] = True
            g["channel_id"] = kanal.id
            g["math_enabled"] = bool(mathe_erlaubt)
            g["numbers_only"] = bool(nur_zahlen)
            if start_zahl is not None:
                g["current"] = int(start_zahl)
                g["last_user_id"] = 0
                if int(start_zahl) > int(g.get("highscore") or 0):
                    g["highscore"] = int(start_zahl)
            return g

        cfg = counting_store.mutate(interaction.guild.id, _upd)
        nxt = int(cfg["current"]) + 1
        intro = discord.Embed(
            title="🔢 Counting ist aktiv!",
            description=(
                f"Ab sofort wird in {kanal.mention} gemeinsam gezählt!\n\n"
                "**Regeln:**\n"
                "• Immer lückenlos **+1** weiterzählen (nichts überspringen, keine Zahl doppelt).\n"
                "• **Abwechselnd zählen** — niemand darf zweimal hintereinander schreiben!\n"
                "• Bei einem Fehler wird der Zähler auf **0** zurückgesetzt.\n\n"
                f"👉 **Die nächste Zahl ist `{nxt}`!**"
            ),
            color=COLOR_SUCCESS,
        )
        try:
            await kanal.send(embed=intro)
        except Exception:
            pass
        await interaction.response.send_message(
            f"✅ Counting in {kanal.mention} aktiviert! (Nächste Zahl: **{nxt}**)",
            ephemeral=True,
        )

    @app_commands.command(name="set", description="⚙️ Setzt den aktuellen Zählerstand manuell (Admins)")
    @app_commands.describe(zahl="Aktuelle Zahl (nächste erwartete Zahl ist zahl + 1)")
    async def cmd_set(
        self,
        interaction: discord.Interaction,
        zahl: app_commands.Range[int, 0, 10000000],
    ) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return

        def _upd(g: dict[str, Any]) -> None:
            g["current"] = int(zahl)
            g["last_user_id"] = 0
            g["last_message_id"] = 0
            if int(zahl) > int(g.get("highscore") or 0):
                g["highscore"] = int(zahl)

        counting_store.mutate(interaction.guild.id, _upd)
        await interaction.response.send_message(
            f"✅ Zählerstand auf **{zahl}** gesetzt — weiter geht es mit **{zahl + 1}**!",
            ephemeral=False,
        )

    @app_commands.command(name="disable", description="⏸️ Deaktiviert das Counting-Spiel (Admins)")
    async def cmd_disable(self, interaction: discord.Interaction) -> None:
        if not is_user_admin(interaction.user):
            await interaction.response.send_message("❌ Nur für Administratoren.", ephemeral=True)
            return
        counting_store.mutate(interaction.guild.id, lambda g: g.update({"enabled": False}))
        await interaction.response.send_message("⏸️ Counting wurde deaktiviert.", ephemeral=True)

    @app_commands.command(name="stats", description="📊 Zeigt Server-Highscore & persönliche Counting-Statistiken")
    @app_commands.describe(member="Optional: anderer User")
    async def cmd_stats(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            return
        target = member or interaction.user
        g = counting_store.get_guild(interaction.guild.id)
        u = (g.get("users") or {}).get(str(target.id), {"correct": 0, "fails": 0, "highest": 0})
        correct = int(u.get("correct") or 0)
        fails = int(u.get("fails") or 0)
        total_u = correct + fails
        acc = (correct / total_u * 100.0) if total_u > 0 else 100.0

        embed = discord.Embed(
            title=f"🔢 Counting-Statistik – {target.display_name}",
            color=COLOR_INFO,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        ch_str = f"<#{g['channel_id']}>" if g.get("channel_id") else "Nicht eingerichtet"
        embed.add_field(
            name="🌐 Server-Stand",
            value=(
                f"**Kanal:** {ch_str}\n"
                f"**Aktuell:** `{g.get('current', 0):,}` *(nächste: `{int(g.get('current', 0)) + 1:,}`)*\n"
                f"**Highscore:** 🏆 **`{g.get('highscore', 0):,}`**\n"
                f"**Gezählt gesamt:** `{g.get('total_counts', 0):,}`"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"👤 {target.display_name}",
            value=(
                f"✅ **Richtig:** `{correct:,}`\n"
                f"❌ **Fehler:** `{fails:,}`\n"
                f"🎯 **Genauigkeit:** `{acc:.1f}%`\n"
                f"🏔️ **Höchste Zahl:** `{u.get('highest', 0):,}`"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="🏆 Top-Zähler auf diesem Server")
    async def cmd_leaderboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        g = counting_store.get_guild(interaction.guild.id)
        users_map: dict[str, Any] = g.get("users") or {}
        ranked = sorted(
            (
                (int(uid), int(d.get("correct") or 0), int(d.get("fails") or 0))
                for uid, d in users_map.items()
                if uid.isdigit() and isinstance(d, dict) and int(d.get("correct") or 0) > 0
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        if not ranked:
            await interaction.response.send_message("ℹ️ Noch keine gezählten Zahlen vorhanden!", ephemeral=True)
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for idx, (uid, corr, fls) in enumerate(ranked[:15], 1):
            acc = corr / (corr + fls) * 100.0 if (corr + fls) else 100.0
            prefix = medals.get(idx, f"`{idx:02d}.`")
            lines.append(f"{prefix} <@{uid}> — **{corr:,}** gezählt (`{acc:.1f}%` • `{fls}` ❌)")

        embed = discord.Embed(
            title=f"🏆 Counting-Rangliste – {interaction.guild.name}",
            description="\n".join(lines),
            color=COLOR_GOLD,
        )
        embed.set_footer(
            text=f"Server-Highscore: {g.get('highscore', 0):,} • Aktueller Stand: {g.get('current', 0):,}"
        )
        await interaction.response.send_message(embed=embed)


class CountingCog(commands.Cog, name="Counting"):
    """Überwacht den Counting-Kanal in Echtzeit."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.counting_group = CountingGroup()
        self.bot.tree.add_command(self.counting_group)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.counting_group.name, type=self.counting_group.type)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        g = counting_store.get_guild(message.guild.id)
        if not g.get("enabled") or not g.get("channel_id"):
            return
        if message.channel.id != int(g["channel_id"]):
            return

        allow_math = bool(g.get("math_enabled", False))
        parsed_num = parse_counting_message(message.content, allow_math)
        if parsed_num is None:
            if g.get("numbers_only", True):
                try:
                    await message.delete()
                except Exception:
                    pass
            return

        expected = int(g.get("current") or 0) + 1
        last_uid = int(g.get("last_user_id") or 0)
        uid = message.author.id
        uid_s = str(uid)

        # Fehlerprüfung: 1) Zweimal hintereinander derselbe User, 2) Falsche Zahl / Zahl übersprungen / doppelt
        fail_reason: str | None = None
        if last_uid != 0 and uid == last_uid:
            fail_reason = (
                f"{message.author.mention} hat **zweimal hintereinander** gezählt! "
                "Es müssen sich immer mindestens **2 User abwechseln**."
            )
        elif parsed_num != expected:
            fail_reason = (
                f"{message.author.mention} hat **{parsed_num:,}** geschrieben, "
                f"aber erwartet war **{expected:,}**!"
            )

        if fail_reason is not None:
            old_streak = int(g.get("current") or 0)

            def _record_fail(gd: dict[str, Any]) -> int:
                gd["current"] = 0
                gd["last_user_id"] = 0
                gd["last_message_id"] = 0
                u_rec = gd["users"].setdefault(uid_s, {"correct": 0, "fails": 0, "highest": 0})
                u_rec["fails"] = int(u_rec.get("fails") or 0) + 1
                return int(gd.get("highscore") or 0)

            hs = counting_store.mutate(message.guild.id, _record_fail)
            try:
                await message.add_reaction("❌")
            except Exception:
                pass
            embed = discord.Embed(
                title="💥 Reihe abgebrochen! Zähler zurück auf 0!",
                description=(
                    f"{fail_reason}\n\n"
                    f"📉 **Erreichter Stand:** `{old_streak:,}`\n"
                    f"🏆 **Server-Highscore:** `{hs:,}`\n\n"
                    "👉 **Die nächste Zahl ist wieder `1`!**"
                ),
                color=COLOR_ERROR,
            )
            await message.channel.send(embed=embed)
            return

        # Gültige Zahl!
        def _record_ok(gd: dict[str, Any]) -> bool:
            gd["current"] = expected
            gd["last_user_id"] = uid
            gd["last_message_id"] = message.id
            gd["total_counts"] = int(gd.get("total_counts") or 0) + 1
            u_rec = gd["users"].setdefault(uid_s, {"correct": 0, "fails": 0, "highest": 0})
            u_rec["correct"] = int(u_rec.get("correct") or 0) + 1
            if expected > int(u_rec.get("highest") or 0):
                u_rec["highest"] = expected
            is_new_hs = expected > int(gd.get("highscore") or 0)
            if is_new_hs:
                gd["highscore"] = expected
                gd["highscore_by"] = uid
                gd["highscore_at"] = datetime.date.today().isoformat()
            return is_new_hs

        is_new_record = counting_store.mutate(message.guild.id, _record_ok)
        reaction = "☑️" if is_new_record else "✅"
        if expected in (100, 250, 500, 1000, 2500, 5000, 10000):
            reaction = "🎉"
        try:
            await message.add_reaction(reaction)
            if is_new_record and expected in (50, 100, 250, 500, 1000):
                await message.add_reaction("🏆")
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        g = counting_store.get_guild(message.guild.id)
        if not g.get("enabled") or message.channel.id != int(g.get("channel_id") or 0):
            return
        if message.id == int(g.get("last_message_id") or 0) and int(g.get("current") or 0) > 0:
            cur = int(g["current"])
            embed = discord.Embed(
                description=(
                    f"⚠️ {message.author.mention} hat die gültige Zahl **`{cur:,}`** gelöscht!\n"
                    f"👉 **Die nächste Zahl ist `{cur + 1:,}`** *(nicht nochmal `{cur:,}` schreiben!)*."
                ),
                color=COLOR_WARNING,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        g = counting_store.get_guild(after.guild.id)
        if not g.get("enabled") or after.channel.id != int(g.get("channel_id") or 0):
            return
        if after.id == int(g.get("last_message_id") or 0) and int(g.get("current") or 0) > 0:
            cur = int(g["current"])
            embed = discord.Embed(
                description=(
                    f"⚠️ {after.author.mention} hat die Nachricht für **`{cur:,}`** nachträglich bearbeitet!\n"
                    f"👉 **Der Stand bleibt `{cur:,}` — die nächste Zahl ist `{cur + 1:,}`!**"
                ),
                color=COLOR_WARNING,
            )
            try:
                await after.channel.send(embed=embed)
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CountingCog(bot))
