"""
database.py – SQLite-Datenbankschicht für das Verifikationssystem
"""

import sqlite3
import os
import hashlib
import hmac
from datetime import datetime

DB_PATH = os.getenv("VERIFIER_DB_PATH", "verifier.db")
IP_SALT  = os.getenv("IP_SALT", "change_me_to_a_random_string_in_env")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Erstellt alle Tabellen falls sie noch nicht existieren."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS verified_users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id      TEXT NOT NULL,
                discord_name    TEXT,
                ip_hash         TEXT NOT NULL,
                country         TEXT,
                country_code    TEXT,
                city            TEXT,
                isp             TEXT,
                is_vpn          INTEGER DEFAULT 0,
                is_proxy        INTEGER DEFAULT 0,
                is_banned       INTEGER DEFAULT 0,
                ban_reason      TEXT,
                banned_by       TEXT,
                banned_at       TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen       TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(discord_id, ip_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_discord_id ON verified_users(discord_id);
            CREATE INDEX IF NOT EXISTS idx_ip_hash    ON verified_users(ip_hash);
            CREATE INDEX IF NOT EXISTS idx_is_banned  ON verified_users(is_banned);

            CREATE TABLE IF NOT EXISTS ban_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id  TEXT NOT NULL,
                action      TEXT NOT NULL,
                reason      TEXT,
                admin_id    TEXT,
                admin_name  TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                session_id  TEXT PRIMARY KEY,
                discord_id  TEXT NOT NULL,
                discord_name TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at  TEXT NOT NULL
            );
        """)
    print("✅ [DB] Datenbank initialisiert")


def hash_ip(ip: str) -> str:
    """
    Erstellt einen HMAC-SHA256-Hash der IP-Adresse mit dem Salt.
    Irreversibel – man kann die echte IP nicht wiederherstellen.
    """
    return hmac.new(
        IP_SALT.encode(),
        ip.encode(),
        hashlib.sha256,
    ).hexdigest()


# ── Lookup-Funktionen ────────────────────────────────────────────

def get_user_by_discord_id(discord_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM verified_users WHERE discord_id = ? ORDER BY created_at DESC LIMIT 1",
            (discord_id,),
        ).fetchone()


def get_all_entries_for_discord_id(discord_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM verified_users WHERE discord_id = ? ORDER BY created_at DESC",
            (discord_id,),
        ).fetchall()


def get_banned_entry_by_ip_hash(ip_hash: str) -> sqlite3.Row | None:
    """Prüft ob diese IP jemals mit einem gebannten Account verknüpft war."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM verified_users WHERE ip_hash = ? AND is_banned = 1 LIMIT 1",
            (ip_hash,),
        ).fetchone()


def get_all_ip_hashes_for_discord_id(discord_id: str) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ip_hash FROM verified_users WHERE discord_id = ?",
            (discord_id,),
        ).fetchall()
        return [r["ip_hash"] for r in rows]


def get_all_discord_ids_for_ip_hash(ip_hash: str) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT discord_id FROM verified_users WHERE ip_hash = ?",
            (ip_hash,),
        ).fetchall()
        return [r["discord_id"] for r in rows]


# ── Schreib-Funktionen ───────────────────────────────────────────

def upsert_user(
    discord_id: str,
    discord_name: str,
    ip_hash: str,
    country: str = "",
    country_code: str = "",
    city: str = "",
    isp: str = "",
    is_vpn: bool = False,
    is_proxy: bool = False,
    is_banned: bool = False,
    ban_reason: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM verified_users WHERE discord_id = ? AND ip_hash = ?",
            (discord_id, ip_hash),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE verified_users
                   SET discord_name=?, last_seen=?, is_vpn=?, is_proxy=?,
                       country=?, country_code=?, city=?, isp=?
                   WHERE discord_id=? AND ip_hash=?""",
                (discord_name, now, int(is_vpn), int(is_proxy),
                 country, country_code, city, isp,
                 discord_id, ip_hash),
            )
        else:
            conn.execute(
                """INSERT INTO verified_users
                   (discord_id, discord_name, ip_hash, country, country_code,
                    city, isp, is_vpn, is_proxy, is_banned, ban_reason, created_at, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (discord_id, discord_name, ip_hash, country, country_code,
                 city, isp, int(is_vpn), int(is_proxy),
                 int(is_banned), ban_reason, now, now),
            )


def ban_discord_id(
    discord_id: str,
    reason: str = "",
    admin_id: str = "",
    admin_name: str = "",
) -> int:
    """
    Bannt eine Discord-ID + alle IP-Hashes die jemals mit ihr verknüpft waren.
    Gibt die Anzahl der zusätzlich gebannten Accounts zurück.
    """
    now = datetime.utcnow().isoformat()
    extra_banned = 0

    with get_conn() as conn:
        # 1) Alle IP-Hashes dieser Discord-ID holen
        ip_hashes = [
            r["ip_hash"] for r in conn.execute(
                "SELECT DISTINCT ip_hash FROM verified_users WHERE discord_id = ?",
                (discord_id,),
            ).fetchall()
        ]

        # 2) Direkt die Discord-ID bannen
        conn.execute(
            """UPDATE verified_users
               SET is_banned=1, ban_reason=?, banned_by=?, banned_at=?
               WHERE discord_id=?""",
            (reason, admin_id, now, discord_id),
        )

        # 3) Alle anderen Discord-IDs mit denselben IP-Hashes ebenfalls bannen (Alt-Accounts)
        for ip_hash in ip_hashes:
            linked_ids = conn.execute(
                """SELECT DISTINCT discord_id FROM verified_users
                   WHERE ip_hash=? AND discord_id!=? AND is_banned=0""",
                (ip_hash, discord_id),
            ).fetchall()
            for row in linked_ids:
                conn.execute(
                    """UPDATE verified_users
                       SET is_banned=1,
                           ban_reason=?,
                           banned_by=?, banned_at=?
                       WHERE discord_id=?""",
                    (
                        f"Alt-Account von {discord_id} (IP-Kette) | {reason}",
                        admin_id, now,
                        row["discord_id"],
                    ),
                )
                extra_banned += 1

        # 4) Ban-Log Eintrag
        conn.execute(
            """INSERT INTO ban_log (discord_id, action, reason, admin_id, admin_name, created_at)
               VALUES (?, 'BAN', ?, ?, ?, ?)""",
            (discord_id, reason, admin_id, admin_name, now),
        )

    return extra_banned


def unban_discord_id(
    discord_id: str,
    admin_id: str = "",
    admin_name: str = "",
) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """UPDATE verified_users
               SET is_banned=0, ban_reason=NULL, banned_by=NULL, banned_at=NULL
               WHERE discord_id=?""",
            (discord_id,),
        )
        conn.execute(
            """INSERT INTO ban_log (discord_id, action, reason, admin_id, admin_name, created_at)
               VALUES (?, 'UNBAN', NULL, ?, ?, ?)""",
            (discord_id, admin_id, admin_name, now),
        )


def get_recent_verifications(limit: int = 50) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM verified_users ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_all_banned(limit: int = 200) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM verified_users WHERE is_banned=1 ORDER BY banned_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def search_users(query: str) -> list[sqlite3.Row]:
    q = f"%{query}%"
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM verified_users
               WHERE discord_id LIKE ? OR discord_name LIKE ? OR isp LIKE ?
                     OR country LIKE ? OR country_code LIKE ?
               ORDER BY last_seen DESC LIMIT 100""",
            (q, q, q, q, q),
        ).fetchall()


def get_stats() -> dict:
    with get_conn() as conn:
        total     = conn.execute("SELECT COUNT(DISTINCT discord_id) FROM verified_users").fetchone()[0]
        banned    = conn.execute("SELECT COUNT(DISTINCT discord_id) FROM verified_users WHERE is_banned=1").fetchone()[0]
        vpn_users = conn.execute("SELECT COUNT(DISTINCT discord_id) FROM verified_users WHERE is_vpn=1").fetchone()[0]
        today     = conn.execute(
            "SELECT COUNT(DISTINCT discord_id) FROM verified_users WHERE DATE(last_seen)=DATE('now')"
        ).fetchone()[0]
    return {
        "total": total,
        "banned": banned,
        "vpn_users": vpn_users,
        "today": today,
    }
