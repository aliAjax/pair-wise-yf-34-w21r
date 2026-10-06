import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow

FAR_AWAY = (110.0, 30.0, 111.0, 31.0)      # 与默认航线不相交
COVERING = (116.0, 39.7, 116.2, 40.0)      # 覆盖默认航线


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.db = Path(self.tmp.name) / "test.db"; self.svc = DroneAirspaceService(self.db)
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D100", route=None, risk=1, altitude=100):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def approve(self, plan, offline="off-1"):
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        return self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": offline, "reason": "符合要求"})

    def restriction(self, name="临时限制", box=FAR_AWAY):
        return self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": name, "kind": "temporary_limit", "min_lon": box[0], "min_lat": box[1], "max_lon": box[2], "max_lat": box[3], "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "演练"})

    def revise_to_cover(self, restriction_id, expected):
        return self.svc.revise_restriction(restriction_id, "reviewer", "airspace_reviewer", {"expected_revision": expected, "min_lon": COVERING[0], "min_lat": COVERING[1], "max_lon": COVERING[2], "max_lat": COVERING[3]})

    def test_approval_binds_revision_and_restriction_snapshot(self):
        r = self.restriction()
        plan = self.plan(); self.approve(plan)
        entries = self.svc.ledger("airspace_reviewer", "", plan["id"])["ledger"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["status"], "active")
        self.assertEqual(entry["plan_revision"], 1)
        self.assertEqual(entry["route"], plan["route"])
        self.assertEqual((entry["starts_at"], entry["ends_at"]), (plan["starts_at"], plan["ends_at"]))
        self.assertIn(r["id"], [s["id"] for s in entry["restriction_snapshot"]])

    def test_revision_invalidates_and_marks_pending_review_with_reason(self):
        r = self.restriction()
        plan = self.plan(); self.approve(plan)
        result = self.revise_to_cover(r["id"], 1)
        self.assertEqual(result["restriction"]["revision"], 2)
        self.assertEqual(result["affected_plans"], 1)
        self.assertEqual(self.svc.get_plan(plan["id"], "airspace_reviewer")["status"], "pending_review")
        entry = self.svc.ledger("airspace_reviewer", "", plan["id"])["ledger"][0]
        self.assertEqual(entry["status"], "invalidated")
        blockers = entry["invalidation_reason"]["blocking_conflicts"]
        self.assertEqual(blockers[0]["code"], "airspace_restriction")
        self.assertEqual(blockers[0]["restriction_id"], r["id"])
        # 运营方能拿到失效依据
        op_entry = self.svc.ledger("operator", "OP1", plan["id"])["ledger"][0]
        self.assertEqual(op_entry["invalidation_reason"], entry["invalidation_reason"])
        notes = [n for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"] if n["kind"] == "approval_invalidated"]
        self.assertEqual(len(notes), 1)
        self.assertIn("待复核", notes[0]["message"])
        self.assertIn("空域限制", notes[0]["message"])
        with self.assertRaises(ApiError) as ctx: self.svc.ledger("operator", "OP2", plan["id"])
        self.assertEqual(ctx.exception.code, "plan_forbidden")

    def test_concurrent_restriction_revision_conflict(self):
        r = self.restriction()
        self.svc.revise_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "reason": "先提交的算数"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.revise_restriction(r["id"], "reviewer2", "airspace_reviewer", {"expected_revision": 1, "reason": "后到的冲突"})
        self.assertEqual(ctx.exception.code, "restriction_revision_conflict")
        fresh = self.svc.get_restriction(r["id"], "airspace_reviewer")
        self.assertEqual(fresh["reason"], "先提交的算数")
        self.assertEqual([h["revision"] for h in fresh["history"]], [1, 2])
        with self.assertRaises(ApiError) as ctx:
            self.svc.revise_restriction(r["id"], "reviewer2", "airspace_reviewer", {"expected_revision": 1, "reason": "仍用旧版本"})
        self.assertEqual(ctx.exception.code, "restriction_revision_conflict")

    def test_revoke_restores_untouched_approval(self):
        r = self.restriction()
        plan = self.plan(); self.approve(plan)
        self.revise_to_cover(r["id"], 1)
        self.assertEqual(self.svc.get_plan(plan["id"], "airspace_reviewer")["status"], "pending_review")
        result = self.svc.revoke_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 2})
        self.assertEqual(result["restriction"]["status"], "revoked")
        self.assertEqual(self.svc.get_plan(plan["id"], "airspace_reviewer")["status"], "approved")
        entry = self.svc.ledger("airspace_reviewer", "", plan["id"])["ledger"][0]
        self.assertEqual(entry["status"], "restored")
        self.assertIsNone(entry["invalidation_reason"])
        kinds = [n["kind"] for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"]]
        self.assertIn("approval_restored", kinds)
        with self.assertRaises(ApiError) as ctx:
            self.svc.revise_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 3, "reason": "已撤销"})
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_revoke_does_not_restore_changed_plan(self):
        r = self.restriction()
        plan = self.plan(); self.approve(plan)
        self.revise_to_cover(r["id"], 1)
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertEqual(changed["revision"], 2)
        self.svc.revoke_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 2})
        after = self.svc.get_plan(plan["id"], "airspace_reviewer")
        self.assertEqual(after["status"], "draft")
        kinds = [n["kind"] for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"]]
        self.assertNotIn("approval_restored", kinds)
        # 动过的重新走审核
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        reapproved = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 2, "offline_id": "off-re", "reason": "重新审核通过"})
        self.assertEqual(reapproved["plan"]["status"], "approved")
        entries = self.svc.ledger("airspace_reviewer", "", plan["id"])["ledger"]
        self.assertEqual((entries[0]["status"], entries[0]["plan_revision"]), ("active", 2))
        self.assertEqual(entries[1]["status"], "superseded")

    def test_failed_recalc_retries_only_unfinished_items(self):
        r = self.restriction()
        p1 = self.plan("D100"); self.approve(p1, "off-1")
        p2 = self.plan("D200", route=[[116.4, 39.8], [116.5, 39.9]]); self.approve(p2, "off-2")
        original, failed = self.svc._recalc_plan, {"raised": False}
        def flaky(conn, run, plan_id):
            if plan_id == p2["id"] and not failed["raised"]:
                failed["raised"] = True
                raise RuntimeError("模拟写入失败")
            return original(conn, run, plan_id)
        self.svc._recalc_plan = flaky
        with self.assertRaises(RuntimeError):
            self.svc.revise_restriction(r["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.9, "max_lat": 40.0})
        self.svc._recalc_plan = original
        self.assertEqual(self.svc.get_plan(p1["id"], "airspace_reviewer")["status"], "pending_review")
        self.assertEqual(self.svc.get_plan(p2["id"], "airspace_reviewer")["status"], "approved")
        run = self.svc.recalc_runs("airspace_reviewer")["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual([i["status"] for i in run["items"]], ["done", "failed"])
        retry = self.svc.retry_recalc(run["id"], "reviewer", "airspace_reviewer")["recalc_run"]
        self.assertEqual(retry["status"], "done")
        self.assertEqual([i["status"] for i in retry["items"]], ["done", "done"])
        self.assertEqual(self.svc.get_plan(p2["id"], "airspace_reviewer")["status"], "pending_review")
        notes = [n for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"] if n["kind"] == "approval_invalidated"]
        self.assertEqual(len([n for n in notes if n["plan_id"] == p1["id"]]), 1)
        self.assertEqual(len([n for n in notes if n["plan_id"] == p2["id"]]), 1)

    def test_backfill_ledger_for_existing_data(self):
        r = self.restriction()
        plan = self.plan(); self.approve(plan)
        self.svc.repo.conn.execute("DELETE FROM approval_ledger")
        self.svc.repo.conn.execute("DELETE FROM restriction_snapshots")
        reopened = DroneAirspaceService(self.db)
        entries = reopened.ledger("airspace_reviewer", "", plan["id"])["ledger"]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["status"], entries[0]["plan_revision"]), ("active", 1))
        self.assertIn(r["id"], [s["id"] for s in entries[0]["restriction_snapshot"]])
        self.assertEqual(len(reopened.get_restriction(r["id"], "airspace_reviewer")["history"]), 1)


if __name__ == "__main__": unittest.main()
