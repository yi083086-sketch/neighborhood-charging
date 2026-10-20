"""路由公共工具：trace_id、幂等键、错误构造。"""

from fastapi import HTTPException, Request

from .. import db


def trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def idempotency_key(request: Request, kind: str, user: dict, required: bool = False):
    """解析 Idempotency-Key。返回 (完整键, 已存在的实体 id 或 None)。"""
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        if required:
            raise error(400, "IDEMPOTENCY_KEY_REQUIRED", "写接口必须携带 Idempotency-Key")
        return None, None
    full = f"{user.get('tenant_id', '')}:{user.get('username', '')}:{kind}:{key}"
    hit = db.store.idempotency.get(full)
    return full, (hit or {}).get("id")


def remember_idempotency(full_key: str | None, entity_id: str) -> None:
    if full_key:
        db.store.idempotency[full_key] = {"id": entity_id}
