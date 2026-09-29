"""海况修订留存层：表结构与 SQL 访问。

所有方法接收外部 sqlite 连接，在调用方事务内执行，自身不提交、不回滚，
因此“释放旧安排 / 占用新资源 / 登记改派 / 落库结果”要么全部生效，要么全部回滚，
崩溃后不会留下半套占用。
"""
from __future__ import annotations

from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sea_condition_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    sea_state INTEGER NOT NULL,
    drift_direction REAL NOT NULL DEFAULT 0,
    drift_speed_kn REAL NOT NULL DEFAULT 0,
    effective_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    impact_summary TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS revision_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES sea_condition_revisions(id),
    area_id INTEGER NOT NULL REFERENCES search_areas(id),
    original_asset_id INTEGER REFERENCES assets(id),
    replacement_asset_id INTEGER REFERENCES assets(id),
    new_center_lat REAL,
    new_center_lon REAL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_incident ON sea_condition_revisions(incident_id, id);
CREATE INDEX IF NOT EXISTS idx_revision_assignments_rev ON revision_assignments(revision_id, id);
"""


def init_schema(conn) -> None:
    conn.executescript(SCHEMA)


def create_revision(conn, *, incident_id: int, sea_state: int, drift_direction: float,
                    drift_speed_kn: float, effective_at: str, actor: str, now: str) -> dict[str, Any]:
    cur = conn.execute(
        """INSERT INTO sea_condition_revisions
           (incident_id,sea_state,drift_direction,drift_speed_kn,effective_at,status,version,created_by,created_at)
           VALUES(?,?,?,?,?, 'pending', 1, ?, ?)""",
        (incident_id, sea_state, drift_direction, drift_speed_kn, effective_at, actor, now),
    )
    return get_revision(conn, cur.lastrowid)


def get_revision(conn, revision_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sea_condition_revisions WHERE id=?", (revision_id,)).fetchone()
    return dict(row) if row else None


def list_revisions(conn, incident_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sea_condition_revisions WHERE incident_id=? ORDER BY id DESC", (incident_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def insert_assignment(conn, *, revision_id: int, area_id: int, original_asset_id: int | None,
                       replacement_asset_id: int | None, new_center_lat: float | None,
                       new_center_lon: float | None, reason: str, status: str,
                       now: str) -> dict[str, Any]:
    cur = conn.execute(
        """INSERT INTO revision_assignments
           (revision_id,area_id,original_asset_id,replacement_asset_id,new_center_lat,new_center_lon,
            reason,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (revision_id, area_id, original_asset_id, replacement_asset_id,
         new_center_lat, new_center_lon, reason, status, now, now),
    )
    row = conn.execute("SELECT * FROM revision_assignments WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_assignments(conn, revision_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM revision_assignments WHERE revision_id=? ORDER BY id", (revision_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_assignment(conn, assignment_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM revision_assignments WHERE id=?", (assignment_id,)).fetchone()
    return dict(row) if row else None
