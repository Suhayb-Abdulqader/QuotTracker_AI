"""SQLite persistence layer for QuotTracker."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DB_SETTING = os.getenv("DB_URL", "").strip()
if DB_SETTING == ":memory:":
    DB_FILE = DB_SETTING
else:
    db_path = Path(DB_SETTING) if DB_SETTING else Path("quottracker.db")
    DB_FILE = str((db_path if db_path.is_absolute() else BASE_DIR / db_path).resolve())


def init_db():
    """Create tables if they don't exist. Safe to call many times."""
    conn = get_db()
    if DB_FILE != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
        request_no        TEXT PRIMARY KEY,
        door_name         TEXT,
        quotation_no      TEXT,
        vendor_po         TEXT,
        brand             TEXT,
        request_date      TEXT,
        installation_date TEXT,
        source            TEXT,
        created_at        TEXT,
        updated_at        TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tracker (
        request_no        TEXT PRIMARY KEY,
        door_name         TEXT,
        quotation_no      TEXT,
        vendor_po         TEXT,
        brand             TEXT,
        request_date      TEXT,
        installation_date TEXT,
        amount            REAL,
        status            TEXT,
        sharepoint_folder TEXT,
        synced_at         TEXT,
        created_at        TEXT,
        updated_at        TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        request_no    TEXT,
        action        TEXT,
        table_name    TEXT,
        old_values    TEXT,
        new_values    TEXT,
        source        TEXT,
        confirmed_by  TEXT,
        created_at    TEXT)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_created_at "
        "ON audit_log(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_request_created "
        "ON audit_log(request_no, created_at)"
    )
    conn.commit()
    conn.close()


def get_db():
    """Return a plain connection. Caller must commit() and close()."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ── requests ──────────────────────────────────────────────────────

def upsert_request(conn, row: dict, source: str = "sheet"):
    now = _now()
    conn.execute("""
        INSERT INTO requests
            (request_no, door_name, quotation_no, vendor_po, brand,
             request_date, installation_date, source, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(request_no) DO UPDATE SET
            door_name=excluded.door_name,
            quotation_no=excluded.quotation_no,
            vendor_po=excluded.vendor_po,
            brand=excluded.brand,
            request_date=excluded.request_date,
            installation_date=excluded.installation_date,
            source=excluded.source,
            updated_at=excluded.updated_at
    """, (row.get("request_no"), row.get("door_name"), row.get("quotation_no"),
          row.get("vendor_po"), row.get("brand"), row.get("request_date"),
          row.get("installation_date"), source, now, now))


def get_all_requests(conn) -> list[dict]:
    return [dict(r) for r in
            conn.execute("SELECT * FROM requests ORDER BY request_no").fetchall()]


# ── tracker ───────────────────────────────────────────────────────

def insert_tracker_row(conn, row: dict, source: str = "sync"):
    now = _now()
    rno = row.get("Request No") or row.get("request_no")
    cursor = conn.execute("""
        INSERT OR IGNORE INTO tracker
            (request_no, door_name, quotation_no, vendor_po, brand,
             request_date, installation_date, amount, status,
             sharepoint_folder, synced_at, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (rno,
          row.get("Door Name")         or row.get("door_name"),
          row.get("Quotation No")      or row.get("quotation_no"),
          row.get("Vendor PO")         or row.get("vendor_po"),
          row.get("Brand")             or row.get("brand"),
          row.get("Request Date")      or row.get("request_date"),
          row.get("Installation Date") or row.get("installation_date"),
          row.get("Amount") if row.get("Amount") is not None else row.get("amount"),
          row.get("Status")            or row.get("status") or "Pending",
          row.get("SharePoint Folder") or row.get("sharepoint_folder"),
          now, now, now))
    if cursor.rowcount:
        _log(conn, rno, "INSERT", "tracker", None, dict(row), source)
    return cursor.rowcount > 0


def update_tracker_row(conn, request_no: str, updates: dict, source: str = "sync"):
    if not updates:
        return
    col_map = {
        "Door Name": "door_name", "Quotation No": "quotation_no",
        "Vendor PO": "vendor_po", "Brand": "brand",
        "Request Date": "request_date", "Installation Date": "installation_date",
        "Amount": "amount", "Status": "status",
        "SharePoint Folder": "sharepoint_folder",
    }
    db_updates = {col_map.get(k, k): v for k, v in updates.items()
                  if col_map.get(k, k) in set(col_map.values())}
    if not db_updates:
        return
    previous = get_tracker_row(conn, request_no)
    db_updates["updated_at"] = _now()
    cols = ", ".join(f"{c}=?" for c in db_updates)
    conn.execute(f"UPDATE tracker SET {cols} WHERE request_no=?",
                 [*db_updates.values(), request_no])
    _log(conn, request_no, "UPDATE", "tracker", previous, updates, source)


def get_all_tracker(conn) -> list[dict]:
    return [dict(r) for r in
            conn.execute("SELECT * FROM tracker ORDER BY updated_at DESC").fetchall()]


def get_tracker_row(conn, request_no: str) -> dict | None:
    r = conn.execute("SELECT * FROM tracker WHERE request_no=?",
                     (request_no,)).fetchone()
    return dict(r) if r else None


def get_row_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]


# ── audit log ─────────────────────────────────────────────────────

def _log(conn, request_no, action, table_name, old, new, source):
    conn.execute("""
        INSERT INTO audit_log
            (request_no, action, table_name, old_values, new_values, source, confirmed_by, created_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (request_no, action, table_name,
          json.dumps(old, default=str) if old else None,
          json.dumps(new, default=str) if new else None,
          source, "user", _now()))


def get_audit_log(conn, request_no: str = None, limit: int = 100) -> list[dict]:
    if request_no:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE request_no=? ORDER BY created_at DESC LIMIT ?",
            (request_no, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_audit_actions_since(conn, since: str, actions: tuple[str, ...]) -> int:
    placeholders = ",".join("?" for _ in actions)
    row = conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE created_at >= ? "
        f"AND action IN ({placeholders})",
        (since, *actions),
    ).fetchone()
    return int(row[0])


def get_activity_counts(conn, day_start: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count "
        "FROM audit_log WHERE created_at >= ? GROUP BY substr(created_at, 1, 10)",
        (day_start,),
    ).fetchall()
    return {row["day"]: int(row["count"]) for row in rows}
