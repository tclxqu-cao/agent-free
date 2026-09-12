/* Flow Studio 编排画布 —— 零构建单页逻辑（原生 JS）
 * 无限画布性能策略：
 *  - 节点 DOM 缓存（Map）+ 视口裁剪：视口外节点 display:none，平移/缩放按需显隐；
 *  - 连线持久元素 diff 更新：拖动节点只重算与其相连的边（updateEdgesFor）；
 *  - pointermove 高频路径统一 requestAnimationFrame 合帧；
 *  - SVG overflow:visible 真·无边界（去掉固定 4000×3000 画布尺寸）；
 *  - 节点高度缓存（fitView / 端口坐标不再读 DOM）。
 */
"use strict";

/* ================= 全局状态 ================= */
const NODE_W = 188, NODE_H_DEFAULT = 58, CULL_MARGIN = 320;
/* 阻止 iOS Safari 的页面级捏合/双击缩放：双指手势只留给画布（缩放流程图） */
["gesturestart", "gesturechange", "gestureend"].forEach(t =>
  document.addEventListener(t, e => e.preventDefault()));
const state = {
  nodeTypes: {}, agents: [], flows: [],
  graph: null,            // 当前流程（纯 JSON）
  sel: null,              // {kind:'node'|'edge', id}
  view: { x: 40, y: 30, s: 1 },
  lastRun: null,          // 最近一次运行结果
  dirty: false,
  mediaAssets: [],        // 素材库
  mediaKind: "",          // 素材过滤
};
const $ = (q, el = document) => el.querySelector(q);
const $$ = (q, el = document) => [...el.querySelectorAll(q)];
const world = $("#world"), edgesSvg = $("#edges"), wrap = $("#canvas-wrap");
const nodeEls = new Map();      // 节点 id → DOM（视口内才建）
const edgeEls = new Map();      // 边 key → {g, path, text}
const heightCache = new Map();  // 节点 id → 高度（避免拖动时读 DOM）
const svgNS = "http://www.w3.org/2000/svg";

/* ================= API ================= */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body instanceof File || opts.body instanceof Blob
      ? opts.body : (opts.body ? JSON.stringify(opts.body) : undefined),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
  return data;
}

/* ================= 提示 / 弹窗 ================= */
let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg; t.className = kind; t.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.style.display = "none"), kind === "err" ? 4200 : 2400);
}
function openModal(html) { $("#modal").innerHTML = html; $("#modal-mask").classList.add("open"); }
function closeModal() { $("#modal-mask").classList.remove("open"); }
$("#modal-mask").addEventListener("mousedown", e => { if (e.target.id === "modal-mask") closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

/* ================= 几何 ================= */
const nodeById = id => state.graph?.nodes.find(n => n.id === id);
const edgesOf = () => state.graph?.edges || [];
function nodeElOf(id) { return nodeEls.get(id) || null; }
function edgeKey(e) {
  return e._id || (e._id = (crypto.randomUUID?.() || Math.random().toString(36).slice(2)));
}
function nodeH(id) { return heightCache.get(id) || NODE_H_DEFAULT; }
function portPos(n, side) {
  const x = n.pos?.x ?? 0, y = n.pos?.y ?? 0, h = nodeH(n.id);
  return side === "in" ? { x, y: y + h / 2 } : { x: x + NODE_W, y: y + h / 2 };
}
function edgeD(a, b) {
  const dx = Math.max(46, Math.abs(b.x - a.x) * 0.45);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}
function edgeMid(a, b) {
  // 三次贝塞尔 t=0.5 近似中点
  const p = [a, { x: (a.x + b.x) / 2, y: a.y }, { x: (a.x + b.x) / 2, y: b.y }, b];
  return { x: (p[0].x + 3 * p[1].x + 3 * p[2].x + p[3].x) / 8,
           y: (p[0].y + 3 * p[1].y + 3 * p[2].y + p[3].y) / 8 };
}
function applyView() {
  const v = state.view;
  world.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.s})`;
  const label = $("#zoom-label");
  if (label) label.textContent = `${Math.round(v.s * 100)}%`;
}

/* ================= 视口裁剪 ================= */
function visibleRect() {
  const v = state.view, r = wrap.getBoundingClientRect();
  return {
    x0: (-v.x - CULL_MARGIN) / v.s, y0: (-v.y - CULL_MARGIN) / v.s,
    x1: (r.width - v.x + CULL_MARGIN) / v.s, y1: (r.height - v.y + CULL_MARGIN) / v.s,
  };
}
function inViewport(n, rect) {
  const x = n.pos?.x ?? 0, y = n.pos?.y ?? 0;
  return x + NODE_W >= rect.x0 && x <= rect.x1 && y + nodeH(n.id) >= rect.y0 && y <= rect.y1;
}
function ensureNodeEl(n) {
  if (nodeEls.has(n.id)) return nodeEls.get(n.id);
  const el = buildNodeEl(n);
  nodeEls.set(n.id, el);
  world.appendChild(el);
  return el;
}
function updateVisibility() {
  if (!state.graph) return;
  const rect = visibleRect();
  const alive = new Set();
  for (const n of state.graph.nodes) {
    if (inViewport(n, rect)) { alive.add(n.id); ensureNodeEl(n); }
  }
  for (const [id, el] of nodeEls) {
    if (!alive.has(id)) { el.remove(); nodeEls.delete(id); }
  }
}

/* ================= 渲染 ================= */
function renderWorld() {
  if (!state.graph) return;
  for (const el of nodeEls.values()) el.remove();
  nodeEls.clear();
  updateVisibility();
  renderEdges();
  applyView();
  highlightRun();
  syncSelection();
  drawMinimap();
}

function buildNodeEl(n) {
  const meta = state.nodeTypes[n.type] || {};
  const el = document.createElement("div");
  el.className = `node t-${n.type}`;
  el.dataset.id = n.id;
  el.style.left = `${n.pos?.x ?? 0}px`; el.style.top = `${n.pos?.y ?? 0}px`;
  const sub = nodeSub(n);
  el.innerHTML = `
    <div class="type-dot" style="background:${meta.color || "#888"}"></div>
    <div class="node-head"><span class="ico">${meta.icon || "•"}</span>
      <span class="title">${esc(n.label || meta.label || n.id)}</span>
      <button class="del" title="删除节点">×</button></div>
    <div class="node-sub" title="${esc(sub)}">${esc(sub)}</div>
    ${n.type !== "start" ? '<div class="port in" title="输入"></div>' : ""}
    ${n.type !== "end" ? '<div class="port out" title="拖到我节点连线"></div>' : ""}
    <span class="run-badge"></span>`;
  requestAnimationFrame(() => {
    if (el.isConnected) heightCache.set(n.id, el.offsetHeight || NODE_H_DEFAULT);
  });
  const r = runOf(n.id);
  if (r && r.status && r.status !== "pending") {
    el.classList.add(`st-${r.status}`);
    applyBadge(el, r);
  }
  return el;
}

function nodeSub(n) {
  const p = n.params || {};
  switch (n.type) {
    case "agent": return `${p.agent || "?"}.${p.action || "?"}`;
    case "brain": return (p.prompt || "").slice(0, 36).replace(/\n/g, " ") || "推理本机 AgentRoam";
    case "llm": return (p.prompt || "").slice(0, 40).replace(/\n/g, " ") || "未配置提示词";
    case "template": return (p.template || "").slice(0, 40).replace(/\n/g, " ") || "空模板";
    case "end": return (p.output || "").slice(0, 40).replace(/\n/g, " ") || "空回复";
    case "http": return `${p.method || "GET"} ${(p.url || "").slice(0, 26)}`;
    case "storyboard": return `${p.shot_count || 4} 镜 · ${p.aspect_ratio || "16:9"}`;
    case "character": return `${p.name || "角色"} · ${(p.views || []).length} 方位`;
    case "keyframe": return `来源 ${(p.shots_source || "").slice(0, 24)}`;
    case "shot_video": return `每镜 ${p.duration ?? 3}s · ${p.aspect_ratio || "继承分镜"}`;
    case "merge_video": return `来源 ${(p.clips_source || "").slice(0, 26)}`;
    case "asset": return p.asset_id ? `素材 ${p.asset_id}` : "未选择素材";
    case "intent": {
      const names = (p.intents || []).map(i => i.name).filter(Boolean);
      return names.length ? `意图: ${names.join(" / ")}（else 兜底）` : "未配置意图清单";
    }
    case "start": {
      const keys = (p.inputs || []).map(i => i.key || i).filter(Boolean);
      return keys.length ? `输入: ${keys.join(", ")}` : "无输入参数";
    }
    default: return "";
  }
}

/* 边：持久元素 + diff 更新（避免 innerHTML 全量重建） */
function edgeLabel(e, from) {
  const isElse = (e.branch || "").trim().toLowerCase() === "else";
  if (from.type === "condition") return isElse ? "else" : (e.branch || "");
  if (from.type === "intent") return isElse ? "else" : (e.branch || "");
  return "";
}
function updateEdgeEl(key) {
  const item = edgeEls.get(key);
  const e = edgesOf().find(x => edgeKey(x) === key);
  if (!item || !e) return;
  const from = nodeById(e.from), to = nodeById(e.to);
  if (!from || !to) return;
  const a = portPos(from, "out"), b = portPos(to, "in");
  item.path.setAttribute("d", edgeD(a, b));
  item.path.classList.toggle("branch-else",
    (e.branch || "").trim().toLowerCase() === "else");
  if (item.text) {
    const m = edgeMid(a, b), label = edgeLabel(e, from);
    item.text.setAttribute("x", m.x); item.text.setAttribute("y", m.y - 6);
    item.text.textContent = label.length > 34 ? label.slice(0, 33) + "…" : label;
  }
}
function renderEdges() {
  const keys = new Set();
  for (const e of edgesOf()) {
    const key = edgeKey(e);
    keys.add(key);
    if (!edgeEls.has(key)) {
      const from = nodeById(e.from), to = nodeById(e.to);
      if (!from || !to) continue;
      const g = document.createElementNS(svgNS, "g");
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("class", "edge");
      path.dataset.id = key;
      g.appendChild(path);
      let text = null;
      if (edgeLabel(e, from)) {
        text = document.createElementNS(svgNS, "text");
        text.setAttribute("text-anchor", "middle");
        text.style.pointerEvents = "none";
        g.appendChild(text);
      }
      edgesSvg.appendChild(g);
      edgeEls.set(key, { g, path, text });
    }
    updateEdgeEl(key);
  }
  for (const [key, item] of edgeEls) {
    if (!keys.has(key)) { item.g.remove(); edgeEls.delete(key); }
  }
}
function updateEdgesFor(nodeId) {
  for (const e of edgesOf()) {
    if (e.from === nodeId || e.to === nodeId) updateEdgeEl(edgeKey(e));
  }
}

function syncSelection() {
  for (const [id, el] of nodeEls)
    el.classList.toggle("selected", state.sel?.kind === "node" && state.sel.id === id);
  for (const [key, item] of edgeEls)
    item.path.classList.toggle("selected", state.sel?.kind === "edge" && state.sel.id === key);
  renderInspector();
}

/* ================= 画布交互（Pointer Events：鼠标 / 触摸统一） ================= */
const activePtrs = new Map();   // pointerId -> {x, y}
let drag = null;                // {mode:'node'|'pan'|'connect', ...}
let pinch = null;               // 双指缩放 {dist, mid, base}
let frameQueued = false;
const ptrPos = e => ({ x: e.clientX, y: e.clientY });
const isMobile = () => window.matchMedia("(max-width: 860px)").matches;
function closeDrawers() {
  $("#palette").classList.remove("open");
  $("#inspector").classList.remove("open");
  $("#media-panel").classList.remove("open");
}
function scheduleFrame() {
  if (frameQueued) return;
  frameQueued = true;
  requestAnimationFrame(() => {
    frameQueued = false;
    if (!drag) return;
    if (drag.mode === "pan") { updateVisibility(); drawMinimap(); }
    else if (drag.mode === "node") updateEdgesFor(drag.id);
  });
}
function scheduleViewFrame() {
  if (frameQueued) return;
  frameQueued = true;
  requestAnimationFrame(() => {
    frameQueued = false;
    updateVisibility();
    drawMinimap();
  });
}

function startPinch() {
  const [a, b] = [...activePtrs.values()];
  return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
           mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
           base: { ...state.view } };
}
function movePinch() {
  const [a, b] = [...activePtrs.values()];
  const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const s2 = Math.min(2.5, Math.max(0.2, pinch.base.s * dist / pinch.dist));
  const wx = (pinch.mid.x - pinch.base.x) / pinch.base.s;
  const wy = (pinch.mid.y - pinch.base.y) / pinch.base.s;
  state.view = { s: s2, x: mid.x - wx * s2, y: mid.y - wy * s2 };
  applyView(); scheduleViewFrame();
}

wrap.addEventListener("pointerdown", e => {
  activePtrs.set(e.pointerId, ptrPos(e));
  if (activePtrs.size === 2) {          // 第二根手指落下 → 切换为双指缩放
    drag = null; wrap.classList.remove("panning");
    $("#temp-edge")?.remove();
    pinch = startPinch();
    return;
  }
  if (activePtrs.size > 2 || pinch) return;
  if (e.target.closest(".node") || e.target.closest("#run-panel")
      || e.target.closest("#edges") || e.target.closest(".canvas-fab")
      || e.target.closest("#minimap")) return;
  drag = { mode: "pan", sx: e.clientX, sy: e.clientY,
           ox: state.view.x, oy: state.view.y };
  wrap.classList.add("panning");
  closeDrawers();                        // 手机：点画布空白收起抽屉
});
wrap.addEventListener("pointerdown", e => {
  if (pinch || activePtrs.size > 1) return;
  const port = e.target.closest(".port.out");
  if (!port) return;
  e.stopPropagation();
  const nodeEl = port.closest(".node");
  drag = { mode: "connect", from: nodeEl.dataset.id };
  const temp = document.createElementNS(svgNS, "path");
  temp.setAttribute("class", "edge temp"); temp.id = "temp-edge";
  edgesSvg.appendChild(temp);
});
world.addEventListener("pointerdown", e => {
  if (pinch || activePtrs.size > 1) return;
  const nodeEl = e.target.closest(".node");
  if (!nodeEl) return;
  if (e.target.closest(".port.out") || e.target.closest(".del")) return;
  const n = nodeById(nodeEl.dataset.id);
  drag = { mode: "node", id: n.id, sx: e.clientX, sy: e.clientY,
           ox: n.pos?.x ?? 0, oy: n.pos?.y ?? 0, moved: false };
  e.preventDefault();
});
document.addEventListener("pointermove", e => {
  if (activePtrs.has(e.pointerId)) activePtrs.set(e.pointerId, ptrPos(e));
  if (pinch) { if (activePtrs.size >= 2) movePinch(); return; }
  if (!drag) return;
  const v = state.view;
  if (drag.mode === "pan") {
    state.view = { x: drag.ox + e.clientX - drag.sx, y: drag.oy + e.clientY - drag.sy, s: v.s };
    applyView(); scheduleFrame();
  } else if (drag.mode === "node") {
    const dx = (e.clientX - drag.sx) / v.s, dy = (e.clientY - drag.sy) / v.s;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    const n = nodeById(drag.id);
    n.pos = { x: Math.round(drag.ox + dx), y: Math.round(drag.oy + dy) };
    const el = nodeElOf(drag.id);
    if (el) { el.style.left = `${n.pos.x}px`; el.style.top = `${n.pos.y}px`; }
    scheduleFrame(); markDirty();
  } else if (drag.mode === "connect") {
    const from = nodeById(drag.from);
    const a = portPos(from, "out");
    const rect = wrap.getBoundingClientRect();
    const b = { x: (e.clientX - rect.left - v.x) / v.s, y: (e.clientY - rect.top - v.y) / v.s };
    $("#temp-edge")?.setAttribute("d", edgeD(a, b));
  }
});
function pointerEnd(e) {
  activePtrs.delete(e.pointerId);
  if (pinch) { if (activePtrs.size < 2) pinch = null; return; }
  if (!drag) return;
  if (drag.mode === "pan") wrap.classList.remove("panning");
  if (drag.mode === "node" && !drag.moved) select({ kind: "node", id: drag.id });
  if (drag.mode === "connect") {
    $("#temp-edge")?.remove();
    const nodeEl = document.elementFromPoint(e.clientX, e.clientY)?.closest(".node");
    const to = nodeEl?.dataset.id;
    if (to && to !== drag.from) connect(drag.from, to);
  }
  drag = null;
}
document.addEventListener("pointerup", pointerEnd);
document.addEventListener("pointercancel", pointerEnd);
function zoomAt(cx, cy, factor) {
  const v = state.view;
  const s2 = Math.min(2.5, Math.max(0.2, v.s * factor));
  state.view = { s: s2, x: cx - (cx - v.x) * (s2 / v.s), y: cy - (cy - v.y) * (s2 / v.s) };
  applyView(); updateVisibility(); drawMinimap();
}
wrap.addEventListener("wheel", e => {
  e.preventDefault();
  const rect = wrap.getBoundingClientRect();
  zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
}, { passive: false });
$("#btn-zoom-in").onclick = () => zoomAt(wrap.clientWidth / 2, wrap.clientHeight / 2, 1.3);
$("#btn-zoom-out").onclick = () => zoomAt(wrap.clientWidth / 2, wrap.clientHeight / 2, 1 / 1.3);

function fitView() {
  const ns = state.graph?.nodes || [];
  if (!ns.length) return;
  const pad = 46;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of ns) {
    const h = nodeH(n.id);
    minX = Math.min(minX, n.pos?.x ?? 0); minY = Math.min(minY, n.pos?.y ?? 0);
    maxX = Math.max(maxX, (n.pos?.x ?? 0) + NODE_W);
    maxY = Math.max(maxY, (n.pos?.y ?? 0) + h);
  }
  const vw = wrap.clientWidth, vh = wrap.clientHeight;
  const s = Math.min(1.2, Math.max(0.2, Math.min(
    (vw - pad * 2) / Math.max(1, maxX - minX),
    (vh - pad * 2) / Math.max(1, maxY - minY))));
  state.view = { s, x: (vw - (maxX - minX) * s) / 2 - minX * s,
                 y: (vh - (maxY - minY) * s) / 2 - minY * s };
  applyView(); updateVisibility(); drawMinimap();
}

/* ================= 小地图 ================= */
const TYPE_HUES = { storyboard: "#db2777", character: "#d97706", keyframe: "#0891b2",
  shot_video: "#7c3aed", merge_video: "#059669", asset: "#64748b" };
function drawMinimap() {
  const cv = $("#minimap"), ctx = cv.getContext("2d");
  const ns = state.graph?.nodes || [];
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = "rgba(15,23,42,.78)";
  ctx.fillRect(0, 0, cv.width, cv.height);
  if (!ns.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of ns) {
    minX = Math.min(minX, n.pos?.x ?? 0); minY = Math.min(minY, n.pos?.y ?? 0);
    maxX = Math.max(maxX, (n.pos?.x ?? 0) + NODE_W);
    maxY = Math.max(maxY, (n.pos?.y ?? 0) + nodeH(n.id));
  }
  const pad = 30, w = maxX - minX + pad * 2, h = maxY - minY + pad * 2;
  const k = Math.min(cv.width / w, cv.height / h);
  const ox = (cv.width - w * k) / 2, oy = (cv.height - h * k) / 2;
  const tx = x => ox + (x - minX + pad) * k, ty = y => oy + (y - minY + pad) * k;
  for (const n of ns) {
    ctx.fillStyle = TYPE_HUES[n.type] ||
      (state.nodeTypes[n.type] || {}).color || "#94a3b8";
    ctx.fillRect(tx(n.pos?.x ?? 0), ty(n.pos?.y ?? 0),
                 Math.max(3, NODE_W * k), Math.max(2, nodeH(n.id) * k));
  }
  // 视口框
  const r = wrap.getBoundingClientRect(), v = state.view;
  ctx.strokeStyle = "#38bdf8"; ctx.lineWidth = 1.5;
  ctx.strokeRect(tx(-v.x / v.s), ty(-v.y / v.s), r.width / v.s * k, r.height / v.s * k);
  cv._nav = { minX, minY, k, ox: ox + pad * k, oy: oy + pad * k };
}
$("#minimap").addEventListener("pointerdown", e => {
  const cv = $("#minimap");
  if (!cv._nav || !state.graph) return;
  const rect = cv.getBoundingClientRect();
  const mx = (e.clientX - rect.left) * (cv.width / rect.width);
  const my = (e.clientY - rect.top) * (cv.height / rect.height);
  const nav = cv._nav, r = wrap.getBoundingClientRect();
  const wx = (mx - nav.ox) / nav.k + nav.minX;
  const wy = (my - nav.oy) / nav.k + nav.minY;
  state.view = { s: state.view.s,
                 x: r.width / 2 - wx * state.view.s,
                 y: r.height / 2 - wy * state.view.s };
  applyView(); updateVisibility(); drawMinimap();
});

function select(sel) {
  state.sel = sel; syncSelection();
  if (sel?.kind === "node" && isMobile()) $("#inspector").classList.add("open");
}

function connect(fromId, toId) {
  if (edgesOf().some(e => e.from === fromId && e.to === toId)) return toast("已存在相同连线", "err");
  const from = nodeById(fromId);
  if (from.type === "condition") askBranch(null, b => {
    if (b === null) return;
    state.graph.edges.push({ from: fromId, to: toId, branch: b });
    renderEdges(); markDirty();
  });
  else if (from.type === "intent")
    askBranch((from.params.intents || []).map(i => i.name).filter(Boolean), b => {
      if (b === null) return;
      state.graph.edges.push({ from: fromId, to: toId, branch: b });
      renderEdges(); markDirty();
    });
  else { state.graph.edges.push({ from: fromId, to: toId }); renderEdges(); markDirty(); }
}

function askBranch(options, cb) {
  if (options && options.length) {
    openModal(`
      <h2>意图分支</h2>
      <div class="field"><label>命中哪个意图时走这条连线</label>
        <select id="branch-select">${options.map(o =>
          `<option value="${esc(o)}">${esc(o)}</option>`).join("")}
          <option value="else">else —— 兜底（其它/未命中）</option></select></div>
      <div class="btn-row">
        <button id="branch-cancel">取消</button>
        <button id="branch-ok" class="primary">确定</button></div>`);
  } else {
    openModal(`
      <h2>条件分支</h2>
      <div class="field"><label>分支表达式</label>
        <input id="branch-input" placeholder="例：match.passed_count > 0">
        <div class="hint">变量：节点 id（如 match.total）、input.xxx、vars.today；
          支持 > < == != in and or not、算术。留空用按钮设为 else 兜底。</div></div>
      <div class="btn-row">
        <button id="branch-else">else 兜底分支</button>
        <button id="branch-cancel">取消</button>
        <button id="branch-ok" class="primary">确定</button></div>`);
  }
  const finish = v => { closeModal(); cb(v); };
  $("#branch-ok").onclick = () =>
    finish(options && options.length ? $("#branch-select").value : $("#branch-input").value.trim());
  if ($("#branch-else")) $("#branch-else").onclick = () => finish("else");
  $("#branch-cancel").onclick = () => finish(null);
  const first = options && options.length ? $("#branch-select") : $("#branch-input");
  first.focus();
}

/* 节点删除 / 边删除 / 双击改分支 */
world.addEventListener("click", e => {
  const del = e.target.closest(".del");
  if (del) { deleteNode(del.closest(".node").dataset.id); }
});
edgesSvg.addEventListener("click", e => {
  const p = e.target.closest("path.edge");
  if (p) select({ kind: "edge", id: p.dataset.id });
});
edgesSvg.addEventListener("dblclick", e => {
  const p = e.target.closest("path.edge");
  if (!p) return;
  const edge = edgesOf().find(x => (x._id || "") === p.dataset.id);
  const from = nodeById(edge?.from);
  if (!edge || (from?.type !== "condition" && from?.type !== "intent")) return;
  const options = from.type === "intent"
    ? (from.params.intents || []).map(i => i.name).filter(Boolean) : null;
  askBranch(options, b => {
    if (b === null) return;
    edge.branch = b;
    renderEdges(); markDirty();
  });
});
document.addEventListener("keydown", e => {
  if (e.key === "Delete" && state.sel && !e.target.matches("input, textarea, select")) {
    if (state.sel.kind === "node") deleteNode(state.sel.id);
    else {
      const idx = edgesOf().findIndex(x => x._id === state.sel.id);
      if (idx >= 0) { state.graph.edges.splice(idx, 1); renderEdges(); markDirty(); }
      select(null);
    }
  }
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); saveFlow(); }
});

function deleteNode(id) {
  const n = nodeById(id);
  if (!n) return;
  if ((n.type === "start" || n.type === "end") &&
      state.graph.nodes.filter(x => x.type === n.type).length <= 1)
    return toast(`${n.type === "start" ? "开始" : "结束"}节点不能删除（可改参数）`, "err");
  state.graph.nodes = state.graph.nodes.filter(x => x.id !== id);
  state.graph.edges = edgesOf().filter(e => e.from !== id && e.to !== id);
  const el = nodeEls.get(id);
  if (el) { el.remove(); nodeEls.delete(id); }
  select(null); renderEdges(); drawMinimap(); markDirty();
}

/* ================= 属性面板 ================= */
function renderInspector() {
  const box = $("#insp-body");
  if (!state.graph) { box.innerHTML = '<div class="empty-tip">先选择或新建一个流程。</div>'; return; }
  if (!state.sel) return renderFlowSettings(box);
  if (state.sel.kind === "edge") return renderEdgeInfo(box);
  renderNodeForm(box);
}

function renderFlowSettings(box) {
  const g = state.graph;
  box.innerHTML = `
    <h3>流程设置</h3>
    <div class="field"><label>名称</label><input id="f-name" value="${esc(g.name)}"></div>
    <div class="field"><label>ID</label><input id="f-id" value="${esc(g.id)}" ${g._saved ? "disabled" : ""}></div>
    <div class="field"><label>描述</label><textarea id="f-desc" rows="3">${esc(g.description)}</textarea></div>
    <div class="field"><label>触发话术（每行一条，供意图路由）</label>
      <textarea id="f-triggers" rows="4">${esc((g.triggers || []).join("\n"))}</textarea></div>
    <div class="btn-row"><button id="f-save" class="primary">保存流程</button></div>
    <div class="palette-note" style="margin-top:10px">点击画布节点编辑参数；运行结果在底部面板，点节点查看输入输出与生成的图片/视频。</div>
    ${renderRunOutput()}`;
  bindFlowSettings();
}

function bindFlowSettings() {
  const g = state.graph;
  $("#f-name").oninput = e => { g.name = e.target.value; markDirty(); };
  $("#f-desc").oninput = e => { g.description = e.target.value; markDirty(); };
  $("#f-triggers").oninput = e => {
    g.triggers = e.target.value.split("\n").map(s => s.trim()).filter(Boolean); markDirty();
  };
  $("#f-id").oninput = e => { g.id = e.target.value.trim(); markDirty(); };
  $("#f-save").onclick = saveFlow;
}

function renderEdgeInfo(box) {
  const edge = edgesOf().find(x => x._id === state.sel.id);
  if (!edge) { box.innerHTML = '<div class="empty-tip">该连线已不存在。</div>'; return; }
  const from = nodeById(edge.from), to = nodeById(edge.to);
  box.innerHTML = `
    <h3>连线</h3>
    <div class="empty-tip">${esc(from?.label || edge.from)} → ${esc(to?.label || edge.to)}</div>
    ${from?.type === "condition" ? `
      <div class="field"><label>分支表达式</label><input id="e-branch" value="${esc(edge.branch || "")}"></div>
      <div class="btn-row">
        <button id="e-else">设为 else</button>
        <button id="e-del" class="danger">删除连线</button>
        <button id="e-ok" class="primary">应用</button></div>` : `
      <div class="btn-row"><button id="e-del" class="danger">删除连线</button></div>`}`;
  $("#e-del").onclick = () => {
    state.graph.edges = edgesOf().filter(x => x !== edge);
    select(null); renderEdges(); markDirty();
  };
  if ($("#e-ok")) {
    $("#e-ok").onclick = () => { edge.branch = $("#e-branch").value.trim() || "else"; renderEdges(); markDirty(); };
    $("#e-else").onclick = () => { $("#e-branch").value = "else"; };
  }
}

function renderNodeForm(box) {
  const n = nodeById(state.sel.id);
  if (!n) { box.innerHTML = '<div class="empty-tip">节点已删除。</div>'; return; }
  const meta = state.nodeTypes[n.type] || {};
  const fields = (meta.form || []).map(f => {
    if (n.type === "agent" && ["agent", "action", "args"].includes(f.key)) return "";
    if (n.type === "intent" && f.key === "intents") return "";
    if (f.widget === "views") return "";
    if (f.widget === "asset") return "";
    return formField(n, f);
  }).join("");
  const agentFields = n.type === "agent" ? agentFieldHtml(n) : "";
  const intentFields = n.type === "intent" ? intentFieldHtml(n) : "";
  const viewsFields = viewsFieldHtml(n);
  const assetFields = assetFieldHtml(n);
  box.innerHTML = `
    <span class="badge" style="background:${meta.color || "#888"}">${meta.icon || ""} ${meta.label || n.type}</span>
    <div class="palette-note" style="margin:0 0 6px">${esc(meta.desc || "")}</div>
    <div class="field"><label>节点名称</label><input id="n-label" value="${esc(n.label)}"></div>
    ${fields}
    ${agentFields}
    ${intentFields}
    ${viewsFields}
    ${assetFields}
    <div class="btn-row"><button id="n-del" class="danger">删除节点</button></div>
    ${renderRunOutput(n.id)}`;
  $("#n-label").oninput = e => { n.label = e.target.value; refreshNodeEl(n); markDirty(); };
  bindFormFields(n);
  if (n.type === "agent") bindAgentSelects(n);
  if (n.type === "intent") bindIntentEditor(n);
  if (n.type === "character") bindViewsEditor(n);
  if (n.type === "asset") bindAssetSelect(n);
  $("#n-del").onclick = () => deleteNode(n.id);
}

function formField(n, f) {
  const val = n.params[f.key];
  const common = `data-pkey="${f.key}" data-widget="${f.widget}"`;
  switch (f.widget) {
    case "textarea": return `<div class="field"><label>${f.label}</label>
      <textarea ${common} rows="${f.rows || 4}">${esc(getVal(n, f.key))}</textarea>
      ${f.hint ? `<div class="hint">${f.hint}</div>` : ""}</div>`;
    case "json": return `<div class="field"><label>${f.label}</label>
      <textarea ${common} rows="${f.rows || 5}">${esc(JSON.stringify(val ?? tryDefault(f), null, 2))}</textarea>
      ${f.hint ? `<div class="hint">${f.hint}</div>` : ""}</div>`;
    case "bool": return `<div class="field"><label>${f.label}
      <input type="checkbox" ${common} ${val ?? tryDefault(f) ? "checked" : ""} style="width:auto"></label></div>`;
    case "number": return `<div class="field"><label>${f.label}</label>
      <input type="number" ${common} value="${val ?? tryDefault(f) ?? ""}"></div>`;
    case "select": return `<div class="field"><label>${f.label}</label>
      <select ${common}>${(f.options || []).map(o =>
        `<option value="${esc(o)}" ${(val ?? tryDefault(f)) === o ? "selected" : ""}>${esc(o || "（继承分镜层）")}</option>`).join("")}
      </select></div>`;
    default: return `<div class="field"><label>${f.label}</label>
      <input ${common} value="${esc(getVal(n, f.key) || tryDefault(f) || "")}">
      ${f.hint ? `<div class="hint">${f.hint}</div>` : ""}</div>`;
  }
}
function getVal(n, key) { const v = n.params[key]; return typeof v === "string" ? v : (v == null ? "" : JSON.stringify(v)); }
function tryDefault(f) { return f.default; }

function bindFormFields(n) {
  $$("#insp-body [data-pkey]").forEach(el => {
    const key = el.dataset.pkey, widget = el.dataset.widget;
    const save = () => {
      let v;
      if (widget === "bool") v = el.checked;
      else if (widget === "number") v = el.value === "" ? null : Number(el.value);
      else if (widget === "json") {
        try { v = JSON.parse(el.value || "null"); el.style.border = ""; }
        catch { el.style.border = "1px solid var(--err)"; return; }
      } else v = el.value;
      n.params[key] = v;
      refreshNodeEl(n); markDirty();
    };
    el.addEventListener(el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "input", save);
  });
}

function refreshNodeEl(n) {
  const el = nodeEls.get(n.id);
  if (!el) return;
  const fresh = buildNodeEl(n);
  fresh.className += ` ${[...el.classList].filter(c => c.startsWith("st-")).join(" ")}`;
  el.replaceWith(fresh);
  nodeEls.set(n.id, fresh);
  updateEdgesFor(n.id);
}

/* ================= agent 下拉 ================= */
function agentFieldHtml(n) {
  const p = n.params;
  const agent = state.agents.find(a => a.agent === p.agent);
  const opts = state.agents.map(a =>
    `<option value="${esc(a.agent)}" ${a.agent === p.agent ? "selected" : ""}>${esc(a.agent)}</option>`).join("");
  const actions = agent ? agent.actions.map(x =>
    `<option value="${esc(x.action)}" ${x.action === p.action ? "selected" : ""}>${esc(x.action)} — ${esc(x.description)}</option>`).join("") : "";
  return `
    <div class="field"><label>Agent</label><select data-pkey="agent" data-widget="text">
      ${opts || '<option value="">（无已注册 agent）</option>'}</select></div>
    <div class="field"><label>能力 Action</label><select data-pkey="action" data-widget="text">
      ${actions || '<option value="">—</option>'}</select></div>
    <div class="field"><label>参数（JSON，值支持 {{模板}}）</label>
      <textarea data-pkey="args" data-widget="json" rows="4">${esc(JSON.stringify(p.args || {}, null, 2))}</textarea>
      <div class="hint" id="action-params">${actionParamsHint(agent, p.action)}</div></div>`;
}
function actionParamsHint(agent, action) {
  const a = agent?.actions.find(x => x.action === action);
  if (!a || !a.params.length) return "该能力无必填参数。";
  return "参数：" + a.params.map(p =>
    `${p.key}${p.required ? "*" : ""}（${p.type || "string"}）${p.description ? " — " + p.description : ""}`).join("；");
}

/* ================= 意图清单编辑器 ================= */
function intentFieldHtml(n) {
  const intents = n.params.intents || [];
  return `
    <div class="field"><label>意图清单（命中即走同名连线，未命中走 else）</label>
      <div id="intent-list">${intents.map((it, i) => intentRowHtml(it, i)).join("")}</div>
      <button id="intent-add" type="button" style="margin-top:6px">+ 添加意图</button></div>`;
}
function intentRowHtml(it, i) {
  return `<div class="intent-row" data-idx="${i}">
    <input data-ikey="name" value="${esc(it.name || "")}" placeholder="意图名（如 find_jobs）">
    <input data-ikey="description" value="${esc(it.description || "")}" placeholder="描述（给 LLM 看的判断依据）">
    <textarea data-ikey="samples" rows="2" placeholder="示例话术，每行一条（关键词降级依据）">${esc((it.samples || []).join("\n"))}</textarea>
    <button class="intent-del" type="button" title="删除意图">×</button>
  </div>`;
}
function bindIntentEditor(n) {
  n.params.intents = n.params.intents || [];
  $$("#intent-list [data-ikey]").forEach(el => el.addEventListener("input", () => {
    const it = n.params.intents[+el.closest(".intent-row").dataset.idx];
    const k = el.dataset.ikey;
    it[k] = k === "samples"
      ? el.value.split("\n").map(s => s.trim()).filter(Boolean) : el.value;
    refreshNodeEl(n); markDirty();
  }));
  $$("#intent-list .intent-del").forEach(btn => btn.onclick = () => {
    n.params.intents.splice(+btn.closest(".intent-row").dataset.idx, 1);
    renderInspector();
  });
  $("#intent-add").onclick = () => {
    n.params.intents.push({ name: `intent_${n.params.intents.length + 1}`,
                            description: "", samples: [] });
    renderInspector();
  };
}

/* ================= 角色方位编辑器 ================= */
function viewsFieldHtml(n) {
  if (n.type !== "character") return "";
  const views = n.params.views || [];
  return `
    <div class="field"><label>方位清单（每个方位生成一张设定图）</label>
      <div id="views-list">${views.map((v, i) => `
        <div class="views-row" data-idx="${i}">
          <input data-vkey value="${esc(v)}" placeholder="如：正面 / 左侧 45° / 背面">
          <button class="views-del" type="button" title="删除方位">×</button>
        </div>`).join("")}</div>
      <button id="views-add" type="button" style="margin-top:6px">+ 添加方位</button></div>`;
}
function bindViewsEditor(n) {
  n.params.views = n.params.views || ["正面"];
  $$("#views-list [data-vkey]").forEach(el => el.addEventListener("input", () => {
    n.params.views[+el.closest(".views-row").dataset.idx] = el.value;
    refreshNodeEl(n); markDirty();
  }));
  $$("#views-list .views-del").forEach(btn => btn.onclick = () => {
    n.params.views.splice(+btn.closest(".views-row").dataset.idx, 1);
    renderInspector();
  });
  $("#views-add").onclick = () => {
    n.params.views.push(`方位${n.params.views.length + 1}`);
    renderInspector();
  };
}

/* ================= 素材选择器（asset 节点） ================= */
function assetFieldHtml(n) {
  if (n.type !== "asset") return "";
  const opts = state.mediaAssets.map(a =>
    `<option value="${esc(a.id)}" ${a.id === n.params.asset_id ? "selected" : ""}>
      ${esc(a.name)}（${a.kind}）</option>`).join("");
  const cur = state.mediaAssets.find(a => a.id === n.params.asset_id);
  return `
    <div class="field"><label>选择素材（素材库面板可上传）</label>
      <select id="asset-select"><option value="">（未选择）</option>${opts}</select></div>
      <button id="asset-refresh" type="button">↻ 刷新素材列表</button>
    <div id="asset-preview">${cur ? mediaThumbHtml(cur) : ""}</div>`;
}
function bindAssetSelect(n) {
  const sel = $("#asset-select");
  if (sel) sel.onchange = () => {
    n.params.asset_id = sel.value;
    const cur = state.mediaAssets.find(a => a.id === sel.value);
    $("#asset-preview").innerHTML = cur ? mediaThumbHtml(cur) : "";
    refreshNodeEl(n); markDirty();
  };
  $("#asset-refresh")?.addEventListener("click", async () => {
    await loadMedia();
    renderInspector();
  });
}

/* ================= 运行输出展示（含图片/视频预览） ================= */
function runOf(nodeId) {
  return (state.lastRun?.node_runs || []).find(x => x.node_id === nodeId) || null;
}
function mediaThumbHtml(a) {
  const url = a.url || `/media/files/${a.path}`;
  if (a.kind === "image")
    return `<img class="media-thumb" src="${esc(url)}" alt="${esc(a.name)}" loading="lazy">`;
  if (a.kind === "video")
    return `<video class="media-thumb" src="${esc(url)}" controls preload="metadata"></video>`;
  if (a.kind === "audio")
    return `<audio class="media-thumb" src="${esc(url)}" controls preload="metadata"></audio>`;
  return `<div class="media-thumb media-file">📄 ${esc(a.name)}</div>`;
}
function mediaGridFromOutput(output) {
  if (!output) return "";
  let items = [];
  if (Array.isArray(output.views)) items = items.concat(output.views);
  if (Array.isArray(output.frames)) items = items.concat(output.frames);
  if (Array.isArray(output.clips)) items = items.concat(output.clips);
  if (output.url && output.path) items = [output];
  if (!items.length) return "";
  const cells = items.map(it => {
    if (!it.url && !it.path) return "";
    const kind = String(it.path || "").endsWith(".mp4") || String(it.url || "").includes(".mp4")
      ? "video" : "image";
    const url = it.url || `/media/files/${it.path}`;
    return kind === "video"
      ? `<video class="media-thumb" src="${esc(url)}" controls preload="metadata"></video>`
      : `<img class="media-thumb" src="${esc(url)}" alt="" loading="lazy">`;
  }).filter(Boolean).join("");
  return cells ? `<div class="media-grid">${cells}</div>` : "";
}
function renderRunOutput(nodeId) {
  const run = state.lastRun;
  if (!run) return "";
  const part = nodeId ? runOf(nodeId) : null;
  if (nodeId && !part) return "";
  const data = nodeId ? { output: part.output, error: part.error, status: part.status }
                      : { output: run.output, error: run.error, status: run.status };
  return `
    <h3 style="margin-top:10px">${nodeId ? "节点输入输出" : "流程输出"}</h3>
    <div class="field"><label>状态：${data.status || "-"}</label></div>
    ${data.error ? `<div class="field"><label style="color:var(--err)">错误</label>
      <div class="json-view">${esc(data.error)}</div></div>` : ""}
    ${mediaGridFromOutput(data.output)}
    <div class="json-view">${esc(fmtJson(data.output))}</div>`;
}
function fmtJson(v) { try { return JSON.stringify(v, null, 2); } catch { return String(v); } }

/* ================= 运行状态高亮 ================= */
function applyBadge(el, r) {
  const colors = { success: "var(--ok)", failed: "var(--err)", skipped: "var(--skip)", running: "#2563eb" };
  const names = { success: "成功", failed: "失败", skipped: "降级", running: "运行中" };
  const badge = el.querySelector(".run-badge");
  if (!badge) return;
  badge.textContent = names[r.status] || "";
  badge.style.background = colors[r.status] || "#888";
}
function highlightRun(upTo = Infinity) {
  const runs = (state.lastRun?.node_runs || []).slice(0, upTo === Infinity ? undefined : upTo + 1);
  const byNode = Object.fromEntries(runs.map(r => [r.node_id, r]));
  for (const el of nodeEls.values()) {
    el.classList.remove("st-running", "st-success", "st-failed", "st-skipped");
    const r = byNode[el.dataset.id];
    if (!r || r.status === "pending") { el.querySelector(".run-badge").textContent = ""; continue; }
    el.classList.add(`st-${r.status}`);
    applyBadge(el, r);
  }
}

async function animateRun(run) {
  state.lastRun = run;
  for (const el of nodeEls.values())
    el.classList.remove("st-running", "st-success", "st-failed", "st-skipped");
  const runs = run.node_runs || [];
  for (let i = 0; i < runs.length; i++) {
    const el = nodeElOf(runs[i].node_id);
    if (!el) continue;
    el.classList.add("st-running");
    await sleep(Math.min(160, 60 + (runs[i].ms || 0) / 8));
    el.classList.remove("st-running");
    highlightRun(i);
  }
  highlightRun();
  renderRunPanel(run);
  renderInspector();
  loadMedia();   // 生成的素材入库后刷新素材库（异步，不阻塞）
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ================= 运行面板 ================= */
function renderRunPanel(run) {
  const panel = $("#run-panel");
  panel.classList.add("open");
  $("#rp-title").textContent = `运行 ${run.run_id} · ${run.flow_name}`;
  const st = $("#rp-status");
  st.textContent = run.status === "success" ? "✅ 成功" : `❌ 失败：${run.error || ""}`;
  st.style.color = run.status === "success" ? "var(--ok)" : "var(--err)";
  const names = { success: "成功", failed: "失败", skipped: "降级" };
  const colors = { success: "var(--ok)", failed: "var(--err)", skipped: "var(--skip)" };
  $("#rp-list").innerHTML = (run.node_runs || []).map(r => `
    <div class="rp-row" data-node="${esc(r.node_id)}">
      <span class="st" style="background:${colors[r.status] || "#888"}">${names[r.status] || r.status}</span>
      <b>${esc(r.label)}</b>
      <span class="errmsg">${esc(r.error || "")}</span>
      <span class="ms">${r.ms}ms</span></div>`).join("");
  $$("#rp-list .rp-row").forEach(row => row.onclick = () => {
    const n = nodeById(row.dataset.node);
    if (n) select({ kind: "node", id: n.id });
  });
}
$("#rp-close").onclick = () => $("#run-panel").classList.remove("open");

/* ================= 工具函数 ================= */
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function markDirty() { state.dirty = true; }
function maxPos() {
  let x = 40, y = 30;
  for (const n of state.graph?.nodes || []) x = Math.max(x, (n.pos?.x || 0));
  return { x: x + 60, y: Math.max(...state.graph?.nodes?.map(n => n.pos?.y || 0), 0) };
}

/* ================= 流程加载 / 保存 ================= */
async function loadFlowList(selectId) {
  state.flows = await api("/api/flows");
  const sel = $("#flow-select");
  sel.innerHTML = state.flows.map(f => `<option value="${esc(f.id)}">${esc(f.name)}</option>`).join("")
    || '<option value="">（暂无流程）</option>';
  if (selectId) sel.value = selectId;
  return sel.value;
}

async function loadFlow(id) {
  if (!id) { state.graph = null; state.lastRun = null; select(null); renderWorld(); renderInspector(); return; }
  const data = await api(`/api/flows/${encodeURIComponent(id)}`);
  data._saved = true;
  state.graph = data;
  state.lastRun = null;
  state.sel = null;
  state.view = { x: 40, y: 30, s: 1 };
  renderWorld(); renderInspector();
  fitView();
  closeDrawers();
  $("#run-panel").classList.remove("open");
}

async function saveFlow() {
  if (!state.graph) return;
  const g = state.graph;
  if (!g.name.trim()) return toast("请先填写流程名称", "err");
  if (!g.id.trim()) return toast("请先填写流程 ID（字母数字-）", "err");
  try {
    const saved = await api(g._saved ? `/api/flows/${encodeURIComponent(g.id)}` : "/api/flows",
      { method: g._saved ? "PUT" : "POST", body: graphBody() });
    saved._saved = true;
    state.graph = saved;
    state.dirty = false;
    toast("已保存 ✅", "ok");
    await loadFlowList(g.id);
    select(null);
  } catch (e) { toast(`保存失败：${e.message}`, "err"); }
}
function graphBody() {
  const g = state.graph;
  return { id: g.id, name: g.name, description: g.description, triggers: g.triggers || [],
           nodes: (g.nodes || []).map(n => ({ id: n.id, type: n.type, label: n.label,
                                              params: n.params || {}, pos: n.pos || {} })),
           edges: (g.edges || []).map(e => ({ from: e.from, to: e.to, ...(e.branch ? { branch: e.branch } : {}) })) };
}

/* 新建 / 删除流程 */
$("#btn-new").onclick = () => {
  openModal(`
    <h2>新建流程</h2>
    <div class="field"><label>流程名称</label><input id="nf-name" placeholder="例：客服工单分流"></div>
    <div class="field"><label>流程 ID（字母数字-）</label><input id="nf-id" placeholder="留空自动生成"></div>
    <div class="btn-row"><button id="nf-cancel">取消</button>
      <button id="nf-ok" class="primary">创建</button></div>`);
  $("#nf-cancel").onclick = closeModal;
  $("#nf-ok").onclick = async () => {
    const name = $("#nf-name").value.trim();
    if (!name) return toast("请填写名称", "err");
    try {
      const g = await api("/api/flows", { method: "POST", body: {
        id: $("#nf-id").value.trim(), name,
        nodes: [
          { id: "start", type: "start", label: "开始", params: { inputs: [] }, pos: { x: 60, y: 160 } },
          { id: "end", type: "end", label: "结束", params: { output: "{{vars.today}} 完成" }, pos: { x: 420, y: 160 } },
        ],
        edges: [{ from: "start", to: "end" }],
      } });
      closeModal();
      await loadFlowList(g.id);
      await loadFlow(g.id);
      toast("已创建 ✅", "ok");
    } catch (e) { toast(`创建失败：${e.message}`, "err"); }
  };
};
$("#btn-del").onclick = async () => {
  const id = $("#flow-select").value;
  if (!id || !confirm(`确认删除流程「${id}」？`)) return;
  await api(`/api/flows/${encodeURIComponent(id)}`, { method: "DELETE" });
  await loadFlowList();
  await loadFlow($("#flow-select").value);
  toast("已删除", "ok");
};
$("#btn-save").onclick = saveFlow;
$("#flow-select").onchange = e => loadFlow(e.target.value);

/* ================= 手机抽屉 / 适配 ================= */
$("#btn-palette").onclick = () => {
  $("#palette").classList.toggle("open");
  $("#inspector").classList.remove("open");
  $("#media-panel").classList.remove("open");
};
$("#btn-insp").onclick = () => {
  $("#inspector").classList.toggle("open");
  $("#palette").classList.remove("open");
  $("#media-panel").classList.remove("open");
};
$("#palette-close").onclick = () => $("#palette").classList.remove("open");
$("#insp-close").onclick = () => $("#inspector").classList.remove("open");
$("#btn-fit").onclick = fitView;
let resizeTimer;
window.addEventListener("resize", () => {
  closeDrawers();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(fitView, 200);   // 视口变化（旋转/改窗口）后自动重新适配
});

/* ================= 模型设置 ================= */
const MODEL_FIELDS = {
  llm: [
    ["use_config_llm", "bool", "继承 config.yaml 的 llm 配置"],
    ["base_url", "text", "Base URL（不继承时填写，如 https://api.deepseek.com/v1）"],
    ["api_key", "text", "API Key"], ["model", "text", "模型名"],
    ["timeout", "number", "超时秒"],
  ],
  image: [
    ["enabled", "bool", "启用文生图模型（未启用生成占位图）"],
    ["base_url", "text", "Base URL（OpenAI 兼容 /images/generations）"],
    ["api_key", "text", "API Key"], ["model", "text", "模型名"],
    ["timeout", "number", "超时秒"],
  ],
  video: [
    ["enabled", "bool", "启用图生视频模型（未启用生成占位片段）"],
    ["base_url", "text", "Base URL"],
    ["api_key", "text", "API Key"], ["model", "text", "模型名"],
    ["submit_path", "text", "提交任务路径"], ["poll_path", "text", "轮询路径（{task_id} 占位）"],
    ["interval", "number", "轮询间隔秒"], ["timeout", "number", "总超时秒"],
  ],
};
$("#btn-models").onclick = async () => {
  let cfg;
  try { cfg = await api("/api/video/models"); }
  catch (e) { return toast(`读取模型配置失败：${e.message}`, "err"); }
  const sections = Object.entries(MODEL_FIELDS).map(([group, fields]) => `
    <h3 style="margin:14px 0 4px">${{ llm: "分镜 / 文案 LLM", image: "文生图（关键帧 / 角色设定）",
      video: "图生视频（镜头片段）" }[group]}</h3>
    ${fields.map(([key, widget, label]) => widget === "bool"
      ? `<div class="field"><label>${label}
           <input type="checkbox" data-mgroup="${group}" data-mkey="${key}"
             ${cfg[group]?.[key] ? "checked" : ""} style="width:auto"></label></div>`
      : `<div class="field"><label>${label}</label>
           <input data-mgroup="${group}" data-mkey="${key}" value="${esc(cfg[group]?.[key] ?? "")}"></div>`
    ).join("")}`).join("");
  openModal(`
    <h2>模型设置</h2>
    <div class="palette-note">未启用的类型自动降级为占位素材（流水线仍可跑通）。
      视频走通用两段式（提交 + 轮询），兼容 OpenAI 风格中转。</div>
    ${sections}
    <div class="btn-row"><button id="models-cancel">取消</button>
      <button id="models-save" class="primary">保存</button></div>`);
  $("#models-cancel").onclick = closeModal;
  $("#models-save").onclick = async () => {
    const next = JSON.parse(JSON.stringify(cfg));
    $$("#modal [data-mgroup]").forEach(el => {
      const gname = el.dataset.mgroup, key = el.dataset.mkey;
      next[gname] = next[gname] || {};
      next[gname][key] = el.type === "checkbox" ? el.checked
        : (el.type === "number" ? (el.value === "" ? null : Number(el.value)) : el.value);
    });
    try {
      await api("/api/video/models", { method: "PUT", body: next });
      closeModal();
      toast("模型配置已保存 ✅", "ok");
    } catch (e) { toast(`保存失败：${e.message}`, "err"); }
  };
};

/* ================= 素材库面板 ================= */
async function loadMedia() {
  try { state.mediaAssets = await api("/api/assets?limit=500"); }
  catch { state.mediaAssets = []; }
  renderMediaPanel();
}
function renderMediaPanel() {
  const kinds = [["", "全部"], ["image", "图片"], ["video", "视频"], ["audio", "音频"], ["file", "文件"]];
  $("#media-filters").innerHTML = kinds.map(([k, label]) =>
    `<button class="chip ${state.mediaKind === k ? "on" : ""}" data-mkind="${k}">${label}</button>`).join("");
  $$("#media-filters .chip").forEach(c => c.onclick = () => {
    state.mediaKind = c.dataset.mkind; renderMediaPanel();
  });
  const list = state.mediaAssets.filter(a => !state.mediaKind || a.kind === state.mediaKind);
  $("#media-grid").innerHTML = list.map(a => `
    <div class="media-card" data-aid="${esc(a.id)}">
      ${mediaThumbHtml(a)}
      <div class="media-meta"><span title="${esc(a.name)}">${esc(a.name)}</span>
        <span class="media-sub">${esc(a.kind)} · ${Math.max(1, Math.round((a.bytes || 0) / 1024))}KB</span></div>
      <button class="media-del" title="删除素材">×</button>
    </div>`).join("") || '<div class="empty-tip">暂无素材。运行视频流程或上传文件后出现在这里。</div>';
  $$("#media-grid .media-del").forEach(btn => btn.onclick = async e => {
    e.stopPropagation();
    const aid = btn.closest(".media-card").dataset.aid;
    try { await api(`/api/assets/${aid}`, { method: "DELETE" }); await loadMedia(); }
    catch (err) { toast(`删除失败：${err.message}`, "err"); }
  });
}
$("#btn-media").onclick = () => { $("#media-panel").classList.toggle("open"); loadMedia(); };
$("#media-close").onclick = () => $("#media-panel").classList.remove("open");
$("#media-upload-btn").onclick = () => $("#media-upload-input").click();
$("#media-upload-input").onchange = async e => {
  const files = [...e.target.files || []];
  if (!files.length) return;
  toast(`上传 ${files.length} 个文件…`);
  for (const f of files) {
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    const kind = f.type.startsWith("image/") ? "image" : f.type.startsWith("video/") ? "video"
      : f.type.startsWith("audio/") ? "audio" : "file";
    try {
      await api(`/api/assets/upload?ext=${encodeURIComponent(ext)}&kind=${kind}`
        + `&name=${encodeURIComponent(f.name)}`,
        { method: "POST", body: f, headers: {} });
    } catch (err) { toast(`上传失败 ${f.name}：${err.message}`, "err"); }
  }
  e.target.value = "";
  await loadMedia();
  toast("上传完成 ✅", "ok");
};

/* ================= 运行 ================= */
$("#btn-run").onclick = () => {
  if (!state.graph) return;
  const inputs = state.graph.nodes.find(n => n.type === "start")?.params?.inputs || [];
  const rows = inputs.map(i => {
    const key = typeof i === "string" ? i : i.key;
    const def = typeof i === "string" ? "" : (i.default ?? "");
    return `<div class="field"><label>${esc(key)}</label>
      <input data-inkey="${esc(key)}" value="${esc(def)}"></div>`;
  }).join("");
  openModal(`
    <h2>运行「${esc(state.graph.name)}」</h2>
    ${rows || '<div class="empty-tip">该流程无输入参数。</div>'}
    <div class="btn-row"><button id="run-cancel">取消</button>
      <button id="run-ok" class="primary">▶ 运行</button></div>`);
  $("#run-cancel").onclick = closeModal;
  $("#run-ok").onclick = async () => {
    const values = {};
    $$("#modal [data-inkey]").forEach(el => {
      const v = el.value.trim();
      values[el.dataset.inkey] = v === "true" ? true : v === "false" ? false : v;
    });
    closeModal();
    closeDrawers();
    $("#run-status").textContent = "运行中…"; $("#run-status").className = "";
    try {
      const run = await api(`/api/flows/${encodeURIComponent(state.graph.id)}/run`,
        { method: "POST", body: { inputs: values } });
      $("#run-status").textContent = run.status === "success" ? "✅ 成功" : "❌ 失败";
      $("#run-status").className = run.status === "success" ? "ok" : "err";
      await animateRun(run);
    } catch (e) {
      $("#run-status").textContent = "❌ 失败"; $("#run-status").className = "err";
      toast(e.message, "err");
    }
  };
};

/* ================= 意图触发 ================= */
async function chat() {
  const msg = $("#chat-input").value.trim();
  if (!msg) return;
  $("#chat-input").value = "";
  closeDrawers();
  toast("正在理解意图…");
  try {
    const r = await api("/api/agent/chat", { method: "POST", body: { message: msg } });
    if (r.matched && r.run) {
      if (r.flow_id !== state.graph?.id) {
        await loadFlowList(r.flow_id);
        await loadFlow(r.flow_id);
      }
      const st = r.run.status === "success" ? "✅" : "❌";
      $("#run-status").textContent = `${st} ${r.via === "llm" ? "LLM" : "关键词"}路由`;
      $("#run-status").className = r.run.status === "success" ? "ok" : "err";
      await animateRun(r.run);
      $("#rp-title").textContent = `意图触发 · ${r.run.flow_name}`;
      const ok = r.run.status === "success";
      toast(`${ok ? "✅" : "❌"} 已触发「${r.run.flow_name}」${r.reply ? "，回复见弹窗" : ""}`,
            ok ? "ok" : "err");
      if (r.reply) showReplyModal(r);
    } else {
      showReplyModal(r);
    }
  } catch (e) { toast(`触发失败：${e.message}`, "err"); }
}
function showReplyModal(r) {
  openModal(`
    <h2>${r.matched ? `已触发「${esc(r.run?.flow_name || r.flow_id)}」` : "未匹配到流程"}</h2>
    <div class="json-view" style="background:#f8fafc;color:#334155;max-height:340px">${esc(r.reply || "（无回复）")}</div>
    <div class="btn-row"><button id="rp-ok" class="primary">关闭</button></div>`);
  $("#rp-ok").onclick = closeModal;
}
$("#chat-send").onclick = chat;
$("#chat-input").addEventListener("keydown", e => { if (e.key === "Enter") chat(); });

/* ================= 节点面板（分组） ================= */
const PALETTE_GROUPS = [
  ["基础", ["start", "end", "brain", "llm", "agent", "condition", "intent", "template", "http"]],
  ["视频制作", ["storyboard", "character", "keyframe", "shot_video", "merge_video", "asset"]],
];
function renderPalette() {
  $("#palette-list").innerHTML = PALETTE_GROUPS.map(([title, types]) => {
    const items = types.filter(t => state.nodeTypes[t]).map(t => {
      const m = state.nodeTypes[t];
      return `<div class="palette-item" data-type="${t}">
        <span class="ico" style="color:${m.color}">${m.icon}</span>${m.label}</div>`;
    }).join("");
    return items ? `<h4 class="palette-group">${title}</h4>${items}` : "";
  }).join("");
  $$("#palette-list .palette-item").forEach(el => el.onclick = () => addNode(el.dataset.type));
}
function addNode(type) {
  if (!state.graph) return toast("先新建或选择流程", "err");
  const meta = state.nodeTypes[type] || {};
  let id = type, i = 2;
  while (nodeById(id)) id = `${type}${i++}`;
  const p = maxPos();
  const defaults = {};
  for (const f of meta.form || []) if (f.default !== undefined) defaults[f.key] = f.default;
  if (type === "llm") { defaults.system = defaults.system || "你是得力助手。"; defaults.prompt = defaults.prompt || "{{input.message}}"; defaults.required = false; }
  if (type === "template") defaults.template = "";
  if (type === "intent") defaults.intents = [{ name: "intent_1", description: "", samples: [] }];
  if (type === "character" && !defaults.views) defaults.views = ["正面", "左侧", "右侧", "背面"];
  state.graph.nodes.push({ id, type, label: meta.label || type, params: defaults,
                           pos: { x: p.x, y: Math.max(p.y, 40) } });
  ensureNodeEl(state.graph.nodes[state.graph.nodes.length - 1]);
  renderEdges(); drawMinimap(); markDirty();
  select({ kind: "node", id });
  const el = nodeEls.get(id);
  if (el) el.scrollIntoView?.({ block: "nearest" });
}

/* ================= 启动 ================= */
(async function init() {
  try {
    [state.nodeTypes, state.agents] = await Promise.all([
      api("/api/node-types"), api("/api/agents")]);
    renderPalette();
    const first = await loadFlowList();
    if (first) await loadFlow(first);
    else renderInspector();
    loadMedia();
  } catch (e) {
    toast(`初始化失败：${e.message}`, "err");
  }
})();
