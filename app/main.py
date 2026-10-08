# app/main.py
import os
import re
from contextlib import asynccontextmanager
import threading
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import bilibili
import downloader
from app.task_store import TaskStore
from app.cleanup import DownloadCleanup, ProtectFileTransfers

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

store = TaskStore()
cleanup = DownloadCleanup(
    DOWNLOAD_DIR, store,
    retention_seconds=int(os.getenv("DOWNLOAD_RETENTION_HOURS", "24")) * 3600,
    interval_seconds=int(os.getenv("DOWNLOAD_CLEANUP_INTERVAL_SECONDS", "3600")),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup.start()
    try:
        yield
    finally:
        cleanup.stop()


app = FastAPI(title="Video Downloader Web", lifespan=lifespan)
app.add_middleware(ProtectFileTransfers, cleanup=cleanup)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/files", StaticFiles(directory=str(DOWNLOAD_DIR)), name="files")

# 从下载日志里解析“已保存”的文件路径，用于生成前端下载链接
_SAVED_RE = re.compile(r"\[OK\] 已保存:\s*(.+?)\s*\(\d+ bytes\)")


class TaskRequest(BaseModel):
    links: list[str]


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html")


def _run_task(task_id: str) -> None:
    # 下载全程受保护；不持有锁进行网络请求，多个下载仍可并发。
    with cleanup.protect():
        _run_task_protected(task_id)


def _run_task_protected(task_id: str) -> None:
    """在后台线程里执行下载任务，通过回调把进度/日志写回 TaskStore。"""
    task = store.get(task_id)
    if task is None:
        return
    links = list(task.links)

    def log(msg: str) -> None:
        store.append_log(task_id, msg)
        m = _SAVED_RE.search(msg)
        if m:
            fname = Path(m.group(1)).name
            store.add_file(task_id, f"/files/{quote(fname)}")

    def progress(pct: int, phase: str = "") -> None:
        store.update(task_id, progress=pct, phase=phase)

    session = requests.Session()
    store.update(task_id, status="running", phase="download")

    bili_links = [l for l in links if downloader.detect_platform(l) == "bilibili"]
    dy_links = [l for l in links if downloader.detect_platform(l) != "bilibili"]

    ok = 0
    total = len(links)
    try:
        for link in bili_links:
            log(f"[输入] {link[:80]}")
            try:
                if bilibili.process_bilibili(
                        session, link, DOWNLOAD_DIR, log_cb=log, progress_cb=progress):
                    ok += 1
            except Exception as e:
                log(f"[x] 失败: {e}")

        for link in dy_links:
            log(f"[输入] {link[:80]}")
            try:
                if downloader.download_douyin(
                        session, link, DOWNLOAD_DIR, log_cb=log, progress_cb=progress):
                    ok += 1
            except Exception as e:
                log(f"[x] 失败: {e}")

        status = "done" if ok == total else "failed"
        store.update(task_id, status=status, progress=100, phase="done")
        if ok != total:
            store.update(task_id, error=f"仅 {ok}/{total} 个链接下载成功")
    except Exception as e:
        store.update(task_id, status="failed", phase="done", error=str(e))
        log(f"[x] 任务异常: {e}")
    finally:
        session.close()


@app.post("/api/tasks")
def create_task(body: TaskRequest):
    links = [l.strip() for l in body.links if l and l.strip()]
    if not links:
        raise HTTPException(status_code=400, detail="链接列表不能为空")
    task = store.create(links)
    threading.Thread(target=_run_task, args=(task.id,), daemon=True).start()
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