"""Gemeinsame Berechtigungs-Checks, Zeit-Parser & Log-Helper."""
from __future__ import annotations

import re
import discord
from discord import app_commands

from bot_core.config import (
    ADMIN_ROLE_NAME,
    ADMIN_USER_IDS,
    ALLOWED_CHANNEL_ID,
    COLOR_ERROR,
    FISHING_CHANNEL_ID,
    GAMBLING_CHANNEL_ID,
    LOG_CHANNEL_ID,
    MOD_ROLE_NAME,
)


def parse_time(time_str: str) -> int | None:
    """Parst Zeitangaben wie 10s, 5m, 2h, 1d in Sekunden."""
    match = re.match(r"^(\d+)\s*([smhd])$", time_str.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return amount * multipliers[unit]


def is_user_admin(member: discord.abc.User) -> bool:
    if member.id in ADMIN_USER_IDS:
        return True
    if isinstance(member, discord.Member):
        if member.guild_permissions.administrator:
            return True
        if any(role.name == ADMIN_ROLE_NAME for role in member.roles):
            return True
    return False


def has_mod_role(member: discord.abc.User) -> bool:
    if is_user_admin(member):
        return True
    if isinstance(member, discord.Member):
        return any(role.name == MOD_ROLE_NAME for role in member.roles)
    return False


def is_allowed_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if ALLOWED_CHANNEL_ID == 0 or interaction.channel_id == ALLOWED_CHANNEL_ID:
            return True
        if is_user_admin(interaction.user):
            return True
        embed = discord.Embed(
            title="❌ Falscher Kanal",
            description=f"Dieser Befehl ist nur in <#{ALLOWED_CHANNEL_ID}> verfügbar.",
            color=COLOR_ERROR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    return app_commands.check(predicate)


def is_gambling_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if GAMBLING_CHANNEL_ID == 0 or interaction.channel_id == GAMBLING_CHANNEL_ID:
            return True
        await interaction.response.send_message(
            f"🎰 Gambling ist nur in <#{GAMBLING_CHANNEL_ID}> erlaubt!",
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


def is_fishing_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if FISHING_CHANNEL_ID == 0 or interaction.channel_id == FISHING_CHANNEL_ID:
            return True
        await interaction.response.send_message(
            f"🎣 Angeln & Jobs sind nur in <#{FISHING_CHANNEL_ID}> erlaubt!",
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


def is_admin_user():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_user_admin(interaction.user):
            return True
        embed = discord.Embed(
            title="❌ Keine Berechtigung",
            description="Dieser Befehl ist nur für **Administratoren** verfügbar.",
            color=COLOR_ERROR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    return app_commands.check(predicate)


async def send_log(embed: discord.Embed, bot: discord.Client) -> None:
    """Sendet ein Embed sicher in den konfigurierten Discord-Log-Kanal."""
    if not LOG_CHANNEL_ID:
        return
    try:
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel is None:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(embed=embed)
    except Exception as e:
        print(f"⚠️ Fehler beim Senden des Logs: {e}")
