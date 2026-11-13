"""认证路由：API Key 登录换取 HMAC token；/me 供前端启动时探测。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import (
    Principal,
    authenticate,
    clear_login_failures,
    issue_token,
    login_allowed,
    record_login_failure,
    require_principal,
)
from app.core.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


class LoginRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    ip = (request.client.host if request.client else "") or "unknown"
    if not login_allowed(ip):
        raise HTTPException(status_code=429, detail="登录尝试过多，请稍后再试")
    principal = authenticate(body.api_key)
    if principal is None:
        record_login_failure(ip)
        raise HTTPException(status_code=401, detail="API Key 无效")
    clear_login_failures(ip)
    return {
        "token": issue_token(principal),
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "expires_in": int(settings.session_ttl_hours) * 3600,
    }


@router.get("/me")
async def me(principal: Principal = Depends(require_principal)):
    return {
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "via": principal.via,
        "auth_enabled": settings.auth_enabled,
    }


@router.post("/logout")
async def logout():
    # 无状态 token：客户端删除即可；保留接口便于前端统一调用
    return {"ok": True}
