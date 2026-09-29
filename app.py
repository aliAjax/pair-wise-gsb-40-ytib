"""Maritime search-and-rescue coordination service."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from revision_judgment import (
    area_drift_center,
    evaluate_area,
    haversine_km,
    pick_replacement,
)
from revision_store import init_schema as init_revision_schema
from revision_store import (
    create_revision,
    get_assignment,
    get_revision,
    insert_assignment,
    list_assignments,
    list_revisions,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "maritime_sar.db"
ACTIVE_INCIDENT = {"reported", "coordinating", "recovering"}
CLOSED_INCIDENT = {"closed", "cancelled", "duplicate"}
# 区域状态：planned 未分配 / assigned 未出动 / active 已出动 / gap 缺口 / completed,abandoned 已结束
OPEN_AREA = {"planned", "assigned", "active", "gap"}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def validate_position(lat: Any, lon: Any) -> tuple[float, float]:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError) as exc:
        raise DomainError("经纬度必须是数值") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise DomainError("经纬度超出有效范围")
    return lat, lon


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class MaritimeSARService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    vessel_name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    uncertainty_km REAL NOT NULL,
                    drift_direction REAL NOT NULL DEFAULT 0,
                    drift_speed_kn REAL NOT NULL DEFAULT 0,
                    sea_state INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reported',
                    lead_org TEXT NOT NULL,
                    duplicate_of INTEGER REFERENCES incidents(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    speed_kn REAL NOT NULL,
                    range_km REAL NOT NULL,
                    max_sea_state INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS search_areas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    code TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 3,
                    status TEXT NOT NULL DEFAULT 'planned',
                    assigned_asset_id INTEGER REFERENCES assets(id),
                    note TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    area_id INTEGER REFERENCES search_areas(id),
                    client_event_id TEXT NOT NULL UNIQUE,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unverified',
                    distance_from_incident_km REAL NOT NULL,
                    reporter TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL,
                    merged_at TEXT
                );
                CREATE TABLE IF NOT EXISTS offline_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_batch_id TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    merged_at TEXT,
                    summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER REFERENCES incidents(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_clues_incident ON clues(incident_id, recorded_at);
                CREATE INDEX IF NOT EXISTS idx_timeline_incident ON timeline(incident_id, id);
                """
            )
            init_revision_schema(conn)

    def _audit(self, conn: sqlite3.Connection, incident_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(incident_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (incident_id, actor, action, json_dump(details), utcnow()),
        )

    def create_incident(self, actor: str, role: str, code: str, vessel_name: str,
                        latitude: float, longitude: float, uncertainty_km: float,
                        sea_state: int, lead_org: str, drift_direction: float = 0,
                        drift_speed_kn: float = 0, description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "operator"}, "创建遇险事件")
        code, vessel_name, lead_org = code.strip(), vessel_name.strip(), lead_org.strip()
        if not code or not vessel_name or not lead_org:
            raise DomainError("事件编号、船名和负责机构不能为空")
        lat, lon = validate_position(latitude, longitude)
        try:
            uncertainty_km = float(uncertainty_km)
            sea_state = int(sea_state)
            drift_direction = float(drift_direction)
            drift_speed_kn = float(drift_speed_kn)
        except (TypeError, ValueError) as exc:
            raise DomainError("不确定半径、海况和漂移参数必须是数值") from exc
        if uncertainty_km <= 0 or uncertainty_km > 1000:
            raise DomainError("不确定半径应在 0 到 1000 公里之间")
        if not 0 <= sea_state <= 9 or drift_speed_kn < 0:
            raise DomainError("海况或漂移速度无效")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT * FROM incidents WHERE vessel_name=? AND status IN ('reported','coordinating','recovering') ORDER BY id DESC",
                (vessel_name,),
            ).fetchall()
            duplicate_of = None
            for row in duplicate:
                if haversine_km(lat, lon, row["latitude"], row["longitude"]) <= max(20.0, uncertainty_km + row["uncertainty_km"]):
                    duplicate_of = row["id"]
                    break
            status = "duplicate" if duplicate_of else "reported"
            try:
                cur = conn.execute(
                    """INSERT INTO incidents(code,vessel_name,description,latitude,longitude,uncertainty_km,
                       drift_direction,drift_speed_kn,sea_state,status,lead_org,duplicate_of,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (code, vessel_name, description.strip(), lat, lon, uncertainty_km, drift_direction, drift_speed_kn,
                     sea_state, status, lead_org, duplicate_of, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("事件编号已存在", 409) from exc
            incident_id = int(cur.lastrowid)
            self._audit(conn, incident_id, actor, "incident.reported", {"duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "incident.duplicate_detected", {"duplicate_incident": code})
            return dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def list_assets(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM assets ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def add_asset(self, actor: str, role: str, name: str, kind: str,
                  capabilities: list[str], latitude: float, longitude: float,
                  speed_kn: float, range_km: float, max_sea_state: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "登记搜救资源")
        lat, lon = validate_position(latitude, longitude)
        name, kind = name.strip(), kind.strip()
        caps = sorted({str(item).strip() for item in capabilities if str(item).strip()})
        if not name or not kind or not caps:
            raise DomainError("资源名称、类型和能力不能为空")
        try:
            speed_kn, range_km, max_sea_state = float(speed_kn), float(range_km), int(max_sea_state)
        except (TypeError, ValueError) as exc:
            raise DomainError("速度和航程参数必须是数值") from exc
        if speed_kn <= 0 or range_km <= 0 or not 0 <= max_sea_state <= 9:
            raise DomainError("速度、航程或适用海况无效")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO assets(name,kind,capabilities,latitude,longitude,speed_kn,range_km,max_sea_state,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (name, kind, json_dump(caps), lat, lon, speed_kn, range_km, max_sea_state, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("资源名称已存在", 409) from exc
            self._audit(conn, None, actor, "asset.registered", {"asset_id": cur.lastrowid, "name": name})
            return dict(conn.execute("SELECT * FROM assets WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_search_area(self, actor: str, role: str, incident_id: int, code: str,
                           kind: str, center_lat: float, center_lon: float,
                           radius_km: float, priority: int = 3, note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "创建搜索区域")
        lat, lon = validate_position(center_lat, center_lon)
        kind, code = kind.strip(), code.strip()
        if not kind or not code:
            raise DomainError("区域类型和编号不能为空")
        try:
            radius_km, priority = float(radius_km), int(priority)
        except (TypeError, ValueError) as exc:
            raise DomainError("半径和优先级必须是数值") from exc
        if radius_km <= 0 or not 1 <= priority <= 5:
            raise DomainError("搜索半径或优先级无效")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("当前事件不能创建搜索区域", 409)
            try:
                cur = conn.execute(
                    """INSERT INTO search_areas(incident_id,code,kind,center_lat,center_lon,radius_km,priority,note,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (incident_id, code, kind, lat, lon, radius_km, priority, note.strip(), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("搜索区域编号已存在", 409) from exc
            self._audit(conn, incident_id, actor, "area.created", {"area_id": cur.lastrowid, "code": code})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (cur.lastrowid,)).fetchone())

    def assign_area(self, actor: str, role: str, area_id: int, asset_id: int,
                    expected_asset_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "分配搜索任务")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
            asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not area or not asset:
                raise DomainError("搜索区域或资源不存在", 404)
            if area["assigned_asset_id"] is not None:
                raise DomainError("搜索区域已经分配", 409)
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (area["incident_id"],)).fetchone()
            if not incident or incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("事件当前不可分配", 409)
            if expected_asset_version is not None and asset["version"] != int(expected_asset_version):
                raise DomainError("资源状态已变化，请刷新后重试", 409)
            if asset["status"] != "available":
                raise DomainError("资源当前不可用", 409)
            if incident["sea_state"] > asset["max_sea_state"]:
                raise DomainError("海况超出资源能力", 409)
            capabilities = json.loads(asset["capabilities"])
            if area["kind"] not in capabilities:
                raise DomainError("资源不具备该搜索区域能力", 409)
            distance = haversine_km(asset["latitude"], asset["longitude"], area["center_lat"], area["center_lon"])
            if distance > asset["range_km"]:
                raise DomainError("搜索区域超出资源航程", 409)
            now = utcnow()
            changed = conn.execute(
                "UPDATE assets SET status='assigned',version=version+1,updated_at=? WHERE id=? AND status='available' AND version=?",
                (now, asset_id, asset["version"]),
            )
            if changed.rowcount != 1:
                raise DomainError("资源已被其他任务占用", 409)
            conn.execute(
                "UPDATE search_areas SET assigned_asset_id=?,status='assigned',version=version+1,updated_at=? WHERE id=?",
                (asset_id, now, area_id),
            )
            self._audit(conn, area["incident_id"], actor, "area.assigned", {"area_id": area_id, "asset_id": asset_id, "distance_km": round(distance, 2)})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone())

    def record_clue(self, actor: str, role: str, incident_id: int, client_event_id: str,
                    latitude: float, longitude: float, confidence: float, source: str,
                    area_id: int | None = None, details: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "operator", "field"}, "记录搜索线索")
        lat, lon = validate_position(latitude, longitude)
        event_id, source = client_event_id.strip(), source.strip()
        if not event_id or not source:
            raise DomainError("事件幂等编号和线索来源不能为空")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise DomainError("线索置信度必须是数值") from exc
        if not 0 <= confidence <= 1:
            raise DomainError("置信度应在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM clues WHERE client_event_id=?", (event_id,)).fetchone()
            if existing:
                return dict(existing)
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["status"] in CLOSED_INCIDENT:
                raise DomainError("已结束事件不能新增线索", 409)
            if area_id is not None:
                area = conn.execute("SELECT * FROM search_areas WHERE id=? AND incident_id=?", (area_id, incident_id)).fetchone()
                if not area:
                    raise DomainError("搜索区域不属于该事件", 409)
            distance = haversine_km(incident["latitude"], incident["longitude"], lat, lon)
            status = "unverified" if distance <= incident["uncertainty_km"] * 3 else "invalid"
            cur = conn.execute(
                """INSERT INTO clues(incident_id,area_id,client_event_id,latitude,longitude,confidence,source,status,
                   distance_from_incident_km,reporter,details,recorded_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (incident_id, area_id, event_id, lat, lon, confidence, source, status, distance, actor, details.strip(), utcnow()),
            )
            self._audit(conn, incident_id, actor, "clue.recorded", {"clue_id": cur.lastrowid, "status": status, "event_id": event_id})
            return dict(conn.execute("SELECT * FROM clues WHERE id=?", (cur.lastrowid,)).fetchone())

    def verify_clue(self, actor: str, role: str, clue_id: int, status: str,
                    expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "analyst"}, "核验线索")
        if status not in {"verified", "rejected", "unverified"}:
            raise DomainError("线索状态无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clue = conn.execute("SELECT * FROM clues WHERE id=?", (clue_id,)).fetchone()
            if not clue:
                raise DomainError("线索不存在", 404)
            if status == "verified" and clue["status"] == "invalid" and role != "coordinator":
                raise DomainError("异常位置线索只能由协调员确认", 403)
            conn.execute("UPDATE clues SET status=?,merged_at=? WHERE id=?", (status, utcnow(), clue_id))
            self._audit(conn, clue["incident_id"], actor, "clue.reviewed", {"clue_id": clue_id, "status": status})
            return dict(conn.execute("SELECT * FROM clues WHERE id=?", (clue_id,)).fetchone())

    def withdraw_asset(self, actor: str, role: str, asset_id: int, reason: str,
                       expected_asset_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "撤回资源")
        if not reason.strip():
            raise DomainError("撤回原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not asset:
                raise DomainError("资源不存在", 404)
            if asset["version"] != int(expected_asset_version):
                raise DomainError("资源状态已变化，请刷新后重试", 409)
            if asset["status"] == "available":
                raise DomainError("资源当前未分配", 409)
            now = utcnow()
            areas = conn.execute("SELECT id,incident_id FROM search_areas WHERE assigned_asset_id=? AND status IN ('assigned','active')", (asset_id,)).fetchall()
            for area in areas:
                conn.execute("UPDATE search_areas SET assigned_asset_id=NULL,status='planned',version=version+1,updated_at=? WHERE id=?", (now, area["id"]))
                self._audit(conn, area["incident_id"], actor, "area.unassigned", {"area_id": area["id"], "reason": reason.strip()})
            conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?", (now, asset_id))
            self._audit(conn, None, actor, "asset.withdrawn", {"asset_id": asset_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone())

    def transfer_incident(self, actor: str, role: str, incident_id: int, new_org: str,
                          expected_version: int, note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "移交事件")
        new_org = new_org.strip()
        if not new_org:
            raise DomainError("接收机构不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["status"] in CLOSED_INCIDENT:
                raise DomainError("已结束事件不能移交", 409)
            if incident["version"] != int(expected_version):
                raise DomainError("事件已变化，请刷新后重试", 409)
            conn.execute(
                "UPDATE incidents SET lead_org=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (new_org, utcnow(), incident_id, expected_version),
            )
            self._audit(conn, incident_id, actor, "incident.transferred", {"from": incident["lead_org"], "to": new_org, "note": note.strip()})
            return dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def complete_area(self, actor: str, role: str, area_id: int, outcome: str,
                      expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "结束搜索区域")
        if outcome not in {"completed", "abandoned"}:
            raise DomainError("区域结束结论无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
            if not area:
                raise DomainError("搜索区域不存在", 404)
            if area["status"] in {"completed", "abandoned"}:
                raise DomainError("搜索区域已经结束", 409)
            if expected_version is not None and area["version"] != int(expected_version):
                raise DomainError("搜索区域已变化，请刷新后重试", 409)
            if area["assigned_asset_id"] is not None:
                conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?", (utcnow(), area["assigned_asset_id"]))
            conn.execute("UPDATE search_areas SET status=?,assigned_asset_id=NULL,version=version+1,updated_at=? WHERE id=?", (outcome, utcnow(), area_id))
            self._audit(conn, area["incident_id"], actor, "area." + outcome, {"area_id": area_id})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone())

    def close_incident(self, actor: str, role: str, incident_id: int, outcome: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "结束事件")
        if outcome not in {"resolved", "cancelled", "false_alarm"}:
            raise DomainError("结束结论无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["version"] != int(expected_version):
                raise DomainError("事件已变化，请刷新后重试", 409)
            active_area = conn.execute(
                "SELECT COUNT(*) AS c FROM search_areas WHERE incident_id=? AND status IN ('planned','assigned','active')",
                (incident_id,),
            ).fetchone()["c"]
            if active_area and outcome != "false_alarm":
                raise DomainError("仍有未结束搜索区域，不能关闭事件", 409)
            status = "closed" if outcome == "resolved" else "cancelled"
            conn.execute("UPDATE incidents SET status=?,version=version+1,updated_at=? WHERE id=?", (status, utcnow(), incident_id))
            self._audit(conn, incident_id, actor, "incident.closed", {"outcome": outcome})
            return dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def activate_area(self, actor: str, role: str, area_id: int,
                      expected_version: int | None = None) -> dict[str, Any]:
        """出动：已分配未出动（assigned）转为已出动（active），资源仍由该任务占用。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "出动搜索任务")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
            if not area:
                raise DomainError("搜索区域不存在", 404)
            if area["status"] != "assigned" or area["assigned_asset_id"] is None:
                raise DomainError("只有已分配未出动的区域可以出动", 409)
            if expected_version is not None and area["version"] != int(expected_version):
                raise DomainError("搜索区域已变化，请刷新后重试", 409)
            conn.execute(
                "UPDATE search_areas SET status='active',version=version+1,updated_at=? WHERE id=?",
                (utcnow(), area_id),
            )
            self._audit(conn, area["incident_id"], actor, "area.activated", {"area_id": area_id})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone())

    def register_sea_revision(self, actor: str, role: str, incident_id: int, sea_state: int,
                              drift_direction: float, drift_speed_kn: float,
                              effective_at: str) -> dict[str, Any]:
        """协调员登记新海况、漂移参数与生效时刻，生成待应用修订。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "登记海况修订")
        try:
            incident_id = int(incident_id)
            sea_state = int(sea_state)
            drift_direction = float(drift_direction)
            drift_speed_kn = float(drift_speed_kn)
        except (TypeError, ValueError) as exc:
            raise DomainError("事件、海况和漂移参数必须是数值") from exc
        if not 0 <= sea_state <= 9:
            raise DomainError("海况等级应在 0 到 9 之间")
        if not 0 <= drift_direction < 360:
            raise DomainError("漂移方向应在 0 到 360 之间")
        if drift_speed_kn < 0:
            raise DomainError("漂移速度不能为负")
        effective_at = str(effective_at).strip()
        if not effective_at:
            raise DomainError("生效时刻不能为空")
        try:
            datetime.fromisoformat(effective_at)
        except ValueError as exc:
            raise DomainError("生效时刻格式无效") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("当前事件不能登记海况修订", 409)
            revision = create_revision(
                conn, incident_id=incident_id, sea_state=sea_state, drift_direction=drift_direction,
                drift_speed_kn=drift_speed_kn, effective_at=effective_at, actor=actor, now=utcnow(),
            )
            self._audit(conn, incident_id, actor, "sea_revision.registered",
                        {"revision_id": revision["id"], "sea_state": sea_state,
                         "drift_direction": drift_direction, "drift_speed_kn": drift_speed_kn,
                         "effective_at": effective_at})
            return revision

    def _load_revision_context(self, conn, revision_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        revision = get_revision(conn, revision_id)
        if not revision:
            raise DomainError("海况修订不存在", 404)
        incident = conn.execute("SELECT * FROM incidents WHERE id=?", (revision["incident_id"],)).fetchone()
        if not incident:
            raise DomainError("事件不存在", 404)
        return revision, dict(incident)

    def _plan_revision(self, conn, revision: dict[str, Any], incident: dict[str, Any]) -> dict[str, Any]:
        """试算修订影响：哪些区域受影响、原船、接手船或缺口。不落库。"""
        areas = [dict(r) for r in conn.execute(
            "SELECT * FROM search_areas WHERE incident_id=? AND status IN ('planned','assigned','active','gap') ORDER BY priority,id",
            (incident["id"],),
        ).fetchall()]
        sim_assets = [dict(r) for r in conn.execute("SELECT * FROM assets ORDER BY id").fetchall()]
        affected: list[dict[str, Any]] = []
        unaffected: list[dict[str, Any]] = []
        summary = {"affected": 0, "reassigned": 0, "reassigning": 0, "gaps": 0, "unaffected": 0}
        for area in areas:
            original = next((a for a in sim_assets if a["id"] == area["assigned_asset_id"]), None) if area["assigned_asset_id"] else None
            if original is None:
                unaffected.append(area)
                summary["unaffected"] += 1
                continue
            impact = evaluate_area(area, original, revision["sea_state"], revision["drift_direction"],
                                   revision["drift_speed_kn"], revision["effective_at"])
            if not impact["affected"]:
                unaffected.append(area)
                summary["unaffected"] += 1
                continue
            summary["affected"] += 1
            new_lat, new_lon = impact["new_center"]
            if area["status"] == "assigned":
                # 未出动：释放后重排，原船先回到可用池参与匹配
                for asset in sim_assets:
                    if asset["id"] == original["id"]:
                        asset["status"] = "available"
                replacement = pick_replacement(sim_assets, area["kind"], revision["sea_state"],
                                               impact["new_center"], exclude_asset_ids=set())
                disposition = "reassigned" if replacement else "gap"
                if replacement:
                    for asset in sim_assets:
                        if asset["id"] == replacement["id"]:
                            asset["status"] = "assigned"
                    summary["reassigned"] += 1
                else:
                    summary["gaps"] += 1
            elif area["status"] == "active":
                # 已出动：登记改派，接手确认前原船仍负责
                replacement = pick_replacement(sim_assets, area["kind"], revision["sea_state"],
                                               impact["new_center"], exclude_asset_ids={original["id"]})
                disposition = "reassigning" if replacement else "gap"
                if replacement:
                    for asset in sim_assets:
                        if asset["id"] == replacement["id"]:
                            asset["status"] = "reserved"
                    summary["reassigning"] += 1
                else:
                    summary["gaps"] += 1
            else:
                replacement = None
                disposition = "gap"
                summary["gaps"] += 1
            affected.append({
                "area": area,
                "original_asset": original,
                "reasons": impact["reasons"],
                "new_center": [new_lat, new_lon],
                "distance_km": impact["distance_km"],
                "replacement_asset": replacement,
                "disposition": disposition,
            })
        return {"affected": affected, "unaffected": unaffected, "summary": summary}

    def preview_sea_revision(self, actor: str, role: str, revision_id: int) -> dict[str, Any]:
        """应用前先看受影响区域与重排/缺口结果。"""
        with self.connect() as conn:
            revision, incident = self._load_revision_context(conn, revision_id)
            plan = self._plan_revision(conn, revision, incident)
            return {"revision": revision, "incident": incident, **plan}

    def _stored_apply_result(self, conn, revision: dict[str, Any]) -> dict[str, Any]:
        assignments = list_assignments(conn, revision["id"])
        summary = json.loads(revision["impact_summary"]) if revision["impact_summary"] else {}
        return {"revision": revision, "summary": summary, "assignments": assignments, "idempotent": True}

    def apply_sea_revision(self, actor: str, role: str, revision_id: int, expected_version: int) -> dict[str, Any]:
        """确认应用修订。

        - 乐观锁：事件版本与 expected_version 不一致时返回 409，后到的协调员必须刷新重试；
        - 幂等：已应用的修订重放返回完整结果，不重复改动；
        - 原子：释放旧安排、占用新资源、登记改派、缺口与落库在同一事务，
          写入失败整体回滚，重试只会得到完整结果，崩溃后不留半套占用。
        """
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "应用海况修订")
        try:
            expected_version = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise DomainError("事件版本必须是数值") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision, incident = self._load_revision_context(conn, revision_id)
            if revision["status"] == "applied":
                return self._stored_apply_result(conn, revision)
            if incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("当前事件不能应用海况修订", 409)
            if incident["version"] != expected_version:
                raise DomainError("事件已变化，请刷新后重试", 409)
            # 再次按版本锁定事件行，杜绝并发修订
            locked = conn.execute(
                "SELECT id FROM incidents WHERE id=? AND version=?", (incident["id"], expected_version)
            ).fetchone()
            if not locked:
                raise DomainError("事件已变化，请刷新后重试", 409)
            now = utcnow()
            conn.execute(
                "UPDATE incidents SET sea_state=?,drift_direction=?,drift_speed_kn=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (revision["sea_state"], revision["drift_direction"], revision["drift_speed_kn"],
                 now, incident["id"], expected_version),
            )
            areas = [dict(r) for r in conn.execute(
                "SELECT * FROM search_areas WHERE incident_id=? AND status IN ('planned','assigned','active','gap') ORDER BY priority,id",
                (incident["id"],),
            ).fetchall()]
            sim_assets = [dict(r) for r in conn.execute("SELECT * FROM assets ORDER BY id").fetchall()]
            assignments: list[dict[str, Any]] = []
            summary = {"affected": 0, "reassigned": 0, "reassigning": 0, "gaps": 0, "repositioned": 0}
            for area in areas:
                original = next((a for a in sim_assets if a["id"] == area["assigned_asset_id"]), None) if area["assigned_asset_id"] else None
                if original is None:
                    new_lat, new_lon = area_drift_center(area, revision["drift_direction"],
                                                         revision["drift_speed_kn"], revision["effective_at"])
                    conn.execute(
                        "UPDATE search_areas SET center_lat=?,center_lon=?,version=version+1,updated_at=? WHERE id=?",
                        (new_lat, new_lon, now, area["id"]),
                    )
                    summary["repositioned"] += 1
                    continue
                impact = evaluate_area(area, original, revision["sea_state"], revision["drift_direction"],
                                       revision["drift_speed_kn"], revision["effective_at"])
                new_lat, new_lon = impact["new_center"]
                if not impact["affected"]:
                    conn.execute(
                        "UPDATE search_areas SET center_lat=?,center_lon=?,version=version+1,updated_at=? WHERE id=?",
                        (new_lat, new_lon, now, area["id"]),
                    )
                    summary["repositioned"] += 1
                    continue
                summary["affected"] += 1
                reason = ",".join(impact["reasons"])
                if area["status"] == "assigned":
                    # 未出动：释放后重排
                    for asset in sim_assets:
                        if asset["id"] == original["id"]:
                            asset["status"] = "available"
                    conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?",
                                 (now, original["id"]))
                    conn.execute(
                        "UPDATE search_areas SET assigned_asset_id=NULL,status='planned',version=version+1,updated_at=? WHERE id=?",
                        (now, area["id"]),
                    )
                    replacement = pick_replacement(sim_assets, area["kind"], revision["sea_state"],
                                                   impact["new_center"], exclude_asset_ids=set())
                    if replacement is None:
                        conn.execute(
                            "UPDATE search_areas SET status='gap',assigned_asset_id=NULL,center_lat=?,center_lon=?,version=version+1,updated_at=? WHERE id=?",
                            (new_lat, new_lon, now, area["id"]),
                        )
                        row = insert_assignment(
                            conn, revision_id=revision["id"], area_id=area["id"],
                            original_asset_id=original["id"], replacement_asset_id=None,
                            new_center_lat=new_lat, new_center_lon=new_lon, reason=reason,
                            status="gap", now=now,
                        )
                        summary["gaps"] += 1
                        self._audit(conn, incident["id"], actor, "sea_revision.gap",
                                    {"revision_id": revision["id"], "area_id": area["id"], "original_asset_id": original["id"]})
                    else:
                        for asset in sim_assets:
                            if asset["id"] == replacement["id"]:
                                asset["status"] = "assigned"
                        conn.execute("UPDATE assets SET status='assigned',version=version+1,updated_at=? WHERE id=?",
                                     (now, replacement["id"]))
                        conn.execute(
                            "UPDATE search_areas SET assigned_asset_id=?,status='assigned',center_lat=?,center_lon=?,version=version+1,updated_at=? WHERE id=?",
                            (replacement["id"], new_lat, new_lon, now, area["id"]),
                        )
                        row = insert_assignment(
                            conn, revision_id=revision["id"], area_id=area["id"],
                            original_asset_id=original["id"], replacement_asset_id=replacement["id"],
                            new_center_lat=new_lat, new_center_lon=new_lon, reason=reason,
                            status="reassigned", now=now,
                        )
                        summary["reassigned"] += 1
                        self._audit(conn, incident["id"], actor, "sea_revision.reassigned",
                                    {"revision_id": revision["id"], "area_id": area["id"],
                                     "original_asset_id": original["id"], "replacement_asset_id": replacement["id"]})
                elif area["status"] == "active":
                    # 已出动：先登记改派，接手确认前原船仍负责
                    replacement = pick_replacement(sim_assets, area["kind"], revision["sea_state"],
                                                   impact["new_center"], exclude_asset_ids={original["id"]})
                    if replacement is None:
                        conn.execute(
                            "UPDATE search_areas SET status='gap',assigned_asset_id=NULL,center_lat=?,center_lon=?,version=version+1,updated_at=? WHERE id=?",
                            (new_lat, new_lon, now, area["id"]),
                        )
                        conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?",
                                     (now, original["id"]))
                        row = insert_assignment(
                            conn, revision_id=revision["id"], area_id=area["id"],
                            original_asset_id=original["id"], replacement_asset_id=None,
                            new_center_lat=new_lat, new_center_lon=new_lon, reason=reason,
                            status="gap", now=now,
                        )
                        summary["gaps"] += 1
                        self._audit(conn, incident["id"], actor, "sea_revision.gap",
                                    {"revision_id": revision["id"], "area_id": area["id"], "original_asset_id": original["id"]})
                    else:
                        for asset in sim_assets:
                            if asset["id"] == replacement["id"]:
                                asset["status"] = "reserved"
                        conn.execute("UPDATE assets SET status='reserved',version=version+1,updated_at=? WHERE id=?",
                                     (now, replacement["id"]))
                        conn.execute(
                            "UPDATE search_areas SET center_lat=?,center_lon=?,version=version+1,updated_at=? WHERE id=?",
                            (new_lat, new_lon, now, area["id"]),
                        )
                        row = insert_assignment(
                            conn, revision_id=revision["id"], area_id=area["id"],
                            original_asset_id=original["id"], replacement_asset_id=replacement["id"],
                            new_center_lat=new_lat, new_center_lon=new_lon, reason=reason,
                            status="reassigning", now=now,
                        )
                        summary["reassigning"] += 1
                        self._audit(conn, incident["id"], actor, "sea_revision.reassigning",
                                    {"revision_id": revision["id"], "area_id": area["id"],
                                     "original_asset_id": original["id"], "replacement_asset_id": replacement["id"]})
                else:
                    # planned/gap 且无资源：仅更新漂移中心
                    conn.execute(
                        "UPDATE search_areas SET center_lat=?,center_lon=?,version=version+1,updated_at=? WHERE id=?",
                        (new_lat, new_lon, now, area["id"]),
                    )
                    row = None
                if row is not None:
                    assignments.append(row)
            conn.execute(
                "UPDATE sea_condition_revisions SET status='applied',applied_at=?,impact_summary=?,version=version+1 WHERE id=?",
                (now, json_dump(summary), revision["id"]),
            )
            self._audit(conn, incident["id"], actor, "sea_revision.applied",
                        {"revision_id": revision["id"], "summary": summary})
            applied = get_revision(conn, revision["id"])
            return {"revision": applied, "summary": summary, "assignments": assignments, "idempotent": False}

    def confirm_revision_handover(self, actor: str, role: str, assignment_id: int) -> dict[str, Any]:
        """接手船确认接手：原船解除责任，区域改由接手船负责。此前原船仍负责。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "field"}, "接手确认")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assignment = get_assignment(conn, assignment_id)
            if not assignment:
                raise DomainError("改派记录不存在", 404)
            if assignment["status"] != "reassigning":
                raise DomainError("该改派记录不是待接手状态", 409)
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (assignment["area_id"],)).fetchone()
            if not area:
                raise DomainError("搜索区域不存在", 404)
            replacement = conn.execute("SELECT * FROM assets WHERE id=?", (assignment["replacement_asset_id"],)).fetchone()
            if not replacement or replacement["status"] != "reserved":
                raise DomainError("接手资源当前不可用，请重新登记改派", 409)
            now = utcnow()
            conn.execute(
                "UPDATE search_areas SET assigned_asset_id=?,version=version+1,updated_at=? WHERE id=?",
                (replacement["id"], now, area["id"]),
            )
            conn.execute("UPDATE assets SET status='assigned',version=version+1,updated_at=? WHERE id=?",
                         (now, replacement["id"]))
            conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?",
                         (now, assignment["original_asset_id"]))
            conn.execute(
                "UPDATE revision_assignments SET status='handed_over',confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=?",
                (actor, now, now, assignment_id),
            )
            self._audit(conn, area["incident_id"], actor, "sea_revision.handover_confirmed",
                        {"assignment_id": assignment_id, "area_id": area["id"],
                         "original_asset_id": assignment["original_asset_id"],
                         "replacement_asset_id": replacement["id"]})
            return get_assignment(conn, assignment_id)

    def list_sea_revisions(self, incident_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return list_revisions(conn, incident_id)

    def list_revision_assignments(self, revision_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            revision = get_revision(conn, revision_id)
            if not revision:
                raise DomainError("海况修订不存在", 404)
            return {"revision": revision, "assignments": list_assignments(conn, revision_id)}

    def merge_offline_batch(self, actor: str, role: str, client_batch_id: str,
                            events: list[dict[str, Any]]) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"operator", "field", "coordinator"}, "合并离线记录")
        batch_id = client_batch_id.strip()
        if not batch_id or not isinstance(events, list):
            raise DomainError("批次编号和事件列表不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM offline_batches WHERE client_batch_id=?", (batch_id,)).fetchone()
            if existing:
                return {"batch_id": batch_id, "idempotent": True, "status": existing["status"], "summary": json.loads(existing["summary"])}
            results = []
            for event in events:
                event_id = str(event.get("client_event_id", "")).strip()
                try:
                    if not event_id:
                        raise DomainError("离线事件缺少 client_event_id")
                    if event.get("type") == "clue":
                        existing_clue = conn.execute("SELECT id FROM clues WHERE client_event_id=?", (event_id,)).fetchone()
                        if existing_clue:
                            results.append({"client_event_id": event_id, "status": "merged", "record_id": existing_clue["id"], "idempotent": True})
                            continue
                        incident_id = int(event["incident_id"])
                        lat, lon = validate_position(event["latitude"], event["longitude"])
                        confidence = float(event["confidence"])
                        if not 0 <= confidence <= 1:
                            raise DomainError("置信度应在 0 到 1 之间")
                        incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
                        if not incident:
                            raise DomainError("事件不存在", 404)
                        if incident["status"] in CLOSED_INCIDENT:
                            raise DomainError("已结束事件不能新增线索", 409)
                        area_id = event.get("area_id")
                        if area_id is not None and not conn.execute(
                            "SELECT 1 FROM search_areas WHERE id=? AND incident_id=?", (area_id, incident_id)
                        ).fetchone():
                            raise DomainError("搜索区域不属于该事件", 409)
                        distance = haversine_km(incident["latitude"], incident["longitude"], lat, lon)
                        status = "unverified" if distance <= incident["uncertainty_km"] * 3 else "invalid"
                        cur = conn.execute(
                            """INSERT INTO clues(incident_id,area_id,client_event_id,latitude,longitude,confidence,source,status,
                               distance_from_incident_km,reporter,details,recorded_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (incident_id, area_id, event_id, lat, lon, confidence,
                             str(event.get("source", "offline")).strip(), status, distance, actor,
                             str(event.get("details", "")).strip(), utcnow()),
                        )
                        self._audit(conn, incident_id, actor, "clue.recorded", {"clue_id": cur.lastrowid, "status": status, "event_id": event_id})
                        results.append({"client_event_id": event_id, "status": "merged", "record_id": cur.lastrowid})
                    elif event.get("type") == "timeline":
                        incident_id = int(event["incident_id"])
                        if not conn.execute("SELECT 1 FROM incidents WHERE id=?", (incident_id,)).fetchone():
                            raise DomainError("事件不存在", 404)
                        self._audit(conn, incident_id, actor, event.get("action", "offline.note"), event.get("details", {}))
                        results.append({"client_event_id": event_id, "status": "merged", "record_id": None})
                    else:
                        raise DomainError("不支持的离线事件类型")
                except (DomainError, KeyError, TypeError, ValueError) as exc:
                    results.append({"client_event_id": event_id, "status": "rejected", "error": str(exc)})
            summary = {"accepted": sum(1 for item in results if item["status"] == "merged"), "rejected": sum(1 for item in results if item["status"] == "rejected"), "events": results}
            now = utcnow()
            conn.execute(
                "INSERT INTO offline_batches(client_batch_id,actor,status,received_at,merged_at,summary) VALUES(?,?,?,?,?,?)",
                (batch_id, actor, "merged", now, now, json_dump(summary)),
            )
            self._audit(conn, None, actor, "offline.batch_merged", {"batch_id": batch_id, **{k: summary[k] for k in ("accepted", "rejected")}})
            return {"batch_id": batch_id, "idempotent": False, "status": "merged", "summary": summary}

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            incidents = [dict(r) for r in conn.execute("SELECT * FROM incidents ORDER BY id DESC").fetchall()]
            areas = [dict(r) for r in conn.execute("SELECT * FROM search_areas ORDER BY priority,id").fetchall()]
            clues = [dict(r) for r in conn.execute("SELECT * FROM clues ORDER BY id DESC LIMIT 200").fetchall()]
            assets = [dict(r) for r in conn.execute("SELECT * FROM assets ORDER BY id").fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 300").fetchall()]
        return {"incidents": incidents, "assets": assets, "search_areas": areas, "clues": clues, "timeline": timeline}

    def incident_timeline(self, incident_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM timeline WHERE incident_id=? ORDER BY id", (incident_id,)).fetchall()
        return [dict(r) for r in rows]

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM incidents").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        incident = self.create_incident("coord-demo", "coordinator", "SAR-2026-001", "远星号", 31.2, 122.5, 15.0, 3, "东海搜救中心", description="演示遇险事件")
        self.add_asset("coord-demo", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 22.0, 180.0, 6)
        self.add_asset("coord-demo", "coordinator", "救助直升机", "aircraft", ["air", "night"], 30.8, 122.1, 180.0, 260.0, 5)
        self.create_search_area("coord-demo", "coordinator", incident["id"], "AREA-A", "surface", 31.2, 122.5, 20.0, 1, "首要搜索区")
        return {"seeded": True, "incident_id": incident["id"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: MaritimeSARService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DomainError("Content-Length 无效") from exc
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise DomainError("JSON 请求体必须是对象")
        return data

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                self._send(200, {"status": "ok", "service": "maritime-sar"})
                return
            if path == "/api/state":
                self._send(200, self.service.state(*self._actor()))
                return
            if path.startswith("/api/incidents/") and path.endswith("/timeline"):
                incident_id = int(path.split("/")[3])
                self._send(200, {"timeline": self.service.incident_timeline(incident_id)})
                return
            if path.startswith("/api/incidents/") and path.endswith("/sea-revisions"):
                incident_id = int(path.split("/")[3])
                self._send(200, {"revisions": self.service.list_sea_revisions(incident_id)})
                return
            if path.startswith("/api/sea-revisions/") and path.endswith("/assignments"):
                revision_id = int(path.split("/")[3])
                self._send(200, self.service.list_revision_assignments(revision_id))
                return
            if path.startswith("/api/sea-revisions/"):
                revision_id = int(path.split("/")[3])
                self._send(200, self.service.preview_sea_revision(*self._actor(), revision_id))
                return
            self._send(404, {"error": "接口不存在"})
        except (DomainError, ValueError) as exc:
            self._send(getattr(exc, "status", 400), {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            data, (actor, role) = self._json(), self._actor()
            if path == "/api/incidents":
                result = self.service.create_incident(actor, role, **data)
            elif path == "/api/assets":
                result = self.service.add_asset(actor, role, **data)
            elif path == "/api/areas":
                result = self.service.create_search_area(actor, role, **data)
            elif path == "/api/assignments":
                result = self.service.assign_area(actor, role, **data)
            elif path == "/api/clues":
                result = self.service.record_clue(actor, role, **data)
            elif path == "/api/clues/verify":
                result = self.service.verify_clue(actor, role, **data)
            elif path == "/api/assets/withdraw":
                result = self.service.withdraw_asset(actor, role, **data)
            elif path == "/api/areas/complete":
                result = self.service.complete_area(actor, role, **data)
            elif path == "/api/incidents/transfer":
                result = self.service.transfer_incident(actor, role, **data)
            elif path == "/api/incidents/close":
                result = self.service.close_incident(actor, role, **data)
            elif path == "/api/offline/batch":
                result = self.service.merge_offline_batch(actor, role, **data)
            elif path == "/api/areas/activate":
                result = self.service.activate_area(actor, role, **data)
            elif path == "/api/sea-revisions":
                result = self.service.register_sea_revision(actor, role, **data)
            elif path.startswith("/api/sea-revisions/") and path.endswith("/apply"):
                revision_id = int(path.split("/")[3])
                result = self.service.apply_sea_revision(actor, role, revision_id, **data)
            elif path.startswith("/api/revision-assignments/") and path.endswith("/confirm"):
                assignment_id = int(path.split("/")[3])
                result = self.service.confirm_revision_handover(actor, role, assignment_id)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: MaritimeSARService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Maritime SAR service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="海上搜救协调服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8206)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = MaritimeSARService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
