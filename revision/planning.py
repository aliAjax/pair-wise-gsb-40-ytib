"""判断层：海况修订的影响计算与资源重排，纯函数，不触碰数据库。"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .geo import haversine_km
from .models import (
    ACTION_REASSIGN_GAP,
    ACTION_REASSIGN_PENDING,
    ACTION_RELEASED_REPLANNED,
    ACTION_RELEASED_UNASSIGNED,
    Impact,
)

DistanceFn = Callable[[float, float, float, float], float]


def choose_replacement(
    assets: Iterable[dict[str, Any]],
    area: dict[str, Any],
    new_sea_state: int,
    excluded_ids: set[int],
    distance_fn: DistanceFn = haversine_km,
) -> dict[str, Any] | None:
    """在可用资源中挑选接手船：能力匹配、扛得住新海况、航程够，取最近且编号最小者。"""
    best: tuple[tuple[float, int], dict[str, Any]] | None = None
    for asset in assets:
        if asset["id"] in excluded_ids or asset["status"] != "available":
            continue
        if asset["max_sea_state"] < new_sea_state:
            continue
        if area["kind"] not in asset["capabilities"]:
            continue
        distance = distance_fn(asset["latitude"], asset["longitude"], area["center_lat"], area["center_lon"])
        if distance > asset["range_km"]:
            continue
        key = (round(distance, 6), asset["id"])
        if best is None or key < best[0]:
            best = (key, asset)
    return best[1] if best else None


def plan_impacts(
    areas: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    new_sea_state: int,
    distance_fn: DistanceFn = haversine_km,
) -> list[Impact]:
    """计算新海况下的受影响区域与处置决定。

    受影响 = 当前负责船只扛不住新海况。未出动的释放后重排，已出动的登记改派；
    同一次修订内已被选中的接手船进入 reserved，避免一艘船接下冲突任务。
    """
    impacts: list[Impact] = []
    reserved: set[int] = set()
    assets_by_id = {asset["id"]: asset for asset in assets}
    for area in sorted(areas, key=lambda item: (item["priority"], item["id"])):
        if area["status"] not in ("assigned", "active"):
            continue
        original = assets_by_id.get(area["assigned_asset_id"] or -1)
        if original is None or original["max_sea_state"] >= new_sea_state:
            continue
        dispatched = area["status"] == "active"
        replacement = choose_replacement(
            assets, area, new_sea_state, reserved | {original["id"]}, distance_fn
        )
        if replacement is not None:
            reserved.add(replacement["id"])
        if dispatched:
            action = ACTION_REASSIGN_PENDING if replacement else ACTION_REASSIGN_GAP
        else:
            action = ACTION_RELEASED_REPLANNED if replacement else ACTION_RELEASED_UNASSIGNED
        impacts.append(
            Impact(
                area_id=area["id"],
                area_code=area["code"],
                action=action,
                dispatched=dispatched,
                original_asset_id=original["id"],
                replacement_asset_id=replacement["id"] if replacement else None,
            )
        )
    return impacts
