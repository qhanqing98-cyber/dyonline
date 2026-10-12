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
# 初始管理员密码：8–256 字符；仅没有任何账号时生效
# 生成：python3 -c "import secrets;print(secrets.token_urlsafe(32))"
ADMIN_USERNAME=admin
ADMIN_PASSWORD=这里填生成的长密码

# 服务器上没有 Edge，必须用 chromium
DOUYIN_ENGINE=chromium

# 并发与内存：2G 内存建议 1~2
MAX_CONCURRENCY=2
QUEUE_LIMIT=20
USER_QUEUE_LIMIT=3

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

浏览器打开 `http://服务器地址:8080` → 应跳转到登录页 → 输入管理员用户名和密码 → 进入下载台。

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

4. **账号与任务按用户隔离**，管理员通过服务器命令创建账号；不提供注册；每用户未结束任务默认最多 3 个。

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
# 浏览器打开 http://127.0.0.1:8000 → 登录页 → 输入管理员用户名和密码

# 5. 收工（数据保留在 ./data 和 ./downloads）
docker compose down
```

---

## 八、安全清单（上线前逐项确认）

- [ ] 首次设置 `ADMIN_PASSWORD`，或通过账号管理命令创建管理员；使用者各有独立账号
- [ ] HTTP 8080 使用 `COOKIE_SECURE=false`；如通过 HTTPS 网关访问再调整
- [ ] 安全组放行 22 / 8080，8000 不对公网开放
- [ ] `.env` 没有被提交进 Git（已在 `.gitignore` 中）
- [ ] `MAX_CONCURRENCY` 与服务器内存匹配
- [ ] 不对外公开宣传，避免被爬虫和滥用者盯上
- [ ] 遵守平台服务条款与当地法律，仅用于下载自己有权保存的内容


## 九、运维日志

应用运维日志采用单行 JSON 输出到 stdout，Docker 负责收集；网页任务日志继续存 SQLite，
保持每个任务最多 500 条。日志时间为带时区的 UTC，查看时可换算为本地时间。
`LOG_LEVEL=INFO` 为默认值，可设 `DEBUG / INFO / WARNING / ERROR / CRITICAL`；
无效值回退 INFO 并记录配置警告。DEBUG 会增加日志量，建议排查结束后恢复 INFO。

常用日志字段：`time`、`level`、`module`、`event`、`message`。
请求记录包含 `request_id`、方法、路径、状态码、耗时、响应是否完成及是否检测到断线；
任务记录包含 `task_id`，并按事件补充平台、阶段、状态和耗时。
请求 ID 由服务端生成，正常响应头 `X-Request-ID` 可用于查询对应日志。
创建任务的记录同时带请求 ID 与任务 ID；线程池复制上下文，让后台日志可追溯到提交请求。
HTTP 耗时覆盖响应传输过程，因此下载文件的耗时包含向用户传输的时间。
未处理异常保留后台堆栈，继续交由框架处理；异常 500 响应不保证带 `X-Request-ID`。

```bash
# 查看最近 30 分钟、最多 200 条应用日志，并继续跟踪
# --no-log-prefix 让每行保持可独立解析的 JSON
docker compose logs --no-log-prefix --since 30m --tail 200 -f app

# 查看 Caddy 访问日志（采用 Caddy 原生字段，如 request、status、duration）
docker compose logs --no-log-prefix --since 30m --tail 200 caddy

# 按任务 ID 搜索（替换为真实任务 ID）
docker compose logs --no-log-prefix --since 24h app | grep -F '任务ID'

# 按请求 ID 搜索（替换为响应头中的 ID）
docker compose logs --no-log-prefix --since 1h app | grep -F '请求ID'
```

关键事件包括：`task_created`、`task_started`、`task_finished`、`download_log`、
`queue_rejected`、`disk_rejected`、`http_request`、`request_exception`、
`cleanup_finished`、`cleanup_exception` 和 `external_tool_finished`。
正常探活与静态资源请求在 app 中仅记 DEBUG；Caddy 的访问日志仍会记录它们。
清理汇总记录删除数量、释放字节数、失败数量和耗时；忙碌跳过以及单文件详情仅记 DEBUG。
yt-dlp 服务模式输出会接入任务日志并降低进度刷屏频率，退出码记录在运维日志；
ffmpeg 失败记录脱敏后的 stderr 末尾，最多 8KB。

应用输出、异常、任务消息和外部工具输出经过共用脱敏处理；日志里的 URL 查询值被隐藏，
配置口令、Cookie 和 Authorization 被遮蔽。不记录请求体、完整环境配置或请求头。
Caddy 访问日志去掉 URI 查询串并移除请求和响应头字段。
CLI 保留终端进度；上述 JSON 格式主要针对 Web 服务。

app 与 Caddy 都采用 Docker `json-file` 驱动，`max-size=10m`、`max-file=3`。
这是每个容器的容量轮转，不是固定天数；日志越多，保留的时间越短。
删除或重新创建容器可能丢失旧日志，单纯重启现有容器通常仍可查看保留内容。
SQLite 任务日志存于 data 卷，与 Docker 运维日志采用不同的保存方式。

配置更改后需要重新创建容器使日志驱动配置生效；更新命令由运维人员执行：

```bash
docker compose up -d --build
```

日志功能不改写历史任务消息。账号隔离的自动结构迁移见下一节；历史记录中若曾出现敏感值，
需另行处理，不能认为新增脱敏机制已清理过去写入的数据。


## 十、用户账号与隔离（第一版）

账号登录始终启用，不再支持空口令关闭鉴权。首次启动没有账号时，以
`ADMIN_USERNAME=admin` 和 `ADMIN_PASSWORD` 创建管理员；旧部署没有配置
`ADMIN_PASSWORD` 时使用 `ACCESS_TOKEN` 的值作为初始密码。旧 Cookie 自动失效，需重新登录。
密码须为 8–256 字符，用户名须为 3–32 位字母、数字、点、下划线或连字符。
以后修改环境变量不会覆盖已有密码，请使用下面的重置命令。

升级前停止下载并备份数据库与文件，再重新构建：

```bash
docker compose stop app
# 复制整个 data 目录，包含可能存在的 SQLite WAL 文件
cp -a data "data-backup-$(date +%Y%m%d-%H%M%S)"
cp -a downloads "downloads-backup-$(date +%Y%m%d-%H%M%S)"
docker compose up -d --build
```

数据库自动增加 users、sessions 表及任务 user_id、storage_layout 列。
无归属的旧任务分配给第一个管理员；没有管理员时保持隐藏，创建管理员并重启 app 后分配。
旧媒体文件保持在顶层，不批量移动；新下载使用 `downloads/用户ID/任务ID/`，清理程序同时处理两种布局。
迁移可重复执行；后续回滚须使用备份并与对应版本一起恢复，避免混用两种目录布局。

账号由有服务器权限的管理员管理，没有开放注册或网页用户管理页：

```bash
# 未设置初始管理员密码时，交互创建首个管理员
# 密码不会显示，也不会放进命令行参数或 shell 历史
docker compose exec app python -m app.users create admin --admin
# 如果是运行后通过命令创建首个管理员，重启以分配旧任务
docker compose restart app

# 创建普通账号、查看账号、重置密码
docker compose exec app python -m app.users create alice
docker compose exec app python -m app.users list
docker compose exec app python -m app.users reset-password alice
```

本机可将上述容器命令替换为 `.venv/Scripts/python -m app.users ...`。
重置密码撤销该账号所有会话；退出只撤销当前会话。服务重启后，未过期会话仍可用。
浏览器 Cookie 存随机会话凭证，数据库只存其散列；密码使用独立盐值的 PBKDF2-SHA256（600000 次）。

所有任务列表、详情、日志和文件接口只读取当前登录用户的数据，包括管理员。
别人任务与不存在任务统一返回 404，不能靠修改任务 ID 读取别人文件。
用户 ID 由服务端会话获取，前端提交的 user_id 不参与归属判断。
运维日志增加 user_id；平台 Cookie 仍由服务端统一配置，第一版不支持个人平台 Cookie。
全局队列和磁盘保护继续生效；每用户任务数限制见下节，存储额度和公平调度尚未实现。

验收时用两个浏览器会话登录不同账号，各提交一个任务，然后交换任务与下载地址；
双方都应得到 404，各自列表只显示自己的任务。退出后重用旧 Cookie 应返回 401。


## 十一、每用户任务上限

`USER_QUEUE_LIMIT=3` 限制每个账号的“排队中 + 下载中”任务总数，管理员也受限。
超过上限返回 429，页面提示当前数量和上限；其他账号仍可在全局名额范围内提交。
`QUEUE_LIMIT` 仍是所有账号的未结束任务总上限。每个任务的链接数量仍由 `MAX_LINKS_PER_TASK` 限制。
无效或小于 1 的 `USER_QUEUE_LIMIT` 回退默认值 3；改变配置后需重新创建 app 容器。

检查用户数量、检查全局数量及预占名额在同一把锁内完成，防止并发提交突破上限。
下载成功、失败、磁盘拒绝都会释放名额；数据库创建或线程池提交失败也会回滚名额。
服务重启后原未结束任务已按原规则标记为失败，因此计数从零开始。
该计数依赖单进程，继续保持 `uvicorn --workers 1`。

拒绝日志事件为 `user_queue_rejected`，含 `user_id`、`user_pending`、`user_queue_limit`，
可按用户 ID 查询。没有新增数据库列，也没有改变线程池先进先出的调度方式。

```dotenv
USER_QUEUE_LIMIT=3
QUEUE_LIMIT=20
```
