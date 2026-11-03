#!/usr/bin/env python3
"""Importiert exportierte Twitch-Chat-.txt-Dateien direkt in Modcheck (data.json).

Komplett ohne Drittanbieter-Abhängigkeiten lauffähig (reines Standard-Python).

Verwendung:
    python3 tools/import_chat_txt.py pfad/zur/twitch_chat_user.txt
    python3 tools/import_chat_txt.py *.txt
    python3 tools/import_chat_txt.py datei.txt --discord-id 123456789012345678 --twitch-name user
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


def _load_json(filepath: str) -> dict:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("users", {})
    data.setdefault("streams", {})
    data.setdefault("stream_meta", {})
    data.setdefault("message_counts", {})
    return data


def _save_json_atomic(filepath: str, data: dict) -> None:
    target_dir = os.path.dirname(os.path.abspath(filepath)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
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
) -> tuple[int, int]:
    """Liest eine exportierte .txt-Datei ein und aktualisiert data (streams + message_counts)."""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\r\n") for ln in f]

    header_user = ""
    parsed_rows: list[tuple[str, int, int, str]] = []

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
        try:
            datetime.date.fromisoformat(day_iso)
        except ValueError:
            continue
        if not (0 <= hh < 24 and 0 <= mm < 60):
            continue
        if not header_user:
            header_user = m.group("user").strip().lower()
        parsed_rows.append((day_iso, hh, mm, f"{hh:02d}:{mm:02d}"))

    if not parsed_rows:
        print(f"⚠️ Keine gültigen Chat-Zeilen in {filepath} gefunden.")
        return 0, 0

    twitch_login = (forced_twitch or header_user).strip().lower()
    matched_uid = forced_uid

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

    if matched_uid not in data["users"]:
        data["users"][matched_uid] = {
            "twitch_name": twitch_login,
            "display_name": twitch_login,
            "connected_at": min(r[0] for r in parsed_rows),
        }
    else:
        existing_start = data["users"][matched_uid].get("connected_at")
        earliest_day = min(r[0] for r in parsed_rows)
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
        f"✅ {os.path.basename(filepath)} -> User '{twitch_login}' (Discord {matched_uid}): "
        f"{len(parsed_rows):,} Nachrichten an {len(by_day)} Stream-Tagen importiert!"
    )
    return len(parsed_rows), len(by_day)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importiert exportierte Twitch-Chat-.txt-Dateien in Modcheck (data.json)."
    )
    parser.add_argument("files", nargs="+", help="Pfad(e) zu den exportierten .txt-Dateien")
    parser.add_argument("--data", default=DEFAULT_DATA_FILE, help="Pfad zur data.json")
    parser.add_argument("--discord-id", help="Optionale Discord-User-ID (falls noch nicht verknüpft)")
    parser.add_argument("--twitch-name", help="Optionaler Twitch-Loginname")
    args = parser.parse_args()

    data = _load_json(args.data)
    total_msgs = 0
    total_days = 0

    for fp in args.files:
        if not os.path.isfile(fp):
            print(f"⚠️ Datei nicht gefunden: {fp}")
            continue
        m_cnt, d_cnt = import_single_file(fp, data, args.discord_id, args.twitch_name)
        total_msgs += m_cnt
        total_days += d_cnt

    if total_msgs > 0:
        _save_json_atomic(args.data, data)
        print(
            f"\n🎉 Fertig! Insgesamt {total_msgs:,} Nachrichten ({total_days} Tage) "
            f"in {args.data} gespeichert."
        )


if __name__ == "__main__":
    main()
