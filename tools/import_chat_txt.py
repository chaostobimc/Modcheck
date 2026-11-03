#!/usr/bin/env python3
"""Importiert exportierte Twitch-Chat-.txt-Dateien direkt in Modcheck (data.json).

Komplett ohne Drittanbieter-Abhängigkeiten lauffähig (reines Standard-Python).

Verwendung:
    python3 tools/import_chat_txt.py chat_export/*.txt
    python3 tools/import_chat_txt.py datei.txt --discord-id 829299014083936276 --twitch-name chaostobi_ --display-name Chaos
    python3 tools/import_chat_txt.py --reset ...
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_FILE = os.path.join(ROOT_DIR, "data.json")

LINE_RE = re.compile(
    r"^\[(?P<day>\d{2})\.(?P<mon>\d{2})\.(?P<year>\d{4})\s+"
    r"(?P<hour>\d{2}):(?P<min>\d{2})(?::(?P<sec>\d{2}))?\]\s+"
    r"(?P<user>[^:]+):\s*(?P<text>.*)$"
)
HEADER_USER_RE = re.compile(r"^User:\s*(.+)$", re.IGNORECASE)


def _empty_data() -> dict:
    return {
        "users": {},
        "streams": {},
        "stream_meta": {},
        "message_counts": {},
    }


def _load_json(filepath: str, reset: bool = False) -> dict:
    if reset or not os.path.exists(filepath):
        return _empty_data()
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("users", {})
    data.setdefault("streams", {})
    data.setdefault("stream_meta", {})
    data.setdefault("message_counts", {})
    return data


def _save_json_atomic(filepath: str, data: dict) -> None:
    # Chronologische Sortierung der Datums-Keys für eine saubere data.json
    sorted_data = {
        "users": data.get("users", {}),
        "streams": dict(sorted(data.get("streams", {}).items())),
        "stream_meta": dict(sorted(data.get("stream_meta", {}).items())),
        "message_counts": dict(sorted(data.get("message_counts", {}).items())),
    }
    target_dir = os.path.dirname(os.path.abspath(filepath)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted_data, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def import_single_file(
    filepath: str,
    data: dict,
    forced_uid: str | None = None,
    forced_twitch: str | None = None,
    forced_display: str | None = None,
) -> tuple[int, int]:
    """Liest eine exportierte .txt-Datei ein und aktualisiert data (streams + message_counts)."""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\r\n") for ln in f]

    header_user = ""
    raw_rows: list[tuple[str, int, int, bool, str, str]] = []

    for ln in lines:
        if not header_user:
            hm = HEADER_USER_RE.match(ln.strip())
            if hm:
                header_user = hm.group(1).strip().lower()
                continue

        m = LINE_RE.match(ln)
        if not m:
            continue
        day_iso = f"{m.group('year')}-{m.group('mon')}-{m.group('day')}"
        hh = int(m.group("hour"))
        mm = int(m.group("min"))
        has_sec = m.group("sec") is not None
        row_user = m.group("user").strip()
        row_text = m.group("text").strip()
        try:
            datetime.date.fromisoformat(day_iso)
        except ValueError:
            continue
        if not (0 <= hh < 24 and 0 <= mm < 60):
            continue
        if not header_user:
            header_user = row_user.lower()

        # Reine DOM-Header-Artefakte ("[Datum 00:00] user: user") ignorieren
        if not has_sec and hh == 0 and mm == 0 and row_text.lower() == row_user.lower():
            continue

        raw_rows.append((day_iso, hh, mm, has_sec, f"{hh:02d}:{mm:02d}", row_user))

    # Falls die Datei exakte GQL-Sekunden-Einträge enthält, DOM-Duplikate ohne Sekunden an GQL-Tagen herausfiltern
    gql_days = {r[0] for r in raw_rows if r[3]}
    has_any_gql = bool(gql_days)
    parsed_rows: list[tuple[str, int, int, str]] = []
    for day_iso, hh, mm, has_sec, hm_str, _ in raw_rows:
        if not has_sec and has_any_gql and day_iso in gql_days:
            continue
        parsed_rows.append((day_iso, hh, mm, hm_str))

    if not parsed_rows:
        print(f"⚠️ Keine gültigen Chat-Zeilen in {filepath} gefunden.")
        return 0, 0

    twitch_login = (forced_twitch or header_user).strip().lower()
    matched_uid = str(forced_uid).strip() if forced_uid else None

    if not matched_uid:
        matched_uid = next(
            (
                uid
                for uid, uinfo in data.get("users", {}).items()
                if uinfo.get("twitch_name", "").lower() == twitch_login
            ),
            None,
        )

    if not matched_uid:
        print(
            f"❌ Twitch-User '{twitch_login}' ist noch nicht in data.json per /connect verknüpft!\n"
            f"   Nutze zuerst /connect im Discord oder übergib --discord-id <DISCORD_ID>."
        )
        return 0, 0

    earliest_day = min(r[0] for r in parsed_rows)
    if matched_uid not in data["users"]:
        data["users"][matched_uid] = {
            "twitch_name": twitch_login,
            "display_name": forced_display or twitch_login,
            "connected_at": earliest_day,
        }
    else:
        data["users"][matched_uid]["twitch_name"] = twitch_login
        if forced_display:
            data["users"][matched_uid]["display_name"] = forced_display
        existing_start = data["users"][matched_uid].get("connected_at")
        if not existing_start or earliest_day < existing_start:
            data["users"][matched_uid]["connected_at"] = earliest_day

    by_day: dict[str, list[tuple[int, int, str]]] = {}
    for day_iso, hh, mm, hm_str in parsed_rows:
        by_day.setdefault(day_iso, []).append((hh, mm, hm_str))

    for day_iso, events in sorted(by_day.items()):
        stream_list = data["streams"].setdefault(day_iso, [])
        if matched_uid not in stream_list:
            stream_list.append(matched_uid)

        hours_dict: dict[str, int] = {}
        slots_set: set[int] = set()
        timestamps: list[str] = []

        for hh, mm, hm_str in events:
            hk = str(hh)
            hours_dict[hk] = hours_dict.get(hk, 0) + 1
            slots_set.add(hh * 12 + (mm // 5))
            timestamps.append(hm_str)

        timestamps.sort()
        day_mc = data["message_counts"].setdefault(day_iso, {})
        day_mc[matched_uid] = {
            "count": len(events),
            "hours": hours_dict,
            "slots": sorted(slots_set),
            "first_ts": timestamps[0],
            "last_ts": timestamps[-1],
        }

    print(
        f"✅ {os.path.basename(filepath)} -> User '{twitch_login}' "
        f"(Discord {matched_uid}, Display '{data['users'][matched_uid]['display_name']}'): "
        f"{len(parsed_rows):,} Nachrichten an {len(by_day)} Stream-Tagen importiert!"
    )
    return len(parsed_rows), len(by_day)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importiert exportierte Twitch-Chat-.txt-Dateien in Modcheck (data.json)."
    )
    parser.add_argument("files", nargs="+", help="Pfad(e) zu den exportierten .txt-Dateien")
    parser.add_argument("--data", default=DEFAULT_DATA_FILE, help="Pfad zur data.json")
    parser.add_argument("--reset", action="store_true", help="Alte data.json vor dem Import komplett leeren")
    parser.add_argument("--discord-id", help="Optionale Discord-User-ID")
    parser.add_argument("--twitch-name", help="Optionaler Twitch-Loginname")
    parser.add_argument("--display-name", help="Optionaler Anzeigename im Discord")
    args = parser.parse_args()

    data = _load_json(args.data, reset=args.reset)
    total_msgs = 0
    total_days = 0

    for fp in args.files:
        if not os.path.isfile(fp):
            print(f"⚠️ Datei nicht gefunden: {fp}")
            continue
        m_cnt, d_cnt = import_single_file(
            fp,
            data,
            forced_uid=args.discord_id,
            forced_twitch=args.twitch_name,
            forced_display=args.display_name,
        )
        total_msgs += m_cnt
        total_days += d_cnt

    if total_msgs > 0 or args.reset:
        _save_json_atomic(args.data, data)
        print(
            f"\n🎉 Fertig! Insgesamt {total_msgs:,} Nachrichten ({total_days} User-Stream-Tage) "
            f"in {args.data} gespeichert."
        )


if __name__ == "__main__":
    main()
