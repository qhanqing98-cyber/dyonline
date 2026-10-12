"""服务端随机会话鉴权；用户身份只从数据库读取。"""
from urllib.parse import quote
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from app import config
from app.logging_setup import log_context

_PUBLIC_PATHS = {"/login", "/api/login", "/healthz", "/favicon.ico"}


def attach_cookie(response, token):
    response.set_cookie(config.COOKIE_NAME, token, max_age=config.COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/")


def clear_cookie(response):
    response.delete_cookie(config.COOKIE_NAME, path="/", secure=config.COOKIE_SECURE,
                           httponly=True, samesite="lax")


class AuthMiddleware:
    def __init__(self, app, users):
        self.app, self.users = app, users

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope["path"]
        # 同源 Cookie 认证的写操作拒绝显式跨站来源。
        origin = request.headers.get("origin")
        if scope["method"] not in ("GET", "HEAD", "OPTIONS") and (
            request.headers.get("sec-fetch-site") == "cross-site" or
            (origin and origin != str(request.base_url).rstrip("/"))
        ):
            return await JSONResponse({"detail": "不允许跨站操作"}, status_code=403)(scope, receive, send)
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await self.app(scope, receive, send)
        user = self.users.resolve_session(request.cookies.get(config.COOKIE_NAME, ""))
        if user:
            scope.setdefault("state", {})["user"] = user
            with log_context(user_id=user.id):
                return await self.app(scope, receive, send)
        if path.startswith("/api/"):
            response = JSONResponse({"detail": "未登录或登录已失效"}, status_code=401)
        else:
            response = RedirectResponse("/login?next=" + quote(path, safe=""), status_code=302)
        await response(scope, receive, send)
