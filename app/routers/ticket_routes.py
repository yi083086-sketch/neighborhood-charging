"""工单与维修：分派、接单、处理中、待验收、关闭。

关闭前必须完成：1) 设备心跳检查（新鲜窗口内） 2) 测试订单验证。
"""

from fastapi import APIRouter, Depends, Request

from .. import db
from ..auth import current_user, require_roles
from ..config import settings
from ..models import TicketCreateIn, TicketDispatchIn, TicketProcessIn
from .common import error, trace_id

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

FLOW = ["pending_dispatch", "dispatched", "accepted", "processing",
        "pending_acceptance", "closed"]


def _get_ticket(tid: str) -> dict:
    t = next((x for x in db.store.tickets if x["id"] == tid), None)
    if not t:
        raise error(404, "TICKET_NOT_FOUND", "工单不存在")
    return t


def _move(t: dict, status: str, operator: str) -> None:
    if FLOW.index(status) != FLOW.index(t["status"]) + 1:
        raise error(409, "STATE_INVALID",
                    f"工单状态 {t['status']} 不能变更为 {status}（须按流程推进）")
    t["status"] = status
    t["updated_at"] = db.now_iso()
    t["history"].append({"status": status, "operator": operator, "ts": db.now_iso()})


def _visible(user: dict, t: dict) -> bool:
    if user["role"] == "resident":
        return t.get("creator") == user["sub"]
    if user["role"] in {"admin", "finance"}:
        return True
    return t["community_id"] in set(user.get("communities") or [])


@router.get("")
def list_tickets(status: str | None = None, user: dict = Depends(current_user)):
    items = [t for t in db.store.tickets if _visible(user, t)]
    if status:
        items = [t for t in items if t["status"] == status]
    return {"items": sorted(items, key=lambda t: t["created_at"], reverse=True)}


@router.post("")
def create_ticket(body: TicketCreateIn, request: Request,
                  user: dict = Depends(current_user)):
    """手动创建工单；居民创建视为投诉（P3）。"""
    pile = next((p for p in db.store.piles if p["id"] == body.pile_id), None)
    if not pile:
        raise error(404, "PILE_NOT_FOUND", "充电桩不存在")
    if user["role"] == "resident":
        scope = set(user.get("communities") or [])
        if pile["community_id"] not in scope:
            raise error(403, "SCOPE_DENIED", "超出授权社区范围")
        priority, source = "P3", "complaint"
    else:
        priority, source = body.priority, body.source
    tid = trace_id(request)
    with db.store.lock:
        ticket_id = db.store.next_id("T")
        ticket = {
            "id": ticket_id, "priority": priority, "pile_id": pile["id"],
            "community_id": pile["community_id"], "title": body.title,
            "description": body.description, "source": source, "source_event_no": None,
            "status": "pending_dispatch", "assignee": None, "dispatch_note": "",
            "repair_note": "", "created_at": db.now_iso(), "updated_at": db.now_iso(),
            "closed_at": None, "heartbeat_checked_at": None, "test_order_id": None,
            "creator": user["sub"],
            "history": [{"status": "created", "operator": user["username"], "ts": db.now_iso()}],
        }
        db.store.tickets.append(ticket)
    db.store.audit(user["username"], user["role"], "ticket.created", ticket_id,
                   f"{priority} {body.title}", tid)
    return {"ticket": ticket, "trace_id": tid}


@router.post("/{tid}/dispatch")
def dispatch(tid: str, body: TicketDispatchIn, request: Request,
             user: dict = Depends(require_roles("admin", "operator"))):
    t = _get_ticket(tid)
    if not _visible(user, t):
        raise error(403, "SCOPE_DENIED", "超出授权社区范围")
    if t["status"] != "pending_dispatch":
        raise error(409, "STATE_INVALID", "仅待分派工单可分派")
    assignee = next((u for u in db.store.users
                     if u["username"] == body.assignee and u["role"] == "maintenance"), None)
    if not assignee:
        raise error(422, "ASSIGNEE_INVALID", "分派对象必须是维修人员")
    rtrace = trace_id(request)
    with db.store.lock:
        t["assignee"] = body.assignee
        t["dispatch_note"] = body.note
        _move(t, "dispatched", user["username"])
    db.store.audit(user["username"], user["role"], "ticket.dispatched", tid,
                   f"assignee={body.assignee}", rtrace)
    return {"ticket": t, "trace_id": rtrace}


@router.post("/{tid}/accept")
def accept(tid: str, request: Request,
           user: dict = Depends(require_roles("admin", "maintenance"))):
    t = _get_ticket(tid)
    if t["status"] != "dispatched":
        raise error(409, "STATE_INVALID", "仅已分派工单可接单")
    if user["role"] == "maintenance" and t["assignee"] != user["username"]:
        raise error(403, "FORBIDDEN", "只能接收分派给本人的工单")
    rtrace = trace_id(request)
    with db.store.lock:
        _move(t, "accepted", user["username"])
    db.store.audit(user["username"], user["role"], "ticket.accepted", tid, "", rtrace)
    return {"ticket": t, "trace_id": rtrace}


@router.post("/{tid}/start")
def start(tid: str, request: Request,
          user: dict = Depends(require_roles("admin", "maintenance"))):
    t = _get_ticket(tid)
    if t["status"] != "accepted":
        raise error(409, "STATE_INVALID", "仅已接单工单可开始处理")
    rtrace = trace_id(request)
    with db.store.lock:
        _move(t, "processing", user["username"])
    db.store.audit(user["username"], user["role"], "ticket.processing", tid, "", rtrace)
    return {"ticket": t, "trace_id": rtrace}


@router.post("/{tid}/resolve")
def resolve(tid: str, body: TicketProcessIn, request: Request,
            user: dict = Depends(require_roles("admin", "maintenance"))):
    """维修回填，进入待验收。"""
    t = _get_ticket(tid)
    if t["status"] != "processing":
        raise error(409, "STATE_INVALID", "仅处理中工单可回填维修结果")
    rtrace = trace_id(request)
    with db.store.lock:
        t["repair_note"] = body.repair_note
        _move(t, "pending_acceptance", user["username"])
    db.store.audit(user["username"], user["role"], "ticket.pending_acceptance", tid,
                   body.repair_note[:64], rtrace)
    return {"ticket": t, "trace_id": rtrace}


@router.post("/{tid}/check-heartbeat")
def check_heartbeat(tid: str, request: Request,
                    user: dict = Depends(require_roles("admin", "maintenance", "operator"))):
    t = _get_ticket(tid)
    fresh = db.heartbeat_is_fresh(t["pile_id"])
    hb = db.latest_heartbeat(t["pile_id"])
    rtrace = trace_id(request)
    with db.store.lock:
        if fresh:
            t["heartbeat_checked_at"] = db.now_iso()
    db.store.audit(user["username"], user["role"], "ticket.heartbeat_check", tid,
                   "通过" if fresh else "未通过", rtrace)
    return {"ok": fresh, "last_heartbeat": hb["ts"] if hb else None,
            "window_minutes": settings.HEARTBEAT_FRESH_MINUTES, "trace_id": rtrace}


@router.post("/{tid}/close")
def close(tid: str, request: Request,
          user: dict = Depends(require_roles("admin", "maintenance", "operator"))):
    """关闭工单：必须完成心跳检查与测试订单验证（验收门禁）。"""
    t = _get_ticket(tid)
    if t["status"] != "pending_acceptance":
        raise error(409, "STATE_INVALID", "仅待验收工单可关闭")
    checks = {
        "heartbeat": db.heartbeat_is_fresh(t["pile_id"]),
        "test_order": any(o["pile_id"] == t["pile_id"] and o["test_order"]
                          and o["status"] == "completed" for o in db.store.orders),
    }
    if not all(checks.values()):
        missing = [k for k, v in checks.items() if not v]
        raise error(409, "ACCEPTANCE_INCOMPLETE",
                    f"验收未完成：{'、'.join('心跳检查' if m == 'heartbeat' else '测试订单验证' for m in missing)}未通过")
    rtrace = trace_id(request)
    with db.store.lock:
        t["heartbeat_checked_at"] = t["heartbeat_checked_at"] or db.now_iso()
        _move(t, "closed", user["username"])
        t["closed_at"] = db.now_iso()
        # 恢复设备可用
        pile = next((p for p in db.store.piles if p["id"] == t["pile_id"]), None)
        if pile and pile["status"] in {"fault", "offline", "maintenance"}:
            pile["status"] = "online"
            for g in db.store.guns:
                if g["pile_id"] == pile["id"] and g["status"] in {"fault", "offline"}:
                    g["status"] = "available"
    db.store.audit(user["username"], user["role"], "ticket.closed", tid,
                   "验收通过（心跳+测试订单）", rtrace)
    return {"ticket": t, "checks": checks, "trace_id": rtrace}
