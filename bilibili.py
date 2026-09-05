#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站(bilibili)视频下载器：纯 requests + wbi 签名，无浏览器依赖。

原理:
    1. 解析 BV/av 号(b23.tv 短链自动跳转)
    2. view 接口拿标题、UP主、分 P 列表(每 P 的 cid)
    3. playurl 接口(wbi 签名)拿播放地址:
         - 默认(≤720P): fnval=0 返回 durl → mp4 单文件直链, 直接下载
         - --hd(1080P): fnval=16 返回 dash(音视频分离) → ffmpeg 合并
        (1080P 需登录 cookie + ffmpeg)
"""

import hashlib
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
PLAYURL_API = "https://api.bilibili.com/x/player/wbi/playurl"

REFERER = "https://www.bilibili.com/"

# wbi 签名固定置换表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AV_RE = re.compile(r"\bav(\d+)\b", re.I)
_B23_RE = re.compile(r"https?://b23\.tv/[A-Za-z0-9]+")
_P_RE = re.compile(r"[?&]p=(\d+)")


def _emit(log_cb, msg):
    """统一日志输出:有回调走回调(GUI)，否则 print(CLI)。"""
    if log_cb:
        log_cb(msg)
    else:
        print(msg)


def _get_mixin_key(orig):
    return ''.join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _get_wbi_keys(session):
    j = session.get(NAV_API, timeout=15).json()
    img = j['data']['wbi_img']['img_url'].rsplit('/', 1)[1].split('.')[0]
    sub = j['data']['wbi_img']['sub_url'].rsplit('/', 1)[1].split('.')[0]
    return img, sub


def _enc_wbi(params, img_key, sub_key):
    mixin_key = _get_mixin_key(img_key + sub_key)
    p = dict(params)
    p['wts'] = int(time.time())
    p = {k: p[k] for k in sorted(p)}
    p = {k: v for k, v in p.items() if not any(ch in str(v) for ch in "!'()*")}
    query = urlencode(p)
    p['w_rid'] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return p


def resolve_input(text):
    """返回 (bvid 或 None, aid 或 None)。支持 BV/av/b23.tv 短链/完整 URL。"""
    text = (text or "").strip()
    s = _B23_RE.search(text)
    if s:
        try:
            r = requests.get(s.group(0), headers={"User-Agent": UA},
                             allow_redirects=True, timeout=15)
            text = r.url
        except Exception as e:
            raise ValueError(f"b23.tv 短链解析失败: {e}")
    bv = _BV_RE.search(text)
    if bv:
        return bv.group(0), None
    av = _AV_RE.search(text)
    if av:
        return None, av.group(1)
    raise ValueError("无法解析 B 站视频号(需 BV... 或 av... 或完整链接)")


def get_view(session, bvid=None, aid=None):
    params = {"bvid": bvid} if bvid else {"aid": aid}
    j = session.get(VIEW_API, params=params, timeout=15).json()
    if j.get("code") != 0:
        raise RuntimeError(f"view 接口错误: {j.get('message')}")
    return j["data"]


def get_playurl(session, bvid, cid, qn=64, fnval=0):
    img, sub = _get_wbi_keys(session)
    params = _enc_wbi(
        {"bvid": bvid, "cid": cid, "qn": qn, "fnval": fnval,
         "fnver": 0, "fourk": 1},
        img, sub,
    )
    j = session.get(PLAYURL_API, params=params, timeout=20).json()
    if j.get("code") != 0:
        raise RuntimeError(f"playurl 接口错误 code={j.get('code')}: {j.get('message')}")
    return j["data"]


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
def _download_stream(session, url, out_path, log_cb=None, progress_cb=None,
                     phase="download"):
    """流式下载单个 url 到文件，返回字节数。phase 供调用方区分 DASH 阶段。"""
    headers = {"User-Agent": UA, "Referer": REFERER}
    done = 0
    with session.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if progress_cb:
                        progress_cb(pct, phase)
                    print(f"\r    下载中 {done}/{total} ({pct}%)",
                          end="", flush=True)
        if total:
            print()
    return done


def download_durl(session, data, out_path, log_cb=None, progress_cb=None):
    """mp4 直链(durl 可能多段，按顺序拼接)。跨段累加出整体精确进度。"""
    durl = data.get("durl") or []
    if not durl:
        return False
    total_size = sum(seg.get("size") or 0 for seg in durl)
    size = 0
    with open(out_path, "wb") as f:
        for i, seg in enumerate(durl, 1):
            headers = {"User-Agent": UA, "Referer": REFERER}
            with session.get(seg["url"], headers=headers, stream=True,
                             timeout=60) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    size += len(chunk)
                    if total_size and progress_cb:
                        pct = min(size * 100 // total_size, 100)
                        progress_cb(pct, f"segment {i}/{len(durl)}")
    return size > 0


def download_dash(session, data, out_path, log_cb=None, progress_cb=None):
    """DASH 分片: 下载最高清 video + 最高音质 audio, 用 ffmpeg 合并。

    进度映射: 视频流 0-50%、音频流 50-90%、ffmpeg 合并 99%。
    """
    dash = data.get("dash") or {}
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    if not videos:
        return False
    video = max(videos, key=lambda v: v.get("id", 0))
    audio = max(audios, key=lambda a: a.get("bandwidth", 0)) if audios else None

    tmp_v = out_path.with_suffix(".v.m4s")
    tmp_a = out_path.with_suffix(".a.m4s")

    def _map(pct, start, end):
        return start + int(pct * (end - start) / 100)

    try:
        _download_stream(session, video["base_url"], tmp_v, log_cb=log_cb,
                         progress_cb=(lambda p: progress_cb(_map(p, 0, 50), "video"))
                         if progress_cb else None)
        if audio:
            _download_stream(session, audio["base_url"], tmp_a, log_cb=log_cb,
                             progress_cb=(lambda p: progress_cb(_map(p, 50, 90), "audio"))
                             if progress_cb else None)
            if progress_cb:
                progress_cb(99, "merge")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(tmp_v), "-i", str(tmp_a),
                 "-c", "copy", str(out_path)],
                check=True, capture_output=True,
            )
        else:
            tmp_v.rename(out_path)
        if progress_cb:
            progress_cb(100, "done")
    finally:
        tmp_v.unlink(missing_ok=True)
        tmp_a.unlink(missing_ok=True)
    return out_path.exists()


def sanitize(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:80] or fallback


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def process_bilibili(session, link, outdir, p_choice=None, hd=False,
                     cookie=None, cookies_browser=None,
                     log_cb=None, progress_cb=None):
    session.headers.update({"User-Agent": UA, "Referer": REFERER})
    bvid, aid = resolve_input(link)
    if cookie:
        session.headers["Cookie"] = cookie
    view = get_view(session, bvid, aid)
    bvid = view["bvid"]
    title = view.get("title") or ""
    owner = (view.get("owner") or {}).get("name", "")
    pages = view.get("pages") or []

    _emit(log_cb, f"[*] 标题: {title}")
    _emit(log_cb, f"[*] UP主: {owner} | 分P数: {len(pages)}")

    # 选择要下载的分 P
    if p_choice and p_choice.upper() != "ALL":
        try:
            idxs = [int(p_choice) - 1]
        except ValueError:
            raise ValueError(f"--p 参数无效: {p_choice}(应为页码数字或 ALL)")
    else:
        idxs = list(range(len(pages)))

    ok = 0
    for idx in idxs:
        if idx < 0 or idx >= len(pages):
            _emit(log_cb, f"[!] 分P {idx + 1} 不存在，跳过")
            continue
        pg = pages[idx]
        cid = pg["cid"]
        part = (pg.get("part") or "").strip()
        page_no = pg.get("page", idx + 1)

        if len(pages) > 1:
            fname = f"{sanitize(title, bvid)}_p{page_no:02d}"
            if part:
                fname += f"_{sanitize(part, '')}"
        else:
            fname = sanitize(title, bvid)
        out_path = outdir / f"{fname}.mp4"
        if out_path.exists():
            _emit(log_cb, f"[*] 已存在，跳过: {out_path.name}")
            ok += 1
            continue

        _emit(log_cb, f"--- 下载 分P {page_no}/{len(pages)}: {part or title[:30]} ---")
        if progress_cb:
            progress_cb(0, f"page {page_no}/{len(pages)}")
        try:
            if hd:
                data = get_playurl(session, bvid, cid, qn=80, fnval=16)
                # 检查实际能拿到的最高清晰度
                dash_vids = (data.get("dash") or {}).get("video") or []
                top_q = max((v.get("id", 0) for v in dash_vids), default=0)
                if top_q < 80:
                    _emit(log_cb, f"[提示] 未登录最高仅 {top_q}P(1080P 需 --cookie 传登录态)")
                if not download_dash(session, data, out_path,
                                     log_cb=log_cb, progress_cb=progress_cb):
                    raise RuntimeError("DASH 无可用流")
            else:
                data = get_playurl(session, bvid, cid, qn=64, fnval=0)
                if not download_durl(session, data, out_path,
                                     log_cb=log_cb, progress_cb=progress_cb):
                    raise RuntimeError("mp4 直链无可用流(durl 为空)")
            size = out_path.stat().st_size
            _emit(log_cb, f"[OK] 已保存: {out_path} ({size} bytes)")
            ok += 1
        except Exception as e:
            _emit(log_cb, f"[x] 下载失败: {e}")
    return ok == len(idxs)

