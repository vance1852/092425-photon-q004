"""芯片批次、工艺版本和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS process_versions(
 version_id TEXT PRIMARY KEY,
 product TEXT NOT NULL,
 version_label TEXT NOT NULL,
 parent_version_id TEXT REFERENCES process_versions(version_id),
 change_reason TEXT NOT NULL,
 params_json TEXT NOT NULL,
 params_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('draft','frozen')),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 frozen_by TEXT, frozen_at TEXT,
 UNIQUE(product, version_label));
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 process_version_id TEXT REFERENCES process_versions(version_id),
 bound_by TEXT, bound_at TEXT);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS version_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS idempotency_keys(
 scope TEXT NOT NULL, key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,key));
"""

# 早期版本的 chip_lots 没有工艺版本绑定列，这里按列补齐。
_LOT_BINDING_COLUMNS = (
    ("process_version_id", "TEXT REFERENCES process_versions(version_id)"),
    ("bound_by", "TEXT"),
    ("bound_at", "TEXT"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_lots(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(chip_lots)")}
    for name, definition in _LOT_BINDING_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE chip_lots ADD COLUMN {name} {definition}")


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    _migrate_lots(db)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))


def version_event(db: sqlite3.Connection, version_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO version_events(version_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (version_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))
