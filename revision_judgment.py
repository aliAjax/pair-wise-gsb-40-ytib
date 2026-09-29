"""海况修订领域判断层。

只包含纯计算：漂移位置、区域影响评估、接手船匹配。
不访问数据库，不产生副作用，可独立测试。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def drift_hours(area_created_at: str, effective_at: str) -> float:
    """从区域建立到生效时刻之间的漂移小时数（不早于区域建立）。"""
    try:
        hours = (parse_ts(effective_at) - parse_ts(area_created_at)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        hours = 0.0
    return max(0.0, hours)


def drift_center(lat: float, lon: float, direction_deg: float, speed_kn: float, hours: float) -> tuple[float, float]:
    """沿漂移方向（去向方位角，0=北，90=东）推算漂移后的中心。"""
    distance_km = speed_kn * hours * 1.852
    dlat = distance_km * math.cos(math.radians(direction_deg)) / 111.32
    dlon = distance_km * math.sin(math.radians(direction_deg)) / (111.32 * max(0.1, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


def area_drift_center(area: dict[str, Any], drift_direction: float,
                      drift_speed_kn: float, effective_at: str) -> tuple[float, float]:
    hours = drift_hours(area["created_at"], effective_at)
    return drift_center(area["center_lat"], area["center_lon"], drift_direction, drift_speed_kn, hours)


def evaluate_area(area: dict[str, Any], asset: dict[str, Any], sea_state: int,
                  drift_direction: float, drift_speed_kn: float,
                  effective_at: str) -> dict[str, Any]:
    """判断某区域在新海况/漂移下是否受影响。

    受影响原因：
    - sea_state：新海况超出资源可承受等级
    - out_of_range：漂移后区域中心超出资源航程
    """
    new_lat, new_lon = area_drift_center(area, drift_direction, drift_speed_kn, effective_at)
    reasons: list[str] = []
    if sea_state > asset["max_sea_state"]:
        reasons.append("sea_state")
    distance = haversine_km(asset["latitude"], asset["longitude"], new_lat, new_lon)
    if distance > asset["range_km"]:
        reasons.append("out_of_range")
    return {
        "affected": bool(reasons),
        "reasons": reasons,
        "new_center": (new_lat, new_lon),
        "distance_km": round(distance, 3),
    }


def pick_replacement(assets: list[dict[str, Any]], area_kind: str, sea_state: int,
                     new_center: tuple[float, float],
                     exclude_asset_ids: set[int] | None = None) -> dict[str, Any] | None:
    """在可用资源中挑选接手船：能力匹配、海况可承受、航程可达新中心，取最近者。"""
    exclude = exclude_asset_ids or set()
    candidates: list[tuple[float, dict[str, Any]]] = []
    for asset in assets:
        if asset["id"] in exclude:
            continue
        if asset["status"] != "available":
            continue
        if sea_state > asset["max_sea_state"]:
            continue
        try:
            caps = json.loads(asset["capabilities"])
        except (TypeError, ValueError):
            continue
        if area_kind not in caps:
            continue
        distance = haversine_km(asset["latitude"], asset["longitude"], new_center[0], new_center[1])
        if distance > asset["range_km"]:
            continue
        candidates.append((distance, asset))
    candidates.sort(key=lambda item: (item[0], item[1]["id"]))
    return candidates[0][1] if candidates else None
