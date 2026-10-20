"""预约：查询可用设备、创建/取消预约，防止同一枪口时间冲突。"""

from fastapi import APIRouter, Depends, Request

from .. import db
from ..auth import current_user
from ..models import ReservationIn
from .common import error, idempotency_key, remember_idempotency, trace_id

router = APIRouter(prefix="/api/reservations", tags=["reservations"])


@router.get("")
def list_reservations(user: dict = Depends(current_user)):
    if user["role"] == "resident":
        items = [r for r in db.store.reservations if r["user_id"] == user["sub"]]
    else:
        scope = None if user["role"] in {"admin", "finance"} else set(user.get("communities") or [])
        items = [r for r in db.store.reservations
                 if scope is None or r["community_id"] in scope]
    return {"items": sorted(items, key=lambda r: r["created_at"], reverse=True)}


@router.post("")
def create_reservation(body: ReservationIn, request: Request,
                       user: dict = Depends(current_user)):
    gun = next((g for g in db.store.guns if g["id"] == body.gun_id), None)
    if not gun:
        raise error(404, "GUN_NOT_FOUND", "枪口不存在")
    pile = next(p for p in db.store.piles if p["id"] == gun["pile_id"])
    scope = set(user.get("communities") or [])
    if user["role"] not in {"admin", "finance"} and pile["community_id"] not in scope:
        raise error(403, "SCOPE_DENIED", "超出授权社区范围")
    if gun["status"] in {"fault", "offline"}:
        raise error(409, "GUN_UNAVAILABLE", "该枪口当前不可预约（故障/离线）")

    try:
        start = db.parse_iso(body.start_time)
        end = db.parse_iso(body.end_time)
    except ValueError:
        raise error(422, "TIME_INVALID", "时间格式无效，应为 ISO8601")
    if end <= start:
        raise error(422, "TIME_INVALID", "结束时间必须晚于开始时间")

    full_key, hit = idempotency_key(request, "reservation", user)
    if hit:
        existing = next(r for r in db.store.reservations if r["id"] == hit)
        return {"reservation": existing, "replayed": True, "trace_id": trace_id(request)}

    tid = trace_id(request)
    with db.store.lock:
        conflict = db.find_reservation_conflict(body.gun_id, start, end)
        if conflict:
            db.store.audit(user["username"], user["role"], "reservation.conflict",
                           body.gun_id, f"与 {conflict['id']} 冲突", tid, result="conflict")
            raise error(409, "RESERVATION_CONFLICT",
                        f"该枪口在所选时段已有预约（{conflict['id']}），请更换时间")
        rid = db.store.next_id("R")
        reservation = {
            "id": rid, "user_id": user["sub"], "gun_id": body.gun_id,
            "pile_id": pile["id"], "community_id": pile["community_id"],
            "start_time": start.isoformat(), "end_time": end.isoformat(),
            "status": "active", "note": body.note, "created_at": db.now_iso(),
            "idempotency_key": full_key,
        }
        db.store.reservations.append(reservation)
        remember_idempotency(full_key, rid)
    db.store.audit(user["username"], user["role"], "reservation.created", rid,
                   f"{body.gun_id} {body.start_time}~{body.end_time}", tid)
    return {"reservation": reservation, "replayed": False, "trace_id": tid}


@router.post("/{rid}/cancel")
def cancel_reservation(rid: str, request: Request, user: dict = Depends(current_user)):
    r = next((x for x in db.store.reservations if x["id"] == rid), None)
    if not r:
        raise error(404, "RESERVATION_NOT_FOUND", "预约不存在")
    if user["role"] == "resident" and r["user_id"] != user["sub"]:
        raise error(403, "FORBIDDEN", "只能取消本人预约")
    if r["status"] != "active":
        raise error(409, "STATE_INVALID", "仅生效中的预约可取消")
    tid = trace_id(request)
    with db.store.lock:
        r["status"] = "cancelled"
    db.store.audit(user["username"], user["role"], "reservation.cancelled", rid, "", tid)
    return {"reservation": r, "trace_id": tid}
