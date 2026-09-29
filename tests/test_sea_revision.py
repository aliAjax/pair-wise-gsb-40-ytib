import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
from app import DomainError, MaritimeSARService


class SeaRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = MaritimeSARService(Path(self.tmp.name) / "test.db")
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-001", "海燕号", 31.0, 122.0, 10.0, 3, "东海中心"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _asset(self, name="海巡01", max_sea=5, lat=31.0, lon=122.0, speed=20, rng=100):
        return self.service.add_asset("coord1", "coordinator", name, "vessel", ["surface"],
                                      lat, lon, speed, rng, max_sea)

    def _area(self, code="A-01", lat=31.0, lon=122.0):
        return self.service.create_search_area("coord1", "coordinator", self.incident["id"],
                                               code, "surface", lat, lon, 5, 1)

    def _assign(self, area, asset):
        return self.service.assign_area("coord1", "coordinator", area["id"], asset["id"], asset["version"])

    def _fresh(self, area):
        return [a for a in self.service.state()["search_areas"] if a["id"] == area["id"]][0]

    def test_assigned_area_is_released_and_reassigned(self):
        # 未出动：新海况超出原船能力，释放后重排给能接手的船
        old = self._asset("海巡01", max_sea=5)
        new = self._asset("海巡02", max_sea=8, lat=31.02, lon=122.02)
        area = self._area()
        self._assign(area, old)
        revision = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 8, 90, 2.0, "2026-09-29T12:00:00+00:00"
        )
        preview = self.service.preview_sea_revision("coord1", "coordinator", revision["id"])
        self.assertEqual(1, preview["summary"]["affected"])
        self.assertEqual("reassigned", preview["affected"][0]["disposition"])
        self.assertEqual(new["id"], preview["affected"][0]["replacement_asset"]["id"])
        # 预览不落库
        self.assertEqual("pending", self.service.list_sea_revisions(self.incident["id"])[0]["status"])
        applied = self.service.apply_sea_revision("coord1", "coordinator", revision["id"], self.incident["version"])
        self.assertEqual("applied", applied["revision"]["status"])
        self.assertEqual(1, applied["summary"]["reassigned"])
        area_after = self._fresh(area)
        self.assertEqual("assigned", area_after["status"])
        self.assertEqual(new["id"], area_after["assigned_asset_id"])
        assets = {a["name"]: a["status"] for a in self.service.list_assets()}
        self.assertEqual("available", assets["海巡01"])
        self.assertEqual("assigned", assets["海巡02"])

    def test_active_area_keeps_original_until_handover_confirm(self):
        # 已出动：先登记改派，接手确认前原船仍负责
        old = self._asset("海巡01", max_sea=5)
        new = self._asset("海巡02", max_sea=8, lat=31.02, lon=122.02)
        area = self._area()
        self._assign(area, old)
        area = self._fresh(area)
        self.service.activate_area("coord1", "coordinator", area["id"], area["version"])
        revision = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 8, 90, 2.0, "2026-09-29T12:00:00+00:00"
        )
        preview = self.service.preview_sea_revision("coord1", "coordinator", revision["id"])
        self.assertEqual("reassigning", preview["affected"][0]["disposition"])
        applied = self.service.apply_sea_revision("coord1", "coordinator", revision["id"], self.incident["version"])
        self.assertEqual(1, applied["summary"]["reassigning"])
        assignment = applied["assignments"][0]
        self.assertEqual("reassigning", assignment["status"])
        self.assertEqual(old["id"], assignment["original_asset_id"])
        self.assertEqual(new["id"], assignment["replacement_asset_id"])
        # 确认前：区域仍由原船负责，接手船处于 reserved 占用
        area_before = self._fresh(area)
        self.assertEqual("active", area_before["status"])
        self.assertEqual(old["id"], area_before["assigned_asset_id"])
        assets = {a["name"]: a["status"] for a in self.service.list_assets()}
        self.assertEqual("assigned", assets["海巡01"])
        self.assertEqual("reserved", assets["海巡02"])
        # 接手确认后：原船解除责任，接手船负责
        confirmed = self.service.confirm_revision_handover("coord1", "coordinator", assignment["id"])
        self.assertEqual("handed_over", confirmed["status"])
        self.assertEqual("coord1", confirmed["confirmed_by"])
        area_after = self._fresh(area)
        self.assertEqual("active", area_after["status"])
        self.assertEqual(new["id"], area_after["assigned_asset_id"])
        assets = {a["name"]: a["status"] for a in self.service.list_assets()}
        self.assertEqual("available", assets["海巡01"])
        self.assertEqual("assigned", assets["海巡02"])
        # 重复确认冲突
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_revision_handover("coord1", "coordinator", assignment["id"])
        self.assertEqual(409, ctx.exception.status)

    def test_gap_when_no_replacement_available(self):
        # 海况超出所有船能力：未出动释放后留缺口；已出动登记改派无接手也留缺口
        old = self._asset("海巡01", max_sea=5)
        area = self._area()
        self._assign(area, old)
        area = self._fresh(area)
        self.service.activate_area("coord1", "coordinator", area["id"], area["version"])
        revision = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 9, 0, 0.0, "2026-09-29T12:00:00+00:00"
        )
        preview = self.service.preview_sea_revision("coord1", "coordinator", revision["id"])
        self.assertEqual("gap", preview["affected"][0]["disposition"])
        self.assertIsNone(preview["affected"][0]["replacement_asset"])
        applied = self.service.apply_sea_revision("coord1", "coordinator", revision["id"], self.incident["version"])
        self.assertEqual(1, applied["summary"]["gaps"])
        self.assertEqual("gap", applied["assignments"][0]["status"])
        self.assertIsNone(applied["assignments"][0]["replacement_asset_id"])
        area_after = self._fresh(area)
        self.assertEqual("gap", area_after["status"])
        self.assertIsNone(area_after["assigned_asset_id"])
        self.assertEqual("available", self.service.list_assets()[0]["status"])

    def test_concurrent_revision_sees_version_conflict(self):
        self._asset("海巡01", max_sea=5)
        rev1 = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 6, 90, 1.0, "2026-09-29T12:00:00+00:00"
        )
        rev2 = self.service.register_sea_revision(
            "coord2", "coordinator", self.incident["id"], 7, 180, 2.0, "2026-09-29T12:00:00+00:00"
        )
        self.service.apply_sea_revision("coord1", "coordinator", rev1["id"], self.incident["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.apply_sea_revision("coord2", "coordinator", rev2["id"], self.incident["version"])
        self.assertEqual(409, ctx.exception.status)

    def test_retry_after_apply_is_idempotent(self):
        self._asset("海巡01", max_sea=5)
        rev = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 6, 90, 1.0, "2026-09-29T12:00:00+00:00"
        )
        first = self.service.apply_sea_revision("coord1", "coordinator", rev["id"], self.incident["version"])
        self.assertFalse(first["idempotent"])
        second = self.service.apply_sea_revision("coord1", "coordinator", rev["id"], self.incident["version"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(len(first["assignments"]), len(second["assignments"]))

    def test_failed_apply_rolls_back_and_retry_succeeds(self):
        old = self._asset("海巡01", max_sea=5)
        self._asset("海巡02", max_sea=8, lat=31.02, lon=122.02)
        area = self._area()
        self._assign(area, old)
        area = self._fresh(area)
        self.service.activate_area("coord1", "coordinator", area["id"], area["version"])
        rev = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 8, 90, 2.0, "2026-09-29T12:00:00+00:00"
        )
        original_eval = appmod.evaluate_area
        appmod.evaluate_area = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟写入失败"))
        with self.assertRaises(RuntimeError):
            self.service.apply_sea_revision("coord1", "coordinator", rev["id"], self.incident["version"])
        appmod.evaluate_area = original_eval
        # 无半套占用：区域仍由原船负责，资源未被释放/占用，修订仍待应用
        area_after = self._fresh(area)
        self.assertEqual("active", area_after["status"])
        self.assertEqual(old["id"], area_after["assigned_asset_id"])
        self.assertEqual("assigned", self.service.list_assets()[0]["status"])
        self.assertEqual("pending", self.service.list_sea_revisions(self.incident["id"])[0]["status"])
        # 重试得到完整结果
        applied = self.service.apply_sea_revision("coord1", "coordinator", rev["id"], self.incident["version"])
        self.assertEqual("applied", applied["revision"]["status"])
        self.assertEqual(1, applied["summary"]["reassigning"])

    def test_drift_beyond_range_marks_out_of_range(self):
        # 海况未超出能力，但漂移把区域中心推到航程之外 -> 受影响
        old = self._asset("海巡01", max_sea=9, rng=10)
        area = self._area(lat=31.0, lon=122.0)
        self._assign(area, old)
        rev = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 4, 90, 30.0, "2026-09-30T12:00:00+00:00"
        )
        preview = self.service.preview_sea_revision("coord1", "coordinator", rev["id"])
        self.assertEqual(1, preview["summary"]["affected"])
        self.assertIn("out_of_range", preview["affected"][0]["reasons"])

    def test_unaffected_area_only_repositions(self):
        # 海况不变、漂移很小：不受影响，仅更新漂移中心
        old = self._asset("海巡01", max_sea=5)
        area = self._area()
        self._assign(area, old)
        rev = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 3, 90, 0.1, "2026-09-29T12:00:00+00:00"
        )
        applied = self.service.apply_sea_revision("coord1", "coordinator", rev["id"], self.incident["version"])
        self.assertEqual(0, applied["summary"]["affected"])
        self.assertEqual(1, applied["summary"]["repositioned"])
        self.assertEqual("assigned", self._fresh(area)["status"])

    def test_viewer_cannot_register_or_apply(self):
        rev = self.service.register_sea_revision(
            "coord1", "coordinator", self.incident["id"], 6, 90, 1.0, "2026-09-29T12:00:00+00:00"
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.register_sea_revision("look", "viewer", self.incident["id"], 6, 90, 1.0,
                                               "2026-09-29T12:00:00+00:00")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.apply_sea_revision("look", "viewer", rev["id"], self.incident["version"])
        self.assertEqual(403, ctx2.exception.status)


if __name__ == "__main__":
    unittest.main()
