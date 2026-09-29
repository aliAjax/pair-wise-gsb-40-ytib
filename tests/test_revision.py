import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402
from revision.store import RevisionStore  # noqa: E402


class RevisionFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = MaritimeSARService(self.db_path)
        self.revisions = self.service.revisions
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-100", "海燕号", 31.0, 122.0, 10.0, 3, "东海中心"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def add_asset(self, name, max_sea_state):
        return self.service.add_asset(
            "coord1", "coordinator", name, "vessel", ["surface"], 31.0, 122.0, 20, 300, max_sea_state
        )

    def add_area(self, code, lat=31.1, lon=122.1, priority=1):
        return self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], code, "surface", lat, lon, 8, priority
        )

    def register(self, token="rev-1", sea_state=7, base=1, actor="coord1"):
        return self.revisions.register(
            actor, "coordinator", incident_id=self.incident["id"], sea_state=sea_state,
            drift_direction=120.0, drift_speed_kn=1.5, effective_at="2026-09-29T18:00:00Z",
            base_plan_version=base, client_token=token, note="冷空气过境",
        )

    def state_maps(self):
        state = self.service.state()
        return (
            {a["name"]: a for a in state["assets"]},
            {a["code"]: a for a in state["search_areas"]},
            {i["id"]: i for i in state["incidents"]},
        )

    def test_replan_undispatched_and_reassign_dispatched(self):
        old1 = self.add_asset("旧船-1", 4)
        old2 = self.add_asset("旧船-2", 4)
        new1 = self.add_asset("新船-1", 8)
        new2 = self.add_asset("新船-2", 8)
        area1 = self.add_area("A-1")
        area2 = self.add_area("A-2", lat=31.2, lon=122.2, priority=2)
        self.service.assign_area("coord1", "coordinator", area1["id"], old1["id"])
        self.service.assign_area("coord1", "coordinator", area2["id"], old2["id"])
        self.service.depart_area("coord1", "coordinator", area2["id"])

        registered = self.register()
        self.assertFalse(registered["idempotent"])
        revision_id = registered["revision"]["id"]
        self.assertEqual(1, registered["revision"]["revision_no"])
        self.assertEqual("2026-09-29T18:00:00+00:00", registered["revision"]["effective_at"])

        preview = self.revisions.preview(revision_id)
        self.assertFalse(preview["applied"])
        actions = {item["area_code"]: item for item in preview["impacts"]}
        self.assertEqual("released_replanned", actions["A-1"]["action"])
        self.assertEqual(new1["id"], actions["A-1"]["replacement_asset_id"])
        self.assertEqual("新船-1", actions["A-1"]["replacement_asset_name"])
        self.assertEqual("reassign_pending", actions["A-2"]["action"])
        self.assertEqual(new2["id"], actions["A-2"]["replacement_asset_id"])
        # 预览只是查看，不改变任何占用
        assets, areas, incidents = self.state_maps()
        self.assertEqual("assigned", assets["旧船-1"]["status"])
        self.assertEqual("available", assets["新船-1"]["status"])
        self.assertEqual(3, incidents[self.incident["id"]]["sea_state"])

        applied = self.revisions.confirm("coord1", "coordinator", revision_id)
        self.assertFalse(applied["idempotent"])
        self.assertEqual(2, applied["plan_version"])
        self.assertEqual([], applied["gaps"])
        assets, areas, incidents = self.state_maps()
        # 未出动：原船释放、新船接手
        self.assertEqual("available", assets["旧船-1"]["status"])
        self.assertEqual("assigned", assets["新船-1"]["status"])
        self.assertEqual(new1["id"], areas["A-1"]["assigned_asset_id"])
        # 已出动：接手确认前原船负责，接手船已预留防止冲突任务
        self.assertEqual(old2["id"], areas["A-2"]["assigned_asset_id"])
        self.assertEqual("active", areas["A-2"]["status"])
        self.assertEqual("assigned", assets["新船-2"]["status"])
        self.assertEqual(7, incidents[self.incident["id"]]["sea_state"])
        self.assertEqual(2, incidents[self.incident["id"]]["plan_version"])

        view = self.revisions.view(self.incident["id"])
        reassignment = view["revisions"][0]["reassignments"][0]
        self.assertEqual("pending_takeover", reassignment["status"])
        self.assertEqual("旧船-2", reassignment["original_asset_name"])
        self.assertEqual("新船-2", reassignment["replacement_asset_name"])
        self.assertEqual([], view["gaps"])

        done = self.revisions.confirm_takeover("coord1", "coordinator", reassignment["id"])
        self.assertFalse(done["idempotent"])
        self.assertEqual("confirmed", done["reassignment"]["status"])
        again = self.revisions.confirm_takeover("coord1", "coordinator", reassignment["id"])
        self.assertTrue(again["idempotent"])
        assets, areas, _ = self.state_maps()
        self.assertEqual(new2["id"], areas["A-2"]["assigned_asset_id"])
        self.assertEqual("available", assets["旧船-2"]["status"])

    def test_gap_when_no_takeover_available(self):
        old1 = self.add_asset("旧船-1", 4)
        old2 = self.add_asset("旧船-2", 4)
        area1 = self.add_area("A-1")
        area2 = self.add_area("A-2", lat=31.2, lon=122.2, priority=2)
        self.service.assign_area("coord1", "coordinator", area1["id"], old1["id"])
        self.service.assign_area("coord1", "coordinator", area2["id"], old2["id"])
        self.service.depart_area("coord1", "coordinator", area2["id"])

        revision_id = self.register()["revision"]["id"]
        applied = self.revisions.confirm("coord1", "coordinator", revision_id)
        actions = {item["area_code"]: item["action"] for item in applied["impacts"]}
        self.assertEqual("released_unassigned", actions["A-1"])
        self.assertEqual("reassign_gap", actions["A-2"])
        self.assertEqual(2, len(applied["gaps"]))

        assets, areas, _ = self.state_maps()
        self.assertIsNone(areas["A-1"]["assigned_asset_id"])
        self.assertEqual("planned", areas["A-1"]["status"])
        self.assertEqual("available", assets["旧船-1"]["status"])
        # 找不到接手：留下缺口，原船继续负责
        self.assertEqual(old2["id"], areas["A-2"]["assigned_asset_id"])

        view = self.revisions.view(self.incident["id"])
        kinds = sorted(gap["kind"] for gap in view["gaps"])
        self.assertEqual(["takeover_gap", "unassigned_gap"], kinds)
        gap = [g for g in view["gaps"] if g["kind"] == "takeover_gap"][0]
        self.assertEqual("旧船-2", gap["original_asset_name"])
        self.assertEqual("A-2", gap["area_code"])
        self.assertEqual(1, gap["revision_no"])

    def test_concurrent_revision_gets_version_conflict(self):
        first = self.register(token="rev-a")["revision"]["id"]
        second = self.register(token="rev-b", actor="coord2")["revision"]["id"]
        self.revisions.confirm("coord1", "coordinator", first)
        with self.assertRaises(DomainError) as ctx:
            self.revisions.confirm("coord2", "coordinator", second)
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("版本冲突", str(ctx.exception))
        # 后到一方基于新计划版本重新登记后可应用
        retry = self.register(token="rev-b2", base=2, actor="coord2")["revision"]["id"]
        applied = self.revisions.confirm("coord2", "coordinator", retry)
        self.assertEqual(3, applied["plan_version"])

    def test_idempotent_register_and_confirm(self):
        old1 = self.add_asset("旧船-1", 4)
        new1 = self.add_asset("新船-1", 8)
        area1 = self.add_area("A-1")
        self.service.assign_area("coord1", "coordinator", area1["id"], old1["id"])

        first = self.register(token="rev-x")
        second = self.register(token="rev-x")
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["revision"]["id"], second["revision"]["id"])

        revision_id = first["revision"]["id"]
        applied = self.revisions.confirm("coord1", "coordinator", revision_id)
        replay = self.revisions.confirm("coord1", "coordinator", revision_id)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(applied["impacts"], replay["impacts"])
        self.assertEqual(applied["plan_version"], replay["plan_version"])
        # 重试不产生重复占用
        assets, areas, _ = self.state_maps()
        self.assertEqual("assigned", assets["新船-1"]["status"])
        self.assertEqual("available", assets["旧船-1"]["status"])
        self.assertEqual(new1["id"], areas["A-1"]["assigned_asset_id"])
        view = self.revisions.view(self.incident["id"])
        self.assertEqual(1, len(view["revisions"]))
        self.assertEqual(1, len(view["revisions"][0]["impacts"]))

    def test_failed_apply_rolls_back_and_retry_is_complete(self):
        old1 = self.add_asset("旧船-1", 4)
        new1 = self.add_asset("新船-1", 8)
        area1 = self.add_area("A-1")
        self.service.assign_area("coord1", "coordinator", area1["id"], old1["id"])
        revision_id = self.register()["revision"]["id"]

        store = self.revisions.store
        original = store._apply_impact

        def crash(*args, **kwargs):
            raise RuntimeError("模拟写入中途崩溃")

        store._apply_impact = crash
        with self.assertRaises(RuntimeError):
            self.revisions.confirm("coord1", "coordinator", revision_id)
        store._apply_impact = original

        # 崩溃后不留半套占用
        assets, areas, incidents = self.state_maps()
        self.assertEqual("assigned", assets["旧船-1"]["status"])
        self.assertEqual("available", assets["新船-1"]["status"])
        self.assertEqual(old1["id"], areas["A-1"]["assigned_asset_id"])
        self.assertEqual(3, incidents[self.incident["id"]]["sea_state"])
        self.assertEqual(1, incidents[self.incident["id"]]["plan_version"])
        self.assertEqual("registered", self.revisions.store.get_revision(revision_id)["status"])

        # 重试得到完整结果
        applied = self.revisions.confirm("coord1", "coordinator", revision_id)
        self.assertEqual(1, len(applied["impacts"]))
        assets, areas, _ = self.state_maps()
        self.assertEqual(new1["id"], areas["A-1"]["assigned_asset_id"])

    def test_recovery_marks_non_terminal_revision_failed(self):
        revision_id = self.register()["revision"]["id"]
        with self.revisions.store.connect() as conn:
            conn.execute("UPDATE sea_revisions SET status='applying' WHERE id=?", (revision_id,))
        RevisionStore(self.db_path)  # 模拟服务重启后的恢复
        self.assertEqual("failed", self.revisions.store.get_revision(revision_id)["status"])
        with self.assertRaises(DomainError) as ctx:
            self.revisions.confirm("coord1", "coordinator", revision_id)
        self.assertEqual(409, ctx.exception.status)

    def test_withdraw_blocked_for_pending_takeover(self):
        old2 = self.add_asset("旧船-2", 4)
        new2 = self.add_asset("新船-2", 8)
        area2 = self.add_area("A-2")
        self.service.assign_area("coord1", "coordinator", area2["id"], old2["id"])
        self.service.depart_area("coord1", "coordinator", area2["id"])
        revision_id = self.register()["revision"]["id"]
        self.revisions.confirm("coord1", "coordinator", revision_id)
        asset = [a for a in self.service.list_assets() if a["id"] == new2["id"]][0]
        with self.assertRaises(DomainError) as ctx:
            self.service.withdraw_asset("coord1", "coordinator", new2["id"], "临时调离", asset["version"])
        self.assertEqual(409, ctx.exception.status)

    def test_permission_and_validation(self):
        with self.assertRaises(DomainError) as ctx:
            self.revisions.register(
                "op1", "operator", incident_id=self.incident["id"], sea_state=5,
                drift_direction=0, drift_speed_kn=1, effective_at="2026-09-29T18:00:00Z",
                base_plan_version=1, client_token="t-1",
            )
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError):
            self.register(token="")
        with self.assertRaises(DomainError):
            self.revisions.register(
                "coord1", "coordinator", incident_id=self.incident["id"], sea_state=5,
                drift_direction=0, drift_speed_kn=1, effective_at="不是时间",
                base_plan_version=1, client_token="t-2",
            )


if __name__ == "__main__":
    unittest.main()
