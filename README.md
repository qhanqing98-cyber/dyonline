# 个人视频下载工具

一个支持 **抖音** 与 **B 站（bilibili）** 无水印视频下载的工具，提供命令行（CLI）与 Web 两种使用方式。

- 抖音：使用 Playwright 驱动系统自带的 Edge 浏览器拦截详情接口，绕过反爬签名，直接获取无水印直链下载。
- B 站：纯 `requests` + wbi 签名，无需浏览器，支持多分 P 与 1080P 高清（需登录态 + ffmpeg）。
- Web：基于 FastAPI 的轻量页面，用于提交链接并查看下载任务。

---

## 功能特性

### 抖音（downloader.py）
- 无水印视频下载（自动避开带 `watermark=1` 的 `download_addr`，只取 `play_addr` 系）。
- 支持多种输入格式：分享链接、口令文本、纯视频 ID（15~20 位数字）。
- 短链（`v.douyin.com`）自动跳转解析。
- 失败时可自动用 **yt-dlp** 兜底，支持从浏览器读取 cookie（`edge` / `chrome`）。

### B 站（bilibili.py）
- 解析 BV 号 / av 号 / b23.tv 短链 / 完整链接。
- 获取标题、UP 主、分 P 列表，支持按页码下载指定分 P 或全部。
- 默认下载 ≤720P 的 mp4 直链（单文件，无需 ffmpeg）。
- `--hd` 下载 1080P（DASH 音视频分离，ffmpeg 合并，需登录 cookie + ffmpeg）。

### Web（app/）
- FastAPI 服务，提供首页、静态资源与下载文件目录访问。
- 完整下载任务 API：创建任务、查询单个任务状态/进度/日志、列出所有任务。
- 后台线程异步执行下载，进度与日志实时回写内存任务仓库。

---

## 目录结构

```
dyonline/
├── app/
│   ├── __init__.py       # 包标记
│   ├── main.py           # FastAPI 应用入口
│   └── task_store.py     # 下载任务仓库（内存版）
├── static/
│   └── style.css         # 前端样式
├── templates/
│   └── index.html        # 首页模板
├── downloads/            # 下载文件保存目录（运行时自动创建）
├── bilibili.py           # B 站下载模块
├── downloader.py         # 抖音下载 + CLI 入口
├── requirements.txt      # Python 依赖清单
└── README.md
```

---

## 环境要求

- **Python** 3.10+
- **微软 Edge 浏览器**（抖音下载依赖系统自带 Edge，Playwright 使用 `channel="msedge"` 无头启动）
- **ffmpeg**（仅 B 站 1080P 高清下载需要，需加入系统 PATH）

---

## 安装

```bash
# 1. （推荐）创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. 安装 Playwright 浏览器驱动（使用系统 Edge 时无需下载 chromium）
playwright install msedge
```

> `requirements.txt` 内容：`fastapi`、`uvicorn[standard]`、`jinja2`、`requests`、`playwright`、`yt-dlp`。

---

## 使用方法

### 方式一：命令行（CLI）

```bash
python downloader.py "分享链接或口令"
```

支持一次传入多个链接（自动区分抖音与 B 站）：

```bash
python downloader.py -o D:/downloads "链接1" "链接2" ...
```

**抖音相关参数：**

```bash
# 失败时用 yt-dlp 从 Edge 读取 cookie 兜底
python downloader.py --cookies-from-browser edge "链接"

# 强制使用 yt-dlp 下载
python downloader.py --yt-dlp --cookies-from-browser edge "链接"
```

**B 站相关参数：**

```bash
# 下载指定分 P（页码，从 1 开始）或 ALL（全部）
python downloader.py --p 2 "BV1xxxx"

# 下载 1080P 高清（需登录 cookie + 本机安装 ffmpeg）
python downloader.py --hd --cookie "SESSDATA=xxx" "BV1xxxx"
```

完整参数列表：

| 参数 | 说明 |
|------|------|
| `links` | 分享链接/口令，可一次给多个 |
| `-o, --output` | 保存目录（默认当前目录） |
| `--cookies-from-browser` | 抖音失败时用 yt-dlp 从浏览器读 cookie 兜底（如 `edge`/`chrome`） |
| `--yt-dlp` | 抖音强制使用 yt-dlp 下载 |
| `--p` | B 站：选择分 P（页码数字或 `ALL`，默认全部） |
| `--hd` | B 站：下载 1080P 高清（DASH + ffmpeg） |
| `--cookie` | B 站：传登录 Cookie（如 `SESSDATA=xxx`） |

---

### 方式二：Web 服务

```bash
uvicorn app.main:app --reload
```

浏览器访问：<http://127.0.0.1:8000>

首页提供链接输入框（每行一个链接），提交后创建后台下载任务，并实时显示进度条、日志与下载完成的文件链接；下载文件通过 `/files` 路径访问。

### HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 首页 |
| `POST` | `/api/tasks` | 创建下载任务，请求体 `{"links": ["链接1", "链接2"]}` |
| `GET` | `/api/tasks` | 列出所有任务 |
| `GET` | `/api/tasks/{task_id}` | 查询单个任务的进度、日志、文件 |
| `GET` | `/files/{filename}` | 下载已保存的视频文件 |

---

## 实现原理

### 抖音
1. 解析分享链接/口令，提取 `aweme_id`。
2. Playwright 无头启动 Edge 打开作品页，浏览器自动完成反爬 JS（签名/验证 cookie）。
3. 拦截详情接口 `/aweme/v1/web/aweme/detail/` 的响应，从 `aweme_detail.video.play_addr.url_list` 取无水印直链。
4. 用 `requests` 带 `Referer` 流式下载到本地。

### B 站
1. 解析 BV/av 号（b23.tv 短链自动跳转）。
2. `view` 接口获取标题、UP 主、分 P 列表（每 P 的 cid）。
3. `playurl` 接口（wbi 签名）获取播放地址：
   - 默认（≤720P）：`fnval=0` 返回 `durl` → mp4 单文件直链，直接下载。
   - `--hd`（1080P）：`fnval=16` 返回 `dash`（音视频分离）→ ffmpeg 合并。

---

## 注意事项

- 本工具仅供个人学习、研究及备份自己拥有权限的内容使用，请遵守目标平台的服务条款与当地法律法规，勿用于任何商业或侵权用途。
- 抖音下载依赖系统 Edge 浏览器，首次使用请确保已安装 Edge。
- B 站 1080P 高清下载需登录 Cookie（`SESSDATA` 等）且本机已安装 ffmpeg。
- 下载的文件统一保存在 `downloads/` 目录（或 `-o` 指定的目录），同名文件会自动跳过。
