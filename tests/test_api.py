"""验收测试：认证、RBAC、租户隔离、预约冲突、订单幂等、事件去重、
故障升级、工单验收、审批拦截与 Agent 工具规范。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_store():
    db.store.reset()
    yield


def login(username: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- 认证 ----------

def test_login_success_and_me():
    data = login("admin", "admin123")
    assert data["access_token"] and data["user"]["role"] == "admin"
    me = client.get("/api/auth/me", headers=auth(data["access_token"]))
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin"


def test_login_wrong_password():
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "BAD_CREDENTIALS"


def test_login_rate_limit_lock():
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "ops1", "password": "bad"})
    r = client.post("/api/auth/login", json={"username": "ops1", "password": "ops123"})
    assert r.status_code == 429


def test_unauthenticated_access():
    assert client.get("/api/orders").status_code == 401


def test_logout_revokes_token():
    data = login("admin", "admin123")
    h = auth(data["access_token"])
    assert client.post("/api/auth/logout", headers=h).status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401


# ---------- RBAC 与数据隔离 ----------

def test_resident_cannot_read_others_orders():
    data = login("resident1", "user123")
    r = client.get("/api/orders", headers=auth(data["access_token"]))
    orders = r.json()["items"]
    assert orders and all(o["user_id"] == "u_resident1" for o in orders)
    # 直接读取他人订单被拒
    assert client.get("/api/orders/O-1002", headers=auth(data["access_token"])).status_code == 403


def test_role_menu_permissions():
    resident = login("resident1", "user123")
    assert client.get("/api/approvals", headers=auth(resident["access_token"])).status_code in (200,)
    # 居民不允许发起停机审批
    r = client.post("/api/approvals", headers=auth(resident["access_token"]),
                    json={"type": "shutdown", "target": "p1", "reason": "test"})
    assert r.status_code == 403
    # 维修人员不能决策审批
    fix = login("fix1", "fix123")
    r = client.post("/api/approvals/A-1002/decide", headers=auth(fix["access_token"]),
                    json={"decision": "approved"})
    assert r.status_code == 403
    # 仅管理员可访问审计与用户管理
    ops = login("ops1", "ops123")
    assert client.get("/api/audits", headers=auth(ops["access_token"])).status_code == 403
    admin = login("admin", "admin123")
    assert client.get("/api/audits", headers=auth(admin["access_token"])).status_code == 200
    assert client.get("/api/users", headers=auth(admin["access_token"])).status_code == 200


def test_operator_community_scope_isolation():
    """ops1 仅授权 c1/c2，不能看到 c3（翠湖湾）设备。"""
    data = login("ops1", "ops123")
    h = auth(data["access_token"])
    piles = client.get("/api/piles", headers=h).json()["items"]
    assert piles and all(p["community_id"] in {"c1", "c2"} for p in piles)
    assert client.get("/api/piles/p6", headers=h).status_code == 403
    assert client.get("/api/piles?community_id=c3", headers=h).status_code == 403


# ---------- 预约冲突 ----------

def test_reservation_conflict():
    data = login("resident1", "user123")
    h = {**auth(data["access_token"]), "Idempotency-Key": uuid.uuid4().hex}
    body = {"gun_id": "g2_2", "start_time": "2026-12-01T10:00:00+00:00",
            "end_time": "2026-12-01T12:00:00+00:00"}
    r1 = client.post("/api/reservations", headers=h, json=body)
    assert r1.status_code == 200
    # 同一枪口重叠时段 → 409
    h2 = {**auth(data["access_token"]), "Idempotency-Key": uuid.uuid4().hex}
    body2 = dict(body, start_time="2026-12-01T11:00:00+00:00",
                 end_time="2026-12-01T13:00:00+00:00")
    r2 = client.post("/api/reservations", headers=h2, json=body2)
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "RESERVATION_CONFLICT"


def test_reservation_idempotent_replay():
    data = login("resident1", "user123")
    key = uuid.uuid4().hex
    h = {**auth(data["access_token"]), "Idempotency-Key": key}
    body = {"gun_id": "g2_2", "start_time": "2026-12-02T10:00:00+00:00",
            "end_time": "2026-12-02T11:00:00+00:00"}
    r1 = client.post("/api/reservations", headers=h, json=body)
    r2 = client.post("/api/reservations", headers=h, json=body)
    assert r1.json()["reservation"]["id"] == r2.json()["reservation"]["id"]
    assert r2.json()["replayed"] is True


# ---------- 订单幂等 ----------

def test_order_idempotency_key_required():
    data = login("resident1", "user123")
    r = client.post("/api/orders", headers=auth(data["access_token"]),
                    json={"gun_id": "g2_2"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_order_idempotent_replay():
    data = login("resident1", "user123")
    key = uuid.uuid4().hex
    h = {**auth(data["access_token"]), "Idempotency-Key": key}
    before = len(db.store.orders)
    r1 = client.post("/api/orders", headers=h, json={"gun_id": "g2_2"})
    r2 = client.post("/api/orders", headers=h, json={"gun_id": "g2_2"})
    assert r1.json()["order"]["id"] == r2.json()["order"]["id"]
    assert len(db.store.orders) == before + 1


# ---------- 事件去重与故障升级 ----------

def test_event_dedup_no_duplicate_ticket():
    data = login("ops1", "ops123")
    h = auth(data["access_token"])
    event = {"event_no": "E-TEST-1", "pile_id": "p2", "type": "temperature",
             "severity": "critical", "payload": {"temp_c": 90}}
    r1 = client.post("/api/device-events", headers=h, json=event)
    assert r1.status_code == 200 and r1.json()["ticket_id"]
    tickets_after_first = len(db.store.tickets)
    r2 = client.post("/api/device-events", headers=h, json=event)
    assert r2.json()["duplicate"] is True
    assert len(db.store.tickets) == tickets_after_first


def test_fault_escalation_priority():
    data = login("ops1", "ops123")
    h = auth(data["access_token"])
    # critical 温度 → P1
    client.post("/api/device-events", headers=h, json={
        "event_no": "E-P1-1", "pile_id": "p1", "type": "temperature",
        "severity": "critical", "payload": {"temp_c": 95}})
    t1 = next(t for t in db.store.tickets if t["source_event_no"] == "E-P1-1")
    assert t1["priority"] == "P1"
    # warn 离线 → P2（p4 在 ops1 授权的 c2 范围内）
    client.post("/api/device-events", headers=h, json={
        "event_no": "E-P2-1", "pile_id": "p4", "type": "offline",
        "severity": "warn", "payload": {}})
    t2 = next(t for t in db.store.tickets if t["source_event_no"] == "E-P2-1")
    assert t2["priority"] == "P2"


# ---------- 工单验收门禁 ----------

def test_ticket_close_requires_acceptance():
    ops = auth(login("ops1", "ops123")["access_token"])
    fix = auth(login("fix1", "fix123")["access_token"])
    # T-1002 处于待分派：直接关闭应被拒
    assert client.post("/api/tickets/T-1002/close", headers=fix).status_code == 409
    # 推进 T-1002：分派 → 接单 → 处理 → 回填
    assert client.post("/api/tickets/T-1002/dispatch", headers=ops,
                       json={"assignee": "fix1"}).status_code == 200
    assert client.post("/api/tickets/T-1002/accept", headers=fix).status_code == 200
    assert client.post("/api/tickets/T-1002/start", headers=fix).status_code == 200
    assert client.post("/api/tickets/T-1002/resolve", headers=fix,
                       json={"repair_note": "重启通信模块"}).status_code == 200
    # p5 无新鲜心跳 → 关闭被验收门禁拦截
    r = client.post("/api/tickets/T-1002/close", headers=fix)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ACCEPTANCE_INCOMPLETE"
    # 补心跳 + 测试订单后可关闭（T-1001 已具备条件）
    assert client.post("/api/tickets/T-1001/close", headers=fix).status_code == 200
    pile = next(p for p in db.store.piles if p["id"] == "p3")
    assert pile["status"] == "online"


# ---------- 审批拦截 ----------

def test_approval_no_duplicate_decision_or_execution():
    fin = auth(login("fin1", "fin123")["access_token"])
    r1 = client.post("/api/approvals/A-1002/decide", headers=fin,
                     json={"decision": "approved"})
    assert r1.status_code == 200
    # 重复审批被拒
    r2 = client.post("/api/approvals/A-1002/decide", headers=fin,
                     json={"decision": "rejected"})
    assert r2.status_code == 409
    # 执行一次成功，重复执行被拒
    assert client.post("/api/approvals/A-1002/execute", headers=fin).status_code == 200
    r3 = client.post("/api/approvals/A-1002/execute", headers=fin)
    assert r3.status_code == 409
    # 停机效果生效
    assert next(p for p in db.store.piles if p["id"] == "p5")["status"] == "maintenance"


def test_duplicate_pending_approval_rejected():
    ops = auth(login("ops1", "ops123")["access_token"])
    body = {"type": "shutdown", "target": "p4", "reason": "检修"}
    assert client.post("/api/approvals", headers=ops, json=body).status_code == 200
    r = client.post("/api/approvals", headers=ops, json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DUPLICATE_APPROVAL"


# ---------- Agent ----------

def test_agent_readonly_query_returns_trace_and_sources():
    ops = auth(login("ops1", "ops123")["access_token"])
    r = client.post("/api/agent/chat", headers=ops,
                    json={"message": "今天各社区设备可用率和收入对比如何？"})
    assert r.status_code == 200
    data = r.json()
    assert data["trace_id"] and data["status"] == "success"
    assert data["risk_level"] == "read"
    assert data["data_sources"]
    assert data["answer"]


def test_agent_high_risk_creates_approval_not_execute():
    ops = auth(login("ops1", "ops123")["access_token"])
    before = len(db.store.approvals)
    r = client.post("/api/agent/chat", headers=ops,
                    json={"message": "滨江苑 CP-A02 有安全隐患，申请停机"})
    data = r.json()
    assert data["status"] == "approval_created"
    assert data["approval_id"]
    assert len(db.store.approvals) == before + 1
    created = next(a for a in db.store.approvals if a["id"] == data["approval_id"])
    assert created["status"] == "pending"  # 未执行，等待人工决策
    # 设备不应被直接停机（p5 即滨江苑 CP-A02，仍保持原状态）
    assert next(p for p in db.store.piles if p["id"] == "p5")["status"] == "offline"


def test_agent_resident_scope_denied():
    res = auth(login("resident1", "user123")["access_token"])
    r = client.post("/api/agent/chat", headers=res,
                    json={"message": "申请对 CP-A01 停机"})
    assert r.json()["status"] == "denied"


def test_agent_stream_sse():
    ops = auth(login("ops1", "ops123")["access_token"])
    with client.stream("POST", "/api/agent/chat", headers=ops,
                       json={"message": "设备状态", "stream": True}) as resp:
        chunks = [line for line in resp.iter_lines() if line.startswith("data:")]
    assert chunks[-1] == "data: [DONE]"
    assert any('"final"' in c for c in chunks)


# ---------- 健康检查 ----------

def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
