"""SQLite storage for one demo household: event brief, quotes, calls, bookings, check-ins, alerts.

Vendors are read-only synthetic data in data/vendors.json (the contest allows synthetic data only).
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import checkin_policy

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
    done_at TEXT,
    confirmation_id INTEGER,
    dispatch_state TEXT NOT NULL DEFAULT 'scheduled',
    current_attempt_id INTEGER,
    next_attempt_at TEXT,
    expires_at TEXT
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
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    push_token TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendor_updates (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    vendor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_confirmations (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    vendor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    terms TEXT NOT NULL,
    approved_quote TEXT NOT NULL,
    answers TEXT NOT NULL,
    call_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'primary',
    purpose TEXT NOT NULL DEFAULT 'booking',
    parent_confirmation_id INTEGER,
    parent_vendor_id TEXT,
    family_approved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checkin_attempts (
    id INTEGER PRIMARY KEY,
    checkin_id INTEGER NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    call_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
"""


def now() -> str:
    return checkin_policy.now().isoformat(timespec='seconds')


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(booking_confirmations)')}
    for name, definition in {'role': "TEXT NOT NULL DEFAULT 'primary'", 'purpose': "TEXT NOT NULL DEFAULT 'booking'",
            'parent_confirmation_id': 'INTEGER', 'parent_vendor_id': 'TEXT', 'family_approved': 'INTEGER NOT NULL DEFAULT 0'}.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE booking_confirmations ADD COLUMN {name} {definition}')
    checkin_columns = {row['name'] for row in conn.execute('PRAGMA table_info(checkins)')}
    for name, definition in {'confirmation_id': 'INTEGER', 'dispatch_state': "TEXT NOT NULL DEFAULT 'scheduled'",
            'current_attempt_id': 'INTEGER', 'next_attempt_at': 'TEXT', 'expires_at': 'TEXT'}.items():
        if name not in checkin_columns:
            conn.execute(f'ALTER TABLE checkins ADD COLUMN {name} {definition}')
    conn.execute("UPDATE checkins SET dispatch_state = 'completed' WHERE status != 'pending' AND dispatch_state = 'scheduled'")
    conn.commit()
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


def event(event_id: int) -> tuple[int, dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT id, brief FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise LookupError("No such function")
    return row["id"], json.loads(row["brief"])


def save_participant(participant_id: str, push_token: str | None) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO participants VALUES (?, ?, ?)", (participant_id, push_token, now()))


def participant(participant_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone()
    return dict(row) if row else None


def save_update(event_id: int, vendor_id: str, kind: str, data: dict[str, Any], status: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO vendor_updates (event_id, vendor_id, created_at, kind, data, status) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, vendor_id, now(), kind, json.dumps(data, ensure_ascii=False), status),
        )
    return cur.lastrowid


def updates(event_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM vendor_updates WHERE event_id = ? ORDER BY id DESC", (event_id,)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"])} for r in rows]


def update(update_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM vendor_updates WHERE id = ?", (update_id,)).fetchone()
    if not row:
        raise LookupError("No such vendor update")
    return {**dict(row), "data": json.loads(row["data"])}


def resolve_update(update_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE vendor_updates SET status = ? WHERE id = ?", (status, update_id))


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


def _confirmation(row) -> dict[str, Any] | None:
    if not row:
        return None
    result = {**dict(row), **{key: json.loads(row[key]) for key in ('terms', 'approved_quote', 'answers')}}
    result['terms'].setdefault('purpose', result['purpose'])  # old primary approvals remain valid after migration
    return result


def confirmation(confirmation_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM booking_confirmations WHERE id = ?", (confirmation_id,)).fetchone()
    if not row:
        raise LookupError("No such booking confirmation")
    return _confirmation(row)


def latest_confirmation(event_id: int, category: str, role: str = 'primary') -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM booking_confirmations WHERE event_id = ? AND category = ? AND role = ? ORDER BY id DESC LIMIT 1", (event_id, category, role)).fetchone()
    return _confirmation(row)


def confirmations(event_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM booking_confirmations WHERE event_id = ? ORDER BY id DESC", (event_id,)).fetchall()
    return [_confirmation(row) for row in rows]


def add_confirmation(event_id: int, category: str, vendor_id: str, terms: dict, quote: dict, *, role: str = 'primary',
                     purpose: str = 'booking', parent_confirmation_id: int | None = None, parent_vendor_id: str | None = None,
                     family_approved: bool = False) -> int:
    with connect() as conn:
        conn.execute("UPDATE booking_confirmations SET status = 'superseded', updated_at = ? WHERE event_id = ? AND category = ? AND role = ? AND status IN ('pending', 'calling', 'changed')", (now(), event_id, category, role))
        cur = conn.execute("""INSERT INTO booking_confirmations
            (event_id, category, vendor_id, status, terms, approved_quote, answers, created_at, updated_at, role, purpose, parent_confirmation_id, parent_vendor_id, family_approved)
            VALUES (?, ?, ?, 'pending', ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, category, vendor_id, json.dumps(terms, ensure_ascii=False), json.dumps(quote, ensure_ascii=False), now(), now(),
             role, purpose, parent_confirmation_id, parent_vendor_id, int(family_approved)))
        return cur.lastrowid


def set_confirmation(confirmation_id: int, status: str, *, call_id: str | None = None, reason: str | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE booking_confirmations SET status = ?, call_id = COALESCE(?, call_id), reason = ?, updated_at = ? WHERE id = ?",
                     (status, call_id, reason, now(), confirmation_id))


def claim_confirmation(confirmation_id: int) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE booking_confirmations SET status = 'calling', updated_at = ? WHERE id = ? AND status = 'pending'", (now(), confirmation_id)).rowcount == 1


def add_confirmation_answer(confirmation_id: int, call_id: str, index: int, answer: dict) -> bool:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT answers FROM booking_confirmations WHERE id = ? AND call_id = ? AND status = 'calling'", (confirmation_id, call_id)).fetchone()
        if not row:
            return False
        answers = json.loads(row["answers"])
        if len(answers) != index:
            return False
        answers.append(answer)
        conn.execute("UPDATE booking_confirmations SET answers = ?, updated_at = ? WHERE id = ?", (json.dumps(answers, ensure_ascii=False), now(), confirmation_id))
        return True


def complete_confirmation(confirmation_id: int, call_id: str, answer_count: int, schedule: list[tuple[str, str]], message: str) -> bool:
    """Confirmation, booking activation and check-ins commit together, including across a crash."""
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT * FROM booking_confirmations WHERE id = ? AND call_id = ? AND status = 'calling'", (confirmation_id, call_id)).fetchone()
        if not row or len(json.loads(row['answers'])) != answer_count:
            return False
        latest = conn.execute('SELECT MAX(id) AS id FROM booking_confirmations WHERE event_id = ? AND category = ? AND role = ?', (row['event_id'], row['category'], row['role'])).fetchone()
        primary = conn.execute("SELECT vendor_id, status FROM bookings WHERE event_id = ? AND category = ? AND role = 'primary'", (row['event_id'], row['category'])).fetchone()
        if latest['id'] != confirmation_id or not primary:
            return False
        if row['role'] == 'primary':
            if primary['vendor_id'] != row['vendor_id'] or primary['status'] != 'confirming':
                return False
        else:
            backup = conn.execute("SELECT vendor_id, status FROM bookings WHERE event_id = ? AND category = ? AND role = 'backup'", (row['event_id'], row['category'])).fetchone()
            parent = conn.execute("SELECT id, status FROM booking_confirmations WHERE event_id = ? AND category = ? AND role = 'primary' ORDER BY id DESC LIMIT 1", (row['event_id'], row['category'])).fetchone()
            if not backup or backup['vendor_id'] != row['vendor_id'] or primary['vendor_id'] != row['parent_vendor_id'] or not parent or parent['id'] != row['parent_confirmation_id']:
                return False
            if row['purpose'] == 'standby':
                if backup['status'] != 'confirming_backup' or primary['status'] != 'active' or parent['status'] != 'confirmed':
                    return False
            elif row['purpose'] == 'replacement':
                if backup['status'] != 'activating_backup' or primary['status'] != 'replacement_pending' or not row['family_approved']:
                    return False
            else:
                return False
        conn.execute("UPDATE booking_confirmations SET status = 'confirmed', updated_at = ? WHERE id = ?", (now(), confirmation_id))
        if row['purpose'] == 'standby':
            conn.execute("UPDATE bookings SET status = 'standby' WHERE event_id = ? AND category = ? AND role = 'backup'", (row['event_id'], row['category']))
            conn.execute("UPDATE enquiries SET status = 'standby_confirmed', updated_at = ? WHERE event_id = ? AND vendor_id = ?", (now(), row['event_id'], row['vendor_id']))
            conn.execute('INSERT INTO alerts (event_id, created_at, level, message) VALUES (?, ?, ?, ?)', (row['event_id'], now(), 'info', message))
            return True
        if row['role'] == 'backup':
            conn.execute("UPDATE booking_confirmations SET status = 'superseded', updated_at = ? WHERE event_id = ? AND category = ? AND role = 'primary' AND status IN ('pending','calling','changed')", (now(), row['event_id'], row['category']))
            conn.execute("UPDATE booking_confirmations SET role = 'primary' WHERE id = ?", (confirmation_id,))
            conn.execute("UPDATE bookings SET vendor_id = ?, status = 'active', created_at = ? WHERE event_id = ? AND category = ? AND role = 'primary'", (row['vendor_id'], now(), row['event_id'], row['category']))
            conn.execute("UPDATE bookings SET vendor_id = ?, status = 'replaced' WHERE event_id = ? AND category = ? AND role = 'backup'", (row['parent_vendor_id'], row['event_id'], row['category']))
            conn.execute("UPDATE enquiries SET status = 'replaced', updated_at = ? WHERE event_id = ? AND vendor_id = ?", (now(), row['event_id'], row['parent_vendor_id']))
        conn.execute("UPDATE bookings SET status = 'active' WHERE event_id = ? AND category = ? AND role = 'primary'", (row['event_id'], row['category']))
        conn.execute("UPDATE enquiries SET status = 'confirmed', updated_at = ? WHERE event_id = ? AND vendor_id = ?", (now(), row['event_id'], row['vendor_id']))
        conn.execute("DELETE FROM checkins WHERE event_id = ? AND category = ? AND status = 'pending'", (row['event_id'], row['category']))
        terms = json.loads(row['terms'])
        conn.executemany("INSERT INTO checkins (event_id, vendor_id, category, label, due_at, status, confirmation_id, expires_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            [(row['event_id'], row['vendor_id'], row['category'], label, due, confirmation_id,
              checkin_policy.expires_at(terms, row['category'], label, due).isoformat(timespec='seconds') if due is not None else None) for label, due in schedule])
        conn.execute('INSERT INTO alerts (event_id, created_at, level, message) VALUES (?, ?, ?, ?)', (row['event_id'], now(), 'info', message))
        return True


def clear_pending_checkins(event_id: int, category: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM checkins WHERE event_id = ? AND category = ? AND status = 'pending'", (event_id, category))


def clear_vendor_pending_checkins(event_id: int, category: str, vendor_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM checkins WHERE event_id = ? AND category = ? AND vendor_id = ? AND status = 'pending'", (event_id, category, vendor_id))


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


def finish_checkin(checkin_id: int, status: str, eta: str | None, notes: str | None, *, attempt_id: int | None = None) -> bool:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if attempt_id is None:
            changed = conn.execute("UPDATE checkins SET status = ?, eta = ?, notes = ?, done_at = ?, dispatch_state = 'completed' WHERE id = ? AND status = 'pending' AND dispatch_state = 'scheduled'",
                (status, eta, notes, now(), checkin_id)).rowcount
        else:
            changed = conn.execute("UPDATE checkins SET status = ?, eta = ?, notes = ?, done_at = ?, dispatch_state = 'completed' WHERE id = ? AND status = 'pending' AND current_attempt_id = ? AND dispatch_state = 'running'",
                (status, eta, notes, now(), checkin_id, attempt_id)).rowcount
            if changed:
                conn.execute("UPDATE checkin_attempts SET status = 'completed', finished_at = ? WHERE id = ? AND status = 'running'", (now(), attempt_id))
        return changed == 1


def checkin_attempt(attempt_id: int) -> dict:
    with connect() as conn:
        row = conn.execute('SELECT * FROM checkin_attempts WHERE id = ?', (attempt_id,)).fetchone()
    if not row:
        raise LookupError('No such check-in attempt')
    return dict(row)


def checkin_attempts(checkin_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM checkin_attempts WHERE checkin_id = ? ORDER BY id', (checkin_id,)).fetchall()]


def enqueue_checkin(checkin_id: int, confirmation_id: int, expiry: str, origin: str, at: str) -> int | None:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT * FROM checkins WHERE id = ? AND status = 'pending' AND dispatch_state = 'scheduled'", (checkin_id,)).fetchone()
        if not row:
            return None
        cur = conn.execute("INSERT INTO checkin_attempts (checkin_id, origin, status, created_at) VALUES (?, ?, 'queued', ?)", (checkin_id, origin, at))
        attempt_id = cur.lastrowid
        conn.execute("UPDATE checkins SET dispatch_state = 'queued', current_attempt_id = ?, confirmation_id = ?, expires_at = ?, next_attempt_at = NULL WHERE id = ?", (attempt_id, confirmation_id, expiry, checkin_id))
        return attempt_id


def claim_checkin_attempt(attempt_id: int, at: str) -> bool:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT a.checkin_id FROM checkin_attempts a JOIN checkins c ON c.id = a.checkin_id WHERE a.id = ? AND a.status = 'queued' AND c.status = 'pending' AND c.current_attempt_id = a.id AND c.dispatch_state = 'queued'", (attempt_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE checkin_attempts SET status = 'running', started_at = ? WHERE id = ?", (at, attempt_id))
        conn.execute("UPDATE checkins SET dispatch_state = 'running' WHERE id = ?", (row['checkin_id'],))
        return True


def bind_checkin_attempt(attempt_id: int, call_id: str) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE checkin_attempts SET call_id = ? WHERE id = ? AND status = 'running' AND (call_id IS NULL OR call_id = ?)", (call_id, attempt_id, call_id)).rowcount == 1


def release_checkin_attempt(attempt_id: int, status: str, reason: str, at: str, next_attempt_at: str | None) -> None:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT checkin_id FROM checkin_attempts WHERE id = ? AND status IN ('queued', 'running')", (attempt_id,)).fetchone()
        if not row:
            return
        conn.execute('UPDATE checkin_attempts SET status = ?, reason = ?, finished_at = ? WHERE id = ?', (status, reason, at, attempt_id))
        conn.execute("UPDATE checkins SET dispatch_state = 'scheduled', current_attempt_id = NULL, next_attempt_at = ?, notes = ? WHERE id = ? AND status = 'pending' AND current_attempt_id = ?", (next_attempt_at, reason, row['checkin_id'], attempt_id))


def skip_checkin(checkin_id: int, reason: str, at: str) -> bool:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT current_attempt_id FROM checkins WHERE id = ? AND status = 'pending' AND dispatch_state IN ('scheduled', 'queued')", (checkin_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE checkins SET status = 'skipped', dispatch_state = 'skipped', notes = ?, done_at = ? WHERE id = ?", (reason, at, checkin_id))
        if row['current_attempt_id']:
            conn.execute("UPDATE checkin_attempts SET status = 'stale', reason = ?, finished_at = ? WHERE id = ? AND status = 'queued'", (reason, at, row['current_attempt_id']))
        return True


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
