"""Gambling & Multiplayer Cog: Economy, 12 Casino-Games & 4 PvP/Multiplayer-Games."""
from __future__ import annotations

import asyncio
import datetime
import random
import discord
from discord import app_commands
from discord.ext import commands

from bot_core.checks import is_admin_user, is_gambling_channel
from bot_core.config import (
    C4_COLS,
    C4_EMPTY,
    C4_RED,
    C4_ROWS,
    C4_YELLOW,
    CARD_RANKS,
    CARD_SUITS,
    COLOR_CONNECT4,
    COLOR_ERROR,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_RR,
    COLOR_SUCCESS,
    COLOR_TICTACTOE,
    COLOR_WARNING,
    CRASH_CHEAT,
    DAILY_REWARD,
    HORSES,
    MAX_BET,
    PLINKO_COLORS,
    PLINKO_MULTIPLIERS,
    PLINKO_ROWS,
    ROULETTE_BLACK,
    ROULETTE_RED,
    RR_CHAMBERS,
    RR_JOIN_TIMEOUT,
    RR_MAX_PLAYERS,
    RR_MIN_PLAYERS,
    SLOT_PAYOUTS,
    SLOT_SYMBOLS,
    TTT_EMPTY,
    TTT_O,
    TTT_X,
    today_berlin,
)
from bot_core.stats import bot_runtime, stats_tracker
from bot_core.storage import (
    generate_vault_code,
    get_balance,
    get_vault_data,
    load_bank,
    refund_interrupted_escrows,
    register_bet_escrow,
    release_bet_escrow,
    save_bank,
    save_vault,
    set_balance,
    update_balance,
)


class _EscrowActiveGames(dict):
    """Verknüpft laufende Spiele mit persistentem Einsatz-Escrow für 100% Schutz bei Bot-Neustarts."""

    def __init__(self) -> None:
        super().__init__()
        self._escrow_ids: dict[int, list[str]] = {}

    def add_escrow(self, user_id: int, amount: int, game_name: str) -> None:
        eid = register_bet_escrow(user_id, amount, game_name)
        if eid:
            self._escrow_ids.setdefault(user_id, []).append(eid)

    def pop(self, key: Any, *args: Any) -> Any:
        for eid in self._escrow_ids.pop(key, []):
            release_bet_escrow(eid)
        return super().pop(key, *args)


class _EscrowRRGames(dict):
    """Schützt Einsätze in Russisches-Roulette-Lobbys bei einem Bot-Neustart."""

    def __init__(self) -> None:
        super().__init__()
        self._escrows_by_ch: dict[int, list[str]] = {}

    def add_player_escrow(self, channel_id: int, user_id: int, amount: int) -> None:
        eid = register_bet_escrow(user_id, amount, "Russisches Roulette")
        if eid:
            self._escrows_by_ch.setdefault(channel_id, []).append(eid)

    def pop(self, key: Any, *args: Any) -> Any:
        for eid in self._escrows_by_ch.pop(key, []):
            release_bet_escrow(eid)
        return super().pop(key, *args)


active_games: _EscrowActiveGames = _EscrowActiveGames()
bot_runtime.active_games = active_games
active_rr_games: _EscrowRRGames = _EscrowRRGames()


def _record_net(user: discord.abc.User, net_delta: int) -> None:
    try:
        stats_tracker.record_gambling_net(user.id, getattr(user, "display_name", str(user.id)), net_delta)
    except Exception:
        pass


async def check_bet(interaction: discord.Interaction, einsatz: int) -> bool:
    if einsatz <= 0:
        await interaction.response.send_message("❌ Der Einsatz muss mindestens **1 🪙** betragen!", ephemeral=True)
        return False
    if einsatz > MAX_BET:
        await interaction.response.send_message(
            f"❌ Maximaler Einsatz ist **{MAX_BET:,} 🪙**!", ephemeral=True
        )
        return False
    bal = get_balance(interaction.user.id)
    if einsatz > bal:
        await interaction.response.send_message(
            f"❌ Nicht genug Coins! Du hast **{bal:,} 🪙**.", ephemeral=True
        )
        return False
    return True


async def check_active_game(interaction: discord.Interaction) -> bool:
    if interaction.user.id in active_games:
        await interaction.response.send_message(
            f"❌ Du spielst gerade schon **{active_games[interaction.user.id]}**!",
            ephemeral=True,
        )
        return False
    return True


# ── Karten- & Slot-Helper ──────────────────────────────────────────────────

def _spin_slot() -> str:
    symbols, weights = zip(*SLOT_SYMBOLS)
    return random.choices(symbols, weights=weights, k=1)[0]


def _make_deck() -> list[tuple[str, str, int]]:
    deck = [(rank, suit, val) for suit in CARD_SUITS for rank, val in CARD_RANKS]
    random.shuffle(deck)
    return deck


def _hand_value(hand: list[tuple[str, str, int]]) -> int:
    total = sum(c[2] for c in hand)
    aces = sum(1 for c in hand if c[0] == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def _fmt_hand(hand: list[tuple[str, str, int]], hide_second: bool = False) -> str:
    if hide_second and len(hand) >= 2:
        return f"`[{hand[0][0]}{hand[0][1]}]` `[🂠]`"
    return " ".join(f"`[{r}{s}]`" for r, s, _ in hand)


# ═══════════════════════════════════════════════════════════════════════════
# SOLO GAME VIEWS (mit Auto-Cashout / Refund Timeout-Schutz!)
# ═══════════════════════════════════════════════════════════════════════════

class MinesView(discord.ui.View):
    def __init__(self, user: discord.abc.User, einsatz: int, mine_count: int) -> None:
        super().__init__(timeout=120)
        self.user = user
        self.user_id = user.id
        self.einsatz = einsatz
        self.mine_count = mine_count
        self.grid_size = 4
        self.total_fields = 16
        self.safe_fields = 16 - mine_count
        self.revealed: set[int] = set()
        self.mines: set[int] = set(random.sample(range(16), mine_count))
        self.game_over = False
        self.multiplier = 1.0
        self.message: discord.Message | None = None
        self._build_buttons()

    def _calc_multiplier(self) -> float:
        safe_rev = len([r for r in self.revealed if r not in self.mines])
        if safe_rev == 0:
            return 1.0
        mult = 1.0
        for i in range(safe_rev):
            mult *= (self.total_fields - i) / (self.safe_fields - i)
        return round(mult * 0.97, 2)

    def _build_buttons(self) -> None:
        self.clear_items()
        for i in range(self.total_fields):
            row = i // 4
            if i in self.revealed:
                lbl = "💣" if i in self.mines else "💎"
                st = discord.ButtonStyle.danger if i in self.mines else discord.ButtonStyle.success
                btn = discord.ui.Button(label=lbl, style=st, row=row, disabled=True)
            elif self.game_over:
                lbl = "💣" if i in self.mines else "⬜"
                st = discord.ButtonStyle.danger if i in self.mines else discord.ButtonStyle.secondary
                btn = discord.ui.Button(label=lbl, style=st, row=row, disabled=True)
            else:
                btn = discord.ui.Button(label="⬜", style=discord.ButtonStyle.secondary, row=row)
                btn.callback = self._make_tile_cb(i)
            self.add_item(btn)

        if not self.game_over and len(self.revealed) > 0:
            payout = int(self.einsatz * self.multiplier)
            cashout = discord.ui.Button(
                label=f"💰 Cashout ({payout:,} 🪙 • ×{self.multiplier})",
                style=discord.ButtonStyle.success,
                row=4,
            )
            cashout.callback = self._cashout_cb
            self.add_item(cashout)

    def _make_tile_cb(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
                return
            if self.game_over:
                return
            self.revealed.add(idx)

            if idx in self.mines:
                self.game_over = True
                active_games.pop(self.user_id, None)
                _record_net(self.user, -self.einsatz)
                self._build_buttons()
                self.stop()
                embed = discord.Embed(
                    title="💥 BOOM! Mine getroffen!",
                    description=(
                        f"Du hast **{self.einsatz:,} 🪙** verloren.\n"
                        f"Aufgedeckte Diamanten: **{len(self.revealed) - 1}/{self.safe_fields}**"
                    ),
                    color=COLOR_ERROR,
                )
                embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                await interaction.response.edit_message(embed=embed, view=self)
                return

            self.multiplier = self._calc_multiplier()
            if len(self.revealed) >= self.safe_fields:
                self.game_over = True
                active_games.pop(self.user_id, None)
                payout = int(self.einsatz * self.multiplier)
                update_balance(self.user_id, payout)
                _record_net(self.user, payout - self.einsatz)
                self._build_buttons()
                self.stop()
                embed = discord.Embed(
                    title="🏆 PERFEKT! Alle Diamanten gefunden!",
                    description=(
                        f"Multiplikator: **×{self.multiplier}**\n"
                        f"💰 Auszahlung: **{payout:,} 🪙** (+{payout - self.einsatz:,})"
                    ),
                    color=COLOR_GOLD,
                )
                embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
                await interaction.response.edit_message(embed=embed, view=self)
                return

            self._build_buttons()
            payout = int(self.einsatz * self.multiplier)
            embed = discord.Embed(
                title=f"💣 Mines ({self.mine_count} Minen)",
                description=(
                    f"💎 Gefunden: **{len(self.revealed)}/{self.safe_fields}**\n"
                    f"📈 Aktuell: **×{self.multiplier}** (**{payout:,} 🪙**)"
                ),
                color=COLOR_INFO,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return _cb

    async def _cashout_cb(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id or self.game_over:
            return
        self.game_over = True
        active_games.pop(self.user_id, None)
        payout = int(self.einsatz * self.multiplier)
        update_balance(self.user_id, payout)
        _record_net(self.user, payout - self.einsatz)
        self._build_buttons()
        self.stop()
        embed = discord.Embed(
            title="💰 Mines – Ausgezahlt!",
            description=(
                f"Multiplikator: **×{self.multiplier}**\n"
                f"💰 Gewinn: **{payout:,} 🪙** (+{payout - self.einsatz:,} 🪙)"
            ),
            color=COLOR_SUCCESS,
        )
        embed.set_footer(text=f"Kontostand: {get_balance(self.user_id):,} 🪙")
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        if self.game_over:
            return
        self.game_over = True
        active_games.pop(self.user_id, None)
        if len(self.revealed) == 0:
            update_balance(self.user_id, self.einsatz)
        else:
            payout = int(self.einsatz * self.multiplier)
            update_balance(self.user_id, payout)
            _record_net(self.user, payout - self.einsatz)


class CrashView(discord.ui.View):
    def __init__(self, user: discord.abc.User, einsatz: int) -> None:
        super().__init__(timeout=60)
        self.user = user
        self.user_id = user.id
        self.einsatz = einsatz
        self.cashed_out = False
        self.cashout_mult = 1.0
        self.current_mult = 1.0

    @discord.ui.button(label="💰 CASHOUT!", style=discord.ButtonStyle.success)
    async def btn_cashout(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Nicht dein Spiel!", ephemeral=True)
            return
        if self.cashed_out:
            return
        self.cashed_out = True
        self.cashout_mult = self.current_mult
        button.disabled = True
        self.stop()
        await interaction.response.defer()


class BlackjackView(discord.ui.View):
    def __init__(self, user: discord.abc.User, einsatz: int, deck: list, player_hand: list, dealer_hand: list) -> None:
        super().__init__(timeout=90)
        self.user = user
        self.user_id = user.id
        self.einsatz = einsatz
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.finished = False
        if get_balance(self.user_id) < einsatz:
            self.btn_double.disabled = True

    def build_embed(self, reveal: bool = False, result_title: str = "🃏 Blackjack", color: int = COLOR_INFO, extra: str = "") -> discord.Embed:
        p_val = _hand_value(self.player_hand)
        d_val = _hand_value(self.dealer_hand) if reveal else self.dealer_hand[0][2]
        embed = discord.Embed(title=result_title, description=extra, color=color)
        embed.add_field(
            name=f"🧑 Deine Hand ({p_val})",
            value=_fmt_hand(self.player_hand),
            inline=False,
        )
        embed.add_field(
            name=f"🎩 Dealer ({d_val if reveal else f'{d_val}+?'})",
            value=_fmt_hand(self.dealer_hand, hide_second=not reveal),
            inline=False,
        )
        embed.set_footer(text=f"Einsatz: {self.einsatz:,} 🪙 • Kontostand: {get_balance(self.user_id):,} 🪙")
        return embed

    async def _finish_dealer(self, interaction: discord.Interaction) -> None:
        self.finished = True
        active_games.pop(self.user_id, None)
        self.stop()
        while _hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        p_val = _hand_value(self.player_hand)
        d_val = _hand_value(self.dealer_hand)

        if d_val > 21 or p_val > d_val:
            payout = self.einsatz * 2
            update_balance(self.user_id, payout)
            _record_net(self.user, self.einsatz)
            embed = self.build_embed(
                True, "🎉 Blackjack – Gewonnen!", COLOR_SUCCESS,
                f"💰 Du gewinnst **{payout:,} 🪙** (+{self.einsatz:,} 🪙)!"
            )
        elif p_val == d_val:
            update_balance(self.user_id, self.einsatz)
            embed = self.build_embed(
                True, "🤝 Blackjack – Push (Unentschieden)!", COLOR_WARNING,
                "Einsatz zurückerstattet."
            )
        else:
            _record_net(self.user, -self.einsatz)
            embed = self.build_embed(
                True, "💀 Blackjack – Dealer gewinnt!", COLOR_ERROR,
                f"Verlust: **-{self.einsatz:,} 🪙**"
            )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="🃏 Hit (Karte)", style=discord.ButtonStyle.primary)
    async def btn_hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id or self.finished:
            return
        self.btn_double.disabled = True
        self.player_hand.append(self.deck.pop())
        p_val = _hand_value(self.player_hand)
        if p_val > 21:
            self.finished = True
            active_games.pop(self.user_id, None)
            _record_net(self.user, -self.einsatz)
            self.stop()
            embed = self.build_embed(
                True, "💥 Überkauft (Bust)!", COLOR_ERROR, f"Über 21! Verlust: **-{self.einsatz:,} 🪙**"
            )
            await interaction.response.edit_message(embed=embed, view=None)
        elif p_val == 21:
            await self._finish_dealer(interaction)
        else:
            await interaction.response.edit_message(embed=self.build_embed(False), view=self)

    @discord.ui.button(label="✋ Stand (Halten)", style=discord.ButtonStyle.success)
    async def btn_stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id or self.finished:
            return
        await self._finish_dealer(interaction)

    @discord.ui.button(label="💰 Double Down", style=discord.ButtonStyle.secondary)
    async def btn_double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id or self.finished:
            return
        if get_balance(self.user_id) < self.einsatz:
            await interaction.response.send_message("❌ Nicht genug Coins für Double Down!", ephemeral=True)
            return
        update_balance(self.user_id, -self.einsatz)
        self.einsatz *= 2
        self.player_hand.append(self.deck.pop())
        if _hand_value(self.player_hand) > 21:
            self.finished = True
            active_games.pop(self.user_id, None)
            _record_net(self.user, -self.einsatz)
            self.stop()
            embed = self.build_embed(
                True, "💥 Double Down Bust!", COLOR_ERROR, f"Über 21! Verlust: **-{self.einsatz:,} 🪙**"
            )
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await self._finish_dealer(interaction)

    async def on_timeout(self) -> None:
        if not self.finished:
            self.finished = True
            active_games.pop(self.user_id, None)
            if len(self.player_hand) == 2:
                update_balance(self.user_id, self.einsatz)


class TowersView(discord.ui.View):
    DIFF_CONFIG = {
        "easy": (3, 2, 1.42),
        "medium": (3, 1, 2.85),
        "hard": (4, 1, 3.80),
    }

    def __init__(self, user: discord.abc.User, einsatz: int, difficulty: str) -> None:
        super().__init__(timeout=120)
        self.user = user
        self.user_id = user.id
        self.einsatz = einsatz
        self.cols, self.safe_per_row, self.step_mult = self.DIFF_CONFIG[difficulty]
        self.max_floors = 6
        self.floor = 0
        self.multiplier = 1.0
        self.game_over = False
        self.tower = [
            set(random.sample(range(self.cols), self.safe_per_row))
            for _ in range(self.max_floors)
        ]
        self.picks: list[int] = []
        self._build_buttons()

    def render_tower(self, reveal_all: bool = False) -> str:
        lines = []
        for f in range(self.max_floors - 1, -1, -1):
            mult_f = round((self.step_mult ** (f + 1)), 2)
            row_cells = []
            for c in range(self.cols):
                if f < len(self.picks):
                    if c == self.picks[f]:
                        row_cells.append("💎" if c in self.tower[f] else "💥")
                    elif reveal_all:
                        row_cells.append("🟩" if c in self.tower[f] else "🟥")
                    else:
                        row_cells.append("⬛")
                elif f == self.floor and not self.game_over:
                    row_cells.append("🟦")
                elif reveal_all:
                    row_cells.append("🟩" if c in self.tower[f] else "🟥")
                else:
                    row_cells.append("⬜")
            lines.append(f"`E{f + 1}` {''.join(row_cells)} `×{mult_f}`")
        return "\n".join(lines)

    def _build_buttons(self) -> None:
        self.clear_items()
        if self.game_over:
            return
        for c in range(self.cols):
            btn = discord.ui.Button(label=f"Tür {c + 1}", style=discord.ButtonStyle.primary, row=0)
            btn.callback = self._make_pick_cb(c)
            self.add_item(btn)
        if self.floor > 0:
            payout = int(self.einsatz * self.multiplier)
            co = discord.ui.Button(
                label=f"💰 Cashout ({payout:,} 🪙)",
                style=discord.ButtonStyle.success,
                row=1,
            )
            co.callback = self._cashout
            self.add_item(co)

    def _make_pick_cb(self, col: int):
        async def _cb(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id or self.game_over:
                return
            self.picks.append(col)
            if col not in self.tower[self.floor]:
                self.game_over = True
                active_games.pop(self.user_id, None)
                _record_net(self.user, -self.einsatz)
                self.stop()
                embed = discord.Embed(
                    title="💥 Towers – Eingestürzt!",
                    description=f"{self.render_tower(True)}\n\nVerlust: **-{self.einsatz:,} 🪙**",
                    color=COLOR_ERROR,
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

            self.floor += 1
            self.multiplier = round(self.step_mult ** self.floor, 2)
            if self.floor >= self.max_floors:
                self.game_over = True
                active_games.pop(self.user_id, None)
                payout = int(self.einsatz * self.multiplier)
                update_balance(self.user_id, payout)
                _record_net(self.user, payout - self.einsatz)
                self.stop()
                embed = discord.Embed(
                    title="🏆 Towers – Spitze erreicht!",
                    description=(
                        f"{self.render_tower(True)}\n\n"
                        f"💰 Gewinn: **{payout:,} 🪙** (×{self.multiplier})"
                    ),
                    color=COLOR_GOLD,
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

            self._build_buttons()
            embed = discord.Embed(
                title="🏰 Towers",
                description=(
                    f"{self.render_tower()}\n\n"
                    f"Aktuell: **×{self.multiplier}** (**{int(self.einsatz * self.multiplier):,} 🪙**)"
                ),
                color=COLOR_INFO,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return _cb

    async def _cashout(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id or self.game_over:
            return
        self.game_over = True
        active_games.pop(self.user_id, None)
        payout = int(self.einsatz * self.multiplier)
        update_balance(self.user_id, payout)
        _record_net(self.user, payout - self.einsatz)
        self.stop()
        embed = discord.Embed(
            title="💰 Towers – Ausgezahlt!",
            description=f"{self.render_tower(True)}\n\n💰 Auszahlung: **{payout:,} 🪙** (×{self.multiplier})",
            color=COLOR_SUCCESS,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        if not self.game_over:
            self.game_over = True
            active_games.pop(self.user_id, None)
            if self.floor == 0:
                update_balance(self.user_id, self.einsatz)
            else:
                payout = int(self.einsatz * self.multiplier)
                update_balance(self.user_id, payout)
                _record_net(self.user, payout - self.einsatz)


class HighLowView(discord.ui.View):
    def __init__(self, user: discord.abc.User, einsatz: int) -> None:
        super().__init__(timeout=90)
        self.user = user
        self.user_id = user.id
        self.einsatz = einsatz
        self.current_num = random.randint(2, 13)
        self.multiplier = 1.0
        self.streak = 0
        self.game_over = False
        self._sync_buttons()

    @staticmethod
    def _card_label(n: int) -> str:
        names = {11: "Bube (11)", 12: "Dame (12)", 13: "König (13)", 14: "Ass (14)"}
        return names.get(n, str(n))

    def _sync_buttons(self) -> None:
        self.btn_cashout.disabled = self.streak == 0
        if self.streak > 0:
            self.btn_cashout.label = f"💰 Cashout ({int(self.einsatz * self.multiplier):,} 🪙)"

    async def _guess(self, interaction: discord.Interaction, mode: str) -> None:
        if interaction.user.id != self.user_id or self.game_over:
            return
        nxt = random.randint(2, 14)
        won = (
            (mode == "high" and nxt > self.current_num)
            or (mode == "low" and nxt < self.current_num)
            or (mode == "same" and nxt == self.current_num)
        )
        old = self.current_num
        self.current_num = nxt

        if not won:
            self.game_over = True
            active_games.pop(self.user_id, None)
            _record_net(self.user, -self.einsatz)
            self.stop()
            embed = discord.Embed(
                title="💀 High-Low – Falsch geraten!",
                description=(
                    f"Vorherige Karte: **{self._card_label(old)}**\n"
                    f"Gezogene Karte: **{self._card_label(nxt)}**\n\n"
                    f"Verlust: **-{self.einsatz:,} 🪙**"
                ),
                color=COLOR_ERROR,
            )
            await interaction.response.edit_message(embed=embed, view=None)
            return

        self.streak += 1
        step = 3.5 if mode == "same" else 1.45
        self.multiplier = round(self.multiplier * step, 2)
        self._sync_buttons()
        embed = discord.Embed(
            title=f"🃏 High-Low – Serie {self.streak}🔥",
            description=(
                f"Neue Karte: **`{self._card_label(self.current_num)}`** (Werte 2–14)\n\n"
                f"📈 Multiplikator: **×{self.multiplier}** (**{int(self.einsatz * self.multiplier):,} 🪙**)"
            ),
            color=COLOR_SUCCESS,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬆️ Höher", style=discord.ButtonStyle.primary)
    async def btn_high(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._guess(interaction, "high")

    @discord.ui.button(label="⬇️ Niedriger", style=discord.ButtonStyle.primary)
    async def btn_low(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._guess(interaction, "low")

    @discord.ui.button(label="🎯 Gleich (×3.5)", style=discord.ButtonStyle.secondary)
    async def btn_same(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._guess(interaction, "same")

    @discord.ui.button(label="💰 Cashout", style=discord.ButtonStyle.success)
    async def btn_cashout(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id or self.game_over or self.streak == 0:
            return
        self.game_over = True
        active_games.pop(self.user_id, None)
        payout = int(self.einsatz * self.multiplier)
        update_balance(self.user_id, payout)
        _record_net(self.user, payout - self.einsatz)
        self.stop()
        embed = discord.Embed(
            title="💰 High-Low – Ausgezahlt!",
            description=f"Serie: **{self.streak}** • Gewinn: **{payout:,} 🪙** (×{self.multiplier})",
            color=COLOR_GOLD,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        if not self.game_over:
            self.game_over = True
            active_games.pop(self.user_id, None)
            if self.streak == 0:
                update_balance(self.user_id, self.einsatz)
            else:
                payout = int(self.einsatz * self.multiplier)
                update_balance(self.user_id, payout)
                _record_net(self.user, payout - self.einsatz)


# ═══════════════════════════════════════════════════════════════════════════
# MULTIPLAYER VIEWS (Duel, Russian Roulette, Connect 4, TicTacToe)
# ═══════════════════════════════════════════════════════════════════════════

class DuelAcceptView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, einsatz: int) -> None:
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.einsatz = einsatz
        self.resolved = False

    @discord.ui.button(label="⚔️ Annehmen!", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Nur der Herausgeforderte kann annehmen!", ephemeral=True)
            return
        if get_balance(self.opponent.id) < self.einsatz:
            await interaction.response.send_message("❌ Du hast nicht genug Coins!", ephemeral=True)
            return

        self.resolved = True
        self.stop()
        update_balance(self.opponent.id, -self.einsatz)
        active_games.pop(self.challenger.id, None)

        r1 = random.randint(1, 100)
        r2 = random.randint(1, 100)
        while r1 == r2:
            r2 = random.randint(1, 100)

        pot = self.einsatz * 2
        fee = int(pot * 0.05)
        payout = pot - fee
        winner = self.challenger if r1 > r2 else self.opponent
        loser = self.opponent if winner == self.challenger else self.challenger
        update_balance(winner.id, payout)
        _record_net(winner, payout - self.einsatz)
        _record_net(loser, -self.einsatz)

        embed = discord.Embed(
            title=f"⚔️ Würfel-Duell – {winner.display_name} gewinnt!",
            description=(
                f"🎲 **{self.challenger.display_name}** würfelt: **{r1}**\n"
                f"🎲 **{self.opponent.display_name}** würfelt: **{r2}**\n\n"
                f"🏆 {winner.mention} gewinnt **{payout:,} 🪙**! *(5% Hausgebühr)*"
            ),
            color=COLOR_GOLD,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id not in (self.opponent.id, self.challenger.id):
            await interaction.response.send_message("❌ Nicht für dich!", ephemeral=True)
            return
        self.resolved = True
        self.stop()
        update_balance(self.challenger.id, self.einsatz)
        active_games.pop(self.challenger.id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Duell abgebrochen",
                description="Einsatz wurde zurückerstattet.",
                color=COLOR_ERROR,
            ),
            view=None,
        )

    async def on_timeout(self) -> None:
        if not self.resolved:
            update_balance(self.challenger.id, self.einsatz)
            active_games.pop(self.challenger.id, None)


class RRTurnView(discord.ui.View):
    def __init__(self, player: discord.Member, chambers: int) -> None:
        super().__init__(timeout=15)
        self.player = player
        self.chambers = chambers
        self.choice: str | None = None
        self.done = asyncio.Event()

    async def _pick(self, interaction: discord.Interaction, choice: str, title: str, color: int) -> None:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
            return
        self.choice = choice
        self.stop()
        self.done.set()
        await interaction.response.edit_message(
            embed=discord.Embed(title=title, description=f"**{self.player.display_name}** handelt...", color=color),
            view=None,
        )

    @discord.ui.button(label="🔫 Abdrücken!", style=discord.ButtonStyle.danger, row=0)
    async def btn_shoot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._pick(interaction, "shoot", "🔫 Abdrücken...", COLOR_RR)

    @discord.ui.button(label="🔄 Trommel drehen", style=discord.ButtonStyle.primary, row=0)
    async def btn_spin(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._pick(interaction, "spin", "🔄 Trommel neu drehen...", COLOR_WARNING)

    @discord.ui.button(label="👉 Weitergeben", style=discord.ButtonStyle.secondary, row=0)
    async def btn_pass(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._pick(interaction, "pass", "👉 Revolver weitergegeben!", COLOR_INFO)

    @discord.ui.button(label="🎲 Doppelt (2 Kammern)", style=discord.ButtonStyle.danger, row=1)
    async def btn_double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._pick(interaction, "double", "🎲 DOPPELT ODER NICHTS!", 0xFF0000)

    async def on_timeout(self) -> None:
        if self.choice is None:
            self.choice = "shoot"
            self.done.set()


class RRForceShootView(discord.ui.View):
    def __init__(self, player: discord.Member) -> None:
        super().__init__(timeout=10)
        self.player = player
        self.done = asyncio.Event()

    @discord.ui.button(label="🔫 ABDRÜCKEN!", style=discord.ButtonStyle.danger)
    async def btn_shoot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
            return
        self.stop()
        self.done.set()
        await interaction.response.edit_message(
            embed=discord.Embed(title="🔫 Abdrücken...", color=COLOR_RR), view=None
        )

    async def on_timeout(self) -> None:
        self.done.set()


async def play_russian_roulette(
    channel: discord.abc.Messageable, players: list[discord.Member], einsatz: int, channel_id: int
) -> None:
    active_rr_games.pop(channel_id, None)
    pot = einsatz * len(players)
    alive = list(players)
    eliminated: list[discord.Member] = []
    random.shuffle(alive)
    chamber = random.randint(0, RR_CHAMBERS - 1)
    current_chamber = 0
    round_num = 0
    bonus_multipliers: dict[int, float] = {}
    force_shoot_next = False

    await channel.send(
        embed=discord.Embed(
            title="🔫 Russisches Roulette gestartet!",
            description=f"**{len(alive)} Spieler** • **{RR_CHAMBERS} Kammern** • 💰 **Pot:** {pot:,} 🪙",
            color=COLOR_RR,
        )
    )
    await asyncio.sleep(2)

    player_idx = 0
    while len(alive) > 1:
        player = alive[player_idx % len(alive)]
        round_num += 1

        if force_shoot_next:
            fv = RRForceShootView(player)
            await channel.send(
                content=player.mention,
                embed=discord.Embed(
                    title=f"🔫 {player.display_name} – DU MUSST SCHIESSEN!",
                    description="Der Revolver wurde weitergegeben!",
                    color=0xFF0000,
                ),
                view=fv,
            )
            try:
                await asyncio.wait_for(fv.done.wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            force_shoot_next = False
            choice = "shoot"
        else:
            tv = RRTurnView(player, RR_CHAMBERS)
            alive_str = "\n".join(
                f"{'👉 ' if p.id == player.id else ''}🟢 {p.display_name}" for p in alive
            )
            embed = discord.Embed(
                title=f"🔫 Runde {round_num} – {player.display_name} ist dran!",
                description=f"**Überlebende:**\n{alive_str}\n\nKammer `{current_chamber + 1}/{RR_CHAMBERS}` • Pot: **{pot:,} 🪙**",
                color=COLOR_RR,
            )
            await channel.send(content=player.mention, embed=embed, view=tv)
            try:
                await asyncio.wait_for(tv.done.wait(), timeout=17)
            except asyncio.TimeoutError:
                pass
            choice = tv.choice or "shoot"

        await asyncio.sleep(1.2)

        if choice == "pass":
            force_shoot_next = True
            player_idx += 1
            continue

        if choice == "spin":
            chamber = random.randint(0, RR_CHAMBERS - 1)
            current_chamber = 0

        checks = 2 if choice == "double" else 1
        hit = any(((current_chamber + i) % RR_CHAMBERS) == chamber for i in range(checks))

        if hit:
            alive.remove(player)
            eliminated.append(player)
            _record_net(player, -einsatz)
            player_idx = player_idx % max(1, len(alive))
            await channel.send(
                embed=discord.Embed(
                    title="💥 BANG!",
                    description=f"**{player.mention}** wurde getroffen und scheidet aus! 💀",
                    color=COLOR_ERROR,
                )
            )
            try:
                await player.timeout(datetime.timedelta(seconds=60), reason="Russisches Roulette 💥")
            except Exception:
                pass
            chamber = random.randint(0, RR_CHAMBERS - 1)
            current_chamber = 0
        else:
            if choice == "double":
                bonus_multipliers[player.id] = bonus_multipliers.get(player.id, 1.0) + 0.5
            current_chamber = (current_chamber + checks) % RR_CHAMBERS
            player_idx += 1
            await channel.send(
                embed=discord.Embed(
                    title="😮‍💨 *klick* – Überlebt!",
                    description=f"**{player.mention}** überlebt die Runde!",
                    color=COLOR_SUCCESS,
                )
            )
        await asyncio.sleep(1.2)

    winner = alive[0]
    w_bonus = bonus_multipliers.get(winner.id, 1.0)
    fee = int(pot * 0.05)
    payout = int((pot - fee) * w_bonus)
    update_balance(winner.id, payout)
    _record_net(winner, payout - einsatz)

    loser_list = "\n".join(f"💀 {p.display_name} (-{einsatz:,} 🪙)" for p in eliminated)
    embed = discord.Embed(
        title="🏆 RUSSISCHES ROULETTE – SIEGER!",
        description=(
            f"**{winner.mention}** überlebt als Letzter!\n\n"
            f"💰 **Gewinn: {payout:,} 🪙**\n"
            f"🔫 Runden: {round_num}"
        ),
        color=COLOR_GOLD,
    )
    embed.add_field(name="💀 Ausgeschieden", value=loser_list or "–", inline=False)
    embed.set_thumbnail(url=winner.display_avatar.url)
    await channel.send(embed=embed)


class RussianRouletteJoinView(discord.ui.View):
    def __init__(self, host: discord.Member, einsatz: int, channel_id: int) -> None:
        super().__init__(timeout=RR_JOIN_TIMEOUT)
        self.host = host
        self.einsatz = einsatz
        self.channel_id = channel_id
        self.players: list[discord.Member] = [host]
        self.started = False
        self.cancelled = False

    @discord.ui.button(label="🔫 Mitspielen!", style=discord.ButtonStyle.danger)
    async def btn_join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.started or self.cancelled or not isinstance(interaction.user, discord.Member):
            return
        if any(p.id == interaction.user.id for p in self.players):
            await interaction.response.send_message("❌ Du bist bereits dabei!", ephemeral=True)
            return
        if len(self.players) >= RR_MAX_PLAYERS:
            await interaction.response.send_message("❌ Die Lobby ist voll!", ephemeral=True)
            return
        if get_balance(interaction.user.id) < self.einsatz:
            await interaction.response.send_message(
                f"❌ Du benötigst **{self.einsatz:,} 🪙**!", ephemeral=True
            )
            return

        update_balance(interaction.user.id, -self.einsatz)
        active_rr_games.add_player_escrow(self.channel_id, interaction.user.id, self.einsatz)
        self.players.append(interaction.user)
        plist = "\n".join(f"🔫 {p.mention}" for p in self.players)
        embed = discord.Embed(
            title="🔫 Russisches Roulette – Lobby",
            description=(
                f"**Host:** {self.host.mention}\n"
                f"**Einsatz:** {self.einsatz:,} 🪙 • **Pot:** {self.einsatz * len(self.players):,} 🪙\n\n"
                f"**Spieler ({len(self.players)}/{RR_MAX_PLAYERS}):**\n{plist}"
            ),
            color=COLOR_RR,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶️ Starten!", style=discord.ButtonStyle.success)
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("❌ Nur der Host kann starten!", ephemeral=True)
            return
        if len(self.players) < RR_MIN_PLAYERS:
            await interaction.response.send_message(
                f"❌ Mindestens **{RR_MIN_PLAYERS}** Spieler erforderlich!", ephemeral=True
            )
            return
        self.started = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="🔫 Trommel wird geladen...", color=COLOR_RR), view=None
        )
        await play_russian_roulette(interaction.channel, self.players, self.einsatz, self.channel_id)

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("❌ Nur der Host kann abbrechen!", ephemeral=True)
            return
        self.cancelled = True
        self.stop()
        for p in self.players:
            update_balance(p.id, self.einsatz)
        active_rr_games.pop(self.channel_id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Abgebrochen", description="Einsätze erstattet.", color=COLOR_ERROR),
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.started or self.cancelled:
            return
        if len(self.players) >= RR_MIN_PLAYERS:
            self.started = True
            ch = self.host.guild.get_channel(self.channel_id)
            if ch:
                await play_russian_roulette(ch, self.players, self.einsatz, self.channel_id)
        else:
            for p in self.players:
                update_balance(p.id, self.einsatz)
            active_rr_games.pop(self.channel_id, None)


# ── Connect 4 & TicTacToe ──────────────────────────────────────────────────

class Connect4Game:
    def __init__(self, p1_id: int, p2_id: int) -> None:
        self.board = [[C4_EMPTY for _ in range(C4_COLS)] for _ in range(C4_ROWS)]
        self.player1_id = p1_id
        self.player2_id = p2_id
        self.current_turn = p1_id
        self.game_over = False

    @property
    def current_piece(self) -> str:
        return C4_RED if self.current_turn == self.player1_id else C4_YELLOW

    @property
    def other_player(self) -> int:
        return self.player2_id if self.current_turn == self.player1_id else self.player1_id

    def drop_piece(self, col: int) -> int:
        for row in range(C4_ROWS - 1, -1, -1):
            if self.board[row][col] == C4_EMPTY:
                self.board[row][col] = self.current_piece
                return row
        return -1

    def check_winner(self, row: int, col: int) -> bool:
        piece = self.board[row][col]
        for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
            cnt = 1
            for sign in (1, -1):
                r, c = row + dr * sign, col + dc * sign
                while 0 <= r < C4_ROWS and 0 <= c < C4_COLS and self.board[r][c] == piece:
                    cnt += 1
                    r += dr * sign
                    c += dc * sign
            if cnt >= 4:
                return True
        return False

    def is_full(self) -> bool:
        return all(self.board[0][c] != C4_EMPTY for c in range(C4_COLS))

    def render_board(self) -> str:
        rows = ["".join(r) for r in self.board]
        rows.append("1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣")
        return "\n".join(rows)


class Connect4View(discord.ui.View):
    def __init__(self, game: Connect4Game, p1: discord.Member, p2: discord.Member, einsatz: int) -> None:
        super().__init__(timeout=120)
        self.game = game
        self.p1 = p1
        self.p2 = p2
        self.einsatz = einsatz
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for col in range(C4_COLS):
            btn = discord.ui.Button(
                label=str(col + 1),
                style=discord.ButtonStyle.secondary,
                disabled=self.game.board[0][col] != C4_EMPTY or self.game.game_over,
                row=0 if col < 4 else 1,
            )
            btn.callback = self._make_cb(col)
            self.add_item(btn)

    def _make_cb(self, col: int):
        async def _cb(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.game.current_turn:
                await interaction.response.send_message("❌ Du bist nicht am Zug!", ephemeral=True)
                return
            row = self.game.drop_piece(col)
            if row == -1:
                return

            if self.game.check_winner(row, col):
                self.game.game_over = True
                active_games.pop(self.p1.id, None)
                active_games.pop(self.p2.id, None)
                winner = self.p1 if self.game.current_turn == self.p1.id else self.p2
                loser = self.p2 if winner == self.p1 else self.p1
                payout = int(self.einsatz * 2 * 0.95)
                update_balance(winner.id, payout)
                _record_net(winner, payout - self.einsatz)
                _record_net(loser, -self.einsatz)
                self.stop()
                embed = discord.Embed(
                    title=f"🏆 4 Gewinnt – {winner.display_name} gewinnt!",
                    description=f"{self.game.render_board()}\n\n🎉 {winner.mention} gewinnt **{payout:,} 🪙**!",
                    color=COLOR_GOLD,
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

            if self.game.is_full():
                self.game.game_over = True
                active_games.pop(self.p1.id, None)
                active_games.pop(self.p2.id, None)
                update_balance(self.p1.id, self.einsatz)
                update_balance(self.p2.id, self.einsatz)
                self.stop()
                embed = discord.Embed(
                    title="🤝 4 Gewinnt – Unentschieden!",
                    description=f"{self.game.render_board()}\n\nEinsätze zurückerstattet.",
                    color=COLOR_WARNING,
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

            self.game.current_turn = self.game.other_player
            cur = self.p1 if self.game.current_turn == self.p1.id else self.p2
            self._build()
            embed = discord.Embed(
                title="🟡🔴 4 Gewinnt",
                description=(
                    f"{self.game.render_board()}\n\n"
                    f"{self.game.current_piece} **{cur.display_name}** ist am Zug!\n"
                    f"💰 Pot: **{self.einsatz * 2:,} 🪙**"
                ),
                color=COLOR_CONNECT4,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return _cb

    async def on_timeout(self) -> None:
        if not self.game.game_over:
            self.game.game_over = True
            winner = self.p1 if self.game.other_player == self.p1.id else self.p2
            loser = self.p2 if winner == self.p1 else self.p1
            payout = int(self.einsatz * 2 * 0.95)
            update_balance(winner.id, payout)
            _record_net(winner, payout - self.einsatz)
            _record_net(loser, -self.einsatz)
            active_games.pop(self.p1.id, None)
            active_games.pop(self.p2.id, None)


class Connect4AcceptView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, einsatz: int) -> None:
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.einsatz = einsatz
        self.accepted = False

    @discord.ui.button(label="✅ Annehmen!", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Nicht für dich!", ephemeral=True)
            return
        if get_balance(self.opponent.id) < self.einsatz:
            await interaction.response.send_message("❌ Nicht genug Coins!", ephemeral=True)
            return
        self.accepted = True
        update_balance(self.opponent.id, -self.einsatz)
        active_games[self.opponent.id] = "4 Gewinnt"
        active_games.add_escrow(self.opponent.id, self.einsatz, "4 Gewinnt")
        game = Connect4Game(self.challenger.id, self.opponent.id)
        view = Connect4View(game, self.challenger, self.opponent, self.einsatz)
        embed = discord.Embed(
            title="🟡🔴 4 Gewinnt",
            description=f"{game.render_board()}\n\n{C4_RED} **{self.challenger.display_name}** beginnt!",
            color=COLOR_CONNECT4,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id not in (self.opponent.id, self.challenger.id):
            return
        self.accepted = True
        update_balance(self.challenger.id, self.einsatz)
        active_games.pop(self.challenger.id, None)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Abgelehnt", description="Einsatz erstattet.", color=COLOR_ERROR),
            view=None,
        )

    async def on_timeout(self) -> None:
        if not self.accepted:
            update_balance(self.challenger.id, self.einsatz)
            active_games.pop(self.challenger.id, None)


class TicTacToeView(discord.ui.View):
    WIN_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))

    def __init__(self, p1: discord.Member, p2: discord.Member, einsatz: int) -> None:
        super().__init__(timeout=120)
        self.p1 = p1
        self.p2 = p2
        self.einsatz = einsatz
        self.board = [TTT_EMPTY] * 9
        self.turn = p1.id
        self.game_over = False
        self._build()

    def _render(self) -> str:
        return "\n".join(" ".join(self.board[r * 3 : r * 3 + 3]) for r in range(3))

    def _build(self) -> None:
        self.clear_items()
        for i in range(9):
            row = i // 3
            val = self.board[i]
            if val == TTT_EMPTY and not self.game_over:
                btn = discord.ui.Button(label="\u200b", style=discord.ButtonStyle.secondary, row=row)
                btn.callback = self._make_cb(i)
            elif val == TTT_X:
                btn = discord.ui.Button(label="X", style=discord.ButtonStyle.danger, row=row, disabled=True)
            elif val == TTT_O:
                btn = discord.ui.Button(label="O", style=discord.ButtonStyle.primary, row=row, disabled=True)
            else:
                btn = discord.ui.Button(label="\u200b", style=discord.ButtonStyle.secondary, row=row, disabled=True)
            self.add_item(btn)

    def _make_cb(self, pos: int):
        async def _cb(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.turn or self.game_over:
                await interaction.response.send_message("❌ Du bist nicht dran!", ephemeral=True)
                return
            self.board[pos] = TTT_X if self.turn == self.p1.id else TTT_O
            won = any(
                self.board[a] == self.board[b] == self.board[c] != TTT_EMPTY
                for a, b, c in self.WIN_LINES
            )
            if won:
                self.game_over = True
                active_games.pop(self.p1.id, None)
                active_games.pop(self.p2.id, None)
                winner = self.p1 if self.turn == self.p1.id else self.p2
                loser = self.p2 if winner == self.p1 else self.p1
                payout = int(self.einsatz * 2 * 0.95)
                update_balance(winner.id, payout)
                _record_net(winner, payout - self.einsatz)
                _record_net(loser, -self.einsatz)
                self._build()
                self.stop()
                embed = discord.Embed(
                    title=f"🏆 Tic Tac Toe – {winner.display_name} gewinnt!",
                    description=f"{self._render()}\n\n🎉 {winner.mention} gewinnt **{payout:,} 🪙**!",
                    color=COLOR_GOLD,
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            if TTT_EMPTY not in self.board:
                self.game_over = True
                active_games.pop(self.p1.id, None)
                active_games.pop(self.p2.id, None)
                update_balance(self.p1.id, self.einsatz)
                update_balance(self.p2.id, self.einsatz)
                self._build()
                self.stop()
                embed = discord.Embed(
                    title="🤝 Tic Tac Toe – Unentschieden!",
                    description=f"{self._render()}\n\nEinsätze zurückerstattet.",
                    color=COLOR_WARNING,
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            self.turn = self.p2.id if self.turn == self.p1.id else self.p1.id
            cur = self.p1 if self.turn == self.p1.id else self.p2
            self._build()
            embed = discord.Embed(
                title="❌⭕ Tic Tac Toe",
                description=f"{self._render()}\n\n**{cur.display_name}** ist dran!",
                color=COLOR_TICTACTOE,
            )
            await interaction.response.edit_message(embed=embed, view=self)

        return _cb

    async def on_timeout(self) -> None:
        if not self.game_over:
            self.game_over = True
            winner = self.p2 if self.turn == self.p1.id else self.p1
            loser = self.p1 if winner == self.p2 else self.p2
            payout = int(self.einsatz * 2 * 0.95)
            update_balance(winner.id, payout)
            _record_net(winner, payout - self.einsatz)
            _record_net(loser, -self.einsatz)
            active_games.pop(self.p1.id, None)
            active_games.pop(self.p2.id, None)


class TicTacToeAcceptView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, einsatz: int) -> None:
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.einsatz = einsatz
        self.accepted = False

    @discord.ui.button(label="✅ Annehmen!", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Nicht für dich!", ephemeral=True)
            return
        if get_balance(self.opponent.id) < self.einsatz:
            await interaction.response.send_message("❌ Nicht genug Coins!", ephemeral=True)
            return
        self.accepted = True
        update_balance(self.opponent.id, -self.einsatz)
        active_games[self.opponent.id] = "Tic Tac Toe"
        active_games.add_escrow(self.opponent.id, self.einsatz, "Tic Tac Toe")
        view = TicTacToeView(self.challenger, self.opponent, self.einsatz)
        embed = discord.Embed(
            title="❌⭕ Tic Tac Toe",
            description=f"{view._render()}\n\n❌ **{self.challenger.display_name}** beginnt!",
            color=COLOR_TICTACTOE,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id not in (self.opponent.id, self.challenger.id):
            return
        self.accepted = True
        update_balance(self.challenger.id, self.einsatz)
        active_games.pop(self.challenger.id, None)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Abgelehnt", description="Einsatz erstattet.", color=COLOR_ERROR),
            view=None,
        )

    async def on_timeout(self) -> None:
        if not self.accepted:
            update_balance(self.challenger.id, self.einsatz)
            active_games.pop(self.challenger.id, None)


# ═══════════════════════════════════════════════════════════════════════════
# GAMBLING COG
# ═══════════════════════════════════════════════════════════════════════════

class GamblingCog(commands.Cog, name="Gambling"):
    """Economy, Casino-Games & Multiplayer-Spiele."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        refunded = refund_interrupted_escrows()
        if refunded:
            total = sum(int(r.get("amount") or 0) for r in refunded)
            print(
                f"💰 [Casino] {len(refunded)} durch Neustart unterbrochene Spieleinsätze "
                f"({total:,} Coins) automatisch zurückerstattet!"
            )

    # ── Economy ───────────────────────────────────────────────────────────

    @app_commands.command(name="balance", description="💰 Zeigt deinen aktuellen Coin-Kontostand")
    @app_commands.describe(member="Optional: Kontostand eines anderen Users")
    @is_gambling_channel()
    async def cmd_balance(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        target = member or interaction.user
        bal = get_balance(target.id)
        embed = discord.Embed(
            title="💰 Kontostand",
            description=f"{target.mention} besitzt **{bal:,} 🪙**",
            color=COLOR_GOLD,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="daily", description="🎁 Hole deine tägliche Coin-Belohnung ab!")
    @is_gambling_channel()
    async def cmd_daily(self, interaction: discord.Interaction) -> None:
        uid_s = str(interaction.user.id)
        today = today_berlin()
        today_s = str(today)
        yesterday_s = str(today - datetime.timedelta(days=1))

        bank = load_bank()
        last = bank.get(f"{uid_s}_daily")
        if last == today_s:
            await interaction.response.send_message(
                "⏳ Du hast deine tägliche Belohnung heute bereits abgeholt! Komm morgen wieder.",
                ephemeral=True,
            )
            return

        streak = int(bank.get(f"{uid_s}_daily_streak", 0))
        streak = (streak + 1) if last == yesterday_s else 1
        bonus = min((streak - 1) * 25, 250)
        reward = DAILY_REWARD + bonus

        bank[f"{uid_s}_daily"] = today_s
        bank[f"{uid_s}_daily_streak"] = streak
        bank[uid_s] = int(bank.get(uid_s, 1000)) + reward
        save_bank(bank)

        embed = discord.Embed(
            title="🎁 Tägliche Belohnung!",
            description=(
                f"Du erhältst **{reward:,} 🪙**!\n"
                f"🔥 Daily-Streak: **{streak} Tag{'e' if streak != 1 else ''}** *(+{bonus} Bonus)*\n\n"
                f"💰 Neuer Kontostand: **{bank[uid_s]:,} 🪙**"
            ),
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="pay", description="💸 Überweise Coins an einen anderen User")
    @app_commands.describe(empfaenger="Empfänger der Coins", betrag="Anzahl der Coins")
    @is_gambling_channel()
    async def cmd_pay(
        self, interaction: discord.Interaction, empfaenger: discord.Member, betrag: int
    ) -> None:
        if empfaenger.bot or empfaenger.id == interaction.user.id:
            await interaction.response.send_message("❌ Ungültiger Empfänger!", ephemeral=True)
            return
        if betrag <= 0:
            await interaction.response.send_message("❌ Der Betrag muss positiv sein!", ephemeral=True)
            return
        if get_balance(interaction.user.id) < betrag:
            await interaction.response.send_message("❌ Nicht genug Coins!", ephemeral=True)
            return

        update_balance(interaction.user.id, -betrag)
        update_balance(empfaenger.id, betrag)
        embed = discord.Embed(
            title="💸 Überweisung erfolgreich",
            description=f"{interaction.user.mention} hat **{betrag:,} 🪙** an {empfaenger.mention} überwiesen!",
            color=COLOR_SUCCESS,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="give", description="➕ Gibt einem User Coins (nur Admins)")
    @is_admin_user()
    async def cmd_give(
        self, interaction: discord.Interaction, member: discord.Member, betrag: int
    ) -> None:
        new_bal = update_balance(member.id, betrag)
        await interaction.response.send_message(
            f"✅ {member.mention} wurden **{betrag:,} 🪙** gutgeschrieben (Neu: **{new_bal:,} 🪙**).",
            ephemeral=True,
        )

    @app_commands.command(name="setbalance", description="⚙️ Setzt den Kontostand eines Users (nur Admins)")
    @is_admin_user()
    async def cmd_setbalance(
        self, interaction: discord.Interaction, member: discord.Member, betrag: int
    ) -> None:
        new_bal = set_balance(member.id, betrag)
        await interaction.response.send_message(
            f"✅ Kontostand von {member.mention} auf **{new_bal:,} 🪙** gesetzt.",
            ephemeral=True,
        )

    @app_commands.command(name="coinleaderboard", description="🏆 Top 15 der reichsten User")
    @is_gambling_channel()
    async def cmd_coinleaderboard(self, interaction: discord.Interaction) -> None:
        bank = load_bank()
        entries = [(int(k), int(v)) for k, v in bank.items() if k.isdigit() and isinstance(v, int)]
        entries.sort(key=lambda x: x[1], reverse=True)
        if not entries:
            await interaction.response.send_message("Noch keine Kontostände vorhanden.", ephemeral=True)
            return

        lines = []
        for idx, (uid, coins) in enumerate(entries[:15], 1):
            m = interaction.guild.get_member(uid) if interaction.guild else None
            name = m.display_name if m else f"User {uid}"
            prefix = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"`#{idx}`")
            lines.append(f"{prefix} **{name}** — `{coins:,} 🪙`")

        embed = discord.Embed(
            title="🏆 Coin-Leaderboard",
            description="\n".join(lines),
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="gamblehelp", description="🎰 Übersicht aller Casino- & Multiplayer-Spiele")
    @is_gambling_channel()
    async def cmd_gamblehelp(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🎰 Casino & Spiele-Übersicht",
            description=f"Maximaler Einsatz pro Spiel: **{MAX_BET:,} 🪙**",
            color=COLOR_GOLD,
        )
        embed.add_field(
            name="🎲 Solo-Spiele",
            value=(
                "`/bet` — Slot-Machine (bis ×25)\n"
                "`/coinflip` — Kopf oder Zahl (×2)\n"
                "`/blackjack` — Blackjack (Hit/Stand/Double)\n"
                "`/mines` — Minenfeld mit Cashout\n"
                "`/crash` — Steigender Multiplikator\n"
                "`/towers` — Turm besteigen\n"
                "`/highlow` — Kartenraten\n"
                "`/roulette` — Französisches Roulette\n"
                "`/horserace` — Pferderennen (×3.8)\n"
                "`/plinko` — Plinko-Pyramide (bis ×5)\n"
                "`/vault` & `/vaultinfo` — Tresor-Jackpot knacken"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚔️ Multiplayer & PvP",
            value=(
                "`/duel` — 1v1 Würfel-Duell\n"
                "`/connect4` — 4 Gewinnt\n"
                "`/tictactoe` — Tic Tac Toe\n"
                "`/rr` — Russisches Roulette (2–8 Spieler)"
            ),
            inline=False,
        )
        embed.add_field(
            name="💰 Coins verdienen",
            value="`/daily` • `/fish` • `/jobs` (`/chop`, `/mine`, `/hunt`, `/smith`, `/cook`)",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Solo Casino Games ─────────────────────────────────────────────────

    @app_commands.command(name="bet", description="🎰 Spiele an der Slot-Machine!")
    @app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)")
    @is_gambling_channel()
    async def cmd_bet(self, interaction: discord.Interaction, einsatz: int) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        uid = interaction.user.id
        active_games[uid] = "Slots"
        update_balance(uid, -einsatz)
        try:
            s1, s2, s3 = _spin_slot(), _spin_slot(), _spin_slot()
            embed = discord.Embed(
                title="🎰 Slot-Machine",
                description="`| ❓ | ❓ | ❓ |`\n\n*Walzen drehen sich...*",
                color=COLOR_INFO,
            )
            await interaction.response.send_message(embed=embed)
            await asyncio.sleep(0.7)
            embed.description = f"`| {s1} | ❓ | ❓ |`"
            await interaction.edit_original_response(embed=embed)
            await asyncio.sleep(0.7)
            embed.description = f"`| {s1} | {s2} | ❓ |`"
            await interaction.edit_original_response(embed=embed)
            await asyncio.sleep(0.7)

            if s1 == s2 == s3:
                mult = SLOT_PAYOUTS.get(s1, 3)
                payout = einsatz * mult
            elif s1 == s2 or s2 == s3 or s1 == s3:
                mult = 1.5
                payout = int(einsatz * 1.5)
            else:
                mult = 0
                payout = 0

            if payout > 0:
                update_balance(uid, payout)
            net = payout - einsatz
            _record_net(interaction.user, net)

            embed = discord.Embed(
                title="🎉 JACKPOT!" if mult >= 8 else ("✨ Gewinn!" if payout > 0 else "💀 Verloren!"),
                description=(
                    f"`| {s1} | {s2} | {s3} |`\n\n"
                    f"**Einsatz:** {einsatz:,} 🪙\n"
                    f"**Auszahlung:** {payout:,} 🪙 ({'+' if net >= 0 else ''}{net:,} 🪙)"
                ),
                color=COLOR_GOLD if mult >= 8 else (COLOR_SUCCESS if payout > 0 else COLOR_ERROR),
            )
            embed.set_footer(text=f"Kontostand: {get_balance(uid):,} 🪙")
            await interaction.edit_original_response(embed=embed)
        finally:
            active_games.pop(uid, None)

    @app_commands.command(name="coinflip", description="🪙 Wirf eine Münze – Kopf oder Zahl!")
    @app_commands.describe(einsatz="Dein Einsatz", wahl="Kopf oder Zahl")
    @app_commands.choices(
        wahl=[
            app_commands.Choice(name="🪙 Kopf", value="kopf"),
            app_commands.Choice(name="🦅 Zahl", value="zahl"),
        ]
    )
    @is_gambling_channel()
    async def cmd_coinflip(self, interaction: discord.Interaction, einsatz: int, wahl: str) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        uid = interaction.user.id
        active_games[uid] = "Coinflip"
        update_balance(uid, -einsatz)
        try:
            await interaction.response.send_message(
                embed=discord.Embed(title="🪙 Münze fliegt...", color=COLOR_INFO)
            )
            await asyncio.sleep(1.2)
            result = random.choice(["kopf", "zahl"])
            won = result == wahl
            if won:
                update_balance(uid, einsatz * 2)
                _record_net(interaction.user, einsatz)
            else:
                _record_net(interaction.user, -einsatz)

            embed = discord.Embed(
                title=f"🪙 Es ist {result.capitalize()}! — {'Gewonnen!' if won else 'Verloren!'}",
                description=(
                    f"Deine Wahl: **{wahl.capitalize()}**\n"
                    f"{'💰 Gewinn: **+' + f'{einsatz:,}' + ' 🪙**' if won else '💸 Verlust: **-' + f'{einsatz:,}' + ' 🪙**'}"
                ),
                color=COLOR_SUCCESS if won else COLOR_ERROR,
            )
            embed.set_footer(text=f"Kontostand: {get_balance(uid):,} 🪙")
            await interaction.edit_original_response(embed=embed)
        finally:
            active_games.pop(uid, None)

    @app_commands.command(name="mines", description="💣 Mines – Decke Diamanten auf und zahle rechtzeitig aus!")
    @app_commands.describe(einsatz="Dein Einsatz", minen="Anzahl der Minen (1-14)")
    @is_gambling_channel()
    async def cmd_mines(self, interaction: discord.Interaction, einsatz: int, minen: int = 3) -> None:
        if not (1 <= minen <= 14):
            await interaction.response.send_message("❌ Minenanzahl muss zwischen 1 und 14 liegen!", ephemeral=True)
            return
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        update_balance(interaction.user.id, -einsatz)
        active_games[interaction.user.id] = "Mines"
        active_games.add_escrow(interaction.user.id, einsatz, "Mines")
        view = MinesView(interaction.user, einsatz, minen)
        embed = discord.Embed(
            title=f"💣 Mines ({minen} Minen)",
            description=f"**Einsatz:** {einsatz:,} 🪙\nKlicke auf die Felder, um Diamanten zu finden!",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="crash", description="🚀 Crash – Steige aus bevor die Rakete explodiert!")
    @app_commands.describe(einsatz="Dein Einsatz")
    @is_gambling_channel()
    async def cmd_crash(self, interaction: discord.Interaction, einsatz: int) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        uid = interaction.user.id
        update_balance(uid, -einsatz)
        active_games[uid] = "Crash"
        active_games.add_escrow(uid, einsatz, "Crash")

        try:
            r = random.random()
            if CRASH_CHEAT and r < 0.15:
                crash_point = 1.0
            elif r < 0.04:
                crash_point = 1.0
            else:
                crash_point = min(round(0.97 / (1.0 - random.random()), 2), 25.0)
                crash_point = max(1.05, crash_point)

            view = CrashView(interaction.user, einsatz)
            embed = discord.Embed(
                title="🚀 Crash startet...",
                description=f"**Multiplikator:** `×1.00`\n**Auszahlung:** {einsatz:,} 🪙",
                color=COLOR_INFO,
            )
            await interaction.response.send_message(embed=embed, view=view)

            mult = 1.00
            while mult < crash_point and not view.cashed_out:
                await asyncio.sleep(1.0)
                if view.cashed_out:
                    break
                step = 0.12 if mult < 2.0 else (0.28 if mult < 5.0 else 0.65)
                mult = round(mult + step, 2)
                if mult >= crash_point:
                    break
                view.current_mult = mult
                embed.description = (
                    f"🚀 **Multiplikator:** `×{mult:.2f}`\n"
                    f"💰 **Aktueller Wert:** {int(einsatz * mult):,} 🪙"
                )
                try:
                    await interaction.edit_original_response(embed=embed, view=view)
                except Exception:
                    break

            view.stop()
            if view.cashed_out:
                payout = int(einsatz * view.cashout_mult)
                update_balance(uid, payout)
                _record_net(interaction.user, payout - einsatz)
                embed = discord.Embed(
                    title=f"💰 Crash – Ausgestiegen bei ×{view.cashout_mult:.2f}!",
                    description=(
                        f"Gewinn: **{payout:,} 🪙** (+{payout - einsatz:,} 🪙)\n"
                        f"*(Rakete wäre bei ×{crash_point:.2f} gecrasht)*"
                    ),
                    color=COLOR_SUCCESS,
                )
            else:
                _record_net(interaction.user, -einsatz)
                embed = discord.Embed(
                    title=f"💥 CRASH bei ×{crash_point:.2f}!",
                    description=f"Du bist zu spät ausgestiegen! Verlust: **-{einsatz:,} 🪙**",
                    color=COLOR_ERROR,
                )
            embed.set_footer(text=f"Kontostand: {get_balance(uid):,} 🪙")
            await interaction.edit_original_response(embed=embed, view=None)
        finally:
            active_games.pop(uid, None)

    @app_commands.command(name="blackjack", description="🃏 Spiele eine Runde Blackjack gegen den Dealer!")
    @app_commands.describe(einsatz="Dein Einsatz")
    @is_gambling_channel()
    async def cmd_blackjack(self, interaction: discord.Interaction, einsatz: int) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        uid = interaction.user.id
        update_balance(uid, -einsatz)
        deck = _make_deck()
        p_hand = [deck.pop(), deck.pop()]
        d_hand = [deck.pop(), deck.pop()]

        if _hand_value(p_hand) == 21:
            if _hand_value(d_hand) == 21:
                update_balance(uid, einsatz)
                view = BlackjackView(interaction.user, einsatz, deck, p_hand, d_hand)
                await interaction.response.send_message(
                    embed=view.build_embed(True, "🤝 Doppel-Blackjack (Push)!", COLOR_WARNING)
                )
            else:
                payout = int(einsatz * 2.5)
                update_balance(uid, payout)
                _record_net(interaction.user, payout - einsatz)
                view = BlackjackView(interaction.user, einsatz, deck, p_hand, d_hand)
                await interaction.response.send_message(
                    embed=view.build_embed(
                        True, "👑 NATÜRLICHER BLACKJACK! (×2.5)", COLOR_GOLD,
                        f"💰 Auszahlung: **{payout:,} 🪙**!"
                    )
                )
            return

        active_games[uid] = "Blackjack"
        active_games.add_escrow(uid, einsatz, "Blackjack")
        view = BlackjackView(interaction.user, einsatz, deck, p_hand, d_hand)
        await interaction.response.send_message(embed=view.build_embed(False), view=view)

    @app_commands.command(name="horserace", description="🏇 Wette auf ein Pferd im Live-Rennen!")
    @app_commands.describe(einsatz="Dein Einsatz", pferd="Nummer des Pferdes (1-4)")
    @app_commands.choices(
        pferd=[
            app_commands.Choice(name="1. 🐎 Blitz", value=1),
            app_commands.Choice(name="2. 🏇 Donner", value=2),
            app_commands.Choice(name="3. 🦄 Sturmwind", value=3),
            app_commands.Choice(name="4. 🐴 Schatten", value=4),
        ]
    )
    @is_gambling_channel()
    async def cmd_horserace(self, interaction: discord.Interaction, einsatz: int, pferd: int) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        uid = interaction.user.id
        active_games[uid] = "Pferderennen"
        update_balance(uid, -einsatz)
        try:
            track_len = 12
            pos = [0, 0, 0, 0]

            def render() -> str:
                lines = []
                for idx, h in enumerate(HORSES):
                    p = min(pos[idx], track_len)
                    bar = "─" * p + h["emoji"] + "─" * (track_len - p) + "🏁"
                    mark = "◀" if (idx + 1) == pferd else ""
                    lines.append(f"`{idx + 1}.` {bar} **{h['name']}** {mark}")
                return "\n".join(lines)

            embed = discord.Embed(title="🏇 Pferderennen startet!", description=render(), color=COLOR_INFO)
            await interaction.response.send_message(embed=embed)

            while max(pos) < track_len:
                await asyncio.sleep(0.9)
                for i in range(4):
                    pos[i] += random.randint(1, 3)
                embed.description = render()
                await interaction.edit_original_response(embed=embed)

            max_p = max(pos)
            winners = [i for i, v in enumerate(pos) if v == max_p]
            winner_idx = random.choice(winners)
            won = (winner_idx + 1) == pferd

            if won:
                payout = int(einsatz * 3.8)
                update_balance(uid, payout)
                _record_net(interaction.user, payout - einsatz)
                title = f"🏆 {HORSES[winner_idx]['name']} gewinnt! Du räumst ab!"
                extra = f"\n\n💰 **Gewinn: {payout:,} 🪙** (×3.8)"
                col = COLOR_GOLD
            else:
                _record_net(interaction.user, -einsatz)
                title = f"🏁 {HORSES[winner_idx]['name']} gewinnt!"
                extra = f"\n\n💸 Dein Pferd hat verloren (**-{einsatz:,} 🪙**)."
                col = COLOR_ERROR

            embed = discord.Embed(title=title, description=render() + extra, color=col)
            embed.set_footer(text=f"Kontostand: {get_balance(uid):,} 🪙")
            await interaction.edit_original_response(embed=embed)
        finally:
            active_games.pop(uid, None)

    @app_commands.command(name="towers", description="🏰 Towers – Klettere Etage für Etage nach oben!")
    @app_commands.describe(einsatz="Dein Einsatz", schwierigkeit="Schwierigkeitsgrad")
    @app_commands.choices(
        schwierigkeit=[
            app_commands.Choice(name="🟢 Einfach (2/3 sicher)", value="easy"),
            app_commands.Choice(name="🟡 Mittel (1/3 sicher)", value="medium"),
            app_commands.Choice(name="🔴 Schwer (1/4 sicher)", value="hard"),
        ]
    )
    @is_gambling_channel()
    async def cmd_towers(
        self, interaction: discord.Interaction, einsatz: int, schwierigkeit: str = "easy"
    ) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        update_balance(interaction.user.id, -einsatz)
        active_games[interaction.user.id] = "Towers"
        active_games.add_escrow(interaction.user.id, einsatz, "Towers")
        view = TowersView(interaction.user, einsatz, schwierigkeit)
        embed = discord.Embed(
            title="🏰 Towers",
            description=f"{view.render_tower()}\n\nWähle unten eine Tür für Etage 1!",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="highlow", description="🃏 High-Low – Ist die nächste Karte höher oder niedriger?")
    @app_commands.describe(einsatz="Dein Einsatz")
    @is_gambling_channel()
    async def cmd_highlow(self, interaction: discord.Interaction, einsatz: int) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        update_balance(interaction.user.id, -einsatz)
        active_games[interaction.user.id] = "High-Low"
        active_games.add_escrow(interaction.user.id, einsatz, "High-Low")
        view = HighLowView(interaction.user, einsatz)
        embed = discord.Embed(
            title="🃏 High-Low",
            description=(
                f"Startkarte: **`{view._card_label(view.current_num)}`** (Werte 2–14)\n\n"
                f"Ist die nächste Karte höher, niedriger oder gleich?"
            ),
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="roulette", description="🎡 Roulette – Setze auf Farbe, Gerade/Ungerade oder Zahl!")
    @app_commands.describe(
        einsatz="Dein Einsatz",
        wette="rot, schwarz, gruen, gerade, ungerade, 1-18, 19-36 oder eine Zahl 0-36",
    )
    @is_gambling_channel()
    async def cmd_roulette(self, interaction: discord.Interaction, einsatz: int, wette: str) -> None:
        w = wette.strip().lower()
        valid_words = {"rot", "red", "schwarz", "black", "gruen", "grün", "green", "gerade", "even", "ungerade", "odd", "1-18", "19-36"}
        exact_num: int | None = None
        if w.isdigit() and 0 <= int(w) <= 36:
            exact_num = int(w)
        elif w not in valid_words:
            await interaction.response.send_message(
                "❌ Ungültige Wette! Erlaubt: `rot`, `schwarz`, `gruen`, `gerade`, `ungerade`, `1-18`, `19-36` oder `0`–`36`.",
                ephemeral=True,
            )
            return

        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return

        uid = interaction.user.id
        active_games[uid] = "Roulette"
        update_balance(uid, -einsatz)
        try:
            await interaction.response.send_message(
                embed=discord.Embed(title="🎡 Die Kugel rollt...", color=COLOR_INFO)
            )
            await asyncio.sleep(1.5)
            rolled = random.randint(0, 36)
            col_emoji = "🟢" if rolled == 0 else ("🔴" if rolled in ROULETTE_RED else "⚫")

            mult = 0
            if exact_num is not None and rolled == exact_num:
                mult = 36
            elif w in ("gruen", "grün", "green") and rolled == 0:
                mult = 36
            elif w in ("rot", "red") and rolled in ROULETTE_RED:
                mult = 2
            elif w in ("schwarz", "black") and rolled in ROULETTE_BLACK:
                mult = 2
            elif w in ("gerade", "even") and rolled != 0 and rolled % 2 == 0:
                mult = 2
            elif w in ("ungerade", "odd") and rolled % 2 == 1:
                mult = 2
            elif w == "1-18" and 1 <= rolled <= 18:
                mult = 2
            elif w == "19-36" and 19 <= rolled <= 36:
                mult = 2

            payout = einsatz * mult
            if payout > 0:
                update_balance(uid, payout)
            _record_net(interaction.user, payout - einsatz)

            embed = discord.Embed(
                title=f"🎡 Ergebnis: {col_emoji} {rolled}",
                description=(
                    f"**Deine Wette:** `{wette}`\n"
                    f"{'🎉 **Gewonnen:** ' + f'{payout:,} 🪙 (×{mult})' if payout > 0 else f'💸 **Verloren:** -{einsatz:,} 🪙'}"
                ),
                color=COLOR_GOLD if mult >= 36 else (COLOR_SUCCESS if payout > 0 else COLOR_ERROR),
            )
            embed.set_footer(text=f"Kontostand: {get_balance(uid):,} 🪙")
            await interaction.edit_original_response(embed=embed)
        finally:
            active_games.pop(uid, None)

    @app_commands.command(name="plinko", description="🔴 Plinko – Lass die Kugel durch die Pyramide fallen!")
    @app_commands.describe(einsatz="Dein Einsatz in Coins (max 2.500)")
    @is_gambling_channel()
    async def cmd_plinko(self, interaction: discord.Interaction, einsatz: int) -> None:
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        uid = interaction.user.id
        active_games[uid] = "Plinko"
        update_balance(uid, -einsatz)
        try:
            pos = 4
            path = []
            for row in range(PLINKO_ROWS):
                d = random.choice([-1, 1])
                pos = max(0, min(8, pos + d))
                path.append({"row": row, "pos": pos, "dir": d})

            final_slot = pos
            multiplier = PLINKO_MULTIPLIERS[final_slot]
            win = int(einsatz * multiplier)
            profit = win - einsatz

            def render_board(ball_row: int = -1, ball_pos: int = -1, finished: bool = False) -> str:
                lines = []
                for r in range(PLINKO_ROWS):
                    pin_cnt = r + 3
                    pad = (PLINKO_ROWS + 2) - pin_cnt
                    row_s = " " * pad
                    for p in range(pin_cnt):
                        if r == ball_row:
                            ps = int(p / (pin_cnt - 1) * 8) if pin_cnt > 1 else 4
                            row_s += "🔴" if ps == ball_pos else "⚪"
                        else:
                            row_s += "⚪"
                    lines.append(row_s)
                mult_s = "".join(
                    "🔴" if (finished and i == final_slot) else PLINKO_COLORS.get(m, "⬜")
                    for i, m in enumerate(PLINKO_MULTIPLIERS)
                )
                lines.append(mult_s)
                return "\n".join(lines)

            embed = discord.Embed(
                title="🔴 Plinko",
                description=f"**Einsatz:** {einsatz:,} 🪙\n\n```\n{render_board()}\n```",
                color=COLOR_INFO,
            )
            await interaction.response.send_message(embed=embed)

            for step in range(0, PLINKO_ROWS, 2):
                await asyncio.sleep(0.55)
                p = path[step]
                embed.description = f"**Einsatz:** {einsatz:,} 🪙\n\n```\n{render_board(step, p['pos'])}\n```"
                try:
                    await interaction.edit_original_response(embed=embed)
                except Exception:
                    break

            await asyncio.sleep(0.6)
            if win > 0:
                update_balance(uid, win)
            _record_net(interaction.user, profit)

            embed = discord.Embed(
                title=f"{'🎉' if multiplier >= 1.5 else '💀'} Plinko – ×{multiplier}",
                description=(
                    f"```\n{render_board(finished=True)}\n```\n"
                    f"**Einsatz:** {einsatz:,} 🪙 • **Auszahlung:** {win:,} 🪙 ({'+' if profit >= 0 else ''}{profit:,} 🪙)"
                ),
                color=COLOR_GOLD if multiplier >= 2.5 else (COLOR_SUCCESS if profit >= 0 else COLOR_ERROR),
            )
            embed.set_footer(text=f"Kontostand: {get_balance(uid):,} 🪙")
            await interaction.edit_original_response(embed=embed)
        finally:
            active_games.pop(uid, None)

    @app_commands.command(name="vault", description="🔐 Versuche den 3-stelligen Tresor-Code zu knacken!")
    @app_commands.describe(code="Dein 3-stelliger Code-Versuch (000-999)")
    @is_gambling_channel()
    async def cmd_vault(self, interaction: discord.Interaction, code: str) -> None:
        uid = interaction.user.id
        attempt_cost = 25
        c = code.strip()
        if not c.isdigit() or len(c) != 3:
            await interaction.response.send_message(
                "❌ Der Code muss exakt 3 Ziffern haben (z.B. `042`).", ephemeral=True
            )
            return
        if get_balance(uid) < attempt_cost:
            await interaction.response.send_message(
                f"❌ Ein Versuch kostet **{attempt_cost} 🪙**!", ephemeral=True
            )
            return

        update_balance(uid, -attempt_cost)
        vault = get_vault_data()
        vault["attempts"] = int(vault.get("attempts", 0)) + 1
        vault["jackpot"] = int(vault.get("jackpot", 1000)) + attempt_cost

        if c == vault["code"]:
            jackpot = vault["jackpot"]
            attempts = vault["attempts"]
            update_balance(uid, jackpot)
            _record_net(interaction.user, jackpot - attempt_cost)
            vault["code"] = generate_vault_code()
            vault["jackpot"] = 1000
            vault["attempts"] = 0
            save_vault(vault)

            embed = discord.Embed(
                title="🔓 TRESOR GEKNACKT! 🔓",
                description=(
                    f"**{interaction.user.mention}** hat den Code **`{c}`** nach **{attempts}** Versuchen geknackt!\n\n"
                    f"💰 **JACKPOT: {jackpot:,} 🪙**"
                ),
                color=COLOR_GOLD,
            )
            await interaction.response.send_message(embed=embed)
        else:
            save_vault(vault)
            _record_net(interaction.user, -attempt_cost)
            secret = vault["code"]
            exact = sum(1 for a, b in zip(c, secret) if a == b)
            close = sum(1 for a, b in zip(c, secret) if a != b and abs(int(a) - int(b)) == 1)
            hints = []
            if exact:
                hints.append(f"🟢 **{exact}** Ziffer(n) exakt richtig!")
            if close:
                hints.append(f"🟡 **{close}** Ziffer(n) um ±1 daneben!")
            if not hints:
                hints.append("🔴 Keine Ziffer stimmt oder ist nah dran.")

            embed = discord.Embed(
                title="🔐 Falscher Code!",
                description=(
                    f"**Versuch:** `{c}`\n" + "\n".join(hints) + "\n\n"
                    f"🏦 **Tresor-Jackpot:** {vault['jackpot']:,} 🪙 *(Versuche: {vault['attempts']})*"
                ),
                color=COLOR_WARNING,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="vaultinfo", description="🏦 Zeigt den aktuellen Tresor-Jackpot")
    @is_gambling_channel()
    async def cmd_vaultinfo(self, interaction: discord.Interaction) -> None:
        v = get_vault_data()
        embed = discord.Embed(
            title="🏦 The Vault – Status",
            description=(
                f"💰 **Aktueller Jackpot:** `{v.get('jackpot', 1000):,} 🪙`\n"
                f"🔢 **Bisherige Versuche:** `{v.get('attempts', 0)}`\n\n"
                f"Nutze `/vault <000-999>` für **25 🪙** pro Versuch!"
            ),
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Multiplayer Games ─────────────────────────────────────────────────

    @app_commands.command(name="duel", description="⚔️ Fordere einen User zum 1v1 Würfel-Duell heraus!")
    @app_commands.describe(gegner="Dein Gegner", einsatz="Einsatz pro Person")
    @is_gambling_channel()
    async def cmd_duel(
        self, interaction: discord.Interaction, gegner: discord.Member, einsatz: int
    ) -> None:
        if gegner.bot or gegner.id == interaction.user.id:
            await interaction.response.send_message("❌ Ungültiger Gegner!", ephemeral=True)
            return
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        update_balance(interaction.user.id, -einsatz)
        active_games[interaction.user.id] = "Duell"
        active_games.add_escrow(interaction.user.id, einsatz, "Duell")
        view = DuelAcceptView(interaction.user, gegner, einsatz)
        embed = discord.Embed(
            title="⚔️ Würfel-Duell Herausforderung!",
            description=(
                f"{interaction.user.mention} fordert {gegner.mention} heraus!\n"
                f"💰 **Einsatz:** {einsatz:,} 🪙 pro Person"
            ),
            color=COLOR_WARNING,
        )
        await interaction.response.send_message(content=gegner.mention, embed=embed, view=view)

    @app_commands.command(name="rr", description="🔫 Starte eine Lobby für Russisches Roulette (2–8 Spieler)!")
    @app_commands.describe(einsatz="Einsatz pro Person (max 2.500)")
    @is_gambling_channel()
    async def cmd_rr(self, interaction: discord.Interaction, einsatz: int) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        if not await check_bet(interaction, einsatz):
            return
        if interaction.channel_id in active_rr_games:
            await interaction.response.send_message(
                "❌ In diesem Kanal läuft bereits Russisches Roulette!", ephemeral=True
            )
            return
        update_balance(interaction.user.id, -einsatz)
        active_rr_games[interaction.channel_id] = True
        active_rr_games.add_player_escrow(interaction.channel_id, interaction.user.id, einsatz)
        view = RussianRouletteJoinView(interaction.user, einsatz, interaction.channel_id)
        embed = discord.Embed(
            title="🔫 Russisches Roulette – Lobby geöffnet!",
            description=(
                f"**Host:** {interaction.user.mention}\n"
                f"💰 **Einsatz:** {einsatz:,} 🪙 pro Person\n"
                f"👥 **Spieler:** 1/{RR_MAX_PLAYERS}\n\n"
                f"Klicke auf **🔫 Mitspielen!** zum Beitreten."
            ),
            color=COLOR_RR,
        )
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="connect4", description="🟡🔴 Fordere jemanden zu 4 Gewinnt um Coins heraus!")
    @app_commands.describe(gegner="Dein Gegner", einsatz="Einsatz pro Person")
    @is_gambling_channel()
    async def cmd_connect4(
        self, interaction: discord.Interaction, gegner: discord.Member, einsatz: int
    ) -> None:
        if gegner.bot or gegner.id == interaction.user.id:
            await interaction.response.send_message("❌ Ungültiger Gegner!", ephemeral=True)
            return
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        update_balance(interaction.user.id, -einsatz)
        active_games[interaction.user.id] = "4 Gewinnt"
        active_games.add_escrow(interaction.user.id, einsatz, "4 Gewinnt")
        view = Connect4AcceptView(interaction.user, gegner, einsatz)
        embed = discord.Embed(
            title="🟡🔴 4 Gewinnt – Herausforderung!",
            description=(
                f"{interaction.user.mention} fordert {gegner.mention} heraus!\n"
                f"💰 **Einsatz:** {einsatz:,} 🪙 • **Pot:** {einsatz * 2:,} 🪙"
            ),
            color=COLOR_CONNECT4,
        )
        await interaction.response.send_message(content=gegner.mention, embed=embed, view=view)

    @app_commands.command(name="tictactoe", description="❌⭕ Fordere jemanden zu Tic Tac Toe um Coins heraus!")
    @app_commands.describe(gegner="Dein Gegner", einsatz="Einsatz pro Person")
    @is_gambling_channel()
    async def cmd_tictactoe(
        self, interaction: discord.Interaction, gegner: discord.Member, einsatz: int
    ) -> None:
        if gegner.bot or gegner.id == interaction.user.id:
            await interaction.response.send_message("❌ Ungültiger Gegner!", ephemeral=True)
            return
        if not await check_active_game(interaction) or not await check_bet(interaction, einsatz):
            return
        update_balance(interaction.user.id, -einsatz)
        active_games[interaction.user.id] = "Tic Tac Toe"
        active_games.add_escrow(interaction.user.id, einsatz, "Tic Tac Toe")
        view = TicTacToeAcceptView(interaction.user, gegner, einsatz)
        embed = discord.Embed(
            title="❌⭕ Tic Tac Toe – Herausforderung!",
            description=(
                f"{interaction.user.mention} fordert {gegner.mention} heraus!\n"
                f"💰 **Einsatz:** {einsatz:,} 🪙 • **Pot:** {einsatz * 2:,} 🪙"
            ),
            color=COLOR_TICTACTOE,
        )
        await interaction.response.send_message(content=gegner.mention, embed=embed, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GamblingCog(bot))
