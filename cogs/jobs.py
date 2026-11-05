"""Jobs, Fishing & Shop Cog: Angeln, 5 Berufe (inkl. Schmiede- & Koch-Minigames) & Twitch-Shop."""
from __future__ import annotations

import asyncio
import datetime
import random
import time
from typing import Any
import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import is_fishing_channel, send_log
from bot_core.config import (
    BAIT_PACKS,
    COLOR_ERROR,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_TWITCH,
    COLOR_WARNING,
    COOKING_ALL_INGREDIENTS,
    COOKING_COOLDOWN_UPGRADES,
    COOKING_RECIPES,
    COOKING_TOOLS,
    COOLDOWN_UPGRADES,
    FISH_LIST,
    FISH_RARITY_COLORS,
    FISH_RARITY_NAMES,
    FISHING_RODS,
    HUNTING_ANIMALS,
    HUNTING_BOWS,
    HUNTING_COOLDOWN_UPGRADES,
    MINING_COOLDOWN_UPGRADES,
    MINING_ORES,
    MINING_PICKAXES,
    SMITHING_COOLDOWN_UPGRADES,
    SMITHING_HAMMERS,
    SMITHING_ITEMS,
    STREAMER_CHANNEL,
    WOODCUTTING_AXES,
    WOODCUTTING_COOLDOWN_UPGRADES,
    WOODCUTTING_TREES,
)
from bot_core.storage import (
    get_balance,
    get_fishing_data,
    get_job_data,
    load_bank,
    load_data,
    save_fishing_data,
    save_job_data,
    update_balance,
)
from bot_core.twitch_service import get_cached_rewards


# ── Auswahl-Algorithmen für Angeln & Sammel-Berufe ─────────────────────────

def _catch_fish(rod_key: str, has_bait: bool) -> dict[str, Any]:
    rod = FISHING_RODS.get(rod_key, FISHING_RODS["basic"])
    bait_mult = 1.5 if has_bait else 1.0
    weights = []
    for fish in FISH_LIST:
        w = float(fish["weight"])
        r = fish["rarity"]
        if r == "junk":
            w *= rod["junk_reduce"] * (0.5 if has_bait else 1.0)
        elif r in ("rare", "epic", "legendary"):
            w *= rod["rare_bonus"] * bait_mult
        elif r == "uncommon":
            w *= (1.0 + (rod["rare_bonus"] - 1.0) * 0.5) * (1.2 if has_bait else 1.0)
        weights.append(max(0.1, w))

    picked = random.choices(FISH_LIST, weights=weights, k=1)[0]
    raw_val = random.randint(picked["min_val"], picked["max_val"])
    final_val = max(1, int(raw_val * rod["value_bonus"]))
    return {**picked, "value": final_val}


def _pick_job_item(items: list[dict[str, Any]], tool_key: str, tools: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tool = tools.get(tool_key, next(iter(tools.values())))
    weights = []
    for item in items:
        w = float(item["weight"])
        if item["rarity"] in ("rare", "epic", "legendary"):
            w *= tool.get("rare_bonus", 1.0)
        elif item["rarity"] == "uncommon":
            w *= 1.0 + (tool.get("rare_bonus", 1.0) - 1.0) * 0.5
        weights.append(max(0.1, w))

    picked = random.choices(items, weights=weights, k=1)[0]
    raw_val = random.randint(picked["min_val"], picked["max_val"])
    final_val = max(1, int(raw_val * tool.get("bonus", 1.0)))
    return {**picked, "value": final_val}


def _get_job_cooldown(job_data: dict[str, Any], cd_map: dict[int, dict[str, Any]]) -> int:
    lvl = int(job_data.get("cooldown_level", 0))
    return int(cd_map.get(lvl, cd_map[0])["cooldown"])


# ── Interaktive Minigame-Views: Schmieden & Kochen ─────────────────────────

class SmithingView(discord.ui.View):
    DECOYS = ["🪨", "💨", "❄️", "💧", "🪵", "🧱"]

    def __init__(self, user_id: int, item: dict[str, Any], hammer: dict[str, Any]) -> None:
        super().__init__(timeout=30)
        self.user_id = user_id
        self.item = item
        self.hammer = hammer
        self.target_emoji = "🔥"
        self.required_hits = int(item["hits"])
        self.hits = 0
        self.mistakes = 0
        self.start_ts = time.monotonic()
        self.finished = False
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        if self.finished:
            return
        pool = random.sample(self.DECOYS, 4) + [self.target_emoji]
        random.shuffle(pool)
        for idx, em in enumerate(pool):
            btn = discord.ui.Button(emoji=em, style=discord.ButtonStyle.secondary, row=0)
            btn.callback = self._make_cb(em)
            self.add_item(btn)

    def _make_cb(self, clicked_emoji: str):
        async def _cb(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id or self.finished:
                await interaction.response.send_message("❌ Nicht deine Schmiede!", ephemeral=True)
                return

            if clicked_emoji == self.target_emoji:
                self.hits += 1
            else:
                self.mistakes += 1
                self.hits = max(0, self.hits - 1)

            if self.hits >= self.required_hits:
                self.finished = True
                self.stop()
                elapsed = max(1.0, time.monotonic() - self.start_ts)
                speed_mult = 1.6 if elapsed < 6 else (1.3 if elapsed < 10 else (1.0 if elapsed < 16 else 0.8))
                acc_mult = max(0.6, 1.0 - self.mistakes * 0.15)
                val = max(10, int(self.item["base_val"] * self.hammer["bonus"] * speed_mult * acc_mult))

                jd = get_job_data(self.user_id, "smithing")
                jd["total_actions"] = jd.get("total_actions", 0) + 1
                jd["total_earned"] = jd.get("total_earned", 0) + val
                if val > jd.get("best_value", 0):
                    jd["best_value"] = val
                    jd["best_name"] = self.item["name"]
                if speed_mult >= 1.6 and self.mistakes == 0:
                    jd["legendary_count"] = jd.get("legendary_count", 0) + 1
                save_job_data(self.user_id, "smithing", jd)
                update_balance(self.user_id, val)

                quality = "🌟 MEISTERWERK" if (speed_mult >= 1.6 and self.mistakes == 0) else (
                    "✨ Sehr gut" if speed_mult >= 1.3 else "✅ Solide"
                )
                embed = discord.Embed(
                    title=f"⚒️ {self.item['emoji']} {self.item['name']} geschmiedet!",
                    description=(
                        f"**Qualität:** {quality} ({elapsed:.1f}s • {self.mistakes} Fehler)\n"
                        f"💰 **Lohn:** **{val:,} 🪙**"
                    ),
                    color=COLOR_GOLD if "MEISTERWERK" in quality else COLOR_SUCCESS,
                )
                embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                await interaction.response.edit_message(embed=embed, view=None)
                return

            self._rebuild()
            embed = discord.Embed(
                title=f"⚒️ Schmieden – {self.item['emoji']} {self.item['name']}",
                description=(
                    f"Triff **{self.target_emoji}**!\n"
                    f"🔨 Fortschritt: **{self.hits}/{self.required_hits}** *(Fehler: {self.mistakes})*"
                ),
                color=COLOR_INFO,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return _cb


class CookingView(discord.ui.View):
    def __init__(self, user_id: int, recipe: dict[str, Any], tool: dict[str, Any]) -> None:
        super().__init__(timeout=40)
        self.user_id = user_id
        self.recipe = recipe
        self.tool = tool
        self.selected: list[str] = []
        self.finished = False

        needed = list(recipe["ingredients"])
        others = [i for i in COOKING_ALL_INGREDIENTS if i not in needed]
        choices = needed + random.sample(others, min(len(others), max(0, 8 - len(needed))))
        random.shuffle(choices)

        for idx, ing in enumerate(choices[:8]):
            btn = discord.ui.Button(emoji=ing, style=discord.ButtonStyle.secondary, row=idx // 4)
            btn.callback = self._make_ing_cb(ing)
            self.add_item(btn)

        done_btn = discord.ui.Button(label="✅ Servieren!", style=discord.ButtonStyle.success, row=2)
        done_btn.callback = self._finish_cb
        self.add_item(done_btn)

    def _make_ing_cb(self, ing: str):
        async def _cb(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id or self.finished:
                await interaction.response.send_message("❌ Nicht deine Küche!", ephemeral=True)
                return
            if ing in self.selected:
                self.selected.remove(ing)
            else:
                self.selected.append(ing)
            chosen_str = " ".join(self.selected) if self.selected else "*noch nichts*"
            embed = discord.Embed(
                title=f"🍳 Kochen – {self.recipe['emoji']} {self.recipe['name']}",
                description=(
                    f"📝 **Gewählte Zutaten:** {chosen_str}\n"
                    f"📊 **{len(self.selected)}/{len(self.recipe['ingredients'])}** Zutaten ausgewählt"
                ),
                color=COLOR_INFO,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return _cb

    async def _finish_cb(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id or self.finished:
            return
        self.finished = True
        self.stop()

        target_set = set(self.recipe["ingredients"])
        picked_set = set(self.selected)
        correct = len(target_set & picked_set)
        wrong = len(picked_set - target_set)
        ratio = max(0.0, (correct - wrong * 0.5) / max(1, len(target_set)))

        if ratio <= 0:
            embed = discord.Embed(
                title="🔥 Angebrannt!",
                description=(
                    f"Die Zutaten passten leider gar nicht!\n"
                    f"Richtig gewesen wäre: {' '.join(self.recipe['ingredients'])}"
                ),
                color=COLOR_ERROR,
            )
            await interaction.response.edit_message(embed=embed, view=None)
            return

        perfect = picked_set == target_set and len(self.selected) == len(self.recipe["ingredients"])
        mult = 1.5 if perfect else ratio
        val = max(5, int(self.recipe["base_val"] * self.tool["bonus"] * mult))

        jd = get_job_data(self.user_id, "cooking")
        jd["total_actions"] = jd.get("total_actions", 0) + 1
        jd["total_earned"] = jd.get("total_earned", 0) + val
        if val > jd.get("best_value", 0):
            jd["best_value"] = val
            jd["best_name"] = self.recipe["name"]
        if perfect:
            jd["legendary_count"] = jd.get("legendary_count", 0) + 1
        save_job_data(self.user_id, "cooking", jd)
        update_balance(self.user_id, val)

        embed = discord.Embed(
            title=f"{'👨‍🍳 PERFEKT!' if perfect else '🍽️ Serviert!'} {self.recipe['emoji']} {self.recipe['name']}",
            description=(
                f"Treffer: **{correct}/{len(target_set)}** Zutaten\n"
                f"💰 **Verdient:** **{val:,} 🪙**"
            ),
            color=COLOR_GOLD if perfect else COLOR_SUCCESS,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
        await interaction.response.edit_message(embed=embed, view=None)


# ── Twitch Reward Redemption View ──────────────────────────────────────────

class RedeemSelectView(discord.ui.View):
    def __init__(self, user: discord.abc.User, rewards: list[dict[str, Any]], bot: commands.Bot) -> None:
        super().__init__(timeout=60)
        self.user = user
        self.rewards = rewards
        self.bot = bot
        options = [
            discord.SelectOption(
                label=r["title"][:90],
                description=f"Preis: {r['coin_cost']:,} Coins ({r['twitch_cost']:,} Kanalpunkte)",
                value=str(idx),
            )
            for idx, r in enumerate(rewards[:25])
        ]
        self.picker = discord.ui.Select(placeholder="🎁 Twitch-Belohnung zum Einlösen wählen...", options=options)
        self.picker.callback = self._on_pick
        self.add_item(self.picker)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Nicht dein Menü!", ephemeral=True)
            return
        reward = self.rewards[int(self.picker.values[0])]
        cost = int(reward["coin_cost"])
        bal = get_balance(self.user.id)
        if bal < cost:
            await interaction.response.send_message(
                f"❌ Nicht genug Coins! Du benötigst **{cost:,} 🪙** (Du hast **{bal:,} 🪙**).",
                ephemeral=True,
            )
            return

        update_balance(self.user.id, -cost)
        data = load_data()
        t_name = data.get("users", {}).get(str(self.user.id), {}).get("twitch_name", "Nicht verknüpft")

        embed = discord.Embed(
            title="🎉 Twitch-Belohnung eingelöst!",
            description=(
                f"**Belohnung:** {reward['title']}\n"
                f"**Preis:** {cost:,} 🪙\n"
                f"**Verbleibend:** {get_balance(self.user.id):,} 🪙\n\n"
                f"Das Mod-Team / der Streamer wurde automatisch im Log benachrichtigt!"
            ),
            color=COLOR_TWITCH,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

        log_embed = discord.Embed(
            title="🎁 Shop-Einlösung (Twitch-Reward)",
            color=COLOR_TWITCH,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        log_embed.add_field(name="👤 Discord", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="🟣 Twitch", value=f"`{t_name}`", inline=True)
        log_embed.add_field(name="💰 Bezahlt", value=f"`{cost:,} 🪙`", inline=True)
        log_embed.add_field(name="🏆 Belohnung", value=f"**{reward['title']}**", inline=False)
        await send_log(log_embed, self.bot, interaction.guild_id)


# ═══════════════════════════════════════════════════════════════════════════
# JOBS & FISHING COG
# ═══════════════════════════════════════════════════════════════════════════

WOOD_ANIMATIONS = [
    ["🌳🪓", "🌳💨🪓", "🌳🪵💨", "🪵🪵✅"],
    ["🪓🌲", "🪓💥🌲", "🪓🌲💨", "🪵✅"],
]
MINE_ANIMATIONS = [
    ["⛏️🪨", "⛏️💥🪨", "⛏️💎💨", "💎✅"],
    ["🪨⛏️", "🪨💥⛏️", "✨🪨⛏️", "💎✨"],
]
HUNT_ANIMATIONS = [
    ["🏹👀", "🏹🌿👀", "🏹💨🎯", "🎯✅"],
    ["👀🌲", "🏹🌲", "🏹💨", "🎯🦌"],
]


class JobsCog(commands.Cog, name="Jobs"):
    """Angeln, Berufe, Upgrades & Twitch-Kanalpunkte-Shop."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Fishing Commands ──────────────────────────────────────────────────

    @app_commands.command(name="fish", description="🎣 Wirf deine Angel aus und fange Fische!")
    @is_fishing_channel()
    async def cmd_fish(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        fd = get_fishing_data(uid)
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        cd_lvl = int(fd.get("cooldown_level", 0))
        cooldown = COOLDOWN_UPGRADES.get(cd_lvl, COOLDOWN_UPGRADES[0])["cooldown"]

        last = float(fd.get("last_fish_time", 0))
        if now - last < cooldown:
            nxt = int(last + cooldown)
            await interaction.response.send_message(
                f"⏳ Deine Schnur wird noch eingeholt! Nächster Wurf <t:{nxt}:R>.",
                ephemeral=True,
            )
            return

        has_bait = int(fd.get("bait", 0)) > 0
        if has_bait:
            fd["bait"] = int(fd["bait"]) - 1
        fd["last_fish_time"] = now
        save_fishing_data(uid, fd)

        rod_key = fd.get("rod", "basic")
        rod = FISHING_RODS.get(rod_key, FISHING_RODS["basic"])

        embed = discord.Embed(
            title=f"🎣 {interaction.user.display_name} angelt...",
            description=f"**Rute:** {rod['name']}" + (" • 🪱 *Köder aktiv!*" if has_bait else "") + "\n\n🌊 🎣 ...",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed)
        await asyncio.sleep(1.3)

        caught = _catch_fish(rod_key, has_bait)
        fd = get_fishing_data(uid)
        fd["total_caught"] = int(fd.get("total_caught", 0)) + 1
        fd["total_earned"] = int(fd.get("total_earned", 0)) + caught["value"]
        if caught["value"] > int(fd.get("biggest_catch", 0)):
            fd["biggest_catch"] = caught["value"]
            fd["biggest_catch_name"] = caught["name"]
        if caught["rarity"] == "legendary":
            fd["legendary_caught"] = int(fd.get("legendary_caught", 0)) + 1

        inv = fd.setdefault("inventory", {})
        inv_entry = inv.setdefault(
            caught["name"],
            {"emoji": caught["emoji"], "count": 0, "total_value": 0, "rarity": caught["rarity"]},
        )
        inv_entry["count"] = int(inv_entry.get("count", 0)) + 1
        inv_entry["total_value"] = int(inv_entry.get("total_value", 0)) + caught["value"]

        save_fishing_data(uid, fd)
        new_bal = update_balance(uid, caught["value"])

        nxt_ts = int(now + cooldown)
        r_color = FISH_RARITY_COLORS.get(caught["rarity"], COLOR_INFO)
        r_name = FISH_RARITY_NAMES.get(caught["rarity"], "")

        embed = discord.Embed(
            title=f"{'🌟 LEGENDÄR! ' if caught['rarity'] == 'legendary' else ''}{caught['emoji']} {caught['name']} gefangen!",
            description=(
                f"💰 **Verkaufswert:** `{caught['value']:,} 🪙`\n"
                f"📊 **Seltenheit:** {r_name}\n"
                f"🪱 **Köder übrig:** `{fd.get('bait', 0)}`\n\n"
                f"⏱️ Nächster Wurf: <t:{nxt_ts}:R>"
            ),
            color=r_color,
        )
        embed.set_footer(text=f"Kontostand: {new_bal:,} 🪙 • {rod['name']}")
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="fishstats", description="📊 Zeigt deine Angel-Statistiken & Ausrüstung")
    @is_fishing_channel()
    async def cmd_fishstats(self, interaction: discord.Interaction) -> None:
        fd = get_fishing_data(interaction.user.id)
        rod_key = fd.get("rod", "basic")
        rod = FISHING_RODS.get(rod_key, FISHING_RODS["basic"])
        cd_lvl = int(fd.get("cooldown_level", 0))
        cd_info = COOLDOWN_UPGRADES.get(cd_lvl, COOLDOWN_UPGRADES[0])

        embed = discord.Embed(
            title=f"🎣 Angel-Statistiken – {interaction.user.display_name}",
            color=COLOR_INFO,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="🎣 Rute", value=rod["name"], inline=True)
        embed.add_field(name="⏱️ Cooldown", value=f"**{cd_info['cooldown']}s** (Stufe {cd_lvl})", inline=True)
        embed.add_field(name="🪱 Köder", value=f"**{fd.get('bait', 0)}**", inline=True)
        embed.add_field(name="🐟 Gefangen", value=f"**{fd.get('total_caught', 0):,}**", inline=True)
        embed.add_field(name="💰 Verdient", value=f"**{fd.get('total_earned', 0):,} 🪙**", inline=True)
        embed.add_field(name="🌟 Legendäre", value=f"**{fd.get('legendary_caught', 0)}**", inline=True)
        embed.add_field(
            name="🏆 Rekordfang",
            value=f"**{fd.get('biggest_catch_name', 'Noch nichts')}** ({fd.get('biggest_catch', 0):,} 🪙)",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="upgrade", description="⬆️ Kaufe die nächstbessere Angelrute!")
    @is_fishing_channel()
    async def cmd_upgrade(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        fd = get_fishing_data(uid)
        order = list(FISHING_RODS.keys())
        cur_idx = order.index(fd.get("rod", "basic")) if fd.get("rod", "basic") in order else 0
        if cur_idx >= len(order) - 1:
            await interaction.response.send_message("🌟 Du besitzt bereits die beste Angelrute!", ephemeral=True)
            return

        nxt_key = order[cur_idx + 1]
        nxt_rod = FISHING_RODS[nxt_key]
        bal = get_balance(uid)
        if bal < nxt_rod["cost"]:
            await interaction.response.send_message(
                f"❌ **{nxt_rod['name']}** kostet **{nxt_rod['cost']:,} 🪙** *(Du hast {bal:,} 🪙)*.",
                ephemeral=True,
            )
            return

        update_balance(uid, -nxt_rod["cost"])
        fd["rod"] = nxt_key
        save_fishing_data(uid, fd)
        embed = discord.Embed(
            title="⬆️ Neue Angelrute gekauft!",
            description=(
                f"Du angelst jetzt mit **{nxt_rod['name']}**!\n"
                f"📈 Wert-Bonus: `×{nxt_rod['value_bonus']}` • Seltenheits-Bonus: `×{nxt_rod['rare_bonus']}`"
            ),
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="upgradecooldown", description="⏱️ Verkürze deinen Angel-Cooldown!")
    @is_fishing_channel()
    async def cmd_upgradecooldown(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        fd = get_fishing_data(uid)
        cur_lvl = int(fd.get("cooldown_level", 0))
        max_lvl = max(COOLDOWN_UPGRADES.keys())
        if cur_lvl >= max_lvl:
            await interaction.response.send_message("🌟 Maximales Cooldown-Level bereits erreicht!", ephemeral=True)
            return

        nxt = COOLDOWN_UPGRADES[cur_lvl + 1]
        bal = get_balance(uid)
        if bal < nxt["cost"]:
            await interaction.response.send_message(
                f"❌ **{nxt['name']}** kostet **{nxt['cost']:,} 🪙** *(Du hast {bal:,} 🪙)*.",
                ephemeral=True,
            )
            return

        update_balance(uid, -nxt["cost"])
        fd["cooldown_level"] = cur_lvl + 1
        save_fishing_data(uid, fd)
        embed = discord.Embed(
            title="⏱️ Angel-Cooldown verbessert!",
            description=f"Neuer Cooldown: **{nxt['cooldown']} Sekunden** ({nxt['name']})!",
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="buybait", description="🪱 Kaufe Köder für höhere Chancen auf seltene Fische!")
    @app_commands.describe(paket="Größe des Köderpakets")
    @app_commands.choices(
        paket=[
            app_commands.Choice(name="🪱 Klein (5 Köder – 100 🪙)", value="small"),
            app_commands.Choice(name="🪱 Mittel (15 Köder – 250 🪙)", value="medium"),
            app_commands.Choice(name="🪱 Groß (40 Köder – 600 🪙)", value="large"),
        ]
    )
    @is_fishing_channel()
    async def cmd_buybait(self, interaction: discord.Interaction, paket: str) -> None:
        uid = interaction.user.id
        pack = BAIT_PACKS[paket]
        if get_balance(uid) < pack["cost"]:
            await interaction.response.send_message(
                f"❌ Du benötigst **{pack['cost']:,} 🪙**!", ephemeral=True
            )
            return
        update_balance(uid, -pack["cost"])
        fd = get_fishing_data(uid)
        fd["bait"] = int(fd.get("bait", 0)) + pack["amount"]
        save_fishing_data(uid, fd)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🪱 Köder gekauft!",
                description=f"**+{pack['amount']} Köder** hinzugefügt! (Gesamt: **{fd['bait']}**)",
                color=COLOR_SUCCESS,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="fishtop", description="🏆 Top-10 der erfolgreichsten Angler")
    @is_fishing_channel()
    async def cmd_fishtop(self, interaction: discord.Interaction) -> None:
        bank = load_bank()
        anglers = []
        for k, v in bank.items():
            if k.endswith("_fishing") and isinstance(v, dict):
                uid_str = k.replace("_fishing", "")
                if uid_str.isdigit():
                    anglers.append((int(uid_str), int(v.get("total_earned", 0)), int(v.get("total_caught", 0))))
        anglers.sort(key=lambda x: x[1], reverse=True)
        if not anglers:
            await interaction.response.send_message("Noch keine Fänge verzeichnet!", ephemeral=True)
            return

        lines = []
        for idx, (uid, earned, caught) in enumerate(anglers[:10], 1):
            m = interaction.guild.get_member(uid) if interaction.guild else None
            name = m.display_name if m else f"User {uid}"
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"`#{idx}`")
            lines.append(f"{medal} **{name}** — `{earned:,} 🪙` ({caught:,} Fische)")

        await interaction.response.send_message(
            embed=discord.Embed(title="🏆 Angler-Rangliste", description="\n".join(lines), color=COLOR_GOLD)
        )

    # ── Shop & Twitch Rewards ─────────────────────────────────────────────

    @app_commands.command(name="shop", description="🛒 Zeigt Upgrades, Köder & einlösbare Twitch-Belohnungen")
    async def cmd_shop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rewards = await get_cached_rewards()
        embed = discord.Embed(
            title="🛒 Community-Shop",
            description=f"Dein Kontostand: **{get_balance(interaction.user.id):,} 🪙**",
            color=COLOR_GOLD,
        )
        embed.add_field(
            name="🎣 Angel- & Job-Upgrades",
            value=(
                "`/upgrade` & `/upgradecooldown` — Angelrute & Tempo verbessern\n"
                "`/buybait` — Köderpakete für seltene Fänge\n"
                "`/jobupgrade <job>` & `/jobcooldown <job>` — Berufs-Werkzeuge verbessern"
            ),
            inline=False,
        )
        if rewards:
            r_lines = [
                f"🟣 **{r['title']}** — `{r['coin_cost']:,} 🪙` *({r['twitch_cost']:,} KP)*"
                for r in rewards[:12]
            ]
            embed.add_field(
                name=f"🎁 Twitch-Kanalpunkte-Belohnungen ({STREAMER_CHANNEL})",
                value="\n".join(r_lines) + "\n\n👉 Einlösen mit `/redeem`!",
                inline=False,
            )
        else:
            embed.add_field(
                name="🎁 Twitch-Belohnungen",
                value="*Aktuell keine aktiven Twitch-Rewards geladen oder Token nicht gesetzt.*",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="redeem", description="🎁 Löse deine Coins gegen echte Twitch-Belohnungen ein!")
    async def cmd_redeem(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rewards = await get_cached_rewards()
        if not rewards:
            await interaction.followup.send(
                "❌ Derzeit sind keine einlösbaren Twitch-Belohnungen verfügbar.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="🎁 Twitch-Belohnung einlösen",
            description=(
                f"Dein Kontostand: **{get_balance(interaction.user.id):,} 🪙**\n"
                "Wähle unten im Menü die gewünschte Stream-Belohnung aus:"
            ),
            color=COLOR_TWITCH,
        )
        await interaction.followup.send(
            embed=embed,
            view=RedeemSelectView(interaction.user, rewards, self.bot),
            ephemeral=True,
        )

    # ── 5 Berufe (Jobs) ───────────────────────────────────────────────────

    async def _run_gather_job(
        self,
        interaction: discord.Interaction,
        job_key: str,
        job_name: str,
        job_emoji: str,
        items: list[dict[str, Any]],
        tools: dict[str, dict[str, Any]],
        cd_upgrades: dict[int, dict[str, Any]],
        animations: list[list[str]],
    ) -> None:
        uid = interaction.user.id
        jd = get_job_data(uid, job_key)
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        cooldown = _get_job_cooldown(jd, cd_upgrades)
        last = float(jd.get("last_action_time", 0))
        if now - last < cooldown:
            await interaction.response.send_message(
                f"⏳ Du kannst <t:{int(last + cooldown)}:R> wieder {job_name.lower()}!",
                ephemeral=True,
            )
            return

        jd["last_action_time"] = now
        save_job_data(uid, job_key, jd)

        tool_key = jd.get("tool", next(iter(tools.keys())))
        tool = tools.get(tool_key, next(iter(tools.values())))

        frames = random.choice(animations)
        embed = discord.Embed(
            title=f"{job_emoji} {interaction.user.display_name} {job_name.lower()}t...",
            description=f"**Werkzeug:** {tool['name']}\n\n{frames[0]}",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed)
        for frame in frames[1:]:
            await asyncio.sleep(0.65)
            embed.description = f"**Werkzeug:** {tool['name']}\n\n{frame}"
            await interaction.edit_original_response(embed=embed)

        res = _pick_job_item(items, tool_key, tools)
        jd = get_job_data(uid, job_key)
        jd["total_earned"] = int(jd.get("total_earned", 0)) + res["value"]
        jd["total_actions"] = int(jd.get("total_actions", 0)) + 1
        if res["value"] > int(jd.get("best_value", 0)):
            jd["best_value"] = res["value"]
            jd["best_name"] = res["name"]
        if res["rarity"] == "legendary":
            jd["legendary_count"] = int(jd.get("legendary_count", 0)) + 1

        save_job_data(uid, job_key, jd)
        new_bal = update_balance(uid, res["value"])

        embed = discord.Embed(
            title=f"{'🌟 LEGENDÄR! ' if res['rarity'] == 'legendary' else ''}{res['emoji']} {res['name']} erhalten!",
            description=(
                f"💰 **Wert:** `{res['value']:,} 🪙`\n"
                f"📊 **Seltenheit:** {FISH_RARITY_NAMES.get(res['rarity'], '')}\n\n"
                f"⏱️ Nächster Einsatz: <t:{int(now + cooldown)}:R>"
            ),
            color=FISH_RARITY_COLORS.get(res["rarity"], COLOR_INFO),
        )
        embed.set_footer(text=f"Kontostand: {new_bal:,} 🪙 • {tool['name']}")
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="chop", description="🪓 Bäume fällen und Holz sammeln!")
    @is_fishing_channel()
    async def cmd_chop(self, interaction: discord.Interaction) -> None:
        await self._run_gather_job(
            interaction, "woodcutting", "Holzfällen", "🪓",
            WOODCUTTING_TREES, WOODCUTTING_AXES, WOODCUTTING_COOLDOWN_UPGRADES, WOOD_ANIMATIONS,
        )

    @app_commands.command(name="mine", description="⛏️ Erze abbauen und Edelsteine sammeln!")
    @is_fishing_channel()
    async def cmd_mine(self, interaction: discord.Interaction) -> None:
        await self._run_gather_job(
            interaction, "mining", "Bergbau", "⛏️",
            MINING_ORES, MINING_PICKAXES, MINING_COOLDOWN_UPGRADES, MINE_ANIMATIONS,
        )

    @app_commands.command(name="hunt", description="🏹 Auf die Jagd gehen!")
    @is_fishing_channel()
    async def cmd_hunt(self, interaction: discord.Interaction) -> None:
        await self._run_gather_job(
            interaction, "hunting", "Jagen", "🏹",
            HUNTING_ANIMALS, HUNTING_BOWS, HUNTING_COOLDOWN_UPGRADES, HUNT_ANIMATIONS,
        )

    @app_commands.command(name="smith", description="⚒️ Schmiede Gegenstände! (Reaktionsspiel)")
    @is_fishing_channel()
    async def cmd_smith(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        jd = get_job_data(uid, "smithing")
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        cooldown = _get_job_cooldown(jd, SMITHING_COOLDOWN_UPGRADES)
        last = float(jd.get("last_action_time", 0))
        if now - last < cooldown:
            await interaction.response.send_message(
                f"⏳ Du kannst <t:{int(last + cooldown)}:R> wieder schmieden!", ephemeral=True
            )
            return

        jd["last_action_time"] = now
        save_job_data(uid, "smithing", jd)
        hammer = SMITHING_HAMMERS.get(jd.get("tool", "stone"), SMITHING_HAMMERS["stone"])
        item = random.choice(SMITHING_ITEMS)
        view = SmithingView(uid, item, hammer)
        embed = discord.Embed(
            title=f"⚒️ Schmieden – {item['emoji']} {item['name']}",
            description=(
                f"Klicke so schnell wie möglich auf **{view.target_emoji}**!\n"
                f"🔨 Schläge: **0/{view.required_hits}** • 🛠️ {hammer['name']}"
            ),
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="cook", description="🍳 Koche ein Gericht nach Rezept! (Merkspiel)")
    @is_fishing_channel()
    async def cmd_cook(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        jd = get_job_data(uid, "cooking")
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        cooldown = _get_job_cooldown(jd, COOKING_COOLDOWN_UPGRADES)
        last = float(jd.get("last_action_time", 0))
        if now - last < cooldown:
            await interaction.response.send_message(
                f"⏳ Du kannst <t:{int(last + cooldown)}:R> wieder kochen!", ephemeral=True
            )
            return

        jd["last_action_time"] = now
        save_job_data(uid, "cooking", jd)
        tool = COOKING_TOOLS.get(jd.get("tool", "basic"), COOKING_TOOLS["basic"])
        recipe = random.choice(COOKING_RECIPES)

        embed = discord.Embed(
            title=f"📖 Rezept: {recipe['emoji']} {recipe['name']}",
            description=(
                f"**Merke dir die Zutaten (3 Sekunden)!**\n\n"
                f"📝 {'  '.join(recipe['ingredients'])}"
            ),
            color=COLOR_WARNING,
        )
        await interaction.response.send_message(embed=embed)
        await asyncio.sleep(3.0)

        view = CookingView(uid, recipe, tool)
        embed2 = discord.Embed(
            title=f"🍳 Kochen – {recipe['emoji']} {recipe['name']}",
            description=f"Wähle die **{len(recipe['ingredients'])}** richtigen Zutaten und klicke auf **✅ Servieren!**",
            color=COLOR_INFO,
        )
        await interaction.edit_original_response(embed=embed2, view=view)

    JOB_CHOICES = [
        app_commands.Choice(name="🪓 Holzfällen", value="woodcutting"),
        app_commands.Choice(name="⛏️ Bergbau", value="mining"),
        app_commands.Choice(name="🏹 Jagen", value="hunting"),
        app_commands.Choice(name="⚒️ Schmieden", value="smithing"),
        app_commands.Choice(name="🍳 Kochen", value="cooking"),
    ]

    @app_commands.command(name="jobupgrade", description="⬆️ Verbessere dein Werkzeug für einen Beruf!")
    @app_commands.choices(job=JOB_CHOICES)
    @is_fishing_channel()
    async def cmd_jobupgrade(self, interaction: discord.Interaction, job: str) -> None:
        uid = interaction.user.id
        jd = get_job_data(uid, job)
        tools = {
            "woodcutting": WOODCUTTING_AXES,
            "mining": MINING_PICKAXES,
            "hunting": HUNTING_BOWS,
            "smithing": SMITHING_HAMMERS,
            "cooking": COOKING_TOOLS,
        }[job]
        order = list(tools.keys())
        cur_key = jd.get("tool", order[0])
        cur_idx = order.index(cur_key) if cur_key in order else 0
        if cur_idx >= len(order) - 1:
            await interaction.response.send_message("🌟 Du hast bereits das beste Werkzeug!", ephemeral=True)
            return

        nxt_key = order[cur_idx + 1]
        nxt_tool = tools[nxt_key]
        bal = get_balance(uid)
        if bal < nxt_tool["cost"]:
            await interaction.response.send_message(
                f"❌ **{nxt_tool['name']}** kostet **{nxt_tool['cost']:,} 🪙** *(Du hast {bal:,} 🪙)*.",
                ephemeral=True,
            )
            return

        update_balance(uid, -nxt_tool["cost"])
        jd["tool"] = nxt_key
        save_job_data(uid, job, jd)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⬆️ Werkzeug verbessert!",
                description=f"**{tools[cur_key]['name']}** → **{nxt_tool['name']}** (Bonus ×{nxt_tool['bonus']})",
                color=COLOR_SUCCESS,
            )
        )

    @app_commands.command(name="jobcooldown", description="⏱️ Verkürze den Cooldown eines Berufs!")
    @app_commands.choices(job=JOB_CHOICES)
    @is_fishing_channel()
    async def cmd_jobcooldown(self, interaction: discord.Interaction, job: str) -> None:
        uid = interaction.user.id
        jd = get_job_data(uid, job)
        cd_map = {
            "woodcutting": WOODCUTTING_COOLDOWN_UPGRADES,
            "mining": MINING_COOLDOWN_UPGRADES,
            "hunting": HUNTING_COOLDOWN_UPGRADES,
            "smithing": SMITHING_COOLDOWN_UPGRADES,
            "cooking": COOKING_COOLDOWN_UPGRADES,
        }[job]
        cur_lvl = int(jd.get("cooldown_level", 0))
        if cur_lvl >= max(cd_map.keys()):
            await interaction.response.send_message("🌟 Maximales Cooldown-Level erreicht!", ephemeral=True)
            return

        nxt = cd_map[cur_lvl + 1]
        if get_balance(uid) < nxt["cost"]:
            await interaction.response.send_message(
                f"❌ Kostet **{nxt['cost']:,} 🪙**!", ephemeral=True
            )
            return

        update_balance(uid, -nxt["cost"])
        jd["cooldown_level"] = cur_lvl + 1
        save_job_data(uid, job, jd)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⏱️ Cooldown verkürzt!",
                description=f"Neuer Cooldown: **{nxt['cooldown']}s** ({nxt['name']})",
                color=COLOR_SUCCESS,
            )
        )

    @app_commands.command(name="jobstats", description="📊 Zeigt deine Statistiken für einen Beruf")
    @app_commands.choices(job=JOB_CHOICES)
    @is_fishing_channel()
    async def cmd_jobstats(self, interaction: discord.Interaction, job: str) -> None:
        jd = get_job_data(interaction.user.id, job)
        tools = {
            "woodcutting": WOODCUTTING_AXES,
            "mining": MINING_PICKAXES,
            "hunting": HUNTING_BOWS,
            "smithing": SMITHING_HAMMERS,
            "cooking": COOKING_TOOLS,
        }[job]
        cd_map = {
            "woodcutting": WOODCUTTING_COOLDOWN_UPGRADES,
            "mining": MINING_COOLDOWN_UPGRADES,
            "hunting": HUNTING_COOLDOWN_UPGRADES,
            "smithing": SMITHING_COOLDOWN_UPGRADES,
            "cooking": COOKING_COOLDOWN_UPGRADES,
        }[job]
        names = {
            "woodcutting": "🪓 Holzfällen",
            "mining": "⛏️ Bergbau",
            "hunting": "🏹 Jagen",
            "smithing": "⚒️ Schmieden",
            "cooking": "🍳 Kochen",
        }
        t_key = jd.get("tool", next(iter(tools.keys())))
        tool = tools.get(t_key, next(iter(tools.values())))
        cd = cd_map.get(int(jd.get("cooldown_level", 0)), cd_map[0])

        embed = discord.Embed(title=f"📊 {names[job]} – Statistik", color=COLOR_INFO)
        embed.add_field(name="🛠️ Werkzeug", value=tool["name"], inline=True)
        embed.add_field(name="⏱️ Cooldown", value=f"**{cd['cooldown']}s**", inline=True)
        embed.add_field(name="📊 Aktionen", value=f"**{jd.get('total_actions', 0):,}**", inline=True)
        embed.add_field(name="💰 Verdient", value=f"**{jd.get('total_earned', 0):,} 🪙**", inline=True)
        embed.add_field(
            name="🏆 Rekord",
            value=f"**{jd.get('best_name', 'Noch nichts')}** ({jd.get('best_value', 0):,} 🪙)",
            inline=True,
        )
        embed.add_field(name="🌟 Meister/Legendär", value=f"**{jd.get('legendary_count', 0)}**", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="jobs", description="📋 Übersicht aller Berufe & Verdienstmöglichkeiten")
    @is_fishing_channel()
    async def cmd_jobs(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📋 Verfügbare Berufe",
            description="Verdiene Coins durch ehrliche Arbeit – verbessere Werkzeuge & Cooldowns!",
            color=COLOR_INFO,
        )
        embed.add_field(name="🪓 Holzfällen (`/chop`)", value="Sammle Edelhölzer.", inline=True)
        embed.add_field(name="⛏️ Bergbau (`/mine`)", value="Baue Erze & Diamanten ab.", inline=True)
        embed.add_field(name="🏹 Jagen (`/hunt`)", value="Spüre Wildtiere auf.", inline=True)
        embed.add_field(name="⚒️ Schmieden (`/smith`)", value="Reaktions-Minispiel.", inline=True)
        embed.add_field(name="🍳 Kochen (`/cook`)", value="Rezept-Gedächtnisspiel.", inline=True)
        embed.add_field(name="🎣 Angeln (`/fish`)", value="Fische & Schätze angeln.", inline=True)
        embed.add_field(
            name="⬆️ Upgrades & Stats",
            value="`/jobupgrade <job>` • `/jobcooldown <job>` • `/jobstats <job>`",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JobsCog(bot))
