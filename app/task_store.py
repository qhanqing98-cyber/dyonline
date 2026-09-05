# app/task_store.py
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from uuid import uuid4


@dataclass
class DownloadTask:
    # 每个任务都有一个唯一 id，前端后面用它查询进度
    id: str

    # 用户提交的视频链接列表
    links: list[str]

    # 任务状态：queued 排队中，running 运行中，done 完成，failed 失败
    status: str = "queued"

    # 下载进度，0 到 100
    progress: int = 0

    # 当前阶段，比如 queued、download、done
    phase: str = "queued"

    # 后端运行时产生的日志
    logs: list[str] = field(default_factory=list)

    # 下载完成后的文件链接
    files: list[str] = field(default_factory=list)

    # 失败原因
    error: str | None = None

    # 创建时间，方便以后排查任务
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class TaskStore:
    # 这是一个内存版任务仓库，服务重启后数据会丢
    def __init__(self):
        self.tasks = {}
        self.lock = Lock()

    def create(self, links: list[str]) -> DownloadTask:
        # uuid4 用来生成随机任务 id
        task = DownloadTask(id=uuid4().hex, links=links)

        # 多线程会同时读写 tasks，所以这里加锁
        with self.lock:
            self.tasks[task.id] = task

        return task

    def get(self, task_id: str) -> DownloadTask | None:
        with self.lock:
            return self.tasks.get(task_id)

    def all(self) -> list[DownloadTask]:
        with self.lock:
            return list(self.tasks.values())

    def update(self, task_id: str, **changes) -> DownloadTask | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                for key, value in changes.items():
                    setattr(task, key, value)
            return task

    def append_log(self, task_id: str, msg: str) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                task.logs.append(msg)

    def add_file(self, task_id: str, url: str) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task and url not in task.files:
                task.files.append(url)