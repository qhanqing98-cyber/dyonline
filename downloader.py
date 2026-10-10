#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音(www.douyin.com)无水印视频下载器
====================================
原理:
    用 Playwright 驱动系统自带的 Edge(无头)打开作品页，浏览器会自己跑完
    抖音的反爬 JS(签名/验证 cookie)，我们直接拦截页面发出的详情接口响应
    /aweme/v1/web/aweme/detail/，从 aweme_detail.video.play_addr.url_list
    里取无水印直链，再用 requests 带 Referer 下载到本地。

    (play_addr 为无水印地址；download_addr 带 watermark=1，需避开)

依赖: requests, playwright(本机已装), 系统自带 Edge
安装: pip install requests playwright

用法:
    python downloader.py "分享链接或口令"
    python downloader.py -o D:/downloads "链接1" "链接2" ...
    python downloader.py --cookies-from-browser edge "链接"  # 失败时 yt-dlp 兜底
    python downloader.py --yt-dlp --cookies-from-browser edge "链接"  # 强制 yt-dlp
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import (
    Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright,
)

import bilibili
from app import config
from app.logging_setup import redact, set_log_context

# 模拟新版 Edge/Chrome 浏览器 UA
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

_SHORT_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_\-]+/?")
_IN_ID_RE = re.compile(
    r"(?:/video/|/note/|share/video/|share/note/|modal_id=|aweme_id="
    r"|item_ids=|item_id=|vid=|video_id=)(\d{15,20})"
)
_PURE_ID_RE = re.compile(r"^\d{15,20}$")


def _emit(log_cb, msg):
    """统一日志输出:有回调走回调(GUI)，否则 print(CLI)。"""
    if log_cb:
        log_cb(msg)
    else:
        print(redact(msg))


# ---------------------------------------------------------------------------
# 分享链接 / 口令 解析
# ---------------------------------------------------------------------------
def resolve_short_link(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         allow_redirects=True, timeout=15)
        # 优先从最终跳转 URL 里找 ID
        m = _IN_ID_RE.search(r.url)
        if m:
            return m.group(1)
        # 有些短链返回 JS 跳转页，从 HTML 里找明确的 ID 特征
        m = _IN_ID_RE.search(r.text)
        if m:
            return m.group(1)
    except Exception as e:
        raise ValueError(f"短链解析失败: {e}")
    raise ValueError("短链解析失败: 未找到视频 ID")


def extract_aweme_id(text):
    """从分享链接或口令文本中提取 aweme_id(可容忍中文/英文引号等标点)。"""
    text = (text or "").strip()
    if _PURE_ID_RE.match(text):
        return text
    # 直接在整段文本上找带明确路径的 ID，不受前后引号/标点影响
    m = _IN_ID_RE.search(text)
    if m:
        return m.group(1)
    # 短链 v.douyin.com(字符集安全，不会吞掉引号)
    s = _SHORT_RE.search(text)
    if s:
        return resolve_short_link(s.group(0))
    # 兜底: 孤立的 15~20 位数字
    m = re.search(r"\d{15,20}", text)
    if m:
        return m.group(0)
    raise ValueError("无法解析出视频 ID，请提供抖音分享链接或口令")


def detect_platform(link):
    """根据链接特征判断平台: 'bilibili' 或 'douyin'。"""
    lk = (link or "")
    if ("bilibili.com" in lk) or ("b23.tv" in lk) or \
            re.search(r"(^|[/\s])(BV[0-9A-Za-z]{10}|av\d+)", lk):
        return "bilibili"
    return "douyin"


# ---------------------------------------------------------------------------
# 浏览器启动（引擎可切换：本机用系统 Edge，服务器用 Playwright 自带 Chromium）
# ---------------------------------------------------------------------------
_CHROMIUM_ARGS = [
    # 容器里以 root 运行 Chromium 必须关沙箱，否则直接崩（不加就是启动即退出）
    "--no-sandbox",
    # 容器 /dev/shm 默认只有 64MB，不加这个打开大页面会随机 OOM
    "--disable-dev-shm-usage",
    # 抹掉最显眼的自动化特征，降低被风控识别的概率
    "--disable-blink-features=AutomationControlled",
]


def _launch_browser(p, engine=None):
    """按配置启动 Chromium 内核浏览器。

    msedge   —— 复用系统自带 Edge（Windows 本机首选，抖音反爬识别率最低）
    chromium —— Playwright 自带内核（Linux 服务器唯一选择，需 playwright install chromium）
    """
    engine = (engine or os.getenv("DOUYIN_BROWSER_CHANNEL") or config.DOUYIN_ENGINE).lower()
    if engine == "chromium":
        try:
            return p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        except Exception as e:
            # 最常见的失败是没下载浏览器内核，给出可直接照抄的修复命令
            raise RuntimeError(
                "chromium 启动失败，请先执行 playwright install chromium"
                "（或把 DOUYIN_ENGINE 改回 msedge）"
                f"：{e}"
            ) from e
    return p.chromium.launch(channel="msedge", headless=True)


def _new_context(browser):
    """创建带反爬伪装的浏览器上下文，并按需注入 cookie。"""
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1280, "height": 800},
    )
    # 服务器上没有浏览器登录态，--cookies-from-browser 用不了，只能靠环境变量注入
    if config.DOUYIN_COOKIE:
        cookies = []
        for part in config.DOUYIN_COOKIE.split(";"):
            name, _, value = part.strip().partition("=")
            if name and value:
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".douyin.com",
                    "path": "/",
                })
        if cookies:
            ctx.add_cookies(cookies)
    return ctx


# ---------------------------------------------------------------------------
# 核心: 用浏览器拦截详情接口，拿到无水印直链
# ---------------------------------------------------------------------------
def extract_media(detail):
    desc = (detail.get("desc") or "").strip()
    author = ((detail.get("author") or {}).get("nickname") or "").strip()
    video = detail.get("video") or {}
    candidates = []
    # 只取 play_addr 系(无水印)；download_addr 带 watermark=1 不要
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        for u in ((video.get(key) or {}).get("url_list") or []):
            if u:
                candidates.append(u.replace("playwm", "play"))
    images = detail.get("images")
    return {
        "desc": desc,
        "author": author,
        "candidates": list(dict.fromkeys(candidates)),
        "is_image_post": bool(images) and not video,
        "image_count": len(images) if images else 0,
    }


def fetch_video_info(page, aweme_id, log_cb=None):
    """先接收页面响应，再独立等待详情数据，不依赖整个页面加载完成。"""
    captured = {}
    diagnostics = []
    navigation_ms = int(os.getenv("DOUYIN_NAVIGATION_TIMEOUT_MS", "30000"))
    media_ms = int(os.getenv("DOUYIN_MEDIA_TIMEOUT_MS", "30000"))
    if navigation_ms <= 0 or media_ms <= 0:
        raise ValueError("抖音导航和数据等待时间必须为正整数毫秒")

    def on_response(resp):
        if captured or "aweme/v1/web/aweme/detail" not in resp.url:
            return
        diagnostics.append(f"详情接口 HTTP {resp.status}")
        try:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("aweme_detail"), dict) and data["aweme_detail"]:
                captured["detail"] = data["aweme_detail"]
            else:
                diagnostics.append("详情接口未返回作品数据")
        except (PlaywrightError, ValueError):
            diagnostics.append("详情接口不是有效 JSON")

    def on_failed(request):
        # 只记录关键请求，不把 Cookie、查询参数或完整接口响应写进日志。
        if request.resource_type == "document" or "aweme/v1/web/aweme/detail" in request.url:
            diagnostics.append(f"关键请求失败: {request.failure}")

    page.on("response", on_response)
    page.on("requestfailed", on_failed)
    try:
        try:
            response = page.goto(f"https://www.douyin.com/video/{aweme_id}",
                                 wait_until="commit", timeout=navigation_ms)
            if response is not None:
                _emit(log_cb, f"[*] 页面响应: HTTP {response.status}，等待视频数据")
        except PlaywrightTimeoutError:
            _emit(log_cb, "[!] 页面导航超时，继续检查已收到的视频数据")
        # 导航等待期间可能已经收到详情，优先使用，避免无谓读取页面。
        deadline = time.monotonic() + media_ms / 1000
        while True:
            if "detail" in captured:
                return extract_media(captured["detail"])
            try:
                src = page.evaluate(
                    "() => { const v=document.querySelector('video');"
                    " return v ? (v.currentSrc||v.src) : null }"
                )
                # blob 地址仅在浏览器上下文有效，requests 无法下载。
                if (isinstance(src, str) and src.startswith(("https://", "http://"))
                        and "douyinstatic" not in src and "uuu_" not in src):
                    return {"desc": aweme_id, "author": "", "candidates": [src],
                            "is_image_post": False, "image_count": 0}
            except PlaywrightError:
                # 页面重定向时执行上下文可能暂时不存在，下轮重试。
                pass
            remaining_ms = (deadline - time.monotonic()) * 1000
            if remaining_ms <= 0:
                break
            page.wait_for_timeout(min(1000, remaining_ms))

        try:
            title = page.title().strip()[:100]
        except PlaywrightError:
            title = "无法读取"
        for message in list(dict.fromkeys(diagnostics))[-6:]:
            _emit(log_cb, f"[!] {message}")
        _emit(log_cb, f"[!] 页面标题: {title or '空'}")
        if page.url == "about:blank":
            raise RuntimeError("抖音页面未打开；请检查容器的 DNS、出口网络、代理及 TLS 连接")
        raise RuntimeError(
            "未获取到视频数据；请根据页面标题和请求日志检查网络、登录/验证码、作品可见性"
        )
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("requestfailed", on_failed)


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
def download(url, out_path, session, log_cb=None, progress_cb=None):
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}
    with session.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if progress_cb:
                        progress_cb(pct, "download")
                    # 服务模式（有 log_cb）下进度由回调回传，不再刷 stdout 造成日志污染
                    if log_cb is None:
                        print(f"\r    下载中 {done}/{total} ({pct}%)",
                              end="", flush=True)
        if total and log_cb is None:
            print()
    return done


def sanitize(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:80] or fallback


def ytdlp_download(link, outdir, cookies_browser, log_cb=None):
    set_log_context(stage="yt_dlp")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "best",
        "-o", str(outdir / "%(title).80s [%(id)s].%(ext)s"),
    ]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    cmd.append(link)
    _emit(log_cb, "[*] 使用 yt-dlp 兜底下载...")
    # Windows 下隐藏子进程黑框(打包成 GUI 后不闪 console)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.monotonic()
    if log_cb is None:
        # 命令行仍直接显示子进程进度，保留现有交互。
        code = subprocess.run(cmd, creationflags=flags).returncode
    else:
        cmd[3:3] = ["--newline"]
        last_progress = float("-inf")
        with subprocess.Popen(cmd, creationflags=flags, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace") as process:
            try:
                for line in process.stdout:
                    message = line.strip()
                    if not message:
                        continue
                    if re.match(r"\[download\]\s+\d", message):
                        now = time.monotonic()
                        if now - last_progress < 2 and not re.search(r"100(?:\.0)?%", message):
                            continue
                        last_progress = now
                    _emit(log_cb, redact(message)[:8192])
            except BaseException:
                process.terminate()
                raise
            code = process.wait()
    logging.getLogger(__name__).log(
        logging.INFO if code == 0 else logging.ERROR, "yt-dlp 执行结束",
        extra={"event": "external_tool_finished", "fields": {
            "tool": "yt-dlp", "exit_code": code,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }},
    )
    return code == 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def process_media(media, aweme_id, outdir, session,
                  log_cb=None, progress_cb=None):
    if media["is_image_post"]:
        _emit(log_cb, f"[!] 这是图文作品({media['image_count']} 张图)，没有视频可下载。")
        return False
    if not media["candidates"]:
        raise RuntimeError("未找到视频地址(作品可能已删除或为私密)")

    title = media["desc"] or aweme_id
    if media["author"]:
        _emit(log_cb, f"[*] 作者: {media['author']}")
    _emit(log_cb, f"[*] 文案: {title[:60]}")

    base = outdir / sanitize(title, aweme_id)
    for idx, url in enumerate(media["candidates"][:6]):
        if "douyinstatic" in url or "uuu_" in url:
            continue
        out_path = Path(f"{base}.mp4") if idx == 0 else Path(f"{base}_{idx}.mp4")
        if out_path.exists():
            _emit(log_cb, f"[*] 已存在，跳过: {out_path.name}")
            return True
        try:
            size = download(url, out_path, session,
                            log_cb=log_cb, progress_cb=progress_cb)
            if size > 100 * 1024:  # >100KB 才算有效视频，排除占位动画
                _emit(log_cb, f"[OK] 已保存: {out_path} ({size} bytes)")
                return True
            _emit(log_cb, f"[!] 第 {idx + 1} 个地址内容过小({size}B)，尝试下一个")
        except Exception as e:
            logging.getLogger(__name__).warning("候选地址下载失败", exc_info=True, extra={"event": "download_candidate_failed"})
            _emit(log_cb, f"[!] 第 {idx + 1} 个地址下载失败: {e}")
    raise RuntimeError("所有候选视频地址均下载失败")


def download_douyin(session, link, outdir, use_ytdlp=False,
                    cookies_browser=None, log_cb=None, progress_cb=None,
                    engine=None):
    """抖音下载完整流程:自管 playwright 生命周期，失败可自动 yt-dlp 兜底。

    GUI 与 CLI 共用此入口，避免各自维护浏览器生命周期。
    """
    if use_ytdlp:
        return ytdlp_download(link, outdir, cookies_browser, log_cb=log_cb)
    set_log_context(stage="resolve_link")
    aweme_id = extract_aweme_id(link)
    _emit(log_cb, f"[*] 视频 ID: {aweme_id}")
    try:
        set_log_context(stage="browser_start")
        with sync_playwright() as p:
            _emit(log_cb, f"[*] 浏览器: {engine or os.getenv('DOUYIN_BROWSER_CHANNEL') or config.DOUYIN_ENGINE}")
            browser = _launch_browser(p, engine)
            try:
                ctx = _new_context(browser)
                page = ctx.new_page()
                set_log_context(stage="fetch_detail")
                media = fetch_video_info(page, aweme_id, log_cb=log_cb)
            finally:
                browser.close()
        set_log_context(stage="download")
        return process_media(media, aweme_id, outdir, session,
                             log_cb=log_cb, progress_cb=progress_cb)
    except Exception as e:
        if cookies_browser:
            _emit(log_cb, f"[x] Playwright 失败: {e}")
            _emit(log_cb, "[*] 尝试 yt-dlp 兜底...")
            return ytdlp_download(link, outdir, cookies_browser, log_cb=log_cb)
        raise


def main():
    ap = argparse.ArgumentParser(description="抖音/B站 无水印视频下载器")
    ap.add_argument("links", nargs="+", help="分享链接/口令，可一次给多个")
    ap.add_argument("-o", "--output", default=".", help="保存目录(默认当前目录)")
    ap.add_argument("--cookies-from-browser", default=None,
                    help="抖音失败时用 yt-dlp 从浏览器读 cookie 兜底，如 edge/chrome")
    ap.add_argument("--yt-dlp", action="store_true", help="抖音强制使用 yt-dlp 下载")
    ap.add_argument("--engine", default=None,
                    help="抖音: 浏览器引擎 msedge(需系统Edge) 或 chromium(需 playwright install chromium)")
    # B 站专用参数
    ap.add_argument("--p", dest="p", default=None,
                    help="B站: 选择分P(页码数字或 ALL，默认下载全部)")
    ap.add_argument("--hd", action="store_true",
                    help="B站: 下载1080P高清(DASH+ffmpeg，需 --cookie 登录态 + 本机装 ffmpeg)")
    ap.add_argument("--cookie", default=None,
                    help="B站: 传登录 Cookie(如 SESSDATA=xxx)，下载高清需要")
    args = ap.parse_args()

    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    # 按平台分类
    bili_links = [l for l in args.links if detect_platform(l) == "bilibili"]
    dy_links = [l for l in args.links if detect_platform(l) != "bilibili"]

    ok = 0
    total = len(args.links)

    # B 站(纯 requests，无需浏览器)
    for link in bili_links:
        print("=" * 60)
        print(f"[输入] {link[:80]}")
        try:
            ok += bool(bilibili.process_bilibili(
                session, link, outdir,
                p_choice=args.p, hd=args.hd, cookie=args.cookie))
        except Exception as e:
            print(f"[x] 失败: {e}")

    # 抖音(Playwright 驱动系统 Edge)
    for link in dy_links:
        print("=" * 60)
        print(f"[输入] {link[:80]}")
        try:
            ok += bool(download_douyin(session, link, outdir,
                                       use_ytdlp=args.yt_dlp,
                                       cookies_browser=args.cookies_from_browser,
                                       engine=args.engine))
        except Exception as e:
            print(f"[x] 失败: {e}")

    print("=" * 60)
    print(f"完成: {ok}/{total}")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()


