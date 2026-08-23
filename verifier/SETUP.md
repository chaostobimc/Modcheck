# ByGorgii Verifier — Setup

## Installation

```bash
cd verifier
pip install -r requirements.txt
```

## .env Variablen (zur bestehenden .env hinzufügen)

```env
# Verifier
VERIFIER_PORT=8000
VERIFIER_URL=https://deine-ngrok-url.ngrok-free.dev

# Discord OAuth2 (gleiche App wie das Dashboard ODER neue App)
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
DISCORD_REDIRECT_URI=https://deine-ngrok-url.ngrok-free.dev/callback

# Welche Rolle bekommt der User nach der Verifikation?
VERIFIED_ROLE_ID=1234567890

# Welcome-Kanal (optional, falls DMs deaktiviert sind)
WELCOME_CHANNEL_ID=1234567890

# IP-Hashing (WICHTIG: langer zufälliger String!)
IP_SALT=irgendein_langer_zufaelliger_string_hier

# Session-Secret (für Cookie-Signierung)
SESSION_SECRET=noch_ein_zufaelliger_string

# Webhook-Secret (damit nur der Bot den /webhook Endpoint aufrufen kann)
WEBHOOK_SECRET=geheimer_token_fuer_webhook

# Datenbankpfad (optional)
VERIFIER_DB_PATH=verifier.db
```

## Discord Developer Portal

1. https://discord.com/developers/applications → deine App (oder neue erstellen)
2. OAuth2 → Redirects → `https://deine-ngrok-url.ngrok-free.dev/callback` hinzufügen
3. Scope: `identify`

## Starten

```bash
# In separatem Terminal:
cd verifier
python main.py

# Oder mit PM2:
pm2 start "python main.py" --name "verifier" --cwd /pfad/zu/verifier
```

## Wie es funktioniert

1. Neuer User joint Discord
2. Bot sendet automatisch DM mit Verifikations-Link
3. User klickt auf Link → öffnet Verifier-Website
4. User loggt sich mit Discord ein (OAuth2)
5. System prüft:
   - A) Ist Discord-ID gebannt? → Zugang verweigert
   - B) Ist IP-Hash mit gebanntem Account verknüpft? → Auto-Ban als Alt-Account
   - C) Alles ok → Verified-Rolle vergeben, Daten gespeichert
6. User hat Zugang

## Admin-Panel

`https://deine-ngrok-url.ngrok-free.dev/admin`

Nur für User die in ADMIN_USER_IDS eingetragen sind.

Features:
- Alle verifizierten User sehen
- Suche nach Name, ID, ISP, Land
- User einzeln anschauen (alle IPs, verknüpfte Accounts)
- Bannen (mit automatischem Alt-Account-Mitbann)
- Entbannen

## Ordnerstruktur

```
verifier/
├── main.py           ← FastAPI App
├── database.py       ← SQLite + alle DB-Funktionen
├── ip_check.py       ← proxycheck.io Integration + IP-Extraktion
├── discord_bot_bridge.py  ← Rolle vergeben, DMs senden
├── requirements.txt
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── verify.html
│   ├── success.html
│   ├── blocked.html
│   ├── admin.html
│   ├── admin_user.html
│   └── admin_action.html
└── verifier.db       ← wird automatisch erstellt
```
