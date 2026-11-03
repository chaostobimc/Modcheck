"""
main.py – ByGorgii Verifikations-Server
FastAPI + Discord OAuth2 + IP-Check + Anti-Alt-System
"""

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

# Eigene Module
from database import (
    init_db, hash_ip,
    get_user_by_discord_id, get_banned_entry_by_ip_hash,
    upsert_user, ban_discord_id, unban_discord_id,
    get_all_entries_for_discord_id, get_all_discord_ids_for_ip_hash,
    get_recent_verifications, get_all_banned, search_users, get_stats,
)
from ip_check import check_ip, get_real_ip
from discord_bot_bridge import (
    give_verified_role, remove_verified_role,
    send_verification_dm, send_welcome_channel_message,
)

# ── Config ───────────────────────────────────────────────────────
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/callback")
ADMIN_USER_IDS_RAW    = os.getenv("ADMIN_USER_IDS", "")
SESSION_SECRET        = os.getenv("SESSION_SECRET", secrets.token_hex(32))
VERIFIER_URL          = os.getenv("VERIFIER_URL", "http://localhost:8000")

ADMIN_USER_IDS: set[str] = {
    x.strip() for x in ADMIN_USER_IDS_RAW.split(",") if x.strip()
}

DISCORD_API    = "https://discord.com/api/v10"
DISCORD_OAUTH  = "https://discord.com/api/oauth2"

# ── FastAPI Setup ─────────────────────────────────────────────────
app = FastAPI(title="ByGorgii Verifier", docs_url=None, redoc_url=None)

_here = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_here, "templates"))

try:
    app.mount("/static", StaticFiles(directory=os.path.join(_here, "static")), name="static")
except Exception:
    pass

# ── Session-Verwaltung (signierte Cookies) ────────────────────────
import hmac as _hmac
import json
import base64


def _sign(data: dict) -> str:
    payload = base64.b64encode(json.dumps(data).encode()).decode()
    sig = _hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _unsign(token: str) -> dict | None:
    try:
        payload, sig = token.rsplit(".", 1)
        expected = _hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig, expected):
            return None
        return json.loads(base64.b64decode(payload).decode())
    except Exception:
        return None


def get_session(request: Request) -> dict:
    token = request.cookies.get("session")
    if not token:
        return {}
    return _unsign(token) or {}


def session_response(response, data: dict):
    response.set_cookie(
        "session", _sign(data),
        httponly=True, samesite="lax",
        max_age=60 * 60 * 24,  # 24h
    )
    return response


def clear_session(response):
    response.delete_cookie("session")
    return response


# ── Discord OAuth2 ────────────────────────────────────────────────
import aiohttp


def discord_oauth_url(state: str = "") -> str:
    params = {
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
    }
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    return f"https://discord.com/api/oauth2/authorize?{qs}"


async def exchange_code(code: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DISCORD_OAUTH}/token",
            data={
                "client_id":     DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()


async def get_discord_user(token: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()


# ── Startup ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    init_db()
    print(f"🌐 Verifier läuft | Admin-IDs: {ADMIN_USER_IDS}")


# ══════════════════════════════════════════════════════════════════
#   ÖFFENTLICHE ROUTEN
# ══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    session = get_session(request)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "session": session,
        "verifier_url": VERIFIER_URL,
    })


@app.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request):
    """Die Seite die User nach dem Discord-Beitritt aufrufen."""
    session = get_session(request)
    return templates.TemplateResponse("verify.html", {
        "request": request,
        "session": session,
        "oauth_url": discord_oauth_url(state="verify"),
    })


@app.get("/login")
async def login(request: Request):
    """Leitet zum Discord OAuth weiter."""
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse(discord_oauth_url(state=state))
    resp.set_cookie("oauth_state", state, httponly=True, max_age=300)
    return resp


@app.get("/callback")
async def callback(request: Request, code: str = "", error: str = "", state: str = ""):
    """Discord OAuth2 Callback."""
    if error or not code:
        return RedirectResponse("/verify?error=oauth_denied")

    try:
        token_data = await exchange_code(code)
        user       = await get_discord_user(token_data["access_token"])
    except Exception as e:
        print(f"❌ [OAuth] Fehler: {e}")
        return RedirectResponse("/verify?error=oauth_failed")

    discord_id   = user["id"]
    discord_name = user.get("global_name") or user.get("username", "?")
    avatar       = user.get("avatar")
    avatar_url   = (
        f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.webp?size=128"
        if avatar else
        f"https://cdn.discordapp.com/embed/avatars/{int(user.get('discriminator','0'))%5}.png"
    )

    # Session setzen (ohne IP-Prüfung – erst auf /do-verify)
    resp = RedirectResponse("/do-verify", status_code=302)
    session_response(resp, {
        "discord_id":   discord_id,
        "discord_name": discord_name,
        "avatar_url":   avatar_url,
    })
    return resp


@app.get("/do-verify")
async def do_verify(request: Request):
    """
    Führt die eigentliche Verifikation durch:
    1. IP holen & hashen
    2. proxycheck.io abfragen
    3. Anti-Alt-Check
    4. Rolle vergeben
    """
    session = get_session(request)
    if not session.get("discord_id"):
        return RedirectResponse("/verify?error=not_logged_in")

    discord_id   = session["discord_id"]
    discord_name = session["discord_name"]

    # ── Schritt A: Ist diese Discord-ID bereits gebannt? ─────────
    existing = get_user_by_discord_id(discord_id)
    if existing and existing["is_banned"]:
        resp = RedirectResponse("/blocked?reason=banned")
        return resp

    # ── IP ermitteln ─────────────────────────────────────────────
    real_ip = get_real_ip(request)
    ip_hash = hash_ip(real_ip)

    # ── Schritt B: Ist diese IP mit einem gebannten Account verknüpft? ──
    banned_by_ip = get_banned_entry_by_ip_hash(ip_hash)
    if banned_by_ip:
        # Alt-Account erkannt → sofort bannen
        upsert_user(
            discord_id=discord_id,
            discord_name=discord_name,
            ip_hash=ip_hash,
            is_banned=True,
        )
        # Alle Einträge dieser Discord-ID bannen
        ban_discord_id(
            discord_id=discord_id,
            reason=f"Alt-Account erkannt via IP-Kette (Original: {banned_by_ip['discord_id']})",
            admin_id="SYSTEM",
            admin_name="Auto-Ban",
        )
        print(f"🚫 [Verifier] Alt-Account erkannt: {discord_name} ({discord_id})")
        return RedirectResponse("/blocked?reason=alt_account")

    # ── IP-Infos holen ────────────────────────────────────────────
    ip_info = await check_ip(real_ip)

    # ── User in DB eintragen ──────────────────────────────────────
    upsert_user(
        discord_id=discord_id,
        discord_name=discord_name,
        ip_hash=ip_hash,
        country=ip_info.country,
        country_code=ip_info.country_code,
        city=ip_info.city,
        isp=ip_info.isp,
        is_vpn=ip_info.is_vpn,
        is_proxy=ip_info.is_proxy,
        is_banned=False,
    )

    # ── Verified-Rolle vergeben ───────────────────────────────────
    role_given = await give_verified_role(discord_id)

    print(
        f"✅ [Verifier] {discord_name} ({discord_id}) verifiziert | "
        f"{ip_info.country_code} | {ip_info.isp} | "
        f"VPN={ip_info.is_vpn} | Rolle={'✅' if role_given else '❌'}"
    )

    resp = RedirectResponse("/success", status_code=302)
    session_response(resp, {**session, "verified": True, "ip_info": {
        "country": ip_info.country,
        "country_code": ip_info.country_code,
        "city": ip_info.city,
        "isp": ip_info.isp,
        "is_vpn": ip_info.is_vpn,
    }})
    return resp


@app.get("/success", response_class=HTMLResponse)
async def success(request: Request):
    session = get_session(request)
    return templates.TemplateResponse("success.html", {
        "request": request,
        "session": session,
    })


@app.get("/blocked", response_class=HTMLResponse)
async def blocked(request: Request, reason: str = "banned"):
    return templates.TemplateResponse("blocked.html", {
        "request": request,
        "reason": reason,
    })


@app.get("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/")
    clear_session(resp)
    return resp


# ══════════════════════════════════════════════════════════════════
#   ADMIN-PANEL
# ══════════════════════════════════════════════════════════════════

def require_admin(request: Request):
    session = get_session(request)
    uid = session.get("discord_id")
    if not uid or uid not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="Kein Zugang")
    return session


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, q: str = ""):
    session = require_admin(request)
    stats   = get_stats()

    if q:
        users = search_users(q)
    else:
        users = get_recent_verifications(50)

    banned = get_all_banned(100)

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "session": session,
        "stats":   stats,
        "users":   [dict(u) for u in users],
        "banned":  [dict(b) for b in banned],
        "query":   q,
    })


@app.get("/admin/user/{discord_id}", response_class=HTMLResponse)
async def admin_user_detail(request: Request, discord_id: str):
    require_admin(request)
    entries    = get_all_entries_for_discord_id(discord_id)
    linked_ids = []
    for e in entries:
        linked = get_all_discord_ids_for_ip_hash(e["ip_hash"])
        linked_ids.extend(linked)
    linked_ids = list(set(linked_ids) - {discord_id})

    return templates.TemplateResponse("admin_user.html", {
        "request":    request,
        "discord_id": discord_id,
        "entries":    [dict(e) for e in entries],
        "linked_ids": linked_ids,
        "session":    get_session(request),
    })


@app.post("/admin/ban")
async def admin_ban(
    request: Request,
    discord_id: str = Form(...),
    reason: str     = Form(""),
):
    session = require_admin(request)
    admin_id   = session["discord_id"]
    admin_name = session.get("discord_name", admin_id)

    extra = ban_discord_id(discord_id, reason, admin_id, admin_name)
    await remove_verified_role(discord_id)

    return templates.TemplateResponse("admin_action.html", {
        "request": request,
        "session": session,
        "title":   "Ban durchgeführt",
        "message": (
            f"**{discord_id}** wurde gebannt.\n"
            f"Zusätzlich {extra} Alt-Account(s) via IP-Kette mitgebannt."
        ),
        "success": True,
    })


@app.post("/admin/unban")
async def admin_unban(
    request: Request,
    discord_id: str = Form(...),
):
    session = require_admin(request)
    admin_id   = session["discord_id"]
    admin_name = session.get("discord_name", admin_id)

    unban_discord_id(discord_id, admin_id, admin_name)
    await give_verified_role(discord_id)

    return templates.TemplateResponse("admin_action.html", {
        "request": request,
        "session": session,
        "title":   "Unban durchgeführt",
        "message": f"**{discord_id}** wurde entbannt und hat die Verified-Rolle zurückbekommen.",
        "success": True,
    })


# ── Discord-Bot Webhook: on_member_join ───────────────────────────
@app.post("/webhook/member-join")
async def webhook_member_join(request: Request):
    """
    Wird vom Discord-Bot aufgerufen wenn ein neuer User joint.
    Sendet automatisch die Verifikations-DM.
    """
    # Einfacher Token-Check
    auth = request.headers.get("X-Bot-Token", "")
    if auth != os.getenv("WEBHOOK_SECRET", ""):
        raise HTTPException(status_code=401)

    data = await request.json()
    discord_id   = data.get("discord_id", "")
    discord_name = data.get("discord_name", "")

    if not discord_id:
        raise HTTPException(status_code=400)

    # DM senden
    dm_ok = await send_verification_dm(discord_id, discord_name)
    # Optional: Welcome-Channel Nachricht
    await send_welcome_channel_message(discord_id, discord_name)

    return JSONResponse({"ok": True, "dm_sent": dm_ok})


# ── API: Status ───────────────────────────────────────────────────
@app.get("/api/stats")
async def api_stats(request: Request):
    require_admin(request)
    return JSONResponse(get_stats())


# ── Startpunkt ────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("VERIFIER_PORT", "8000"))
    print(f"🌐 Verifier startet auf http://0.0.0.0:{port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
