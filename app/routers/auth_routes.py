"""身份与组织：登录、令牌续期、退出、当前用户。"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from .. import db
from ..auth import (REVOKED_TOKENS, create_token, current_user, decode_token,
                    verify_password)
from ..config import settings
from ..models import LoginRequest, RefreshRequest
from .common import error, trace_id

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login_locked(username: str) -> bool:
    rec = db.store.login_failures.get(username)
    if not rec or not rec.get("locked_until"):
        return False
    return datetime.fromisoformat(rec["locked_until"]) > datetime.now(timezone.utc)


def _record_failure(username: str) -> None:
    rec = db.store.login_failures.setdefault(username, {"count": 0, "locked_until": None})
    rec["count"] += 1
    if rec["count"] >= settings.LOGIN_MAX_FAILS:
        from ..db import _ts

        rec["locked_until"] = _ts(settings.LOGIN_LOCK_SECONDS / 60)
        rec["count"] = 0


def _public_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "name": u["name"],
            "role": u["role"], "tenant_id": u["tenant_id"],
            "communities": u["communities"]}


@router.post("/login")
def login(body: LoginRequest, request: Request):
    if _login_locked(body.username):
        raise error(429, "LOGIN_LOCKED", "登录失败次数过多，账号已临时锁定，请稍后再试")
    user = next((u for u in db.store.users if u["username"] == body.username), None)
    if not user or not verify_password(body.password, user["password_hash"]):
        _record_failure(body.username)
        db.store.audit(body.username, "-", "login.failed", "auth", "用户名或密码错误",
                       trace_id(request), result="fail")
        raise error(401, "BAD_CREDENTIALS", "用户名或密码错误")
    db.store.login_failures.pop(body.username, None)
    claims = {"sub": user["id"], "username": user["username"], "role": user["role"],
              "tenant_id": user["tenant_id"], "communities": user["communities"]}
    access = create_token({**claims, "type": "access"}, settings.ACCESS_TOKEN_TTL)
    refresh = create_token({**claims, "type": "refresh"}, settings.REFRESH_TOKEN_TTL)
    db.store.refresh_tokens.add(refresh)
    db.store.audit(user["username"], user["role"], "login", "auth", "", trace_id(request))
    return {"access_token": access, "refresh_token": refresh,
            "token_type": "bearer", "expires_in": settings.ACCESS_TOKEN_TTL,
            "user": _public_user(user), "trace_id": trace_id(request)}


@router.post("/refresh")
def refresh(body: RefreshRequest, request: Request):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh" or body.refresh_token not in db.store.refresh_tokens:
        raise error(401, "REFRESH_INVALID", "刷新令牌无效")
    db.store.refresh_tokens.discard(body.refresh_token)
    claims = {k: payload[k] for k in ("sub", "username", "role", "tenant_id", "communities")}
    access = create_token({**claims, "type": "access"}, settings.ACCESS_TOKEN_TTL)
    new_refresh = create_token({**claims, "type": "refresh"}, settings.REFRESH_TOKEN_TTL)
    db.store.refresh_tokens.add(new_refresh)
    return {"access_token": access, "refresh_token": new_refresh,
            "token_type": "bearer", "expires_in": settings.ACCESS_TOKEN_TTL,
            "trace_id": trace_id(request)}


@router.post("/logout")
def logout(request: Request, user: dict = Depends(current_user)):
    REVOKED_TOKENS.add(request.state.token)
    db.store.audit(user["username"], user["role"], "logout", "auth", "", trace_id(request))
    return {"ok": True, "trace_id": trace_id(request)}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    u = next((x for x in db.store.users if x["id"] == user["sub"]), None)
    if not u:
        raise error(401, "USER_NOT_FOUND", "用户不存在")
    return {"user": _public_user(u),
            "community_names": {c["id"]: c["name"] for c in db.store.communities}}
