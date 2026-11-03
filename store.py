"""SQLite storage for one demo household: event brief, quotes, calls, bookings, check-ins, alerts.

Vendors are read-only synthetic data in data/vendors.json (the contest allows synthetic data only).
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("BANDOBAST_DB", ROOT / "bandobast.db"))
VENDORS_PATH = ROOT / "data" / "vendors.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    brief TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    event_id INTEGER,
    vendor_id TEXT,
    mode TEXT NOT NULL,
    channel TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_secs REAL,
    end_reason TEXT,
    latencies TEXT,
    transcript TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    vendor_id TEXT NOT NULL,
    call_id TEXT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    event_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    role TEXT NOT NULL,          -- primary | backup
    vendor_id TEXT NOT NULL,
    status TEXT NOT NULL,        -- active | replaced | standby | activated
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, category, role)
);
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    vendor_id TEXT NOT NULL,
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL,        -- pending | confirmed | delayed | cancelled | unsure | unreachable
    eta TEXT,
    notes TEXT,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS enquiries (
    event_id INTEGER NOT NULL,
    vendor_id TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,        -- shortlisted | ringing | in_call | quoted | declined | no_answer | failed
                                 -- | approved | rejected | renegotiating
    channel TEXT,                -- app | phone | none
    call_id TEXT,
    note TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_id, vendor_id)
);
CREATE TABLE IF NOT EXISTS devices (
    vendor_id TEXT PRIMARY KEY,  -- the vendor this phone signed in as
    push_token TEXT,             -- Firebase Cloud Messaging token, if any
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovered (
    event_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    data TEXT NOT NULL           -- [{name, source, lat, lon, distance_km, url}]
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,         -- info | warn | critical
    message TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def reset() -> None:
    DB_PATH.unlink(missing_ok=True)


# Vendors


def vendors() -> list[dict[str, Any]]:
    return json.loads(VENDORS_PATH.read_text())


def vendor(vendor_id: str) -> dict[str, Any]:
    for v in vendors():
        if v["id"] == vendor_id:
            return v
    raise KeyError(f"Unknown vendor {vendor_id}")


# Event (one active event: the latest)


def save_event(brief: dict[str, Any]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO events (created_at, brief) VALUES (?, ?)", (now(), json.dumps(brief))
        )
        return cur.lastrowid


def current_event() -> tuple[int, dict[str, Any]] | None:
    with connect() as conn:
        row = conn.execute("SELECT id, brief FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return (row["id"], json.loads(row["brief"])) if row else None


# Calls


def save_call(record: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO calls
               (id, event_id, vendor_id, mode, channel, started_at, duration_secs, end_reason, latencies, transcript)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["id"],
                record.get("event_id"),
                record.get("vendor_id"),
                record["mode"],
                record["channel"],
                record["started_at"],
                record.get("duration_secs"),
                record.get("end_reason"),
                json.dumps(record.get("latencies", [])),
                json.dumps(record.get("transcript", []), ensure_ascii=False),
            ),
        )


def calls(event_id: int | None) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM calls WHERE event_id IS ? OR ? IS NULL ORDER BY started_at DESC",
            (event_id, event_id),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["latencies"] = json.loads(d["latencies"] or "[]")
        d["transcript"] = json.loads(d["transcript"] or "[]")
        out.append(d)
    return out


# Quotes (latest per vendor wins)


def save_quote(event_id: int, vendor_id: str, call_id: str | None, data: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO quotes (event_id, vendor_id, call_id, created_at, data) VALUES (?, ?, ?, ?, ?)",
            (event_id, vendor_id, call_id, now(), json.dumps(data, ensure_ascii=False)),
        )


def latest_quotes(event_id: int) -> dict[str, dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT vendor_id, data FROM quotes WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()
    return {r["vendor_id"]: json.loads(r["data"]) for r in rows}


def quote_from_call(call_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT data FROM quotes WHERE call_id = ? ORDER BY id DESC LIMIT 1", (call_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def call(call_id: str) -> dict[str, Any] | None:
    return next((c for c in calls(None) if c["id"] == call_id), None)


# Bookings


def set_booking(event_id: int, category: str, role: str, vendor_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO bookings (event_id, category, role, vendor_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, category, role, vendor_id, status, now()),
        )


def bookings(event_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE event_id = ? ORDER BY category, role DESC", (event_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# Check-ins


def add_checkins(event_id: int, vendor_id: str, category: str, schedule: list[tuple[str, str]]) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM checkins WHERE event_id = ? AND category = ? AND status = 'pending'",
            (event_id, category),
        )
        conn.executemany(
            """INSERT INTO checkins (event_id, vendor_id, category, label, due_at, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            [(event_id, vendor_id, category, label, due_at) for label, due_at in schedule],
        )


def checkin(checkin_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM checkins WHERE id = ?", (checkin_id,)).fetchone()
    if not row:
        raise KeyError(f"Unknown check-in {checkin_id}")
    return dict(row)


def checkins(event_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM checkins WHERE event_id = ? ORDER BY due_at, category", (event_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def finish_checkin(checkin_id: int, status: str, eta: str | None, notes: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE checkins SET status = ?, eta = ?, notes = ?, done_at = ? WHERE id = ?",
            (status, eta, notes, now(), checkin_id),
        )


# Alerts (shown on the dashboard; later sent to the family on WhatsApp)


def add_alert(event_id: int, level: str, message: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO alerts (event_id, created_at, level, message) VALUES (?, ?, ?, ?)",
            (event_id, now(), level, message),
        )


def alerts(event_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE event_id = ? ORDER BY id DESC", (event_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# Enquiries: where each shortlisted vendor stands for the current event


def set_enquiry(event_id: int, vendor_id: str, category: str, status: str, **fields: Any) -> None:
    with connect() as conn:
        old = conn.execute(
            "SELECT channel, call_id, note FROM enquiries WHERE event_id = ? AND vendor_id = ?", (event_id, vendor_id)
        ).fetchone()
        merged = {**(dict(old) if old else {}), **fields}
        conn.execute(
            """INSERT OR REPLACE INTO enquiries (event_id, vendor_id, category, status, channel, call_id, note, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, vendor_id, category, status, merged.get("channel"), merged.get("call_id"), merged.get("note"), now()),
        )


def enquiries(event_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM enquiries WHERE event_id = ? ORDER BY updated_at", (event_id,)).fetchall()
    return [dict(r) for r in rows]


def enquiry(event_id: int, vendor_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM enquiries WHERE event_id = ? AND vendor_id = ?", (event_id, vendor_id)
        ).fetchone()
    return dict(row) if row else None


# Vendor phones running the app


def save_device(vendor_id: str, push_token: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO devices (vendor_id, push_token, updated_at) VALUES (?, ?, ?)",
            (vendor_id, push_token, now()),
        )


def device(vendor_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM devices WHERE vendor_id = ?", (vendor_id,)).fetchone()
    return dict(row) if row else None


# Businesses found near the venue (shown, never contacted: the demo uses synthetic vendors only)


def save_discovered(event_id: int, category: str, places: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM discovered WHERE event_id = ? AND category = ?", (event_id, category))
        conn.execute(
            "INSERT INTO discovered (event_id, category, data) VALUES (?, ?, ?)",
            (event_id, category, json.dumps(places, ensure_ascii=False)),
        )


def discovered(event_id: int) -> dict[str, list[dict[str, Any]]]:
    with connect() as conn:
        rows = conn.execute("SELECT category, data FROM discovered WHERE event_id = ?", (event_id,)).fetchall()
    return {r["category"]: json.loads(r["data"]) for r in rows}
