#!/usr/bin/env python3
"""Exportiert ein komplettes MEE6-Leaderboard in eine JSON-Datei für `/levels import-mee6`.

Aufruf:
    python tools/export_mee6.py <DISCORD_SERVER_ID> [ausgabe.json]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def export_mee6_guild(guild_id: str, out_path: str) -> None:
    all_players: list[dict] = []
    role_rewards: list[dict] = []

    for page in range(0, 100):
        url = f"https://mee6.xyz/api/plugins/levels/leaderboard/{guild_id}?limit=1000&page={page}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ModcheckMee6Exporter/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                print(
                    "❌ Zugriff verweigert (HTTP 401/403). Bitte aktiviere im MEE6-Dashboard unter "
                    "'Levels' kurz die Option 'Make my server's leaderboard public'."
                )
                sys.exit(1)
            print(f"❌ HTTP-Fehler {exc.code}: {exc.reason}")
            sys.exit(1)

        if page == 0 and isinstance(data.get("role_rewards"), list):
            role_rewards = data["role_rewards"]

        players = data.get("players") or []
        if not players:
            break
        all_players.extend(players)
        print(f"📄 Seite {page + 1} geladen: +{len(players)} Spieler (Gesamt: {len(all_players)})")
        if len(players) < 1000:
            break
        time.sleep(0.5)

    output = {
        "guild_id": guild_id,
        "exported_at": int(time.time()),
        "total_players": len(all_players),
        "role_rewards": role_rewards,
        "players": all_players,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
    print(f"\n✅ Fertig! {len(all_players)} Spieler nach '{out_path}' exportiert.")
    print("👉 Lade diese Datei in Discord mit '/levels import-mee6 datei:...' hoch!")


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].strip().isdigit():
        print("Verwendung: python tools/export_mee6.py <DISCORD_SERVER_ID> [ausgabe.json]")
        sys.exit(1)
    gid = sys.argv[1].strip()
    target = sys.argv[2] if len(sys.argv) >= 3 else f"mee6_export_{gid}.json"
    export_mee6_guild(gid, target)
