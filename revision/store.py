"""留存层：海况修订的 SQLite 事务写入、幂等留存与崩溃恢复。"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Callable

from .common import DomainError, json_dump, utcnow
from .models import (
    ACTION_REASSIGN_GAP,
    ACTION_REASSIGN_PENDING,
    ACTION_RELEASED_REPLANNED,
    ACTION_RELEASED_UNASSIGNED,
    ACTIVE_INCIDENT_STATUSES,
    GAP_ACTIONS,
    REASSIGN_CONFIRMED,
    REASSIGN_GAP,
    REASSIGN_PENDING,
    REVISION_APPLIED,
    REVISION_FAILED,
    REVISION_REGISTERED,
    Impact,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sea_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    revision_no INTEGER NOT NULL,
    client_token TEXT NOT NULL UNIQUE,
    sea_state INTEGER NOT NULL,
    drift_direction REAL NOT NULL,
    drift_speed_kn REAL NOT NULL,
    effective_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    base_plan_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered',
    result TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE(incident_id, revision_no)
);
CREATE TABLE IF NOT EXISTS revision_impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES sea_revisions(id),
    incident_id INTEGER NOT NULL,
    area_id INTEGER NOT NULL REFERENCES search_areas(id),
    action TEXT NOT NULL,
    original_asset_id INTEGER,
    replacement_asset_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reassignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES sea_revisions(id),
    incident_id INTEGER NOT NULL,
    area_id INTEGER NOT NULL REFERENCES search_areas(id),
    original_asset_id INTEGER NOT NULL,
    replacement_asset_id INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    confirmed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_revision_impacts_revision ON revision_impacts(revision_id);
CREATE INDEX IF NOT EXISTS idx_reassignments_revision ON reassignments(revision_id);
"""

# 判断函数签名：由服务层注入，留存层不做任何取舍判断
ImpactComputer = Callable[[list[dict[str, Any]], list[dict[str, Any]], int], list[Impact]]


class RevisionStore:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        self._init_schema()
        self.recover()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(incidents)")}
            if "plan_version" not in columns:
                conn.execute("ALTER TABLE incidents ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 1")

    def recover(self) -> None:
        """崩溃恢复：应用写入在单个事务内完成，正常不会留下中间态；
        防御性把非终态修订标记为失败，保证恢复后不存在半套占用被当成有效占用。"""
        with self.connect() as conn:
            conn.execute(
                "UPDATE sea_revisions SET status=? WHERE status NOT IN (?,?,?)",
                (REVISION_FAILED, REVISION_REGISTERED, REVISION_APPLIED, REVISION_FAILED),
            )

    def _audit(self, conn: sqlite3.Connection, incident_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(incident_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (incident_id, actor, action, json_dump(details), utcnow()),
        )

    def get_revision(self, revision_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sea_revisions WHERE id=?", (revision_id,)).fetchone()
        return dict(row) if row else None

    def planning_state(self, incident_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self.connect() as conn:
            return self._planning_state(conn, incident_id)

    def _planning_state(self, conn: sqlite3.Connection, incident_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        areas = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM search_areas WHERE incident_id=? ORDER BY priority,id", (incident_id,)
            ).fetchall()
        ]
        assets = []
        for row in conn.execute("SELECT * FROM assets ORDER BY id").fetchall():
            asset = dict(row)
            asset["capabilities"] = json.loads(asset["capabilities"])
            assets.append(asset)
        return areas, assets

    def insert_revision(self, *, incident_id: int, client_token: str, sea_state: int,
                        drift_direction: float, drift_speed_kn: float, effective_at: str,
                        note: str, base_plan_version: int, created_by: str) -> tuple[dict[str, Any], bool]:
        """登记修订；client_token 唯一，重复登记返回已有记录，保证重试幂等。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM sea_revisions WHERE client_token=?", (client_token,)).fetchone()
            if existing:
                return dict(existing), True
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if incident is None:
                raise DomainError("事件不存在", 404)
            if incident["status"] not in ACTIVE_INCIDENT_STATUSES:
                raise DomainError("事件当前不可登记海况修订", 409)
            revision_no = conn.execute(
                "SELECT COALESCE(MAX(revision_no),0)+1 AS n FROM sea_revisions WHERE incident_id=?",
                (incident_id,),
            ).fetchone()["n"]
            now = utcnow()
            cur = conn.execute(
                """INSERT INTO sea_revisions(incident_id,revision_no,client_token,sea_state,drift_direction,
                   drift_speed_kn,effective_at,note,base_plan_version,status,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (incident_id, revision_no, client_token, sea_state, drift_direction, drift_speed_kn,
                 effective_at, note, base_plan_version, REVISION_REGISTERED, created_by, now),
            )
            self._audit(conn, incident_id, created_by, "revision.registered",
                        {"revision_no": revision_no, "sea_state": sea_state, "effective_at": effective_at})
            row = conn.execute("SELECT * FROM sea_revisions WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row), False

    def apply_revision(self, revision_id: int, actor: str, compute_impacts: ImpactComputer) -> dict[str, Any]:
        """单个事务内完成版本校验、影响重算和全部占用写入；任何一步失败整体回滚。

        重试同一修订时直接返回已留存的完整结果，不会重复释放或重复占用。
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM sea_revisions WHERE id=?", (revision_id,)).fetchone()
            if row is None:
                raise DomainError("修订不存在", 404)
            revision = dict(row)
            if revision["status"] == REVISION_APPLIED:
                stored = json.loads(revision["result"])
                stored["idempotent"] = True
                return stored
            if revision["status"] != REVISION_REGISTERED:
                raise DomainError("修订已失效，请重新登记", 409)
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (revision["incident_id"],)).fetchone()
            if incident is None:
                raise DomainError("事件不存在", 404)
            if incident["status"] not in ACTIVE_INCIDENT_STATUSES:
                raise DomainError("事件当前不可应用修订", 409)
            if incident["plan_version"] != revision["base_plan_version"]:
                raise DomainError("版本冲突：搜救计划已被他人修订，请基于最新计划重新登记", 409)
            areas, assets = self._planning_state(conn, revision["incident_id"])
            impacts = compute_impacts(areas, assets, revision["sea_state"])
            now = utcnow()
            applied = []
            for impact in impacts:
                reassignment_id = self._apply_impact(conn, revision, impact, now)
                applied.append({**impact.to_dict(), "reassignment_id": reassignment_id})
            new_plan_version = incident["plan_version"] + 1
            conn.execute(
                """UPDATE incidents SET sea_state=?,drift_direction=?,drift_speed_kn=?,
                   plan_version=?,version=version+1,updated_at=? WHERE id=?""",
                (revision["sea_state"], revision["drift_direction"], revision["drift_speed_kn"],
                 new_plan_version, now, revision["incident_id"]),
            )
            gaps = [item for item in applied if item["action"] in GAP_ACTIONS]
            result = {
                "idempotent": False,
                "revision_id": revision["id"],
                "revision_no": revision["revision_no"],
                "incident_id": revision["incident_id"],
                "plan_version": new_plan_version,
                "impacts": applied,
                "gaps": gaps,
                "applied_at": now,
            }
            stored = {key: value for key, value in result.items() if key != "idempotent"}
            conn.execute(
                "UPDATE sea_revisions SET status=?,applied_at=?,result=? WHERE id=?",
                (REVISION_APPLIED, now, json_dump(stored), revision_id),
            )
            self._audit(conn, revision["incident_id"], actor, "revision.applied",
                        {"revision_no": revision["revision_no"], "sea_state": revision["sea_state"],
                         "impacts": len(applied), "gaps": len(gaps)})
            return result

    def _apply_impact(self, conn: sqlite3.Connection, revision: dict[str, Any], impact: Impact, now: str) -> int | None:
        """写入单个区域的处置结果；异常时由外层事务整体回滚，不留半套占用。"""
        reassignment_id = None
        if impact.action in (ACTION_RELEASED_REPLANNED, ACTION_RELEASED_UNASSIGNED):
            conn.execute(
                "UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?",
                (now, impact.original_asset_id),
            )
        if impact.action == ACTION_RELEASED_REPLANNED:
            changed = conn.execute(
                "UPDATE assets SET status='assigned',version=version+1,updated_at=? WHERE id=? AND status='available'",
                (now, impact.replacement_asset_id),
            )
            if changed.rowcount != 1:
                raise DomainError("重排资源已被占用", 409)
            conn.execute(
                "UPDATE search_areas SET assigned_asset_id=?,status='assigned',version=version+1,updated_at=? WHERE id=?",
                (impact.replacement_asset_id, now, impact.area_id),
            )
        elif impact.action == ACTION_RELEASED_UNASSIGNED:
            conn.execute(
                "UPDATE search_areas SET assigned_asset_id=NULL,status='planned',version=version+1,updated_at=? WHERE id=?",
                (now, impact.area_id),
            )
        elif impact.action == ACTION_REASSIGN_PENDING:
            # 接手船立即预留，防止同一艘船再接下冲突任务；区域在确认前仍归原船负责
            changed = conn.execute(
                "UPDATE assets SET status='assigned',version=version+1,updated_at=? WHERE id=? AND status='available'",
                (now, impact.replacement_asset_id),
            )
            if changed.rowcount != 1:
                raise DomainError("接手资源已被占用", 409)
            cur = conn.execute(
                """INSERT INTO reassignments(revision_id,incident_id,area_id,original_asset_id,replacement_asset_id,status,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (revision["id"], revision["incident_id"], impact.area_id,
                 impact.original_asset_id, impact.replacement_asset_id, REASSIGN_PENDING, now),
            )
            reassignment_id = int(cur.lastrowid)
            self._audit(conn, revision["incident_id"], revision["created_by"], "reassignment.registered",
                        {"area_id": impact.area_id, "original_asset_id": impact.original_asset_id,
                         "replacement_asset_id": impact.replacement_asset_id})
        elif impact.action == ACTION_REASSIGN_GAP:
            cur = conn.execute(
                """INSERT INTO reassignments(revision_id,incident_id,area_id,original_asset_id,replacement_asset_id,status,created_at)
                   VALUES(?,?,?,?,NULL,?,?)""",
                (revision["id"], revision["incident_id"], impact.area_id, impact.original_asset_id, REASSIGN_GAP, now),
            )
            reassignment_id = int(cur.lastrowid)
            self._audit(conn, revision["incident_id"], revision["created_by"], "reassignment.gap",
                        {"area_id": impact.area_id, "original_asset_id": impact.original_asset_id})
        conn.execute(
            """INSERT INTO revision_impacts(revision_id,incident_id,area_id,action,original_asset_id,replacement_asset_id,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (revision["id"], revision["incident_id"], impact.area_id, impact.action,
             impact.original_asset_id, impact.replacement_asset_id, now),
        )
        return reassignment_id

    def confirm_takeover(self, reassignment_id: int, actor: str) -> dict[str, Any]:
        """接手确认：确认前原船负责；确认后原船释放、区域改挂接手船。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM reassignments WHERE id=?", (reassignment_id,)).fetchone()
            if row is None:
                raise DomainError("改派单不存在", 404)
            reassignment = dict(row)
            if reassignment["status"] == REASSIGN_CONFIRMED:
                return {"idempotent": True, "reassignment": reassignment}
            if reassignment["status"] != REASSIGN_PENDING:
                raise DomainError("改派当前不能确认接手", 409)
            now = utcnow()
            conn.execute(
                "UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?",
                (now, reassignment["original_asset_id"]),
            )
            conn.execute(
                "UPDATE search_areas SET assigned_asset_id=?,version=version+1,updated_at=? WHERE id=?",
                (reassignment["replacement_asset_id"], now, reassignment["area_id"]),
            )
            conn.execute(
                "UPDATE reassignments SET status=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                (REASSIGN_CONFIRMED, actor, now, reassignment_id),
            )
            self._audit(conn, reassignment["incident_id"], actor, "reassignment.confirmed",
                        {"reassignment_id": reassignment_id, "area_id": reassignment["area_id"],
                         "original_asset_id": reassignment["original_asset_id"],
                         "replacement_asset_id": reassignment["replacement_asset_id"]})
            updated = conn.execute("SELECT * FROM reassignments WHERE id=?", (reassignment_id,)).fetchone()
            return {"idempotent": False, "reassignment": dict(updated)}

    def revision_view(self, incident_id: int | None = None) -> dict[str, Any]:
        """页面数据：历次修订、每条处置的原船/接手船，以及当前仍未闭合的缺口。"""
        with self.connect() as conn:
            if incident_id is None:
                rev_rows = conn.execute("SELECT * FROM sea_revisions ORDER BY incident_id,revision_no").fetchall()
            else:
                rev_rows = conn.execute(
                    "SELECT * FROM sea_revisions WHERE incident_id=? ORDER BY revision_no", (incident_id,)
                ).fetchall()
            impacts = [
                dict(row)
                for row in conn.execute(
                    """SELECT ri.*, sa.code AS area_code,
                              ao.name AS original_asset_name, ar.name AS replacement_asset_name
                       FROM revision_impacts ri
                       JOIN search_areas sa ON sa.id=ri.area_id
                       LEFT JOIN assets ao ON ao.id=ri.original_asset_id
                       LEFT JOIN assets ar ON ar.id=ri.replacement_asset_id
                       ORDER BY ri.id"""
                ).fetchall()
            ]
            reassigns = [
                dict(row)
                for row in conn.execute(
                    """SELECT r.*, sa.code AS area_code,
                              ao.name AS original_asset_name, ar.name AS replacement_asset_name
                       FROM reassignments r
                       JOIN search_areas sa ON sa.id=r.area_id
                       LEFT JOIN assets ao ON ao.id=r.original_asset_id
                       LEFT JOIN assets ar ON ar.id=r.replacement_asset_id
                       ORDER BY r.id"""
                ).fetchall()
            ]
            open_area_ids = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM search_areas WHERE status='planned' AND assigned_asset_id IS NULL"
                ).fetchall()
            }
        revisions = []
        revision_no = {}
        for row in rev_rows:
            revision = dict(row)
            revision.pop("result", None)
            revision["impacts"] = []
            revision["reassignments"] = []
            revisions.append(revision)
            revision_no[revision["id"]] = revision["revision_no"]
        by_revision = {revision["id"]: revision for revision in revisions}
        if incident_id is not None:
            impacts = [item for item in impacts if item["incident_id"] == incident_id]
            reassigns = [item for item in reassigns if item["incident_id"] == incident_id]
        reassignment_by_area = {(r["revision_id"], r["area_id"]): r for r in reassigns}
        for impact in impacts:
            impact["revision_no"] = revision_no.get(impact["revision_id"])
            reassignment = reassignment_by_area.get((impact["revision_id"], impact["area_id"]))
            impact["reassignment_status"] = reassignment["status"] if reassignment else None
            if impact["revision_id"] in by_revision:
                by_revision[impact["revision_id"]]["impacts"].append(impact)
        gaps = []
        for reassignment in reassigns:
            reassignment["revision_no"] = revision_no.get(reassignment["revision_id"])
            if reassignment["revision_id"] in by_revision:
                by_revision[reassignment["revision_id"]]["reassignments"].append(reassignment)
            if reassignment["status"] == REASSIGN_GAP:
                gaps.append({
                    "kind": "takeover_gap",
                    "incident_id": reassignment["incident_id"],
                    "revision_no": reassignment["revision_no"],
                    "area_id": reassignment["area_id"],
                    "area_code": reassignment["area_code"],
                    "original_asset_name": reassignment["original_asset_name"],
                })
        for impact in impacts:
            if impact["action"] == ACTION_RELEASED_UNASSIGNED and impact["area_id"] in open_area_ids:
                gaps.append({
                    "kind": "unassigned_gap",
                    "incident_id": impact["incident_id"],
                    "revision_no": impact["revision_no"],
                    "area_id": impact["area_id"],
                    "area_code": impact["area_code"],
                    "original_asset_name": impact["original_asset_name"],
                })
        return {"revisions": revisions, "gaps": gaps}
