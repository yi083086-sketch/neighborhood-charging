"""审批与财务：申请、风险评估、审批决策、执行与回执。

禁止重复审批（仅 pending 可决策）与重复执行（执行锁，仅 approved 可执行）。
"""

from fastapi import APIRouter, Depends, Request

from .. import db
from ..auth import current_user, require_roles
from ..models import APPROVAL_TYPES, ApprovalDecisionIn, ApprovalIn
from .common import error, trace_id

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

TYPE_NAMES = {"refund": "退款", "shutdown": "停机", "price_change": "调价",
              "batch_reassign": "批量改派"}

AUTO_RISK = {
    "refund": lambda a: f"涉及金额 {a['amount']} 元，" + ("低风险" if a["amount"] < 50 else "中风险，需核对订单与支付流水"),
    "shutdown": lambda a: "停机影响用户充电体验，需评估影响枪口数与替代方案",
    "price_change": lambda a: "调价影响计费与用户协议，需财务复核",
    "batch_reassign": lambda a: "批量改派影响维修时效与责任归属，需运营负责人确认",
}


def _visible(user: dict, a: dict) -> bool:
    if user["role"] in {"admin", "finance"}:
        return True
    if a["applicant"] == user["username"]:
        return True
    scope = set(user.get("communities") or [])
    return bool(a.get("community_id")) and a["community_id"] in scope


@router.get("")
def list_approvals(status: str | None = None, user: dict = Depends(current_user)):
    items = [a for a in db.store.approvals if _visible(user, a)]
    if status:
        items = [a for a in items if a["status"] == status]
    return {"items": sorted(items, key=lambda a: a["created_at"], reverse=True),
            "type_names": TYPE_NAMES}


@router.post("")
def create_approval(body: ApprovalIn, request: Request,
                    user: dict = Depends(current_user)):
    if body.type not in APPROVAL_TYPES:
        raise error(422, "TYPE_INVALID", "未知审批类型")
    if user["role"] == "maintenance":
        raise error(403, "FORBIDDEN", "维修人员不允许发起审批")
    if user["role"] == "resident" and body.type != "refund":
        raise error(403, "FORBIDDEN", "居民用户仅可申请退款审批")
    tid = trace_id(request)
    with db.store.lock:
        dup = next((a for a in db.store.approvals
                    if a["type"] == body.type and a["target"] == body.target
                    and a["status"] == "pending"), None)
        if dup:
            raise error(409, "DUPLICATE_APPROVAL", f"已存在相同对象的待审批申请 {dup['id']}，禁止重复提交")
        aid = db.store.next_id("A")
        approval = {
            "id": aid, "type": body.type, "target": body.target,
            "community_id": body.community_id, "amount": body.amount,
            "reason": body.reason, "applicant": user["username"],
            "risk_assessment": AUTO_RISK[body.type](body.model_dump()),
            "status": "pending", "approver": None, "decided_at": None,
            "executed_at": None, "receipt": None, "created_at": db.now_iso(),
        }
        db.store.approvals.append(approval)
    db.store.audit(user["username"], user["role"], "approval.created", aid,
                   f"{body.type} target={body.target} amount={body.amount}", tid)
    return {"approval": approval, "trace_id": tid}


@router.post("/{aid}/decide")
def decide(aid: str, body: ApprovalDecisionIn, request: Request,
           user: dict = Depends(require_roles("admin", "finance"))):
    a = next((x for x in db.store.approvals if x["id"] == aid), None)
    if not a:
        raise error(404, "APPROVAL_NOT_FOUND", "审批不存在")
    tid = trace_id(request)
    with db.store.lock:
        if a["status"] != "pending":
            raise error(409, "DUPLICATE_DECISION", f"审批已决策（当前状态 {a['status']}），禁止重复审批")
        a["status"] = "approved" if body.decision == "approved" else "rejected"
        a["approver"] = user["username"]
        a["decided_at"] = db.now_iso()
        a["decision_comment"] = body.comment
    db.store.audit(user["username"], user["role"], f"approval.{a['status']}", aid,
                   body.comment[:64], tid)
    return {"approval": a, "trace_id": tid}


@router.post("/{aid}/execute")
def execute(aid: str, request: Request,
            user: dict = Depends(require_roles("admin", "finance"))):
    """执行已批准的审批（带执行锁），并生成回执。"""
    a = next((x for x in db.store.approvals if x["id"] == aid), None)
    if not a:
        raise error(404, "APPROVAL_NOT_FOUND", "审批不存在")
    tid = trace_id(request)
    with db.store.lock:
        if a["status"] in {"executing", "executed"}:
            raise error(409, "DUPLICATE_EXECUTION", "审批已执行/执行中，禁止重复执行")
        if a["status"] != "approved":
            raise error(409, "STATE_INVALID", "仅已批准的审批可执行")
        a["status"] = "executing"
        receipt = _apply_effects(a)
        a["status"] = "executed"
        a["executed_at"] = db.now_iso()
        a["receipt"] = receipt
    db.store.audit(user["username"], user["role"], "approval.executed", aid,
                   receipt[:64], tid)
    return {"approval": a, "trace_id": tid}


def _apply_effects(a: dict) -> str:
    """按审批类型落地业务效果（演示模式在内存中执行）。"""
    if a["type"] == "shutdown":
        pile = next((p for p in db.store.piles
                     if p["id"] == a["target"] or p["name"] == a["target"]), None)
        if pile:
            pile["status"] = "maintenance"
            for g in db.store.guns:
                if g["pile_id"] == pile["id"]:
                    g["status"] = "offline"
            return f"已将 {pile['name']} 置为检修停机状态"
        return f"停机目标 {a['target']} 已记录，等待设备侧确认"
    if a["type"] == "price_change":
        pile = next((p for p in db.store.piles
                     if p["id"] == a["target"] or p["name"] == a["target"]), None)
        if pile and a["amount"] > 0:
            old = pile["price_kwh"]
            pile["price_kwh"] = a["amount"]
            return f"{pile['name']} 电价 {old} → {a['amount']} 元/kWh"
        return f"调价目标 {a['target']} 已记录"
    if a["type"] == "refund":
        order = next((o for o in db.store.orders if o["id"] == a["target"]), None)
        if order:
            order["history"].append({"status": "refund_executed", "operator": "finance",
                                     "ts": db.now_iso()})
            return f"订单 {order['id']} 退款 {a['amount']} 元已原路退回"
        return f"退款 {a['amount']} 元已提交支付通道"
    return f"批量改派 {a['target']} 已执行"
