# Dockerfile
# ============================================================
# DY·ONLINE 视频下载台 —— 生产镜像
#
# 用 Playwright 官方基础镜像：Python + 三大浏览器 + 全套系统依赖都已就绪，
# 比用 python:slim 手动补 libnss3 那一串依赖链省事得多，也不容易出错。
#
# ⚠️ 版本对齐铁律：
#   官方镜像里预装的浏览器只与「同版本」的 playwright Python 包匹配。
#   升级时必须同时改两处 —— 下面的 FROM tag 和 RUN 里的 playwright==x.y.z，
#   否则启动时会因"浏览器版本与库版本不匹配"直接报错。
# ============================================================
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

# ---------- 系统依赖 ----------
# ffmpeg         : B 站 1080P 的 DASH 音视频合并（bilibili.py 会直接调用它）
# fonts-noto-cjk : 中文字体；抖音的 DOM 兜底取标题路径需要它，否则渲染成方块
# tzdata: 时区数据库！缺了它 TZ=Asia/Shanghai 不生效，容器时间会退回 UTC（比国内少 8 小时）
RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//security.ubuntu.com@//mirrors.aliyun.com@g' /etc/apt/sources.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-cjk \
        tzdata \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

# ---------- Python 依赖 ----------
# 先只拷依赖清单：以后改代码时这一层缓存命中，不用重装依赖
COPY requirements.txt .
# 第二次 pip install 是刻意为之：requirements 里写的是 playwright>=1.49.0，
# 可能装到比镜像更新的版本，这里强制拉回与镜像浏览器匹配的版本
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple "playwright==1.60.0"

# ---------- 应用代码 ----------
COPY . .

ENV HOST=0.0.0.0 \
    PORT=8000 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 保证镜像单独运行时目录也存在（compose 里会用卷覆盖这两个路径）
RUN mkdir -p /app/data /app/downloads

EXPOSE 8000

# 探活不依赖 curl：直接用 Python 请求自己的 /healthz
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

# ⚠️ 必须单 worker：ThreadPoolExecutor 和排队计数都是「进程私有」的，
#    多 worker 会让任务队列分叉、各进程状态互不可见。
#    要提升吞吐请调大 MAX_CONCURRENCY，而不是加 --workers。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
