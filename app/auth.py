"""令牌与角色依赖。

- 密码使用 PBKDF2-HMAC-SHA256 加盐哈希，业务服务不保存明文密码。
- 令牌为 HS256 签名的 JWT（标准库实现，避免额外依赖，便于 Serverless 部署）。
- 提供 RBAC 依赖与社区数据范围校验。
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Callable

from fastapi import Depends, HTTPException, Request

from .config import settings

# 角色定义（与需求文档 3 节一致）
ROLES = {
    "admin": "系统管理员",
    "operator": "运营人员",
    "maintenance": "维修人员",
    "finance": "财务/审批人员",
    "resident": "居民用户",
}


# ---------- 密码 ----------

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or hashlib.sha256(uuid.uuid4().bytes).digest()[:16]
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(dk_b64)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return hmac.compare_digest(dk, expect)


# ---------- JWT ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_token(payload: dict, ttl: int) -> str:
    header = {"alg": settings.JWT_ALG, "typ": "JWT"}
    body = dict(payload)
    now = int(time.time())
    body.setdefault("iat", now)
    body["exp"] = now + ttl
    body.setdefault("jti", uuid.uuid4().hex)
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(body, separators=(",", ":")).encode())
    sig = hmac.new(settings.JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def decode_token(token: str) -> dict:
    try:
        h, p, s = token.split(".")
        expect = hmac.new(settings.JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expect), s):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(p))
    except Exception:
        raise HTTPException(status_code=401, detail={"code": "TOKEN_INVALID", "message": "令牌无效"})
    if payload.get("exp", 0) < int(time.time()):
        raise HTTPException(status_code=401, detail={"code": "TOKEN_EXPIRED", "message": "令牌已过期"})
    return payload


# ---------- 依赖 ----------

# 已撤销令牌（退出登录后失效），生产环境应放入 Redis
REVOKED_TOKENS: set[str] = set()


def current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "未认证"})
    token = auth[7:]
    if token in REVOKED_TOKENS:
        raise HTTPException(status_code=401, detail={"code": "TOKEN_REVOKED", "message": "令牌已撤销"})
    payload = decode_token(token)
    request.state.token = token
    request.state.trace_id = getattr(request.state, "trace_id", None) or uuid.uuid4().hex
    return payload


def require_roles(*roles: str) -> Callable:
    """RBAC 依赖：仅允许指定角色访问。"""

    def checker(user: dict = Depends(current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权限"})
        return user

    return checker


def check_community_scope(user: dict, community_id: str | None) -> None:
    """服务端再次校验社区数据范围（前端菜单隐藏不能替代后端鉴权）。"""
    if user["role"] in {"admin", "finance"}:
        return
    allowed = set(user.get("communities") or [])
    if community_id and community_id not in allowed:
        raise HTTPException(status_code=403, detail={"code": "SCOPE_DENIED", "message": "超出授权社区范围"})


def scope_filter(user: dict, community_id: str | None) -> str | None:
    """返回查询应强制限定的社区；None 表示不额外限制。"""
    if user["role"] in {"admin", "finance"}:
        return community_id
    allowed = set(user.get("communities") or [])
    if community_id:
        if community_id not in allowed:
            raise HTTPException(status_code=403, detail={"code": "SCOPE_DENIED", "message": "超出授权社区范围"})
        return community_id
    return None
