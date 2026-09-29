"""应用服务层：海况修订的编排、校验与权限；判断交给 planning，写入交给 store。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from .common import DomainError, clean_actor, require_role
from .models import REVISION_APPLIED
from .planning import plan_impacts
from .store import RevisionStore


def _parse_effective_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError("生效时刻不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError("生效时刻格式无效，应为 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


class RevisionService:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.store = RevisionStore(db_path)

    def register(self, actor: str, role: str, incident_id: Any = None, sea_state: Any = None,
                 drift_direction: Any = 0.0, drift_speed_kn: Any = 0.0, effective_at: Any = "",
                 base_plan_version: Any = None, client_token: Any = "", note: Any = "") -> dict[str, Any]:
        """登记新海况、漂移参数和生效时刻；client_token 保证失败重试幂等。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "登记海况修订")
        try:
            incident_id = int(incident_id)
            sea_state = int(sea_state)
            drift_direction = float(drift_direction)
            drift_speed_kn = float(drift_speed_kn)
            base_plan_version = int(base_plan_version)
        except (TypeError, ValueError) as exc:
            raise DomainError("修订参数必须是数值") from exc
        if not 0 <= sea_state <= 9:
            raise DomainError("海况等级无效")
        if drift_speed_kn < 0:
            raise DomainError("漂移速度无效")
        if base_plan_version < 1:
            raise DomainError("计划版本无效")
        token = str(client_token or "").strip()
        if not token:
            raise DomainError("缺少幂等编号 client_token")
        revision, idempotent = self.store.insert_revision(
            incident_id=incident_id,
            client_token=token,
            sea_state=sea_state,
            drift_direction=drift_direction,
            drift_speed_kn=drift_speed_kn,
            effective_at=_parse_effective_at(effective_at),
            note=str(note or "").strip(),
            base_plan_version=base_plan_version,
            created_by=actor,
        )
        return {"idempotent": idempotent, "revision": revision}

    def preview(self, revision_id: int) -> dict[str, Any]:
        """确认前先看受影响区域；只读计算，不落库。已应用的修订返回留存结果。"""
        revision = self.store.get_revision(int(revision_id))
        if revision is None:
            raise DomainError("修订不存在", 404)
        public = dict(revision)
        result = public.pop("result", None)
        if revision["status"] == REVISION_APPLIED:
            stored = json.loads(result)
            return {"revision": public, "applied": True, "impacts": stored["impacts"], "gaps": stored["gaps"]}
        areas, assets = self.store.planning_state(revision["incident_id"])
        names = {asset["id"]: asset["name"] for asset in assets}
        impacts = [
            {
                **impact.to_dict(),
                "original_asset_name": names.get(impact.original_asset_id),
                "replacement_asset_name": names.get(impact.replacement_asset_id),
            }
            for impact in plan_impacts(areas, assets, revision["sea_state"])
        ]
        return {"revision": public, "applied": False, "impacts": impacts}

    def confirm(self, actor: str, role: str, revision_id: Any = None) -> dict[str, Any]:
        """确认应用：版本冲突、影响重算和占用写入在 store 的单个事务内完成。"""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "应用海况修订")
        return self.store.apply_revision(int(revision_id), actor, plan_impacts)

    def confirm_takeover(self, actor: str, role: str, reassignment_id: Any = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "operator", "field"}, "确认接手")
        return self.store.confirm_takeover(int(reassignment_id), actor)

    def view(self, incident_id: int | None = None) -> dict[str, Any]:
        return self.store.revision_view(incident_id)
