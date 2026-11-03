"""
discord_bot_bridge.py
Kommuniziert mit dem Discord-Bot: Gibt Rollen, sendet DMs, etc.
Nutzt direkt die Discord-REST-API (kein discord.py nötig).
"""

import os
import aiohttp

DISCORD_BOT_TOKEN    = os.getenv("DISCORD_TOKEN", "")
VERIFIED_ROLE_ID     = int(os.getenv("VERIFIED_ROLE_ID", "0"))
GUILD_ID             = int(os.getenv("GUILD_ID", "0"))
WELCOME_CHANNEL_ID   = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
VERIFIER_URL         = os.getenv("VERIFIER_URL", "")   # z.B. https://xyz.ngrok-free.dev

DISCORD_API = "https://discord.com/api/v10"


def _bh() -> dict:
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }


async def give_verified_role(discord_id: str) -> bool:
    """Gibt dem User die Verified-Rolle."""
    if not VERIFIED_ROLE_ID or not GUILD_ID:
        return False
    try:
        url = f"{DISCORD_API}/guilds/{GUILD_ID}/members/{discord_id}/roles/{VERIFIED_ROLE_ID}"
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=_bh()) as resp:
                return resp.status in (204, 200)
    except Exception as e:
        print(f"❌ [Bridge] give_verified_role Fehler: {e}")
        return False


async def remove_verified_role(discord_id: str) -> bool:
    """Entfernt die Verified-Rolle (bei Ban)."""
    if not VERIFIED_ROLE_ID or not GUILD_ID:
        return False
    try:
        url = f"{DISCORD_API}/guilds/{GUILD_ID}/members/{discord_id}/roles/{VERIFIED_ROLE_ID}"
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=_bh()) as resp:
                return resp.status in (204, 200)
    except Exception as e:
        print(f"❌ [Bridge] remove_verified_role Fehler: {e}")
        return False


async def send_dm(discord_id: str, content: str = "", embed: dict = None) -> bool:
    """Sendet eine DM an einen User."""
    try:
        # DM-Kanal erstellen
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/users/@me/channels",
                headers=_bh(),
                json={"recipient_id": discord_id},
            ) as resp:
                if not resp.ok:
                    return False
                dm = await resp.json()
                channel_id = dm["id"]

            # Nachricht senden
            payload = {}
            if content:
                payload["content"] = content
            if embed:
                payload["embeds"] = [embed]

            async with session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=_bh(),
                json=payload,
            ) as resp:
                return resp.ok

    except Exception as e:
        print(f"❌ [Bridge] send_dm Fehler: {e}")
        return False


async def send_verification_dm(discord_id: str, discord_name: str) -> bool:
    """Sendet die Verifikations-DM mit dem Link."""
    if not VERIFIER_URL:
        return False

    embed = {
        "title": "✅ Verifikation erforderlich",
        "description": (
            f"Hey **{discord_name}**! Willkommen auf dem Server.\n\n"
            f"Um Zugang zu allen Kanälen zu erhalten, musst du dich einmalig verifizieren.\n\n"
            f"**Klicke auf den Button unten oder den Link:**\n"
            f"[🔗 Jetzt verifizieren]({VERIFIER_URL}/verify)"
        ),
        "color": 0x6366f1,
        "footer": {"text": "ByGorgii Community • Verifikation"},
    }

    return await send_dm(discord_id, embed=embed)


async def send_welcome_channel_message(discord_id: str, discord_name: str) -> bool:
    """Sendet eine Nachricht im Welcome-Kanal."""
    if not WELCOME_CHANNEL_ID or not VERIFIER_URL:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/channels/{WELCOME_CHANNEL_ID}/messages",
                headers=_bh(),
                json={
                    "content": (
                        f"👋 Willkommen <@{discord_id}>!\n"
                        f"Verifiziere dich hier um Zugang zu erhalten: "
                        f"**{VERIFIER_URL}/verify**"
                    )
                },
            ) as resp:
                return resp.ok
    except Exception as e:
        print(f"❌ [Bridge] welcome Fehler: {e}")
        return False


async def get_guild_member(discord_id: str) -> dict | None:
    """Holt Infos über ein Guild-Mitglied."""
    if not GUILD_ID:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DISCORD_API}/guilds/{GUILD_ID}/members/{discord_id}",
                headers=_bh(),
            ) as resp:
                if resp.ok:
                    return await resp.json()
    except Exception:
        pass
    return None
