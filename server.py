from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from flask import Flask, jsonify, request

APP_NAME = "sagewire-rfid"
APP_VERSION = "0.1.0"
DATABASE_PATH = Path(os.getenv("RFID_DATABASE_PATH", "/data/rfid.db"))
DEDUPE_SECONDS = float(os.getenv("RFID_DEDUPE_SECONDS", "2.0"))
ALLOWED_TECHNOLOGIES = {"UHF", "LF_FDXB", "LF_HDX", "LF_OTHER"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime:
    if not value:
        return now_utc()
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readers (
                reader_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                technology TEXT NOT NULL,
                model TEXT,
                serial_number TEXT,
                location_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                registered_at TEXT NOT NULL,
                last_heartbeat_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                technology TEXT NOT NULL,
                reader_id TEXT NOT NULL,
                antenna_id TEXT,
                tag_id TEXT NOT NULL,
                raw_tag TEXT,
                rssi REAL,
                location_id TEXT,
                observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                duplicate INTEGER NOT NULL DEFAULT 0,
                duplicate_of TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_events_reader_tag_time
            ON events(reader_id, tag_id, observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_received
            ON events(received_at DESC);
            """
        )


def json_object() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return payload


def required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip() or None


def normalize_technology(value: str) -> str:
    technology = value.strip().upper().replace("-", "_")
    aliases = {"FDXB": "LF_FDXB", "FDX_B": "LF_FDXB", "HDX": "LF_HDX", "LF": "LF_OTHER"}
    technology = aliases.get(technology, technology)
    if technology not in ALLOWED_TECHNOLOGIES:
        raise ValueError("technology must be one of: " + ", ".join(sorted(ALLOWED_TECHNOLOGIES)))
    return technology


def normalize_tag(value: str) -> str:
    cleaned = value.strip()
    compact = cleaned.replace(" ", "").replace(":", "")
    if compact and all(c in "0123456789abcdefABCDEF" for c in compact):
        return compact.upper()
    return cleaned


def reader_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reader_id": row["reader_id"], "name": row["name"], "technology": row["technology"],
        "model": row["model"], "serial_number": row["serial_number"], "location_id": row["location_id"],
        "metadata": json.loads(row["metadata_json"] or "{}"), "registered_at": row["registered_at"],
        "last_heartbeat_at": row["last_heartbeat_at"]
    }


def event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"], "event_type": row["event_type"], "technology": row["technology"],
        "reader_id": row["reader_id"], "antenna_id": row["antenna_id"], "tag_id": row["tag_id"],
        "raw_tag": row["raw_tag"], "rssi": row["rssi"], "location_id": row["location_id"],
        "observed_at": row["observed_at"], "received_at": row["received_at"],
        "duplicate": bool(row["duplicate"]), "duplicate_of": row["duplicate_of"],
        "metadata": json.loads(row["metadata_json"] or "{}")
    }


app = Flask(__name__)
init_db()


@app.get("/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return jsonify({"service": APP_NAME, "status": "ok", "version": APP_VERSION,
                        "database": "ok", "dedupe_seconds": DEDUPE_SECONDS, "time": iso_z(now_utc())}), 200
    except sqlite3.Error:
        return jsonify({"service": APP_NAME, "status": "degraded", "version": APP_VERSION,
                        "database": "error", "time": iso_z(now_utc())}), 503


@app.get("/version")
def version():
    return jsonify({"service": APP_NAME, "version": APP_VERSION}), 200


@app.post("/readers/register")
def register_reader():
    try:
        p = json_object()
        reader_id = required_str(p, "reader_id")
        name = required_str(p, "name")
        technology = normalize_technology(required_str(p, "technology"))
        metadata = p.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        with db() as conn:
            existing = conn.execute("SELECT 1 FROM readers WHERE reader_id=?", (reader_id,)).fetchone()
            if existing:
                conn.execute("""UPDATE readers SET name=?,technology=?,model=?,serial_number=?,location_id=?,metadata_json=? WHERE reader_id=?""",
                             (name, technology, optional_str(p, "model"), optional_str(p, "serial_number"),
                              optional_str(p, "location_id"), json.dumps(metadata), reader_id))
                created = False
            else:
                conn.execute("""INSERT INTO readers(reader_id,name,technology,model,serial_number,location_id,metadata_json,registered_at)
                              VALUES(?,?,?,?,?,?,?,?)""",
                             (reader_id, name, technology, optional_str(p, "model"), optional_str(p, "serial_number"),
                              optional_str(p, "location_id"), json.dumps(metadata), iso_z(now_utc())))
                created = True
            row = conn.execute("SELECT * FROM readers WHERE reader_id=?", (reader_id,)).fetchone()
        return jsonify({"ok": True, "created": created, "reader": reader_dict(row)}), 201 if created else 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except sqlite3.Error as exc:
        return jsonify({"ok": False, "error": "database_error", "detail": str(exc)}), 500


@app.post("/readers/<reader_id>/heartbeat")
def heartbeat(reader_id: str):
    try:
        p = request.get_json(silent=True) or {}
        if not isinstance(p, dict):
            raise ValueError("Request body must be a JSON object")
        heartbeat_at = iso_z(parse_time(p.get("observed_at")))
        with db() as conn:
            if conn.execute("SELECT 1 FROM readers WHERE reader_id=?", (reader_id,)).fetchone() is None:
                return jsonify({"ok": False, "error": "reader_not_found"}), 404
            conn.execute("UPDATE readers SET last_heartbeat_at=? WHERE reader_id=?", (heartbeat_at, reader_id))
            row = conn.execute("SELECT * FROM readers WHERE reader_id=?", (reader_id,)).fetchone()
        return jsonify({"ok": True, "reader": reader_dict(row)}), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/readers")
def readers():
    with db() as conn:
        rows = conn.execute("SELECT * FROM readers ORDER BY registered_at DESC").fetchall()
    return jsonify({"ok": True, "count": len(rows), "readers": [reader_dict(r) for r in rows]}), 200


@app.get("/readers/<reader_id>")
def reader(reader_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM readers WHERE reader_id=?", (reader_id,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "reader_not_found"}), 404
    return jsonify({"ok": True, "reader": reader_dict(row)}), 200


@app.post("/events/read")
def read_event():
    try:
        p = json_object()
        technology = normalize_technology(required_str(p, "technology"))
        reader_id = required_str(p, "reader_id")
        tag_id = normalize_tag(required_str(p, "tag_id"))
        antenna_id = optional_str(p, "antenna_id")
        raw_tag = optional_str(p, "raw_tag") or tag_id
        location_id = optional_str(p, "location_id")
        observed_at = parse_time(p.get("observed_at"))
        received_at = now_utc()
        rssi = p.get("rssi")
        if rssi is not None and (isinstance(rssi, bool) or not isinstance(rssi, (int, float))):
            raise ValueError("rssi must be numeric")
        metadata = p.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        with db() as conn:
            reader_row = conn.execute("SELECT * FROM readers WHERE reader_id=?", (reader_id,)).fetchone()
            if reader_row is None:
                return jsonify({"ok": False, "error": "reader_not_registered"}), 409
            previous = conn.execute("""SELECT * FROM events WHERE reader_id=? AND tag_id=?
                                      AND COALESCE(antenna_id,'')=COALESCE(?,'') AND duplicate=0
                                      ORDER BY observed_at DESC LIMIT 1""",
                                    (reader_id, tag_id, antenna_id)).fetchone()
            duplicate = False
            duplicate_of = None
            if previous is not None:
                elapsed = abs((observed_at - parse_time(previous["observed_at"])).total_seconds())
                if elapsed <= DEDUPE_SECONDS:
                    duplicate = True
                    duplicate_of = previous["event_id"]
            event_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO events(event_id,event_type,technology,reader_id,antenna_id,tag_id,raw_tag,rssi,
                          location_id,observed_at,received_at,duplicate,duplicate_of,metadata_json)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (event_id, "rfid.read", technology, reader_id, antenna_id, tag_id, raw_tag,
                          float(rssi) if rssi is not None else None, location_id or reader_row["location_id"],
                          iso_z(observed_at), iso_z(received_at), int(duplicate), duplicate_of, json.dumps(metadata)))
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return jsonify({"ok": True, "duplicate": duplicate, "event": event_dict(row)}), 200 if duplicate else 201
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except sqlite3.Error as exc:
        return jsonify({"ok": False, "error": "database_error", "detail": str(exc)}), 500


@app.get("/events")
def events():
    try:
        limit = max(1, min(int(request.args.get("limit", "100")), 1000))
        include_duplicates = request.args.get("include_duplicates", "false").lower() in {"1", "true", "yes"}
        sql = "SELECT * FROM events" + ("" if include_duplicates else " WHERE duplicate=0") + " ORDER BY received_at DESC LIMIT ?"
        with db() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return jsonify({"ok": True, "count": len(rows), "events": [event_dict(r) for r in rows]}), 200
    except ValueError:
        return jsonify({"ok": False, "error": "limit must be an integer"}), 400


@app.get("/events/latest")
def latest_event():
    reader_id = request.args.get("reader_id")
    include_duplicates = request.args.get("include_duplicates", "false").lower() in {"1", "true", "yes"}
    conditions, params = [], []
    if reader_id:
        conditions.append("reader_id=?")
        params.append(reader_id)
    if not include_duplicates:
        conditions.append("duplicate=0")
    sql = "SELECT * FROM events" + ((" WHERE " + " AND ".join(conditions)) if conditions else "") + " ORDER BY received_at DESC LIMIT 1"
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "no_events"}), 404
    return jsonify({"ok": True, "event": event_dict(row)}), 200


@app.errorhandler(404)
def not_found(_):
    return jsonify({"ok": False, "error": "not_found"}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"ok": False, "error": "method_not_allowed"}), 405


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
