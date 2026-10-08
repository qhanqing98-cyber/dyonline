"""下载目录的定期清理；与下载、HTTP 文件传输共用活动保护。"""
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, Thread

logger = logging.getLogger(__name__)
MEDIA_SUFFIXES = {".mp4", ".m4s", ".webm", ".m4a", ".part", ".ytdl"}


class DownloadCleanup:
    def __init__(self, directory: Path, store, retention_seconds: int,
                 interval_seconds: int):
        if retention_seconds < 0 or interval_seconds <= 0:
            raise ValueError("保留时间必须 >= 0，检查间隔必须 > 0")
        self.directory = directory
        self.store = store
        self.retention_seconds = retention_seconds
        self.interval_seconds = interval_seconds
        self.lock = Lock()
        self.active = 0
        self.stop_event = Event()
        self.thread = None

    @contextmanager
    def protect(self):
        """登记正在使用文件；finally 保证失败或断开连接也解除保护。"""
        with self.lock:
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1

    def cleanup_once(self) -> list[str]:
        """仅清理顶层过期媒体文件；忙碌时跳过，不跟随符号链接。"""
        if self.retention_seconds == 0:
            return []
        removed = []
        cutoff = time.time() - self.retention_seconds
        with self.lock:
            if self.active:
                return []
            for path in self.directory.iterdir():
                if path.suffix.lower() not in MEDIA_SUFFIXES:
                    continue
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed.append(path.name)
                    logger.info("已清理过期下载文件: %s", path.name)
                except OSError:
                    logger.exception("清理文件失败，下轮重试: %s", path.name)
            self.store.remove_files(removed)
        return removed

    def _run(self):
        """可中断等待；单轮出错不会让定期清理线程退出。"""
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.cleanup_once()
            except Exception:
                logger.exception("定期清理失败，下轮重试")

    def start(self):
        """随应用启动后台线程；保留时间为 0 时关闭清理。"""
        if self.retention_seconds == 0:
            return
        self.stop_event.clear()
        self.thread = Thread(target=self._run, name="download-cleanup", daemon=True)
        self.thread.start()

    def stop(self):
        """服务关闭时唤醒并等待清理线程退出。"""
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None


class ProtectFileTransfers:
    """ASGI 中间件：保护整个文件响应，而不只是生成响应头的阶段。"""
    def __init__(self, app, cleanup):
        self.app = app
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/files/"):
            with self.cleanup.protect():
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)
