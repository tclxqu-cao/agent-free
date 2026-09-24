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
const NODE_W = 224, NODE_H_DEFAULT = 58, CULL_MARGIN = 320;
/* 阻止 iOS Safari 的页面级捏合/双击缩放：双指手势只留给画布（缩放流程图） */
["gesturestart", "gesturechange", "gestureend"].forEach(t =>
  document.addEventListener(t, e => e.preventDefault()));
const state = {
  me: null, csrf: "", workspaceId: localStorage.getItem("flow-studio-workspace") || "",
  booted: false, govTab: "overview", govResource: null, policy: null,
  nodeTypes: {}, agents: [], flows: [],
  graph: null,            // 当前流程（纯 JSON）
  sel: null,              // {kind:'node'|'edge', id}
  view: { x: 40, y: 30, s: 1 },
  lastRun: null,          // 最近一次运行结果
  runSubmitting: false,
  runMonitor: { generation: 0, timer: null, controller: null, seq: 0, retries: 0,
    events: [], onComplete: null, active: false },
  dirty: false,
  mediaAssets: [],        // 素材库
  mediaKind: "",          // 素材过滤
  // ---- 资源库（智能体平台） ----
  resTab: "kb",           // kb / skills / mcp / memory / evals
  aiAgents: [],           // 可创建的智能体
  kbs: [],                // 知识库
  kbDocs: {},             // kb_id → 文档列表（懒加载）
  skillList: [],          // 技能
  mcpCfg: { servers: {} },// MCP 服务器配置
  mcpTools: {},           // server → 工具列表（懒加载）
  toolList: [],           // 内置工具
  memScope: "",           // 记忆面板当前作用域
  evalView: null,         // null=列表 | {mode:'run', run_id} | {mode:'compare'}
  viewMode: "agents",     // agents / flows-list / flows-editor
  chatTarget: null,       // 对话中的智能体 id
  chatSessions: {},       // agent_id → session_id
  chatLog: {},            // agent_id → [{role, text, steps?, error?}]
};
const $ = (q, el = document) => el.querySelector(q);
const $$ = (q, el = document) => [...el.querySelectorAll(q)];
const world = $("#world"), edgesSvg = $("#edges"), wrap = $("#canvas-wrap");
const nodeEls = new Map();      // 节点 id → DOM（视口内才建）
const edgeEls = new Map();      // 边 key → {g, path, text}
const heightCache = new Map();  // 节点 id → 高度（避免拖动时读 DOM）
const svgNS = "http://www.w3.org/2000/svg";

/* ================= 主题 ================= */
const THEME_STORAGE_KEY = "flow-studio-theme";
function applyTheme(theme) {
  const value = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = value;
  const button = $("#btn-theme");
  if (button) {
    const nextLabel = value === "light" ? "切换深色主题" : "切换浅色主题";
    button.textContent = value === "light" ? "☾" : "☀";
    button.title = nextLabel;
    button.setAttribute("aria-label", nextLabel);
    button.setAttribute("aria-pressed", String(value === "light"));
  }
}
applyTheme(document.documentElement.dataset.theme);
$("#btn-theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(next);
  try { localStorage.setItem(THEME_STORAGE_KEY, next); } catch { /* 主题仍在当前页面生效 */ }
};

/* ================= API ================= */
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const rawBody = opts.body instanceof File || opts.body instanceof Blob;
  if (!rawBody) headers["Content-Type"] = "application/json";
  if (state.workspaceId) headers["X-Workspace-ID"] = state.workspaceId;
  if (state.csrf && ["POST", "PUT", "PATCH", "DELETE"].includes(opts.method || "GET"))
    headers["X-CSRF-Token"] = state.csrf;
  const res = await fetch(path, {
    ...opts, headers,
    body: rawBody
      ? opts.body : (opts.body ? JSON.stringify(opts.body) : undefined),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(data.detail || data.error || res.statusText);
    error.code = data.code || `http_${res.status}`;
    error.status = res.status;
    error.policy = data.policy;
    if (res.status === 401 && !path.startsWith("/api/auth/")) renderLogin();
    throw error;
  }
  return data;
}

function can(capability) {
  const caps = state.me?.capabilities || [];
  return caps.includes("*") || caps.includes(capability);
}

function authForm(mode) {
  const setup = mode === "setup";
  $("#auth-title").textContent = setup ? "初始化管理员" : "登录 Flow Studio";
  $("#auth-subtitle").textContent = setup
    ? "创建首个 owner 账号，现有资产会进入 Default Workspace。"
    : "使用本机账号进入你的 Workspace。";
  $("#auth-form").innerHTML = `
    ${setup ? '<label>显示名称<input id="auth-display" autocomplete="name" required></label>' : ""}
    <label>用户名<input id="auth-user" autocomplete="username" pattern="[A-Za-z0-9_-]+" required></label>
    <label>密码<input id="auth-pass" type="password" autocomplete="${setup ? "new-password" : "current-password"}" required></label>
    <div id="auth-error" class="form-error"></div>
    <button class="primary" type="submit">${setup ? "创建 owner" : "登录"}</button>`;
  $("#auth-form").onsubmit = async e => {
    e.preventDefault();
    const submit = e.submitter; submit.disabled = true;
    try {
      const data = await api(setup ? "/api/setup" : "/api/auth/login", {
        method: "POST", body: {
          username: $("#auth-user").value.trim(), password: $("#auth-pass").value,
          ...(setup ? { display_name: $("#auth-display").value.trim() } : {}),
        }});
      state.csrf = data.csrf_token;
      state.workspaceId = data.workspace_id || data.workspaces?.[0]?.workspace_id || "default";
      localStorage.setItem("flow-studio-workspace", state.workspaceId);
      await enterApplication();
    } catch (error) {
      $("#auth-error").textContent = error.message;
    } finally { submit.disabled = false; }
  };
  document.body.classList.remove("authenticated");
  setTimeout(() => $("#auth-user")?.focus(), 0);
}
function renderSetup() { authForm("setup"); }
function renderLogin() {
  stopRunTracking(true);
  state.me = null; state.csrf = ""; authForm("login");
}

async function enterApplication() {
  const me = await api("/api/me");
  state.me = me; state.csrf = me.csrf_token;
  state.workspaceId = me.workspace.workspace_id;
  localStorage.setItem("flow-studio-workspace", state.workspaceId);
  $("#workspace-select").innerHTML = me.workspaces.map(w =>
    `<option value="${esc(w.workspace_id)}">${esc(w.name)}</option>`).join("");
  $("#workspace-select").value = state.workspaceId;
  $("#user-role").textContent = me.workspace.role;
  $("#user-name").textContent = me.user.display_name;
  document.body.classList.add("authenticated");
  applyPermissions();
  await loadApplicationData();
}

function applyPermissions() {
  const writable = can("resource.write");
  ["#agents-new", "#flows-new", "#btn-new", "#btn-del", "#btn-save", "#btn-models", "#media-upload-btn"]
    .forEach(selector => { const el = $(selector); if (el) el.hidden = !writable; });
  $("#btn-gov").hidden = !(can("release.approve") || can("audit.read") || writable);
}

async function loadApplicationData() {
  stopRunTracking(true);
  state.graph = null; state.lastRun = null; state.govResource = null; state.dirty = false;
  state.kbDocs = {}; state.mcpTools = {}; state.chatTarget = null;
  ["#gov-panel", "#res-panel", "#media-panel", "#chat-panel"]
    .forEach(selector => $(selector)?.classList.remove("open"));
  [state.nodeTypes, state.agents, state.policy] = await Promise.all([
    api("/api/node-types"), api("/api/agents"), api("/api/governance/policy")]);
  renderPalette();
  await loadFlowList();
  state.graph = null; renderInspector(); renderFlowGovernance();
  await Promise.allSettled([loadMedia(), loadResources()]);
  switchView("agents");
  state.booted = true;
  await restoreRunTracking();
}

$("#workspace-select").onchange = async e => {
  stopRunTracking(true);
  state.workspaceId = e.target.value;
  localStorage.setItem("flow-studio-workspace", state.workspaceId);
  try { await enterApplication(); }
  catch (error) { showApiError(error, "切换 Workspace 失败"); }
};
$("#btn-logout").onclick = async () => {
  stopRunTracking(true);
  try { await api("/api/auth/logout", { method: "POST" }); }
  catch { /* 本地会话失效时仍回到登录页 */ }
  localStorage.removeItem("flow-studio-workspace");
  state.workspaceId = ""; state.booted = false;
  renderLogin();
};

/* ================= 提示 / 弹窗 ================= */
let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg; t.className = kind; t.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.style.display = "none"), kind === "err" ? 4200 : 2400);
}
function openModal(html) {
  delete $("#modal").dataset.runView; delete $("#modal").dataset.runNode;
  $("#modal").classList.remove("agent-editor");
  $("#modal").innerHTML = html; $("#modal-mask").classList.add("open");
}
function closeModal() {
  $("#modal-mask").classList.remove("open");
  delete $("#modal").dataset.runView;
  delete $("#modal").dataset.runNode;
}
$("#modal-mask").addEventListener("mousedown", e => { if (e.target.id === "modal-mask") closeModal(); });
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  cancelCanvasGesture();
  if ($("#modal-mask").classList.contains("open")) closeModal();
  else if (state.viewMode === "flows-editor" && state.sel) select(null);
});

function showApiError(error, fallback = "操作失败") {
  const violations = error?.policy?.violations || [];
  const detail = violations.map(v => `${v.message}${v.path ? `（${v.path}）` : ""}`).join("；");
  toast(`${fallback}：${detail || error?.message || "未知错误"}`, "err");
}

function modalValue({ title, label, value = "", required = false, multiline = false,
                      confirmText = "确定", placeholder = "" }) {
  return new Promise(resolve => {
    openModal(`<h2>${esc(title)}</h2>
      <div class="field"><label>${esc(label)}</label>
        ${multiline
          ? `<textarea id="prompt-value" rows="4" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`
          : `<input id="prompt-value" value="${esc(value)}" placeholder="${esc(placeholder)}">`}
      </div>
      <div class="btn-row"><button id="prompt-cancel">取消</button>
        <button id="prompt-ok" class="primary">${esc(confirmText)}</button></div>`);
    $("#prompt-value").focus();
    $("#prompt-cancel").onclick = () => { closeModal(); resolve(null); };
    $("#prompt-ok").onclick = () => {
      const result = $("#prompt-value").value.trim();
      if (required && !result) return toast(`${label}不能为空`, "err");
      closeModal(); resolve(result);
    };
  });
}

function promptInputs(flow = state.graph) {
  return new Promise(resolve => {
    const inputs = flow?.nodes?.find(n => n.type === "start")?.params?.inputs || [];
    const rows = inputs.map(item => {
      const key = typeof item === "string" ? item : item.key;
      const value = typeof item === "string" ? "" : (item.default ?? "");
      return `<div class="field"><label>${esc(key)}</label>
        <input data-inkey="${esc(key)}" value="${esc(value)}"></div>`;
    }).join("");
    openModal(`<h2>运行输入</h2>
      ${rows || '<div class="empty-tip">该流程无输入参数。</div>'}
      <div class="btn-row"><button id="inputs-cancel">取消</button>
        <button id="inputs-ok" class="primary">继续</button></div>`);
    $("#inputs-cancel").onclick = () => { closeModal(); resolve(null); };
    $("#inputs-ok").onclick = () => {
      const values = {};
      $$("#modal [data-inkey]").forEach(el => {
        const raw = el.value.trim();
        values[el.dataset.inkey] = raw === "true" ? true : raw === "false" ? false : raw;
      });
      closeModal(); resolve(values);
    };
  });
}

async function governanceAction(resourceType, resourceId, version, action, body = {}) {
  return api(`/api/governance/resources/${encodeURIComponent(resourceType)}/` +
    `${encodeURIComponent(resourceId)}/versions/${version}/${action}`,
    { method: "POST", body });
}

async function refreshGovernedResource(resourceType, resourceId) {
  if (resourceType === "agent") await loadResources();
  else {
    await loadFlowList(resourceId);
    if (state.graph?.id === resourceId) await loadFlow(resourceId);
  }
}

async function submitResource(resourceType, resourceId, version) {
  if (!version) return toast("没有可提交的草稿版本", "err");
  const reason = await modalValue({ title: "提交审批", label: "变更说明（可选）",
    multiline: true, confirmText: "提交" });
  if (reason === null) return;
  try {
    await governanceAction(resourceType, resourceId, version, "submit", { reason });
    await refreshGovernedResource(resourceType, resourceId);
    if ($("#gov-panel").classList.contains("open")) await renderGovernancePanel();
    toast("已提交审批", "ok");
  } catch (error) { showApiError(error, "提交失败"); }
}

function openVersions(resourceType, resourceId) {
  state.govResource = { resourceType, resourceId };
  openGovernance("versions");
}

const GOV_TABS = [
  ["overview", "概览", () => true],
  ["versions", "版本", () => Boolean(state.govResource)],
  ["approvals", "审批", () => can("release.approve")],
  ["members", "成员", () => can("member.manage")],
  ["policy", "策略", () => can("resource.read")],
  ["audit", "审计", () => can("audit.read")],
];

function openGovernance(tab = "overview") {
  state.govTab = tab;
  ["#palette", "#inspector", "#media-panel", "#res-panel"]
    .forEach(selector => $(selector)?.classList.remove("open"));
  $("#gov-panel").classList.add("open");
  renderGovernancePanel();
}
$("#btn-gov").onclick = () => openGovernance("overview");
$("#gov-close").onclick = () => $("#gov-panel").classList.remove("open");

async function renderGovernancePanel() {
  const tabs = GOV_TABS.filter(([, , visible]) => visible());
  if (!tabs.some(([key]) => key === state.govTab)) state.govTab = "overview";
  $("#gov-tabs").innerHTML = tabs.map(([key, label]) =>
    `<button class="chip ${state.govTab === key ? "on" : ""}" data-gtab="${key}">${label}</button>`).join("");
  $$("#gov-tabs [data-gtab]").forEach(button => button.onclick = () => {
    state.govTab = button.dataset.gtab; renderGovernancePanel();
  });
  const body = $("#gov-body");
  body.innerHTML = '<div class="res-empty">加载中…</div>';
  try {
    await ({ overview: renderGovOverview, versions: renderGovVersions,
      approvals: renderGovApprovals, members: renderGovMembers,
      policy: renderGovPolicy, audit: renderGovAudit })[state.govTab](body);
  } catch (error) {
    body.innerHTML = `<div class="res-empty">${esc(error.message)}</div>`;
  }
}

async function renderGovOverview(body) {
  let approvals = [];
  if (can("release.approve")) approvals = await api("/api/governance/approvals");
  body.innerHTML = `
    <div class="gov-summary">
      <div><b>${esc(state.me.workspace.name)}</b><span>当前 Workspace</span></div>
      <div><b>${state.aiAgents.length}</b><span>智能体</span></div>
      <div><b>${state.flows.length}</b><span>流程</span></div>
      <div><b>${approvals.length}</b><span>待处理版本</span></div>
    </div>
    <div class="gov-section"><h4>存储边界</h4>
      <div class="res-card"><div class="res-kv"><b>治理元数据</b><span>SQLite</span></div>
        <div class="res-kv"><b>Workspace 资产</b><span>独立文件目录</span></div>
        <div class="res-kv"><b>正式运行</b><span>仅使用已发布版本</span></div></div>
    </div>
    ${can("workspace.manage") ? `<div class="gov-section"><h4>Workspace</h4>
      <button id="workspace-new" class="primary">新建 Workspace</button></div>` : ""}`;
  if ($("#workspace-new")) $("#workspace-new").onclick = createWorkspaceModal;
}

function createWorkspaceModal() {
  openModal(`<h2>新建 Workspace</h2>
    <div class="field"><label>名称</label><input id="ws-name"></div>
    <div class="field"><label>ID（字母数字-_）</label><input id="ws-id"></div>
    <div class="btn-row"><button id="ws-cancel">取消</button>
      <button id="ws-ok" class="primary">创建</button></div>`);
  $("#ws-cancel").onclick = closeModal;
  $("#ws-ok").onclick = async () => {
    const name = $("#ws-name").value.trim();
    const id = $("#ws-id").value.trim() || name.toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    if (!name || !id) return toast("请填写名称和有效 ID", "err");
    try {
      await api("/api/workspaces", { method: "POST", body: { id, name } });
      closeModal(); state.workspaceId = id;
      localStorage.setItem("flow-studio-workspace", id);
      await enterApplication(); toast("Workspace 已创建", "ok");
    } catch (error) { showApiError(error, "创建失败"); }
  };
}

function versionActionsHtml(version) {
  const actions = [];
  if (version.status === "draft" && can("resource.write")) actions.push(["submit", "提交"]);
  if (version.status === "pending" && can("release.approve")) {
    actions.push(["approve", "批准"], ["reject", "拒绝"]);
  }
  const publishable = version.status === "approved" ||
    (!state.policy?.require_approval && ["draft", "pending"].includes(version.status));
  if (publishable && can("release.publish"))
    actions.push(["publish", "发布"]);
  if (version.published_at && version.published_version !== version.version_no && can("release.rollback"))
    actions.push(["rollback", "回滚"]);
  if (version.action !== "delete" && can("runtime.preview")) actions.push(["preview", "预览"]);
  return actions.map(([action, label]) =>
    `<button data-vact="${action}"${action === "reject" ? ' class="danger"' : ""}>${label}</button>`).join("");
}

function versionRow(version) {
  return `<div class="gov-row" data-version="${version.version_no}"
    data-resource-type="${esc(version.resource_type)}" data-resource-id="${esc(version.resource_id)}">
    <div class="gov-main"><div class="gov-title">
      <span class="status-badge" data-status="${esc(version.status)}">v${version.version_no} ${esc(version.status)}</span>
      <span>${esc(version.resource_type)} · ${esc(version.resource_id)}</span>
      ${version.published_version === version.version_no ? '<span class="chip on">当前发布</span>' : ""}
    </div><div class="gov-meta">${esc(version.action)} · ${esc(version.creator_name || version.created_by || "")} · ${esc(formatTime(version.created_at))}${version.reason ? ` · ${esc(version.reason)}` : ""}</div></div>
    <div class="gov-actions">${versionActionsHtml(version)}</div></div>`;
}

async function renderGovVersions(body) {
  const { resourceType, resourceId } = state.govResource;
  const versions = await api(`/api/governance/resources/${encodeURIComponent(resourceType)}/` +
    `${encodeURIComponent(resourceId)}/versions`);
  body.innerHTML = `<div class="res-actions"><button id="versions-back">← 概览</button>
    <b>${esc(resourceType)} · ${esc(resourceId)}</b></div>
    ${versions.map(versionRow).join("") || '<div class="res-empty">暂无版本。</div>'}`;
  $("#versions-back").onclick = () => { state.govTab = "overview"; renderGovernancePanel(); };
  bindVersionActions(body, versions);
}

async function renderGovApprovals(body) {
  const versions = await api("/api/governance/approvals");
  body.innerHTML = versions.map(versionRow).join("") || '<div class="res-empty">没有待处理版本。</div>';
  bindVersionActions(body, versions);
}

function bindVersionActions(body, versions) {
  body.querySelectorAll("[data-version]").forEach(row => {
    const version = versions.find(v => v.version_no === Number(row.dataset.version)
      && v.resource_type === row.dataset.resourceType
      && v.resource_id === row.dataset.resourceId);
    if (!version) return;
    row.querySelectorAll("[data-vact]").forEach(button => button.onclick = async () => {
      const action = button.dataset.vact;
      if (action === "preview") return previewVersion(version);
      const required = action === "reject" || action === "rollback";
      const reason = await modalValue({ title: `${button.textContent} v${version.version_no}`,
        label: required ? "原因" : "说明（可选）", required, multiline: true,
        confirmText: button.textContent });
      if (reason === null) return;
      try {
        await governanceAction(version.resource_type, version.resource_id,
          version.version_no, action, { reason });
        await refreshGovernedResource(version.resource_type, version.resource_id);
        await renderGovernancePanel(); toast(`${button.textContent}完成`, "ok");
      } catch (error) { showApiError(error, `${button.textContent}失败`); }
    });
  });
}

async function previewVersion(version) {
  const scope = runScope();
  let body;
  if (version.resource_type === "flow") {
    const inputs = await promptInputs(version.snapshot); if (inputs === null) return;
    body = { inputs, background: true };
  } else {
    const message = await modalValue({ title: `预览智能体 v${version.version_no}`,
      label: "消息", required: true, multiline: true });
    if (message === null) return;
    body = { message };
  }
  if (scope !== runScope()) return;
  if (version.resource_type === "flow") {
    await submitRun(() => governanceAction("flow", version.resource_id,
      version.version_no, "preview", body));
    return;
  }
  try {
    const result = await governanceAction(version.resource_type, version.resource_id,
      version.version_no, "preview", body);
    openModal(`<h2>预览结果</h2><div class="res-snippet">${esc(result.text || result.error || "（空）")}</div>
      <div class="btn-row"><button id="preview-close" class="primary">关闭</button></div>`),
      $("#preview-close").onclick = closeModal;
    toast("预览完成", "ok");
  } catch (error) { showApiError(error, "预览失败"); }
}

async function renderGovMembers(body) {
  const workspaceId = state.workspaceId;
  const members = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/members`);
  body.innerHTML = `<div class="res-actions"><button id="member-add" class="primary">添加成员</button></div>
    ${members.map(member => `<div class="gov-row" data-member="${esc(member.user_id)}">
      <div class="gov-main"><div class="gov-title">${esc(member.display_name)} <span class="role-badge">${esc(member.role)}</span></div>
        <div class="gov-meta">${esc(member.username)}</div></div>
      <div class="gov-actions">${member.role === "owner" ? "" : `
        <select data-role>${["admin", "editor", "viewer"].map(role =>
          `<option value="${role}" ${member.role === role ? "selected" : ""}>${role}</option>`).join("")}</select>
        <button data-member-save>保存</button><button data-member-remove class="danger">移除</button>`}</div>
    </div>`).join("")}`;
  $("#member-add").onclick = addMemberModal;
  body.querySelectorAll("[data-member]").forEach(row => {
    const userId = row.dataset.member;
    row.querySelector("[data-member-save]")?.addEventListener("click", async () => {
      try {
        await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
          { method: "PUT", body: { role: row.querySelector("[data-role]").value } });
        await renderGovMembers(body); toast("角色已更新", "ok");
      } catch (error) { showApiError(error, "更新失败"); }
    });
    row.querySelector("[data-member-remove]")?.addEventListener("click", async () => {
      if (!confirm("确认移除该成员？")) return;
      try {
        await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
          { method: "DELETE" });
        await renderGovMembers(body); toast("成员已移除", "ok");
      } catch (error) { showApiError(error, "移除失败"); }
    });
  });
}

function addMemberModal() {
  openModal(`<h2>添加成员</h2>
    <div class="field"><label>用户名</label><input id="member-user"></div>
    <div class="field"><label>显示名称</label><input id="member-name"></div>
    <div class="field"><label>初始密码（仅新用户）</label><input id="member-pass" type="password"></div>
    <div class="field"><label>角色</label><select id="member-role">
      <option value="viewer">viewer</option><option value="editor">editor</option>
      ${state.me.workspace.role === "owner" ? '<option value="admin">admin</option>' : ""}</select></div>
    <div class="btn-row"><button id="member-cancel">取消</button><button id="member-ok" class="primary">添加</button></div>`);
  $("#member-cancel").onclick = closeModal;
  $("#member-ok").onclick = async () => {
    try {
      await api(`/api/workspaces/${encodeURIComponent(state.workspaceId)}/members`, {
        method: "POST", body: { username: $("#member-user").value.trim(),
          display_name: $("#member-name").value.trim(), password: $("#member-pass").value,
          role: $("#member-role").value }});
      closeModal(); await renderGovernancePanel(); toast("成员已添加", "ok");
    } catch (error) { showApiError(error, "添加失败"); }
  };
}

const policyList = value => (value || []).join("\n");
const parsePolicyList = value => value.split(/[\n,，]/).map(item => item.trim()).filter(Boolean);
async function renderGovPolicy(body) {
  const policy = await api("/api/governance/policy");
  const editable = can("policy.manage");
  body.innerHTML = `<div class="policy-grid">
    <div class="field"><label>允许的模型（每行一个，空=不限制）</label><textarea id="pol-models" rows="3" ${editable ? "" : "disabled"}>${esc(policyList(policy.allowed_models))}</textarea></div>
    <div class="field"><label>禁用工具</label><textarea id="pol-tools" rows="3" ${editable ? "" : "disabled"}>${esc(policyList(policy.denied_tools))}</textarea></div>
    <div class="field"><label>禁用 MCP</label><textarea id="pol-mcp" rows="3" ${editable ? "" : "disabled"}>${esc(policyList(policy.denied_mcp_servers))}</textarea></div>
    <div class="field"><label>允许的流程节点（空=不限制）</label><textarea id="pol-nodes" rows="3" ${editable ? "" : "disabled"}>${esc(policyList(policy.allowed_flow_node_types))}</textarea></div>
    <div class="field wide"><label>允许的 HTTP 主机</label><textarea id="pol-hosts" rows="2" ${editable ? "" : "disabled"}>${esc(policyList(policy.allowed_http_hosts))}</textarea></div>
    <div class="field"><label>Agent 最大步数</label><input id="pol-steps" type="number" min="1" max="30" value="${policy.max_agent_steps}" ${editable ? "" : "disabled"}></div>
    <div class="field"><label>必需评测集 ID</label><input id="pol-suite" value="${esc(policy.required_eval_suite_id)}" ${editable ? "" : "disabled"}></div>
    <div class="field"><label>最低通过率（0-1）</label><input id="pol-rate" type="number" min="0" max="1" step="0.01" value="${policy.min_eval_pass_rate}" ${editable ? "" : "disabled"}></div>
    <div class="field"><label><input id="pol-approval" type="checkbox" style="width:auto" ${policy.require_approval ? "checked" : ""} ${editable ? "" : "disabled"}> 发布前必须审批</label></div>
  </div>${editable ? '<div class="btn-row"><button id="policy-save" class="primary">保存策略</button></div>' : ""}`;
  if ($("#policy-save")) $("#policy-save").onclick = async () => {
    const next = { allowed_models: parsePolicyList($("#pol-models").value),
      denied_tools: parsePolicyList($("#pol-tools").value),
      denied_mcp_servers: parsePolicyList($("#pol-mcp").value),
      allowed_flow_node_types: parsePolicyList($("#pol-nodes").value),
      allowed_http_hosts: parsePolicyList($("#pol-hosts").value),
      max_agent_steps: Number($("#pol-steps").value),
      require_approval: $("#pol-approval").checked,
      required_eval_suite_id: $("#pol-suite").value.trim(),
      min_eval_pass_rate: Number($("#pol-rate").value) };
    try { state.policy = await api("/api/governance/policy", { method: "PUT", body: next });
      toast("策略已保存", "ok"); }
    catch (error) { showApiError(error, "保存失败"); }
  };
}

async function renderGovAudit(body) {
  const events = await api("/api/governance/audit?limit=200");
  body.innerHTML = events.map(event => `<div class="gov-row">
    <div class="gov-main"><div class="gov-title">${esc(event.event_type)}
      <span class="status-badge" data-status="${event.outcome === "success" ? "published" : "rejected"}">${esc(event.outcome)}</span></div>
      <div class="gov-meta">${esc(formatTime(event.created_at))} · ${esc(event.username || "system")}` +
        `${event.resource_id ? ` · ${esc(event.resource_type)}:${esc(event.resource_id)}${event.version_no ? ` v${event.version_no}` : ""}` : ""}</div>
      ${Object.keys(event.details || {}).length ? `<div class="res-snippet">${esc(fmtJson(event.details))}</div>` : ""}</div>
    </div>`).join("") || '<div class="res-empty">暂无审计事件。</div>';
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

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
  const c = meta.color || "#888";
  el.innerHTML = `
    <div class="type-dot" style="background:${c}"></div>
    <div class="node-head"><span class="ico-chip" style="background:${c}14;color:${c}">${meta.icon || "•"}</span>
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
    case "condition": return `取值: ${p.source || "input.message"}`;
    case "storyboard": return `${p.shot_count || 4} 镜 · ${p.aspect_ratio || "16:9"}`;
    case "character": return `${p.name || "角色"} · ${(p.views || []).length} 方位`;
    case "keyframe": return `来源 ${(p.shots_source || "").slice(0, 24)}`;
    case "shot_video": return `每镜 ${p.duration ?? 3}s · ${p.aspect_ratio || "继承分镜"}`;
    case "merge_video": return `来源 ${(p.clips_source || "").slice(0, 26)}`;
    case "voiceover": return `${p.voice || "继承音色"} · ${p.speed ?? 1}x`;
    case "video_compose": return `${p.burn_subtitles === false ? "外挂字幕" : "烧录字幕"} · ${(p.clips_source || "").slice(0, 18)}`;
    case "asset": return p.asset_id ? `素材 ${p.asset_id}` : "未选择素材";
    case "intent": {
      const names = (p.intents || []).map(i => i.name).filter(Boolean);
      return names.length ? `意图: ${names.join(" / ")}（else 兜底）` : "未配置意图清单";
    }
    case "start": {
      const keys = (p.inputs || []).map(i => i.key || i).filter(Boolean);
      return keys.length ? `输入: ${keys.join(", ")}` : "无输入参数";
    }
    case "ai_agent": {
      const a = state.aiAgents.find(x => x.id === p.ai_agent_id);
      if (!a) return p.ai_agent_id || "未选择智能体";
      if (a.runtime === "customer-agent")
        return `${a.name} · Customer Agent · ${p.skill_id || "未选 Skill"}`;
      return a.flow_id ? `${a.name} · 流程:${a.flow_id}` : `${a.name} · 对话式`;
    }
    case "kb": {
      const ids = p.kb_ids || [];
      return `查 ${ids.length ? ids.join(", ") : "全部知识库"} · top ${p.top_k || 5}`;
    }
    case "memory": return `${p.op || "get"} · ${p.scope || "session"}${p.key ? ` · ${p.key}` : ""}`;
    case "skill": {
      const s = state.skillList.find(x => x.id === p.skill_id);
      return s ? s.name : (p.skill_id || "未选择技能");
    }
    case "mcp": return `${p.server || "?"} / ${p.tool || "?"}`;
    case "tool": return p.tool || "未选择工具";
    default: return "";
  }
}

/* 边：持久元素 + diff 更新（避免 innerHTML 全量重建） */
function edgeLabel(e, from) {
  const isElse = (e.branch || "").trim().toLowerCase() === "else";
  if (from.type === "condition") return conditionOutputMode(from) ? "输出" : conditionRuleLabel(e);
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
  $("#res-panel").classList.remove("open");
}
function captureCanvasPointer(e) {
  // 捕获到稳定的画布容器，节点移出视口或鼠标在容器外松开仍能结束拖拽。
  try { wrap.setPointerCapture(e.pointerId); } catch { /* 指针可能已被浏览器取消 */ }
}
function cancelCanvasGesture() {
  const ended = drag;
  const pointers = new Set(activePtrs.keys());
  if (ended) pointers.add(ended.pointerId);
  // 先清状态，再释放捕获；lostpointercapture 可能在释放时回调。
  drag = null; pinch = null; activePtrs.clear();
  wrap.classList.remove("panning");
  $("#temp-edge")?.remove();
  for (const id of pointers) {
    if (wrap.hasPointerCapture(id)) wrap.releasePointerCapture(id);
  }
  if (ended?.mode === "node" && nodeById(ended.id)) updateEdgesFor(ended.id);
  if (ended?.mode === "pan") updateVisibility();
  if (ended) drawMinimap();
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
  if (e.button !== 0) return;
  activePtrs.set(e.pointerId, ptrPos(e));
  if (activePtrs.size === 2) {          // 第二根手指落下 → 切换为双指缩放
    captureCanvasPointer(e);
    drag = null; wrap.classList.remove("panning");
    $("#temp-edge")?.remove();
    pinch = startPinch();
    return;
  }
  if (activePtrs.size > 2 || pinch) return;
  if (e.target.closest(".node") || e.target.closest("#run-panel")
      || e.target.closest("#edges") || e.target.closest(".canvas-fab")
      || e.target.closest("#minimap")) return;
  drag = { mode: "pan", pointerId: e.pointerId, sx: e.clientX, sy: e.clientY,
           ox: state.view.x, oy: state.view.y };
  captureCanvasPointer(e);
  if (state.sel) select(null);
  wrap.classList.add("panning");
  closeDrawers();                        // 手机：点画布空白收起抽屉
});
wrap.addEventListener("pointerdown", e => {
  if (e.button !== 0 || pinch || activePtrs.size > 1) return;
  const port = e.target.closest(".port.out");
  if (!port) return;
  e.stopPropagation();
  const nodeEl = port.closest(".node");
  drag = { mode: "connect", pointerId: e.pointerId, from: nodeEl.dataset.id };
  captureCanvasPointer(e);
  const temp = document.createElementNS(svgNS, "path");
  temp.setAttribute("class", "edge temp"); temp.id = "temp-edge";
  edgesSvg.appendChild(temp);
});
world.addEventListener("pointerdown", e => {
  if (e.button !== 0 || pinch || activePtrs.size > 1) return;
  const nodeEl = e.target.closest(".node");
  if (!nodeEl) return;
  if (e.target.closest(".port.out") || e.target.closest(".del")) return;
  const n = nodeById(nodeEl.dataset.id);
  drag = { mode: "node", pointerId: e.pointerId, id: n.id, sx: e.clientX, sy: e.clientY,
           ox: n.pos?.x ?? 0, oy: n.pos?.y ?? 0, moved: false };
  captureCanvasPointer(e);
  e.preventDefault();
});
document.addEventListener("pointermove", e => {
  if (!activePtrs.has(e.pointerId) && drag?.pointerId !== e.pointerId) return;
  // 窗口外释放后重入时可能没有 pointerup；不要让无按键的移动继续拖节点。
  if (e.pointerType === "mouse" && !(e.buttons & 1)) { cancelCanvasGesture(); return; }
  if (activePtrs.has(e.pointerId)) activePtrs.set(e.pointerId, ptrPos(e));
  if (pinch) { if (activePtrs.size >= 2) movePinch(); return; }
  if (!drag || drag.pointerId !== e.pointerId) return;
  const v = state.view;
  if (drag.mode === "pan") {
    state.view = { x: drag.ox + e.clientX - drag.sx, y: drag.oy + e.clientY - drag.sy, s: v.s };
    applyView(); scheduleFrame();
  } else if (drag.mode === "node") {
    const dx = (e.clientX - drag.sx) / v.s, dy = (e.clientY - drag.sy) / v.s;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    const n = nodeById(drag.id);
    if (!n) { cancelCanvasGesture(); return; }
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
  if (!activePtrs.has(e.pointerId) && drag?.pointerId !== e.pointerId) return;
  if (pinch) { cancelCanvasGesture(); return; }
  activePtrs.delete(e.pointerId);
  if (!drag || drag.pointerId !== e.pointerId) return;
  const ended = drag;
  cancelCanvasGesture();
  if (ended.mode === "node" && !ended.moved) select({ kind: "node", id: ended.id });
  if (ended.mode === "connect") {
    const nodeEl = document.elementFromPoint(e.clientX, e.clientY)?.closest(".node");
    const to = nodeEl?.dataset.id;
    if (to && to !== ended.from) connect(ended.from, to);
  }
}
document.addEventListener("pointerup", pointerEnd);
document.addEventListener("pointercancel", e => {
  if (activePtrs.has(e.pointerId) || drag?.pointerId === e.pointerId) cancelCanvasGesture();
});
wrap.addEventListener("lostpointercapture", e => {
  if (activePtrs.has(e.pointerId) || drag?.pointerId === e.pointerId) cancelCanvasGesture();
});
window.addEventListener("blur", cancelCanvasGesture);
document.addEventListener("visibilitychange", () => { if (document.hidden) cancelCanvasGesture(); });
function zoomAt(cx, cy, factor) {
  const v = state.view;
  const s2 = Math.min(2.5, Math.max(0.2, v.s * factor));
  state.view = { s: s2, x: cx - (cx - v.x) * (s2 / v.s), y: cy - (cy - v.y) * (s2 / v.s) };
  applyView(); updateVisibility(); drawMinimap();
}
wrap.addEventListener("wheel", e => {
  if (e.target.closest("#run-panel")) return;
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
  if (vw < 40 || vh < 40) return;   // 视图隐藏时尺寸为 0，等切换时再适配
  // 缩放下限 0.7：流程再长，节点也不会缩到看不清
  const s = Math.min(1.1, Math.max(0.7, Math.min(
    (vw - pad * 2) / Math.max(1, maxX - minX),
    (vh - pad * 2) / Math.max(1, maxY - minY))));
  state.view = { s, x: (vw - (maxX - minX) * s) / 2 - minX * s,
                 y: (vh - (maxY - minY) * s) / 2 - minY * s };
  applyView(); updateVisibility(); drawMinimap();
}

/* ================= 小地图 ================= */
const TYPE_HUES = { storyboard: "#db2777", character: "#d97706", keyframe: "#0891b2",
  shot_video: "#7c3aed", merge_video: "#059669", voiceover: "#2563eb",
  video_compose: "#0f766e", asset: "#64748b" };
function drawMinimap() {
  const cv = $("#minimap"), ctx = cv.getContext("2d");
  const ns = state.graph?.nodes || [];
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = "#16181d";
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
  ctx.strokeStyle = "#8b98f8"; ctx.lineWidth = 1.5;
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
  if (from.type === "condition" && conditionOutputMode(from)) {
    if (edgesOf().some(edge => edge.from === fromId)) return toast("输出模式只能连接一个下游节点", "err");
    state.graph.edges.push({ from: fromId, to: toId }); renderEdges(); markDirty();
  }
  else if (from.type === "condition") askConditionRule(null, rule => {
    if (rule === null) return;
    state.graph.edges.push({ from: fromId, to: toId, ...rule });
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
  openModal(`
    <h2>意图分支</h2>
    <div class="field"><label>命中哪个意图时走这条连线</label>
      <select id="branch-select">${(options || []).map(o =>
        `<option value="${esc(o)}">${esc(o)}</option>`).join("")}
        <option value="else">else —— 兜底（其它/未命中）</option></select></div>
    <div class="btn-row">
      <button id="branch-cancel">取消</button>
      <button id="branch-ok" class="primary">确定</button></div>`);
  const finish = v => { closeModal(); cb(v); };
  $("#branch-ok").onclick = () => finish($("#branch-select").value);
  $("#branch-cancel").onclick = () => finish(null);
  $("#branch-select").focus();
}

const CONDITION_OPERATORS = [
  ["equals", "等于 =="], ["not_equals", "不等于 !="],
  ["contains", "包含"], ["not_contains", "不包含"],
  ["greater_than", "大于 >"], ["greater_or_equal", "大于等于 >="],
  ["less_than", "小于 <"], ["less_or_equal", "小于等于 <="],
];
const CONDITION_VALUE_TYPES = [
  ["string", "文本"], ["number", "数字"], ["boolean", "布尔值"], ["null", "空值 null"],
];
function conditionRuleLabel(edge) {
  if ((edge.branch || "").trim().toLowerCase() === "else") return "else";
  if (!edge.operator) return edge.branch || "未配置";
  const symbols = { equals: "==", not_equals: "!=", contains: "包含", not_contains: "不包含",
    greater_than: ">", greater_or_equal: ">=", less_than: "<", less_or_equal: "<=" };
  let value = edge.value;
  if ((edge.value_type || "string") === "string") value = JSON.stringify(String(value ?? ""));
  else if (edge.value_type === "null") value = "null";
  return `${symbols[edge.operator] || edge.operator} ${value ?? ""}`.trim();
}
function conditionOutputMode(node) {
  return Array.isArray(node?.params?.outputs);
}
function applyConditionRule(edge, rule) {
  for (const key of ["branch", "operator", "value", "value_type"]) delete edge[key];
  Object.assign(edge, rule);
}
function askConditionRule(edge, cb) {
  const raw = edge?.operator ? "" : ((edge?.branch || "").toLowerCase() === "else" ? "" : edge?.branch || "");
  openModal(`
    <h2>条件分支</h2>
    <div class="field"><label>比较运算</label><select id="condition-operator">
      ${CONDITION_OPERATORS.map(([value, label]) => `<option value="${value}" ${edge?.operator === value ? "selected" : ""}>${label}</option>`).join("")}
    </select></div>
    <div class="field"><label>比较值类型</label><select id="condition-value-type">
      ${CONDITION_VALUE_TYPES.map(([value, label]) => `<option value="${value}" ${(edge?.value_type || "string") === value ? "selected" : ""}>${label}</option>`).join("")}
    </select></div>
    <div class="field" id="condition-value-row"><label>比较值</label>
      <input id="condition-value" value="${esc(edge?.value ?? "")}" placeholder="例如 /works"></div>
    <details ${raw ? "open" : ""}><summary>高级表达式（兼容旧流程）</summary>
      <div class="field"><label>完整表达式</label>
        <input id="condition-expression" value="${esc(raw)}" placeholder="例如 match.total > 0">
        <div class="hint">填写后优先保存高级表达式；只允许受限变量、比较、布尔和算术语法。</div></div>
    </details>
    <div class="btn-row">
      <button id="condition-else">else 兜底分支</button>
      <button id="condition-cancel">取消</button>
      <button id="condition-ok" class="primary">确定</button></div>`);
  const type = $("#condition-value-type"), valueRow = $("#condition-value-row");
  const syncType = () => { valueRow.style.display = type.value === "null" ? "none" : ""; };
  type.onchange = syncType; syncType();
  const finish = value => { closeModal(); cb(value); };
  $("#condition-else").onclick = () => finish({ branch: "else" });
  $("#condition-cancel").onclick = () => finish(null);
  $("#condition-ok").onclick = () => {
    const expression = $("#condition-expression").value.trim();
    finish(expression ? { branch: expression } : {
      operator: $("#condition-operator").value,
      value_type: type.value,
      value: type.value === "null" ? null : $("#condition-value").value,
    });
  };
  $("#condition-operator").focus();
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
  if (from.type === "condition" && conditionOutputMode(from)) return;
  if (from.type === "condition") askConditionRule(edge, rule => {
    if (rule === null) return;
    applyConditionRule(edge, rule); renderEdges(); markDirty();
  });
  else askBranch((from.params.intents || []).map(i => i.name).filter(Boolean), b => {
    if (b === null) return;
    edge.branch = b; renderEdges(); markDirty();
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
    ${from?.type === "condition" && !conditionOutputMode(from) ? `
      <div class="field"><label>取值路径</label><input value="${esc(from.params?.source || "input.message")}" disabled></div>
      <div class="field"><label>比较规则</label><div class="res-snippet">${esc(conditionRuleLabel(edge))}</div></div>
      <div class="btn-row">
        <button id="e-edit">编辑规则</button>
        <button id="e-del" class="danger">删除连线</button>
      </div>` : `
      <div class="btn-row"><button id="e-del" class="danger">删除连线</button></div>`}`;
  $("#e-del").onclick = () => {
    state.graph.edges = edgesOf().filter(x => x !== edge);
    select(null); renderEdges(); markDirty();
  };
  if ($("#e-edit")) {
    $("#e-edit").onclick = () => askConditionRule(edge, rule => {
      if (rule === null) return;
      applyConditionRule(edge, rule); renderEdges(); markDirty(); renderInspector();
    });
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
  const conditionFields = n.type === "condition" ? conditionBranchesHtml(n) : "";
  const viewsFields = viewsFieldHtml(n);
  const assetFields = assetFieldHtml(n);
  const platformFields = platformFieldHtml(n);
  box.innerHTML = `
    <span class="badge" style="background:${meta.color || "#888"}">${meta.icon || ""} ${meta.label || n.type}</span>
    <div class="palette-note" style="margin:0 0 6px">${esc(meta.desc || "")}</div>
    <div class="field"><label>节点名称</label><input id="n-label" value="${esc(n.label)}"></div>
    ${fields}
    ${agentFields}
    ${intentFields}
    ${conditionFields}
    ${platformFields}
    ${viewsFields}
    ${assetFields}
    <div class="btn-row"><button id="n-del" class="danger">删除节点</button></div>
    ${renderRunOutput(n.id)}`;
  $("#n-label").oninput = e => { n.label = e.target.value; refreshNodeEl(n); markDirty(); };
  bindFormFields(n);
  if (n.type === "agent") bindAgentSelects(n);
  if (n.type === "intent") bindIntentEditor(n);
  if (n.type === "condition") bindConditionBranches(n);
  if (n.type === "character") bindViewsEditor(n);
  if (n.type === "asset") bindAssetSelect(n);
  bindPlatformFields(n);
  $("#n-del").onclick = () => deleteNode(n.id);
}

function conditionBranchesHtml(node) {
  if (conditionOutputMode(node)) {
    const rules = node.params.outputs || [];
    return `<div class="ag-sec">判断并输出</div>
      <div class="field"><label><input type="checkbox" data-condition-output-mode checked style="width:auto"> 使用节点输出模式</label>
        <div class="hint">按顺序执行受限表达式，第一条命中后输出文本；所有结果走同一条下游连线。</div></div>
      ${rules.map((rule, index) => `<div class="res-card" data-condition-output-rule="${index}">
        <div class="field"><label>规则名称</label><input data-condition-output-key="name" value="${esc(rule.name || "")}"></div>
        <div class="field"><label>判断表达式</label><input data-condition-output-key="expression" value="${esc(rule.expression || "")}" placeholder="input.message == '/contact'"></div>
        <div class="field"><label>输出模板</label><textarea data-condition-output-key="output" rows="3">${esc(rule.output || "")}</textarea></div>
        <div class="btn-row"><button type="button" class="danger" data-condition-output-delete="${index}">删除规则</button></div>
      </div>`).join("")}
      <div class="btn-row"><button type="button" data-condition-output-add>添加规则</button></div>
      <div class="field"><label>默认输出</label><textarea data-condition-default-output rows="4">${esc(node.params.default_output || "")}</textarea></div>`;
  }
  const edges = edgesOf().filter(edge => edge.from === node.id);
  return `<div class="ag-sec">分支比较</div>
    <div class="field"><label><input type="checkbox" data-condition-output-mode style="width:auto"> 使用节点输出模式</label></div>
    <div class="hint">按顺序判断，第一条命中后停止；else 作为最后兜底。</div>
    ${edges.map(edge => {
      const target = nodeById(edge.to);
      return `<div class="res-kv"><span>${esc(conditionRuleLabel(edge))} → ${esc(target?.label || edge.to)}</span>
        <button type="button" data-condition-edge="${esc(edgeKey(edge))}">编辑</button></div>`;
    }).join("") || '<div class="hint">连接下游节点后即可配置比较规则。</div>'}`;
}
function bindConditionBranches(node) {
  const mode = $("#insp-body [data-condition-output-mode]");
  if (mode) mode.onchange = () => {
    if (mode.checked) {
      node.params.outputs = [{ name: "rule_1", expression: "input.message == '/command'", output: "" }];
      node.params.default_output = "处理访客请求：{{input.message}}";
    } else {
      delete node.params.outputs;
      delete node.params.default_output;
    }
    refreshNodeEl(node); markDirty(); renderEdges(); renderInspector();
  };
  $$("#insp-body [data-condition-output-rule]").forEach(card => {
    const index = Number(card.dataset.conditionOutputRule);
    card.querySelectorAll("[data-condition-output-key]").forEach(input => input.oninput = () => {
      const rule = node.params.outputs?.[index];
      if (!rule) return;
      rule[input.dataset.conditionOutputKey] = input.value;
      refreshNodeEl(node); markDirty();
    });
  });
  $$("#insp-body [data-condition-output-delete]").forEach(button => button.onclick = () => {
    node.params.outputs.splice(Number(button.dataset.conditionOutputDelete), 1);
    refreshNodeEl(node); markDirty(); renderInspector();
  });
  const add = $("#insp-body [data-condition-output-add]");
  if (add) add.onclick = () => {
    const index = node.params.outputs.length + 1;
    node.params.outputs.push({ name: `rule_${index}`, expression: "", output: "" });
    refreshNodeEl(node); markDirty(); renderInspector();
  };
  const fallback = $("#insp-body [data-condition-default-output]");
  if (fallback) fallback.oninput = () => {
    node.params.default_output = fallback.value;
    refreshNodeEl(node); markDirty();
  };
  $$("#insp-body [data-condition-edge]").forEach(button => button.onclick = () => {
    const edge = edgesOf().find(item => edgeKey(item) === button.dataset.conditionEdge);
    if (!edge) return;
    askConditionRule(edge, rule => {
      if (rule === null) return;
      applyConditionRule(edge, rule); renderEdges(); markDirty(); renderInspector();
    });
  });
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

function dependencyKey(type, id) { return `${type}:${id}`; }
function flowAgentIds(flow) {
  if (state.graph?.id === flow.id) return [...new Set((state.graph.nodes || [])
    .filter(node => node.type === "ai_agent")
    .map(node => String(node.params?.ai_agent_id || "").trim()).filter(Boolean))];
  return flow.ai_agent_ids || [];
}
function dependencyGraph() {
  const graph = new Map();
  const ensure = key => { if (!graph.has(key)) graph.set(key, new Set()); return graph.get(key); };
  for (const agent of state.aiAgents || []) {
    const from = dependencyKey("agent", agent.id); ensure(from);
    if (agent.flow_id) ensure(from).add(dependencyKey("flow", agent.flow_id));
  }
  for (const flow of state.flows || []) {
    const from = dependencyKey("flow", flow.id); ensure(from);
    for (const agentId of flowAgentIds(flow))
      ensure(from).add(dependencyKey("agent", agentId));
  }
  if (state.graph?.id && !(state.flows || []).some(flow => flow.id === state.graph.id)) {
    const from = dependencyKey("flow", state.graph.id); ensure(from);
    for (const agentId of flowAgentIds(state.graph))
      ensure(from).add(dependencyKey("agent", agentId));
  }
  for (const targets of graph.values()) for (const target of targets) ensure(target);
  return graph;
}
function dependencyPath(start, target) {
  const graph = dependencyGraph();
  const queue = [[start, [start]]], visited = new Set();
  while (queue.length) {
    const [current, path] = queue.shift();
    if (current === target) return path;
    if (visited.has(current)) continue;
    visited.add(current);
    for (const next of graph.get(current) || [])
      if (!visited.has(next) || next === target) queue.push([next, [...path, next]]);
  }
  return null;
}
function dependencyLabel(key) {
  const [type, ...idParts] = key.split(":"), id = idParts.join(":");
  if (type === "agent") {
    const item = state.aiAgents.find(agent => agent.id === id);
    return `智能体「${item?.name || id}」`;
  }
  const item = state.flows.find(flow => flow.id === id);
  return `流程「${item?.name || id}」`;
}
function dependencyCycleReason(fromType, fromId, toType, toId) {
  if (!fromId || !toId) return "";
  const from = dependencyKey(fromType, fromId), to = dependencyKey(toType, toId);
  const path = dependencyPath(to, from);
  return path ? `会形成循环：${[from, ...path].map(dependencyLabel).join(" → ")}` : "";
}

function plannedAgentCycleReason(referencingFlowId, targetFlowId) {
  if (!referencingFlowId || !targetFlowId) return "";
  const target = dependencyKey("flow", targetFlowId);
  const referencing = dependencyKey("flow", referencingFlowId);
  const path = dependencyPath(target, referencing);
  if (!path) return "";
  return `会形成循环：${[
    referencing,
    "agent:__new__",
    ...path,
  ].map(key => key === "agent:__new__" ? "新智能体" : dependencyLabel(key)).join(" → ")}`;
}

/* ================= 智能体平台节点编辑器（智能体/知识库/技能/MCP/工具） ================= */
function platformFieldHtml(n) {
  const p = n.params || {};
  if (n.type === "ai_agent") {
    const options = state.aiAgents.map(a => ({
      agent: a,
      reason: dependencyCycleReason("flow", state.graph?.id || "", "agent", a.id),
    }));
    const opts = options.map(({ agent: a, reason }) =>
      `<option value="${esc(a.id)}" ${a.id === p.ai_agent_id ? "selected" : ""}
        ${reason ? `disabled title="${esc(reason)}"` : ""}>
        ${esc(a.name)}（${esc(a.id)}）${reason ? " · 会形成循环" : ""}</option>`).join("");
    const cur = state.aiAgents.find(a => a.id === p.ai_agent_id);
    const blocked = options.filter(option => option.reason);
    const skillOptions = (cur?.skill_ids || []).map(skill =>
      `<option value="${esc(skill)}" ${skill === p.skill_id ? "selected" : ""}>${esc(skill)}</option>`).join("");
    const dynamicSkill = String(p.skill_id || "").includes("{{");
    return `
      <div class="field"><label>选择已有智能体</label>
        <select id="ai-agent-select"><option value="">（请选择智能体）</option>${opts}</select>
        <div class="btn-row">
          ${can("resource.write") ? '<button id="ai-agent-new" type="button" class="primary">＋ 新建智能体</button>' : ""}
          <button id="ai-agent-open" type="button">管理智能体</button>
        </div>
        ${blocked.length ? '<div class="hint">会回到当前流程的智能体已禁用，避免直接或间接循环。</div>' : ""}
        ${cur ? `<div class="hint">${esc(cur.description || "")}
          · 绑定：${[...(cur.kb_ids || []).map(x => "知识库:" + x),
                    ...(cur.skill_ids || []).map(x => "技能:" + x),
                    ...(cur.tool_ids || []).map(x => "工具:" + x),
                    ...(cur.mcp_servers || []).map(x => "MCP:" + x)].join("、") || "无"}
          ${cur.memory ? " · 记忆开" : ""}</div>` : ""}
        ${cur?.runtime === "customer-agent" ? `<div class="field"><label>该节点执行的 Skill</label>
          <select id="ai-agent-skill">
            ${dynamicSkill ? `<option value="${esc(p.skill_id)}" selected>由上游分支动态选择（${esc(p.skill_id)}）</option>` : ""}
            <option value="">（请选择 Skill）</option>${skillOptions}
          </select><div class="hint">Flow 只能选择该 Agent 已绑定的 Skill。</div></div>` : ""}</div>`;
  }
  if (n.type === "kb") {
    const ids = p.kb_ids || [];
    const boxes = state.kbs.map(k =>
      `<label class="res-kv"><input type="checkbox" data-kbid="${esc(k.id)}"
        ${ids.includes(k.id) ? "checked" : ""} style="width:auto">
        ${esc(k.name)}（${k.docs} 文档）</label>`).join("");
    return `
      <div class="field"><label>检索范围（不勾 = 全部知识库）</label>
        <div>${boxes || '<span class="hint">暂无知识库，可在「资源库 → 知识库」创建</span>'}</div>
        <button id="kb-open" type="button" style="margin-top:5px">管理知识库 →</button></div>`;
  }
  if (n.type === "skill") {
    const opts = state.skillList.map(s =>
      `<option value="${esc(s.id)}" ${s.id === p.skill_id ? "selected" : ""}>
        ${esc(s.name)}（${esc(s.id)}）</option>`).join("");
    return `
      <div class="field"><label>选择技能（资源库中创建/编辑）</label>
        <select id="skill-select">${opts || '<option value="">（暂无技能）</option>'}</select>
        <button id="skill-open" type="button" style="margin-top:5px">管理技能 →</button></div>`;
  }
  if (n.type === "mcp") {
    const servers = Object.keys(state.mcpCfg.servers || {});
    const srvOpts = servers.map(s =>
      `<option value="${esc(s)}" ${s === p.server ? "selected" : ""}>${esc(s)}</option>`).join("");
    const curSrv = p.server && servers.includes(p.server) ? p.server : servers[0];
    const tools = curSrv ? (state.mcpTools[curSrv] || []) : [];
    const toolOpts = tools.map(t =>
      `<option value="${esc(t.name)}" ${t.name === p.tool ? "selected" : ""}>
        ${esc(t.name)} — ${esc((t.description || "").slice(0, 40))}</option>`).join("");
    return `
      <div class="field"><label>MCP 服务器</label>
        <select id="mcp-server">${srvOpts || '<option value="">（未配置）</option>'}</select>
        <button id="mcp-open" type="button" style="margin-top:5px">配置 MCP →</button></div>
      <div class="field"><label>工具 ${tools.length ? "" : "（选择服务器后加载）"}</label>
        <select id="mcp-tool">${toolOpts || '<option value="">—</option>'}</select>
        <div class="hint" id="mcp-schema">${mcpSchemaHint(tools, p.tool)}</div></div>`;
  }
  if (n.type === "tool") {
    const opts = state.toolList.map(t =>
      `<option value="${esc(t.name)}" ${t.name === p.tool ? "selected" : ""}>
        ${esc(t.name)} — ${esc(t.description)}</option>`).join("");
    return `
      <div class="field"><label>选择工具</label>
        <select id="tool-select">${opts || '<option value="">（无工具）</option>'}</select></div>`;
  }
  return "";
}
function mcpSchemaHint(tools, name) {
  const t = tools.find(x => x.name === name);
  const props = t?.parameters?.properties;
  if (!props || !Object.keys(props).length) return "该工具无参数或未声明。";
  return "参数：" + Object.entries(props).map(([k, v]) =>
    `${k}${(t.parameters.required || []).includes(k) ? "*" : ""}（${v.type || "any"}）${v.description ? " — " + v.description : ""}`).join("；");
}
function bindPlatformFields(n) {
  const p = n.params || {};
  if (n.type === "ai_agent") {
    const sel = $("#ai-agent-select");
    if (sel) sel.onchange = () => {
      const reason = dependencyCycleReason("flow", state.graph?.id || "", "agent", sel.value);
      if (reason) { sel.value = p.ai_agent_id || ""; return toast(reason, "err"); }
      p.ai_agent_id = sel.value; refreshNodeEl(n); markDirty();
      const selected = state.aiAgents.find(a => a.id === sel.value);
      p.skill_id = selected?.runtime === "customer-agent" ? (selected.skill_ids || [])[0] || "" : "";
      renderInspector();
    };
    $("#ai-agent-new")?.addEventListener("click", () => agentEditModal(null, {
      referencingFlowId: state.graph?.id || "",
      onSaved: saved => {
        p.ai_agent_id = saved.id;
        refreshNodeEl(n);
        markDirty();
        renderInspector();
      },
    }));
    $("#ai-agent-open")?.addEventListener("click", () => openResPanel("agents"));
    const skill = $("#ai-agent-skill");
    if (skill) skill.onchange = () => {
      p.skill_id = skill.value; refreshNodeEl(n); markDirty();
    };
  }
  if (n.type === "kb") {
    $$('#insp-body [data-kbid]').forEach(cb => cb.onchange = () => {
      const ids = new Set(p.kb_ids || []);
      cb.checked ? ids.add(cb.dataset.kbid) : ids.delete(cb.dataset.kbid);
      p.kb_ids = [...ids]; refreshNodeEl(n); markDirty();
    });
    $("#kb-open")?.addEventListener("click", () => openResPanel("kb"));
  }
  if (n.type === "skill") {
    const sel = $("#skill-select");
    if (sel) sel.onchange = () => { p.skill_id = sel.value; refreshNodeEl(n); markDirty(); };
    $("#skill-open")?.addEventListener("click", () => openResPanel("skills"));
  }
  if (n.type === "mcp") {
    const srv = $("#mcp-server");
    if (srv) srv.onchange = async () => {
      p.server = srv.value; p.tool = "";
      await ensureMcpTools(p.server);
      renderInspector();
    };
    const toolSel = $("#mcp-tool");
    if (toolSel) toolSel.onchange = () => { p.tool = toolSel.value; refreshNodeEl(n); markDirty(); };
    $("#mcp-open")?.addEventListener("click", () => openResPanel("mcp"));
  }
  if (n.type === "tool") {
    const sel = $("#tool-select");
    if (sel) sel.onchange = () => { p.tool = sel.value; refreshNodeEl(n); markDirty(); };
  }
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
  if (!runMatchesCanvas()) return null;
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
  if (Array.isArray(output.tracks)) items = items.concat(output.tracks);
  if (output.url && output.path) items.unshift(output);
  if (output.audio) items.push(output.audio);
  if (output.subtitle) items.push(output.subtitle);
  if (!items.length) return "";
  const cells = items.map(it => {
    if (!it.url && !it.path) return "";
    const ref = String(it.path || it.url || "").toLowerCase();
    const kind = it.kind || (ref.endsWith(".mp4") ? "video"
      : ref.endsWith(".wav") || ref.endsWith(".mp3") ? "audio"
        : ref.endsWith(".srt") ? "file" : "image");
    const url = it.url || `/media/files/${it.path}`;
    if (kind === "video")
      return `<video class="media-thumb" src="${esc(url)}" controls preload="metadata"></video>`;
    if (kind === "audio")
      return `<audio class="media-thumb" src="${esc(url)}" controls preload="metadata"></audio>`;
    if (kind === "file")
      return `<a class="media-thumb media-file" href="${esc(url)}" download>下载 ${esc(it.name || "字幕文件")}</a>`;
    return `<img class="media-thumb" src="${esc(url)}" alt="" loading="lazy">`;
  }).filter(Boolean).join("");
  return cells ? `<div class="media-grid">${cells}</div>` : "";
}
function renderRunOutput(nodeId) {
  const run = state.lastRun;
  if (!run || !runMatchesCanvas()) return "";
  const part = nodeId ? runOf(nodeId) : null;
  if (nodeId && !part) return "";
  const data = nodeId ? part : run;
  return `
    <h3 style="margin-top:10px">${nodeId ? "节点输入输出" : "流程输出"}</h3>
    <div class="field"><label>状态：${data.status || "-"}</label></div>
    ${data.error ? `<div class="field"><label style="color:var(--err)">错误</label>
      <div class="json-view">${esc(data.error)}</div></div>` : ""}
    ${tracebackHtml(data.traceback)}
    ${mediaGridFromOutput(data.output)}
    <div class="json-view">${esc(fmtJson(data.output))}</div>`;
}
function fmtJson(v) { try { return JSON.stringify(v, null, 2); } catch { return String(v); } }

/* ================= 持久运行与增量日志 ================= */
const RUN_STATUS = { queued: "排队中", pending: "等待", running: "运行中", success: "成功",
  failed: "失败", skipped: "跳过 / 降级", interrupted: "已中断" };
const terminalRun = run => ["success", "failed", "interrupted"].includes(run?.status);
function runScope() { return `${state.me?.user?.user_id || ""}:${state.workspaceId}`; }
function runStorageKey() { return `flow-studio-active-run:${runScope()}`; }
function rememberRun(run) {
  try { localStorage.setItem(runStorageKey(), run.run_id); } catch { /* 存储不可用仍可实时跟踪 */ }
}
function forgetRun(runId) {
  try {
    if (localStorage.getItem(runStorageKey()) === runId) localStorage.removeItem(runStorageKey());
  } catch { /* 忽略浏览器存储限制 */ }
}
function runMatchesCanvas() {
  const run = state.lastRun, graph = state.graph;
  if (!run || !graph || run.flow_id !== graph.id) return false;
  // 旧记录没有执行图，不能将它的状态误叠到当前版本。
  if (!run.graph) return false;
  const shape = g => JSON.stringify({ nodes: (g.nodes || []).map(n => ({ id: n.id,
    type: n.type, label: n.label, params: n.params || {} })), edges: (g.edges || []).map(e =>
    ({ from: e.from, to: e.to, branch: e.branch || "" })) });
  return shape(run.graph) === shape(graph);
}
function tracebackHtml(value) {
  return value ? `<details class="run-traceback"><summary>完整异常堆栈</summary><pre>${esc(value)}</pre></details>` : "";
}
function applyBadge(el, r) {
  const badge = el.querySelector(".run-badge");
  if (badge) badge.textContent = RUN_STATUS[r.status] || r.status;
}
function highlightRun() {
  const runs = runMatchesCanvas() ? state.lastRun.node_runs || [] : [];
  const byNode = Object.fromEntries(runs.map(r => [r.node_id, r]));
  for (const el of nodeEls.values()) {
    el.classList.remove("st-running", "st-success", "st-failed", "st-skipped");
    const r = byNode[el.dataset.id];
    if (!r || r.status === "pending") { el.querySelector(".run-badge").textContent = ""; continue; }
    el.classList.add(`st-${r.status}`); applyBadge(el, r);
  }
}
function updateRunControls() {
  const busy = state.runSubmitting || state.runMonitor.active;
  ["#btn-run", "#chat-send", "#flow-preview"].forEach(selector => {
    const el = $(selector); if (el) el.disabled = busy;
  });
}
function stopRunTracking(clear = false) {
  const monitor = state.runMonitor;
  monitor.generation++;
  clearTimeout(monitor.timer); monitor.controller?.abort();
  monitor.timer = null; monitor.controller = null; monitor.onComplete = null; monitor.active = false;
  if (clear) {
    state.lastRun = null; state.runSubmitting = false;
    monitor.events = []; monitor.seq = 0;
    $("#run-panel").classList.remove("open");
    $("#rp-list").replaceChildren(); $("#rp-logs").replaceChildren();
    $("#rp-output").replaceChildren(); $("#run-status").textContent = "";
    if ($("#modal").dataset.runView) closeModal();
    highlightRun(); updateRunControls();
  }
}
function showRunTab(tab) {
  $$("[data-run-tab]").forEach(button => button.setAttribute("aria-selected", String(button.dataset.runTab === tab)));
  $("#rp-list").hidden = tab !== "nodes"; $("#rp-logs").hidden = tab !== "logs";
  $("#rp-output").hidden = tab !== "output";
  if (tab === "logs" && $("#rp-follow").checked) $("#rp-logs").scrollTop = $("#rp-logs").scrollHeight;
}
$$("[data-run-tab]").forEach(button => button.onclick = () => showRunTab(button.dataset.runTab));
$("#rp-follow").onchange = () => {
  if ($("#rp-follow").checked) $("#rp-logs").scrollTop = $("#rp-logs").scrollHeight;
};
function renderRunPanel(run) {
  $("#rp-title").textContent = `${run.flow_name || run.flow_id} · ${run.run_id}`;
  const running = (run.node_runs || []).filter(n => n.status === "running").map(n => n.label || n.node_id);
  const status = RUN_STATUS[run.status] || run.status;
  $("#rp-status").textContent = running.length ? `${status}：${running.join("、")}` : status;
  $("#rp-status").style.color = run.status === "success" ? "var(--ok)"
    : ["failed", "interrupted"].includes(run.status) ? "var(--err)" : "var(--warn)";
  $("#run-status").textContent = status;
  $("#run-status").className = run.status === "success" ? "ok" : terminalRun(run) ? "err" : "";
  $("#rp-snapshot").disabled = !run.graph;
  $("#rp-version-note").textContent = !run.graph ? "历史记录未保存执行图，仅展示记录中的节点状态。"
    : runMatchesCanvas() ? "状态来自本次执行图；点击节点查看本次输入输出。"
    : "本次执行图与当前画布不同；请打开「执行图快照」查看实际运行节点。";
  const html = (run.node_runs || []).map(r => `<button class="rp-row" data-node="${esc(r.node_id)}">
    <span class="st" data-status="${esc(r.status)}">${esc(RUN_STATUS[r.status] || r.status)}</span>
    <b>${esc(r.label || r.node_id)}</b><span class="errmsg">${esc(r.error || "")}</span>
    <span class="ms">${r.status === "running" ? "执行中" : r.ms == null ? "" : `${r.ms}ms`}</span></button>`).join("")
    || '<div class="run-empty">等待节点开始执行…</div>';
  // 无变化时保留焦点，滚动位置与展开的堆栈。
  if ($("#rp-list").innerHTML !== html) {
    $("#rp-list").innerHTML = html;
    $$("#rp-list [data-node]").forEach(row => row.onclick = () => showRunNode(row.dataset.node));
  }
  const output = `${run.error ? `<div class="run-error">${esc(run.error)}</div>` : ""}${tracebackHtml(run.traceback)}
    ${mediaGridFromOutput(run.output)}<pre class="json-view">${esc(fmtJson(run.output) ?? "暂无输出")}</pre>`;
  if ($("#rp-output").dataset.value !== output) {
    $("#rp-output").innerHTML = output; $("#rp-output").dataset.value = output;
  }
  highlightRun(); updateRunControls(); refreshRunModal();
}
function appendRunEvents(events) {
  const monitor = state.runMonitor, list = $("#rp-logs");
  for (const event of events) {
    if (event.seq <= monitor.seq) continue;
    monitor.seq = event.seq; monitor.events.push(event);
    if (list.querySelector(".run-empty")) list.replaceChildren();
    const row = document.createElement("div"); row.className = "run-log-entry";
    row.dataset.level = String(event.level || "info").toLowerCase();
    const date = new Date(event.timestamp), timestamp = Number.isNaN(date.valueOf())
      ? event.timestamp || "" : date.toLocaleTimeString();
    row.innerHTML = `<div class="run-log-meta"><time>${esc(timestamp)}</time>
      <span>${esc(event.level || "info")}</span><b>${esc(event.node_label || event.node_id || "流程")}</b>
      <span>#${esc(event.seq)}</span></div><pre>${esc(event.message || event.type)}</pre>${tracebackHtml(event.traceback)}`;
    list.append(row);
  }
  $("#rp-log-count").textContent = monitor.events.length;
  if ($("#rp-follow").checked) list.scrollTop = list.scrollHeight;
}
function trackRun(run, { onComplete = null } = {}) {
  stopRunTracking();
  const monitor = state.runMonitor;
  monitor.seq = 0; monitor.events = []; monitor.retries = 0; monitor.onComplete = onComplete;
  monitor.active = !terminalRun(run);
  state.lastRun = run;
  // 查看已完成的历史，不覆盖仍在后台执行的恢复入口。
  if (!terminalRun(run)) rememberRun(run);
  $("#rp-logs").innerHTML = '<div class="run-empty">正在读取运行日志…</div>';
  $("#rp-log-count").textContent = "0";
  $("#rp-output").dataset.value = "";
  $("#rp-connection").textContent = "正在连接…";
  $("#run-panel").classList.add("open"); switchView("flows-editor");
  $("#gov-panel").classList.remove("open");
  renderRunPanel(run);
  pollRun(monitor.generation, runScope());
}
async function pollRun(generation, scope) {
  const monitor = state.runMonitor;
  if (generation !== monitor.generation || scope !== runScope() || !state.lastRun) return;
  const runId = state.lastRun.run_id;
  monitor.controller = new AbortController();
  let delay = 500;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(runId)}/events?after=${monitor.seq}&limit=200`,
      { signal: monitor.controller.signal });
    if (generation !== monitor.generation || scope !== runScope()) return;
    monitor.retries = 0; state.lastRun = data.run;
    appendRunEvents(data.events || []);
    monitor.seq = Math.max(monitor.seq, data.next_seq || 0);
    renderRunPanel(data.run);
    $("#rp-connection").textContent = data.has_more ? "正在补齐日志…" : terminalRun(data.run) ? "日志已同步" : "实时更新中";
    // 结束状态可能先于分页日志到达，必须排空事件后再停止。
    if (terminalRun(data.run) && !data.has_more) {
      monitor.active = false; updateRunControls();
      forgetRun(runId);
      if (!monitor.events.length) $("#rp-logs").innerHTML = '<div class="run-empty">此运行没有事件日志；旧记录可在节点和输出中查看结果。</div>';
      const onComplete = monitor.onComplete; monitor.onComplete = null;
      if (runMatchesCanvas() && !$("#inspector").contains(document.activeElement)) renderInspector();
      loadMedia().catch(() => {});
      onComplete?.(data.run);
      return;
    }
    if (data.has_more) delay = 0;
  } catch (error) {
    if (generation !== monitor.generation || scope !== runScope() || error.name === "AbortError") return;
    if ([401, 403, 404].includes(error.status)) {
      $("#rp-connection").textContent = `无法继续读取：${error.message}`;
      monitor.active = false; updateRunControls();
      forgetRun(runId); return;
    }
    monitor.retries++;
    delay = Math.min(10000, 500 * 2 ** Math.min(monitor.retries, 5));
    $("#rp-connection").textContent = `连接中断，${delay / 1000} 秒后重连（不会重新执行）`;
  }
  if (generation === monitor.generation && scope === runScope())
    monitor.timer = setTimeout(() => pollRun(generation, scope), delay);
}
async function restoreRunTracking() {
  let runId; try { runId = localStorage.getItem(runStorageKey()); } catch { return; }
  if (!runId) return;
  const scope = runScope(), generation = state.runMonitor.generation;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (scope !== runScope() || generation !== state.runMonitor.generation) return;
    if (!state.dirty && run.flow_id && run.flow_id !== state.graph?.id) {
      try {
        await loadFlow(run.flow_id, { restoreGeneration: generation });
      } catch (error) {
        // 流程被删除或无权读取时，运行记录和执行图快照仍可独立恢复。
        if (scope === runScope() && error.status !== 401)
          toast(`无法加载原流程，将恢复运行记录与执行图快照：${error.message}`, "err");
      }
    }
    if (scope === runScope() && generation === state.runMonitor.generation) trackRun(run);
  } catch (error) {
    if (scope === runScope() && [403, 404].includes(error.status)) forgetRun(runId);
    if (error.status !== 401) toast(`恢复运行失败，可从运行记录重试：${error.message}`, "err");
  }
}
async function submitRun(request, options = {}) {
  if (state.runSubmitting || state.runMonitor.active) {
    $("#run-panel").classList.add("open"); return toast("已有运行正在跟踪，请等待完成或从运行记录切换。");
  }
  const scope = runScope(); state.runSubmitting = true; updateRunControls();
  try {
    const run = await request();
    if (scope !== runScope()) return;
    if (run) trackRun(run, options);
  } catch (error) {
    if (scope === runScope()) showApiError(error, "启动运行失败");
  } finally {
    if (scope === runScope()) { state.runSubmitting = false; updateRunControls(); }
  }
}
function showRunNode(nodeId) {
  const node = state.lastRun?.node_runs?.find(n => n.node_id === nodeId);
  if (!node) return;
  openModal(`<h2>${esc(node.label || nodeId)} · 本次运行</h2><div id="run-node-detail"></div>
    <div class="btn-row"><button id="run-node-close" class="primary">关闭</button></div>`);
  $("#modal").dataset.runView = "node"; $("#modal").dataset.runNode = nodeId;
  $("#run-node-close").onclick = closeModal; refreshRunModal();
}
function showRunSnapshot() {
  if (!state.lastRun?.graph) return;
  openModal('<h2>本次执行图快照</h2><p class="run-caption">只读显示本次实际执行的节点和连线；状态随运行实时更新。</p><div id="run-snapshot"></div><div class="btn-row"><button id="run-snapshot-close" class="primary">关闭</button></div>');
  $("#modal").dataset.runView = "snapshot";
  $("#run-snapshot-close").onclick = closeModal; refreshRunModal();
}
function refreshRunModal() {
  const modal = $("#modal"), run = state.lastRun;
  if (!run || !$("#modal-mask").classList.contains("open")) return;
  if (modal.dataset.runView === "node") {
    const node = run.node_runs?.find(n => n.node_id === modal.dataset.runNode);
    if (!node) return closeModal();
    const html = `<p>${esc(RUN_STATUS[node.status] || node.status)}${node.ms == null ? "" : ` · ${node.ms}ms`}</p>
      ${node.error ? `<div class="run-error">${esc(node.error)}</div>` : ""}${tracebackHtml(node.traceback)}
      ${node.input === undefined ? "" : `<h3>输入</h3><pre class="json-view">${esc(fmtJson(node.input))}</pre>`}
      <h3>输出</h3>${mediaGridFromOutput(node.output)}<pre class="json-view">${esc(fmtJson(node.output) ?? "暂无输出")}</pre>`;
    const box = $("#run-node-detail");
    if (box.dataset.value !== html) { box.innerHTML = html; box.dataset.value = html; }
  } else if (modal.dataset.runView === "snapshot" && run.graph) {
    const nodes = run.graph.nodes || [], runs = Object.fromEntries((run.node_runs || []).map(n => [n.node_id, n]));
    const positions = Object.fromEntries(nodes.map((node, i) => [node.id, {
      x: Number.isFinite(Number(node.pos?.x)) ? Number(node.pos.x) : 0,
      y: Number.isFinite(Number(node.pos?.y)) ? Number(node.pos.y) : i * 90 }]));
    const minX = Math.min(0, ...Object.values(positions).map(p => p.x)), minY = Math.min(0, ...Object.values(positions).map(p => p.y));
    const width = Math.max(400, ...Object.values(positions).map(p => p.x + 248)) - minX;
    const height = Math.max(120, ...Object.values(positions).map(p => p.y + 94)) - minY;
    const edges = (run.graph.edges || []).map(e => {
      const a = positions[e.from], b = positions[e.to]; if (!a || !b) return "";
      return `<path d="M ${a.x + 112} ${a.y + 64} L ${b.x + 112} ${b.y}"/><text x="${(a.x + b.x) / 2 + 120}" y="${(a.y + b.y) / 2 + 32}">${esc(e.branch || "")}</text>`;
    }).join("");
    const boxes = nodes.map(n => {
      const p = positions[n.id], status = runs[n.id]?.status || "pending";
      return `<g class="snapshot-node" data-node="${esc(n.id)}" data-status="${esc(status)}" tabindex="0" role="button" aria-label="${esc(n.label || n.id)}：${esc(RUN_STATUS[status] || status)}" transform="translate(${p.x},${p.y})">
        <rect width="224" height="64" rx="10"/><text x="12" y="25">${esc((n.label || n.id).slice(0, 24))}</text>
        <text class="snapshot-status" x="12" y="48">${esc(RUN_STATUS[status] || status)}</text></g>`;
    }).join("");
    $("#run-snapshot").innerHTML = `<svg viewBox="${minX - 12} ${minY - 12} ${width + 24} ${height + 24}" style="min-width:${Math.min(width, 1100)}px" xmlns="http://www.w3.org/2000/svg"><g class="snapshot-edges">${edges}</g>${boxes}</svg>`;
    $$("#run-snapshot [data-node]").forEach(el => {
      el.onclick = () => showRunNode(el.dataset.node);
      el.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showRunNode(el.dataset.node); } };
    });
  }
}
async function showRunHistory() {
  const scope = runScope(), flowId = state.graph?.id;
  try {
    const result = await api(`/api/runs${flowId ? `?flow_id=${encodeURIComponent(flowId)}` : ""}`);
    if (scope !== runScope()) return;
    const runs = Array.isArray(result) ? result : result.runs || [];
    openModal(`<h2>${esc(state.graph?.name || "全部流程")} · 运行记录</h2><div class="run-history">${runs.map(run =>
      `<button data-run-id="${esc(run.run_id)}"><b>${esc(RUN_STATUS[run.status] || run.status)}</b><span>${esc(run.started_at || run.created_at || "")}</span><small>${esc(run.run_id)}</small></button>`).join("") || '<p class="run-empty">暂无运行记录</p>'}</div><div class="btn-row"><button id="run-history-close">关闭</button></div>`);
    $("#modal").dataset.runView = "history";
    $("#run-history-close").onclick = closeModal;
    $$("[data-run-id]").forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {
        const run = await api(`/api/runs/${encodeURIComponent(button.dataset.runId)}`);
        if (scope !== runScope()) return;
        closeModal(); trackRun(run);
      } catch (error) { if (scope === runScope()) showApiError(error, "读取运行失败"); }
      finally { button.disabled = false; }
    });
  } catch (error) { if (scope === runScope()) showApiError(error, "读取运行记录失败"); }
}
$("#btn-runs").onclick = showRunHistory;
$("#rp-snapshot").onclick = showRunSnapshot;
$("#rp-close").onclick = () => $("#run-panel").classList.remove("open");

/* ================= 工具函数 ================= */
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function markDirty() {
  state.dirty = true;
  if (state.lastRun) renderRunPanel(state.lastRun);
}
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
  const preferred = selectId || state.graph?.id;
  if (preferred && state.flows.some(f => f.id === preferred)) sel.value = preferred;
  if (state.viewMode === "flows-list") renderFlowsHome();
  return sel.value;
}

async function loadFlow(id, { restoreGeneration = null } = {}) {
  if (!id) { state.graph = null; select(null); renderWorld();
    renderInspector(); renderFlowGovernance(); return; }
  const previousGraph = state.graph, scope = runScope();
  const data = await api(`/api/flows/${encodeURIComponent(id)}`);
  if (restoreGeneration !== null && (state.dirty || state.graph !== previousGraph
    || scope !== runScope() || restoreGeneration !== state.runMonitor.generation)) return;
  data._saved = true;
  state.graph = data;
  $("#flow-select").value = id;
  state.sel = null;
  state.view = { x: 40, y: 30, s: 1 };
  renderWorld(); renderInspector();
  renderFlowGovernance();
  fitView();
  closeDrawers();
  if (state.lastRun) renderRunPanel(state.lastRun);
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
    // Saving replaces edge objects and drops their transient UI ids.
    state.sel = null;
    renderWorld();
    renderInspector();
    renderFlowGovernance();
    toast("草稿已保存", "ok");
    await loadFlowList(g.id);
  } catch (e) { toast(`保存失败：${e.message}`, "err"); }
}
function graphBody() {
  const g = state.graph;
  return { id: g.id, name: g.name, description: g.description, triggers: g.triggers || [],
           nodes: (g.nodes || []).map(n => ({ id: n.id, type: n.type, label: n.label,
                                              params: n.params || {}, pos: n.pos || {} })),
           edges: (g.edges || []).map(e => ({ from: e.from, to: e.to, ...(e.branch ? { branch: e.branch } : {}) })) };
}

function renderFlowGovernance() {
  const box = $("#flow-governance");
  if (!box) return;
  const gov = state.graph?._governance;
  if (!gov) { box.innerHTML = ""; return; }
  box.innerHTML = `<span class="status-badge" data-status="${esc(gov.status)}">v${gov.version} ${esc(gov.status)}</span>
    ${can("resource.write") && gov.status === "draft" ? '<button id="flow-submit">提交审批</button>' : ""}
    ${can("runtime.preview") && gov.status !== "published" ? '<button id="flow-preview">预览</button>' : ""}
    <button id="flow-versions">版本</button>`;
  $("#flow-submit")?.addEventListener("click", () =>
    submitResource("flow", state.graph.id, gov.version));
  $("#flow-preview")?.addEventListener("click", async () => {
    const flow = state.graph, scope = runScope();
    const inputs = await promptInputs(flow); if (inputs === null || scope !== runScope()) return;
    await submitRun(() => governanceAction("flow", flow.id, gov.version,
      "preview", { inputs, background: true }));
  });
  $("#flow-versions").onclick = () => openVersions("flow", state.graph.id);
  updateRunControls();
}

/* 新建 / 删除流程 */
function openNewFlowModal() {
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
      await openFlowEditor(g.id);
      toast("已创建 ✅", "ok");
    } catch (e) { toast(`创建失败：${e.message}`, "err"); }
  };
}
$("#btn-new").onclick = openNewFlowModal;
$("#flows-new").onclick = openNewFlowModal;
$("#btn-flow-back").onclick = returnToFlowList;
$("#btn-del").onclick = async () => {
  const id = $("#flow-select").value;
  if (!id || !confirm(`确认删除流程「${id}」？`)) return;
  await api(`/api/flows/${encodeURIComponent(id)}`, { method: "DELETE" });
  state.graph = null; state.sel = null; state.dirty = false;
  await loadFlowList();
  switchView("flows-list");
  toast("已生成删除草稿", "ok");
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
    ["provider", "select", "提供方：http=OpenAI 风格两段式；hf_gradio=Hugging Face Space（匿名免费）", ""],
    ["hf_space", "text", "HF Space id（hf_gradio 用，如 Saravutw/WAN2.2_I2V_LIGHTNING_4-8step_custom）"],
    ["hf_api", "text", "Space 的 Gradio 端点（默认 /generate_video）"],
    ["hf_token", "text", "免费 HF token（可选，大幅提高 ZeroGPU 配额）"],
    ["base_url", "text", "Base URL（http 提供方用）"],
    ["api_key", "text", "API Key（http 提供方用）"], ["model", "text", "模型名"],
    ["submit_path", "text", "提交任务路径"], ["poll_path", "text", "轮询路径（{task_id} 占位）"],
    ["interval", "number", "轮询间隔秒"], ["timeout", "number", "总超时秒"],
  ],
  speech: [
    ["enabled", "bool", "启用 team-agent 文本转语音（失败时中断成片流程）"],
    ["base_url", "text", "voice-service 地址（默认 http://127.0.0.1:17863）"],
    ["token", "text", "Bearer Token（本机回环地址可留空）"],
    ["voice", "text", "默认音色"], ["speed", "number", "默认语速（0.5–2.0）"],
    ["timeout", "number", "单段超时秒"],
  ],
};
$("#btn-models").onclick = async () => {
  let cfg;
  try { cfg = await api("/api/video/models"); }
  catch (e) { return toast(`读取模型配置失败：${e.message}`, "err"); }
  const sections = Object.entries(MODEL_FIELDS).map(([group, fields]) => `
    <h3 style="margin:14px 0 4px">${{ llm: "分镜 / 文案 LLM", image: "文生图（关键帧 / 角色设定）",
      video: "图生视频（镜头片段）", speech: "文本转语音（team-agent voice-service）" }[group]}</h3>
    ${fields.map(([key, widget, label, options]) => widget === "bool"
      ? `<div class="field"><label>${label}
           <input type="checkbox" data-mgroup="${group}" data-mkey="${key}"
             ${cfg[group]?.[key] ? "checked" : ""} style="width:auto"></label></div>`
      : widget === "select"
        ? `<div class="field"><label>${label}</label>
           <select data-mgroup="${group}" data-mkey="${key}">
             ${(options || ["http", "hf_gradio"]).map(o =>
               `<option value="${esc(o)}" ${cfg[group]?.[key] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
           </select></div>`
        : `<div class="field"><label>${label}</label>
             <input data-mgroup="${group}" data-mkey="${key}" value="${esc(cfg[group]?.[key] ?? "")}"></div>`
    ).join("")}`).join("");
  openModal(`
    <h2>模型设置</h2>
    <div class="palette-note">图片和视频模型未启用时自动生成占位素材。
      team-agent 文本转语音用于旁白；未启用或调用失败时，配音节点会明确失败。</div>
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

/* ================= 视图切换：智能体为核，画布为编排工具 ================= */
function switchView(mode) {
  if (mode === "flows") mode = state.graph ? "flows-editor" : "flows-list";
  state.viewMode = mode;
  const inFlows = mode === "flows-list" || mode === "flows-editor";
  $("#nav-agents").classList.toggle("on", mode === "agents");
  $("#nav-flows").classList.toggle("on", inFlows);
  $("#agents-view").classList.toggle("view-hidden", mode !== "agents");
  $("#flows-view").classList.toggle("view-hidden", mode !== "flows-list");
  $("#topbar").classList.toggle("view-hidden", mode !== "flows-editor");
  $("#main").classList.toggle("view-hidden", mode !== "flows-editor");
  if (mode === "agents") renderAgentsHome();
  else if (mode === "flows-list") renderFlowsHome();
  else requestAnimationFrame(() => {   // 画布从隐藏变为可见后重新适配视口
    updateVisibility(); fitView(); drawMinimap();
  });
}
$("#nav-agents").onclick = () => switchView("agents");
function returnToFlowList() {
  if (state.viewMode === "flows-editor" && state.dirty
      && !confirm("当前流程有未保存修改，确认返回工作流列表？")) return;
  state.graph = null; state.sel = null; state.dirty = false;
  renderFlowGovernance();
  switchView("flows-list");
}
$("#nav-flows").onclick = returnToFlowList;

async function openFlowEditor(id) {
  try {
    await loadFlow(id);
    switchView("flows-editor");
  } catch (error) { showApiError(error, "打开流程失败"); }
}

function renderFlowsHome() {
  const grid = $("#flows-grid");
  if (!grid) return;
  if (!state.flows.length) {
    grid.innerHTML = '<div class="res-empty">还没有工作流。点击右上「新增工作流」创建第一条流程。</div>';
    return;
  }
  grid.innerHTML = state.flows.map(flow => {
    const gov = flow._governance || {};
    const agentCount = (flow.ai_agent_ids || []).length;
    return `<article class="flow-card" data-fid="${esc(flow.id)}" tabindex="0">
      <div class="flow-head">
        <span class="flow-ico">◆</span>
        <div class="flow-title"><b>${esc(flow.name)}</b>
          <span class="flow-id">${esc(flow.id)} · v${gov.version || "-"}</span></div>
        <span class="status-badge" data-status="${esc(gov.status || "published")}">${esc(gov.status || "published")}</span>
      </div>
      <div class="flow-meta">
        <span class="chip">${Number(flow.node_count || 0)} 个节点</span>
        <span class="chip">${agentCount} 个智能体</span>
        <span class="chip">${(flow.triggers || []).length} 条触发话术</span>
      </div>
      <div class="flow-actions">
        <button class="primary" data-act="edit">编辑画布</button>
        ${can("resource.write") && gov.status === "draft" ? '<button data-act="submit">提交审批</button>' : ""}
        <button data-act="versions">版本</button>
        ${can("resource.write") ? '<button data-act="del" class="danger">删除</button>' : ""}
      </div>
    </article>`;
  }).join("");
  $$("#flows-grid .flow-card").forEach(card => {
    const id = card.dataset.fid;
    const flow = state.flows.find(item => item.id === id);
    const edit = () => openFlowEditor(id);
    card.querySelector('[data-act="edit"]').onclick = edit;
    card.onclick = event => { if (!event.target.closest("button")) edit(); };
    card.onkeydown = event => {
      if ((event.key === "Enter" || event.key === " ") && !event.target.closest("button")) {
        event.preventDefault(); edit();
      }
    };
    card.querySelector('[data-act="submit"]')?.addEventListener("click", () =>
      submitResource("flow", id, flow?._governance?.version));
    card.querySelector('[data-act="versions"]').onclick = () => openVersions("flow", id);
    card.querySelector('[data-act="del"]')?.addEventListener("click", async () => {
      if (!confirm(`确认删除流程「${id}」？`)) return;
      try {
        await api(`/api/flows/${encodeURIComponent(id)}`, { method: "DELETE" });
        if (state.graph?.id === id) state.graph = null;
        await loadFlowList();
        toast("已生成删除草稿", "ok");
      } catch (error) { showApiError(error, "删除失败"); }
    });
  });
}

function agentOrchestrationMode(agent) {
  return agent?.orchestration?.mode
    || (agent?.runtime === "customer-agent" ? "external_agent"
      : agent?.flow_id ? "flow" : "react");
}

function agentModeChip(agent) {
  const mode = agentOrchestrationMode(agent);
  if (mode === "external_agent") {
    const provider = agent?.orchestration?.provider === "customer-agent"
      || agent?.runtime === "customer-agent" ? "Customer Agent" : "第三方智能体";
    const model = agent?.orchestration?.selection?.model_id || agent?.profile_id || "";
    return `↗ ${provider}${model ? ` · ${esc(model)}` : ""}`;
  }
  if (mode === "flow") {
    return `◆ 流程 · ${esc(agent?.orchestration?.flow_id || agent?.flow_id || "未选择")}`;
  }
  return "◇ 内置 ReAct";
}

function agentPendingDelete(agent) {
  const pending = agent?._governance?.pending_delete;
  return pending?.action === "delete" ? pending : null;
}

function deleteStatusLabel(status) {
  return ({ draft: "删除草稿", pending: "删除待审批", approved: "删除待发布" })[status]
    || "删除处理中";
}

function renderAgentsHome() {
  const grid = $("#agents-grid");
  if (!grid) return;
  if (!state.aiAgents.length) {
    grid.innerHTML = '<div class="res-empty">还没有智能体。点右上「创建智能体」，配对知识库 / 技能 / 工具后即可对话。</div>';
    return;
  }
  grid.innerHTML = state.aiAgents.map(a => {
    const pendingDelete = agentPendingDelete(a);
    const displayGov = pendingDelete || a._governance || {};
    return `<div class="agent-card" data-aid="${esc(a.id)}">
      <div class="agent-head">
        <div class="agent-title"><b>${esc(a.name)}</b></div>
        <span class="status-badge" data-status="${esc(displayGov.status || "published")}">${pendingDelete
          ? esc(deleteStatusLabel(pendingDelete.status)) : esc(displayGov.status || "published")}</span>
      </div>
      <p class="agent-desc">${esc(a.description || "")}</p>
      <div class="agent-actions">
        <button class="primary" data-act="chat">▶ 对话</button>
        ${can("resource.write") ? '<button data-act="edit">编辑</button>' : ""}
        ${can("resource.write") && pendingDelete?.status === "draft"
          ? '<button data-act="submit-delete">提交删除</button>'
          : can("resource.write") && a._governance?.status === "draft" ? '<button data-act="submit">提交审批</button>' : ""}
        <button data-act="versions">版本</button>
        ${can("resource.write") && !pendingDelete ? '<button data-act="del" class="danger">删除</button>' : ""}
      </div>
    </div>`;
  }).join("");
  $$("#agents-grid .agent-card").forEach(card => {
    const id = card.dataset.aid;
    const agent = state.aiAgents.find(a => a.id === id);
    card.querySelector('[data-act="chat"]').onclick = () => openAgentChat(id);
    card.querySelector('[data-act="edit"]')?.addEventListener("click", () =>
      agentEditModal(agent));
    card.querySelector('[data-act="submit"]')?.addEventListener("click", () =>
      submitResource("agent", id, agent?._governance?.version));
    card.querySelector('[data-act="submit-delete"]')?.addEventListener("click", () =>
      submitResource("agent", id, agentPendingDelete(agent)?.version));
    card.querySelector('[data-act="versions"]').onclick = () => openVersions("agent", id);
    card.querySelector('[data-act="del"]')?.addEventListener("click", async () => {
      if (!confirm(`确认删除智能体「${id}」？`)) return;
      try {
        const deleted = await api(`/api/ai-agents/${encodeURIComponent(id)}`, { method: "DELETE" });
        await loadResources();
        openVersions("agent", id);
        toast(`删除草稿 v${deleted._governance?.version || "-"} 已创建，请提交审批`, "ok");
      } catch (error) { showApiError(error, "删除失败"); }
    });
  });
}
$("#agents-new").onclick = () => agentEditModal(null);

/* ================= 智能体对话面板 ================= */
function openAgentChat(id) {
  const a = state.aiAgents.find(x => x.id === id);
  if (!a) return;
  state.chatTarget = id;
  $("#chatp-name").textContent = a.name;
  const mode = $("#chatp-mode");
  mode.textContent = agentModeChip(a).replace(/^[↗◆◇]\s*/, "");
  mode.classList.add("on");
  $("#chat-panel").classList.add("open");
  renderChat();
  $("#chatp-input").focus();
}
function renderChat() {
  const box = $("#chatp-msgs");
  const log = state.chatLog[state.chatTarget] || [];
  box.innerHTML = log.map(m => m.role === "user"
    ? `<div class="msg user">${esc(m.text)}</div>`
    : `<div class="msg bot">${esc(m.text) || (m.error ? "⚠️ " + m.error : "…")}
        ${m.steps && m.steps.length ? `<details class="msg-steps"><summary>执行步骤 ${m.steps.length}</summary>
          <div class="res-snippet">${esc(m.steps.map(s =>
            `· [${s.type}] ${s.name} → ${s.result || ""}`).join("\n"))}</div></details>` : ""}</div>`)
    .join("") || '<div class="empty-tip">开始对话吧。回复会附带工具 / RAG 轨迹。</div>';
  box.scrollTop = box.scrollHeight;
}
async function sendChat() {
  const input = $("#chatp-input");
  const text = input.value.trim();
  if (!text || !state.chatTarget) return;
  input.value = "";
  const id = state.chatTarget;
  (state.chatLog[id] = state.chatLog[id] || []).push({ role: "user", text });
  renderChat();
  const sending = state.chatLog[id];
  sending.push({ role: "bot", text: "", error: null });
  renderChat();
  try {
    const out = await api(`/api/ai-agents/${encodeURIComponent(id)}/invoke`,
      { method: "POST", body: { message: text, session_id: state.chatSessions[id] } });
    const mine = sending[sending.length - 1];
    mine.text = out.text || "";
    mine.steps = out.steps || [];
    mine.error = out.error || null;
    if (out.session_id) state.chatSessions[id] = out.session_id;
  } catch (e) {
    sending[sending.length - 1].error = e.message;
  }
  renderChat();
}
$("#chatp-close").onclick = () => $("#chat-panel").classList.remove("open");
$("#chatp-new").onclick = () => {
  state.chatSessions[state.chatTarget] = null;
  state.chatLog[state.chatTarget] = [];
  renderChat();
};
$("#chatp-send").onclick = sendChat;
$("#chatp-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

/* ================= 资源库面板（智能体 / 知识库 / 技能 / MCP / 记忆） ================= */
const RES_TABS = [["kb", "知识库"], ["skills", "技能"],
                  ["mcp", "MCP"], ["memory", "记忆"], ["evals", "评测"]];
async function ensureMcpTools(server) {
  if (!server || state.mcpTools[server]) return;
    try { state.mcpTools[server] = await api(`/api/mcp/${encodeURIComponent(server)}/tools`); }
  catch (e) { state.mcpTools[server] = []; toast(`MCP ${server} 工具列表失败：${e.message}`, "err"); }
}
function openResPanel(tab) {
  if (tab) state.resTab = tab;
  if (state.resTab === "agents") { switchView("agents"); return; }
  $("#palette").classList.remove("open");
  $("#inspector").classList.remove("open");
  $("#media-panel").classList.remove("open");
  $("#res-panel").classList.add("open");
  renderResPanel();
}
$("#btn-res").onclick = () => openResPanel();
$("#res-close").onclick = () => $("#res-panel").classList.remove("open");

async function loadResources() {
  try {
    [state.aiAgents, state.kbs, state.skillList, state.mcpCfg, state.toolList] =
      await Promise.all([api("/api/ai-agents"), api("/api/kb"), api("/api/skills"),
                         api("/api/mcp"), api("/api/tools")]);
    renderResPanel();
    if (state.viewMode === "agents") renderAgentsHome();
  } catch { /* 服务不可用时静默，面板打开会重试 */ }
}

/* ---- 评测中心 ---- */
async function renderResEvals(body) {
  if (state.evalView) {
    if (state.evalView.mode === "run")
      return evalRunView(body, state.evalView.run_id);
    if (state.evalView.mode === "compare")
      return evalCompareView(body, state.evalView.suite_id);
  }
  let suites = [], runs = [];
  try {
    [suites, runs] = await Promise.all([api("/api/evals/suites"), api("/api/evals/runs")]);
  } catch (e) { body.innerHTML = `<div class="res-empty">${esc(e.message)}</div>`; return; }
  body.innerHTML = `
    <div class="res-actions"><button id="ev-suite-new" class="primary">＋ 新建评测集</button>
      <span class="res-desc">标准问题 + 期望 → 跑平台智能体与 codex 等对比</span></div>
    ${suites.map(s => `
      <div class="res-card" data-suite="${esc(s.id)}">
        <div class="res-title">📋 ${esc(s.name)} <span class="res-desc">${esc(s.id)}</span></div>
        <div class="res-desc">${esc(s.description || "")} · ${s.cases} 个用例</div>
        <div class="res-actions">
          <button data-act="run" class="primary">▶ 运行评测</button>
          <button data-act="cases">用例管理</button>
          <button data-act="del" class="danger">删除</button></div>
      </div>`).join("") || '<div class="res-empty">暂无评测集。</div>'}
    <h3 style="font-size:13.5px;margin:6px 0 0">最近运行</h3>
    ${runs.map(r => `
      <div class="res-card" data-run="${esc(r.run_id)}" data-suiteid="${esc(r.suite_id || "")}">
        <div class="res-title">🧪 ${esc(r.suite_id || "")} · ${esc(r.run_id)}
          <span class="res-desc">${esc(r.started_at || "")}</span></div>
        <div class="res-tags">${(r.targets || []).map(t => {
          const ok = t.passed === t.total && t.total > 0;
          return `<span class="chip" style="${ok ? "background:var(--ok);color:#fff" : ""}">
            ${esc(t.name)} ${t.passed}/${t.total}</span>`;
        }).join("")}</div>
        <div class="res-actions"><button data-act="detail">详情</button>
          <button data-act="compare">对比</button></div>
      </div>`).join("") || '<div class="res-empty">还没有运行记录。</div>'}`;
  $("#ev-suite-new").onclick = () => evalSuiteModal(null);
  $$("#res-body .res-card[data-suite]").forEach(card => {
    const id = card.dataset.suite;
    card.querySelector('[data-act="run"]').onclick = () => evalRunModal(id);
    card.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`确认删除评测集「${id}」？`)) return;
      await api(`/api/evals/suites/${encodeURIComponent(id)}`, { method: "DELETE" });
      renderResEvals(body);
    };
    card.querySelector('[data-act="cases"]').onclick = async () =>
      evalSuiteModal(await api(`/api/evals/suites/${encodeURIComponent(id)}`));
  });
  $$("#res-body .res-card[data-run]").forEach(card => {
    const id = card.dataset.run;
    card.querySelector('[data-act="detail"]').onclick = () => {
      state.evalView = { mode: "run", run_id: id }; renderResPanel();
    };
    card.querySelector('[data-act="compare"]').onclick = () => {
      state.evalView = { mode: "compare", suite_id: card.dataset.suiteid };
      renderResPanel();
    };
  });
}

function evalRunModal(suiteId) {
  const cliPresets = [
    { key: "codex", name: "Codex CLI", type: "cli", command: "codex",
      args: ["exec", "--skip-git-repo-check", "{prompt}"] },
    { key: "claude", name: "Claude CLI", type: "cli", command: "claude",
      args: ["-p", "{prompt}"] },
  ];
  const agents = state.aiAgents.map(a => ({
    key: `platform:${a.id}`, name: `平台:${a.name}`, type: "platform", id: a.id }));
  const all = [...agents, ...cliPresets];
  openModal(`
    <h2>运行评测 · ${esc(suiteId)}</h2>
    <div class="field"><label>选择对比目标（可多选）</label>
      ${all.map((t, i) => `<label class="res-kv">
        <input type="checkbox" data-tgt="${i}" checked style="width:auto">
        ${esc(t.name)}</label>`).join("")}</div>
    <div class="field"><label>自定义目标（可选，JSON：type=cli 填 command/args，{prompt} 占位；type=http 填 url）</label>
      <textarea id="ev-custom" rows="3" placeholder='{"key": "my", "type": "cli", "name": "我的agent", "command": "myagent", "args": ["--q", "{prompt}"]}'></textarea></div>
    <div class="palette-note">CLI 目标每题最长等 5 分钟，外部 agent 无步骤轨迹（这正是对比点之一）。</div>
    <div class="btn-row"><button id="ev-c">取消</button>
      <button id="ev-o" class="primary">▶ 开始评测</button></div>`);
  $("#ev-c").onclick = closeModal;
  $("#ev-o").onclick = async () => {
    let targets = all.filter((_, i) =>
      $(`#modal [data-tgt="${i}"]`).checked);
    const custom = $("#ev-custom").value.trim();
    if (custom) {
      try { targets = targets.concat([JSON.parse(custom)]); }
      catch { return toast("自定义目标不是合法 JSON", "err"); }
    }
    if (!targets.length) return toast("至少选择一个目标", "err");
    closeModal(); openResPanel("evals");
    $("#res-body").innerHTML = '<div class="res-empty">评测运行中…外部 CLI agent 较慢，请耐心等待。</div>';
    try {
      const run = await api(`/api/evals/suites/${encodeURIComponent(suiteId)}/run`,
        { method: "POST", body: { targets }, timeout: 600000 });
      state.evalView = { mode: "run", run_id: run.run_id };
      renderResPanel();
      toast("评测完成 ✅", "ok");
    } catch (e) {
      toast(`评测失败：${e.message}`, "err");
      renderResPanel();
    }
  };
}

function evalSuiteModal(suite) {
  const isNew = !suite;
  const cases = (suite?.cases || []).map(c => ({ ...c }));
  const rowsHtml = () => cases.map((c, i) => `
    <div class="res-card" data-ci="${i}">
      <input data-ck="question" value="${esc(c.question || "")}" placeholder="标准问题" style="width:100%">
      <input data-ck="contains" value="${esc(((c.expect || {}).contains || []).join(", "))}"
        placeholder="必须包含的关键词（逗号分隔）" style="width:100%">
      <input data-ck="not_contains" value="${esc(((c.expect || {}).not_contains || []).join(", "))}"
        placeholder="不得包含（逗号分隔，可空）" style="width:100%">
      <input data-ck="regex" value="${esc((c.expect || {}).regex || "")}" placeholder="正则（可空）" style="width:100%">
      <div style="display:flex;gap:6px;align-items:center">
        <input data-ck="note" value="${esc(c.note || "")}" placeholder="备注" style="flex:1">
        <button data-cdel="${i}" class="danger" style="padding:3px 8px">删</button></div>
    </div>`).join("");
  const renderRows = () => {
    $("#ev-cases").innerHTML = rowsHtml();
    $$("#ev-cases [data-ck]").forEach(el => el.oninput = () => {
      const c = cases[+el.closest("[data-ci]").dataset.ci];
      const k = el.dataset.ck;
      if (k === "contains" || k === "not_contains")
        c.expect = c.expect || {},
        c.expect[k] = el.value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
      else if (k === "regex") (c.expect = c.expect || {}).regex = el.value || undefined;
      else c[k] = el.value;
    });
    $$("#ev-cases [data-cdel]").forEach(btn => btn.onclick = () => {
      cases.splice(+btn.dataset.cdel, 1); renderRows();
    });
  };
  openModal(`
    <h2>${isNew ? "新建评测集" : `编辑评测集 · ${esc(suite.id)}`}</h2>
    <div class="field"><label>名称</label><input id="ev-name" value="${esc(suite?.name || "")}"></div>
    <div class="field"><label>描述</label><input id="ev-desc" value="${esc(suite?.description || "")}"></div>
    <div class="field"><label>用例（标准问题 + 期望）</label>
      <div id="ev-cases" style="display:flex;flex-direction:column;gap:8px;max-height:340px;overflow-y:auto">${rowsHtml()}</div>
      <button id="ev-case-add" type="button" style="margin-top:6px">＋ 添加用例</button></div>
    <div class="btn-row"><button id="ev-c">取消</button><button id="ev-o" class="primary">保存</button></div>`);
  renderRows();
  $("#ev-case-add").onclick = () => {
    cases.push({ question: "", expect: {}, note: "" }); renderRows();
  };
  $("#ev-c").onclick = closeModal;
  $("#ev-o").onclick = async () => {
    const body = {
      id: isNew ? ($("#ev-name").value.trim().toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || `suite-${Date.now() % 10000}`)
        : suite.id,
      name: $("#ev-name").value.trim(), description: $("#ev-desc").value.trim(),
      cases: cases.filter(c => c.question && c.question.trim()) };
    if (!body.cases.length) return toast("至少一个用例", "err");
    try {
      await api(isNew ? "/api/evals/suites" : `/api/evals/suites/${encodeURIComponent(suite.id)}`,
        { method: isNew ? "POST" : "PUT", body });
      closeModal(); toast("评测集已保存 ✅", "ok"); renderResPanel();
    } catch (e) { toast(`保存失败：${e.message}`, "err"); }
  };
}

async function evalRunView(body, runId) {
  body.innerHTML = '<div class="res-empty">加载中…</div>';
  let run;
  try { run = await api(`/api/evals/runs/${encodeURIComponent(runId)}`); }
  catch (e) { body.innerHTML = `<div class="res-empty">${esc(e.message)}</div>`; return; }
  const byTarget = {};
  for (const r of run.results || []) (byTarget[r.target_key] = byTarget[r.target_key] || []).push(r);
  const stepsText = r => (r.steps || []).map(s =>
    `· [${s.type}] ${s.name} ${JSON.stringify(s.args || {}).slice(0, 90)} → ${s.result || ""}`).join("\n");
  body.innerHTML = `
    <div class="res-actions"><button id="ev-back">← 返回</button>
      <button id="ev-cmp">与另一次运行对比</button></div>
    ${(Object.entries(byTarget)).map(([key, rows]) => {
      const passed = rows.filter(r => r.pass).length;
      const caseRows = rows.map(r => `
          <div class="res-doc-row">
            <span style="width:16px">${r.pass ? "✅" : r.error ? "🚫" : "❌"}</span>
            <span class="doc-name" title="${esc(r.question)}">${esc(r.question)}</span>
            <span class="doc-meta">${r.ms}ms</span></div>
          ${r.error ? `<div class="res-desc" style="color:var(--err)">错误：${esc(r.error)}</div>` : ""}
          ${!r.pass && !r.error ? `<div class="res-desc" style="color:var(--err)">未通过：${esc((r.checks || []).filter(c => !c.ok).map(c => c.name + (c.detail ? `（${c.detail}）` : "")).join("；"))}</div>` : ""}
          <div class="res-snippet">答：${esc((r.answer || "").slice(0, 300)) || "（空）"}</div>
          ${(r.steps || []).length ? `<details><summary class="res-desc" style="cursor:pointer">执行步骤（${r.steps.length}）</summary>
            <div class="res-snippet">${esc(stepsText(r))}</div></details>` : ""}`).join("");
      return `
      <div class="res-card">
        <div class="res-title">${passed === rows.length ? "✅" : "⚠️"} ${esc(key)}
          <span class="res-desc">${passed}/${rows.length} 通过 · 平均 ${Math.round(rows.reduce((s, r) => s + r.ms, 0) / rows.length)}ms</span></div>
        ${caseRows}
      </div>`;
    }).join("")}`;
  $("#ev-back").onclick = () => { state.evalView = null; renderResPanel(); };
  $("#ev-cmp").onclick = () => { state.evalView = { mode: "compare", suite_id: run.suite_id }; evalCompareView(body, run.suite_id, runId); };
}

async function evalCompareView(body, suiteId, presetRunId) {
  body.innerHTML = '<div class="res-empty">加载中…</div>';
  let runs = [];
  try { runs = await api(`/api/evals/runs?suite_id=${encodeURIComponent(suiteId || "")}`); }
  catch (e) { body.innerHTML = `<div class="res-empty">${esc(e.message)}</div>`; return; }
  const ids = runs.map(r => r.run_id);
  const a = presetRunId && ids.includes(presetRunId) ? presetRunId : ids[0];
  const b = ids.find(x => x !== a) || a;
  body.innerHTML = `
    <div class="res-actions"><button id="ev-back">← 返回</button>
      <select id="ev-ra">${ids.map(x => `<option ${x === a ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>
      <span class="res-desc">vs</span>
      <select id="ev-rb">${ids.map(x => `<option ${x === b ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>
      <button id="ev-go" class="primary">对比</button></div>
    <div id="ev-cmp-out"></div>`;
  $("#ev-back").onclick = () => { state.evalView = null; renderResPanel(); };
  const doCompare = async () => {
    const ra = $("#ev-ra").value, rb = $("#ev-rb").value;
    if (ra === rb) return toast("请选择两次不同的运行", "err");
    let cmp;
    try { cmp = await api(`/api/evals/compare?run_a=${encodeURIComponent(ra)}&run_b=${encodeURIComponent(rb)}`); }
    catch (e) { return toast(e.message, "err"); }
    $("#ev-cmp-out").innerHTML = `
      <div class="res-card"><div class="res-title">
        ${cmp.diff_count ? `⚠️ ${cmp.diff_count} 个用例结论不一致` : "✅ 两次运行结论一致"}</div></div>
      ${cmp.rows.map(row => `
        <div class="res-card" style="${row.diff ? "border-color:var(--err)" : ""}">
          <div class="res-title">${row.diff ? "⚡" : "·"} ${esc(row.question)}</div>
          ${row.cells.map(c => `
            <div class="res-doc-row">
              <span style="width:16px">${c.pass ? "✅" : "❌"}</span>
              <span class="doc-name" style="max-width:110px" title="${esc(c.target_key)}">${esc(c.target_key)}</span>
              <span class="doc-meta">${c.ms}ms</span></div>
            <div class="res-snippet">${esc(c.answer) || `（${esc(c.error || "无输出")}）`}</div>`).join("")}
        </div>`).join("")}`;
  };
  $("#ev-go").onclick = doCompare;
  doCompare();
}

function renderResPanel() {
  $("#res-tabs").innerHTML = RES_TABS.map(([k, label]) =>
    `<button class="chip ${state.resTab === k ? "on" : ""}" data-rtab="${k}">${label}</button>`).join("");
  $$("#res-tabs .chip").forEach(c => c.onclick = () => { state.resTab = c.dataset.rtab; renderResPanel(); });
  const body = $("#res-body");
  ({ agents: renderResAgents, kb: renderResKb, skills: renderResSkills,
     mcp: renderResMcp, memory: renderResMemory, evals: renderResEvals
  })[state.resTab](body);
  if (!can("resource.write")) requestAnimationFrame(() => {
    $$("#res-body button.danger, #res-body [data-act='edit'], #res-body [data-act='del'], " +
      "#res-body [data-del], #res-agent-new, #res-kb-new, #res-skill-new, #res-mcp-new, " +
      "#mem-add, #ev-suite-new, #ev-case-add").forEach(el => { el.hidden = true; });
  });
}

/* ---- 智能体 ---- */
function renderResAgents(body) {
  body.innerHTML = `
    <div class="res-actions"><button id="res-agent-new" class="primary">＋ 新建智能体</button></div>
    ${state.aiAgents.map(a => {
      const pendingDelete = agentPendingDelete(a);
      return `<div class="res-card" data-aid="${esc(a.id)}">
        <div class="res-title">✨ ${esc(a.name)} <span class="res-desc">${esc(a.id)}</span>
          <span class="spacer"></span>${pendingDelete
            ? `<span class="status-badge" data-status="${esc(pendingDelete.status)}">${esc(deleteStatusLabel(pendingDelete.status))}</span>` : ""}</div>
        <div class="res-desc">${esc(a.description || "")}</div>
        <div class="res-tags">${[
          agentModeChip(a),
          ...(a.kb_ids || []).map(x => `📚 ${esc(x)}`),
          ...(a.skill_ids || []).map(x => `🛠 ${esc(x)}`),
          ...(a.tool_ids || []).map(x => `🧰 ${esc(x)}`),
          ...(a.mcp_servers || []).map(x => `🔌 ${esc(x)}`),
          a.memory ? "💾 记忆" : ""].filter(Boolean).map(t =>
          `<span class="chip">${t}</span>`).join("")}</div>
        <div class="res-actions">
          <button data-act="edit">编辑</button>
          <button data-act="invoke">▶ 调试运行</button>
          ${pendingDelete?.status === "draft" ? '<button data-act="submit-delete">提交删除</button>' : ""}
          <button data-act="versions">版本</button>
          ${pendingDelete ? "" : '<button data-act="del" class="danger">删除</button>'}</div>
      </div>`;
    }).join("") || '<div class="res-empty">还没有智能体，点上方新建。</div>'}`;
  $("#res-agent-new").onclick = () => agentEditModal(null);
  $$("#res-body .res-card").forEach(card => {
    const id = card.dataset.aid;
    const agent = state.aiAgents.find(a => a.id === id);
    card.querySelector('[data-act="edit"]').onclick = () =>
      agentEditModal(agent);
    card.querySelector('[data-act="submit-delete"]')?.addEventListener("click", () =>
      submitResource("agent", id, agentPendingDelete(agent)?.version));
    card.querySelector('[data-act="versions"]').onclick = () => openVersions("agent", id);
    card.querySelector('[data-act="del"]')?.addEventListener("click", async () => {
      if (!confirm(`确认删除智能体「${id}」？`)) return;
      try {
        const deleted = await api(`/api/ai-agents/${encodeURIComponent(id)}`, { method: "DELETE" });
        await loadResources();
        openVersions("agent", id);
        toast(`删除草稿 v${deleted._governance?.version || "-"} 已创建，请提交审批`, "ok");
      } catch (error) { showApiError(error, "删除失败"); }
    });
    card.querySelector('[data-act="invoke"]').onclick = () => agentInvokeModal(id);
  });
}
function agentEditModal(agent, options = {}) {
  const isNew = !agent;
  const a = agent || { id: "", name: "", description: "", system: "你是一个得力的智能体。",
                       runtime: "local", profile_id: "aihub-deepseek",
                       flow_id: "", kb_ids: [], skill_ids: [], tool_ids: [],
                       mcp_servers: [], memory: true, max_steps: 8, rag_top_k: 4 };
  const orchestration = a.orchestration || {};
  let mode = orchestration.mode || (a.runtime === "customer-agent" ? "external_agent" : a.flow_id ? "flow" : "react");
  const connection = orchestration.connection || {};
  const selection = orchestration.selection || {};
  const credentialRef = connection.credential_ref || "customer-agent-default";
  let credentialConfigured = a.credential_configured === true;
  let catalog = null;
  let catalogMessage = "尚未拉取 CA 能力目录";
  const caSelected = {
    skills: new Set(selection.skill_ids || a.skill_ids || []),
    tools: new Set(selection.tool_ids || a.tool_ids || []),
    mcp: new Set(selection.mcp_server_ids || a.mcp_servers || []),
  };
  let caModel = selection.model_id || a.profile_id || "aihub-deepseek";
  let caToolPolicy = selection.tool_policy_id || "";
  const caMemoryInitial = selection.memory_enabled !== undefined
    ? selection.memory_enabled : a.memory;
  const checks = (items, sel, attr) => (items || []).map(it =>
    `<label class="res-kv"><input type="checkbox" data-${attr}="${esc(it.id || it.name || it)}"
      ${sel.includes(it.id || it.name || it) ? "checked" : ""} style="width:auto">
      ${esc(it.name || it.id || it)}</label>`).join("");
  openModal(`
    <h2>${isNew ? "创建智能体" : `编辑智能体 · ${esc(a.id)}`}</h2>
    <div class="ag-sec">身份</div>
    <div class="field"><label>名称</label><input id="ag-name" value="${esc(a.name)}"></div>
    ${isNew ? `<div class="field"><label>ID（字母数字-，留空自动生成）</label><input id="ag-id"></div>` : ""}
    <div class="field"><label>描述</label><input id="ag-desc" value="${esc(a.description)}"></div>
    <div class="field"><label>System 提示词（角色与行为边界）</label>
      <textarea id="ag-system" rows="5">${esc(a.system || "")}</textarea></div>

    <div class="ag-sec">编排方式</div>
    <div class="ag-mode-switch" role="group" aria-label="编排方式">
      <button type="button" data-ag-mode="react">ReAct</button>
      <button type="button" data-ag-mode="external_agent">第三方智能体</button>
      <button type="button" data-ag-mode="flow">流程图</button>
    </div>
    <div class="field" id="ag-flow-row" ${mode !== "flow" ? 'style="display:none"' : ""}>
      <label>绑定流程（消息作为 input.message 进入流程）</label>
      <select id="ag-flow"></select>
      <div class="hint" id="ag-flow-hint">流程里可用「智能体」节点继续组装。</div></div>
    <div id="ag-ca-row" ${mode !== "external_agent" ? 'style="display:none"' : ""}>
      <div class="field"><label>智能体提供方</label>
        <select id="ag-ca-provider" disabled><option value="customer-agent">Customer Agent</option></select></div>
      <div class="field"><label>CA 地址</label>
        <input id="ag-ca-url" value="${esc(connection.base_url || "http://127.0.0.1:3000")}" inputmode="url"></div>
      <div class="field"><label>访问令牌</label>
        <div class="ag-inline"><input id="ag-ca-token" type="password" autocomplete="new-password"
          placeholder="${credentialConfigured ? "已配置，留空保持不变" : "本机无鉴权可留空"}">
          <button type="button" id="ag-ca-clear-token" title="清除已保存令牌" ${credentialConfigured ? "" : "disabled"}>清除</button></div>
        <div class="hint" id="ag-ca-credential">${credentialConfigured ? "凭据已配置" : "未保存凭据"}</div></div>
      <div class="ag-inline ag-catalog-actions">
        <button type="button" id="ag-ca-refresh" title="从 Customer Agent 拉取能力目录">↻ 拉取配置</button>
        <span class="hint" id="ag-ca-status">${esc(catalogMessage)}</span></div>
      <div class="field"><label class="ag-toggle"><input id="ag-ca-include-identity" type="checkbox"
        ${orchestration.include_identity_instructions ? "checked" : ""}>
        将名称和描述加入 CA System 提示词</label>
        <div class="hint">仅影响本次请求的 instructions，不会在 CA 创建或绑定固定智能体。</div></div>
      <div id="ag-ca-catalog"></div>
    </div>

    <div id="ag-local-capabilities" ${mode !== "react" ? 'style="display:none"' : ""}>
    <div class="ag-sec">能力配对</div>
    <div class="field"><label>📚 知识库（自动 RAG 检索注入）</label>
      ${checks(state.kbs, a.kb_ids, "agkb") || '<span class="hint">暂无知识库</span>'}
      <div style="display:flex;align-items:center;gap:6px;margin-top:4px">
        <span class="hint">检索片段数</span>
        <input id="ag-ragk" type="number" value="${a.rag_top_k || 4}" style="width:70px"></div></div>
    <div class="field"><label>🛠 技能（指令包，注入提示词）</label>
      ${checks(state.skillList, a.skill_ids, "agskill") || '<span class="hint">暂无技能</span>'}</div>
    <div class="field"><label>🧰 工具</label>
      ${checks(state.toolList, a.tool_ids, "agtool") || '<span class="hint">暂无工具</span>'}</div>
    <div class="field"><label>🔌 MCP 服务器</label>
      ${checks(Object.keys(state.mcpCfg.servers || {}), a.mcp_servers, "agmcp") || '<span class="hint">未配置 MCP</span>'}</div>

    <div class="ag-sec">记忆与执行</div>
    <div class="field"><label>长期记忆
      <input id="ag-memory" type="checkbox" ${a.memory ? "checked" : ""} style="width:auto"></label>
      <div class="hint">按 agent / 会话作用域自动读写最近对话</div></div>
    <div class="field"><label>工具循环最大步数（对话式）</label>
      <input id="ag-steps" type="number" value="${a.max_steps || 8}"></div></div>
    <div class="btn-row"><button id="ag-cancel">取消</button>
      <button id="ag-ok" class="primary">保存</button></div>`);
  $("#modal").classList.add("agent-editor");
  $("#ag-cancel").onclick = closeModal;
  const renderCACatalog = () => {
    const host = $("#ag-ca-catalog");
    if (!catalog) {
      host.innerHTML = '<div class="ag-catalog-empty">拉取后选择模型与能力</div>';
      return;
    }
    const capabilityGroup = (key, label, items, selected) => {
      const known = new Set(items.map(item => item.id));
      const merged = [...items, ...[...selected].filter(id => !known.has(id)).map(id => ({ id, name: id, stale: true }))];
      return `<div class="ag-cap-group" data-cap-group="${key}">
        <div class="ag-cap-head"><b>${label}</b><input data-ca-filter="${key}" placeholder="筛选"></div>
        <div class="ag-cap-list">${merged.map(item => `<label class="ag-cap-option" data-search="${esc(`${item.name || item.id} ${item.description || ""}`.toLowerCase())}">
          <input type="checkbox" data-ca-cap="${key}" data-ca-id="${esc(item.id)}" ${selected.has(item.id) ? "checked" : ""}>
          <span><b title="${esc(item.name || item.id)}">${esc(item.name || item.id)}</b>${item.stale ? '<em>已失效</em>' : ""}
          ${item.description ? `<small>${esc(item.description)}</small>` : ""}</span></label>`).join("") || '<div class="hint">没有可选项</div>'}</div></div>`;
    };
    const models = catalog.models || [];
    const modelKnown = models.some(item => item.id === caModel);
    const toolPolicies = catalog.toolPolicies || [];
    const policyKnown = !caToolPolicy || toolPolicies.some(item => item.id === caToolPolicy);
    host.innerHTML = `<div class="field"><label>模型</label><select id="ag-ca-model">
      ${!modelKnown && caModel ? `<option value="${esc(caModel)}">${esc(caModel)}（已失效）</option>` : ""}
      ${models.map(item => `<option value="${esc(item.id)}" ${item.id === caModel ? "selected" : ""}>${esc(item.name || item.id)} · ${esc(item.provider || "")}</option>`).join("")}
      </select></div>
      <div class="field"><label>工具执行策略</label><select id="ag-ca-tool-policy">
        <option value="">（不使用策略）</option>
        ${!policyKnown ? `<option value="${esc(caToolPolicy)}" selected>${esc(caToolPolicy)}（已失效）</option>` : ""}
        ${toolPolicies.map(item => `<option value="${esc(item.id)}" ${item.id === caToolPolicy ? "selected" : ""}>${esc(item.name || item.id)}</option>`).join("")}
      </select><div class="hint">策略由 Customer Agent 管理；Flow 只保存策略 ID。</div></div>
      ${capabilityGroup("skills", "Skills", catalog.skills || [], caSelected.skills)}
      ${capabilityGroup("tools", "Tools", catalog.tools || [], caSelected.tools)}
      ${capabilityGroup("mcp", "MCP", catalog.mcpServers || [], caSelected.mcp)}
      <div class="field"><label class="ag-toggle"><input id="ag-ca-memory" type="checkbox"
        ${caMemoryInitial ? "checked" : ""}
        ${catalog.features?.memory ? "" : "disabled"}> <span>记忆</span></label></div>`;
    $("#ag-ca-model").onchange = e => { caModel = e.target.value; };
    $("#ag-ca-tool-policy").onchange = e => { caToolPolicy = e.target.value; };
    $$('[data-ca-cap]', host).forEach(input => input.onchange = () => {
      const set = caSelected[input.dataset.caCap];
      input.checked ? set.add(input.dataset.caId) : set.delete(input.dataset.caId);
    });
    $$('[data-ca-filter]', host).forEach(input => input.oninput = () => {
      const group = $(`[data-cap-group="${input.dataset.caFilter}"]`, host);
      const query = input.value.trim().toLowerCase();
      $$(".ag-cap-option", group).forEach(row => {
        row.hidden = query && !row.dataset.search.includes(query);
      });
    });
  };
  renderCACatalog();
  const renderAgentFlowOptions = () => {
    const agentId = isNew ? ($("#ag-id")?.value.trim() || "") : a.id;
    const selected = $("#ag-flow").value || orchestration.flow_id || a.flow_id || "";
    const flowOptions = (state.flows || []).map(flow => ({
      flow,
      reason: dependencyCycleReason("agent", agentId, "flow", flow.id)
        || (isNew ? plannedAgentCycleReason(options.referencingFlowId, flow.id) : ""),
    }));
    $("#ag-flow").innerHTML = flowOptions.length
      ? `<option value="">（请选择流程）</option>${flowOptions.map(({ flow, reason }) =>
      `<option value="${esc(flow.id)}" ${flow.id === selected ? "selected" : ""}
        ${reason ? `disabled title="${esc(reason)}"` : ""}>
        ${esc(flow.name)}（${esc(flow.id)}）${reason ? " · 会形成循环" : ""}</option>`).join("")}`
      : '<option value="">（暂无流程）</option>';
    const blocked = flowOptions.filter(option => option.reason);
    $("#ag-flow-hint").textContent = blocked.length
      ? "会通过流程回到当前智能体的选项已禁用，避免直接或间接循环。"
      : "流程里可用「智能体」节点继续组装。";
  };
  renderAgentFlowOptions();
  $("#ag-id")?.addEventListener("input", renderAgentFlowOptions);
  const setMode = selected => {
    mode = selected;
    $$('[data-ag-mode]').forEach(button => {
      button.classList.toggle("on", button.dataset.agMode === mode);
      button.setAttribute("aria-pressed", button.dataset.agMode === mode ? "true" : "false");
    });
    $("#ag-flow-row").style.display = mode === "flow" ? "" : "none";
    $("#ag-ca-row").style.display = mode === "external_agent" ? "" : "none";
    $("#ag-local-capabilities").style.display = mode === "react" ? "" : "none";
    if (mode === "flow") renderAgentFlowOptions();
  };
  $$('[data-ag-mode]').forEach(button => button.onclick = () => setMode(button.dataset.agMode));
  setMode(mode);
  $("#ag-ca-refresh").onclick = async () => {
    const button = $("#ag-ca-refresh");
    button.disabled = true; $("#ag-ca-status").textContent = "正在拉取…";
    try {
      catalog = await api("/api/ai-agents/external/catalog", { method: "POST", body: {
        provider: "customer-agent", base_url: $("#ag-ca-url").value.trim(),
        credential_ref: credentialRef, token: $("#ag-ca-token").value || undefined,
      }});
      $("#ag-ca-status").textContent = `已拉取 · ${catalog.models.length} 模型 / ${catalog.skills.length} Skills / ${catalog.tools.length} Tools / ${catalog.mcpServers.length} MCP / ${(catalog.toolPolicies || []).length} 策略`;
      if (!caModel && catalog.models[0]) caModel = catalog.models[0].id;
      renderCACatalog();
    } catch (error) {
      $("#ag-ca-status").textContent = `拉取失败：${error.message}`;
    } finally { button.disabled = false; }
  };
  $("#ag-ca-clear-token").onclick = async () => {
    try {
      await api(`/api/ai-agents/external/credentials/${encodeURIComponent(credentialRef)}`,
        { method: "PUT", body: { clear: true } });
      credentialConfigured = false;
      $("#ag-ca-credential").textContent = "未保存凭据";
      $("#ag-ca-clear-token").disabled = true;
      $("#ag-ca-token").placeholder = "本机无鉴权可留空";
    } catch (error) { toast(`清除凭据失败：${error.message}`, "err"); }
  };
  $("#ag-ok").onclick = async () => {
    if (mode === "external_agent" && !caModel)
      return toast("请选择 CA 模型", "err");
    if (mode === "external_agent" && catalog) {
      const valid = (items, selected) => [...selected].every(id => items.some(item => item.id === id));
      if (!catalog.models.some(item => item.id === caModel)
          || !valid(catalog.skills || [], caSelected.skills)
          || !valid(catalog.tools || [], caSelected.tools)
          || !valid(catalog.mcpServers || [], caSelected.mcp)
          || (caToolPolicy && !(catalog.toolPolicies || []).some(item => item.id === caToolPolicy)))
        return toast("存在已失效的 CA 能力，请移除或重新选择", "err");
    }
    const localSkills = [...$$("#modal [data-agskill]")].filter(c => c.checked).map(c => c.dataset.agskill);
    const localTools = [...$$("#modal [data-agtool]")].filter(c => c.checked).map(c => c.dataset.agtool);
    const localMcp = [...$$("#modal [data-agmcp]")].filter(c => c.checked).map(c => c.dataset.agmcp);
    const caMemory = $("#ag-ca-memory")?.checked === true;
    const caIncludeIdentity = $("#ag-ca-include-identity")?.checked === true;
    const body = {
      id: isNew ? ($("#ag-id").value.trim() || ($("#ag-name").value.trim()
        .toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || `agent-${Date.now() % 10000}`))
        : a.id,
      name: $("#ag-name").value.trim(), description: $("#ag-desc").value.trim(),
      system: $("#ag-system").value,
      orchestration: mode === "external_agent" ? {
        mode, provider: "customer-agent", agent_id: orchestration.agent_id || "",
        include_identity_instructions: caIncludeIdentity,
        connection: { base_url: $("#ag-ca-url").value.trim(), credential_ref: credentialRef },
        selection: { model_id: caModel, skill_ids: [...caSelected.skills],
          tool_ids: [...caSelected.tools], mcp_server_ids: [...caSelected.mcp],
          memory_enabled: caMemory, tool_policy_id: caToolPolicy },
      } : mode === "flow" ? { mode, flow_id: $("#ag-flow").value || "" } : { mode },
      runtime: mode === "external_agent" ? "customer-agent" : "local",
      profile_id: mode === "external_agent" ? caModel : "aihub-deepseek",
      flow_id: mode === "flow" ? ($("#ag-flow").value || "") : "",
      kb_ids: mode === "react" ? [...$$("#modal [data-agkb]")].filter(c => c.checked).map(c => c.dataset.agkb) : [],
      skill_ids: mode === "external_agent" ? [...caSelected.skills] : mode === "react" ? localSkills : [],
      tool_ids: mode === "external_agent" ? [...caSelected.tools] : mode === "react" ? localTools : [],
      mcp_servers: mode === "external_agent" ? [...caSelected.mcp] : mode === "react" ? localMcp : [],
      memory: mode === "external_agent" ? caMemory : mode === "react" ? $("#ag-memory").checked : false,
      max_steps: Number($("#ag-steps").value) || 8,
      rag_top_k: Number($("#ag-ragk").value) || 4,
    };
    if (body.flow_id && !(state.flows || []).some(f => f.id === body.flow_id))
      return toast("绑定的流程不存在", "err");
    try {
      const token = $("#ag-ca-token").value.trim();
      if (mode === "external_agent" && token) {
        await api(`/api/ai-agents/external/credentials/${encodeURIComponent(credentialRef)}`,
          { method: "PUT", body: { token } });
      }
      const saved = await api(isNew ? "/api/ai-agents" : `/api/ai-agents/${encodeURIComponent(a.id)}`,
                              { method: isNew ? "POST" : "PUT", body });
      closeModal(); await loadResources(); renderResPanel();
      options.onSaved?.(state.aiAgents.find(item => item.id === saved.id) || saved);
      toast("智能体已保存 ✅", "ok");
    } catch (e) { toast(`保存失败：${e.message}`, "err"); }
  };
}
function agentInvokeModal(agentId) {
  const agent = state.aiAgents.find(item => item.id === agentId);
  const external = (agent?.orchestration?.mode || (agent?.runtime === "customer-agent" ? "external_agent" : "react")) === "external_agent";
  openModal(`
    <h2>调试运行 · ${esc(agentId)}</h2>
    ${external ? `<div class="field"><label>Skill</label><select id="ag-irun-skill"><option value="">自动选择</option>
      ${(agent.skill_ids || []).map(skill => `<option value="${esc(skill)}">${esc(skill)}</option>`).join("")}
    </select></div>` : ""}
    <div class="field"><label>输入消息</label>
      <textarea id="ag-msg" rows="3" placeholder="问点什么…"></textarea></div>
    <div class="field"><label>会话 ID（留空=新会话）</label><input id="ag-session"></div>
    <div class="btn-row"><button id="ag-irun-cancel">取消</button>
      <button id="ag-irun" class="primary">▶ 运行</button></div>
    <div id="ag-irun-out" class="res-snippet" style="display:none"></div>`);
  $("#ag-irun-cancel").onclick = closeModal;
  $("#ag-irun").onclick = async () => {
    const msg = $("#ag-msg").value.trim();
    if (!msg) return toast("请输入消息", "err");
    $("#ag-irun").disabled = true;
    $("#ag-irun-out").style.display = "block";
    $("#ag-irun-out").textContent = "运行中…";
    try {
      const out = await api(`/api/ai-agents/${encodeURIComponent(agentId)}/invoke`,
        { method: "POST", body: { message: msg,
          skill_id: $("#ag-irun-skill")?.value || undefined,
          session_id: $("#ag-session").value.trim() || undefined } });
      $("#ag-irun-out").textContent =
        `【回复】${out.text || "（空）"}\n\n【工具调用 ${out.tool_calls || 0} 次】\n` +
        (out.steps || []).map(s => `· ${s.name}(${JSON.stringify(s.args).slice(0, 80)}) → ${s.result || ""}`).join("\n")
        + (out.error ? `\n【错误】${out.error}` : "");
    } catch (e) { $("#ag-irun-out").textContent = `失败：${e.message}`; }
    $("#ag-irun").disabled = false;
  };
}

/* ---- 知识库 ---- */
function renderResKb(body) {
  body.innerHTML = `
    <div class="res-actions"><button id="res-kb-new" class="primary">＋ 新建知识库</button></div>
    ${state.kbs.map(k => `
      <div class="res-card" data-kb="${esc(k.id)}">
        <div class="res-title">📚 ${esc(k.name)} <span class="res-desc">${esc(k.id)}</span></div>
        <div class="res-desc">${esc(k.description || "")} · ${k.docs} 文档 / ${k.chunks} 片段</div>
        <div class="res-actions">
          <button data-act="docs">文档管理</button>
          <button data-act="search">检索测试</button>
          <button data-act="del" class="danger">删除</button></div>
        <div data-kbdocs="${esc(k.id)}"></div>
      </div>`).join("") || '<div class="res-empty">还没有知识库，点上方新建。</div>'}`;
  $("#res-kb-new").onclick = () => {
    openModal(`
      <h2>新建知识库</h2>
      <div class="field"><label>名称</label><input id="kbn" placeholder="例：产品手册"></div>
      <div class="field"><label>ID（字母数字-，留空自动生成）</label><input id="kbi"></div>
      <div class="field"><label>描述</label><input id="kbd"></div>
      <div class="btn-row"><button id="kbc">取消</button><button id="kbo" class="primary">创建</button></div>`);
    $("#kbc").onclick = closeModal;
    $("#kbo").onclick = async () => {
      const name = $("#kbn").value.trim();
      if (!name) return toast("请填写名称", "err");
      const id = $("#kbi").value.trim() || name.toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || `kb-${Date.now() % 10000}`;
      try {
        await api("/api/kb", { method: "POST", body: { id, name, description: $("#kbd").value.trim() } });
        closeModal(); await loadResources(); toast("知识库已创建 ✅", "ok");
      } catch (e) { toast(`创建失败：${e.message}`, "err"); }
    };
  };
  $$("#res-body .res-card[data-kb]").forEach(card => {
    const id = card.dataset.kb;
    card.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`确认删除知识库「${id}」及其全部文档？`)) return;
      await api(`/api/kb/${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadResources(); toast("已删除", "ok");
    };
    card.querySelector('[data-act="docs"]').onclick = () => kbDocsToggle(id, card);
    card.querySelector('[data-act="search"]').onclick = () => kbSearchModal(id);
  });
}
async function kbDocsToggle(kbId, card) {
  const box = card.querySelector(`[data-kbdocs="${kbId}"]`);
  if (box.innerHTML) { box.innerHTML = ""; return; }
  let docs = state.kbDocs[kbId];
  if (!docs) { docs = state.kbDocs[kbId] = await api(`/api/kb/${encodeURIComponent(kbId)}/docs`); }
  box.innerHTML = `
    <div style="margin-top:6px;display:flex;flex-direction:column;gap:2px">
      ${docs.map(d => `
        <div class="res-doc-row">
          <span class="doc-name" title="${esc(d.name)}">${esc(d.name)}</span>
          <span class="doc-meta">${d.chunks} 块 · ${Math.max(1, Math.round(d.bytes / 1024))}KB</span>
          <button data-del="${esc(d.doc_id)}" title="删除文档" style="font-size:12px;padding:2px 8px">×</button>
        </div>`).join("") || '<span class="res-desc">暂无文档</span>'}
    </div>
    <div class="res-actions" style="margin-top:6px">
      <button data-act="add">＋ 粘贴文本入库</button>
      <button data-act="upload">⬆ 上传文本文件</button>
      <input type="file" hidden accept=".md,.txt,.json,.csv,.html" data-finput>
    </div>`;
  box.querySelectorAll("[data-del]").forEach(btn => btn.onclick = async () => {
    await api(`/api/kb/${encodeURIComponent(kbId)}/docs/${btn.dataset.del}`, { method: "DELETE" });
    delete state.kbDocs[kbId]; await loadResources(); toast("文档已删除", "ok");
  });
  box.querySelector('[data-act="add"]').onclick = () => {
    openModal(`
      <h2>文本入库 · ${esc(kbId)}</h2>
      <div class="field"><label>文档名</label><input id="kdoc-name"></div>
      <div class="field"><label>内容</label><textarea id="kdoc-text" rows="10"></textarea></div>
      <div class="btn-row"><button id="kdoc-c">取消</button><button id="kdoc-o" class="primary">入库</button></div>`);
    $("#kdoc-c").onclick = closeModal;
    $("#kdoc-o").onclick = async () => {
      const text = $("#kdoc-text").value;
      if (!text.trim()) return toast("内容为空", "err");
      try {
        await api(`/api/kb/${encodeURIComponent(kbId)}/docs`,
          { method: "POST", body: { name: $("#kdoc-name").value.trim() || "未命名文档", text } });
        closeModal(); delete state.kbDocs[kbId]; await loadResources();
        toast("已入库 ✅", "ok");
      } catch (e) { toast(`入库失败：${e.message}`, "err"); }
    };
  };
  const finput = box.querySelector("[data-finput]");
  box.querySelector('[data-act="upload"]').onclick = () => finput.click();
  finput.onchange = async () => {
    for (const f of [...finput.files || []]) {
      try {
        await api(`/api/kb/${encodeURIComponent(kbId)}/docs/upload?name=${encodeURIComponent(f.name)}`,
          { method: "POST", body: await f.text(), headers: {} });
      } catch (e) { toast(`上传失败 ${f.name}：${e.message}`, "err"); }
    }
    finput.value = ""; delete state.kbDocs[kbId]; await loadResources();
    toast("上传完成 ✅", "ok");
  };
}
function kbSearchModal(kbId) {
  openModal(`
    <h2>检索测试 · ${esc(kbId)}</h2>
    <div class="field"><label>查询</label><input id="kq" placeholder="问一句…"></div>
    <div class="btn-row"><button id="kq-go" class="primary">检索</button></div>
    <div id="kq-out" class="res-snippet" style="display:none"></div>`);
  $("#kq").focus();
  const go = async () => {
    try {
      const hits = await api("/api/kb/search", { method: "POST",
        body: { query: $("#kq").value, kb_ids: [kbId], top_k: 5 } });
      $("#kq-out").style.display = "block";
      $("#kq-out").textContent = hits.map(h =>
        `【${h.name}｜相关度 ${h.score}】${h.text}`).join("\n\n") || "（无结果）";
    } catch (e) { toast(e.message, "err"); }
  };
  $("#kq-go").onclick = go;
  $("#kq").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}

/* ---- 技能 ---- */
function renderResSkills(body) {
  body.innerHTML = `
    <div class="res-actions"><button id="res-skill-new" class="primary">＋ 新建技能</button></div>
    ${state.skillList.map(s => `
      <div class="res-card" data-sid="${esc(s.id)}">
        <div class="res-title">🛠 ${esc(s.name)} <span class="res-desc">${esc(s.id)}</span></div>
        <div class="res-desc">${esc(s.description || "")} · ${s.chars} 字</div>
        <div class="res-actions"><button data-act="edit">查看 / 编辑</button>
          <button data-act="del" class="danger">删除</button></div>
      </div>`).join("") || '<div class="res-empty">还没有技能，点上方新建。</div>'}`;
  $("#res-skill-new").onclick = () => skillEditModal(null);
  $$("#res-body .res-card[data-sid]").forEach(card => {
    const id = card.dataset.sid;
    card.querySelector('[data-act="edit"]').onclick = async () =>
      skillEditModal(await api(`/api/skills/${encodeURIComponent(id)}`));
    card.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`确认删除技能「${id}」？`)) return;
      await api(`/api/skills/${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadResources(); toast("已删除", "ok");
    };
  });
}
function skillEditModal(skill) {
  const isNew = !skill;
  openModal(`
    <h2>${isNew ? "新建技能" : `编辑技能 · ${esc(skill.id)}`}</h2>
    <div class="field"><label>技能名</label>
      <input id="sk-name" value="${esc(skill?.name || "")}"></div>
    <div class="field"><label>描述（给 LLM / 节点选择看的）</label>
      <input id="sk-desc" value="${esc(skill?.description || "")}"></div>
    <div class="field"><label>指令内容（Markdown，会注入 system 提示词）</label>
      <textarea id="sk-content" rows="12">${esc(skill?.content || "")}</textarea></div>
    <div class="btn-row"><button id="sk-c">取消</button>
      <button id="sk-o" class="primary">保存</button></div>`);
  $("#sk-c").onclick = closeModal;
  $("#sk-o").onclick = async () => {
    const id = isNew ? ($("#sk-name").value.trim().toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || `skill-${Date.now() % 10000}`)
      : skill.id;
    try {
      await api(`/api/skills/${encodeURIComponent(id)}`, { method: "PUT", body: {
        name: $("#sk-name").value.trim(), description: $("#sk-desc").value.trim(),
        content: $("#sk-content").value } });
      closeModal(); await loadResources(); toast("技能已保存 ✅", "ok");
    } catch (e) { toast(`保存失败：${e.message}`, "err"); }
  };
}

/* ---- MCP ---- */
function renderResMcp(body) {
  const servers = state.mcpCfg.servers || {};
  body.innerHTML = `
    <div class="palette-note">MCP 走 stdio 传输：本机命令行启动的服务进程。
      常见如 <code>npx -y @modelcontextprotocol/server-filesystem /some/dir</code>。</div>
    <div class="res-actions"><button id="res-mcp-new" class="primary">＋ 添加服务器</button></div>
    ${Object.entries(servers).map(([name, cfg]) => `
      <div class="res-card" data-mcp="${esc(name)}">
        <div class="res-title">🔌 ${esc(name)}</div>
        <div class="res-snippet">${esc(cfg.command)} ${(cfg.args || []).join(" ")}</div>
        <div class="res-actions"><button data-act="tools">列出工具</button>
          <button data-act="edit">编辑</button>
          <button data-act="del" class="danger">删除</button></div>
        <div data-mcptools></div>
      </div>`).join("") || '<div class="res-empty">未配置 MCP 服务器。</div>'}`;
  $("#res-mcp-new").onclick = () => mcpEditModal(null);
  $$("#res-body .res-card[data-mcp]").forEach(card => {
    const name = card.dataset.mcp;
    card.querySelector('[data-act="tools"]').onclick = async () => {
      const box = card.querySelector("[data-mcptools]");
      box.innerHTML = '<span class="res-desc">连接中…</span>';
      await ensureMcpTools(name);
      const tools = state.mcpTools[name] || [];
      box.innerHTML = tools.map(t => `
        <div class="res-doc-row"><span class="doc-name" title="${esc(t.description || "")}">
          <b>${esc(t.name)}</b> — ${esc(t.description || "")}</span></div>`)
        .join("") || '<span class="res-desc">无工具或连接失败</span>';
    };
    card.querySelector('[data-act="edit"]').onclick = () =>
      mcpEditModal([name, servers[name]]);
    card.querySelector('[data-act="del"]').onclick = async () => {
      delete servers[name];
      state.mcpCfg = await api("/api/mcp", { method: "PUT", body: { servers } });
      await loadResources(); toast("已删除", "ok");
    };
  });
}
function mcpEditModal(existing) {
  const isNew = !existing;
  const [name, cfg] = existing || ["", { command: "", args: [], env: {} }];
  openModal(`
    <h2>${isNew ? "添加 MCP 服务器" : `编辑 MCP · ${esc(name)}`}</h2>
    <div class="field"><label>名称（字母数字-）</label><input id="mc-name" value="${esc(name)}"></div>
    <div class="field"><label>启动命令</label>
      <input id="mc-cmd" placeholder="如 npx / python3 / uvx" value="${esc(cfg.command || "")}"></div>
    <div class="field"><label>参数（每行一个）</label>
      <textarea id="mc-args" rows="4">${esc((cfg.args || []).join("\n"))}</textarea></div>
    <div class="field"><label>环境变量 JSON（可空）</label>
      <textarea id="mc-env" rows="3">${esc(JSON.stringify(cfg.env || {}, null, 2))}</textarea></div>
    <div class="btn-row"><button id="mc-c">取消</button><button id="mc-o" class="primary">保存</button></div>`);
  $("#mc-c").onclick = closeModal;
  $("#mc-o").onclick = async () => {
    const servers = JSON.parse(JSON.stringify(state.mcpCfg.servers || {}));
    let env = {};
    try { env = JSON.parse($("#mc-env").value || "{}"); }
    catch { return toast("环境变量不是合法 JSON", "err"); }
    servers[$("#mc-name").value.trim()] = {
      command: $("#mc-cmd").value.trim(),
      args: $("#mc-args").value.split("\n").map(s => s.trim()).filter(Boolean),
      env };
    try {
      state.mcpCfg = await api("/api/mcp", { method: "PUT", body: { servers } });
      closeModal(); await loadResources(); toast("MCP 配置已保存 ✅", "ok");
    } catch (e) { toast(`保存失败：${e.message}`, "err"); }
  };
}

/* ---- 记忆 ---- */
async function renderResMemory(body) {
  let scopes = [];
  try { scopes = await api("/api/memory/scopes"); } catch { /* 忽略 */ }
  const cur = state.memScope || (scopes[0]?.scope ?? "global");
  body.innerHTML = `
    <div id="res-mem-scopes" style="display:flex;gap:5px;flex-wrap:wrap">
      ${scopes.map(s => `<button class="chip ${s.scope === cur ? "on" : ""}" data-scope="${esc(s.scope)}">
        ${esc(s.scope)}（${s.count}）</button>`).join("")}
      ${scopes.length ? "" : '<span class="res-desc">暂无记忆条目（流程/智能体运行后出现）</span>'}
    </div>
    <div class="res-actions">
      <input id="mem-k" placeholder="key" style="max-width:110px">
      <input id="mem-v" placeholder="value" style="flex:1">
      <button id="mem-add" class="primary">写入 ${esc(cur)}</button></div>
    <div id="res-mem-list" style="display:flex;flex-direction:column;gap:6px"></div>`;
  $$("#res-mem-scopes .chip").forEach(c => c.onclick = () => {
    state.memScope = c.dataset.scope; renderResMemory(body);
  });
  $("#mem-add").onclick = async () => {
    const k = $("#mem-k").value.trim(), v = $("#mem-v").value;
    if (!k) return toast("请填写 key", "err");
    let val = v; try { val = JSON.parse(v); } catch { /* 保持字符串 */ }
    await api("/api/memory", { method: "POST", body: { scope: cur, key: k, value: val } });
    toast("已写入 ✅", "ok"); renderResMemory(body);
  };
  try {
    const items = await api(`/api/memory?scope=${encodeURIComponent(cur)}&limit=100`);
    $("#res-mem-list").innerHTML = items.map(it => `
      <div class="res-card"><div class="res-title"><b>${esc(it.key)}</b>
        <span class="spacer"></span>
        <button data-mk="${esc(it.key)}" title="删除" style="font-size:12px;padding:2px 8px">×</button></div>
        <div class="res-snippet">${esc(fmtJson(it.value))}</div></div>`).join("")
      || '<div class="res-empty">该作用域暂无条目。</div>';
    $$("#res-mem-list [data-mk]").forEach(btn => btn.onclick = async () => {
      await api(`/api/memory?scope=${encodeURIComponent(cur)}&key=${encodeURIComponent(btn.dataset.mk)}`,
        { method: "DELETE" });
      renderResMemory(body);
    });
  } catch (e) { $("#res-mem-list").innerHTML = `<div class="res-empty">${esc(e.message)}</div>`; }
}

/* ================= 运行 ================= */
$("#btn-run").onclick = async () => {
  if (!state.graph) return;
  const flow = state.graph, scope = runScope();
  const values = await promptInputs(flow);
  if (values === null || scope !== runScope()) return;
  closeDrawers();
  await submitRun(() => api(`/api/flows/${encodeURIComponent(flow.id)}/run`,
    { method: "POST", body: { inputs: values, background: true } }));
};

/* ================= 意图触发 ================= */
async function chat() {
  if (state.runSubmitting || state.runMonitor.active) return;
  const msg = $("#chat-input").value.trim();
  if (!msg) return;
  $("#chat-input").value = "";
  closeDrawers(); toast("正在理解意图…");
  await submitRun(async () => {
    const scope = runScope();
    const result = await api("/api/agent/chat", { method: "POST", body: { message: msg, background: true } });
    if (scope !== runScope()) return null;
    if (result.matched && result.run) return result.run;
    showReplyModal(result); return null;
  }, { onComplete: run => {
    const output = run.output;
    const reply = typeof output === "string" ? output : output?.text || fmtJson(output);
    toast(`「${run.flow_name}」${RUN_STATUS[run.status] || run.status}`,
      run.status === "success" ? "ok" : "err");
    if (reply || run.error) showReplyModal({ matched: true, flow_id: run.flow_id, run, reply: run.error || reply });
  } });
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

/* ================= 节点面板（分组） =================
 * 知识库 / 记忆 / 技能 / 工具 / MCP 都由智能体配置；
 * 画布只暴露统一的智能体节点。旧节点类型继续保留，以兼容历史流程。 */
const PALETTE_GROUPS = [
  ["流程", ["start", "end", "llm", "condition", "intent", "template", "http"]],
  ["智能体", ["ai_agent"]],
  ["视频制作", ["storyboard", "character", "keyframe", "shot_video", "voiceover", "video_compose", "merge_video", "asset"]],
];
const PALETTE_DESC = {
  start: "流程入口与输入变量", end: "流程出口，产出回复",
  llm: "大模型文本生成", condition: "表达式选分支",
  intent: "话术意图分流", template: "渲染文本模板", http: "发起 HTTP 请求",
  ai_agent: "调用智能体（自带知识库/记忆/技能/工具）", brain: "交给外部运行时推理",
  tool: "调用内置工具", mcp: "调用 MCP 服务器的工具",
  agent: "调用本地注册的能力（job_agent 等）",
  storyboard: "剧情 → 分镜表", character: "多方位角色设定图", keyframe: "逐镜生成首帧图",
  shot_video: "关键帧图生视频", voiceover: "逐镜生成 WAV 旁白",
  video_compose: "对齐配音、字幕并输出成片", merge_video: "ffmpeg 合成长片", asset: "引用素材库",
};
function renderPalette() {
  $("#palette-list").innerHTML = PALETTE_GROUPS.map(([title, types]) => {
    const items = types.filter(t => state.nodeTypes[t]).map(t => {
      const m = state.nodeTypes[t];
      const c = m.color || "#64748b";
      return `<div class="palette-item" data-type="${t}" tabindex="0"
        style="--pc:${c}; --pc-soft:${c}1c; --pc-line:${c}45"
        title="${esc(m.desc || "")}">
        <span class="ico-chip">${m.icon || "•"}</span>
        <span class="lbl">${esc(m.label || t)}<small>${esc(PALETTE_DESC[t] || "")}</small></span>
      </div>`;
    }).join("");
    return items ? `<h4 class="palette-group"${title === "视频制作" ? ' data-g="video"' : ""}>${title}</h4>${items}` : "";
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
  if (type === "ai_agent" && state.aiAgents.length) {
    const candidate = state.aiAgents.find(agent =>
      !dependencyCycleReason("flow", state.graph.id, "agent", agent.id));
    if (candidate) defaults.ai_agent_id = candidate.id;
  }
  if (type === "skill" && state.skillList.length) defaults.skill_id = state.skillList[0].id;
  if (type === "mcp") {
    const servers = Object.keys(state.mcpCfg.servers || {});
    if (servers.length) defaults.server = servers[0];
  }
  if (type === "tool" && state.toolList.length) defaults.tool = state.toolList[0].name;
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
    const setup = await api("/api/setup/status");
    if (!setup.initialized) return renderSetup();
    try { await enterApplication(); }
    catch (error) {
      if (error.status === 403 && state.workspaceId) {
        state.workspaceId = "";
        localStorage.removeItem("flow-studio-workspace");
        try { await enterApplication(); return; } catch { /* 转登录 */ }
      }
      renderLogin();
    }
  } catch (e) {
    renderLogin();
    $("#auth-error").textContent = `初始化失败：${e.message}`;
  }
})();
