# 部署指南

把 DY·ONLINE 部署到服务器上，由 Caddy 的 8080 端口对外提供下载服务，反向代理 app 容器的 8000。

---

## 一、前置条件

| 项 | 要求 |
|---|---|
| 服务器 | 2 核 2G 起（**2G 内存建议 `MAX_CONCURRENCY=1`**），40G 磁盘，Ubuntu 22.04+ |
| 访问地址 | `http://服务器地址:8080`，域名可选 |
| 本机工具 | 能 SSH 到服务器即可 |

> 首次 `docker compose build` 需要拉取约 1.5-2GB 的镜像，请预留时间。

---

## 二、快速开始（8080 入口）

### 1. 服务器装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker    # 免 sudo 用 docker
```

### 2. 拉代码

```bash
git clone https://github.com/qhanqing98-cyber/dyonline.git
cd dyonline
```

### 3. 配置

```bash
cp .env.example .env
nano .env
```

**必改项**：

```dotenv
# 访问口令：不设置的话任何人都能用你的服务器下载！
# 生成：python3 -c "import secrets;print(secrets.token_urlsafe(32))"
ACCESS_TOKEN=这里填生成的长口令

# 服务器上没有 Edge，必须用 chromium
DOUYIN_ENGINE=chromium

# 并发与内存：2G 内存建议 1~2
MAX_CONCURRENCY=2
QUEUE_LIMIT=20

# 当前入口为 HTTP；若前置代理提供 HTTPS，再按访问方式调整
COOKIE_SECURE=false

# 视频保留 24 小时，每小时检查
DOWNLOAD_RETENTION_HOURS=24
DOWNLOAD_CLEANUP_INTERVAL_SECONDS=3600
```

### 4. 端口与容器网络

Caddy 映射 `8080:8080`，转发到同一 compose 网络里的 `app:8000`。
app 的 8000 仅映射到宿主机回环用于本地检查，无需公网放行。

### 5. 启动

```bash
docker compose up -d --build
docker compose ps            # 等 STATUS 变成 healthy
docker compose logs -f app   # 看启动日志（Ctrl+C 退出，不影响服务）
```

### 6. 验证

浏览器打开 `http://服务器地址:8080` → 应跳转到登录页 → 输入 `ACCESS_TOKEN` → 进入下载台。

命令行快速验证：

```bash
curl http://服务器地址:8080/healthz
# {"status":"ok","disk_free_mb":...,"queue_pending":0,"max_concurrency":2}
```

### 7. 防火墙 / 安全组

放行 **22 / 8080**，**不要**放行 8000（应用只监听回环，由 Caddy 转发）。

---

## 三、常用运维命令

```bash
docker compose logs -f app         # 实时日志
docker compose restart app         # 重启应用
docker compose down                # 停止并删除容器（数据目录保留）
docker compose up -d --build       # 更新代码后重新构建启动
docker compose exec app bash       # 进容器排查
df -h                              # 看磁盘（这项目会天天用到）
du -sh downloads                   # 看下载目录占用
```

### 更新代码

```bash
git pull
docker compose up -d --build
```

---

## 四、故障排查

| 现象 | 原因与处理 |
|---|---|
| 建任务后一直「下载中」 | `docker compose logs -f app` 看日志；容器被重启过的任务会自动标记为「服务重启导致任务中断」 |
| 抖音任务失败、日志出现验证码/风控 | **见第六节「已知限制」**，机房 IP 跑抖音风险较高 |
| B 站 1080P 失败 | 需要 `BILI_COOKIE`（登录态）；容器内已含 ffmpeg |
| 页面 502 | `docker compose ps` 看 app 是否 healthy；`logs app` 看是否启动报错 |
| 8080 无法访问 | 确认 `8080:8080` 映射及安全组放行；`docker compose logs caddy` |
| 磁盘满、服务异常 | `du -sh downloads` 检查；清理旧视频后重启 |
| 内存不足 / 容器被杀 | 调小 `MAX_CONCURRENCY`；或调大 compose 里的 `memory` 限制 |
| 登录后刷新又跳回登录页 | `COOKIE_SECURE=true` 但你在用 HTTP 访问 —— 改成 false，或改走 HTTPS |

---

## 五、HTTP 与 HTTPS

当前 Caddy 配置使用 HTTP 8080，`COOKIE_SECURE=false`。
如在前方已有 HTTPS 网关，按实际访问方式配置 Cookie；本配置不会自动申请域名证书。

---

## 六、已知限制

1. **抖音在服务器上可能被风控。**
   本机走系统 Edge（`DOUYIN_ENGINE=msedge`）反爬识别率最低；服务器只能用 Chromium。
   如果日志里频繁出现验证码/取不到数据，可选降级方案：
   - 在 `.env` 里设置 `DOUYIN_COOKIE`（形如 `ttwid=xxx; msToken=xxx`）注入登录态；
   - 或让抖音链接走 yt-dlp（需先解决容器内 cookie 来源）；
   - 最稳妥：**服务器上只开放 B 站功能，抖音留在本机使用**。

2. **视频自动过期。** 默认保留 24 小时，每小时检查一次；有下载或文件传输时跳过本轮。
   可调整 `DOWNLOAD_RETENTION_HOURS`（0 为关闭）与 `DOWNLOAD_CLEANUP_INTERVAL_SECONDS`。
   仅删除顶层媒体文件，不跟随符号链接；任务与日志保留，文件列表同步更新到 SQLite。

3. **单 worker 限制。** 不能靠 `--workers` 提吞吐（任务队列是进程私有的），只能调 `MAX_CONCURRENCY`。

4. **单共享口令**，不是多用户账号体系。所有使用者共用同一个口令、看到同一批任务。

---

## 七、本地 Docker 验证（上云前彩排）

在 Windows / macOS 上先跑一遍，能提前验证三件事：容器内 Chromium 能否启动、
抖音在 Linux 无头环境下是否被风控、数据卷是否正确持久化。

```bash
# 1. .env 里注意：
#    DOUYIN_ENGINE=chromium   （容器里没有 Edge）
#    COOKIE_SECURE=false      （本地是 HTTP）
#    MAX_CONCURRENCY=1

# 2. 构建镜像（首次约 2GB 下载）
docker compose build app

# 3. 只启动应用（跳过 Caddy：本地 80 端口常被系统进程占用）
docker compose up -d app
docker compose ps

# 4. 验证
curl http://127.0.0.1:8000/healthz
# 浏览器打开 http://127.0.0.1:8000 → 登录页 → 输入 ACCESS_TOKEN

# 5. 收工（数据保留在 ./data 和 ./downloads）
docker compose down
```

---

## 八、安全清单（上线前逐项确认）

- [ ] `ACCESS_TOKEN` 已设置为足够长的随机串（**不做这步 = 把服务器送给别人**）
- [ ] HTTP 8080 使用 `COOKIE_SECURE=false`；如通过 HTTPS 网关访问再调整
- [ ] 安全组放行 22 / 8080，8000 不对公网开放
- [ ] `.env` 没有被提交进 Git（已在 `.gitignore` 中）
- [ ] `MAX_CONCURRENCY` 与服务器内存匹配
- [ ] 不对外公开宣传，避免被爬虫和滥用者盯上
- [ ] 遵守平台服务条款与当地法律，仅用于下载自己有权保存的内容
