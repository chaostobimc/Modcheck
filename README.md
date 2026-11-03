# 🚀 Modcheck v2.1 — Discord & Twitch Bot

Ein modularer, extrem ressourcenschonender All-in-One Community-Bot für **Discord** und **Twitch** — optimiert für den Dauerbetrieb (24/7) auf einem **Raspberry Pi 5**.

---

## ✨ Features im Überblick

- 📺 **Präzises Twitch-Aktivitäts-Tracking & Mod-Monitor** (`cogs/activity.py` & `bot_core/twitch_service.py`)
  - **5-Minuten-Präsenzfenster (`slots`) & Schichtzeiten**: Erfasst nicht nur reine Nachrichtenzahlen, sondern die echte aktive Mod-Begleitzeit pro Stream (immun gegen Spam!)
  - **Fairer Aktivitäts-Score (0–100 Pkt.) & fairer Tracking-Start (`connected_at`)**: Neue Mods werden nicht rückwirkend für ältere Streams vor ihrer Registrierung abgestraft
  - **Stream-Metadaten & Historie (`/streams`)**: Speichert automatisch Stream-Titel, Kategorien, Peak-Viewer, Dauer und anwesende Mods
  - **Team-Monitor (`/teamstatus`, `/checkinactive`)**: Live-Überblick über alle Mods, Vormonats-Trends (`📈`/`📉`), 24h-Stundenprofil & Wochentags-Analyse
  - **Interaktive Ansichten**: Direktes Blättern durch Monate, Jahresübersicht & Zeiträume in `/activity`, `/myactivity`, `/leaderboard` sowie 1-Klick-Stornierung in `/absences`
- 🎵 **Musik & Radio Player** (`cogs/music.py`)
  - YouTube-Suche & Direktlinks mit automatischem Multi-Client-Fallback (`tv`, `android`, `ios`, `web`) gegen HTTP-403-Fehler
  - Spotify-Tracks & Playlists (`spotdl` + öffentlicher oEmbed-Fallback ohne API-Keys)
  - 10 vorinstallierte Live-Radiosender + weltweite Suche über die Radio-Browser API
  - **Lautloses Verlassen bei Inaktivität**: Verlässt leere oder inaktive Voice-Kanäle komplett lautlos ohne Chat-Nachrichten
- 🎰 **Economy, Casino & PvP-Spiele** (`cogs/gambling.py`)
  - Atomare Coin-Wirtschaft mit Double-Spend-Schutz & automatischem Cashout/Refund bei Spiel-Timeouts
  - Solo-Casino (12 Spiele) & Multiplayer/PvP (`/connect4`, `/tictactoe`, `/rr`, `/duel`)
- 🎣 **Angeln, 5 Berufe & Twitch-Shop** (`cogs/jobs.py`)
  - Angeln (`/fish`), Holzfällen (`/chop`), Bergbau (`/mine`), Jagd (`/hunt`), Schmieden (`/smith`) & Kochen (`/cook`)
  - Einlösen erspielter Coins gegen echte **Twitch-Kanalpunkte-Belohnungen** (`/shop`, `/redeem`)
- 🎙️ **Temp-Voice & Ticket-System** (`cogs/temp_voice.py`, `cogs/tickets.py`)
  - Automatische temporäre Sprachkanäle mit nativen `UserSelect`-Menüs & 10-Minuten-Owner-Recovery
  - Persistentes Support-Ticket-System mit Claim-Funktion & automatischem UTF-8-Transkript-Versand

---

## 🗂️ Projektstruktur

```text
Modcheck/
├── main.py                  # Schlanker Startpunkt (Discord + Twitch)
├── bot_core/                # Zentrale Dienste & Datenschicht
│   ├── config.py            # .env-Konfiguration, Konstanten & Europe/Berlin-Helfer
│   ├── storage.py           # Thread-sichere, atomare JSON-Persistenz + gedrosseltes Speichern
│   ├── stats.py             # Statistik-Tracker für Discord-Nachrichten, Voice & Casino
│   ├── checks.py            # Slash-Command-Berechtigungs- & Kanal-Checks
│   ├── activity_helpers.py  # Aktivitäts-Scores, Kalender, 24h-Profile & Berichte
│   └── twitch_service.py    # Twitch IRC Client, Stream-Metadaten & Helix API Service
├── cogs/                    # Modulare Discord.py Cogs (74 Slash-Commands)
│   ├── general.py           # /help, /status, /profile, /userinfo, /remind & Logs
│   ├── activity.py          # Twitch-Tracking, /activity, /teamstatus, /streams, /absent, Reviews
│   ├── tickets.py           # Persistentes Ticket-System & Transkripte
│   ├── temp_voice.py        # Dynamische Sprachkanäle & Control-Panel
│   ├── music.py             # YouTube, Spotify & Radio Player (lautloser Idle-Disconnect)
│   ├── gambling.py          # Economy, Solo-Casino & PvP-Minispiele
│   └── jobs.py              # Angeln, 5 Berufe & Twitch-Belohnungs-Shop
├── modcheck-bot.service     # Systemd-Unit für Raspberry Pi 5 Autostart
├── requirements.txt         # Schlanke Laufzeitabhängigkeiten (nur 8 Pakete)
└── .env.example             # Vorlage aller Umgebungsvariablen
```

---

## 🛠️ Installation & Update (Raspberry Pi 5 / Linux)

```bash
git pull
./venv/bin/pip install -r requirements.txt
sudo systemctl restart modcheck-bot.service
```
