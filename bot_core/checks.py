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
    GUILD_ID,
    LOG_CHANNEL_ID,
    MOD_ROLE_NAME,
)
from bot_core.server_store import get_dynamic_channel_id, get_dynamic_role_id, server_cfg_store


def parse_time(time_str: str) -> int | None:
    """Parst Zeitangaben wie 10s, 5m, 2h, 1d, 1w in Sekunden."""
    match = re.match(r"^(\d+)\s*([smhdw])$", time_str.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return amount * multipliers[unit]


def is_user_admin(member: discord.abc.User) -> bool:
    if member.id in ADMIN_USER_IDS:
        return True
    if isinstance(member, discord.Member):
        if member.guild_permissions.administrator:
            return True
        dyn_admin_rid = get_dynamic_role_id(member.guild.id, "admin_role_id", 0)
        if dyn_admin_rid:
            return any(role.id == dyn_admin_rid for role in member.roles)
        if any(role.name == ADMIN_ROLE_NAME for role in member.roles):
            return True
    return False


def has_mod_role(member: discord.abc.User) -> bool:
    if is_user_admin(member):
        return True
    if isinstance(member, discord.Member):
        if member.guild_permissions.moderate_members or member.guild_permissions.manage_messages:
            return True
        dyn_mod_rid = get_dynamic_role_id(member.guild.id, "mod_role_id", 0)
        if dyn_mod_rid:
            return any(role.id == dyn_mod_rid for role in member.roles)
        return any(role.name == MOD_ROLE_NAME for role in member.roles)
    return False


def is_allowed_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        target_ch = get_dynamic_channel_id(interaction.guild_id, "allowed_channel_id", ALLOWED_CHANNEL_ID)
        if target_ch == 0 or interaction.channel_id == target_ch:
            return True
        if is_user_admin(interaction.user):
            return True
        embed = discord.Embed(
            title="❌ Falscher Kanal",
            description=f"Dieser Befehl ist nur in <#{target_ch}> verfügbar.",
            color=COLOR_ERROR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    return app_commands.check(predicate)


def is_gambling_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        target_ch = get_dynamic_channel_id(interaction.guild_id, "gambling_channel_id", GAMBLING_CHANNEL_ID)
        if target_ch == 0 or interaction.channel_id == target_ch:
            return True
        await interaction.response.send_message(
            f"🎰 Gambling ist nur in <#{target_ch}> erlaubt!",
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


def is_fishing_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        target_ch = get_dynamic_channel_id(interaction.guild_id, "fishing_channel_id", FISHING_CHANNEL_ID)
        if target_ch == 0 or interaction.channel_id == target_ch:
            return True
        await interaction.response.send_message(
            f"🎣 Angeln & Jobs sind nur in <#{target_ch}> erlaubt!",
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


async def send_log(embed: discord.Embed, bot: discord.Client, guild_id: int | None = None) -> None:
    """Sendet ein Embed sicher in den konfigurierten Discord-Log-Kanal."""
    gid = int(guild_id or GUILD_ID or 0)
    cfg_logs = server_cfg_store.get_guild(gid).get("logs", {}) if gid else {}
    target_id = (
        int((cfg_logs.get("mod") or {}).get("channel_id") or 0)
        or int(cfg_logs.get("mod_log_channel_id") or 0)
        or get_dynamic_channel_id(gid, "log_channel_id", LOG_CHANNEL_ID)
    )
    if not target_id:
        return
    try:
        log_channel = bot.get_channel(target_id)
        if log_channel is None:
            log_channel = await bot.fetch_channel(target_id)
        if log_channel:
            await log_channel.send(embed=embed)
    except Exception as e:
        print(f"⚠️ Fehler beim Senden des Logs: {e}")
