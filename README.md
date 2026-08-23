# Modcheck – Discord + Twitch Mod-Bot

Bot mit Ticket-System, Musik-Player (YouTube/Spotify), AI-Antworten,
Gambling-/Mini-Games, Temp-Voice-Kanälen und Web-Dashboard.
Optimiert für den **Raspberry Pi 5**.

---

## 🍓 Raspberry Pi 5 – Einrichtung (Schritt für Schritt)

### 1. System-Pakete installieren

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-dev git ffmpeg
```

> ⚠️ **`ffmpeg` ist zwingend** für die Musik (und TTS) nötig.
> Ohne ffmpeg startet der Bot, aber **keine Musik** funktioniert.
> Der Bot zeigt beim Start einen klaren Hinweis, falls FFmpeg fehlt.

### 2. Projekt holen

```bash
cd ~
git clone <dein-repo-url> Modcheck
cd Modcheck
```

### 3. Python-Umgebung

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

> 💡 **yt-dlp regelmäßig aktualisieren**, damit YouTube-Playback
> weiter funktioniert:
> ```bash
> pip install -U yt-dlp
> ```

### 4. Konfiguration

```bash
cp .env.example .env
nano .env        # Token & Kanal-IDs eintragen
```

Wichtigste Werte:
- `DISCORD_TOKEN` – Bot-Token (Developer Portal)
- `GUILD_ID` – deine Server-ID
- `ADMIN_USER_IDS` – deine User-ID(s), kommagetrennt
- `TICKET_CATEGORY_ID`, `TICKET_LOG_CHANNEL_ID`, `TICKET_SUPPORT_ROLE`
- `LOG_CHANNEL_ID`, `TEMP_VOICE_CHANNEL_ID`, … (siehe `.env.example`)

### 5. Testen

```bash
python main.py
```

Im Start-Banner siehst du einen **System-Check** (Python, FFmpeg,
PyNaCl, yt-dlp) und den Status aller Module.

### 6. Als Dienst laufen lassen (autostart + Neustart bei Crash)

```bash
sudo cp modcheck-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now modcheck-bot
```

Nützliche Befehle:

```bash
sudo systemctl status modcheck-bot    # Status
journalctl -u modcheck-bot -f         # Logs live
sudo systemctl restart modcheck-bot   # Neustart
```

> Der Dienst startet den Bot automatisch nach Neustart des Pis und
> startet ihn neu, wenn er abstürzt (`Restart=always`).

---

## 🎫 Ticket-System

- `/ticketpanel` (nur Admins) sendet das Panel mit dem
  **„Ticket erstellen“-Button** in einen Kanal.
- Tickets erhalten die Buttons **Claimen / Schließen / Transcript**.
- Offene Tickets überleben einen Bot-Neustart (Buttons funktionieren
  danach weiterhin).
- Logs gehen an `TICKET_LOG_CHANNEL_ID` (oder `LOG_CHANNEL_ID`).

## 🎵 Musik

- `/play <Titel | YouTube-URL | Spotify-URL | Playlist>`
- `/search`, `/skip`, `/stop`, `/queue`, `/shuffle`, `/autoplay`, `/nowplaying`
- Der Bot verlässt den Voice-Kanal nach `MUSIC_IDLE_TIMEOUT` Sekunden
  Inaktivität oder wenn der Kanal leer ist.

**Wenn die Musik nicht läuft, prüfe in dieser Reihenfolge:**

1. `ffmpeg` installiert? → `which ffmpeg` (sonst: `sudo apt install -y ffmpeg`)
2. Bot hat **Verbinden + Sprechen** in dem Voice-Kanal?
3. `pip install -U yt-dlp` (YouTube ändert oft die API)
4. Start-Log: `journalctl -u modcheck-bot` nach `[Music]`-Einträgen durchsuchen.

## 🤖 AI-Antworten

Bot pingen → KI-Antwort (OpenRouter). Ohne Key wird das AI-Feature
sauber deaktiviert. Bei API-Fehlern bekommt der User eine **stille DM** –
es werden keine Fehler-Nachrichten mehr in den Chat gepostet.

## 🌐 Dashboard

Läuft im selben Prozess wie der Bot:

```
http://<pi-ip>:5000
```

---

## 📁 Struktur

```
main.py                  # Der komplette Bot (Discord + Twitch + Dashboard)
dashboard/               # Templates & Assets des Dashboards
verifier/                # Optionales Verifier-Web (separat)
requirements.txt         # Python-Abhängigkeiten
modcheck-bot.service     # systemd-Unit für den Raspberry Pi
```
