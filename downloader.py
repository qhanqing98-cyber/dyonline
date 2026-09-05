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
import os
import re
import subprocess
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

import bilibili

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
        print(msg)


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


def fetch_video_info(page, aweme_id):
    """打开作品页，拦截详情接口返回作品数据。"""
    captured = {}

    def on_response(resp):
        if captured:
            return
        if "aweme/v1/web/aweme/detail" in resp.url:
            try:
                d = resp.json()
                if d.get("aweme_detail"):
                    captured["detail"] = d["aweme_detail"]
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(f"https://www.douyin.com/video/{aweme_id}",
                  wait_until="domcontentloaded", timeout=60000)
        for _ in range(12):
            if captured:
                break
            page.wait_for_timeout(2000)
    finally:
        page.remove_listener("response", on_response)

    if "detail" in captured:
        return extract_media(captured["detail"])

    # 兜底: 直接读 <video> 标签地址
    for _ in range(10):
        src = page.evaluate(
            "() => { const v=document.querySelector('video');"
            " return v ? (v.currentSrc||v.src) : null }"
        )
        if src and "douyinstatic" not in src and "uuu_" not in src:
            try:
                desc = page.evaluate(
                    "() => { const e=document.querySelector('h1');"
                    " return e ? e.innerText.trim() : null }"
                )
            except Exception:
                desc = None
            return {"desc": desc or aweme_id, "author": "",
                    "candidates": [src], "is_image_post": False, "image_count": 0}
    raise RuntimeError("未能获取视频数据(可能触发验证码，可加 --cookies-from-browser edge 兜底)")


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
                    print(f"\r    下载中 {done}/{total} ({pct}%)",
                          end="", flush=True)
        if total:
            print()
    return done


def sanitize(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:80] or fallback


def ytdlp_download(link, outdir, cookies_browser, log_cb=None):
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
    return subprocess.run(cmd, creationflags=flags).returncode == 0


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
            _emit(log_cb, f"[!] 第 {idx + 1} 个地址下载失败: {e}")
    raise RuntimeError("所有候选视频地址均下载失败")


def download_douyin(session, link, outdir, use_ytdlp=False,
                    cookies_browser=None, log_cb=None, progress_cb=None):
    """抖音下载完整流程:自管 playwright 生命周期，失败可自动 yt-dlp 兜底。

    GUI 与 CLI 共用此入口，避免各自维护浏览器生命周期。
    """
    if use_ytdlp:
        return ytdlp_download(link, outdir, cookies_browser, log_cb=log_cb)
    aweme_id = extract_aweme_id(link)
    _emit(log_cb, f"[*] 视频 ID: {aweme_id}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                ctx = browser.new_context(user_agent=USER_AGENT, locale="zh-CN")
                page = ctx.new_page()
                media = fetch_video_info(page, aweme_id)
            finally:
                browser.close()
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
                                       cookies_browser=args.cookies_from_browser))
        except Exception as e:
            print(f"[x] 失败: {e}")

    print("=" * 60)
    print(f"完成: {ok}/{total}")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()


