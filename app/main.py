# app/main.py
import os
import logging
import re
from contextlib import asynccontextmanager
from contextvars import copy_context
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

import bilibili
import downloader
from app import auth, config
from app.task_store import TaskStore
from app.cleanup import DownloadCleanup, ProtectFileTransfers
from app.logging_setup import (
    configure_logging, RequestLoggingMiddleware, log_context,
    set_log_context, redact, message_level,
)

DOWNLOAD_DIR = config.DOWNLOAD_DIR

configure_logging()
logger = logging.getLogger(__name__)
store = TaskStore()
cleanup = DownloadCleanup(
    DOWNLOAD_DIR, store,
    retention_seconds=int(os.getenv("DOWNLOAD_RETENTION_HOURS", "24")) * 3600,
    interval_seconds=int(os.getenv("DOWNLOAD_CLEANUP_INTERVAL_SECONDS", "3600")),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("服务启动", extra={"event": "service_started", "fields": {"max_concurrency": config.MAX_CONCURRENCY, "queue_limit": config.QUEUE_LIMIT}})
    cleanup.start()
    try:
        yield
    finally:
        logger.info("服务开始关闭", extra={"event": "service_stopping"})
        cleanup.stop()
        # 等下载任务退出后再关数据库，避免后台线程访问已关闭连接。
        _pool.shutdown(wait=True)
        store.close()
        logger.info("服务已关闭", extra={"event": "service_stopped"})


app = FastAPI(title="Video Downloader Web", lifespan=lifespan)
app.add_middleware(ProtectFileTransfers, cleanup=cleanup)
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))

# 静态资源照常挂载。下载文件不再裸挂目录 —— 旧版 /files 不带任何校验，
# 任何人访问 /files/ 就能浏览整个 downloads 目录、下载所有任务的文件；
# 现在改为 /api/tasks/{id}/files/{name} 受控接口（见下方 get_task_file）。
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")

# 访问鉴权中间件（config.ACCESS_TOKEN 为空时内部直接放行，等于关闭）
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# 从下载日志里解析“已保存”的文件路径，用于生成前端下载链接
_SAVED_RE = re.compile(r"\[OK\] 已保存:\s*(.+?)\s*\(\d+ bytes\)")


class TaskRequest(BaseModel):
    # 单任务链接数上限：防止一次灌入海量链接占满队列
    links: list[str] = Field(max_length=config.MAX_LINKS_PER_TASK)

    @field_validator("links")
    @classmethod
    def _check_links(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("链接列表不能为空")
        for link in value:
            if len(link) > config.MAX_LINK_LEN:
                raise ValueError(f"单条链接过长（上限 {config.MAX_LINK_LEN} 字符）")
        return value


# ---------------------------------------------------------------------------
# 并发控制：下载线程池 + 排队计数
# 旧版每个请求无限制开线程，一个脚本就能把服务器内存打爆。
# 现在最多 MAX_CONCURRENCY 个任务真正在下载，其余排队，排满直接返回 429。
# ---------------------------------------------------------------------------
_pool = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENCY, thread_name_prefix="dl")
_pending_lock = threading.Lock()
_pending = 0


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html")


def _run_task(task_id: str) -> None:
    """保护下载全程，并确保提前失败时也释放排队名额。"""
    global _pending
    started = time.monotonic()
    with log_context(task_id=task_id, stage="task"):
        logger.info("任务开始执行", extra={"event": "task_started"})
        try:
            with cleanup.protect():
                _run_task_protected(task_id)
        except Exception as exc:
            store.update(task_id, status="failed", phase="done", error=redact(exc))
            store.append_log(task_id, f"[x] 任务异常: {exc}")
            logger.exception("任务执行异常", extra={"event": "task_exception"})
        finally:
            try:
                task = store.get(task_id)
                logger.log(logging.INFO if task and task.status == "done" else logging.ERROR,
                           "任务执行结束", extra={"event": "task_finished", "fields": {
                               "status": task.status if task else "missing",
                               "duration_ms": round((time.monotonic() - started) * 1000, 2),
                           }})
            finally:
                with _pending_lock:
                    _pending -= 1


def _run_task_protected(task_id: str) -> None:
    """在线程池里执行下载任务，通过回调把进度/日志写回 TaskStore。"""
    task = store.get(task_id)
    if task is None:
        return
    links = list(task.links)

    def log(msg: str, *, exc_info=False) -> None:
        # 文件识别在脱敏前执行；仅日志和对外错误使用脱敏文本。
        m = _SAVED_RE.search(msg)
        if m:
            # 只存文件名；下载走 GET /api/tasks/{id}/files/{name}（带归属校验）
            fname = Path(m.group(1)).name
            store.add_file(task_id, fname)
        store.append_log(task_id, msg)
        logger.log(message_level(msg), msg, exc_info=exc_info, extra={"event": "download_log"})

    def progress(pct: int, phase: str = "") -> None:
        # 进度回调频次很高，写库节流已在 TaskStore.update 内部做掉，这里直接转发
        set_log_context(stage=phase or "download")
        store.update(task_id, progress=pct, phase=phase)

    # 磁盘保护：剩余空间不足直接拒绝，防止把服务器写满导致整个服务挂掉
    free_mb = shutil.disk_usage(DOWNLOAD_DIR).free // (1024 * 1024)
    if free_mb < config.MIN_FREE_MB:
        logger.warning("磁盘不足，拒绝下载", extra={"event": "disk_rejected", "fields": {"disk_free_mb": free_mb, "min_free_mb": config.MIN_FREE_MB}})
        store.update(task_id, status="failed", phase="done",
                     error=f"磁盘剩余空间不足（{free_mb}MB < {config.MIN_FREE_MB}MB），任务拒绝执行")
        return

    session = requests.Session()
    store.update(task_id, status="running", phase="download")

    bili_links = [l for l in links if downloader.detect_platform(l) == "bilibili"]
    dy_links = [l for l in links if downloader.detect_platform(l) != "bilibili"]

    ok = 0
    total = len(links)
    try:
        for link in bili_links:
            set_log_context(platform="bilibili", stage="fetch_detail")
            log(f"[输入] {link[:80]}")
            try:
                if bilibili.process_bilibili(
                        session, link, DOWNLOAD_DIR, log_cb=log, progress_cb=progress):
                    ok += 1
            except Exception as e:
                log(f"[x] 失败: {e}", exc_info=True)

        for link in dy_links:
            set_log_context(platform="douyin", stage="fetch_detail")
            log(f"[输入] {link[:80]}")
            try:
                if downloader.download_douyin(
                        session, link, DOWNLOAD_DIR, log_cb=log, progress_cb=progress):
                    ok += 1
            except Exception as e:
                log(f"[x] 失败: {e}", exc_info=True)

        status = "done" if ok == total else "failed"
        store.update(task_id, status=status, progress=100, phase="done")
        if ok != total:
            store.update(task_id, error=f"仅 {ok}/{total} 个链接下载成功")
    except Exception as e:
        store.update(task_id, status="failed", phase="done", error=redact(e))
        log(f"[x] 任务异常: {e}", exc_info=True)
    finally:
        session.close()


@app.post("/api/tasks")
def create_task(body: TaskRequest):
    global _pending
    links = [l.strip() for l in body.links if l and l.strip()]
    if not links:
        raise HTTPException(status_code=400, detail="链接列表不能为空")
    with _pending_lock:
        if _pending >= config.QUEUE_LIMIT:
            logger.warning("排队任务已满", extra={"event": "queue_rejected", "fields": {"queue_pending": _pending}})
            raise HTTPException(status_code=429,
                                detail="服务器繁忙：排队任务已满，请稍后再试")
        _pending += 1
    try:
        task = store.create(links)
        logger.info("任务已创建", extra={"event": "task_created", "fields": {"task_id": task.id, "link_count": len(links)}})
        context = copy_context()
        _pool.submit(context.run, _run_task, task.id)
    except Exception:
        with _pending_lock:
            _pending -= 1
        raise
    return asdict(task)


@app.get("/api/tasks")
def list_tasks():
    return [asdict(t) for t in store.all()]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return asdict(task)


@app.get("/api/tasks/{task_id}/files/{filename}")
def get_task_file(task_id: str, filename: str):
    """受控下载接口：只允许取该任务自己产出的文件。

    安全三连：归属校验（403）→ 只取 basename 防路径穿越 → 存在性检查（404）。
    """
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    fname = Path(filename).name            # 防 ../../ 类路径穿越
    if fname not in task.files:
        raise HTTPException(status_code=403, detail="该文件不属于此任务")
    path = DOWNLOAD_DIR / fname
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已不存在（可能已被清理）")
    return FileResponse(path, filename=fname, media_type="application/octet-stream")


@app.get("/healthz")
def healthz():
    """探活 + 运行状态，部署时给反向代理 / 监控用。"""
    free_mb = shutil.disk_usage(DOWNLOAD_DIR).free // (1024 * 1024)
    with _pending_lock:
        queued = _pending
    return {
        "status": "ok",
        "disk_free_mb": free_mb,
        "queue_pending": queued,
        "max_concurrency": config.MAX_CONCURRENCY,
    }


# ---------------------------------------------------------------------------
# 登录 / 退出
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    # 未启用鉴权时不该出现登录页，直接回首页，避免把使用者绕晕
    if not auth.enabled():
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html")


class LoginRequest(BaseModel):
    # 限长只是防御性约束，避免超长输入
    token: str = Field(max_length=256)


@app.post("/api/login")
def do_login(body: LoginRequest):
    if not auth.enabled():
        raise HTTPException(status_code=400, detail="服务端未启用访问口令")
    if not auth.check_token(body.token):
        time.sleep(0.8)          # 失败延迟：抬高暴力破解成本
        # 统一文案，不区分具体原因，避免泄露内部信息
        raise HTTPException(status_code=401, detail="口令错误")
    response = JSONResponse({"ok": True})
    auth.attach_cookie(response)
    return response


@app.post("/api/logout")
def do_logout():
    response = JSONResponse({"ok": True})
    auth.clear_cookie(response)
    return response
