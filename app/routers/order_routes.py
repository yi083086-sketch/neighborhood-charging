"""订单：状态机（待支付/已预约/充电中/已完成/已取消/异常），幂等创建，状态变化留痕。"""

from fastapi import APIRouter, Depends, Request

from .. import db
from ..auth import current_user
from ..models import OrderIn
from .common import error, idempotency_key, remember_idempotency, trace_id

router = APIRouter(prefix="/api/orders", tags=["orders"])

# 允许的状态迁移
TRANSITIONS = {
    "pending_payment": {"charging", "cancelled"},
    "reserved": {"charging", "cancelled"},
    "charging": {"completed", "abnormal"},
    "completed": set(),
    "cancelled": set(),
    "abnormal": {"completed", "cancelled"},
}


def _change_status(order: dict, status: str, operator: str) -> None:
    if status not in TRANSITIONS[order["status"]]:
        raise error(409, "STATE_INVALID",
                    f"订单状态 {order['status']} 不允许变更为 {status}")
    order["status"] = status
    order["updated_at"] = db.now_iso()
    order["history"].append({"status": status, "operator": operator, "ts": db.now_iso()})


@router.get("")
def list_orders(status: str | None = None, user: dict = Depends(current_user)):
    if user["role"] == "resident":
        items = [o for o in db.store.orders if o["user_id"] == user["sub"]]
    else:
        scope = None if user["role"] in {"admin", "finance"} else set(user.get("communities") or [])
        items = [o for o in db.store.orders if scope is None or o["community_id"] in scope]
    if status:
        items = [o for o in items if o["status"] == status]
    return {"items": sorted(items, key=lambda o: o["created_at"], reverse=True)}


@router.get("/{oid}")
def get_order(oid: str, user: dict = Depends(current_user)):
    o = next((x for x in db.store.orders if x["id"] == oid), None)
    if not o:
        raise error(404, "ORDER_NOT_FOUND", "订单不存在")
    if user["role"] == "resident" and o["user_id"] != user["sub"]:
        raise error(403, "FORBIDDEN", "无权查看他人订单")
    return {"order": o}


@router.post("")
def create_order(body: OrderIn, request: Request, user: dict = Depends(current_user)):
    """创建订单：必须携带 Idempotency-Key，重复提交返回原订单。"""
    gun = next((g for g in db.store.guns if g["id"] == body.gun_id), None)
    if not gun:
        raise error(404, "GUN_NOT_FOUND", "枪口不存在")
    pile = next(p for p in db.store.piles if p["id"] == gun["pile_id"])
    scope = set(user.get("communities") or [])
    if user["role"] not in {"admin", "finance", "maintenance"} and pile["community_id"] not in scope:
        raise error(403, "SCOPE_DENIED", "超出授权社区范围")

    reservation = None
    if body.reservation_id:
        reservation = next((r for r in db.store.reservations
                            if r["id"] == body.reservation_id), None)
        if not reservation or reservation["gun_id"] != body.gun_id:
            raise error(422, "RESERVATION_MISMATCH", "预约不存在或与枪口不匹配")
        if reservation["user_id"] != user["sub"] and user["role"] == "resident":
            raise error(403, "FORBIDDEN", "只能使用本人预约创建订单")

    full_key, hit = idempotency_key(request, "order", user, required=True)
    if hit:
        existing = next(o for o in db.store.orders if o["id"] == hit)
        return {"order": existing, "replayed": True, "trace_id": trace_id(request)}

    tid = trace_id(request)
    with db.store.lock:
        oid = db.store.next_id("O")
        status = "completed" if body.test_order else "pending_payment"
        order = {
            "id": oid, "user_id": user["sub"], "gun_id": body.gun_id,
            "pile_id": pile["id"], "community_id": pile["community_id"],
            "reservation_id": body.reservation_id, "status": status,
            "amount": body.amount, "kwh": 0.0, "test_order": body.test_order,
            "created_at": db.now_iso(), "updated_at": db.now_iso(),
            "idempotency_key": full_key,
            "history": [{"status": status, "operator": user["username"], "ts": db.now_iso()}],
        }
        db.store.orders.append(order)
        if reservation:
            reservation["status"] = "completed"
        if body.test_order:
            order["history"].append({"status": "test_verified", "operator": "system",
                                     "ts": db.now_iso()})
        remember_idempotency(full_key, oid)
    db.store.audit(user["username"], user["role"], "order.created", oid,
                   f"gun={body.gun_id} test={body.test_order}", tid)
    return {"order": order, "replayed": False, "trace_id": tid}


@router.post("/{oid}/pay")
def pay_order(oid: str, request: Request, user: dict = Depends(current_user)):
    o = next((x for x in db.store.orders if x["id"] == oid), None)
    if not o:
        raise error(404, "ORDER_NOT_FOUND", "订单不存在")
    if user["role"] == "resident" and o["user_id"] != user["sub"]:
        raise error(403, "FORBIDDEN", "只能支付本人订单")
    tid = trace_id(request)
    with db.store.lock:
        _change_status(o, "charging", user["username"])
        if not o["amount"]:
            pile = next(p for p in db.store.piles if p["id"] == o["pile_id"])
            o["amount"] = round(pile["price_kwh"] * 10, 2)
            o["kwh"] = 10.0
    db.store.audit(user["username"], user["role"], "order.paid", oid, "", tid)
    return {"order": o, "trace_id": tid}


@router.post("/{oid}/complete")
def complete_order(oid: str, request: Request, user: dict = Depends(current_user)):
    if user["role"] not in {"admin", "operator", "maintenance"}:
        raise error(403, "FORBIDDEN", "无权限")
    o = next((x for x in db.store.orders if x["id"] == oid), None)
    if not o:
        raise error(404, "ORDER_NOT_FOUND", "订单不存在")
    tid = trace_id(request)
    with db.store.lock:
        _change_status(o, "completed", user["username"])
    db.store.audit(user["username"], user["role"], "order.completed", oid, "", tid)
    return {"order": o, "trace_id": tid}


@router.post("/{oid}/cancel")
def cancel_order(oid: str, request: Request, user: dict = Depends(current_user)):
    o = next((x for x in db.store.orders if x["id"] == oid), None)
    if not o:
        raise error(404, "ORDER_NOT_FOUND", "订单不存在")
    if user["role"] == "resident" and o["user_id"] != user["sub"]:
        raise error(403, "FORBIDDEN", "只能取消本人订单")
    tid = trace_id(request)
    with db.store.lock:
        _change_status(o, "cancelled", user["username"])
    db.store.audit(user["username"], user["role"], "order.cancelled", oid, "", tid)
    return {"order": o, "trace_id": tid}
