# app/config.py
"""运行期配置中心：所有可变项都来自环境变量 / .env，代码里不再写死。

优先级：真实环境变量  >  项目根目录的 .env  >  代码里的默认值

这样做的好处：
- 同一份代码，本机（msedge/127.0.0.1）和服务器（chromium/0.0.0.0）行为不同，但不改一行代码
- 密钥（口令、cookie）只存在 .env 里，永远不会进 Git
"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（app/config.py 的上两级）
BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    """把项目根目录的 .env 读进 os.environ（已存在的变量不覆盖）。

    刻意不用 python-dotenv：为这点功能加一个依赖不划算，逻辑也就 15 行。
    """
    path = BASE_DIR / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # 引号包裹的值原样保留（允许里面出现 #）；否则按 # 切掉行内注释
        if value[:1] in ('"', "'"):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split("#", 1)[0].strip()
        os.environ.setdefault(key, value)   # 真实环境变量优先，不覆盖


def _str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default   # 写错了就退回默认值，不让服务起不来


def _bool(name: str, default: bool = False) -> bool:
    return _str(name, "1" if default else "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


_load_dotenv()

# ---------------------------------------------------------------------------
# 服务监听
# ---------------------------------------------------------------------------
HOST = _str("HOST", "127.0.0.1")   # 服务器上改成 0.0.0.0
PORT = _int("PORT", 8000)

# ---------------------------------------------------------------------------
# 目录与数据库
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = Path(_str("DOWNLOAD_DIR", str(BASE_DIR / "downloads"))).resolve()
DATA_DIR = Path(_str("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = Path(_str("DB_PATH", str(DATA_DIR / "app.db"))).resolve()

# ---------------------------------------------------------------------------
# 并发与队列（公网部署时这是保命的两个值）
# ---------------------------------------------------------------------------
MAX_CONCURRENCY = _int("MAX_CONCURRENCY", 2)   # 同时下载数；每个浏览器约 150-300MB 内存
QUEUE_LIMIT = _int("QUEUE_LIMIT", 20)          # 排队上限，超出直接回 429

# ---------------------------------------------------------------------------
# 访问鉴权（ACCESS_TOKEN 留空 = 关闭鉴权，本机开发用）
# ---------------------------------------------------------------------------
ACCESS_TOKEN = _str("ACCESS_TOKEN", "")
COOKIE_NAME = _str("COOKIE_NAME", "dy_token")
COOKIE_MAX_AGE = _int("COOKIE_MAX_AGE", 60 * 60 * 24 * 30)   # 登录保持 30 天
COOKIE_SECURE = _bool("COOKIE_SECURE", False)                # HTTPS 部署时设 true

# ---------------------------------------------------------------------------
# 入参限制（防止有人一次塞 10 万个链接打爆服务）
# ---------------------------------------------------------------------------
MAX_LINKS_PER_TASK = _int("MAX_LINKS_PER_TASK", 20)
MAX_LINK_LEN = _int("MAX_LINK_LEN", 2048)

# ---------------------------------------------------------------------------
# 磁盘保护
# ---------------------------------------------------------------------------
MIN_FREE_MB = _int("MIN_FREE_MB", 2048)   # 剩余空间低于此值拒绝新任务

# ---------------------------------------------------------------------------
# 平台相关
# ---------------------------------------------------------------------------
DOUYIN_ENGINE = _str("DOUYIN_ENGINE", "msedge")   # msedge(本机) | chromium(服务器)
DOUYIN_COOKIE = _str("DOUYIN_COOKIE", "")         # 服务器上注入抖音登录态
BILI_COOKIE = _str("BILI_COOKIE", "")             # B 站登录态（1080P 需要）

# 目录必须存在，否则挂载 StaticFiles / 建库会直接报错
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def summary() -> str:
    """启动日志用：打印生效配置，敏感值只显示长度不打码原文。"""

    def mask(value: str) -> str:
        return "未设置" if not value else f"已设置({len(value)}字符)"

    return (
        f"HOST={HOST} PORT={PORT} 并发={MAX_CONCURRENCY} 队列上限={QUEUE_LIMIT} "
        f"引擎={DOUYIN_ENGINE} 鉴权={mask(ACCESS_TOKEN)} 下载目录={DOWNLOAD_DIR}"
    )
