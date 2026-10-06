import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow
import json


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D100", route=None, risk=1, altitude=100):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[117.0, 39.8], [117.2, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)),
            "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def approve(self, plan_id, offline_id, role="airspace_reviewer", override=False):
        sub = self.svc.submit(plan_id, "op-user", "operator", "OP1", {})["plan"]
        body = {"expected_revision": sub["revision"], "offline_id": offline_id, "reason": "满足要求"}
        if override: body["override_reason"] = "应急救援授权"
        return self.svc.approve(plan_id, "reviewer", role, body)

    def restriction(self, name="临时限制", min_lon=116.0, min_lat=39.7, max_lon=116.2, max_lat=40.0):
        return self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": name, "kind": "temporary_limit",
            "min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat, "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})

    def test_restriction_change_invalidates_approved_plan_with_reasons(self):
        r = self.restriction()
        p = self.plan()
        self.approve(p["id"], "off-1")
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "approved")
        # 扩大限制覆盖计划航线 -> 重算 -> 转待复核，通知写清扣在哪
        self.svc.update_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1,
            "name": "临时限制", "kind": "temporary_limit", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 118.0, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=2)), "reason": "扩大"})
        got = self.svc.get_plan(p["id"], "airspace_reviewer", "")
        self.assertEqual(got["status"], "pending_review")
        notif = [n for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"] if n["kind"] == "approval_invalidated"]
        self.assertEqual(len(notif), 1)
        self.assertIn("temporary_limit", notif[0]["message"])
        self.assertIn("临时限制", notif[0]["message"])

    def test_restriction_revoke_auto_recovers_unchanged_plan(self):
        r = self.restriction()
        p = self.plan()
        self.approve(p["id"], "off-2")
        self.svc.update_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1,
            "name": "临时限制", "kind": "temporary_limit", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 118.0, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=2)), "reason": "扩大"})
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "pending_review")
        # 撤销限制 -> 版本/航线/时间都没动过 -> 自动恢复批准
        self.svc.revoke_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 2})
        got = self.svc.get_plan(p["id"], "airspace_reviewer", "")
        self.assertEqual(got["status"], "approved")
        recovered = [n for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"] if n["kind"] == "approval_recovered"]
        self.assertEqual(len(recovered), 1)

    def test_changed_plan_does_not_auto_recover(self):
        r = self.restriction()
        p = self.plan()
        self.approve(p["id"], "off-3")
        self.svc.update_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1,
            "name": "临时限制", "kind": "temporary_limit", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 118.0, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=2)), "reason": "扩大"})
        got = self.svc.get_plan(p["id"], "airspace_reviewer", "")
        self.assertEqual(got["status"], "pending_review")
        # 运营方变更计划 -> 草稿，需重新审核
        self.svc.change(p["id"], "op-user", "operator", "OP1", {"expected_revision": got["revision"],
            "route": [[117.01, 39.81], [117.21, 39.91]]})
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "draft")
        self.svc.revoke_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 2})
        # 动过的不自动恢复，仍是草稿，需重新提交审核
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "draft")
        sub = self.svc.submit(p["id"], "op-user", "operator", "OP1", {})["plan"]
        re = self.svc.approve(p["id"], "reviewer", "airspace_reviewer",
                              {"expected_revision": sub["revision"], "offline_id": "off-3b", "reason": "复核通过"})
        self.assertEqual(re["plan"]["status"], "approved")

    def test_optimistic_concurrency_first_commit_wins(self):
        r = self.restriction()
        # 审核员甲先提交（revision 1 -> 2）
        upd = self.svc.update_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1,
            "name": "临时限制", "kind": "temporary_limit", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.3, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=2)), "reason": "甲"})
        self.assertEqual(upd["revision"], 2)
        # 审核员乙用旧 revision 提交 -> 看到冲突
        with self.assertRaises(ApiError) as ctx:
            self.svc.update_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1,
                "name": "临时限制", "kind": "temporary_limit", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.9, "max_lat": 40.0,
                "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)),
                "ends_at": iso(self.start + timedelta(hours=2)), "reason": "乙"})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        self.assertEqual(ctx.exception.status, 409)

    def test_recalc_resumes_after_failure_without_duplicate_notifications(self):
        r = self.restriction()
        plans = [self.plan(f"D{i}", route=[[117.0 + i * 0.3, 39.8], [117.2 + i * 0.3, 39.9]]) for i in range(3)]
        for i, p in enumerate(plans):
            self.approve(p["id"], f"off-{i}")
        # 在第 2 个计划处注入一次瞬时失败
        orig = self.svc._recalc_plan
        state = {"calls": 0}
        def failing(conn, run, plan):
            state["calls"] += 1
            if state["calls"] == 2:
                raise RuntimeError("simulated crash")
            return orig(conn, run, plan)
        self.svc._recalc_plan = failing
        with self.assertRaises(RuntimeError):
            self.svc.update_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1,
                "name": "临时限制", "kind": "temporary_limit", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 118.0, "max_lat": 40.0,
                "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)),
                "ends_at": iso(self.start + timedelta(hours=2)), "reason": "扩大"})
        self.svc._recalc_plan = orig
        # 崩溃后：最近一次重算 run 仍 running，只有第 1 个计划落了 item
        run = self.svc.repo.conn.execute("SELECT * FROM recalc_runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "running")
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM recalc_items WHERE run_id=?", (run["id"],)).fetchone()["c"], 1)
        # 恢复：只重试没写完的部分，不漏计划，不重复通知
        self.svc._resume_runs()
        run = self.svc.repo.conn.execute("SELECT * FROM recalc_runs WHERE id=?", (run["id"],)).fetchone()
        self.assertEqual(run["status"], "done")
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) c FROM recalc_items WHERE run_id=?", (run["id"],)).fetchone()["c"], 3)
        notif = [n for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"] if n["kind"] == "approval_invalidated"]
        self.assertEqual(len(notif), 3)
        for p in plans:
            self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "pending_review")

    def test_backfill_populates_snapshot_and_restriction_links(self):
        # 限制覆盖计划航线 -> 批准时通过紧急授权批准，快照应绑定该限制
        r = self.restriction(min_lon=117.0, min_lat=39.7, max_lon=117.3, max_lat=40.0)
        p = self.plan(route=[[117.05, 39.8], [117.2, 39.9]])
        self.approve(p["id"], "off-4", role="commander", override=True)
        # 模拟台账建立前的旧数据：清空快照与链接
        self.svc.repo.conn.execute("UPDATE approvals SET snapshot_json=NULL")
        self.svc.repo.conn.execute("DELETE FROM approval_restrictions")
        # 重新初始化服务 -> 按当前关系回填
        svc2 = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        a = svc2.repo.conn.execute("SELECT snapshot_json FROM approvals WHERE plan_id=?", (p["id"],)).fetchone()
        self.assertIsNotNone(a["snapshot_json"])
        snap = json.loads(a["snapshot_json"])
        self.assertEqual(snap["revision"], 1)
        links = svc2.repo.conn.execute("SELECT restriction_id FROM approval_restrictions WHERE approval_id=(SELECT id FROM approvals WHERE plan_id=?)", (p["id"],)).fetchall()
        self.assertEqual([l["restriction_id"] for l in links], [r["id"]])

    def test_approval_binds_snapshot_and_restriction_snapshot(self):
        r = self.restriction(min_lon=117.0, min_lat=39.7, max_lon=117.3, max_lat=40.0)
        p = self.plan(route=[[117.05, 39.8], [117.2, 39.9]])
        self.approve(p["id"], "off-5", role="commander", override=True)
        approvals = self.svc.get_plan(p["id"], "airspace_reviewer", "")["approvals"]
        self.assertEqual(len(approvals), 1)
        a = approvals[0]
        self.assertEqual(a["snapshot"]["revision"], 1)
        self.assertEqual(a["snapshot"]["route"], [[117.05, 39.8], [117.2, 39.9]])
        self.assertEqual(a["snapshot"]["starts_at"], p["starts_at"])
        links = self.svc.repo.conn.execute("SELECT restriction_json FROM approval_restrictions WHERE approval_id=?", (a["id"],)).fetchall()
        self.assertEqual(len(links), 1)
        bound = json.loads(links[0]["restriction_json"])
        self.assertEqual(bound["id"], r["id"])
        self.assertEqual(bound["name"], "临时限制")

    def test_new_restriction_invalidates_approved_plan(self):
        p = self.plan()
        self.approve(p["id"], "off-6")
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "approved")
        # 新建一条覆盖计划航线的限制 -> 重算 -> 待复核
        self.restriction(name="新禁飞区", min_lon=117.0, min_lat=39.7, max_lon=118.0, max_lat=40.0)
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "pending_review")


if __name__ == "__main__": unittest.main()
