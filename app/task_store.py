# app/task_store.py
"""下载任务仓库：SQLite 持久化版。

对外 5 个方法签名与旧内存版完全一致（create/get/all/update/append_log/add_file），
app/main.py 无需感知存储从 dict 换成了 SQLite。
数据落在 data/app.db（路径见 app/config.py 的 DB_PATH），服务重启后任务、进度、日志全部还在。

注意：必须以单 worker 运行（uvicorn --workers 1）。
并发下载由 ThreadPoolExecutor 控制，多 worker 会让任务队列分叉、状态互不可见。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from json import dumps, loads
from pathlib import Path
from sqlite3 import connect, Row
from threading import Lock
from uuid import uuid4

from app import config


def _now() -> str:
    """统一时间戳：UTC + 显式时区偏移。

    - 带 +00:00 后缀，前端 new Date() 解析零歧义，自动换算成浏览器本地时间；
      之前的 datetime.now().isoformat() 不带时区，隐含依赖「容器时区 == 浏览器时区」，
      容器一旦退回 UTC 就会出现 8 小时的时间偏差
    - timespec="milliseconds"：ES 规范只保证支持 3 位小数，
      datetime 默认的 6 位微秒在部分浏览器会解析失败（返回 Invalid Date）
    - 存 UTC 是行业标准：换服务器时区、换机器都不影响历史数据；
      同格式 ISO 字符串排序 == 时间排序，ORDER BY created_at DESC 不受影响
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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

    # 创建时间（UTC + 时区偏移，见 _now()），方便以后排查任务
    created_at: str = field(default_factory=_now)


class TaskStore:
    """SQLite 版任务仓库。

    关键点：
    - check_same_thread=False：uvicorn 的请求线程与后台下载线程共用同一连接
    - 所有读写都在 self.lock 里：SQLite 写入本身是串行的，一把锁最简单可靠
    - WAL 模式：读不阻塞写，前端轮询与下载线程写进度互不拖累
    - logs 单独建表：避免每追加一条日志就重写整个 JSON 列
    """

    # 每个任务最多保留的日志条数（超出自动裁剪最旧的）
    LOG_LIMIT = 500

    # update() 允许修改的字段白名单，防止误改 id / created_at 这类关键列
    _UPDATABLE = ("status", "progress", "phase", "error")

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        id         TEXT PRIMARY KEY,
        links      TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'queued',
        progress   INTEGER NOT NULL DEFAULT 0,
        phase      TEXT NOT NULL DEFAULT 'queued',
        files      TEXT NOT NULL DEFAULT '[]',
        error      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS logs (
        task_id TEXT NOT NULL,
        seq     INTEGER NOT NULL,
        msg     TEXT NOT NULL,
        PRIMARY KEY (task_id, seq)
    );
    """

    def __init__(self, db_path=None):
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.lock = Lock()
        self.conn = connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = Row
        self.conn.executescript(self._SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL").fetchone()
        self.conn.commit()

        # 进度节流用：{task_id: (progress, phase)}
        self._last_progress = {}

        self._fail_zombie_tasks()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _row_to_task(self, row) -> DownloadTask:
        """把数据库行重建为 DownloadTask 对象（含日志）。

        必须返回 dataclass 实例而不是 row —— main.py 里用 asdict() 序列化。
        """
        logs = self.conn.execute(
            "SELECT msg FROM logs WHERE task_id=? ORDER BY seq", (row["id"],)
        ).fetchall()
        return DownloadTask(
            id=row["id"],
            links=loads(row["links"]),
            status=row["status"],
            progress=row["progress"],
            phase=row["phase"],
            logs=[r["msg"] for r in logs],
            files=loads(row["files"]),
            error=row["error"],
            created_at=row["created_at"],
        )

    def _fail_zombie_tasks(self) -> None:
        """启动时把残留的 running/queued 标为失败。

        进程被 Ctrl+C / OOM / 容器重启杀掉后，这些任务不可能再有结果；
        不处理的话前端会永远显示“下载中”。
        """
        cur = self.conn.execute(
            "UPDATE tasks SET status='failed', phase='done',"
            " error='服务重启导致任务中断', updated_at=?"
            " WHERE status IN ('running','queued')",
            (_now(),),
        )
        self.conn.commit()
        if cur.rowcount:
            print(f"[task_store] 已将 {cur.rowcount} 个中断任务标记为失败")

    # ------------------------------------------------------------------
    # 对外接口（签名与旧内存版完全一致，main.py 无需改动）
    # ------------------------------------------------------------------
    def create(self, links: list[str]) -> DownloadTask:
        # uuid4 用来生成随机任务 id
        task = DownloadTask(id=uuid4().hex, links=list(links))
        now = _now()

        with self.lock:
            self.conn.execute(
                "INSERT INTO tasks"
                " (id, links, status, progress, phase, files, error, created_at, updated_at)"
                " VALUES (?, ?, 'queued', 0, 'queued', '[]', NULL, ?, ?)",
                (task.id, dumps(task.links, ensure_ascii=False), now, now),
            )
            self.conn.commit()
        return task

    def get(self, task_id: str) -> DownloadTask | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return self._row_to_task(row) if row else None

    def all(self) -> list[DownloadTask]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def update(self, task_id: str, **changes) -> DownloadTask | None:
        if not changes:
            return self.get(task_id)

        # --- 进度节流（可选的性能优化，整段删掉不影响功能）----------------
        # 下载器每读 256KB 就回调一次进度，值没变就没必要写库：
        # 一次 100MB 的下载大约能省掉 400 次 UPDATE。
        if set(changes) <= {"progress", "phase"}:
            throttle_key = (changes.get("progress"), changes.get("phase"))
            with self.lock:
                if self._last_progress.get(task_id) == throttle_key:
                    row = self.conn.execute(
                        "SELECT * FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                    return self._row_to_task(row) if row else None
                self._last_progress[task_id] = throttle_key
        # ------------------------------------------------------------------

        sets, params = [], []
        for key, value in changes.items():
            if key not in self._UPDATABLE:
                raise ValueError(f"不支持更新的字段: {key}")
            sets.append(f"{key} = ?")
            params.append(value)
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(task_id)

        with self.lock:
            self.conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return self._row_to_task(row) if row else None

    def append_log(self, task_id: str, msg: str) -> None:
        with self.lock:
            exists = self.conn.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not exists:
                return
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM logs WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            self.conn.execute(
                "INSERT INTO logs (task_id, seq, msg) VALUES (?, ?, ?)",
                (task_id, seq, msg),
            )
            # 裁剪最旧的日志，防止长任务把库撑大
            self.conn.execute(
                "DELETE FROM logs WHERE task_id=? AND seq <= ?",
                (task_id, seq - self.LOG_LIMIT),
            )
            self.conn.commit()

    def add_file(self, task_id: str, url: str) -> None:
        with self.lock:
            row = self.conn.execute(
                "SELECT files FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return
            files = loads(row["files"])
            if url in files:
                return
            files.append(url)
            self.conn.execute(
                "UPDATE tasks SET files=?, updated_at=? WHERE id=?",
                (dumps(files, ensure_ascii=False), _now(), task_id),
            )
            self.conn.commit()

    def close(self) -> None:
        """关闭连接（uvicorn 退出时不一定调用；WAL 模式下数据也不会丢）。"""
        with self.lock:
            self.conn.close()
            self._last_progress.clear()
