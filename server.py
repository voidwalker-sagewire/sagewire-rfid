from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field


APP_NAME = "sagewire-rfid"
APP_VERSION = "0.2.0"
DATABASE_PATH = Path(
    os.getenv("RFID_DATABASE_PATH", "/data/rfid.db")
)
DEDUPE_SECONDS = float(
    os.getenv("RFID_DEDUPE_SECONDS", "2.0")
)

ALLOWED_TECHNOLOGIES = {
    "UHF",
    "LF_FDXB",
    "LF_HDX",
    "LF_OTHER",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_datetime(value: datetime | None) -> datetime:
    if value is None:
        return now_utc()

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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
            ON events(
                reader_id,
                tag_id,
                observed_at DESC
            );

            CREATE INDEX IF NOT EXISTS idx_events_received
            ON events(received_at DESC);
            """
        )


def normalize_technology(value: str) -> str:
    technology = (
        value.strip()
        .upper()
        .replace("-", "_")
    )

    aliases = {
        "FDXB": "LF_FDXB",
        "FDX_B": "LF_FDXB",
        "HDX": "LF_HDX",
        "LF": "LF_OTHER",
    }

    technology = aliases.get(
        technology,
        technology,
    )

    if technology not in ALLOWED_TECHNOLOGIES:
        allowed = ", ".join(
            sorted(ALLOWED_TECHNOLOGIES)
        )
        raise ValueError(
            f"technology must be one of: {allowed}"
        )

    return technology


def normalize_tag(value: str) -> str:
    cleaned = value.strip()

    compact = (
        cleaned.replace(" ", "")
        .replace(":", "")
    )

    if compact and all(
        character
        in "0123456789abcdefABCDEF"
        for character in compact
    ):
        return compact.upper()

    return cleaned


def reader_dict(
    row: sqlite3.Row,
) -> dict[str, Any]:
    return {
        "reader_id": row["reader_id"],
        "name": row["name"],
        "technology": row["technology"],
        "model": row["model"],
        "serial_number": row["serial_number"],
        "location_id": row["location_id"],
        "metadata": json.loads(
            row["metadata_json"] or "{}"
        ),
        "registered_at": row["registered_at"],
        "last_heartbeat_at": row[
            "last_heartbeat_at"
        ],
    }


def event_dict(
    row: sqlite3.Row,
) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "technology": row["technology"],
        "reader_id": row["reader_id"],
        "antenna_id": row["antenna_id"],
        "tag_id": row["tag_id"],
        "raw_tag": row["raw_tag"],
        "rssi": row["rssi"],
        "location_id": row["location_id"],
        "observed_at": row["observed_at"],
        "received_at": row["received_at"],
        "duplicate": bool(row["duplicate"]),
        "duplicate_of": row["duplicate_of"],
        "metadata": json.loads(
            row["metadata_json"] or "{}"
        ),
    }


class ReaderRegistration(BaseModel):
    model_config = ConfigDict(
        extra="forbid"
    )

    reader_id: str = Field(
        min_length=1
    )
    name: str = Field(
        min_length=1
    )
    technology: str = Field(
        min_length=1
    )
    model: str | None = None
    serial_number: str | None = None
    location_id: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict
    )


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid"
    )

    observed_at: datetime | None = None


class RFIDReadRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid"
    )

    technology: str = Field(
        min_length=1
    )
    reader_id: str = Field(
        min_length=1
    )
    tag_id: str = Field(
        min_length=1
    )
    antenna_id: str | None = None
    raw_tag: str | None = None
    rssi: float | None = None
    location_id: str | None = None
    observed_at: datetime | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict
    )


app = FastAPI(
    title="SageWire RFID",
    description=(
        "Hardware-independent RFID reader "
        "registration, health, normalization, "
        "deduplication, and event history."
    ),
    version=APP_VERSION,
)

init_db()


@app.get(
    "/health",
    tags=["System"],
)
def health() -> dict[str, Any]:
    try:
        with db() as conn:
            conn.execute("SELECT 1")

        return {
            "service": APP_NAME,
            "status": "ok",
            "version": APP_VERSION,
            "database": "ok",
            "dedupe_seconds": DEDUPE_SECONDS,
            "time": iso_z(now_utc()),
        }

    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "database": "error",
                "message": str(exc),
            },
        ) from exc


@app.get(
    "/version",
    tags=["System"],
)
def version() -> dict[str, str]:
    return {
        "service": APP_NAME,
        "version": APP_VERSION,
    }


@app.post(
    "/readers/register",
    tags=["Readers"],
    responses={
        200: {
            "description": (
                "Existing reader updated"
            )
        },
        201: {
            "description": (
                "New reader registered"
            )
        },
    },
)
def register_reader(
    payload: ReaderRegistration,
    response: Response,
):
    try:
        technology = normalize_technology(
            payload.technology
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    reader_id = payload.reader_id.strip()
    name = payload.name.strip()

    with db() as conn:
        existing = conn.execute(
            """
            SELECT 1
            FROM readers
            WHERE reader_id=?
            """,
            (reader_id,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE readers
                SET
                    name=?,
                    technology=?,
                    model=?,
                    serial_number=?,
                    location_id=?,
                    metadata_json=?
                WHERE reader_id=?
                """,
                (
                    name,
                    technology,
                    payload.model,
                    payload.serial_number,
                    payload.location_id,
                    json.dumps(
                        payload.metadata
                    ),
                    reader_id,
                ),
            )

            created = False

        else:
            conn.execute(
                """
                INSERT INTO readers(
                    reader_id,
                    name,
                    technology,
                    model,
                    serial_number,
                    location_id,
                    metadata_json,
                    registered_at
                )
                VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    reader_id,
                    name,
                    technology,
                    payload.model,
                    payload.serial_number,
                    payload.location_id,
                    json.dumps(
                        payload.metadata
                    ),
                    iso_z(now_utc()),
                ),
            )

            created = True

        row = conn.execute(
            """
            SELECT *
            FROM readers
            WHERE reader_id=?
            """,
            (reader_id,),
        ).fetchone()

    response.status_code = (
        status.HTTP_201_CREATED
        if created
        else status.HTTP_200_OK
    )

    return {
        "ok": True,
        "created": created,
        "reader": reader_dict(row),
    }


@app.post(
    "/readers/{reader_id}/heartbeat",
    tags=["Readers"],
)
def heartbeat(
    reader_id: str,
    payload: HeartbeatRequest | None = None,
):
    observed_at = (
        payload.observed_at
        if payload
        else None
    )

    heartbeat_at = iso_z(
        normalize_datetime(observed_at)
    )

    with db() as conn:
        exists = conn.execute(
            """
            SELECT 1
            FROM readers
            WHERE reader_id=?
            """,
            (reader_id,),
        ).fetchone()

        if exists is None:
            raise HTTPException(
                status_code=404,
                detail="reader_not_found",
            )

        conn.execute(
            """
            UPDATE readers
            SET last_heartbeat_at=?
            WHERE reader_id=?
            """,
            (
                heartbeat_at,
                reader_id,
            ),
        )

        row = conn.execute(
            """
            SELECT *
            FROM readers
            WHERE reader_id=?
            """,
            (reader_id,),
        ).fetchone()

    return {
        "ok": True,
        "reader": reader_dict(row),
    }


@app.get(
    "/readers",
    tags=["Readers"],
)
def readers():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM readers
            ORDER BY registered_at DESC
            """
        ).fetchall()

    return {
        "ok": True,
        "count": len(rows),
        "readers": [
            reader_dict(row)
            for row in rows
        ],
    }


@app.get(
    "/readers/{reader_id}",
    tags=["Readers"],
)
def reader(reader_id: str):
    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM readers
            WHERE reader_id=?
            """,
            (reader_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="reader_not_found",
        )

    return {
        "ok": True,
        "reader": reader_dict(row),
    }


@app.post(
    "/events/read",
    tags=["Events"],
    responses={
        200: {
            "description": (
                "Duplicate RFID read recorded"
            )
        },
        201: {
            "description": (
                "New RFID read recorded"
            )
        },
    },
)
def read_event(
    payload: RFIDReadRequest,
    response: Response,
):
    try:
        technology = normalize_technology(
            payload.technology
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    reader_id = payload.reader_id.strip()
    tag_id = normalize_tag(
        payload.tag_id
    )

    antenna_id = (
        payload.antenna_id.strip()
        if payload.antenna_id
        else None
    )

    raw_tag = (
        payload.raw_tag.strip()
        if payload.raw_tag
        else tag_id
    )

    observed_at = normalize_datetime(
        payload.observed_at
    )

    received_at = now_utc()

    with db() as conn:
        reader_row = conn.execute(
            """
            SELECT *
            FROM readers
            WHERE reader_id=?
            """,
            (reader_id,),
        ).fetchone()

        if reader_row is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "reader_not_registered"
                ),
            )

        previous = conn.execute(
            """
            SELECT *
            FROM events
            WHERE reader_id=?
              AND tag_id=?
              AND COALESCE(
                    antenna_id,
                    ''
                  ) = COALESCE(
                    ?,
                    ''
                  )
              AND duplicate=0
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (
                reader_id,
                tag_id,
                antenna_id,
            ),
        ).fetchone()

        duplicate = False
        duplicate_of = None

        if previous is not None:
            previous_at = (
                datetime.fromisoformat(
                    previous[
                        "observed_at"
                    ].replace(
                        "Z",
                        "+00:00",
                    )
                )
            )

            elapsed = abs(
                (
                    observed_at
                    - previous_at
                ).total_seconds()
            )

            if elapsed <= DEDUPE_SECONDS:
                duplicate = True
                duplicate_of = previous[
                    "event_id"
                ]

        event_id = str(uuid.uuid4())

        conn.execute(
            """
            INSERT INTO events(
                event_id,
                event_type,
                technology,
                reader_id,
                antenna_id,
                tag_id,
                raw_tag,
                rssi,
                location_id,
                observed_at,
                received_at,
                duplicate,
                duplicate_of,
                metadata_json
            )
            VALUES(
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                event_id,
                "rfid.read",
                technology,
                reader_id,
                antenna_id,
                tag_id,
                raw_tag,
                payload.rssi,
                (
                    payload.location_id
                    or reader_row[
                        "location_id"
                    ]
                ),
                iso_z(observed_at),
                iso_z(received_at),
                int(duplicate),
                duplicate_of,
                json.dumps(
                    payload.metadata
                ),
            ),
        )

        row = conn.execute(
            """
            SELECT *
            FROM events
            WHERE event_id=?
            """,
            (event_id,),
        ).fetchone()

    response.status_code = (
        status.HTTP_200_OK
        if duplicate
        else status.HTTP_201_CREATED
    )

    return {
        "ok": True,
        "duplicate": duplicate,
        "event": event_dict(row),
    }


@app.get(
    "/events",
    tags=["Events"],
)
def events(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
    include_duplicates: bool = False,
):
    if include_duplicates:
        sql = """
            SELECT *
            FROM events
            ORDER BY received_at DESC
            LIMIT ?
        """
    else:
        sql = """
            SELECT *
            FROM events
            WHERE duplicate=0
            ORDER BY received_at DESC
            LIMIT ?
        """

    with db() as conn:
        rows = conn.execute(
            sql,
            (limit,),
        ).fetchall()

    return {
        "ok": True,
        "count": len(rows),
        "events": [
            event_dict(row)
            for row in rows
        ],
    }


@app.get(
    "/events/latest",
    tags=["Events"],
)
def latest_event(
    reader_id: str | None = None,
    include_duplicates: bool = False,
):
    conditions: list[str] = []
    params: list[Any] = []

    if reader_id:
        conditions.append(
            "reader_id=?"
        )
        params.append(reader_id)

    if not include_duplicates:
        conditions.append(
            "duplicate=0"
        )

    sql = """
        SELECT *
        FROM events
    """

    if conditions:
        sql += (
            " WHERE "
            + " AND ".join(conditions)
        )

    sql += """
        ORDER BY received_at DESC
        LIMIT 1
    """

    with db() as conn:
        row = conn.execute(
            sql,
            params,
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="no_events",
        )

    return {
        "ok": True,
        "event": event_dict(row),
}
