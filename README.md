# 🚀 Modcheck v3.0 — Discord & Twitch Bot

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

## 🧩 All-in-One-Module einrichten

Nach dem Start stehen `/feature-status` (Admin-Setup), `/help` und `/status` zur Verfügung. Im Setup-Center können viele Kanal- und Rollenzuordnungen pro Server ohne Neustart geändert werden; Spezialoptionen liegen in den jeweiligen Befehlsgruppen:

**Setup-Center bedienen:** Bereich wählen → Funktion wählen → Einstellung auswählen → Kanal oder Rolle auswählen. Bei Funktionen mit nur einer Einstellung erscheint die Auswahl direkt. Der aktuelle Wert und ggf. ein An-/Aus-Schalter stehen auf der Funktionsseite. Eine Kanalwahl allein aktiviert die Funktion nicht automatisch. Über „Auswahl zurücksetzen“ wird die Auswahl entfernt bzw. der angezeigte Fallback verwendet; Auto-Rollen können dort hinzugefügt oder alle zusammen entfernt werden. Weitere Optionen und Funktionen ohne feste Kanal-/Rollenbindung sind mit den passenden Slash-Befehlen aufgeführt. Twitch-/YouTube-Alerts werden weiterhin botweit gespeichert und sind entsprechend gekennzeichnet.


- `/birthday`: Geburtstage und Tagesrolle; der 29. Februar wird in Nicht-Schaltjahren am 28. Februar gefeiert.
- `/counting`: Zählkanal, Wechsel der Teilnehmer und Highscore.
- `/rank` und `/levels`: Chat-/Voice-XP, Levelrollen, Coin-Boni und MEE6-Import. Das Leaderboard muss für den direkten MEE6-Abruf erreichbar sein. Alternativ `python tools/export_mee6.py SERVER_ID` ausführen und die JSON-Datei mit `/levels import-mee6` hochladen. Der tatsächliche Zugriff hängt von MEE6 und dessen API ab.
- `/roles` und `/welcome`: Rollenpanels, Reaction-/Auto-/Sticky-Rollen, Begrüßungen, Boosts und Statistikkanäle.
- `/mod`, `/automod` und `/logs`: Moderationsfälle, konfigurierbare Filter und Audit-Kanäle. Automatische Warn-Eskalationen müssen ausdrücklich eingerichtet werden.
- `/giveaway`, `/poll`, `/suggest`, `/starboard`, `/customcmd` und `/afk`: Community-Funktionen.

### Statistik-Kanäle direkt im Menü

`/feature-status` → **Server & Rollen** → **Statistik-Zähler** → **Zähler erstellen**: Typ auswählen, optional eine Kategorie und einen eigenen Namen mit `{count}` festlegen, dann **Kanal erstellen**. Der Bot legt einen nicht beitretbaren Sprachkanal an. Zur Auswahl stehen unter anderem Discord-Mitglieder (mit oder ohne Bots) und Twitch-Follower. Unter **Zähler verwalten** können angelegte Zähler nach Bestätigung wieder gelöscht werden. Pro Typ wird ein vorhandener Zähler nicht doppelt erstellt. Alternativ bieten `/welcome counter-create` und `/welcome counter-remove` dieselben Funktionen.

Die Aktualisierung erfolgt ungefähr alle 10 Minuten; der letzte Wert bleibt bei einem Twitch-Fehler erhalten. Für Twitch wird der konfigurierte `STREAMER_CHANNEL` oder `STREAMER_USER_ID` sowie `TWITCH_CLIENT_ID` und ein gültiges App- oder Benutzer-Token benötigt (z. B. App-Token über `TWITCH_CLIENT_SECRET`). Twitch muss den Zugriff erlauben. Falls der erste Abruf fehlschlägt, wird kein Kanal erstellt und das Menü zeigt einen Hinweis. Der Bot benötigt außerdem **Kanäle verwalten** in der gewählten Kategorie.

Im Discord Developer Portal **Server Members Intent** und **Message Content Intent** aktivieren; für Online-Zähler zusätzlich **Presence Intent**. Der Bot benötigt die jeweils passenden Kanal-, Nachrichten-, Rollen- und Moderationsrechte, und seine höchste Rolle muss über Rollen bzw. Mitgliedern stehen, die er verwalten soll. Persistente Rollen-, Giveaway- und Umfrage-Buttons funktionieren nach Neustarts weiter.

Die neuen Daten liegen in `server_config.json`, `birthdays.json`, `counting.json`, `levels.json`, `roles_config.json`, `moderation.json` und `community.json`. Diese Dateien sind aus Git ausgeschlossen: **regelmäßig zusammen mit den bestehenden Bot-Daten sichern**, insbesondere vor Updates oder Migrationen. Discord-Events und MEE6-Import sollten auf einem Testserver überprüft werden; ein Offline-Test kann keine echten API-Rechte oder Netzwerkzugriffe prüfen.

Tests lokal ausführen: `python -m unittest discover -s tests -v` (nach Installation der Abhängigkeiten).

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
├── cogs/                    # Modulare Discord.py Cogs (98 Top-Level Slash-Commands)
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
