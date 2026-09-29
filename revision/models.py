"""数据层：海况修订的领域数据结构与常量，不含判断与存储逻辑。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

ACTIVE_INCIDENT_STATUSES = frozenset({"reported", "coordinating", "recovering"})
CLOSED_INCIDENT_STATUSES = frozenset({"closed", "cancelled", "duplicate"})

# 修订状态：已登记 -> 已应用 / 已失败
REVISION_REGISTERED = "registered"
REVISION_APPLIED = "applied"
REVISION_FAILED = "failed"

# 受影响区域的处置动作
ACTION_RELEASED_REPLANNED = "released_replanned"    # 未出动：释放原船并重排到新船
ACTION_RELEASED_UNASSIGNED = "released_unassigned"  # 未出动：释放后无人可排，区域空置
ACTION_REASSIGN_PENDING = "reassign_pending"        # 已出动：登记改派，等待接手确认
ACTION_REASSIGN_GAP = "reassign_gap"                # 已出动：找不到接手，留下缺口
GAP_ACTIONS = frozenset({ACTION_RELEASED_UNASSIGNED, ACTION_REASSIGN_GAP})

# 改派单状态
REASSIGN_PENDING = "pending_takeover"
REASSIGN_CONFIRMED = "confirmed"
REASSIGN_GAP = "gap"


@dataclass
class Impact:
    """单个受影响区域的处置决定。"""

    area_id: int
    area_code: str
    action: str
    dispatched: bool
    original_asset_id: int
    replacement_asset_id: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
