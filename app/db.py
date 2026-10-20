"""数据访问层。

- DEMO_MODE=true：使用进程内存储并注入演示数据，可部署在 Netlify 等无数据库环境。
- DEMO_MODE=false：要求配置 MYSQL_URL（身份中心）与 POSTGRES_URL（业务库），
  健康检查会探测连通性；数据库故障时接口返回可识别错误码 DB_UNAVAILABLE。
- 所有写操作在锁内完成，保证事务性与幂等判断的原子性。
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone

from .auth import hash_password
from .config import settings


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ts(offset_minutes: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).replace(
        microsecond=0
    ).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Store:
    """进程内存储（演示模式）。结构与 database/init.sql 中的表一一对应。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    # ---------- 生命周期 ----------

    def reset(self) -> None:
        with self.lock:
            self.tenants: list[dict] = []
            self.users: list[dict] = []
            self.communities: list[dict] = []
            self.zones: list[dict] = []
            self.piles: list[dict] = []
            self.guns: list[dict] = []
            self.device_events: list[dict] = []
            self.reservations: list[dict] = []
            self.orders: list[dict] = []
            self.tickets: list[dict] = []
            self.approvals: list[dict] = []
            self.audits: list[dict] = []
            self.agent_runs: list[dict] = []
            self.memory_short: dict[str, list[dict]] = {}
            self.memory_long: list[dict] = []
            self.login_failures: dict[str, dict] = {}
            self.idempotency: dict[str, dict] = {}
            self.refresh_tokens: set[str] = set()
            self._seq = 1000
            if settings.DEMO_MODE:
                self._seed()

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._seq += 1
            return f"{prefix}-{self._seq}"

    # ---------- 审计 ----------

    def audit(self, user: str, role: str, action: str, obj: str, detail: str = "",
              trace_id: str = "", result: str = "ok") -> None:
        with self.lock:
            self.audits.append({
                "ts": now_iso(), "user": user, "role": role, "action": action,
                "object": obj, "detail": detail, "trace_id": trace_id, "result": result,
            })

    # ---------- 种子数据 ----------

    def _seed(self) -> None:
        self.tenants.append({"id": "t1", "name": "邻里充电", "status": "active"})

        for cid, name in [("c1", "阳光花园"), ("c2", "滨江苑"), ("c3", "翠湖湾")]:
            self.communities.append({"id": cid, "tenant_id": "t1", "name": name,
                                     "address": f"{name}小区", "status": "active"})
        self.zones += [
            {"id": "z1a", "community_id": "c1", "name": "地下车库A区"},
            {"id": "z1b", "community_id": "c1", "name": "地面停车区"},
            {"id": "z2a", "community_id": "c2", "name": "地下车库A区"},
            {"id": "z3a", "community_id": "c3", "name": "地面停车区"},
        ]

        piles = [
            ("p1", "c1", "z1a", "CP-A01", "online", 7.0),
            ("p2", "c1", "z1a", "CP-A02", "online", 7.0),
            ("p3", "c1", "z1b", "CP-B01", "fault", 7.0),
            ("p4", "c2", "z2a", "CP-A01", "online", 11.0),
            ("p5", "c2", "z2a", "CP-A02", "offline", 11.0),
            ("p6", "c3", "z3a", "CP-C01", "online", 7.0),
        ]
        for pid, cid, zid, name, status, kw in piles:
            self.piles.append({
                "id": pid, "tenant_id": "t1", "community_id": cid, "zone_id": zid,
                "name": name, "status": status, "power_kw": kw,
                "price_kwh": 1.2, "price_hour": 0.5, "created_at": _ts(-60 * 24 * 30),
            })
            for no in (1, 2):
                self.guns.append({
                    "id": f"g{pid[1:]}_{no}", "pile_id": pid, "connector_no": no,
                    "status": "available",
                })

        # 预置一些枪口状态
        gun_status = {"g3_1": "fault", "g3_2": "fault", "g5_1": "offline",
                      "g5_2": "offline", "g1_1": "charging"}
        for g in self.guns:
            if g["id"] in gun_status:
                g["status"] = gun_status[g["id"]]

        users = [
            ("admin", "系统管理员", "admin", ["c1", "c2", "c3"]),
            ("ops1", "张运营", "operator", ["c1", "c2"]),
            ("fix1", "李维修", "maintenance", ["c1", "c2", "c3"]),
            ("fin1", "王财务", "finance", ["c1", "c2", "c3"]),
            ("resident1", "陈先生", "resident", ["c1"]),
            ("resident2", "刘女士", "resident", ["c2"]),
        ]
        passwords = {"admin": "admin123", "ops1": "ops123", "fix1": "fix123",
                     "fin1": "fin123", "resident1": "user123", "resident2": "user123"}
        for uname, name, role, comms in users:
            self.users.append({
                "id": f"u_{uname}", "tenant_id": "t1", "username": uname,
                "name": name, "role": role, "communities": comms,
                "password_hash": hash_password(passwords[uname]),
                "status": "active", "created_at": _ts(-60 * 24 * 60),
            })

        # 设备事件：心跳（保证验收用桩 p3 心跳新鲜）+ 故障 + 离线
        for pid in ("p1", "p2", "p3", "p4", "p6"):
            self.device_events.append({
                "event_no": f"E-HB-{pid}", "pile_id": pid, "type": "heartbeat",
                "severity": "info", "payload": {"soc_sample": 0}, "ts": _ts(-5),
            })
        self.device_events += [
            {"event_no": "E-1002", "pile_id": "p3", "type": "temperature",
             "severity": "critical", "payload": {"temp_c": 86.5, "gun": 1}, "ts": _ts(-180)},
            {"event_no": "E-1003", "pile_id": "p5", "type": "offline",
             "severity": "warn", "payload": {"last_seen_minutes": 40}, "ts": _ts(-120)},
            {"event_no": "E-1004", "pile_id": "p4", "type": "charge_interrupted",
             "severity": "warn", "payload": {"order_id": "O-1002"}, "ts": _ts(-90)},
        ]

        # 预约
        self.reservations += [
            {"id": "R-1001", "user_id": "u_resident1", "gun_id": "g2_1", "pile_id": "p2",
             "community_id": "c1", "start_time": _ts(60), "end_time": _ts(120),
             "status": "active", "note": "", "created_at": _ts(-30),
             "idempotency_key": "seed-r1"},
            {"id": "R-1002", "user_id": "u_resident1", "gun_id": "g1_2", "pile_id": "p1",
             "community_id": "c1", "start_time": _ts(-60 * 24), "end_time": _ts(-60 * 24 + 60),
             "status": "completed", "note": "", "created_at": _ts(-60 * 25),
             "idempotency_key": "seed-r2"},
            {"id": "R-1003", "user_id": "u_resident2", "gun_id": "g4_1", "pile_id": "p4",
             "community_id": "c2", "start_time": _ts(-60 * 26), "end_time": _ts(-60 * 26 + 90),
             "status": "cancelled", "note": "行程变更", "created_at": _ts(-60 * 27),
             "idempotency_key": "seed-r3"},
        ]

        # 订单
        orders = [
            ("O-1001", "u_resident1", "g1_2", "p1", "c1", "completed", 32.4, 27.0, False, "R-1002"),
            ("O-1002", "u_resident2", "g4_1", "p4", "c2", "charging", 12.0, 10.0, False, None),
            ("O-1003", "u_resident1", "g2_1", "p2", "c1", "pending_payment", 0.0, 0.0, False, "R-1001"),
            ("O-1004", "u_resident2", "g4_2", "p4", "c2", "cancelled", 0.0, 0.0, False, None),
            ("O-1005", "u_admin", "g3_1", "p3", "c1", "completed", 1.2, 1.0, True, None),
            ("O-1006", "u_resident1", "g1_1", "p1", "c1", "abnormal", 8.8, 7.3, False, None),
        ]
        for oid, uid, gid, pid, cid, status, amount, kwh, test, rid in orders:
            self.orders.append({
                "id": oid, "user_id": uid, "gun_id": gid, "pile_id": pid,
                "community_id": cid, "reservation_id": rid, "status": status,
                "amount": amount, "kwh": kwh, "test_order": test,
                "created_at": _ts(-60 * 24), "updated_at": _ts(-60),
                "idempotency_key": f"seed-{oid}",
                "history": [{"status": status, "operator": uid, "ts": _ts(-60)}],
            })

        # 工单：T-1001 已维修待验收（p3 心跳新鲜 + 存在测试订单 O-1005，可演示关闭）
        self.tickets += [
            {"id": "T-1001", "priority": "P1", "pile_id": "p3", "community_id": "c1",
             "title": "枪口温度异常", "description": "1号枪温度 86.5℃，触发高温告警",
             "source": "event", "source_event_no": "E-1002", "status": "pending_acceptance",
             "assignee": "fix1", "dispatch_note": "优先处理", "repair_note": "更换温控传感器并复检",
             "created_at": _ts(-170), "updated_at": _ts(-20), "closed_at": None,
             "heartbeat_checked_at": None, "test_order_id": None,
             "history": [
                 {"status": "created", "operator": "system", "ts": _ts(-170)},
                 {"status": "dispatched", "operator": "ops1", "ts": _ts(-160)},
                 {"status": "accepted", "operator": "fix1", "ts": _ts(-150)},
                 {"status": "processing", "operator": "fix1", "ts": _ts(-140)},
                 {"status": "pending_acceptance", "operator": "fix1", "ts": _ts(-20)},
             ]},
            {"id": "T-1002", "priority": "P2", "pile_id": "p5", "community_id": "c2",
             "title": "充电桩离线", "description": "CP-A02 离线超过 30 分钟",
             "source": "event", "source_event_no": "E-1003", "status": "pending_dispatch",
             "assignee": None, "dispatch_note": "", "repair_note": "",
             "created_at": _ts(-110), "updated_at": _ts(-110), "closed_at": None,
             "heartbeat_checked_at": None, "test_order_id": None,
             "history": [{"status": "created", "operator": "system", "ts": _ts(-110)}]},
        ]

        # 审批：一条已执行退款（报表用），一条待审批停机申请
        self.approvals += [
            {"id": "A-1001", "type": "refund", "target": "O-1006", "community_id": "c1",
             "amount": 8.8, "reason": "充电异常中断，用户申请退款", "applicant": "resident1",
             "risk_assessment": "金额小于 50 元，低风险", "status": "executed",
             "approver": "fin1", "decided_at": _ts(-60 * 20), "executed_at": _ts(-60 * 19),
             "receipt": "退款已原路退回", "created_at": _ts(-60 * 22)},
            {"id": "A-1002", "type": "shutdown", "target": "p5", "community_id": "c2",
             "amount": 0.0, "reason": "设备离线且存在安全隐患，申请停机检修",
             "applicant": "ops1", "risk_assessment": "影响 2 个枪口，建议尽快审批",
             "status": "pending", "approver": None, "decided_at": None,
             "executed_at": None, "receipt": None, "created_at": _ts(-50)},
        ]

        self.audits.append({
            "ts": _ts(-60 * 22), "user": "system", "role": "system", "action": "seed",
            "object": "demo", "detail": "演示数据初始化", "trace_id": "", "result": "ok",
        })
        # 避免与种子数据 ID（O-1006/T-1002/A-1002…）冲突
        self._seq = 2000


store = Store()


# ---------- 业务规则辅助 ----------

def overlaps(r1_start: datetime, r1_end: datetime, r2_start: datetime, r2_end: datetime) -> bool:
    return r1_start < r2_end and r2_start < r1_end


def find_reservation_conflict(gun_id: str, start: datetime, end: datetime,
                              exclude_id: str | None = None) -> dict | None:
    for r in store.reservations:
        if r["gun_id"] != gun_id or r["id"] == exclude_id:
            continue
        if r["status"] not in {"active"}:
            continue
        if overlaps(start, end, parse_iso(r["start_time"]), parse_iso(r["end_time"])):
            return r
    return None


def latest_heartbeat(pile_id: str) -> dict | None:
    events = [e for e in store.device_events if e["pile_id"] == pile_id and e["type"] == "heartbeat"]
    return max(events, key=lambda e: e["ts"], default=None)


def heartbeat_is_fresh(pile_id: str) -> bool:
    hb = latest_heartbeat(pile_id)
    if not hb:
        return False
    age = datetime.now(timezone.utc) - parse_iso(hb["ts"])
    return age <= timedelta(minutes=settings.HEARTBEAT_FRESH_MINUTES)


def compute_metrics(community_id: str | None = None) -> dict:
    """运营指标：设备可用率、利用率、故障率、平均修复时长、预约成功率、收入、退款、社区对比。"""

    def in_scope(cid: str) -> bool:
        return community_id is None or cid == community_id

    piles = [p for p in store.piles if in_scope(p["community_id"])]
    pile_ids = {p["id"] for p in piles}
    guns = [g for g in store.guns if g["pile_id"] in pile_ids]
    orders = [o for o in store.orders if in_scope(o["community_id"])]
    reservations = [r for r in store.reservations if in_scope(r["community_id"])]
    tickets = [t for t in store.tickets if in_scope(t["community_id"])]

    total_piles = len(piles)
    online = sum(1 for p in piles if p["status"] == "online")
    fault = sum(1 for p in piles if p["status"] == "fault")
    charging_guns = sum(1 for g in guns if g["status"] == "charging")

    closed = [t for t in tickets if t["status"] == "closed" and t.get("closed_at")]
    mttr_hours = None
    if closed:
        deltas = [(parse_iso(t["closed_at"]) - parse_iso(t["created_at"])).total_seconds() / 3600
                  for t in closed]
        mttr_hours = round(sum(deltas) / len(deltas), 1)

    finished_res = [r for r in reservations if r["status"] in {"completed", "active", "cancelled"}]
    success_res = [r for r in finished_res if r["status"] in {"completed", "active"}]
    revenue = round(sum(o["amount"] for o in orders if o["status"] == "completed"), 2)
    refund = round(sum(a["amount"] for a in store.approvals
                       if a["type"] == "refund" and a["status"] == "executed"
                       and in_scope(a.get("community_id") or "")), 2)

    per_community = []
    for c in store.communities:
        if community_id and c["id"] != community_id:
            continue
        m = compute_metrics(c["id"]) if community_id is None else None
        cp = [p for p in store.piles if p["community_id"] == c["id"]]
        online_c = sum(1 for p in cp if p["status"] == "online")
        orders_c = [o for o in store.orders if o["community_id"] == c["id"]]
        tickets_c = [t for t in store.tickets if t["community_id"] == c["id"]]
        per_community.append({
            "community_id": c["id"],
            "community_name": c["name"],
            "piles": len(cp),
            "availability": round(online_c / len(cp), 3) if cp else None,
            "revenue": round(sum(o["amount"] for o in orders_c if o["status"] == "completed"), 2),
            "open_tickets": sum(1 for t in tickets_c if t["status"] != "closed"),
        })

    return {
        "device_availability": round(online / total_piles, 3) if total_piles else None,
        "utilization": round(charging_guns / len(guns), 3) if guns else None,
        "fault_rate": round(fault / total_piles, 3) if total_piles else None,
        "mttr_hours": mttr_hours,
        "reservation_success_rate": round(len(success_res) / len(finished_res), 3) if finished_res else None,
        "revenue": revenue,
        "refund_amount": refund,
        "orders_total": len(orders),
        "open_tickets": sum(1 for t in tickets if t["status"] != "closed"),
        "piles_total": total_piles,
        "piles_online": online,
        "piles_fault": fault,
        "per_community": per_community,
    }


def check_external_services() -> dict:
    """健康检查：演示模式返回内存存储状态；正式模式探测数据库连通性。"""
    if settings.DEMO_MODE:
        return {"mode": "demo", "memory_store": "ok",
                "mysql": "skipped", "postgres": "skipped", "redis": "skipped"}
    result: dict = {"mode": "production"}
    try:
        import pymysql  # type: ignore

        from urllib.parse import urlparse

        u = urlparse(settings.MYSQL_URL)
        conn = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username,
                               password=u.password, connect_timeout=3)
        conn.close()
        result["mysql"] = "ok"
    except Exception as exc:  # 数据库故障时返回可识别错误并记录日志
        result["mysql"] = f"error: {exc.__class__.__name__}"
    result["postgres"] = "configured" if settings.POSTGRES_URL else "missing"
    result["redis"] = "configured" if settings.REDIS_URL else "missing"
    return result
