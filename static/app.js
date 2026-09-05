/* ============================================================
   DY·ONLINE 前端逻辑
   数据流：textarea → POST /api/tasks → TaskStore(内存) →
           后台线程下载 → GET /api/tasks/{id} 轮询 → 渲染任务卡片
   ============================================================ */
"use strict";

/* ---------- DOM 引用 ---------- */
const $ = (sel) => document.querySelector(sel);

const linksEl   = $("#links");        // 链接输入框
const detectedEl = $("#detected");    // 平台识别徽章容器
const tasksEl   = $("#tasks");        // 任务列表容器
const emptyEl   = $("#empty");        // 空状态
const statsEl   = $("#stats");        // 顶部统计胶囊
const toastsEl  = $("#toasts");       // 通知容器
const btnSubmit = $("#btn-submit");
const btnPaste  = $("#btn-paste");
const btnClear  = $("#btn-clear");
const btnHide   = $("#btn-hide-done");
const tabsEl    = $("#tabs");

/* ---------- 全局状态 ---------- */
const tasks = new Map();        // taskId -> 任务快照（来自后端 JSON）
const snaps = new Map();        // taskId -> 上次渲染的 JSON 字符串，用于变更检测
const expanded = new Set();     // 展开了日志的任务 id
let filter = "all";             // 当前过滤：all | running | done | failed
let polling = false;            // 轮询循环是否已启动（防重复）
let submitting = false;         // 提交按钮防连点

/* ---------- 常量表 ---------- */
const STATUS = {
  queued:  { text: "排队中", ico: "◷" },
  running: { text: "下载中", ico: "◉" },
  done:    { text: "已完成", ico: "✓" },
  failed:  { text: "失败",   ico: "✕" },
};

const PHASE = { queued: "排队等待", download: "正在下载", done: "已结束" };

/* ============================================================
   工具函数
   ============================================================ */

/** XSS 防护：把 HTML 特殊字符转义成实体，防止日志/链接里注入脚本 */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** 平台识别：与后端 downloader.detect_platform 规则保持一致 */
function detectPlatform(link) {
  if (/bilibili\.com|b23\.tv/i.test(link)) return "bilibili";
  if (/^(BV[0-9A-Za-z]{10}|av\d+)$/i.test(link.trim())) return "bilibili";
  if (/douyin\.com|iesdouyin\.com/i.test(link)) return "douyin";
  if (/^\d{15,20}$/.test(link.trim())) return "douyin"; // 纯数字视频 ID
  return "unknown";
}

/** ISO 时间 → 相对时间（"3 分钟前"） */
function fmtRelative(iso) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  return Math.floor(diff / 86400) + " 天前";
}

/** 弹出右上角通知，3.5 秒后自动消失 */
function toast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<span class="t-dot"></span><span>${escapeHtml(msg)}</span>`;
  toastsEl.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");                       // 播放退场动画
    el.addEventListener("animationend", () => el.remove());
  }, 3500);
}

/** 从文件 URL 里取出文件名并解码（%E4%B9%8B → 之） */
function fileName(url) {
  try { return decodeURIComponent(url.split("/").pop()); }
  catch { return url.split("/").pop(); }
}

/* ============================================================
   输入区：实时平台识别
   ============================================================ */

/** input 事件触发：逐行识别平台，更新 "抖音×2 · B站×1 · 未识别×1" 徽章 */
function updateDetected() {
  const lines = linksEl.value.split("\n").map((s) => s.trim()).filter(Boolean);
  const cnt = { douyin: 0, bilibili: 0, unknown: 0 };
  lines.forEach((l) => cnt[detectPlatform(l)]++);
  detectedEl.innerHTML =
    (lines.length === 0) ? "" :
    (cnt.douyin   ? `<span class="det dy">抖音 ×<span class="n">${cnt.douyin}</span></span>` : "") +
    (cnt.bilibili ? `<span class="det bili">B站 ×<span class="n">${cnt.bilibili}</span></span>` : "") +
    (cnt.unknown  ? `<span class="det bad">未识别 ×<span class="n">${cnt.unknown}</span></span>` : "");
}

/* ============================================================
   提交任务
   ============================================================ */

async function submitLinks() {
  if (submitting) return;
  const links = linksEl.value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!links.length) { toast("请先粘贴至少一个链接", "error"); linksEl.focus(); return; }

  submitting = true;
  btnSubmit.disabled = true;
  btnSubmit.innerHTML = `<span class="spinner"></span><span class="btn-label">创建中…</span>`;

  try {
    // fetch 发送 JSON 到后端：POST /api/tasks {"links": [...]}
    const res = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ links }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const task = await res.json();   // 后端返回完整任务对象
    tasks.set(task.id, task);
    linksEl.value = "";
    updateDetected();
    toast(`任务已创建：${links.length} 个链接`, "success");
    startPolling();
    render();
  } catch (e) {
    toast("创建失败：" + e.message, "error");
  } finally {
    submitting = false;
    btnSubmit.disabled = false;
    btnSubmit.innerHTML = `
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      <span class="btn-label">开始下载</span>`;
  }
}

/* ============================================================
   轮询：只查未结束的任务，页面隐藏时暂停
   ============================================================ */

function startPolling() {
  if (polling) return;
  polling = true;
  setInterval(async () => {
    if (document.hidden) return;               // 后台标签页不浪费请求
    const active = [...tasks.values()].filter(
      (t) => t.status !== "done" && t.status !== "failed"
    );
    if (!active.length) return;                // 没有活跃任务就跳过本轮
    // 并行查询所有活跃任务（Promise.all 一起发）
    await Promise.all(active.map(async (t) => {
      try {
        const res = await fetch("/api/tasks/" + t.id);
        if (res.ok) tasks.set(t.id, await res.json());
      } catch { /* 网络抖动时下一轮重试 */ }
    }));
    render();
  }, 1500);
}

/** 页面加载 / 刷新后，从后端拉取全部历史任务（内存仓库还在） */
async function loadAll() {
  try {
    const res = await fetch("/api/tasks");
    if (res.ok) {
      (await res.json()).forEach((t) => tasks.set(t.id, t));
      render();
      if ([...tasks.values()].some((t) => t.status === "running" || t.status === "queued")) {
        startPolling();
      }
    }
  } catch { /* 首次启动后端无数据，忽略 */ }
}

/* ============================================================
   渲染
   ============================================================ */

/** 匹配当前过滤器的任务列表 */
function visibleTasks() {
  return [...tasks.values()]
    .filter((t) => {
      if (filter === "all") return true;
      if (filter === "running") return t.status === "running" || t.status === "queued";
      return t.status === filter;
    })
    .sort((a, b) => b.created_at.localeCompare(a.created_at)); // 新任务在前
}

function render() {
  renderStats();
  renderTasks();
}

/** 顶部统计：总任务 / 进行中 / 已完成 / 失败 */
function renderStats() {
  const all = [...tasks.values()];
  const run = all.filter((t) => t.status === "running" || t.status === "queued").length;
  const ok  = all.filter((t) => t.status === "done").length;
  const err = all.filter((t) => t.status === "failed").length;
  statsEl.innerHTML = `
    <span class="stat-chip"><span class="dot"></span>任务 <b>${all.length}</b></span>
    <span class="stat-chip c-run"><span class="dot"></span>进行中 <b>${run}</b></span>
    <span class="stat-chip c-ok"><span class="dot"></span>完成 <b>${ok}</b></span>
    <span class="stat-chip c-err"><span class="dot"></span>失败 <b>${err}</b></span>`;
}

function renderTasks() {
  const list = visibleTasks();

  emptyEl.hidden = list.length > 0;   // 有任务时隐藏空状态

  // 变更检测：任何任务快照变化或列表变化才重绘，避免每 1.5s 无谓刷新
  const fingerprint = list.map((t) => t.id + ":" + JSON.stringify(t)).join("|");
  if (fingerprint === renderTasks._fp) return;
  renderTasks._fp = fingerprint;

  tasksEl.innerHTML = list.map(taskCard).join("");

  // 展开日志的卡片自动滚到底部（看最新输出）
  expanded.forEach((id) => {
    const box = tasksEl.querySelector(`.logs[data-id="${id}"]`);
    if (box) box.scrollTop = box.scrollHeight;
  });
}

/** 单张任务卡片的 HTML 模板 */
function taskCard(t) {
  const st = STATUS[t.status] || { text: t.status, ico: "·" };
  const firstLink = (t.links && t.links[0]) || "";
  const more = t.links && t.links.length > 1 ? ` +${t.links.length - 1}` : "";

  const files = (t.files || []).map((f) => `
    <a href="${escapeHtml(f)}" download title="点击下载">
      <span aria-hidden="true">⬇</span><span class="fname">${escapeHtml(fileName(f))}</span>
    </a>`).join("");

  // 日志按类型着色：[OK] 绿色 / [x] 失败红色
  const logs = (t.logs || []).slice(-40).map((l) => {
    const cls = /^\[OK\]/.test(l) ? "ok" : /^\[x\]/.test(l) ? "err" : "";
    return `<div class="log ${cls}">${escapeHtml(l)}</div>`;
  }).join("");

  const isOpen = expanded.has(t.id);
  const phase = PHASE[t.phase] || t.phase || "";

  return `
  <article class="task ${escapeHtml(t.status)}">
    <div class="task-head">
      <span class="badge"><span class="ico">${st.ico}</span>${st.text}</span>
      <span class="task-link" title="${escapeHtml(firstLink)}">${escapeHtml(firstLink)}${more}</span>
      <span class="task-time">${escapeHtml(fmtRelative(t.created_at))}</span>
      <span class="task-id">#${escapeHtml(t.id.slice(0, 8))}</span>
    </div>
    <div class="bar"><div class="bar-fill" style="width:${Number(t.progress) || 0}%"></div></div>
    <div class="bar-meta">
      <span>${escapeHtml(phase)}</span>
      <span class="pct">${Number(t.progress) || 0}%</span>
    </div>
    ${t.error ? `<div class="error">${escapeHtml(t.error)}</div>` : ""}
    ${files ? `<div class="files">${files}</div>` : ""}
    <button class="log-toggle ${isOpen ? "open" : ""}" data-toggle="${escapeHtml(t.id)}">
      <span class="arrow">▸</span>运行日志（${(t.logs || []).length}）
    </button>
    ${isOpen && logs ? `<div class="logs" data-id="${escapeHtml(t.id)}">${logs}</div>` : ""}
  </article>`;
}

/* ============================================================
   事件绑定
   ============================================================ */

btnSubmit.addEventListener("click", submitLinks);

// Ctrl+Enter 快捷提交
linksEl.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") submitLinks();
});

linksEl.addEventListener("input", updateDetected);

btnClear.addEventListener("click", () => {
  linksEl.value = "";
  updateDetected();
  linksEl.focus();
});

// 从系统剪贴板读取（需要浏览器授权；失败时提示手动粘贴）
btnPaste.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) { toast("剪贴板是空的", "info"); return; }
    linksEl.value = linksEl.value ? linksEl.value.replace(/\s*$/, "\n") + text.trim() : text.trim();
    updateDetected();
    linksEl.focus();
  } catch {
    toast("无法读取剪贴板，请用 Ctrl+V 手动粘贴", "error");
  }
});

// 隐藏已结束任务（仅前端视图，不删后端数据）
btnHide.addEventListener("click", () => {
  let n = 0;
  [...tasks.keys()].forEach((id) => {
    const st = tasks.get(id).status;
    if (st === "done" || st === "failed") { tasks.delete(id); n++; }
  });
  if (filter !== "all") switchFilter("all");
  renderTasks._fp = null;   // 强制重绘
  render();
  toast(n ? `已隐藏 ${n} 个已结束任务` : "没有可隐藏的任务", n ? "success" : "info");
});

// 过滤标签切换（事件委托：一个监听器管所有 tab）
tabsEl.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) switchFilter(tab.dataset.filter);
});

function switchFilter(f) {
  filter = f;
  tabsEl.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.filter === f)
  );
  renderTasks._fp = null;   // 过滤条件变了，强制重绘
  renderTasks();
}

// 日志展开/收起（事件委托：卡片是整体重绘的，不能直接绑在按钮上）
tasksEl.addEventListener("click", (e) => {
  const btn = e.target.closest(".log-toggle");
  if (!btn) return;
  const id = btn.dataset.toggle;
  expanded.has(id) ? expanded.delete(id) : expanded.add(id);
  renderTasks._fp = null;
  renderTasks();
});

/* ---------- 启动 ---------- */
updateDetected();
loadAll();
