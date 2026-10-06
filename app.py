#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8205
ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row; self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, offline_id TEXT UNIQUE,
            override_kind TEXT, snapshot_json TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approval_restrictions(
            approval_id INTEGER NOT NULL REFERENCES approvals(id), restriction_id INTEGER NOT NULL REFERENCES restrictions(id),
            restriction_json TEXT NOT NULL, PRIMARY KEY(approval_id, restriction_id)
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recalc_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL, restriction_id INTEGER,
            status TEXT NOT NULL DEFAULT 'running', created_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS recalc_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES recalc_runs(id), plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            outcome TEXT NOT NULL, detail_json TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            UNIQUE(run_id, plan_id)
        );
        """)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the initial release for pre-existing databases."""
        for table, column, ddl in (
            ("restrictions", "revision", "ALTER TABLE restrictions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"),
            ("approvals", "snapshot_json", "ALTER TABLE approvals ADD COLUMN snapshot_json TEXT"),
        ):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(ddl)

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try: yield self.conn; self.conn.execute("COMMIT")
        except Exception: self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path):
        self.repo = Repository(path)
        self.backfill_ledger()

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    # ----- 台账快照：每条批准绑住当时的计划版本与限制快照 -----
    @staticmethod
    def _plan_snapshot(plan: sqlite3.Row) -> dict[str, Any]:
        return {"revision": plan["revision"], "route": json.loads(plan["route_json"]),
                "starts_at": plan["starts_at"], "ends_at": plan["ends_at"]}

    @staticmethod
    def _restriction_snapshot(restriction: sqlite3.Row) -> dict[str, Any]:
        return {k: restriction[k] for k in ("id", "name", "kind", "min_lon", "min_lat", "max_lon", "max_lat",
                                            "min_altitude", "max_altitude", "starts_at", "ends_at", "reason", "status", "revision")}

    def _overlapping_active_restrictions(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> list[sqlite3.Row]:
        route = self._route(plan); bbox = route_bbox(route)
        start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        out: list[sqlite3.Row] = []
        for r in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (r["min_lon"], r["min_lat"], r["max_lon"], r["max_lat"])
            if boxes_overlap(bbox, rbox) and times_overlap(start, end, parse_time(r["starts_at"]), parse_time(r["ends_at"])):
                out.append(r)
        return out

    def _capture_approval_snapshot(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> tuple[str, list[tuple[int, str]]]:
        snapshot = self._plan_snapshot(plan)
        links = [(r["id"], json.dumps(self._restriction_snapshot(r), ensure_ascii=False, sort_keys=True))
                 for r in self._overlapping_active_restrictions(conn, plan)]
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True), links

    def backfill_ledger(self) -> None:
        """为台账建立前已存在的批准按当前关系回填计划快照与限制链接（不重算、不通知）。"""
        with self.repo.tx() as conn:
            approvals = list(conn.execute("SELECT * FROM approvals"))
            for a in approvals:
                plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (a["plan_id"],)).fetchone()
                if not plan: continue
                if not a["snapshot_json"]:
                    conn.execute("UPDATE approvals SET snapshot_json=? WHERE id=?",
                                 (json.dumps(self._plan_snapshot(plan), ensure_ascii=False, sort_keys=True), a["id"]))
                existing = {r["restriction_id"] for r in conn.execute("SELECT restriction_id FROM approval_restrictions WHERE approval_id=?", (a["id"],))}
                for r in self._overlapping_active_restrictions(conn, plan):
                    if r["id"] in existing: continue
                    conn.execute("INSERT OR IGNORE INTO approval_restrictions(approval_id,restriction_id,restriction_json) VALUES(?,?,?)",
                                 (a["id"], r["id"], json.dumps(self._restriction_snapshot(r), ensure_ascii=False, sort_keys=True)))

    # ----- 限制变更后的失效重算（可恢复、不重不漏） -----
    def _recalculate(self, trigger: str, restriction_id: int | None = None) -> dict[str, Any]:
        self._resume_runs()
        with self.repo.tx() as conn:
            cur = conn.execute("INSERT INTO recalc_runs(trigger,restriction_id,status,created_at) VALUES(?,?, 'running',?)",
                               (trigger, restriction_id, iso()))
            run_id = cur.lastrowid
        self._process_run(run_id)
        return self._run_summary(run_id)

    def _resume_runs(self) -> None:
        for run in list(self.repo.conn.execute("SELECT * FROM recalc_runs WHERE status='running' ORDER BY id")):
            self._process_run(run["id"])

    def _process_run(self, run_id: int) -> None:
        while True:
            with self.repo.tx() as conn:
                run = conn.execute("SELECT * FROM recalc_runs WHERE id=?", (run_id,)).fetchone()
                if not run or run["status"] != "running": return
                plan = conn.execute("""SELECT p.* FROM flight_plans p
                                       WHERE p.status IN ('approved','pending_review')
                                         AND NOT EXISTS (SELECT 1 FROM recalc_items i WHERE i.run_id=? AND i.plan_id=p.id)
                                       ORDER BY p.id LIMIT 1""", (run_id,)).fetchone()
                if not plan:
                    conn.execute("UPDATE recalc_runs SET status='done', finished_at=? WHERE id=?", (iso(), run_id))
                    return
                self._recalc_plan(conn, run, plan)

    @staticmethod
    def _already_notified(conn: sqlite3.Connection, plan_id: int, kind: str) -> bool:
        return conn.execute("SELECT 1 FROM notifications WHERE plan_id=? AND kind=? LIMIT 1", (plan_id, kind)).fetchone() is not None

    @staticmethod
    def _conflict_reasons(conflicts: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for c in conflicts:
            if c["code"] == "airspace_restriction": parts.append(f"{c.get('kind','')}:{c.get('name','')}")
            elif c["code"] == "adjacent_traffic": parts.append("相邻航路冲突")
            else: parts.append(c.get("message", c["code"]))
        return "；".join(parts) if parts else "未知冲突"

    def _unchanged_since_approval(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> bool:
        row = conn.execute("SELECT snapshot_json FROM approvals WHERE plan_id=? AND decision='approved' ORDER BY id DESC LIMIT 1", (plan["id"],)).fetchone()
        if not row or not row["snapshot_json"]: return False
        snap = json.loads(row["snapshot_json"]); current = self._plan_snapshot(plan)
        return (snap.get("revision") == current["revision"] and snap.get("route") == current["route"]
                and snap.get("starts_at") == current["starts_at"] and snap.get("ends_at") == current["ends_at"])

    def _recalc_plan(self, conn: sqlite3.Connection, run: sqlite3.Row, plan: sqlite3.Row) -> None:
        report = self._conflict_report(conn, plan)
        conflicts = report["hard_violations"] + report["blocking_conflicts"]
        clean = not conflicts
        outcome, notified, detail = "kept_approved", 0, {"revision": plan["revision"]}
        if plan["status"] == "approved":
            if not clean:
                outcome = "invalidated"; reasons = self._conflict_reasons(conflicts)
                conn.execute("UPDATE flight_plans SET status='pending_review',updated_at=? WHERE id=?", (iso(), plan["id"]))
                Repository.notify(conn, plan["id"], "approval_invalidated",
                                  f"飞行计划 {plan['callsign']} 因空域变化不再满足批准条件，已转待复核：{reasons}")
                Repository.audit(conn, plan["id"], "system", "airspace", "approval_invalidated",
                                 {"run_id": run["id"], "reasons": reasons, "conflicts": conflicts})
                notified, detail["reasons"] = 1, reasons
        elif plan["status"] == "pending_review":
            if clean:
                if self._unchanged_since_approval(conn, plan):
                    outcome = "recovered"
                    conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan["id"]))
                    Repository.notify(conn, plan["id"], "approval_recovered",
                                      f"飞行计划 {plan['callsign']} 空域限制已撤销且计划版本、航线、时间均未变更，自动恢复批准")
                    Repository.audit(conn, plan["id"], "system", "airspace", "approval_auto_recovered", {"run_id": run["id"]})
                    notified = 1
                else:
                    outcome = "recheck_needed"
                    if not self._already_notified(conn, plan["id"], "recheck_needed"):
                        Repository.notify(conn, plan["id"], "recheck_needed",
                                          f"飞行计划 {plan['callsign']} 空域限制已撤销，但计划版本、航线或时间已变更，需重新提交审核")
                        notified = 1
            else:
                outcome = "still_pending"; detail["reasons"] = self._conflict_reasons(conflicts)
        conn.execute("INSERT INTO recalc_items(run_id,plan_id,outcome,detail_json,notified,created_at) VALUES(?,?,?,?,?,?)",
                     (run["id"], plan["id"], outcome, json.dumps(detail, ensure_ascii=False, sort_keys=True), notified, iso()))

    def _run_summary(self, run_id: int) -> dict[str, Any]:
        run = self.repo.conn.execute("SELECT * FROM recalc_runs WHERE id=?", (run_id,)).fetchone()
        items = [dict(r) for r in self.repo.conn.execute("SELECT plan_id,outcome,notified FROM recalc_items WHERE run_id=? ORDER BY id", (run_id,))]
        counts: dict[str, int] = {}
        for it in items: counts[it["outcome"]] = counts.get(it["outcome"], 0) + 1
        return {"run_id": run_id, "status": run["status"], "trigger": run["trigger"],
                "restriction_id": run["restriction_id"], "processed": len(items), "counts": counts, "items": items}

    # ----- 限制维护（乐观并发：先提交算数，后到见冲突） -----
    def _validate_restriction(self, body: dict[str, Any]) -> tuple[str, str, str, float, float, float, float, float, float, datetime, datetime]:
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        return name, kind, reason, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, start, end

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, start, end = self._validate_restriction(body)
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            rid = cur.lastrowid
            result = dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (rid,)).fetchone())
        self._recalculate("create", rid)
        return result

    def update_restriction(self, restriction_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        name, kind, reason, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, start, end = self._validate_restriction(body)
        with self.repo.tx() as conn:
            row = conn.execute("SELECT * FROM restrictions WHERE id=?", (restriction_id,)).fetchone()
            if not row: raise ApiError(404, "restriction_not_found", "限制不存在")
            if row["status"] == "revoked": raise ApiError(409, "restriction_revoked", "已撤销的限制不能修改")
            if row["revision"] != expected: raise ApiError(409, "revision_conflict", "限制已被其他审核员修改，请刷新后重试")
            new_rev = expected + 1
            conn.execute("""UPDATE restrictions SET name=?,kind=?,min_lon=?,min_lat=?,max_lon=?,max_lat=?,min_altitude=?,max_altitude=?,
                            starts_at=?,ends_at=?,reason=?,revision=? WHERE id=?""",
                         (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, new_rev, restriction_id))
            Repository.audit(conn, None, actor, role, "restriction_updated", {"restriction_id": restriction_id, "from_revision": expected, "to_revision": new_rev})
            result = dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (restriction_id,)).fetchone())
        self._recalculate("update", restriction_id)
        return result

    def revoke_restriction(self, restriction_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            row = conn.execute("SELECT * FROM restrictions WHERE id=?", (restriction_id,)).fetchone()
            if not row: raise ApiError(404, "restriction_not_found", "限制不存在")
            if row["status"] == "revoked": return {"restriction": dict(row), "idempotent": True}
            if row["revision"] != expected: raise ApiError(409, "revision_conflict", "限制已被其他审核员修改，请刷新后重试")
            new_rev = expected + 1
            conn.execute("UPDATE restrictions SET status='revoked',revision=? WHERE id=?", (new_rev, restriction_id))
            Repository.audit(conn, None, actor, role, "restriction_revoked", {"restriction_id": restriction_id, "from_revision": expected, "to_revision": new_rev})
            result = dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (restriction_id,)).fetchone())
        self._recalculate("revoke", restriction_id)
        return {"restriction": result, "idempotent": False}

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}:
            approvals = []
            for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,)):
                d = dict(r); d["snapshot"] = json.loads(d.pop("snapshot_json")) if d.get("snapshot_json") else None
                approvals.append(d)
            result["approvals"] = approvals
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected", "pending_review"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            if report["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            override_kind = "emergency_authority" if report["blocking_conflicts"] else None
            snapshot_json, restriction_links = self._capture_approval_snapshot(conn, plan)
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,snapshot_json,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, snapshot_json, iso()))
            for rid, rjson in restriction_links:
                conn.execute("INSERT OR IGNORE INTO approval_restrictions(approval_id,restriction_id,restriction_json) VALUES(?,?,?)", (cur.lastrowid, rid, rjson))
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", {"override_reason": override, "conflicts": report["blocking_conflicts"]})
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "approved", f"飞行计划 {plan['callsign']} 已批准")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": cur.lastrowid, "override_kind": override_kind}

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            revision = expected + 1
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"]})
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
        return {"expired": len(rows)}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        return {"plans": plans, "restrictions": restrictions, "server_time": iso()}


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: DroneAirspaceService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "drone-airspace"}
        actor, role, operator = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, operator)
        if path == "/api/notifications": return 200, self.service.notifications(actor, role, operator)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "restrictions"] and parts[2].isdigit():
            rid, action = int(parts[2]), parts[3]
            if action == "update": return 200, self.service.update_restriction(rid, actor, role, body)
            if action == "revoke": return 200, self.service.revoke_restriction(rid, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
            }
            if action in routes: return 200, routes[action]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path); send_json(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            send_json(self, exc.status, payload)
        except Exception as exc: print(f"unhandled error: {exc!r}"); send_json(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = DroneAirspaceService(db_path); handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
