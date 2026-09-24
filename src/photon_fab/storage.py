"""芯片批次、测量记录与工艺版本的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS process_versions(
 process_id TEXT NOT NULL, version INTEGER NOT NULL,
 parent_id TEXT, parent_version INTEGER,
 state TEXT NOT NULL CHECK(state IN ('draft','frozen')),
 product TEXT NOT NULL,
 parameters_json TEXT NOT NULL, parameters_sha256 TEXT NOT NULL,
 change_reason TEXT, created_by TEXT NOT NULL,
 created_at TEXT NOT NULL, frozen_at TEXT,
 PRIMARY KEY(process_id,version),
 FOREIGN KEY(parent_id,parent_version) REFERENCES process_versions(process_id,version));
CREATE TABLE IF NOT EXISTS lot_process_bindings(
 lot_id TEXT PRIMARY KEY REFERENCES chip_lots(lot_id),
 process_id TEXT NOT NULL, version INTEGER NOT NULL,
 bound_by TEXT NOT NULL, bound_at TEXT NOT NULL, change_reason TEXT,
 FOREIGN KEY(process_id,version) REFERENCES process_versions(process_id,version));
CREATE TABLE IF NOT EXISTS process_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 process_id TEXT NOT NULL, version INTEGER NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS process_idempotency(
 scope TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(scope,idempotency_key));
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
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


def process_event(db: sqlite3.Connection, process_id: str, version: int, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO process_events(process_id,version,event_type,actor,payload,created_at) VALUES(?,?,?,?,?,?)",
        (process_id, version, event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), utcnow()),
    )
