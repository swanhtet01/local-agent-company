"""Stable local configuration and bounded company-store identity rules."""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from pathlib import Path


COMPANY_STORE_SCHEMA = "local-company.store.v1"
COMPANY_DB_SCHEMA_VERSION = 1
COMPANY_INSTANCE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_COMPANY_IDENTITY_SQLITE_LENGTH_LIMIT = 4096
_COMPANY_IDENTITY_VALUE_BYTE_LIMIT = 256
_COMPANY_METADATA_COLUMN_MARKERS = [
    (0, 1, 1, 1, 1, 1, 1),
    (1, 1, 1, 1, 1, 1, 1),
]


def valid_company_instance_id(value: object) -> bool:
    if type(value) is not str or COMPANY_INSTANCE_ID_PATTERN.fullmatch(value) is None:
        return False
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        return False
    return parsed.hex == value and parsed.version == 4


def read_validated_company_instance_id(db: sqlite3.Connection) -> str:
    """Read the exact v1 identity under a temporary SQLite allocation ceiling."""
    previous_limit = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    db.setlimit(
        sqlite3.SQLITE_LIMIT_LENGTH,
        min(previous_limit, _COMPANY_IDENTITY_SQLITE_LENGTH_LIMIT),
    )
    try:
        return _read_company_instance_id_under_limit(db)
    finally:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)


def _read_company_instance_id_under_limit(db: sqlite3.Connection) -> str:
    version_row = db.execute("PRAGMA user_version").fetchone()
    objects = db.execute(
        "SELECT type FROM sqlite_master WHERE name='company_metadata' LIMIT 2"
    ).fetchall()
    columns = db.execute(
        "SELECT cid, "
        "CASE cid WHEN 0 THEN name='key' WHEN 1 THEN name='value' ELSE 0 END, "
        "type='TEXT', \"notnull\"=1, dflt_value IS NULL, "
        "CASE cid WHEN 0 THEN pk=1 WHEN 1 THEN pk=0 ELSE 0 END, hidden=0 "
        "FROM pragma_table_xinfo('company_metadata') ORDER BY cid LIMIT 3"
    ).fetchall()
    row_markers = db.execute(
        "SELECT 1 FROM company_metadata LIMIT 3"
    ).fetchall() if (
        objects == [("table",)] and columns == _COMPANY_METADATA_COLUMN_MARKERS
    ) else []
    if (
        version_row != (COMPANY_DB_SCHEMA_VERSION,)
        or objects != [("table",)]
        or columns != _COMPANY_METADATA_COLUMN_MARKERS
        or len(row_markers) != 2
    ):
        raise ValueError("invalid local company store identity")

    def bounded_value(key: str) -> object:
        size_rows = db.execute(
            "SELECT typeof(value), length(CAST(value AS BLOB)) "
            "FROM company_metadata WHERE key=? LIMIT 2",
            (key,),
        ).fetchall()
        if (
            len(size_rows) != 1
            or size_rows[0][0] != "text"
            or type(size_rows[0][1]) is not int
            or size_rows[0][1] < 1
            or size_rows[0][1] > _COMPANY_IDENTITY_VALUE_BYTE_LIMIT
        ):
            raise ValueError("invalid local company store identity")
        value_rows = db.execute(
            "SELECT value FROM company_metadata WHERE key=? LIMIT 2", (key,),
        ).fetchall()
        if len(value_rows) != 1 or type(value_rows[0][0]) is not str:
            raise ValueError("invalid local company store identity")
        return value_rows[0][0]

    schema = bounded_value("instance_schema")
    instance_id = bounded_value("instance_id")
    if schema != COMPANY_STORE_SCHEMA or not valid_company_instance_id(instance_id):
        raise ValueError("invalid local company store identity")
    return instance_id


def default_company_home() -> Path:
    """Resolve a per-user default that never depends on the current directory."""
    configured = os.getenv("LOCAL_COMPANY_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
        if candidate.anchor or candidate.drive or candidate.root or ".." in candidate.parts:
            raise ValueError("LOCAL_COMPANY_HOME must be absolute or user-home relative")
        return Path.home() / candidate
    return Path.home() / ".local-company"
