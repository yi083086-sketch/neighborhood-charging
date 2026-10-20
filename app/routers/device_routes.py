"""设备与事件：社区、区域、充电桩、枪口、设备事件（幂等去重 + 故障自动建单）。"""

from fastapi import APIRouter, Depends, Request

from .. import db
from ..auth import current_user, require_roles, scope_filter
from ..models import EVENT_TYPES, DeviceEventIn
from .common import error, trace_id

router = APIRouter(prefix="/api", tags=["devices"])

# 故障事件 → 工单优先级（告警升级）
FAULT_PRIORITY = {
    ("temperature", "critical"): "P1",
    ("overload", "critical"): "P1",
    ("fault", "critical"): "P1",
    ("fault", "warn"): "P2",
    ("offline", "warn"): "P2",
    ("offline", "critical"): "P1",
    ("charge_interrupted", "warn"): "P2",
}


@router.get("/communities")
def list_communities(user: dict = Depends(current_user)):
    scope = set(user.get("communities") or [])
    items = [c for c in db.store.communities
             if user["role"] in {"admin", "finance"} or c["id"] in scope]
    return {"items": items, "trace_id": ""}


@router.get("/zones")
def list_zones(community_id: str | None = None, user: dict = Depends(current_user)):
    scope_filter(user, community_id)  # 越权社区直接 403
    scope = None if user["role"] in {"admin", "finance"} else set(user.get("communities") or [])
    items = [z for z in db.store.zones
             if (scope is None or z["community_id"] in scope)
             and (community_id is None or z["community_id"] == community_id)]
    return {"items": items}


def _pile_view(p: dict) -> dict:
    guns = [g for g in db.store.guns if g["pile_id"] == p["id"]]
    return {**p, "guns": guns,
            "gun_count": len(guns),
            "available_guns": sum(1 for g in guns if g["status"] == "available")}


@router.get("/piles")
def list_piles(community_id: str | None = None, status: str | None = None,
               user: dict = Depends(current_user)):
    scope_filter(user, community_id)  # 越权社区直接 403
    scope = None if user["role"] in {"admin", "finance"} else set(user.get("communities") or [])
    items = []
    for p in db.store.piles:
        if scope is not None and p["community_id"] not in scope:
            continue
        if community_id and p["community_id"] != community_id:
            continue
        if status and p["status"] != status:
            continue
        items.append(_pile_view(p))
    return {"items": items, "total": len(items)}


@router.get("/piles/{pile_id}")
def get_pile(pile_id: str, user: dict = Depends(current_user)):
    pile = next((p for p in db.store.piles if p["id"] == pile_id), None)
    if not pile:
        raise error(404, "PILE_NOT_FOUND", "充电桩不存在")
    scope_filter(user, pile["community_id"])
    events = sorted((e for e in db.store.device_events if e["pile_id"] == pile_id),
                    key=lambda e: e["ts"], reverse=True)[:20]
    tickets = [t for t in db.store.tickets if t["pile_id"] == pile_id]
    return {"pile": _pile_view(pile), "recent_events": events, "tickets": tickets}


@router.get("/device-events")
def list_events(pile_id: str | None = None, type: str | None = None, limit: int = 50,
                user: dict = Depends(current_user)):
    scope = None if user["role"] in {"admin", "finance"} else set(user.get("communities") or [])
    pile_community = {p["id"]: p["community_id"] for p in db.store.piles}
    items = []
    for e in sorted(db.store.device_events, key=lambda x: x["ts"], reverse=True):
        if scope is not None and pile_community.get(e["pile_id"]) not in scope:
            continue
        if pile_id and e["pile_id"] != pile_id:
            continue
        if type and e["type"] != type:
            continue
        items.append(e)
        if len(items) >= limit:
            break
    return {"items": items}


@router.post("/device-events")
def ingest_event(body: DeviceEventIn, request: Request,
                 user: dict = Depends(require_roles("admin", "operator", "maintenance"))):
    """设备事件接入：事件编号唯一去重；故障事件自动创建 P1/P2/P3 工单。"""
    if body.type not in EVENT_TYPES:
        raise error(422, "EVENT_TYPE_INVALID", f"未知事件类型：{body.type}")
    pile = next((p for p in db.store.piles if p["id"] == body.pile_id), None)
    if not pile:
        raise error(404, "PILE_NOT_FOUND", "充电桩不存在")
    scope_filter(user, pile["community_id"])

    tid = trace_id(request)
    with db.store.lock:
        existing = next((e for e in db.store.device_events
                         if e["event_no"] == body.event_no), None)
        if existing:
            # 幂等：重复事件不重复建单
            db.store.audit(user["username"], user["role"], "device_event.duplicate",
                           body.event_no, "重复事件已去重", tid)
            return {"duplicate": True, "event": existing, "ticket_id": None, "trace_id": tid}

        event = {"event_no": body.event_no, "pile_id": body.pile_id, "type": body.type,
                 "severity": body.severity, "payload": body.payload, "ts": db.now_iso()}
        db.store.device_events.append(event)

        ticket_id = None
        priority = FAULT_PRIORITY.get((body.type, body.severity))
        if priority and not any(t["source_event_no"] == body.event_no for t in db.store.tickets):
            ticket_id = db.store.next_id("T")
            title_map = {"temperature": "温度异常", "overload": "过载告警", "fault": "设备故障",
                         "offline": "充电桩离线", "charge_interrupted": "充电中断"}
            db.store.tickets.append({
                "id": ticket_id, "priority": priority, "pile_id": pile["id"],
                "community_id": pile["community_id"],
                "title": f"{title_map.get(body.type, body.type)}（{pile['name']}）",
                "description": f"事件 {body.event_no}：{body.type}，payload={body.payload}",
                "source": "event", "source_event_no": body.event_no,
                "status": "pending_dispatch", "assignee": None, "dispatch_note": "",
                "repair_note": "", "created_at": db.now_iso(), "updated_at": db.now_iso(),
                "closed_at": None, "heartbeat_checked_at": None, "test_order_id": None,
                "history": [{"status": "created", "operator": "system", "ts": db.now_iso()}],
            })
            if body.type in {"fault", "temperature", "overload"}:
                pile["status"] = "fault"
                for g in db.store.guns:
                    if g["pile_id"] == pile["id"]:
                        g["status"] = "fault"
            elif body.type == "offline":
                pile["status"] = "offline"
                for g in db.store.guns:
                    if g["pile_id"] == pile["id"]:
                        g["status"] = "offline"
            db.store.audit("system", "system", "ticket.auto_created", ticket_id,
                           f"{priority}，来源事件 {body.event_no}", tid)

    db.store.audit(user["username"], user["role"], "device_event.ingested",
                   body.event_no, f"type={body.type}", tid)
    return {"duplicate": False, "event": event, "ticket_id": ticket_id, "trace_id": tid}
