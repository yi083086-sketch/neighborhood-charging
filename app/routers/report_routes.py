"""报表、运营指标、审计日志与系统管理。"""

from fastapi import APIRouter, Depends, Request

from .. import db
from ..auth import ROLES, current_user, require_roles, scope_filter
from .common import trace_id

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/dashboard")
def dashboard(community_id: str | None = None, user: dict = Depends(current_user)):
    forced = scope_filter(user, community_id)
    m = db.compute_metrics(forced)
    m["generated_at"] = db.now_iso()
    return m


@router.get("/reports")
def reports(period: str = "day", community_id: str | None = None,
            user: dict = Depends(current_user)):
    """日报/周报与异常复盘。"""
    forced = scope_filter(user, community_id)
    m = db.compute_metrics(forced)
    critical_events = [e for e in db.store.device_events if e["severity"] == "critical"]
    open_p1 = [t for t in db.store.tickets if t["priority"] == "P1" and t["status"] != "closed"]
    return {
        "period": period,
        "generated_at": db.now_iso(),
        "metrics": m,
        "review": {
            "critical_events": critical_events[-10:],
            "open_p1_tickets": open_p1,
            "summary": f"本期严重事件 {len(critical_events)} 起，未关闭 P1 工单 {len(open_p1)} 个",
        },
    }


@router.get("/audits")
def audits(limit: int = 100, user: dict = Depends(require_roles("admin"))):
    items = sorted(db.store.audits, key=lambda a: a["ts"], reverse=True)[:limit]
    return {"items": items}


@router.get("/users")
def users(user: dict = Depends(require_roles("admin"))):
    items = [{"id": u["id"], "username": u["username"], "name": u["name"],
              "role": u["role"], "role_name": ROLES[u["role"]],
              "communities": u["communities"], "status": u["status"],
              "created_at": u["created_at"]} for u in db.store.users]
    return {"items": items, "roles": ROLES}


@router.get("/meta")
def meta(request: Request):
    """前端元信息（演示模式下提供演示账号，便于体验）。"""
    from ..config import settings

    demo_accounts = []
    if settings.DEMO_MODE:
        demo_accounts = [
            {"username": "admin", "password": "admin123", "role": "admin", "role_name": ROLES["admin"]},
            {"username": "ops1", "password": "ops123", "role": "operator", "role_name": ROLES["operator"]},
            {"username": "fix1", "password": "fix123", "role": "maintenance", "role_name": ROLES["maintenance"]},
            {"username": "fin1", "password": "fin123", "role": "finance", "role_name": ROLES["finance"]},
            {"username": "resident1", "password": "user123", "role": "resident", "role_name": ROLES["resident"]},
        ]
    return {"app": "邻里充电运营平台", "mode": "demo" if settings.DEMO_MODE else "production",
            "demo_accounts": demo_accounts, "trace_id": trace_id(request)}
