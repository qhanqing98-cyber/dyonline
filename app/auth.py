# app/auth.py
"""访问鉴权：单共享口令 + HttpOnly Cookie。

设计要点：
- 用「中间件」而不是路由依赖实现：一次拦截页面 / API / 静态资源，不会有漏网路由
- 白名单：登录页、登录接口、静态资源、探活接口（不放行的话登录页自己的 CSS 都会被拦住 → 死锁）
- Cookie 里存 sha256(口令) 而不是明文：Cookie 泄露 ≠ 口令泄露
- 校验用 secrets.compare_digest 常数时间比较，避免时序攻击逐位推导口令
- config.ACCESS_TOKEN 为空 = 完全关闭鉴权（本机开发用）
"""
from __future__ import annotations

import hashlib
from secrets import compare_digest
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import config

# 免登录即可访问的路径 / 前缀
_PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/healthz", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/static/",)


def enabled() -> bool:
    """是否启用了访问鉴权。"""
    return bool(config.ACCESS_TOKEN)


def token_digest(token: str) -> str:
    """口令 → Cookie 中保存的散列值（绝不下发明文口令）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_token(token: str) -> bool:
    """校验用户提交的口令（常数时间比较）。"""
    return bool(config.ACCESS_TOKEN) and compare_digest(
        (token or "").strip(), config.ACCESS_TOKEN
    )


def verify(request: Request) -> bool:
    """校验请求携带的 Cookie 是否有效。"""
    if not enabled():
        return True
    got = request.cookies.get(config.COOKIE_NAME, "")
    return bool(got) and compare_digest(got, token_digest(config.ACCESS_TOKEN))


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def attach_cookie(response) -> None:
    """登录成功后下发 Cookie。"""
    response.set_cookie(
        key=config.COOKIE_NAME,
        value=token_digest(config.ACCESS_TOKEN),
        max_age=config.COOKIE_MAX_AGE,
        httponly=True,                # JS 读不到 → XSS 偷不走
        samesite="lax",               # 跨站请求不带 → 挡住大部分 CSRF
        secure=config.COOKIE_SECURE,  # HTTPS 部署时置 true
        path="/",
    )


def clear_cookie(response) -> None:
    """退出登录：清除 Cookie。"""
    response.delete_cookie(config.COOKIE_NAME, path="/")


class AuthMiddleware(BaseHTTPMiddleware):
    """统一拦截未登录请求：API 回 401 JSON，页面 302 跳登录页。"""

    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path) or verify(request):
            return await call_next(request)

        # API 请求：交给前端处理（前端会跳转登录页）
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "未登录或登录已失效"}, status_code=401)

        # 页面请求：记住原地址，登录后跳回去
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=302)
