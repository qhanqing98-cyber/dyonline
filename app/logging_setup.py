"""统一 JSON 日志、敏感值脱敏和请求/后台任务上下文。"""
import hashlib
import json
import logging
import re
import sys
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

_context = ContextVar("log_context", default={})
logger = logging.getLogger(__name__)
_URL = re.compile(r"https?://[^\s<>\"']+")
_HEADER = re.compile(r"(?im)(?:cookie|set-cookie|authorization|proxy-authorization)[\"']?\s*:\s*[^\r\n]+")
_SECRET_PAIR = re.compile(
    r"(?i)\b(access_token|password|passwd|secret|token|cookie|authorization|"
    r"sessdata|bili_jct|ttwid|mstoken|s_v_web_id|sessionid|sid_tt|signature|"
    r"x-bogus|x-gnarly|api_key)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)


def redact(value):
    """输出边界统一脱敏；原始下载数据不修改。"""
    text = str(value)
    from app import config
    secrets = set()
    for key in ("ACCESS_TOKEN", "ADMIN_PASSWORD", "DOUYIN_COOKIE", "BILI_COOKIE"):
        secret = getattr(config, key, "")
        if secret:
            secrets.add(secret)
            if key in ("ACCESS_TOKEN", "ADMIN_PASSWORD"):
                secrets.add(hashlib.sha256(secret.encode()).hexdigest())
            else:
                secrets.update(part.partition("=")[2].strip()
                               for part in secret.split(";") if "=" in part)
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        for variant in {secret, quote(secret, safe=""), json.dumps(secret, ensure_ascii=False)[1:-1]}:
            if len(variant) < 4:
                text = re.sub(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", "[REDACTED]", text)
            else:
                text = text.replace(variant, "[REDACTED]")

    def safe_url(match):
        try:
            url = urlsplit(match.group())
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path,
                               "[REDACTED]" if url.query else "", ""))
        except ValueError:
            return "[REDACTED_URL]"

    text = _URL.sub(safe_url, text)
    text = _HEADER.sub(lambda m: m.group().split(":", 1)[0] + ": [REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", text)
    return _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)


@contextmanager
def log_context(**fields):
    """上下文仅在当前请求/任务有效，finally 避免线程复用时串号。"""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def set_log_context(**fields):
    _context.set({**_context.get(), **fields})


def message_level(message):
    if message.startswith(("[x]", "ERROR:")):
        return logging.ERROR
    if message.startswith(("[!]", "[提示]", "WARNING:")):
        return logging.WARNING
    return logging.INFO


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname, "module": record.name,
            "event": getattr(record, "event", "application_log"),
            "message": redact(record.getMessage()),
        }
        fields = {**_context.get(), **getattr(record, "fields", {})}
        for key, value in fields.items():
            result[key] = value if isinstance(value, (int, float, bool)) or value is None else redact(value)
        if record.exc_info and record.exc_info[0] is not None:
            result["error_type"] = record.exc_info[0].__name__
            result["traceback"] = redact("".join(traceback.format_exception(*record.exc_info)))
        return json.dumps(result, ensure_ascii=False, default=str)


def configure_logging(level=None):
    """仅管理一个 stdout handler；反复初始化不会重复记录。"""
    from app import config
    name = str(level if level is not None else config.LOG_LEVEL).upper()
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
              "WARN": logging.WARNING, "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
              "FATAL": logging.CRITICAL, "NOTSET": logging.NOTSET}
    invalid = name not in levels
    root = logging.getLogger()
    # 接管已有配置，统一 Uvicorn 与应用格式；外部测试捕获 handler 不删除。
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not getattr(handler, "_log_capture", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(levels.get(name, logging.INFO))
    for name_ in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name_)
        uv.handlers.clear()
        uv.propagate = True
        uv.disabled = False
        uv.setLevel(logging.NOTSET)
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    if invalid:
        logger.warning("无效 LOG_LEVEL，使用 INFO", extra={"event": "logging_config_invalid"})


class RequestLoggingMiddleware:
    """纯 ASGI：从响应开始到流式响应结束均在同一个请求上下文中。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = None
        completed = False
        disconnected = False
        failed = False

        async def receive_logged():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def send_logged(message):
            nonlocal status, completed, disconnected
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message["headers"] = headers + [(b"x-request-id", request_id.encode())]
            try:
                await send(message)
            except OSError:
                disconnected = True
                raise
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
            if message["type"] == "http.response.pathsend":
                completed = True

        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive_logged, send_logged)
            except BaseException as exc:
                failed = True
                if type(exc).__name__ == "CancelledError":
                    disconnected = True
                logger.log(logging.WARNING if disconnected else logging.ERROR, "请求未完成", exc_info=True,
                             extra={"event": "request_exception", "fields": {"method": scope["method"], "path": scope["path"]}})
                raise
            finally:
                effective_status = status if status is not None else (500 if failed and not disconnected else None)
                quiet = scope["path"] == "/healthz" or scope["path"].startswith("/static/")
                level = (logging.ERROR if (failed and not disconnected) or (effective_status or 0) >= 500 else
                         logging.WARNING if disconnected or not completed or (effective_status or 0) >= 400 else
                         logging.DEBUG if quiet else logging.INFO)
                logger.log(level, "HTTP 请求结束", extra={"event": "http_request", "fields": {
                    "method": scope["method"], "path": scope["path"], "status_code": effective_status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "completed": completed, "disconnected": disconnected,
                    "user_id": getattr(scope.get("state", {}).get("user"), "id", None),
                }})
