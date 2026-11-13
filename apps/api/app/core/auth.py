"""API Key 认证与租户解析。

- API_KEYS 格式：key1:tenant-a:user-a,key2:tenant-b:user-b
- 登录返回 HMAC 签名 token（无状态，无需用户表），前端存 localStorage 并放 Authorization
- AUTH_LOCALHOST_BYPASS=true 时回环地址按默认租户放行（本地开发/评测）
- 生产/反代部署必须 AUTH_LOCALHOST_BYPASS=false，否则代理后的请求会被误判为本机
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

from app.core.config import BASE_DIR, get_settings

settings = get_settings()

_SECRET_FILE = BASE_DIR / ".session_secret"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    via: str = "api_key"  # api_key / localhost / disabled


def _load_secret() -> str:
    if settings.session_secret.strip():
        return settings.session_secret.strip()
    try:
        if _SECRET_FILE.exists():
            return _SECRET_FILE.read_text(encoding="utf-8").strip()
        value = secrets.token_hex(32)
        _SECRET_FILE.write_text(value, encoding="utf-8")
        return value
    except OSError:
        # 只读文件系统（容器）下退回进程内随机值：重启后 token 失效，但不影响鉴权
        return secrets.token_hex(32)


_SECRET = _load_secret()


def parse_api_keys(raw: str) -> dict[str, tuple[str, str]]:
    """key -> (tenant_id, user_id)。非法条目跳过。"""
    result: dict[str, tuple[str, str]] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        key = parts[0].strip()
        if not key:
            continue
        tenant = (parts[1] if len(parts) > 1 else settings.default_tenant_id).strip()
        user = (parts[2] if len(parts) > 2 else settings.default_user_id).strip()
        result[key] = (tenant or settings.default_tenant_id, user or settings.default_user_id)
    return result


_API_KEYS = parse_api_keys(settings.api_keys)

# 登录失败限流（进程内，足够挡住脚本爆破；多副本需换 Redis）
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_WINDOW = 60.0
_LOGIN_MAX_FAILS = 10


def login_allowed(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILS[ip] = recent
    return len(recent) < _LOGIN_MAX_FAILS


def record_login_failure(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


def clear_login_failures(ip: str) -> None:
    _LOGIN_FAILS.pop(ip, None)


def issue_token(principal: Principal) -> str:
    payload = {
        "sub": principal.user_id,
        "tenant": principal.tenant_id,
        "exp": int(time.time()) + int(settings.session_ttl_hours) * 3600,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str) -> Principal | None:
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(
            _SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        tenant = str(payload.get("tenant") or "").strip()
        user = str(payload.get("sub") or "").strip()
        if not tenant or not user:
            return None
        return Principal(user_id=user, tenant_id=tenant)
    except Exception:
        return None


def authenticate(api_key: str) -> Principal | None:
    candidate = api_key.strip()
    if not candidate:
        return None
    for key, (tenant, user) in _API_KEYS.items():
        if hmac.compare_digest(key, candidate):
            return Principal(user_id=user, tenant_id=tenant)
    return None


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    header_key = (request.headers.get("x-api-key") or "").strip()
    if header_key:
        return header_key
    # EventSource 不能带 header；仅 SSE 路径允许 query token（前端优先用 fetch+header）
    if request.url.path.endswith("/stream"):
        return (request.query_params.get("token") or "").strip()
    return ""


def resolve_request(request: Request) -> Principal | None:
    """中间件与依赖共用的解析逻辑。返回 None 表示未认证。"""
    if not settings.auth_enabled:
        return Principal(
            user_id=settings.default_user_id,
            tenant_id=settings.default_tenant_id,
            via="disabled",
        )
    if settings.auth_localhost_bypass:
        host = (request.client.host if request.client else "") or ""
        if host in _LOOPBACK:
            return Principal(
                user_id=settings.default_user_id,
                tenant_id=settings.default_tenant_id,
                via="localhost",
            )
    token = _bearer(request)
    if not token:
        return None
    principal = verify_token(token)
    if principal is not None:
        return principal
    # 兼容直接把 API Key 放在 Authorization/X-API-Key 的脚本调用
    return authenticate(token)


async def require_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        principal = resolve_request(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="未认证：请在 Authorization 中提供 Bearer token 或 X-API-Key")
    return principal
