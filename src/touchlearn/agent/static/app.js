/* TouchLearn chat front end.
 * No framework, no build step: the competition machine may be offline.
 */
(() => {
  "use strict";

  const MAX_FILE = 30 * 1024 * 1024;
  const MAX_TOTAL = 80 * 1024 * 1024;
  const MAX_FILES = 12;
  const ACCEPT = ".png,.jpg,.jpeg,.webp,.bmp,.pdf,.stl,.obj,.json,.txt";
  const ALLOWED = new Set(ACCEPT.split(","));

  const SUBJECT_LABEL = {
    auto: "任意需求",
    function: "函数图像",
    robot: "机械臂",
    mesh: "三维文件",
    scene: "触觉场景",
    unsupported: "暂不支持",
  };
  const MODE_LABEL = {
    tactile_plate: "A5 触觉板",
    volumetric_model: "三维立体模型",
    silhouette_relief: "A5 轮廓浮雕",
  };
  const STATUS_LABEL = {
    PASS: "通过",
    FAIL: "失败",
    DEVIATION: "工艺偏离",
    NEEDS_REVIEW: "待复核",
    NOT_APPLICABLE: "不适用",
  };
  const KEY_POINT_LABEL = {
    root: "零点",
    minimum: "极小值",
    maximum: "极大值",
    y_intercept: "y 轴截距",
    extremum: "极值",
  };
  const JOB_TABS = [
    ["preview", "预览与下载"],
    ["compliance", "合规报告"],
    ["audio", "讲解音频"],
    ["qr", "二维码"],
    ["print", "打印说明"],
  ];
  const SELECTION_KEY = "touchlearn.llm.selection.v1";
  const FALLBACK_EFFORTS = [
    { id: "low", label: "低", note: "响应更快，适合大多数分类与结构化任务" },
    { id: "medium", label: "中", note: "在速度与推理之间取平衡" },
    { id: "high", label: "高", note: "复杂描述更稳，响应更慢" },
  ];

  const state = {
    health: null,
    providers: null,
    models: null,
    selection: null,
    popoverOpen: false,
    popoverView: "models",
    templates: [],
    files: [],
    sending: false,
    turn: 0,
    lastRun: null,
    activeJob: null,
    timer: null,
    viewer: null,
    speaking: null,
    bootstrapped: false,
    sceneEditor: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  class AuthError extends Error {}

  /* ------------------------------------------------------------------ */
  /* Helpers                                                             */
  /* ------------------------------------------------------------------ */

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function icon(name) {
    return `<svg class="icon" aria-hidden="true"><use href="#icon-${name}"/></svg>`;
  }

  function fmtBytes(bytes) {
    if (!Number.isFinite(bytes)) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function fmtNumber(value, digits = 2) {
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value ?? "");
    const text = number.toFixed(digits);
    return text.includes(".") ? text.replace(/\.?0+$/, "") : text;
  }

  function fmtTime(value) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  let toastTimer = null;
  function toast(message, isError = false) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("error", Boolean(isError));
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), 4200);
  }

  async function fetchJSON(url, options = {}) {
    const response = await fetch(url, { credentials: "same-origin", ...options });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (response.status === 401) {
      throw new AuthError((payload && payload.error) || "需要访问码。");
    }
    if (!response.ok) {
      throw new Error((payload && payload.error) || `请求失败（${response.status}）`);
    }
    return payload;
  }

  function fileExt(name) {
    const index = String(name).lastIndexOf(".");
    return index >= 0 ? String(name).slice(index).toLowerCase() : "";
  }

  function base64FromFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const comma = result.indexOf(",");
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(new Error(`读取文件失败：${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  async function copyText(text) {
    const value = String(text || "");
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      toast("链接已复制");
      return;
    } catch (error) {
      /* fall through to the legacy path */
    }
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "readonly");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      toast("链接已复制");
    } catch (error) {
      toast("复制失败，请手动选择链接。", true);
    } finally {
      area.remove();
    }
  }

  function downloadBlob(content, filename, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  /* ------------------------------------------------------------------ */
  /* Chat shell                                                          */
  /* ------------------------------------------------------------------ */

  function scrollToEnd() {
    const scroller = $("#chatScroll");
    requestAnimationFrame(() => {
      scroller.scrollTop = scroller.scrollHeight;
    });
  }

  function showConversation() {
    $("#welcome").classList.add("hidden");
  }

  function userMessage(text, files) {
    const installed = Array.isArray(files) ? files : [];
    const wrap = document.createElement("div");
    wrap.className = "msg user";
    const fileHtml = installed.length
      ? `<div class="bubble-files">${installed.map((file) => `<span class="bubble-file">${escapeHtml(file.name)}</span>`).join("")}</div>`
      : "";
    wrap.innerHTML = `<div class="bubble">${escapeHtml(text || "（已添加文件）")}${fileHtml}</div>`;
    $("#messages").appendChild(wrap);
    scrollToEnd();
  }

  function agentMessage() {
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    wrap.innerHTML = `
      <span class="avatar" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></span>
      <div class="msg-body">
        <div class="msg-meta"><strong>触见</strong><span>${fmtTime()}</span></div>
      </div>
    `;
    $("#messages").appendChild(wrap);
    return { wrap, body: wrap.querySelector(".msg-body") };
  }

  function agentText(body, text, isBad = false) {
    const node = document.createElement("div");
    node.className = `agent-text${isBad ? " bad" : ""}`;
    node.innerHTML = escapeHtml(text).replace(/\n/g, "<br>");
    body.appendChild(node);
    scrollToEnd();
    return node;
  }

  function thinking(body, text) {
    const node = document.createElement("div");
    node.className = "thinking";
    node.innerHTML = `<span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>${escapeHtml(text)}</span>`;
    body.appendChild(node);
    scrollToEnd();
    return node;
  }

  function setThinking(node, text) {
    const label = node.querySelector("span:last-child");
    if (label) label.textContent = text;
  }

  function retryRow(body, request, files) {
    const row = document.createElement("div");
    row.className = "draft-actions";
    row.innerHTML = `
      <button class="btn primary" type="button" data-retry>${icon("refresh")}重试</button>
      <button class="btn ghost" type="button" data-edit>修改描述</button>
    `;
    row.querySelector("[data-retry]").addEventListener("click", () => {
      row.remove();
      runTurn(request, { userText: "", files, echoUser: false });
    });
    row.querySelector("[data-edit]").addEventListener("click", () => {
      $("#promptInput").focus();
    });
    body.appendChild(row);
  }

  /* ------------------------------------------------------------------ */
  /* Auth and status                                                     */
  /* ------------------------------------------------------------------ */

  function setStatus(kind, text, title) {
    const pill = $("#statusPill");
    pill.classList.remove("ok", "bad");
    if (kind) pill.classList.add(kind);
    $("#statusText").textContent = text;
    pill.title = title || text;
  }

  function showLogin() {
    $("#loginOverlay").classList.remove("hidden");
    setTimeout(() => $("#accessCode").focus(), 60);
  }

  function hideLogin() {
    $("#loginOverlay").classList.add("hidden");
    $("#loginError").classList.add("hidden");
  }

  function fillIcpNode(node, value) {
    if (!node) return;
    if (!value) {
      node.classList.add("hidden");
      node.textContent = "";
      return;
    }
    const link = document.createElement("a");
    link.href = "https://beian.miit.gov.cn/";
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = value;
    node.textContent = "";
    node.append(link);
    node.classList.remove("hidden");
  }

  function renderIcp(health) {
    const value = health && health.icp ? String(health.icp).trim() : "";
    fillIcpNode($("#loginIcp"), value);
    fillIcpNode($("#siteIcp"), value);
  }

  async function refreshHealth() {
    try {
      const health = await fetchJSON("/api/health");
      state.health = health;
      renderIcp(health);
      let host = "";
      try {
        host = new URL(health.public_base_url).host;
      } catch (error) {
        host = health.public_base_url || "";
      }
      setStatus("ok", health.authenticated ? "在线" : "待登录", `二维码地址：${health.public_base_url}`);
      renderModelChip();
      if (health.access_required && !health.authenticated) {
        showLogin();
        return false;
      }
      hideLogin();
      if (!state.bootstrapped) {
        state.bootstrapped = true;
        await afterAuth();
      }
      return true;
    } catch (error) {
      setStatus("bad", "离线", String(error.message || error));
      return false;
    }
  }

  async function afterAuth() {
    await Promise.all([loadProviders(), loadTemplates()]);
  }

  async function loadProviders() {
    try {
      state.providers = await fetchJSON("/api/providers");
    } catch (error) {
      state.providers = null;
    }
    state.models = state.providers && state.providers.models ? state.providers.models : null;
    syncSelection();
    renderModelChip();
    if (state.popoverOpen) renderModelPopover();
  }

  async function loadTemplates() {
    try {
      const payload = await fetchJSON("/api/scene-templates");
      state.templates = payload.templates || [];
    } catch (error) {
      state.templates = [];
    }
    renderTemplateList();
  }

  /* ------------------------------------------------------------------ */
  /* Model and thinking-strength picker                                  */
  /* ------------------------------------------------------------------ */

  function readStoredSelection() {
    try {
      const raw = window.localStorage.getItem(SELECTION_KEY);
      const value = raw ? JSON.parse(raw) : null;
      return value && typeof value === "object" ? value : null;
    } catch (error) {
      return null;
    }
  }

  function saveSelection(selection) {
    try {
      window.localStorage.setItem(SELECTION_KEY, JSON.stringify(selection));
    } catch (error) {
      /* private mode or storage disabled: the in-memory choice still works */
    }
  }

  function supportsEffort() {
    return !(state.models && state.models.supports_effort === false);
  }

  function effortOptions() {
    if (!supportsEffort()) return [];
    const list = state.models && Array.isArray(state.models.efforts) ? state.models.efforts : [];
    return list.length ? list : FALLBACK_EFFORTS;
  }

  function effortLabel(id) {
    const entry = effortOptions().find((item) => item.id === id);
    return entry ? entry.label : String(id || "");
  }

  function modelEntry(id) {
    const list = state.models && Array.isArray(state.models.models) ? state.models.models : [];
    return list.find((item) => String(item.id) === String(id)) || null;
  }

  function syncSelection() {
    const models = state.models;
    if (!models || !models.configured || !Array.isArray(models.models) || !models.models.length) {
      state.selection = null;
      return;
    }
    const stored = state.selection && state.selection.model ? state.selection : readStoredSelection();
    const ids = new Set(models.models.map((item) => String(item.id)));
    const defaultModel = String((models.default && models.default.model) || models.models[0].id || "");
    const model = stored && ids.has(String(stored.model || "")) ? String(stored.model) : defaultModel;
    const efforts = effortOptions();
    const effortIds = new Set(efforts.map((item) => item.id));
    const defaultEffort = String((models.default && models.default.effort) || "low");
    const effort = !efforts.length
      ? ""
      : (stored && effortIds.has(String(stored.effort || ""))
        ? String(stored.effort)
        : (effortIds.has(defaultEffort) ? defaultEffort : "low"));
    state.selection = { model, effort };
    saveSelection(state.selection);
  }

  function renderModelChip() {
    const chip = $("#modelChip");
    if (!chip) return;
    const text = $("#modelChipText");
    const setText = (value, title) => {
      if (text) text.textContent = value;
      else chip.textContent = value;
      chip.title = title || value;
    };
    const llm = state.health && state.health.llm;
    chip.classList.remove("active", "offline");
    if (!llm) {
      setText("离线规则", "尚未读取模型配置");
      chip.classList.add("offline");
      return;
    }
    if (llm.mode === "locked") {
      setText("需要访问码", "登录后选择模型与思考强度");
      chip.classList.add("offline");
      return;
    }
    const models = state.models;
    if (!models || !models.configured || !state.selection) {
      setText("离线规则", llm.message || "未配置大模型：使用规则与离线模板");
      chip.classList.add("offline");
      return;
    }
    const entry = modelEntry(state.selection.model);
    const effort = state.selection.effort ? effortLabel(state.selection.effort) : "";
    const short = entry ? entry.short || entry.label : state.selection.model;
    setText(
      effort ? `${short} · 思考${effort}` : short,
      `${models.label || models.provider || "已配置"} · ${entry ? entry.label : state.selection.model}${effort ? ` · 思考强度 ${effort}` : ""}`,
    );
    chip.classList.toggle("active", state.popoverOpen);
  }

  function renderModelPopover() {
    const popover = $("#modelPopover");
    if (!popover) return;
    const models = state.models;
    if (!models || !models.configured || !Array.isArray(models.models) || !models.models.length) {
      popover.innerHTML = `
        <div class="popover-head">
          <div class="popover-title"><strong>模型</strong><small>当前实例没有配置可用模型</small></div>
          <button class="popover-close" type="button" data-close-model aria-label="关闭">${icon("close")}</button>
        </div>
        <p class="popover-empty">智能体会使用规则解析与 8 个离线模板，仍然可以生成可打印的触觉板。配置服务端环境变量后，这里会出现可选模型。</p>
        <div class="popover-actions"><button class="btn sm" type="button" data-model-settings>打开设置</button></div>
      `;
      return;
    }
    const selected = state.selection || {};
    const current = modelEntry(selected.model);
    const efforts = effortOptions();
    const effortEntry = efforts.find((item) => item.id === selected.effort);
    if (state.popoverView === "effort" && !supportsEffort()) state.popoverView = "models";

    if (state.popoverView === "effort") {
      popover.innerHTML = `
        <div class="popover-head">
          <button class="popover-icon-btn" type="button" data-model-view aria-label="返回模型列表">${icon("back")}</button>
          <div class="popover-title"><strong>思考强度</strong><small>${escapeHtml(current ? current.label : "")}</small></div>
          <button class="popover-close" type="button" data-close-model aria-label="关闭">${icon("close")}</button>
        </div>
        <div class="effort-list" role="listbox" aria-label="思考强度">
          ${efforts.map((item) => {
            const active = item.id === selected.effort;
            return `
              <button class="effort-row${active ? " selected" : ""}" type="button" role="option" aria-selected="${active}" data-effort="${escapeHtml(item.id)}">
                <span class="effort-glyph${active ? " on" : ""}" aria-hidden="true"><i></i><i></i><i></i></span>
                <span class="effort-copy"><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.note || "")}</small></span>
                <span class="model-check">${active ? icon("check") : ""}</span>
              </button>
            `;
          }).join("")}
        </div>
        <p class="popover-foot">强度越高，复杂描述的处理越稳，但响应更慢。长 JSON 输出被截断时会自动关闭隐藏推理重试。</p>
      `;
      return;
    }

    popover.innerHTML = `
      <div class="popover-head">
        <div class="popover-title">
          <strong>选择模型</strong>
          <small>${escapeHtml(models.label || models.provider || "当前服务商")} · 切换只影响下一次请求</small>
        </div>
        <button class="popover-close" type="button" data-close-model aria-label="关闭">${icon("close")}</button>
      </div>
      <div class="model-list" role="listbox" aria-label="模型">
        ${models.models.map((item) => {
          const active = String(item.id) === String(selected.model);
          return `
            <button class="model-row${active ? " selected" : ""}" type="button" role="option" aria-selected="${active}" data-model="${escapeHtml(item.id)}">
              <span class="model-row-head">
                <span class="model-name">${escapeHtml(item.label)}</span>
                ${item.tag ? `<span class="model-tag${item.vision ? " vision" : ""}">${escapeHtml(item.tag)}</span>` : ""}
                ${item.default ? '<span class="model-tag subtle">初始默认</span>' : ""}
              </span>
              <span class="model-note">${escapeHtml(item.note || "")}</span>
              <span class="model-check">${active ? icon("check") : ""}</span>
            </button>
          `;
        }).join("")}
      </div>
      ${supportsEffort() ? `
      <button class="effort-entry" type="button" data-effort-view>
        <span class="effort-glyph${effortEntry && effortEntry.id !== "low" ? " on" : ""}" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="effort-entry-copy">
          <strong>思考强度</strong>
          <small>${escapeHtml(effortEntry ? `${effortEntry.label} · ${effortEntry.note}` : "低 · 响应更快")}</small>
        </span>
        <span class="effort-current">${escapeHtml(effortEntry ? effortEntry.label : "")}</span>
        ${icon("chevron")}
      </button>` : ""}
      <p class="popover-foot">${escapeHtml(models.note || "")}</p>
    `;
  }

  function openModelPopover(view = "models") {
    const models = state.models;
    if (!models || !models.configured || !state.selection) {
      openSettings();
      return;
    }
    state.popoverView = view === "effort" && supportsEffort() ? "effort" : "models";
    state.popoverOpen = true;
    renderModelPopover();
    $("#modelPopover").classList.remove("hidden");
    $("#modelChip").setAttribute("aria-expanded", "true");
    renderModelChip();
  }

  function closeModelPopover() {
    if (!state.popoverOpen) return;
    state.popoverOpen = false;
    const popover = $("#modelPopover");
    if (popover) popover.classList.add("hidden");
    const chip = $("#modelChip");
    if (chip) chip.setAttribute("aria-expanded", "false");
    renderModelChip();
  }

  function toggleModelPopover() {
    if (state.popoverOpen) {
      closeModelPopover();
      return;
    }
    openModelPopover("models");
  }

  function chooseModel(id) {
    const entry = modelEntry(id);
    if (!entry || !state.selection) return;
    state.selection = { ...state.selection, model: String(entry.id) };
    saveSelection(state.selection);
    closeModelPopover();
    const visionHint = entry.vision ? "；读图请求也会优先用它" : "";
    toast(`已切换到 ${entry.label}${visionHint}`);
  }

  function chooseEffort(id) {
    const entry = effortOptions().find((item) => item.id === id);
    if (!entry || !state.selection) return;
    state.selection = { ...state.selection, effort: String(entry.id) };
    saveSelection(state.selection);
    closeModelPopover();
    toast(`思考强度已设为「${entry.label}」`);
  }

  async function submitLogin(event) {
    event.preventDefault();
    const input = $("#accessCode");
    const error = $("#loginError");
    const button = $("#loginBtn");
    const code = input.value.trim();
    if (!code) {
      error.textContent = "请输入访问码。";
      error.classList.remove("hidden");
      return;
    }
    button.disabled = true;
    button.textContent = "验证中…";
    error.classList.add("hidden");
    try {
      await fetchJSON("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      hideLogin();
      toast("已进入工作台");
      state.bootstrapped = false;
      await refreshHealth();
    } catch (err) {
      error.textContent = err.message || "访问码不正确。";
      error.classList.remove("hidden");
    } finally {
      button.disabled = false;
      button.textContent = "进入工作台";
    }
  }

  async function logout() {
    try {
      await fetchJSON("/api/logout", { method: "POST" });
    } catch (error) {
      /* the reload below still clears the UI */
    }
    closeSheets();
    location.reload();
  }

  /* ------------------------------------------------------------------ */
  /* Composer                                                            */
  /* ------------------------------------------------------------------ */

  function updateSend() {
    const button = $("#sendBtn");
    const text = $("#promptInput").value.trim();
    button.disabled = state.sending || (!text && !state.files.length);
  }

  function resizeComposer() {
    const input = $("#promptInput");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
  }

  function renderAttachments() {
    const bar = $("#attachmentBar");
    if (!state.files.length) {
      bar.innerHTML = "";
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");
    bar.innerHTML = state.files.map((file, index) => `
      <span class="file-chip">
        <span class="ext">${escapeHtml(fileExt(file.name).replace(".", "").toUpperCase() || "FILE")}</span>
        <span class="name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
        <button type="button" data-remove-file="${index}" aria-label="移除 ${escapeHtml(file.name)}">×</button>
      </span>
    `).join("");
  }

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    for (const file of incoming) {
      const ext = fileExt(file.name);
      if (!ALLOWED.has(ext)) {
        toast(`不支持 ${ext || "该文件"}。可上传图片、PDF、STL、OBJ、TXT 或 JSON。`, true);
        continue;
      }
      if (state.files.length >= MAX_FILES) {
        toast(`一次最多添加 ${MAX_FILES} 个文件。`, true);
        break;
      }
      if (file.size > MAX_FILE) {
        toast(`${file.name} 超过 30 MB 限制。`, true);
        continue;
      }
      const total = state.files.reduce((sum, item) => sum + item.size, 0) + file.size;
      if (total > MAX_TOTAL) {
        toast("文件总大小超过 80 MB 限制。", true);
        continue;
      }
      if (state.files.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) {
        continue;
      }
      state.files.push(file);
    }
    renderAttachments();
    updateSend();
  }

  function clearComposer() {
    state.files = [];
    $("#promptInput").value = "";
    renderAttachments();
    resizeComposer();
    updateSend();
  }

  function prefillComposer(prompt) {
    const input = $("#promptInput");
    const marker = prompt.indexOf("@@");
    const text = marker >= 0 ? prompt.slice(0, marker) + prompt.slice(marker + 2) : prompt;
    input.value = text;
    resizeComposer();
    updateSend();
    input.focus();
    const caret = marker >= 0 ? marker : text.length;
    input.setSelectionRange(caret, caret);
    toast(marker >= 0 ? "已填入输入框，请在 y= 后填写你的函数。" : "已填入输入框，可修改后发送。");
  }

  function missingFunctionExpression(text) {
    return /\b(?:y|f\s*\(\s*x\s*\))\s*=\s*(?:$|[\s，,。；;]*(?:做成|生成|画出?|绘制|$))/i.test(text);
  }

  function handleSubmit(event) {
    event.preventDefault();
    if (state.sending) return;
    const text = $("#promptInput").value.trim();
    const files = state.files.slice();
    if (!text && !files.length) {
      toast("先描述你的需求，或添加一个文件。", true);
      return;
    }
    if (!files.length && missingFunctionExpression(text)) {
      toast("请在 y= 后面填写函数解析式，例如 y=x^2-2x-4。", true);
      $("#promptInput").focus();
      return;
    }
    const request = { subject: "auto", text };
    if (state.pendingTemplate) {
      request.template = state.pendingTemplate;
      state.pendingTemplate = "";
    }
    clearComposer();
    runTurn(request, { userText: text, files });
  }

  /* ------------------------------------------------------------------ */
  /* One analysis turn                                                   */
  /* ------------------------------------------------------------------ */

  function draftIntro(draft) {
    if (draft.subject === "function") {
      return draft.ready
        ? "我识别到这是一张函数图像。解析式和视窗已经填好，关键点由确定性几何引擎计算；请核对后再生成。"
        : "我识别到函数图像需求，但还缺少可靠的解析式。请在草稿里补上，或重新上传更清晰的图片。";
    }
    if (draft.subject === "robot") {
      if (!draft.output_mode) {
        return "我读到了机械臂结构。请先选择输出形态：缩小成立体模型，还是做成 A5 浮雕触觉板。";
      }
      return draft.ready
        ? "我读到了机械臂结构，参数在下面。确认后我会生成可直接切片的 STL。"
        : "我读到了机械臂，但还需要补充参数。";
    }
    if (draft.subject === "mesh") {
      return draft.ready
        ? "我已经读取了三维文件并检查网格。请选择缩放或轮廓浮雕参数。"
        : "三维文件读取有问题，请检查文件后重试。";
    }
    if (draft.subject === "scene") {
      const hasCircuit = draft.fields && draft.fields.circuit && typeof draft.fields.circuit === "object";
      if (!draft.ready) return "我还不能可靠地排出这张场景。请补充结构，或选择一个离线模板。";
      if (hasCircuit) {
        return "我把需求整理成了一张 A5 触觉场景草稿，并生成了可编辑电路。元件位置、开关状态和导线已预置，可以直接在图形编辑器里拖动，或输入一句话让智能体修改。";
      }
      return "我把需求整理成了一张 A5 触觉场景草稿。请核对图块顺序、标签和连接关系。";
    }
    return "这类请求目前不能可靠地转成可触摸的 STL。我不会为了出图而编造结构。";
  }

  async function runTurn(request, { userText = "", files = [], echoUser = true } = {}) {
    if (state.sending) {
      toast("上一条还在处理，请稍等。", true);
      return;
    }
    state.sending = true;
    updateSend();
    const token = ++state.turn;
    if (echoUser) userMessage(userText, files);
    showConversation();
    const { wrap, body } = agentMessage();
    const wait = thinking(body, files.length ? `正在读取 ${files.length} 个文件并理解需求…` : "正在理解你的需求…");
    try {
      const payload = { subject: request.subject || "auto", text: request.text || "" };
      if (request.template) payload.template = request.template;
      if (request.scene) payload.scene = request.scene;
      if (request.circuit) payload.circuit = request.circuit;
      // The server validates these against its own catalog.  Only send them
      // when a model is actually configured, so an offline instance sees the
      // same request as before.
      if (state.models && state.models.configured && state.selection) {
        payload.model = state.selection.model;
        if (state.selection.effort) payload.effort = state.selection.effort;
      }
      payload.files = await Promise.all(files.map(async (file) => ({
        name: file.name,
        data: await base64FromFile(file),
      })));
      if (token !== state.turn) return;
      const result = await fetchJSON("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (token !== state.turn) return;
      wait.remove();
      const draft = result.draft;
      state.lastRun = { request, files };
      agentText(body, draftIntro(draft));
      renderDraftCard(body, draft, { request, files });
      scrollToEnd();
    } catch (error) {
      wait.remove();
      if (error instanceof AuthError) {
        agentText(body, "需要访问码才能继续。请在登录窗口输入部署者提供的访问码。", true);
        showLogin();
      } else if (String(error.message || "").includes("不支持所选模型")) {
        const stale = state.selection ? state.selection.model : "";
        await loadProviders();
        const entry = state.selection ? modelEntry(state.selection.model) : null;
        const label = entry ? entry.label : ((state.selection && state.selection.model) || "初始默认");
        agentText(body, `原先选择的模型 ${stale} 已不在当前实例的可用列表里，已回退到 ${label}。请重试。`, true);
        retryRow(body, request, files);
      } else {
        agentText(body, `这次没有理解成功：${error.message || "未知错误"}`, true);
        retryRow(body, request, files);
      }
    } finally {
      state.sending = false;
      updateSend();
    }
  }

  /* ------------------------------------------------------------------ */
  /* Draft card                                                          */
  /* ------------------------------------------------------------------ */

  function confidenceLabel(value) {
    if (value >= 0.85) return "高置信度";
    if (value >= 0.6) return "中等置信度 · 请核对";
    return "低置信度 · 需要补充";
  }

  function field(label, control, full = false) {
    return `<label class="field${full ? " full" : ""}"><span>${escapeHtml(label)}</span>${control}</label>`;
  }

  function renderKeyPoints(points) {
    if (!Array.isArray(points) || !points.length) return "";
    return `
      <div class="table-scroll">
        <table class="data">
          <thead><tr><th>标记</th><th>类型</th><th>x</th><th>y</th></tr></thead>
          <tbody>${points.map((point) => `
            <tr>
              <td>${escapeHtml(point.label)}</td>
              <td>${escapeHtml(KEY_POINT_LABEL[point.kind] || point.kind)}</td>
              <td class="mono">${fmtNumber(point.x, 3)}</td>
              <td class="mono">${fmtNumber(point.y, 3)}</td>
            </tr>
          `).join("")}</tbody>
        </table>
      </div>
    `;
  }

  function renderFunctionFields(draft) {
    const fields = draft.fields || {};
    const autoWindow = fields.auto_window !== false;
    const windowWarnings = Array.isArray(fields.window_warnings) ? fields.window_warnings : [];
    const windowError = String(fields.window_error || "");
    const hasWindow = Number.isFinite(Number(fields.x_min)) && Number.isFinite(Number(fields.x_max))
      && Number.isFinite(Number(fields.y_min)) && Number.isFinite(Number(fields.y_max));
    const windowText = hasWindow
      ? `视窗 x∈[${fmtNumber(fields.x_min, 3)}, ${fmtNumber(fields.x_max, 3)}]，y∈[${fmtNumber(fields.y_min, 3)}, ${fmtNumber(fields.y_max, 3)}]${autoWindow ? " · 由解析式自动计算" : " · 手动视窗"}`
      : "视窗尚未计算；填写解析式后会自动更新。";
    const derived = autoWindow ? "readonly" : "";
    return `
      <div class="field-grid">
        ${field("函数解析式", `<input class="input mono" data-field="expression" type="text" spellcheck="false" autocapitalize="off" autocorrect="off" autocomplete="off" value="${escapeHtml(fields.expression || "")}" placeholder="例如 x^2-2x-3 或 2sin(x)+1">`, true)}
      </div>
      <label class="checkline">
        <input type="checkbox" data-field="auto_window" ${autoWindow ? "checked" : ""}>
        <span><strong>自动视窗</strong>：根据解析式的零点、极值和曲线范围自动计算 x / y 范围；修改解析式后立即重算。</span>
      </label>
      <div class="field-grid">
        ${field("x 最小", `<input class="input${autoWindow ? " is-derived" : ""}" data-field="x_min" type="number" inputmode="decimal" step="any" value="${escapeHtml(fields.x_min ?? "")}" ${derived}>`)}
        ${field("x 最大", `<input class="input${autoWindow ? " is-derived" : ""}" data-field="x_max" type="number" inputmode="decimal" step="any" value="${escapeHtml(fields.x_max ?? "")}" ${derived}>`)}
        ${field("y 最小", `<input class="input${autoWindow ? " is-derived" : ""}" data-field="y_min" type="number" inputmode="decimal" step="any" value="${escapeHtml(fields.y_min ?? "")}" ${derived}>`)}
        ${field("y 最大", `<input class="input${autoWindow ? " is-derived" : ""}" data-field="y_max" type="number" inputmode="decimal" step="any" value="${escapeHtml(fields.y_max ?? "")}" ${derived}>`)}
      </div>
      <div class="window-status${windowError || windowWarnings.length ? " error" : ""}" data-window-status>${escapeHtml(windowError || windowText)}</div>
      <div data-window-warnings>${windowWarnings.length ? `<div class="callout warn"><strong>视窗提示</strong><ul>${windowWarnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : ""}</div>
      <div data-key-points>${renderKeyPoints(fields.key_points)}</div>
      <p class="hint-line">关键点由解析式自动计算。取消自动视窗后，x 必须成对填写；y 两项留空时仍会按当前 x 区间自动计算。</p>
    `;
  }

  function renderRobotFields(draft) {
    const fields = draft.fields || {};
    const spec = fields.spec || {};
    const lengths = Array.isArray(spec.link_lengths_mm) ? spec.link_lengths_mm : [];
    const longest = lengths.length ? Math.max(...lengths) : 40;
    const modeOptions = [];
    if (!draft.output_mode) modeOptions.push('<option value="">请选择输出形态</option>');
    modeOptions.push(`<option value="volumetric_model" ${draft.output_mode === "volumetric_model" ? "selected" : ""}>三维立体缩小模型</option>`);
    modeOptions.push(`<option value="tactile_plate" ${draft.output_mode === "tactile_plate" ? "selected" : ""}>A5 浮雕触觉板</option>`);
    return `
      <div class="field-grid">
        <div class="field full"><span>已理解的描述</span><div class="readonly-value">${escapeHtml(fields.description || "使用快速参数构建，未提供文字描述。")}</div></div>
        ${field("自由度", `<select class="select" data-field="dof">${[3, 4, 5, 6, 7].map((dof) => `<option value="${dof}" ${Number(spec.dof) === dof ? "selected" : ""}>${dof} 轴</option>`).join("")}</select>`)}
        ${field("代表连杆长度 / mm", `<input class="input" data-field="link" type="number" min="8" step="1" value="${escapeHtml(fmtNumber(longest, 1))}">`)}
        ${field("末端执行器", `<select class="select" data-field="end_effector">
          <option value="gripper" ${spec.end_effector === "gripper" ? "selected" : ""}>两指夹爪</option>
          <option value="suction" ${spec.end_effector === "suction" ? "selected" : ""}>吸盘</option>
          <option value="none" ${spec.end_effector === "none" ? "selected" : ""}>无末端</option>
        </select>`)}
        ${field("输出形态", `<select class="select" data-field="output_mode">${modeOptions.join("")}</select>`)}
        <label class="field" data-mode-only="volumetric_model"><span>目标高度 / mm（立体）</span><input class="input" data-field="target_height" type="number" inputmode="decimal" min="30" max="220" step="5" value="${escapeHtml(fmtNumber(fields.target_height_mm || 135, 0))}"></label>
      </div>
      <p class="hint-line">修改代表连杆长度会按比例缩放整条运动链；未标注的尺寸都是比例化估算，不是工程测量。</p>
    `;
  }

  function renderSceneFields(draft) {
    const fields = draft.fields || {};
    const scene = fields.scene || null;
    const summary = fields.summary || {};
    const order = Array.isArray(summary.reading_order) ? summary.reading_order : [];
    const warnings = Array.isArray(fields.warnings) ? fields.warnings : [];
    if (!scene || !Array.isArray(scene.blocks) || !scene.blocks.length) {
      const suggestions = Array.isArray(fields.suggested_templates) && fields.suggested_templates.length
        ? fields.suggested_templates
        : state.templates.slice(0, 8);
      return `
        <div class="callout warn"><strong>还没有可排版的场景。</strong> 请补充图块组成和连接关系，或选择一个离线模板。智能体不会凭空编造结构。</div>
        ${suggestions.length ? `
          <p class="hint-line">这些模板不调用大模型，断网也能生成：</p>
          <div class="template-grid">
            ${suggestions.map((entry) => `
              <button class="template-card" type="button" data-template="${escapeHtml(entry.id)}">
                ${entry.thumbnail ? `<img class="template-thumb" src="${escapeHtml(entry.thumbnail)}?crop=content" alt="${escapeHtml(entry.label)} 预览" loading="lazy">` : ""}
                <strong>${escapeHtml(entry.label)}${entry.editor === "circuit" ? " · 可编辑" : ""}</strong>
                <small>${escapeHtml(entry.description || "")}</small>
              </button>
            `).join("")}
          </div>
        ` : ""}
      `;
    }
    const rows = order.map((item) => `
      <tr>
        <td class="mono">${escapeHtml(item.index)}</td>
        <td>${escapeHtml(item.label || item.id)}</td>
        <td>${escapeHtml(item.label_zh || "")}</td>
        <td>${escapeHtml(item.note || "—")}</td>
      </tr>
    `).join("");
    const circuit = fields.circuit && typeof fields.circuit === "object" ? fields.circuit : null;
    const previewSvg = typeof fields.scene_svg === "string" ? fields.scene_svg : "";
    const previewBlock = `
      <div class="scene-preview-frame">
        ${previewSvg || '<div class="scene-loading">正在准备图形预览…</div>'}
        <div class="scene-preview-open">
          <span>${circuit ? "元件位置、开关状态和导线已预置，可直接拖动或让智能体修改。" : "这是结构预览；电路示意图可以进入图形编辑器逐件调整。"}</span>
          ${circuit ? `<button class="btn sm primary" type="button" data-open-scene-editor>${icon("diagram")}打开图形编辑器</button>` : ""}
        </div>
      </div>
    `;
    return `
      ${previewBlock}
      <div class="field-grid">
        ${field("英文标题（板面）", `<input class="input" data-field="scene_title" type="text" maxlength="32" value="${escapeHtml(scene.title || "")}">`)}
        ${field("中文标题（讲解）", `<input class="input" data-field="scene_title_zh" type="text" maxlength="40" value="${escapeHtml(scene.title_zh || "")}">`)}
      </div>
      ${rows ? `
        <div class="table-scroll">
          <table class="data">
            <thead><tr><th>编号</th><th>标签</th><th>中文</th><th>触摸说明</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      ` : ""}
      ${warnings.length ? `<div class="callout warn"><strong>排版提示</strong><ul>${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : ""}
      <p class="hint-line">图块按阅读顺序自动编号；完整英文标签只在空间允许时出现。位置和连接关系可触摸，绝对尺寸以打印设置为准。</p>
    `;
  }

  function renderMeshFields(draft) {
    const fields = draft.fields || {};
    const info = fields.mesh_info || {};
    return `
      <div class="field-grid">
        <div class="field full"><span>三维文件</span><div class="readonly-value mono">${escapeHtml(fields.mesh_name || "未读取")}</div></div>
        ${field("三角面", `<div class="readonly-value mono">${escapeHtml(info.triangles ?? "—")}</div>`)}
        ${field("水密", `<div class="readonly-value">${info.watertight ? "是" : "否"}</div>`)}
        ${field("包围盒 / mm", `<div class="readonly-value mono">${escapeHtml(Array.isArray(info.bounds_mm) ? info.bounds_mm.map((value) => fmtNumber(value, 1)).join(" × ") : "—")}</div>`, true)}
        ${field("输出形态", `<select class="select" data-field="output_mode">
          <option value="volumetric_model" ${draft.output_mode === "volumetric_model" ? "selected" : ""}>缩放为立体模型</option>
          <option value="silhouette_relief" ${draft.output_mode === "silhouette_relief" ? "selected" : ""}>A5 轮廓浮雕</option>
        </select>`)}
        <label class="field" data-mode-only="volumetric_model"><span>目标高度 / mm</span><input class="input" data-field="target_height" type="number" inputmode="decimal" min="20" max="220" step="5" value="${escapeHtml(fmtNumber(fields.target_height_mm || 120, 0))}"></label>
        <label class="field" data-mode-only="silhouette_relief"><span>浮雕分辨率 / mm</span><input class="input" data-field="resolution" type="number" inputmode="decimal" min="0.6" max="3" step="0.1" value="${escapeHtml(fmtNumber(fields.resolution_mm || 1, 1))}"></label>
      </div>
      ${info.watertight === false ? '<div class="callout warn"><strong>网格不是水密。</strong> 可以生成 STL，但合规报告会标记失败项；建议先在 CAD 中修复。</div>' : ""}
      <p class="hint-line">轮廓浮雕只做投影，不恢复内部结构、标签和隐藏特征。</p>
    `;
  }

  function renderDraftFields(draft) {
    if (draft.subject === "function") return renderFunctionFields(draft);
    if (draft.subject === "robot") return renderRobotFields(draft);
    if (draft.subject === "scene") return renderSceneFields(draft);
    if (draft.subject === "mesh") return renderMeshFields(draft);
    return `
      <div class="callout bad">
        <strong>智能体的判断</strong><br>
        ${escapeHtml((draft.fields && draft.fields.source_text) || "该请求未提供足够信息。")}
      </div>
    `;
  }

  function renderQuestions(draft) {
    const questions = Array.isArray(draft.questions) ? draft.questions : [];
    if (!questions.length) return "";
    return `
      <div class="callout warn">
        <strong>需要你确认</strong>
        <ul data-question-list>${questions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      </div>
    `;
  }

  function renderNotes(draft) {
    const notes = Array.isArray(draft.notes) ? draft.notes : [];
    if (!notes.length) return "";
    return `
      <details class="mini">
        <summary>智能体的依据与限制（${notes.length}）</summary>
        <div class="mini-body">${notes.map((item) => escapeHtml(item)).join("<br><br>")}</div>
      </details>
    `;
  }

  function renderDraftCard(body, draft, context) {
    const percent = Math.round((draft.confidence || 0) * 100);
    const summary = draft.fields && draft.fields.summary;
    const extraPills = draft.subject === "scene" && summary
      ? `<span class="pill">${Number(summary.block_count || 0)} 个图块</span><span class="pill">${Number(summary.primitive_count || 0)} 个图元</span>`
      : "";
    const generation = draft.fields && draft.fields.generation;
    const generationPill = generation && generation.provider
      ? `<span class="pill info">${escapeHtml(generation.provider)}${generation.model ? ` · ${escapeHtml(generation.model)}` : ""}</span>`
      : "";
    const llm = draft.fields && draft.fields.llm;
    const llmPill = llm && llm.model
      ? `<span class="pill">${escapeHtml(llm.label || llm.model)}${llm.effort ? ` · 思考${escapeHtml(effortLabel(llm.effort))}` : ""}${llm.requested ? "" : " · 默认"}</span>`
      : "";
    const subjectPill = `<span class="pill">${escapeHtml(SUBJECT_LABEL[draft.subject] || draft.subject)}</span>`;
    const modePill = draft.output_mode
      ? `<span class="pill info">${escapeHtml(MODE_LABEL[draft.output_mode] || draft.output_mode)}</span>`
      : '<span class="pill warn">待选择形态</span>';
    const readyPill = draft.ready
      ? '<span class="pill good">可以生成</span>'
      : '<span class="pill warn">需要确认</span>';
    const filePill = draft.files && draft.files.length
      ? `<span class="pill">${draft.files.length} 个来源文件</span>`
      : "";
    const routing = draft.fields && draft.fields.routing;
    const routeLine = routing
      ? `<p class="route-line">初始路径判断：${escapeHtml(SUBJECT_LABEL[routing.subject] || routing.subject)} · ${escapeHtml(routing.reason || "")}（${escapeHtml(routing.provider || "heuristic")}）</p>`
      : "";

    const card = document.createElement("section");
    card.className = "card";
    card.innerHTML = `
      <div class="draft-head">
        <div>
          <span class="eyebrow">STEP 02 · 理解草稿</span>
          <h3>${draft.ready ? "请核对，再生成" : "还需要一点信息"}</h3>
          <p>草稿 ${escapeHtml(draft.id)} · ${escapeHtml(fmtTime(draft.created_at))}</p>
        </div>
        <div class="confidence">
          <b>${percent}%</b>
          <small>${escapeHtml(confidenceLabel(draft.confidence || 0))}</small>
          <div class="confidence-bar"><i style="width:${percent}%"></i></div>
        </div>
      </div>
      <div class="draft-summary">${subjectPill}${modePill}${readyPill}${filePill}${extraPills}${llmPill}${generationPill}</div>
      ${routeLine}
      ${renderDraftFields(draft)}
      ${renderQuestions(draft)}
      ${renderNotes(draft)}
      <div class="draft-actions">
        <button class="btn primary" type="button" data-build>${icon("spark")}<span>确认并生成触觉产物</span></button>
        <button class="btn" type="button" data-reanalyze>${icon("refresh")}重新理解</button>
        <button class="btn ghost" type="button" data-download-draft>${icon("download")}原始草稿 JSON</button>
      </div>
      <p class="hint-line" data-build-hint></p>
    `;
    body.appendChild(card);
    bindDraftCard(card, draft, context);
    updateBuildState(draft, card);
    scrollToEnd();
  }

  function bindDraftCard(card, draft, context) {
    card.querySelector("[data-build]").addEventListener("click", () => buildDraft(card, draft));
    card.querySelector("[data-reanalyze]").addEventListener("click", () => {
      runTurn(context.request, { userText: "重新理解这份需求", files: context.files, echoUser: true });
    });
    card.querySelector("[data-download-draft]").addEventListener("click", () => {
      downloadBlob(JSON.stringify(draft, null, 2), `touchlearn-draft-${draft.id}.json`, "application/json");
    });
    $$("[data-template]", card).forEach((button) => {
      button.addEventListener("click", () => chooseTemplate(button.dataset.template));
    });
    $$("[data-open-scene-editor]", card).forEach((button) => {
      button.addEventListener("click", () => {
        const fields = draft.fields || {};
        const circuit = fields.circuit && typeof fields.circuit === "object"
          ? JSON.parse(JSON.stringify(fields.circuit))
          : null;
        const scene = fields.scene && typeof fields.scene === "object"
          ? JSON.parse(JSON.stringify(fields.scene))
          : null;
        const titleInput = card.querySelector('[data-field="scene_title"]');
        const titleZhInput = card.querySelector('[data-field="scene_title_zh"]');
        const title = titleInput ? titleInput.value.trim() : "";
        const titleZh = titleZhInput ? titleZhInput.value.trim() : "";
        if (circuit && title) {
          circuit.title = title;
          if (titleZh) circuit.title_zh = titleZh;
          else delete circuit.title_zh;
        }
        if (scene && title) {
          scene.title = title;
          if (titleZh) scene.title_zh = titleZh;
          else delete scene.title_zh;
        }
        openSceneEditor({ draft, card, circuit, scene });
      });
    });
    if (draft.subject === "function") bindFunctionCard(card, draft);
    card.addEventListener("input", () => {
      updateModeFields(draft, card);
      updateBuildState(draft, card);
    });
    card.addEventListener("change", () => {
      updateModeFields(draft, card);
      updateBuildState(draft, card);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Function window preview                                             */
  /* ------------------------------------------------------------------ */

  function functionPreviewState(card) {
    if (!card.__functionPreview) {
      card.__functionPreview = { seq: 0, timer: null, pending: false, valid: true, message: "" };
    }
    return card.__functionPreview;
  }

  function functionAutoChecked(card) {
    const input = card.querySelector('[data-field="auto_window"]');
    return input ? input.checked : true;
  }

  function setFunctionStatus(card, text, kind = "") {
    const status = card.querySelector("[data-window-status]");
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("error", kind === "error");
    status.classList.toggle("warn", kind === "warn");
    status.classList.toggle("pending", kind === "pending");
  }

  function setFunctionWarnings(card, warnings) {
    const holder = card.querySelector("[data-window-warnings]");
    if (!holder) return;
    const items = (Array.isArray(warnings) ? warnings : []).filter(Boolean);
    holder.innerHTML = items.length
      ? `<div class="callout warn"><strong>视窗提示</strong><ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`
      : "";
  }

  function setFunctionInputsReadonly(card) {
    const auto = functionAutoChecked(card);
    ["x_min", "x_max", "y_min", "y_max"].forEach((name) => {
      const input = card.querySelector(`[data-field="${name}"]`);
      if (!input) return;
      input.readOnly = auto;
      input.classList.toggle("is-derived", auto);
      if (auto) input.setAttribute("aria-readonly", "true");
      else input.removeAttribute("aria-readonly");
    });
  }

  function functionWindowDraft(card) {
    if (functionAutoChecked(card)) return { auto_window: true };
    const read = (name) => {
      const input = card.querySelector(`[data-field="${name}"]`);
      return input ? input.value.trim() : "";
    };
    const labels = { x_min: "x 最小", x_max: "x 最大", y_min: "y 最小", y_max: "y 最大" };
    const xValues = [read("x_min"), read("x_max")];
    if (!xValues[0] || !xValues[1]) {
      throw new Error("取消自动视窗后，请同时填写 x 最小和 x 最大。");
    }
    const yValues = [read("y_min"), read("y_max")];
    if (Boolean(yValues[0]) !== Boolean(yValues[1])) {
      throw new Error("y 最小和 y 最大要么都填写，要么都留空自动计算。");
    }
    const payload = { auto_window: false };
    ["x_min", "x_max"].forEach((name, index) => {
      const value = Number(xValues[index]);
      if (!Number.isFinite(value)) throw new Error(`${labels[name]}必须是数字。`);
      payload[name] = value;
    });
    if (payload.x_max <= payload.x_min) throw new Error("x 的上界必须大于下界。");
    if (yValues[0]) {
      ["y_min", "y_max"].forEach((name, index) => {
        const value = Number(yValues[index]);
        if (!Number.isFinite(value)) throw new Error(`${labels[name]}必须是数字。`);
        payload[name] = value;
      });
      if (payload.y_max <= payload.y_min) throw new Error("y 的上界必须大于下界。");
    }
    return payload;
  }

  function functionPreviewPayload(card) {
    const expressionInput = card.querySelector('[data-field="expression"]');
    const expression = expressionInput ? expressionInput.value.trim() : "";
    if (!expression) throw new Error("请填写函数解析式。");
    return { expression, ...functionWindowDraft(card) };
  }

  function scheduleFunctionPreview(card, draft, delay = 360) {
    const state = functionPreviewState(card);
    clearTimeout(state.timer);
    // Invalidate any in-flight response now, not when the debounced request
    // starts.  Otherwise an older reply can land after the user has typed and
    // re-enable the build button with stale values.
    state.seq += 1;
    const expressionInput = card.querySelector('[data-field="expression"]');
    if (!expressionInput || !expressionInput.value.trim()) {
      state.pending = false;
      state.valid = false;
      state.message = "请填写函数解析式。";
      setFunctionStatus(card, "请填写函数解析式，例如 x^2-2x-4。", "error");
      updateBuildState(draft, card);
      return;
    }
    state.pending = true;
    state.valid = false;
    state.message = "";
    setFunctionStatus(card, "正在按解析式重新计算视窗与关键点…", "pending");
    updateBuildState(draft, card);
    state.timer = setTimeout(() => runFunctionPreview(card, draft), delay);
  }

  async function runFunctionPreview(card, draft) {
    const state = functionPreviewState(card);
    const seq = state.seq;
    let payload;
    try {
      payload = functionPreviewPayload(card);
    } catch (error) {
      state.pending = false;
      state.valid = false;
      state.message = error.message;
      setFunctionStatus(card, error.message, "error");
      updateBuildState(draft, card);
      return;
    }
    try {
      const result = await fetchJSON("/api/function-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (seq !== state.seq) return;
      state.pending = false;
      state.valid = true;
      state.message = "";
      applyFunctionPreview(card, draft, result);
    } catch (error) {
      if (seq !== state.seq) return;
      state.pending = false;
      state.valid = false;
      state.message = error.message || "函数视窗校验失败。";
      if (error instanceof AuthError) {
        showLogin();
        return;
      }
      setFunctionStatus(card, `无法更新视窗：${state.message}`, "error");
      updateBuildState(draft, card);
    }
  }

  function applyFunctionPreview(card, draft, result) {
    const auto = functionAutoChecked(card);
    const setValue = (name, value) => {
      const input = card.querySelector(`[data-field="${name}"]`);
      if (input) input.value = fmtNumber(value, 3);
    };
    if (auto) {
      setValue("x_min", result.x_min);
      setValue("x_max", result.x_max);
      setValue("y_min", result.y_min);
      setValue("y_max", result.y_max);
    } else {
      const yMin = card.querySelector('[data-field="y_min"]');
      const yMax = card.querySelector('[data-field="y_max"]');
      if (yMin && yMax) {
        if (!yMin.value.trim() && !yMax.value.trim()) {
          yMin.placeholder = fmtNumber(result.y_min, 3);
          yMax.placeholder = fmtNumber(result.y_max, 3);
        } else {
          yMin.placeholder = "";
          yMax.placeholder = "";
        }
      }
    }
    const points = card.querySelector("[data-key-points]");
    if (points) {
      points.innerHTML = renderKeyPoints(result.key_points)
        || '<p class="hint-line">当前视窗内没有自动关键点，请检查解析式与范围。</p>';
    }
    updateFunctionQuestions(card, result);
    const warnings = Array.isArray(result.warnings) ? result.warnings.slice() : [];
    if (result.clipped && !warnings.some((item) => String(item).includes("视窗"))) {
      warnings.push(auto
        ? "自动视窗按分位数避开极端值，部分远端采样点不在板面内。"
        : "当前 y 视窗会裁掉部分曲线；可勾选自动视窗或手动增大 y 范围。");
    }
    setFunctionWarnings(card, warnings);
    const mode = auto ? "自动" : (result.y_source === "auto" ? "手动 x / 自动 y" : "手动");
    setFunctionStatus(
      card,
      `视窗 x∈[${fmtNumber(result.x_min, 3)}, ${fmtNumber(result.x_max, 3)}]，y∈[${fmtNumber(result.y_min, 3)}, ${fmtNumber(result.y_max, 3)}] · ${mode}`,
      warnings.length ? "warn" : "",
    );
    updateBuildState(draft, card);
  }

  const FUNCTION_DYNAMIC_QUESTION_RE = /[xXyY]\s*[∈=]|坐标范围|顶点与零点|同一点\s*\(|修改解析式/;

  function updateFunctionQuestions(card, result) {
    const list = card.querySelector("[data-question-list]");
    if (!list) return;
    // The confirmation list is generated during analysis.  Questions that
    // embed an old range or a point coordinate must not survive an expression
    // edit, otherwise the card tells the user to confirm numbers it no longer
    // displays.
    Array.from(list.querySelectorAll("li")).forEach((item) => {
      if (FUNCTION_DYNAMIC_QUESTION_RE.test(item.textContent || "")) item.remove();
    });
    list.querySelectorAll("[data-function-guidance]").forEach((item) => item.remove());
    const auto = result.auto_window !== false;
    const guidance = document.createElement("li");
    guidance.dataset.functionGuidance = "1";
    if (auto) {
      guidance.textContent = "窗口由解析式自动计算；修改解析式后会重新计算。如需固定定义域，请取消“自动视窗”后填写 x 范围。";
    } else if (result.y_source === "auto") {
      guidance.textContent = "当前 x 范围固定，y 范围按曲线自动计算；如需固定 y，请同时填写 y 最小和 y 最大。";
    } else {
      guidance.textContent = "当前为手动视窗；如果曲线被裁切，可扩大 y 范围或重新勾选自动视窗。";
    }
    if (list.firstChild) list.insertBefore(guidance, list.firstChild);
    else list.appendChild(guidance);

    const points = Array.isArray(result.key_points) ? result.key_points : [];
    const extrema = points.filter((point) => point.kind === "minimum" || point.kind === "maximum");
    const roots = points.filter((point) => point.kind === "root");
    const coincident = extrema.find((extremum) => roots.some(
      (root) => Math.abs(Number(root.x) - Number(extremum.x)) < 1e-3
        && Math.abs(Number(root.y) - Number(extremum.y)) < 1e-3,
    ));
    if (coincident) {
      const item = document.createElement("li");
      item.textContent = `顶点与零点在同一点 (${fmtNumber(coincident.x, 3)}, ${fmtNumber(coincident.y, 3)})，希望用何种触觉标记区分（如凸点/盲文标签）？`;
      list.appendChild(item);
    }
  }

  function bindFunctionCard(card, draft) {
    setFunctionInputsReadonly(card);
    if (draft.ready) updateFunctionQuestions(card, draft.fields || {});
    const auto = card.querySelector('[data-field="auto_window"]');
    if (auto) {
      auto.addEventListener("change", () => {
        setFunctionInputsReadonly(card);
        scheduleFunctionPreview(card, draft, 0);
      });
    }
    const expression = card.querySelector('[data-field="expression"]');
    if (expression) {
      expression.addEventListener("input", () => scheduleFunctionPreview(card, draft));
    }
    ["x_min", "x_max", "y_min", "y_max"].forEach((name) => {
      const input = card.querySelector(`[data-field="${name}"]`);
      if (!input) return;
      input.addEventListener("input", () => {
        if (!functionAutoChecked(card)) scheduleFunctionPreview(card, draft);
      });
    });
  }

  function updateModeFields(draft, card) {
    if (draft.subject !== "robot" && draft.subject !== "mesh") return;
    const select = card.querySelector('[data-field="output_mode"]');
    const mode = select ? select.value : (draft.output_mode || "");
    $$("[data-mode-only]", card).forEach((node) => {
      const allowed = String(node.dataset.modeOnly || "").split(/\s+/).filter(Boolean);
      node.hidden = allowed.length > 0 && !allowed.includes(mode);
    });
  }

  function updateBuildState(draft, card) {
    const button = card.querySelector("[data-build]");
    const hint = card.querySelector("[data-build-hint]");
    if (!button) return;
    updateModeFields(draft, card);
    let ok = true;
    let reason = "";
    if (draft.subject === "unsupported") {
      ok = false;
      reason = "这类请求暂时无法可靠生成触觉几何。";
    } else if (draft.subject === "scene") {
      ok = Boolean(draft.fields && draft.fields.scene && Array.isArray(draft.fields.scene.blocks) && draft.fields.scene.blocks.length);
      const title = card.querySelector('[data-field="scene_title"]');
      if (ok && (!title || !title.value.trim())) {
        ok = false;
        reason = "请填写英文标题。";
      }
      if (!ok && !reason) reason = "请先补充场景结构，或选择一个离线模板。";
    } else if (draft.subject === "robot") {
      const mode = card.querySelector('[data-field="output_mode"]');
      const dof = card.querySelector('[data-field="dof"]');
      const link = card.querySelector('[data-field="link"]');
      if (!mode || !mode.value) {
        ok = false;
        reason = "请选择输出形态后再生成。";
      } else if (!dof || !Number.isInteger(Number(dof.value)) || Number(dof.value) < 3 || Number(dof.value) > 7) {
        ok = false;
        reason = "自由度需在 3–7 之间。";
      } else if (!link || !Number.isFinite(Number(link.value)) || Number(link.value) < 8) {
        ok = false;
        reason = "代表连杆长度需为不小于 8 mm 的数字。";
      } else if (mode.value === "volumetric_model") {
        const target = card.querySelector('[data-field="target_height"]');
        if (!target || !Number.isFinite(Number(target.value)) || Number(target.value) < 30 || Number(target.value) > 220) {
          ok = false;
          reason = "目标高度需在 30–220 mm 之间。";
        }
      }
    } else if (draft.subject === "function") {
      const expression = card.querySelector('[data-field="expression"]');
      const preview = functionPreviewState(card);
      const auto = functionAutoChecked(card);
      if (!expression || !expression.value.trim()) {
        ok = false;
        reason = "请填写函数解析式。";
      } else if (preview.pending) {
        ok = false;
        reason = "正在校验解析式与视窗…";
      } else if (preview.valid === false) {
        ok = false;
        reason = preview.message || "请先修正函数解析式或视窗。";
      } else if (!auto) {
        const value = (name) => {
          const input = card.querySelector(`[data-field="${name}"]`);
          return input ? input.value.trim() : "";
        };
        const xMin = value("x_min");
        const xMax = value("x_max");
        const yMin = value("y_min");
        const yMax = value("y_max");
        if (!xMin || !xMax) {
          ok = false;
          reason = "取消自动视窗后，请同时填写 x 最小和 x 最大。";
        } else if (!(Number(xMax) > Number(xMin))) {
          ok = false;
          reason = "x 的上界必须大于下界。";
        } else if (Boolean(yMin) !== Boolean(yMax)) {
          ok = false;
          reason = "y 最小和 y 最大要么都填写，要么都留空自动计算。";
        } else if (yMin && !(Number(yMax) > Number(yMin))) {
          ok = false;
          reason = "y 的上界必须大于下界。";
        }
      }
    } else if (draft.subject === "mesh") {
      const fields = draft.fields || {};
      const error = fields.mesh_info && fields.mesh_info.error;
      const mode = card.querySelector('[data-field="output_mode"]');
      if (error) {
        ok = false;
        reason = "三维文件读取失败，请重新上传。";
      } else if (!fields.mesh_path) {
        ok = false;
        reason = "请先上传 STL 或 OBJ 三维文件。";
      } else if (mode && mode.value === "volumetric_model") {
        const target = card.querySelector('[data-field="target_height"]');
        if (!target || !Number.isFinite(Number(target.value)) || Number(target.value) < 20 || Number(target.value) > 220) {
          ok = false;
          reason = "目标高度需在 20–220 mm 之间。";
        }
      } else if (mode && mode.value === "silhouette_relief") {
        const resolution = card.querySelector('[data-field="resolution"]');
        if (!resolution || !Number.isFinite(Number(resolution.value)) || Number(resolution.value) < 0.6 || Number(resolution.value) > 3) {
          ok = false;
          reason = "浮雕分辨率需在 0.6–3 mm 之间。";
        }
      }
    }
    button.disabled = !ok;
    if (hint) hint.textContent = reason;
  }

  function numberFrom(card, name, label) {
    const input = card.querySelector(`[data-field="${name}"]`);
    if (!input) throw new Error(`缺少字段：${label}`);
    const raw = input.value.trim();
    if (!raw) throw new Error(`${label}不能为空。`);
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`${label}必须是数字。`);
    if (input.min !== "" && Number.isFinite(Number(input.min)) && value < Number(input.min)) {
      throw new Error(`${label}不能小于 ${input.min}。`);
    }
    if (input.max !== "" && Number.isFinite(Number(input.max)) && value > Number(input.max)) {
      throw new Error(`${label}不能大于 ${input.max}。`);
    }
    return value;
  }

  function collectOverrides(draft, card) {
    const fields = {};
    let outputMode = draft.output_mode || "";
    if (draft.subject === "function") {
      const expression = card.querySelector('[data-field="expression"]').value.trim();
      if (!expression) throw new Error("请填写函数解析式。");
      const preview = functionPreviewState(card);
      if (preview.pending) throw new Error("视窗还在计算，请稍等。");
      if (preview.valid === false) throw new Error(preview.message || "请先修正函数解析式或视窗。");
      fields.expression = expression;
      fields.auto_window = functionAutoChecked(card);
      if (!fields.auto_window) {
        const { auto_window: _auto, ...window } = functionWindowDraft(card);
        Object.assign(fields, window);
        // Clear stale draft values when the user leaves y empty so the
        // backend derives y from the current x interval.
        if (window.y_min === undefined) {
          fields.y_min = null;
          fields.y_max = null;
        }
      }
      outputMode = "tactile_plate";
    } else if (draft.subject === "robot") {
      const mode = card.querySelector('[data-field="output_mode"]').value;
      if (!mode) throw new Error("请选择输出形态。");
      outputMode = mode;
      const dof = Number(card.querySelector('[data-field="dof"]').value);
      const link = Number(card.querySelector('[data-field="link"]').value);
      const end = card.querySelector('[data-field="end_effector"]').value;
      if (!Number.isInteger(dof) || dof < 3 || dof > 7) throw new Error("自由度需在 3–7 之间。");
      if (!Number.isFinite(link) || link < 8) throw new Error("代表连杆长度需为不小于 8 mm 的数字。");
      const spec = JSON.parse(JSON.stringify((draft.fields && draft.fields.spec) || {}));
      const lengths = Array.isArray(spec.link_lengths_mm) ? spec.link_lengths_mm.slice(0, dof) : [];
      while (lengths.length < dof) lengths.push(lengths.length ? lengths[lengths.length - 1] : link);
      const currentMax = Math.max(...lengths, 0.001);
      const scale = link / currentMax;
      spec.link_lengths_mm = lengths.map((value) => Number((value * scale).toFixed(3)));
      spec.link_widths_mm = (spec.link_widths_mm || []).slice(0, dof).map((value) => Number((value * Math.max(0.75, scale * 0.9)).toFixed(3)));
      while (spec.link_widths_mm.length < dof) spec.link_widths_mm.push(Math.max(4, Number((link * 0.12).toFixed(3))));
      spec.joint_radii_mm = (spec.joint_radii_mm || []).slice(0, dof).map((value) => Number((value * Math.max(0.8, scale * 0.85)).toFixed(3)));
      while (spec.joint_radii_mm.length < dof) spec.joint_radii_mm.push(Math.max(3, Number((link * 0.08).toFixed(3))));
      spec.joint_angles_deg = (spec.joint_angles_deg || []).slice(0, dof);
      while (spec.joint_angles_deg.length < dof) spec.joint_angles_deg.push(0);
      spec.base_width_mm = Number(Math.min(60, Math.max(24, (spec.base_width_mm || 34) * Math.min(scale, 1.8))).toFixed(3));
      spec.base_height_mm = Number(Math.min(24, Math.max(10, (spec.base_height_mm || 14) * Math.min(scale, 1.8))).toFixed(3));
      spec.end_effector = end;
      spec.dof = dof;
      fields.spec = spec;
      fields.description = (draft.fields && draft.fields.description) || "";
      if (mode === "volumetric_model") {
        fields.target_height_mm = numberFrom(card, "target_height", "目标高度");
      }
    } else if (draft.subject === "scene") {
      const scene = JSON.parse(JSON.stringify((draft.fields && draft.fields.scene) || {}));
      if (!Array.isArray(scene.blocks) || !scene.blocks.length) {
        throw new Error("还没有可排版的场景，请先补充描述或选择模板。");
      }
      const titleInput = card.querySelector('[data-field="scene_title"]');
      const titleZhInput = card.querySelector('[data-field="scene_title_zh"]');
      const title = titleInput ? titleInput.value.trim() : "";
      const titleZh = titleZhInput ? titleZhInput.value.trim() : "";
      if (!title) throw new Error("请填写英文标题。");
      scene.title = title;
      // Do not keep the analysed Chinese title when the user deliberately
      // clears the field; otherwise the build silently uses a stale value.
      if (titleZh) scene.title_zh = titleZh;
      else delete scene.title_zh;
      fields.scene = scene;
      const circuit = draft.fields && draft.fields.circuit;
      if (circuit && typeof circuit === "object") {
        const nextCircuit = JSON.parse(JSON.stringify(circuit));
        // The build recompiles the circuit, so the title fields must be
        // mirrored into the semantic model or the printed plate reverts to
        // the title captured when the editor opened.
        nextCircuit.title = title;
        if (titleZh) nextCircuit.title_zh = titleZh;
        else delete nextCircuit.title_zh;
        fields.circuit = nextCircuit;
        fields.editor = "circuit";
      }
      outputMode = "tactile_plate";
    } else if (draft.subject === "mesh") {
      if (!draft.fields || !draft.fields.mesh_path) {
        throw new Error("请先上传 STL 或 OBJ 三维文件。");
      }
      const mode = card.querySelector('[data-field="output_mode"]');
      outputMode = mode ? mode.value : "volumetric_model";
      if (outputMode === "volumetric_model") {
        fields.target_height_mm = numberFrom(card, "target_height", "目标高度");
      } else {
        fields.resolution_mm = numberFrom(card, "resolution", "浮雕分辨率");
      }
    } else {
      throw new Error("这个草稿还没有可生成的产物。");
    }
    return { fields, output_mode: outputMode };
  }

  async function buildDraft(card, draft) {
    const button = card.querySelector("[data-build]");
    if (!button || button.disabled || button.dataset.busy === "1") return;
    let overrides;
    try {
      overrides = collectOverrides(draft, card);
    } catch (error) {
      toast(error.message, true);
      return;
    }
    button.dataset.busy = "1";
    button.disabled = true;
    const label = button.querySelector("span");
    if (label) label.textContent = "正在提交…";
    try {
      const result = await fetchJSON("/api/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_id: draft.id, overrides }),
      });
      const ui = createJobMessage(result.job_id, draft, card);
      startPolling(ui);
    } catch (error) {
      if (error instanceof AuthError) {
        showLogin();
      } else {
        toast(error.message || "提交生成任务失败", true);
      }
    } finally {
      button.dataset.busy = "";
      if (label) label.textContent = "确认并生成触觉产物";
      updateBuildState(draft, card);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Job card and polling                                                */
  /* ------------------------------------------------------------------ */

  function jobTitle(subject, mode) {
    if (subject === "function") return "函数图像 · A5 触觉板";
    if (subject === "robot") return mode === "volumetric_model" ? "机械臂 · 三维立体模型" : "机械臂 · A5 浮雕触觉板";
    if (subject === "scene") return "触觉场景 · A5 触觉板";
    if (subject === "mesh") return mode === "silhouette_relief" ? "三维文件 · A5 轮廓浮雕" : "三维文件 · 缩放立体模型";
    return "触觉产物";
  }

  function createJobMessage(jobId, draft, draftCard) {
    showConversation();
    const { wrap, body } = agentMessage();
    agentText(body, "收到。现在开始生成 STL、触觉预览、合规报告、讲解音频和二维码；完成后可以直接下载。");
    const card = document.createElement("section");
    card.className = "card";
    body.appendChild(card);
    const ui = {
      jobId,
      body,
      card,
      draftCard,
      subject: draft.subject,
      tab: "preview",
      job: { status: "queued", progress: 0, message: "任务已排队" },
      details: null,
      detailsPromise: null,
      signature: "",
      pollErrors: 0,
      viewer: null,
      wrap,
    };
    card.addEventListener("click", (event) => handleJobClick(event, ui));
    state.activeJob = ui;
    renderJob(ui, true);
    scrollToEnd();
    return ui;
  }

  function jobStepIndex(progress) {
    const value = Number(progress || 0);
    if (value >= 90) return 4;
    if (value >= 72) return 3;
    if (value >= 45) return 2;
    if (value >= 10) return 1;
    return 0;
  }

  function stepperHtml(job) {
    const labels = ["读取草稿", "几何建模", "讲解与二维码", "合规与清单"];
    const active = jobStepIndex(job.progress);
    return `
      <div class="stepper">
        ${labels.map((label, index) => {
          let className = "";
          if (job.status === "failed" && index === Math.min(active, labels.length - 1)) className = "failed";
          else if (job.status === "done" || index < active) className = "done";
          else if (index === active) className = "active";
          return `<div class="step ${className}"><i>${index + 1}</i><span>${label}</span></div>`;
        }).join("")}
      </div>
    `;
  }

  function jobBadges(job) {
    const badges = [];
    if (job.mode) badges.push(`<span class="pill info">${escapeHtml(MODE_LABEL[job.mode] || job.mode)}</span>`);
    if (job.status === "done") {
      badges.push(job.printable
        ? '<span class="pill good">可打印 · 无 FAIL</span>'
        : '<span class="pill bad">存在阻断打印的 FAIL</span>');
      badges.push(job.standards_clear
        ? '<span class="pill good">完全合规</span>'
        : '<span class="pill warn">未达完全合规 · 见报告</span>');
    }
    if (Array.isArray(job.artifacts) && job.artifacts.length) {
      badges.push(`<span class="pill">${job.artifacts.length} 个文件</span>`);
    }
    return badges.join("");
  }

  function renderJob(ui, force = false) {
    const job = ui.job || {};
    const signature = [
      job.status, job.progress, job.message, job.title, job.mode,
      job.printable, job.standards_clear,
      Array.isArray(job.artifacts) ? job.artifacts.length : 0,
      ui.tab,
    ].join("|");
    if (!force && signature === ui.signature) return;
    if (ui.viewer) closeViewer(ui);
    ui.signature = signature;

    const progress = job.status === "done" || job.status === "failed"
      ? 100
      : Math.max(0, Math.min(100, Number(job.progress || 0)));
    const statusLabel = { queued: "排队", building: "建模中", done: "完成", failed: "失败" }[job.status] || job.status;
    const statusClass = job.status === "failed" ? "bad" : job.status === "done" ? "good" : "warn";
    const title = job.title || jobTitle(ui.subject, job.mode);
    const message = job.message || (job.status === "done" ? "全部产物已写入作业目录" : "正在处理");

    let content = `
      <div class="job-top">
        <div>
          <span class="eyebrow">STEP 03 · 触觉产物</span>
          <h3>${escapeHtml(title)}</h3>
          <p>作业 ${escapeHtml(ui.jobId)} · ${escapeHtml(message)}</p>
        </div>
        <span class="pill ${statusClass}">${escapeHtml(statusLabel)}</span>
      </div>
      ${stepperHtml(job)}
      <div class="progress-line"><i style="width:${progress}%"></i></div>
      <div class="progress-meta"><span>${escapeHtml(message)}</span><span>${progress}%</span></div>
      <div class="job-meta">${jobBadges(job)}</div>
    `;

    if (job.status === "failed") {
      content += `
        <div class="callout bad" style="margin-top:12px">
          <strong>生成没有完成。</strong><br>
          ${escapeHtml(job.error || message)}
        </div>
        <div class="action-row">
          <button class="btn" type="button" data-scroll-draft>${icon("refresh")}回到草稿重试</button>
          <a class="btn ghost" href="/files/${encodeURIComponent(ui.jobId)}/error.txt" target="_blank" rel="noopener">查看 error.txt</a>
        </div>
      `;
    } else if (job.status === "done") {
      content += `
        <div class="tabs" role="tablist">
          ${JOB_TABS.map(([key, label]) => `<button class="tab ${ui.tab === key ? "active" : ""}" type="button" data-job-tab="${key}">${label}</button>`).join("")}
        </div>
        <div class="panel" data-panel></div>
      `;
    } else {
      content += `
        <div class="building-note">
          正在生成 STL、触觉预览、合规报告、讲解音频与二维码。这个页面会自动刷新。
        </div>
        <div class="skeleton"></div>
        <div class="skeleton short"></div>
      `;
    }

    ui.card.innerHTML = content;
    if (job.status === "done") {
      renderJobPanel(ui);
      ensureJobDetails(ui);
    }
  }

  function loadingPanel(text) {
    return `<div class="building-note">${escapeHtml(text || "正在读取作业文件…")}</div><div class="skeleton"></div>`;
  }

  function evidencePanel(ui) {
    const manifest = ui.details && ui.details.manifest;
    if (!manifest) return "";
    const artifacts = Array.isArray(manifest.artifacts) ? manifest.artifacts : [];
    const stl = artifacts.find((item) => item && item.name === "model.stl")
      || artifacts.find((item) => item && /\.stl$/i.test(item.name));
    const software = manifest.software || {};
    const fields = (manifest.input && manifest.input.fields) || {};
    const extraction = fields.extraction || {};
    const sourceParts = [];
    if (extraction.provider) sourceParts.push(extraction.provider);
    if (extraction.model) sourceParts.push(extraction.model);
    const source = sourceParts.length ? sourceParts.join(" / ") : "本地解析或图形编辑器";
    const report = ui.details.report || {};
    const verdict = report.standards_clear
      ? "完全合规"
      : report.printable
        ? "可打印，存在偏离项"
        : "未通过";
    const hash = (stl && stl.sha256) || "";
    return `
      <section class="evidence-card">
        <div class="evidence-head">
          <span class="eyebrow">MANUFACTURING EVIDENCE</span>
          <strong>这块板不是大模型画出来的 mesh</strong>
        </div>
        <p class="evidence-note">大模型只负责理解与结构化；毫米坐标、网格和 PASS/FAIL 由本地确定性内核计算。下面这些值可以复核。</p>
        <div class="evidence-grid">
          <div><span>语义来源</span><b>${escapeHtml(source)}</b></div>
          <div><span>几何内核</span><b>TouchLearn ${escapeHtml(software.agent_version || "—")} · Python ${escapeHtml(software.python || "—")}</b></div>
          <div><span>STL SHA-256</span><b class="mono" title="${escapeHtml(hash)}">${escapeHtml(hash ? `${hash.slice(0, 16)}…` : "未找到")}</b></div>
          <div><span>合规判定</span><b>${escapeHtml(verdict)}</b></div>
        </div>
        <div class="evidence-actions">
          <button class="btn ghost" type="button" data-verify-artifacts>${icon("check")}校验产物完整性</button>
          <span class="hint-line" data-verify-result>对照 manifest.json 的 SHA-256 检查文件是否被截断或篡改。</span>
        </div>
      </section>`;
  }

  function previewPanel(ui) {
    const base = `/files/${encodeURIComponent(ui.jobId)}`;
    const mode = ui.job.mode;
    const hint = mode === "tactile_plate"
      ? "A5 触觉板：浮雕面朝上、底座贴床，勿开启顶面熨烫。预览 SVG 与 STL 来自同一份几何，不是另画的示意图。"
      : "立体模型：预览为等轴测投影；切片时请检查支撑与摆放方向。预览与 STL 来自同一份几何。";
    return `
      <div class="preview-frame"><img src="${base}/preview.svg" alt="触觉产物预览"></div>
      <div class="action-row">
        <a class="btn primary" href="${base}/model.stl?download=1" download="touchlearn-${ui.jobId}-model.stl">${icon("download")}下载 STL</a>
        <a class="btn" href="/api/jobs/${encodeURIComponent(ui.jobId)}/bundle.zip" download="touchlearn-${ui.jobId}.zip">${icon("download")}下载整包 ZIP</a>
        <button class="btn" type="button" data-open-viewer>${icon("cube")}三维旋转查看</button>
        <a class="btn" href="${base}/compliance.md" target="_blank" rel="noopener">${icon("external")}打开合规报告</a>
      </div>
      ${evidencePanel(ui)}
      <div data-viewer-slot></div>
      <p class="hint-line">${escapeHtml(hint)}</p>
      <p class="hint-line">STL 是二进制网格文件，请用 Bambu Studio、PrusaSlicer 或 Cura 打开；ZIP 是标准压缩包。Word 和 VS Code 不是查看这两种文件的工具。</p>
    `;
  }

  function compliancePanel(ui) {
    if (!ui.details) {
      ensureJobDetails(ui);
      return loadingPanel("正在读取合规报告…");
    }
    const report = ui.details.report;
    if (!report) {
      return `
        <div class="callout warn"><strong>合规报告还没准备好。</strong> 可以稍后重试，或直接打开 compliance.md。</div>
        <div class="action-row">
          <button class="btn" type="button" data-reload-details>${icon("refresh")}重新读取</button>
          <a class="btn" href="/files/${encodeURIComponent(ui.jobId)}/compliance.md" target="_blank" rel="noopener">打开 compliance.md</a>
        </div>
      `;
    }
    const counts = report.counts || {};
    const order = ["PASS", "FAIL", "DEVIATION", "NEEDS_REVIEW", "NOT_APPLICABLE"];
    const pills = order
      .filter((status) => counts[status])
      .map((status) => {
        const kind = status === "FAIL" ? "bad" : status === "PASS" ? "good" : status === "NOT_APPLICABLE" ? "" : "warn";
        return `<span class="pill ${kind}">${escapeHtml(STATUS_LABEL[status] || status)} ${counts[status]}</span>`;
      }).join("");
    const note = report.printable
      ? (report.standards_clear
        ? '<div class="callout good">几何与工艺检查全部通过，可以进入切片打印。</div>'
        : '<div class="callout warn"><strong>可打印，但还不是“完全合规”。</strong> 报告里存在工艺偏离或待人工复核项；交付给教学场景前请逐条确认。</div>')
      : '<div class="callout bad"><strong>存在阻断打印的 FAIL。</strong> 请先修复网格或参数，再下载 STL。</div>';
    const rows = (report.checks || []).map((check) => `
      <tr>
        <td><span class="status-tag ${escapeHtml(check.status)}">${escapeHtml(STATUS_LABEL[check.status] || check.status)}</span></td>
        <td class="mono">${escapeHtml(check.rule_id)}</td>
        <td>${escapeHtml(check.measured)}</td>
        <td>${escapeHtml(check.message)}</td>
      </tr>
    `).join("");
    const standards = (report.standards || []).map((standard) => `
      <details class="standard-item">
        <summary>
          <span class="standard-number">${escapeHtml(standard.number)}</span>
          <span class="standard-title">${escapeHtml(standard.title)}</span>
          <span class="standard-status">${escapeHtml(standard.status)} · ${escapeHtml(standard.effective || "")}</span>
        </summary>
        <div class="standard-body">
          ${escapeHtml(standard.scope || "")}
          ${standard.note ? `<br><br>${escapeHtml(standard.note)}` : ""}
          <br><br><a href="${escapeHtml(standard.url)}" target="_blank" rel="noopener">${escapeHtml(standard.url)}</a>
        </div>
      </details>
    `).join("");
    return `
      ${note}
      <div class="pill-row" style="margin-top:9px">${pills}</div>
      <div class="check-table-wrap">
        <table class="checks">
          <thead><tr><th>状态</th><th>规则</th><th>实测</th><th>说明</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="standard-list">${standards}</div>
      <p class="hint-line"><strong>可打印</strong>只表示没有 FAIL；<strong>完全合规</strong>还要求没有工艺偏离和待人工复核项。内部检查通过不等于国家标准认证。</p>
    `;
  }

  function audioPanel(ui) {
    if (!ui.details) {
      ensureJobDetails(ui);
      return loadingPanel("正在读取讲解稿与音频…");
    }
    const manifest = ui.details.manifest;
    const audio = (manifest && manifest.audio) || {};
    const script = ui.details.script || "";
    const base = `/files/${encodeURIComponent(ui.jobId)}`;
    const wavReady = Boolean(audio.wav_ready);
    const player = wavReady
      ? `<audio controls preload="metadata" src="${base}/narration.wav"></audio>`
      : `
        <div class="callout">
          当前服务器没有可用的语音合成${audio.tts_message ? `（${escapeHtml(audio.tts_message)}）` : ""}。可以先用浏览器朗读讲解稿。
        </div>
        ${script ? `<div class="action-row"><button class="btn primary" type="button" data-speak>${icon("audio")}<span>朗读讲解稿</span></button></div>` : ""}
      `;
    return `
      ${player}
      <div class="action-row">
        ${wavReady ? `<a class="btn" href="${base}/narration.wav?download=1" download="touchlearn-${ui.jobId}-narration.wav">${icon("download")}下载 WAV</a>` : ""}
        <a class="btn" href="${base}/audio_script.txt?download=1" download="touchlearn-${ui.jobId}-audio_script.txt">${icon("download")}下载讲解稿</a>
        <a class="btn" href="/audio/${encodeURIComponent(ui.jobId)}/" target="_blank" rel="noopener">${icon("external")}手机版音频页</a>
      </div>
      <pre class="script-box">${escapeHtml(script || "讲解稿尚未生成。")}</pre>
      <p class="hint-line">讲解按触摸探索路径组织：先定位板面与方向，再沿曲线或结构追踪，最后报关键点与尺寸。</p>
    `;
  }

  function qrPanel(ui) {
    const base = `/files/${encodeURIComponent(ui.jobId)}`;
    const manifest = ui.details && ui.details.manifest;
    const fallback = `${(state.health && state.health.public_base_url) || location.origin}/audio/${ui.jobId}/`;
    const payload = (manifest && manifest.qr && manifest.qr.payload) || fallback;
    return `
      <div class="qr-layout">
        <div class="qr-frame"><img src="${base}/qr_card.svg" alt="讲解音频二维码卡片"></div>
        <div>
          <h3>手机扫码听讲解</h3>
          <p class="hint-line">二维码地址取自本次访问的域名。用同一网络或同一公网地址打开即可；如果服务停止，链接会失效。</p>
          <span class="qr-url">${escapeHtml(payload)}</span>
          <div class="action-row">
            <a class="btn" href="${base}/qr.png?download=1" download="touchlearn-${ui.jobId}-qr.png">${icon("download")}二维码 PNG</a>
            <a class="btn" href="${base}/qr_card.svg?download=1" download="touchlearn-${ui.jobId}-qr_card.svg">${icon("download")}二维码卡片</a>
            <button class="btn" type="button" data-copy="${escapeHtml(payload)}">${icon("copy")}复制链接</button>
          </div>
        </div>
      </div>
      <p class="hint-line">建议把二维码卡片贴在作品卡或说明页上；二维码指向本作业的讲解音频页，不需要登录。</p>
    `;
  }

  function orientationLabel(value) {
    if (!value) return "—";
    if (String(value).includes("relief face up")) return "浮雕面朝上，底座贴床";
    if (String(value).includes("as previewed")) return "按预览方向摆放，底座贴床";
    return value;
  }

  function supportsLabel(value) {
    if (!value) return "—";
    if (value === "none") return "无";
    if (String(value).includes("tree supports")) return "关节、夹爪和悬垂处使用树状支撑";
    return value;
  }

  function printNote(value) {
    if (!value) return "";
    if (String(value).includes("do not iron")) {
      return "0.2 mm 层高可保留 0.8 mm 浮雕与 0.6 mm 盲文点；不要开启顶面熨烫。";
    }
    if (String(value).includes("standards report flags overhang")) {
      return "请在切片软件中检查预览；报告把超过 5% 的悬垂标为工艺提示，而不是几何错误。";
    }
    return value;
  }

  function printPanel(ui) {
    if (!ui.details) {
      ensureJobDetails(ui);
      return loadingPanel("正在读取打印参数…");
    }
    const manifest = ui.details.manifest;
    if (!manifest) {
      return `
        <div class="callout warn">清单还没准备好。可以重新读取，或直接下载整包。</div>
        <div class="action-row">
          <button class="btn" type="button" data-reload-details>${icon("refresh")}重新读取</button>
          <a class="btn" href="/api/jobs/${encodeURIComponent(ui.jobId)}/bundle.zip" download="touchlearn-${ui.jobId}.zip">下载整包 ZIP</a>
        </div>
      `;
    }
    const print = manifest.print || {};
    const mesh = (manifest.geometry && manifest.geometry.mesh) || {};
    const size = print.size_mm || mesh.size_mm;
    const items = [
      ["尺寸 / mm", Array.isArray(size) ? size.map((value) => fmtNumber(value, 1)).join(" × ") : "—"],
      ["摆放方向", orientationLabel(print.orientation)],
      ["喷嘴", print.nozzle_mm ? `${print.nozzle_mm} mm` : "—"],
      ["层高", print.layer_height_mm ? `${print.layer_height_mm} mm` : "—"],
      ["墙线", print.perimeters ? `${print.perimeters} 圈` : "—"],
      ["填充", print.infill_percent ? `${print.infill_percent}%` : "—"],
      ["支撑", supportsLabel(print.supports)],
      ["三角面", mesh.triangles ?? "—"],
    ];
    return `
      <div class="print-grid">
        ${items.map(([key, value]) => `<div class="print-item"><span class="k">${escapeHtml(key)}</span><span class="v">${escapeHtml(value)}</span></div>`).join("")}
      </div>
      <p class="hint-line">${escapeHtml(printNote(print.notes) || "请按切片软件预览确认摆放方向与支撑。")}</p>
      <div class="action-row">
        <a class="btn" href="/files/${encodeURIComponent(ui.jobId)}/README.txt?download=1" download="touchlearn-${ui.jobId}-README.txt">${icon("download")}下载打印说明</a>
        <a class="btn" href="/api/jobs/${encodeURIComponent(ui.jobId)}/bundle.zip" download="touchlearn-${ui.jobId}.zip">${icon("download")}下载整包 ZIP</a>
      </div>
      <p class="hint-line">本智能体不直接控制打印机；STL 可导入 Bambu Studio、PrusaSlicer、Cura 等切片软件。若模型有悬垂，报告会标记为工艺提示而不是几何错误。</p>
    `;
  }

  function renderJobPanel(ui) {
    const panel = ui.card.querySelector("[data-panel]");
    if (!panel) return;
    // 三维视图是独立 WebGL 上下文，重绘 panel 会直接把它从 DOM 里摘掉。
    // 详情异步返回时不要覆盖正在查看的视图，等用户关闭后再刷新。
    if (ui.viewer) return;
    if (ui.tab === "preview") panel.innerHTML = previewPanel(ui);
    else if (ui.tab === "compliance") panel.innerHTML = compliancePanel(ui);
    else if (ui.tab === "audio") panel.innerHTML = audioPanel(ui);
    else if (ui.tab === "qr") panel.innerHTML = qrPanel(ui);
    else panel.innerHTML = printPanel(ui);
  }

  function handleJobClick(event, ui) {
    const tab = event.target.closest("[data-job-tab]");
    if (tab) {
      ui.tab = tab.dataset.jobTab;
      renderJob(ui, true);
      return;
    }
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      copyText(copy.dataset.copy || "");
      return;
    }
    if (event.target.closest("[data-open-viewer]")) {
      openViewer(ui);
      return;
    }
    const verify = event.target.closest("[data-verify-artifacts]");
    if (verify) {
      verifyArtifacts(ui, verify);
      return;
    }
    if (event.target.closest("[data-close-viewer]")) {
      closeViewer(ui);
      return;
    }
    if (event.target.closest("[data-speak]")) {
      toggleSpeak(ui);
      return;
    }
    if (event.target.closest("[data-scroll-draft]")) {
      if (ui.draftCard) ui.draftCard.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (event.target.closest("[data-reload-details]")) {
      ui.details = null;
      ui.detailsPromise = null;
      ensureJobDetails(ui);
      renderJobPanel(ui);
    }
  }

  async function verifyArtifacts(ui, button) {
    const result = button.parentElement.querySelector("[data-verify-result]");
    button.disabled = true;
    if (result) result.textContent = "正在核对 manifest.json 里的 SHA-256…";
    try {
      const data = await fetchJSON(`/api/jobs/${encodeURIComponent(ui.jobId)}/verify`);
      if (result) {
        if (data.ok) {
          result.textContent = `${data.checked} 个文件哈希一致，产物未被截断或篡改。`;
        } else {
          const names = (data.mismatches || []).map((item) => item.name).filter(Boolean);
          result.textContent = `校验失败：${names.length ? names.join("、") : "有文件缺失"}。建议重新生成。`;
        }
      }
    } catch (error) {
      if (result) result.textContent = `校验失败：${error.message || "网络错误"}`;
    } finally {
      button.disabled = false;
    }
  }

  function ensureJobDetails(ui) {
    if (ui.details || ui.detailsPromise) return ui.detailsPromise || ui.details;
    const base = `/files/${encodeURIComponent(ui.jobId)}`;
    ui.detailsPromise = (async () => {
      const [report, manifest, script] = await Promise.all([
        fetchJSON(`/api/jobs/${encodeURIComponent(ui.jobId)}/report`).catch(() => null),
        fetchJSON(`${base}/manifest.json`).catch(() => null),
        fetch(`${base}/audio_script.txt`, { credentials: "same-origin" })
          .then((response) => (response.ok ? response.text() : ""))
          .catch(() => ""),
      ]);
      ui.details = { report, manifest, script: script || "" };
      renderJobPanel(ui);
    })().finally(() => {
      ui.detailsPromise = null;
    });
    return ui.detailsPromise;
  }

  function startPolling(ui) {
    stopPolling();
    state.activeJob = ui;
    const tick = async () => {
      try {
        const job = await fetchJSON(`/api/jobs/${encodeURIComponent(ui.jobId)}`);
        ui.job = job;
        ui.pollErrors = 0;
        renderJob(ui);
        if (job.status === "done") {
          stopPolling();
          await ensureJobDetails(ui);
        } else if (job.status === "failed") {
          stopPolling();
        }
      } catch (error) {
        if (error instanceof AuthError) {
          stopPolling();
          showLogin();
          return;
        }
        ui.pollErrors += 1;
        if (ui.pollErrors > 10) {
          stopPolling();
          agentText(ui.body, `作业状态读取失败：${error.message || "未知错误"}`, true);
        }
      }
    };
    state.timer = setInterval(tick, 700);
    tick();
  }

  function stopPolling() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }

  /* ------------------------------------------------------------------ */
  /* STL viewer                                                          */
  /* ------------------------------------------------------------------ */

  function parseBinarySTL(buffer) {
    const view = new DataView(buffer);
    if (buffer.byteLength < 84) return null;
    const count = view.getUint32(80, true);
    if (84 + count * 50 !== buffer.byteLength) return null;
    const positions = new Float32Array(count * 9);
    let offset = 84;
    for (let index = 0; index < count; index += 1) {
      offset += 12;
      for (let vertex = 0; vertex < 3; vertex += 1) {
        positions[index * 9 + vertex * 3] = view.getFloat32(offset, true);
        positions[index * 9 + vertex * 3 + 1] = view.getFloat32(offset + 4, true);
        positions[index * 9 + vertex * 3 + 2] = view.getFloat32(offset + 8, true);
        offset += 12;
      }
      offset += 2;
    }
    return { positions, count };
  }

  function parseAsciiSTL(text) {
    const values = [];
    const regex = /vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)/g;
    let match;
    while ((match = regex.exec(text))) {
      values.push(Number(match[1]), Number(match[2]), Number(match[3]));
    }
    if (values.length < 9) return null;
    const count = Math.floor(values.length / 9);
    return { positions: new Float32Array(values.slice(0, count * 9)), count };
  }

  async function loadSTL(url) {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error("无法读取 STL 文件");
    const buffer = await response.arrayBuffer();
    let geometry = parseBinarySTL(buffer);
    if (!geometry) {
      const text = new TextDecoder("utf-8", { fatal: false }).decode(buffer);
      geometry = parseAsciiSTL(text);
    }
    if (!geometry || !geometry.count) throw new Error("STL 解析失败或文件为空");
    return geometry;
  }

  async function openViewer(ui) {
    const slot = ui.card.querySelector("[data-viewer-slot]");
    if (!slot) return;
    if (ui.viewer) {
      closeViewer(ui);
      return;
    }
    slot.innerHTML = `
      <div class="viewer-wrap">
        <div class="viewer-toolbar">
          <button class="viewer-tool" type="button" data-viewer-zoom-out aria-label="缩小" title="缩小">${icon("minus")}</button>
          <button class="viewer-tool" type="button" data-viewer-zoom-in aria-label="放大" title="放大">${icon("plus")}</button>
          <button class="viewer-tool" type="button" data-viewer-reset aria-label="重置视角" title="重置视角">${icon("refresh")}</button>
          <button class="viewer-tool" type="button" data-viewer-fullscreen aria-label="全屏查看" title="全屏查看">${icon("expand")}</button>
          <button class="viewer-tool viewer-close" type="button" data-close-viewer aria-label="关闭三维视图" title="关闭">${icon("close")}</button>
        </div>
        <canvas aria-label="STL 三维预览"></canvas>
        <span class="viewer-hint">拖动旋转 · 滚轮/双指缩放 · 双击重置 · Esc 关闭</span>
        <span class="viewer-zoom" data-viewer-zoom>100%</span>
      </div>
    `;
    try {
      const geometry = await loadSTL(`/files/${encodeURIComponent(ui.jobId)}/model.stl`);
      const canvas = slot.querySelector("canvas");
      if (!canvas || !canvas.isConnected) return;
      ui.viewer = createViewer(canvas, geometry, () => closeViewer(ui));
      requestAnimationFrame(() => {
        if (!slot.isConnected || !ui.viewer) return;
        const scroller = document.querySelector("#chatScroll");
        if (!scroller) return;
        const view = scroller.getBoundingClientRect();
        const rect = slot.getBoundingClientRect();
        if (rect.bottom > view.bottom - 10) {
          scroller.scrollTo({
            top: scroller.scrollTop + rect.bottom - view.bottom + 14,
            behavior: "smooth",
          });
        } else if (rect.top < view.top + 10) {
          scroller.scrollTo({
            top: scroller.scrollTop - (view.top + 10 - rect.top),
            behavior: "smooth",
          });
        }
      });
    } catch (error) {
      slot.innerHTML = `<p class="hint-line" style="color:var(--red)">${escapeHtml(error.message)}</p>`;
      ui.viewer = null;
    }
  }

  function closeViewer(ui) {
    if (!ui) return;
    if (ui.viewer) {
      ui.viewer.destroy();
      ui.viewer = null;
    }
    const slot = ui.card.querySelector("[data-viewer-slot]");
    if (slot) slot.innerHTML = "";
  }

  function createViewer(canvas, geometry, onClose) {
    const wrap = canvas.closest(".viewer-wrap") || canvas.parentElement;
    const gl = canvas.getContext("webgl", {
      alpha: true,
      antialias: true,
      depth: true,
      premultipliedAlpha: false,
      powerPreference: "high-performance",
    }) || canvas.getContext("experimental-webgl");
    if (!gl) {
      throw new Error("当前浏览器不支持 WebGL，无法显示三维预览。请使用最新版 Chrome、Edge 或 Safari。");
    }

    const count = geometry.count;
    const positions = geometry.positions;
    const bounds = {
      minX: Infinity,
      maxX: -Infinity,
      minY: Infinity,
      maxY: -Infinity,
      minZ: Infinity,
      maxZ: -Infinity,
    };
    for (let index = 0; index < count * 9; index += 3) {
      const x = positions[index];
      const y = positions[index + 1];
      const z = positions[index + 2];
      bounds.minX = Math.min(bounds.minX, x);
      bounds.maxX = Math.max(bounds.maxX, x);
      bounds.minY = Math.min(bounds.minY, y);
      bounds.maxY = Math.max(bounds.maxY, y);
      bounds.minZ = Math.min(bounds.minZ, z);
      bounds.maxZ = Math.max(bounds.maxZ, z);
    }
    const center = {
      x: (bounds.minX + bounds.maxX) / 2,
      y: (bounds.minY + bounds.maxY) / 2,
      z: (bounds.minZ + bounds.maxZ) / 2,
    };
    const radius = Math.max(
      0.5 * Math.hypot(
        bounds.maxX - bounds.minX,
        bounds.maxY - bounds.minY,
        bounds.maxZ - bounds.minZ,
      ),
      0.001,
    );
    const corners = [];
    for (const x of [bounds.minX, bounds.maxX]) {
      for (const y of [bounds.minY, bounds.maxY]) {
        for (const z of [bounds.minZ, bounds.maxZ]) {
          corners.push([x - center.x, y - center.y, z - center.z]);
        }
      }
    }

    // 触觉板是“底板 + 凸起字/点”的结构：按面积统计 z 分布，找出面积最大的
    // 上层平面（底板顶面），把高出来的部分标成浮雕。这样即使正对俯视，
    // 字和点也能靠颜色和光照边界和底板分开。
    const relief = (() => {
      const range = bounds.maxZ - bounds.minZ;
      if (range < 0.3) return { baseZ: bounds.maxZ, height: 1 };
      const bins = 96;
      const weights = new Float64Array(bins);
      for (let tri = 0; tri < count; tri += 1) {
        const base = tri * 9;
        const ax = positions[base];
        const ay = positions[base + 1];
        const az = positions[base + 2];
        const bx = positions[base + 3];
        const by = positions[base + 4];
        const bz = positions[base + 5];
        const cx = positions[base + 6];
        const cy = positions[base + 7];
        const cz = positions[base + 8];
        const ux = bx - ax;
        const uy = by - ay;
        const uz = bz - az;
        const vx = cx - ax;
        const vy = cy - ay;
        const vz = cz - az;
        const nx = uy * vz - uz * vy;
        const ny = uz * vx - ux * vz;
        const nz = ux * vy - uy * vx;
        const area = 0.5 * Math.hypot(nx, ny, nz);
        const zc = (az + bz + cz) / 3;
        const index = Math.min(
          bins - 1,
          Math.max(0, Math.floor(((zc - bounds.minZ) / range) * bins)),
        );
        weights[index] += area;
      }
      let best = bins - 1;
      let bestWeight = -1;
      for (let index = Math.floor(bins * 0.55); index < bins; index += 1) {
        if (weights[index] > bestWeight) {
          bestWeight = weights[index];
          best = index;
        }
      }
      const baseZ = bounds.minZ + ((best + 0.5) / bins) * range;
      const height = bounds.maxZ - baseZ;
      if (height < range * 0.03) return { baseZ: bounds.maxZ, height: 1 };
      return { baseZ, height };
    })();

    const normals = new Float32Array(count * 9);
    for (let tri = 0; tri < count; tri += 1) {
      const base = tri * 9;
      const ax = positions[base + 3] - positions[base];
      const ay = positions[base + 4] - positions[base + 1];
      const az = positions[base + 5] - positions[base + 2];
      const bx = positions[base + 6] - positions[base];
      const by = positions[base + 7] - positions[base + 1];
      const bz = positions[base + 8] - positions[base + 2];
      let nx = ay * bz - az * by;
      let ny = az * bx - ax * bz;
      let nz = ax * by - ay * bx;
      const length = Math.hypot(nx, ny, nz) || 1;
      nx /= length;
      ny /= length;
      nz /= length;
      for (let vertex = 0; vertex < 3; vertex += 1) {
        normals[base + vertex * 3] = nx;
        normals[base + vertex * 3 + 1] = ny;
        normals[base + vertex * 3 + 2] = nz;
      }
    }

    const vertexSource = `
      attribute vec3 aPos;
      attribute vec3 aNormal;
      uniform vec3 uCenter;
      uniform float uYaw;
      uniform float uPitch;
      uniform float uScale;
      uniform vec2 uPan;
      uniform vec2 uResolution;
      uniform float uRadius;
      uniform float uBaseZ;
      uniform float uRelief;
      varying vec3 vNormal;
      varying float vRelief;

      void main() {
        vRelief = clamp((aPos.z - uBaseZ) / uRelief, 0.0, 1.0);
        vec3 p = aPos - uCenter;
        float cy = cos(uYaw);
        float sy = sin(uYaw);
        vec3 r = vec3(p.x * cy - p.y * sy, p.x * sy + p.y * cy, p.z);
        float cp = cos(uPitch);
        float sp = sin(uPitch);
        vec3 q = vec3(r.x, r.y * cp - r.z * sp, r.y * sp + r.z * cp);
        vec2 screen = vec2(q.x * uScale + uPan.x, q.y * uScale + uPan.y);
        gl_Position = vec4(screen * 2.0 / uResolution, -q.z / uRadius, 1.0);

        vec3 n = aNormal;
        n = vec3(n.x * cy - n.y * sy, n.x * sy + n.y * cy, n.z);
        n = vec3(n.x, n.y * cp - n.z * sp, n.y * sp + n.z * cp);
        vNormal = n;
      }
    `;
    // 触觉字和圆点只有 0.4–1.6 mm 高，纯平面着色很容易糊成一片。
    // 用 dFdx/dFdy 在面法线突变处压一条暗边，相当于给浮雕加轮廓线。
    const derivatives = gl.getExtension("OES_standard_derivatives");
    const fragmentSource = `
      ${derivatives ? "#extension GL_OES_standard_derivatives : enable" : ""}
      precision mediump float;
      varying vec3 vNormal;
      varying float vRelief;

      void main() {
        vec3 n = normalize(vNormal);
        if (!gl_FrontFacing) n = -n;
        vec3 key = normalize(vec3(-0.38, 0.52, 0.76));
        vec3 fill = normalize(vec3(0.72, -0.42, 0.48));
        float keyLight = max(dot(n, key), 0.0);
        float fillLight = max(dot(n, fill), 0.0);
        // 相机方向补一点“头灯”，避免转到背光角度时整块板发黑。
        float headlight = max(dot(n, vec3(0.0, 0.0, 1.0)), 0.0);
        float up = clamp(n.z, 0.0, 1.0);
        vec3 base = mix(vec3(0.16, 0.35, 0.28), vec3(0.60, 0.88, 0.72), up);
        base = mix(base, vec3(0.30, 0.62, 0.47), vRelief * 0.58);
        vec3 color = base * (0.26 + 0.58 * keyLight + 0.12 * fillLight + 0.16 * headlight);
        vec3 halfDir = normalize(key + vec3(0.0, 0.0, 1.0));
        color += vec3(pow(max(dot(n, halfDir), 0.0), 26.0) * 0.10);
        ${derivatives ? `
        vec3 dx = dFdx(vNormal);
        vec3 dy = dFdy(vNormal);
        float edge = clamp((length(dx) + length(dy)) * 0.95, 0.0, 0.52);
        color *= 1.0 - edge;
        ` : ""}
        gl_FragColor = vec4(color, 1.0);
      }
    `;

    function compileShader(type, source) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        const message = gl.getShaderInfoLog(shader) || "着色器编译失败";
        gl.deleteShader(shader);
        throw new Error(message);
      }
      return shader;
    }

    const program = gl.createProgram();
    gl.attachShader(program, compileShader(gl.VERTEX_SHADER, vertexSource));
    gl.attachShader(program, compileShader(gl.FRAGMENT_SHADER, fragmentSource));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(program) || "着色器链接失败");
    }
    gl.useProgram(program);

    const positionBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
    const normalBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, normalBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, normals, gl.STATIC_DRAW);

    const aPos = gl.getAttribLocation(program, "aPos");
    const aNormal = gl.getAttribLocation(program, "aNormal");
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, normalBuffer);
    gl.enableVertexAttribArray(aNormal);
    gl.vertexAttribPointer(aNormal, 3, gl.FLOAT, false, 0, 0);

    const uniforms = {
      center: gl.getUniformLocation(program, "uCenter"),
      yaw: gl.getUniformLocation(program, "uYaw"),
      pitch: gl.getUniformLocation(program, "uPitch"),
      scale: gl.getUniformLocation(program, "uScale"),
      pan: gl.getUniformLocation(program, "uPan"),
      resolution: gl.getUniformLocation(program, "uResolution"),
      radius: gl.getUniformLocation(program, "uRadius"),
      baseZ: gl.getUniformLocation(program, "uBaseZ"),
      relief: gl.getUniformLocation(program, "uRelief"),
    };

    gl.disable(gl.CULL_FACE);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.clearColor(0, 0, 0, 0);

    const MIN_ZOOM = 0.35;
    const MAX_ZOOM = 12;
    const DEFAULT_YAW = -0.3;
    const DEFAULT_PITCH = 0.46;
    const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
    let yaw = DEFAULT_YAW;
    let pitch = DEFAULT_PITCH;
    let zoom = 1;
    let panX = 0;
    let panY = 0;
    let cssWidth = 1;
    let cssHeight = 1;
    let baseScale = 1;
    let fullscreenPlaceholder = null;
    let frame = 0;
    let observer = null;
    const pointers = new Map();
    let mode = "rotate";
    let lastX = 0;
    let lastY = 0;
    let pinch = null;

    // 按默认视角下模型投影后的真实包围盒取景，而不是按包围球取景。
    // 平板类模型用包围球会浪费约 30% 的画布，字和点自然就小。
    function computeFitScale() {
      const cy = Math.cos(DEFAULT_YAW);
      const sy = Math.sin(DEFAULT_YAW);
      const cp = Math.cos(DEFAULT_PITCH);
      const sp = Math.sin(DEFAULT_PITCH);
      let maxX = 0.001;
      let maxY = 0.001;
      for (const [x, y, z] of corners) {
        const rx = x * cy - y * sy;
        const ry = x * sy + y * cy;
        maxX = Math.max(maxX, Math.abs(rx));
        maxY = Math.max(maxY, Math.abs(ry * cp - z * sp));
      }
      baseScale = Math.min(cssWidth / (2 * maxX * 1.08), cssHeight / (2 * maxY * 1.08));
      if (!Number.isFinite(baseScale) || baseScale <= 0) baseScale = 1;
    }

    function fitScale() {
      return baseScale;
    }

    function updateZoomLabel() {
      const label = wrap ? wrap.querySelector("[data-viewer-zoom]") : null;
      if (label) label.textContent = `${Math.round(zoom * 100)}%`;
    }

    function draw() {
      frame = 0;
      const scale = fitScale() * zoom;
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.useProgram(program);
      gl.uniform3f(uniforms.center, center.x, center.y, center.z);
      gl.uniform1f(uniforms.yaw, yaw);
      gl.uniform1f(uniforms.pitch, pitch);
      gl.uniform1f(uniforms.scale, scale);
      gl.uniform2f(uniforms.pan, panX, panY);
      gl.uniform2f(uniforms.resolution, cssWidth, cssHeight);
      gl.uniform1f(uniforms.radius, radius);
      gl.uniform1f(uniforms.baseZ, relief.baseZ);
      gl.uniform1f(uniforms.relief, relief.height);
      gl.drawArrays(gl.TRIANGLES, 0, count * 3);
      updateZoomLabel();
    }

    function scheduleDraw() {
      if (frame) return;
      frame = requestAnimationFrame(draw);
    }

    function resize() {
      const rect = canvas.getBoundingClientRect();
      const width = Math.max(1, rect.width);
      const height = Math.max(1, rect.height);
      // 即使屏幕是 1x，也用 2x 超采样，再交给浏览器缩小；
      // 触觉字和盲文点的边缘会明显更干净。
      const dpr = Math.min(Math.max(window.devicePixelRatio || 1, 2), 2.5);
      const maxPixels = 12000000;
      const scale = Math.max(0.75, Math.min(dpr, Math.sqrt(maxPixels / (width * height))));
      const pixelWidth = Math.max(1, Math.round(width * scale));
      const pixelHeight = Math.max(1, Math.round(height * scale));
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      cssWidth = width;
      cssHeight = height;
      computeFitScale();
      gl.viewport(0, 0, pixelWidth, pixelHeight);
      scheduleDraw();
    }

    function pointerDown(event) {
      canvas.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 1) {
        mode = "rotate";
        lastX = event.clientX;
        lastY = event.clientY;
        pinch = null;
      } else if (pointers.size === 2) {
        const [first, second] = Array.from(pointers.values());
        mode = "pinch";
        pinch = {
          distance: Math.hypot(first.x - second.x, first.y - second.y) || 1,
          zoom,
          panX,
          panY,
          midX: (first.x + second.x) / 2,
          midY: (first.y + second.y) / 2,
        };
      }
    }

    function pointerMove(event) {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (mode === "pinch" && pointers.size >= 2 && pinch) {
        const [first, second] = Array.from(pointers.values());
        const distance = Math.hypot(first.x - second.x, first.y - second.y) || 1;
        const midX = (first.x + second.x) / 2;
        const midY = (first.y + second.y) / 2;
        const nextZoom = clamp(pinch.zoom * (distance / pinch.distance), MIN_ZOOM, MAX_ZOOM);
        const ratio = nextZoom / pinch.zoom;
        zoom = nextZoom;
        panX = pinch.panX * ratio + (midX - cssWidth / 2) - (pinch.midX - cssWidth / 2) * ratio;
        panY = pinch.panY * ratio + (cssHeight / 2 - midY) - (cssHeight / 2 - pinch.midY) * ratio;
        scheduleDraw();
        return;
      }
      if (mode === "rotate" && pointers.size === 1) {
        const dx = event.clientX - lastX;
        const dy = event.clientY - lastY;
        lastX = event.clientX;
        lastY = event.clientY;
        yaw += dx * 0.008;
        pitch = clamp(pitch + dy * 0.008, -1.5, 1.5);
        scheduleDraw();
      }
    }

    function pointerUp(event) {
      pointers.delete(event.pointerId);
      try {
        canvas.releasePointerCapture(event.pointerId);
      } catch (error) {
        /* pointer may already be released */
      }
      if (!pointers.size) {
        mode = "rotate";
        pinch = null;
        return;
      }
      if (pointers.size === 1) {
        const [remaining] = Array.from(pointers.values());
        mode = "rotate";
        lastX = remaining.x;
        lastY = remaining.y;
        pinch = null;
      }
    }

    function onWheel(event) {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mouseX = event.clientX - rect.left;
      const mouseY = event.clientY - rect.top;
      const oldScale = fitScale() * zoom;
      if (!oldScale) return;
      const worldX = (mouseX - cssWidth / 2 - panX) / oldScale;
      const worldY = (cssHeight / 2 - mouseY - panY) / oldScale;
      const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
      const nextZoom = clamp(zoom * Math.exp(-delta * 0.0018), MIN_ZOOM, MAX_ZOOM);
      const newScale = fitScale() * nextZoom;
      panX = mouseX - cssWidth / 2 - worldX * newScale;
      panY = cssHeight / 2 - mouseY - worldY * newScale;
      zoom = nextZoom;
      scheduleDraw();
    }

    function zoomBy(factor) {
      const nextZoom = clamp(zoom * factor, MIN_ZOOM, MAX_ZOOM);
      if (nextZoom === zoom) return;
      const ratio = nextZoom / zoom;
      panX *= ratio;
      panY *= ratio;
      zoom = nextZoom;
      scheduleDraw();
    }

    function resetView() {
      yaw = DEFAULT_YAW;
      pitch = DEFAULT_PITCH;
      zoom = 1;
      panX = 0;
      panY = 0;
      computeFitScale();
      scheduleDraw();
    }

    // .msg 的入场动画带 transform + fill-mode，会让 position:fixed 相对 .msg
    // 而不是视口定位。全屏时必须把 viewer 临时挂到 body 上，退出时放回原位。
    function enterFullscreen() {
      if (!wrap || !wrap.parentNode) return;
      if (!fullscreenPlaceholder) {
        fullscreenPlaceholder = document.createComment("viewer-fullscreen-placeholder");
        wrap.parentNode.insertBefore(fullscreenPlaceholder, wrap);
      }
      document.body.appendChild(wrap);
      wrap.classList.add("is-fullscreen");
      document.body.classList.add("viewer-lock");
      resetView();
      requestAnimationFrame(() => {
        resize();
        requestAnimationFrame(resize);
      });
    }

    function exitFullscreen() {
      if (!wrap) return;
      wrap.classList.remove("is-fullscreen");
      document.body.classList.remove("viewer-lock");
      if (fullscreenPlaceholder && fullscreenPlaceholder.parentNode) {
        fullscreenPlaceholder.parentNode.replaceChild(wrap, fullscreenPlaceholder);
      } else if (wrap.parentNode === document.body) {
        wrap.remove();
      }
      fullscreenPlaceholder = null;
      resetView();
      requestAnimationFrame(resize);
    }

    function toggleFullscreen() {
      if (!wrap) return;
      const fullscreen = !wrap.classList.contains("is-fullscreen");
      if (fullscreen) enterFullscreen();
      else exitFullscreen();
      const button = wrap.querySelector("[data-viewer-fullscreen]");
      if (button) {
        button.innerHTML = icon(fullscreen ? "contract" : "expand");
        const label = fullscreen ? "退出全屏" : "全屏查看";
        button.setAttribute("aria-label", label);
        button.title = label;
      }
      requestAnimationFrame(resize);
    }

    function onKeyDown(event) {
      if (event.key !== "Escape") return;
      if (wrap && wrap.classList.contains("is-fullscreen")) {
        toggleFullscreen();
        return;
      }
      const close = wrap ? wrap.querySelector("[data-close-viewer]") : null;
      if (close) close.click();
    }

    function onDoubleClick(event) {
      event.preventDefault();
      resetView();
    }

    const zoomIn = wrap ? wrap.querySelector("[data-viewer-zoom-in]") : null;
    const zoomOut = wrap ? wrap.querySelector("[data-viewer-zoom-out]") : null;
    const reset = wrap ? wrap.querySelector("[data-viewer-reset]") : null;
    const fullscreen = wrap ? wrap.querySelector("[data-viewer-fullscreen]") : null;
    const closeButton = wrap ? wrap.querySelector("[data-close-viewer]") : null;
    const onZoomIn = () => zoomBy(1.3);
    const onZoomOut = () => zoomBy(1 / 1.3);
    const onReset = () => resetView();
    const onFullscreen = () => toggleFullscreen();
    // 全屏时 viewer 被移到 body，卡片上的点击委托收不到事件，所以直接监听。
    const onCloseClick = (event) => {
      event.stopPropagation();
      if (typeof onClose === "function") onClose();
    };
    if (zoomIn) zoomIn.addEventListener("click", onZoomIn);
    if (zoomOut) zoomOut.addEventListener("click", onZoomOut);
    if (reset) reset.addEventListener("click", onReset);
    if (fullscreen) fullscreen.addEventListener("click", onFullscreen);
    if (closeButton) closeButton.addEventListener("click", onCloseClick);

    canvas.addEventListener("pointerdown", pointerDown);
    canvas.addEventListener("pointermove", pointerMove);
    canvas.addEventListener("pointerup", pointerUp);
    canvas.addEventListener("pointercancel", pointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDoubleClick);
    document.addEventListener("keydown", onKeyDown);
    if ("ResizeObserver" in window) {
      observer = new ResizeObserver(() => resize());
      observer.observe(canvas);
    } else {
      window.addEventListener("resize", resize);
    }
    resize();

    return {
      destroy() {
        if (observer) observer.disconnect();
        window.removeEventListener("resize", resize);
        document.removeEventListener("keydown", onKeyDown);
        canvas.removeEventListener("pointerdown", pointerDown);
        canvas.removeEventListener("pointermove", pointerMove);
        canvas.removeEventListener("pointerup", pointerUp);
        canvas.removeEventListener("pointercancel", pointerUp);
        canvas.removeEventListener("wheel", onWheel);
        canvas.removeEventListener("dblclick", onDoubleClick);
        if (zoomIn) zoomIn.removeEventListener("click", onZoomIn);
        if (zoomOut) zoomOut.removeEventListener("click", onZoomOut);
        if (reset) reset.removeEventListener("click", onReset);
        if (fullscreen) fullscreen.removeEventListener("click", onFullscreen);
        if (closeButton) closeButton.removeEventListener("click", onCloseClick);
        if (wrap && wrap.classList.contains("is-fullscreen")) {
          wrap.classList.remove("is-fullscreen");
        }
        document.body.classList.remove("viewer-lock");
        if (fullscreenPlaceholder && fullscreenPlaceholder.parentNode) {
          if (wrap) fullscreenPlaceholder.parentNode.replaceChild(wrap, fullscreenPlaceholder);
          else fullscreenPlaceholder.remove();
        } else if (wrap && wrap.parentNode === document.body) {
          wrap.remove();
        }
        fullscreenPlaceholder = null;
        if (frame) cancelAnimationFrame(frame);
      },
    };
  }

  /* ------------------------------------------------------------------ */
  /* Browser speech fallback                                             */
  /* ------------------------------------------------------------------ */

  function stopSpeaking() {
    if (!state.speaking) return;
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    const label = state.speaking.label;
    if (label) label.textContent = "朗读讲解稿";
    state.speaking = null;
  }

  function toggleSpeak(ui) {
    const script = ui.details && ui.details.script;
    if (!script) {
      toast("讲解稿还没有生成。", true);
      return;
    }
    if (!("speechSynthesis" in window)) {
      toast("当前浏览器不支持语音朗读。", true);
      return;
    }
    if (state.speaking) {
      stopSpeaking();
      return;
    }
    const button = ui.card.querySelector("[data-speak]");
    const label = button ? button.querySelector("span") : null;
    const utterance = new SpeechSynthesisUtterance(script);
    utterance.lang = "zh-CN";
    utterance.rate = 0.95;
    const reset = () => {
      if (state.speaking && state.speaking.utterance === utterance) {
        state.speaking = null;
        if (label) label.textContent = "朗读讲解稿";
      }
    };
    utterance.onend = reset;
    utterance.onerror = reset;
    if (label) label.textContent = "停止朗读";
    state.speaking = { utterance, label };
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  }

  /* ------------------------------------------------------------------ */
  /* Templates, settings, sheets                                         */
  /* ------------------------------------------------------------------ */

  function renderTemplateList() {
    const list = $("#templateList");
    if (!list) return;
    if (!state.templates.length) {
      list.innerHTML = '<p class="sheet-lead">模板尚未加载。请检查服务连接或访问码。</p>';
      return;
    }
    list.innerHTML = state.templates.map((entry) => `
      <button class="template-card" type="button" data-template="${escapeHtml(entry.id)}">
        ${entry.thumbnail ? `<img class="template-thumb" src="${escapeHtml(entry.thumbnail)}?crop=content" alt="${escapeHtml(entry.label)} 预览" loading="lazy">` : ""}
        <strong>${escapeHtml(entry.label)}${entry.editor === "circuit" ? " · 可编辑" : ""}</strong>
        <small>${escapeHtml(entry.description || "")}</small>
      </button>
    `).join("");
  }

  function openTemplates() {
    closeModelPopover();
    renderTemplateList();
    $("#templateSheet").classList.remove("hidden");
  }

  function chooseTemplate(templateId) {
    const entry = state.templates.find((item) => item.id === templateId);
    if (!entry) {
      toast("未找到这个模板。", true);
      return;
    }
    if (state.sending) {
      toast("上一条还在处理，请稍等。", true);
      return;
    }
    closeSheets();
    if (entry.editor === "circuit") {
      openSceneEditor({ templateId: entry.id });
      return;
    }
    clearComposer();
    const text = `使用离线模板「${entry.label}」：${entry.description || ""}。请按这个结构做成 A5 触觉板。`;
    runTurn(
      { subject: "auto", text, template: entry.id },
      { userText: `用「${entry.label}」模板做一块 A5 触觉板`, files: [] },
    );
  }

  /* ------------------------------------------------------------------ */
  /* Visual scene / circuit editor                                      */
  /* ------------------------------------------------------------------ */

  const SCENE_SIDE_ZH = { top: "上边", right: "右边", bottom: "下边", left: "左边" };

  function sceneStatus(text, kind = "") {
    const node = $("#sceneEditorStatus");
    if (!node) return;
    node.textContent = text;
    node.classList.remove("warn", "error", "good");
    if (kind) node.classList.add(kind);
  }

  function sceneBusy(busy) {
    if (!state.sceneEditor) return;
    state.sceneEditor.busy = busy;
    ["#sceneEditorApply", "#sceneInstructionBtn", "#sceneUploadBtn", "#sceneResetBtn"].forEach((selector) => {
      const node = $(selector);
      if (node) node.disabled = busy;
    });
  }

  function sceneError(error) {
    if (error instanceof AuthError) {
      showLogin();
      return "需要访问码才能使用图形编辑器。";
    }
    return error.message || "图形编辑器请求失败。";
  }

  function scenePost(url, payload) {
    return fetchJSON(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function sceneContentBox(svg) {
    const raw = (svg.dataset.contentBox || "8,24,132,142").split(",").map(Number);
    return { left: raw[0], top: raw[1], width: raw[2], height: raw[3] };
  }

  function scenePointFromEvent(svg, event) {
    if (!svg || !svg.getScreenCTM) return null;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const local = point.matrixTransform(svg.getScreenCTM().inverse());
    const box = sceneContentBox(svg);
    return {
      mm: { x: local.x, y: local.y },
      norm: {
        x: ((local.x - box.left) / box.width) * 100,
        y: ((local.y - box.top) / box.height) * 100,
      },
    };
  }

  function sceneLoopPoint(component, loop) {
    const [x0, y0, x1, y1] = loop;
    const offset = Math.max(0, Math.min(1, Number(component.offset) || 0));
    if (component.side === "top") return { x: x0 + offset * (x1 - x0), y: y0 };
    if (component.side === "right") return { x: x1, y: y0 + offset * (y1 - y0) };
    if (component.side === "bottom") return { x: x0 + offset * (x1 - x0), y: y1 };
    return { x: x0, y: y0 + offset * (y1 - y0) };
  }

  function nearestLoopPoint(point, loop) {
    const [x0, y0, x1, y1] = loop;
    const sides = [
      { side: "top", ax: x0, ay: y0, bx: x1, by: y0 },
      { side: "right", ax: x1, ay: y0, bx: x1, by: y1 },
      { side: "bottom", ax: x0, ay: y1, bx: x1, by: y1 },
      { side: "left", ax: x0, ay: y0, bx: x0, by: y1 },
    ];
    let best = null;
    for (const item of sides) {
      const dx = item.bx - item.ax;
      const dy = item.by - item.ay;
      const length2 = dx * dx + dy * dy || 1;
      let t = ((point.x - item.ax) * dx + (point.y - item.ay) * dy) / length2;
      t = Math.max(0.06, Math.min(0.94, t));
      const px = item.ax + t * dx;
      const py = item.ay + t * dy;
      const distance = Math.hypot(point.x - px, point.y - py);
      if (!best || distance < best.distance) {
        best = { side: item.side, offset: t, distance };
      }
    }
    return best;
  }

  function sceneComponent() {
    const editor = state.sceneEditor;
    if (!editor || !editor.circuit) return null;
    const components = editor.circuit.components || [];
    return components.find((item) => item.id === editor.selectedId) || components[0] || null;
  }

  function renderScenePalette() {
    const editor = state.sceneEditor;
    const palette = $("#scenePalette");
    if (!palette) return;
    if (!editor || !editor.circuit) {
      palette.innerHTML = '<p class="scene-inspector-empty">这个场景没有可识别的电路元件。</p>';
      return;
    }
    const library = Array.isArray(editor.library) ? editor.library : [];
    palette.innerHTML = library.map((item) => `
      <button type="button" data-scene-add="${escapeHtml(item.type)}" title="添加${escapeHtml(item.label_zh)}">${escapeHtml(item.label_zh)}</button>
    `).join("");
  }

  function renderSceneInspector() {
    const editor = state.sceneEditor;
    const inspector = $("#sceneInspector");
    if (!editor || !inspector) return;
    if (!editor.circuit) {
      inspector.innerHTML = '<p class="scene-inspector-empty">当前是通用场景预览，暂不支持逐块编辑。可以回到对话，用文字描述修改。</p>';
      return;
    }
    const component = sceneComponent();
    if (!component) {
      inspector.innerHTML = '<p class="scene-inspector-empty">回路里还没有元件，从下面的面板添加一个。</p>';
      return;
    }
    editor.selectedId = component.id;
    const library = Array.isArray(editor.library) ? editor.library : [];
    const spec = library.find((item) => item.type === component.type);
    const typeOptions = library.map((item) => `
      <option value="${escapeHtml(item.type)}" ${item.type === component.type ? "selected" : ""}>${escapeHtml(item.label_zh)}</option>
    `).join("");
    const sideOptions = Object.keys(SCENE_SIDE_ZH).map((side) => `
      <option value="${side}" ${component.side === side ? "selected" : ""}>${SCENE_SIDE_ZH[side]}</option>
    `).join("");
    const percent = Math.round((Number(component.offset) || 0) * 100);
    inspector.innerHTML = `
      <label class="field"><span>元件类型</span><select class="select" data-scene-type>${typeOptions}</select></label>
      <label class="field"><span>中文标签</span><input class="input" data-scene-label maxlength="24" value="${escapeHtml(component.label_zh || "")}"></label>
      ${component.type === "switch" ? `
        <label class="field"><span>开关状态</span><select class="select" data-scene-state>
          <option value="open" ${component.state !== "closed" ? "selected" : ""}>断开</option>
          <option value="closed" ${component.state === "closed" ? "selected" : ""}>闭合</option>
        </select></label>
      ` : ""}
      <label class="field"><span>所在边</span><select class="select" data-scene-side>${sideOptions}</select></label>
      <div class="scene-inspector-meta">
        <span class="pill">${SCENE_SIDE_ZH[component.side] || component.side} ${percent}%</span>
        ${spec ? `<span class="pill">${escapeHtml(spec.note || "")}</span>` : ""}
      </div>
      <div class="scene-inspector-actions">
        <button class="btn sm" type="button" data-scene-rotate="ccw">逆时针</button>
        <button class="btn sm" type="button" data-scene-rotate="cw">顺时针</button>
        <button class="btn sm ghost" type="button" data-scene-delete>删除</button>
      </div>
    `;
  }

  function sceneSelect(id) {
    const editor = state.sceneEditor;
    if (!editor) return;
    editor.selectedId = id || "";
    const stage = $("#sceneStage");
    $$(".scene-block", stage).forEach((block) => {
      block.classList.toggle("is-selected", block.dataset.blockId === editor.selectedId);
    });
    renderSceneInspector();
  }

  function applyScenePreview(result, { keepSelection = true } = {}) {
    const editor = state.sceneEditor;
    if (!editor) return;
    const previous = keepSelection ? editor.selectedId : "";
    editor.circuit = result.circuit && typeof result.circuit === "object" ? result.circuit : null;
    editor.scene = result.scene || null;
    editor.preview = result;
    editor.changes = Array.isArray(result.changes) ? result.changes : [];
    if (Array.isArray(result.library) && result.library.length) {
      const known = new Set(editor.library.map((item) => item.type));
      editor.library = result.library.some((item) => !known.has(item.type)) || !editor.library.length
        ? result.library
        : editor.library;
    }
    const components = editor.circuit ? editor.circuit.components || [] : [];
    editor.selectedId = components.some((item) => item.id === previous) ? previous : (components[0] ? components[0].id : "");
    const stage = $("#sceneStage");
    stage.innerHTML = result.svg || '<div class="scene-loading">没有收到图形。</div>';
    const title = result.scene && (result.scene.title_zh || result.scene.title);
    if (title) $("#sceneEditorTitle").textContent = title;
    renderScenePalette();
    renderSceneInspector();
    if (editor.selectedId) sceneSelect(editor.selectedId);
    const hint = $("#sceneEditHint");
    if (hint) {
      hint.textContent = result.warnings && result.warnings.length
        ? `提示：${result.warnings[0]}`
        : "导线会跟着元件重新生成，不需要手动补线。";
    }
  }

  async function sceneApplyOp(op) {
    const editor = state.sceneEditor;
    if (!editor || !editor.circuit || editor.busy) return;
    sceneBusy(true);
    sceneStatus("正在应用修改…");
    try {
      const result = await scenePost("/api/scene-edit", { circuit: editor.circuit, op });
      applyScenePreview(result, { keepSelection: true });
      editor.dirty = true;
      const message = result.changes && result.changes[0] ? result.changes[0] : "已更新电路。";
      sceneStatus(message, "good");
    } catch (error) {
      sceneStatus(sceneError(error), "error");
    } finally {
      sceneBusy(false);
    }
  }

  async function sceneApplyInstruction() {
    const editor = state.sceneEditor;
    const input = $("#sceneInstruction");
    if (!editor || !editor.circuit || editor.busy || !input) return;
    const instruction = input.value.trim();
    if (!instruction) {
      input.focus();
      return;
    }
    sceneBusy(true);
    sceneStatus("正在按这句话修改电路…");
    try {
      const result = await scenePost("/api/scene-edit", {
        circuit: editor.circuit,
        instruction,
        model: state.selection ? state.selection.model : undefined,
        effort: state.selection ? state.selection.effort : undefined,
      });
      applyScenePreview(result, { keepSelection: true });
      editor.dirty = true;
      input.value = "";
      const message = result.changes && result.changes.length ? result.changes.join(" ") : "电路已更新。";
      sceneStatus(message, "good");
    } catch (error) {
      sceneStatus(sceneError(error), "error");
    } finally {
      sceneBusy(false);
    }
  }

  async function sceneAddComponent(type) {
    const editor = state.sceneEditor;
    if (!editor || !editor.circuit || editor.busy) return;
    await sceneApplyOp({ type: "add_component", component_type: type, side: "top", offset: 0.5 });
  }

  async function resetSceneEditor() {
    const editor = state.sceneEditor;
    if (!editor || editor.busy) return;
    if (!window.confirm("恢复默认电路会覆盖当前修改，继续？")) return;
    sceneBusy(true);
    sceneStatus("正在恢复默认串联电路…");
    try {
      const result = await scenePost("/api/scene-preview", {});
      applyScenePreview(result, { keepSelection: false });
      editor.dirty = true;
      sceneStatus("已恢复默认串联电路。", "good");
    } catch (error) {
      sceneStatus(sceneError(error), "error");
    } finally {
      sceneBusy(false);
    }
  }

  async function sceneRecognizeImage(file) {
    if (!file) return;
    if (!String(file.type || "").startsWith("image/")) {
      toast("请选择 PNG、JPG 或 WebP 图片。", true);
      return;
    }
    const editor = state.sceneEditor;
    if (!editor || editor.busy) return;
    sceneBusy(true);
    sceneStatus("正在识别图片里的电路元件…");
    try {
      const data = await base64FromFile(file);
      const result = await scenePost("/api/scene-recognize", {
        files: [{ name: file.name || "circuit.png", data }],
        text: "",
      });
      if (result.kind === "circuit" && result.circuit) {
        applyScenePreview(result, { keepSelection: false });
        editor.dirty = true;
        sceneStatus(result.message || "已识别为电路图，并载入编辑器。", "good");
        const hint = $("#sceneEditHint");
        if (hint && result.questions && result.questions.length) {
          hint.textContent = `需要确认：${result.questions[0]}`;
        }
      } else {
        sceneStatus(result.message || "这张图片没有被识别为电路图。", "warn");
        const hint = $("#sceneEditHint");
        if (hint) {
          hint.textContent = "可以回到主界面上传这张图，按普通触觉示意图生成；也可以继续在这里手动搭建电路。";
        }
      }
    } catch (error) {
      sceneStatus(sceneError(error), "error");
    } finally {
      sceneBusy(false);
    }
  }

  function bindSceneStage() {
    const stage = $("#sceneStage");
    if (!stage || stage.dataset.bound === "1") return;
    stage.dataset.bound = "1";

    stage.addEventListener("pointerdown", (event) => {
      const editor = state.sceneEditor;
      if (!editor || editor.busy || !editor.circuit) return;
      if (event.button !== undefined && event.button !== 0) return;
      const block = event.target.closest(".scene-block");
      if (!block || block.dataset.kind === "wire") return;
      const id = block.dataset.blockId;
      if (!id) return;
      sceneSelect(id);
      const component = (editor.circuit.components || []).find((item) => item.id === id);
      const svg = stage.querySelector("svg.scene-svg");
      const point = scenePointFromEvent(svg, event);
      if (!component || !point || !Array.isArray(editor.circuit.loop)) return;
      const center = sceneLoopPoint(component, editor.circuit.loop);
      editor.drag = {
        pointerId: event.pointerId,
        id,
        block,
        svg,
        start: point,
        center,
        moved: false,
        target: null,
      };
      block.classList.add("is-dragging");
      stage.classList.add("is-dragging");
      try { stage.setPointerCapture(event.pointerId); } catch (error) { /* ignore */ }
      event.preventDefault();
    });

    stage.addEventListener("pointermove", (event) => {
      const editor = state.sceneEditor;
      const drag = editor && editor.drag;
      if (!drag || drag.pointerId !== event.pointerId) return;
      const point = scenePointFromEvent(drag.svg, event);
      if (!point) return;
      const dxMm = point.mm.x - drag.start.mm.x;
      const dyMm = point.mm.y - drag.start.mm.y;
      if (Math.abs(dxMm) + Math.abs(dyMm) > 1.5) drag.moved = true;
      if (!drag.moved) return;
      drag.block.setAttribute("transform", `translate(${dxMm.toFixed(3)} ${dyMm.toFixed(3)})`);
      const moved = {
        x: drag.center.x + (point.norm.x - drag.start.norm.x),
        y: drag.center.y + (point.norm.y - drag.start.norm.y),
      };
      drag.target = nearestLoopPoint(moved, editor.circuit.loop);
      if (drag.target) {
        sceneStatus(`松手放到：${SCENE_SIDE_ZH[drag.target.side]} ${Math.round(drag.target.offset * 100)}%`);
      }
      event.preventDefault();
    });

    const finishDrag = (event, cancelled = false) => {
      const editor = state.sceneEditor;
      const drag = editor && editor.drag;
      if (!drag || drag.pointerId !== event.pointerId) return;
      drag.block.removeAttribute("transform");
      drag.block.classList.remove("is-dragging");
      stage.classList.remove("is-dragging");
      editor.drag = null;
      try { stage.releasePointerCapture(event.pointerId); } catch (error) { /* ignore */ }
      if (cancelled) {
        sceneStatus("拖动已取消。", "warn");
        return;
      }
      if (!drag.moved) {
        sceneStatus("拖动元件可沿回路移动；导线会自动重画。");
        return;
      }
      if (!drag.target) {
        sceneStatus("没有找到合适的回路位置，已取消这次拖动。", "warn");
        return;
      }
      sceneApplyOp({
        type: "move_component",
        id: drag.id,
        side: drag.target.side,
        offset: Number(drag.target.offset.toFixed(3)),
      });
    };

    stage.addEventListener("pointerup", (event) => finishDrag(event, false));
    stage.addEventListener("pointercancel", (event) => finishDrag(event, true));
  }

  async function openSceneEditor({ templateId = "", circuit = null, scene = null, draft = null, card = null, prompt = "" } = {}) {
    if (state.sending) {
      toast("上一条还在处理，请稍等。", true);
      return;
    }
    if (!state.templates.length) {
      try { await loadTemplates(); } catch (error) { /* keep going with defaults */ }
    }
    let template = templateId ? state.templates.find((item) => item.id === templateId) : null;
    if (!template && templateId) {
      toast("未找到这个模板。", true);
      return;
    }
    const payload = {};
    if (circuit && typeof circuit === "object") payload.circuit = circuit;
    else if (scene && typeof scene === "object") payload.scene = scene;
    else if (template && template.circuit) payload.circuit = template.circuit;
    else if (template && template.scene) payload.scene = template.scene;

    state.sceneEditor = {
      templateId,
      prompt,
      draft,
      card,
      circuit: null,
      scene: null,
      library: [],
      selectedId: "",
      busy: false,
      drag: null,
      dirty: false,
      applied: false,
    };
    $("#sceneEditor").classList.remove("hidden");
    $("#sceneEditorApply").textContent = draft && card ? "应用到草稿" : "用这份电路生成";
    $("#sceneEditorTitle").textContent = template ? template.label : "串联电路";
    $("#sceneStage").innerHTML = '<div class="scene-loading">正在编译可编辑图形…</div>';
    $("#sceneInspector").innerHTML = "";
    $("#scenePalette").innerHTML = "";
    $("#sceneEditHint").textContent = "导线会跟着元件重新生成，不需要手动补线。";
    sceneStatus("正在加载可编辑电路…");
    sceneBusy(true);
    bindSceneStage();
    try {
      const result = await scenePost("/api/scene-preview", payload);
      applyScenePreview(result, { keepSelection: false });
      sceneStatus(
        result.changes && result.changes.length ? result.changes[0] : "拖动元件可沿回路移动；导线会自动重画。",
      );
    } catch (error) {
      $("#sceneStage").innerHTML = `<div class="scene-loading">${escapeHtml(sceneError(error))}</div>`;
      sceneStatus(sceneError(error), "error");
    } finally {
      sceneBusy(false);
    }
  }

  function closeSceneEditor({ silent = false } = {}) {
    const editor = state.sceneEditor;
    if (!editor) return;
    if (!silent && editor.dirty && !editor.applied) {
      const leave = window.confirm("关闭后，本次图形修改不会应用到草稿。继续关闭？");
      if (!leave) return;
    }
    state.sceneEditor = null;
    const overlay = $("#sceneEditor");
    if (overlay) overlay.classList.add("hidden");
    const stage = $("#sceneStage");
    if (stage) {
      stage.innerHTML = '<div class="scene-loading">正在编译可编辑图形…</div>';
      stage.classList.remove("is-dragging");
    }
    const inspector = $("#sceneInspector");
    if (inspector) inspector.innerHTML = "";
    const palette = $("#scenePalette");
    if (palette) palette.innerHTML = "";
  }

  function applySceneEditor() {
    const editor = state.sceneEditor;
    if (!editor || editor.busy) return;
    if (!editor.circuit) {
      toast("当前预览没有可编辑的电路。", true);
      return;
    }
    const circuit = JSON.parse(JSON.stringify(editor.circuit));
    const scene = editor.scene ? JSON.parse(JSON.stringify(editor.scene)) : null;
    const plain = editor.preview && editor.preview.svg_plain
      ? editor.preview.svg_plain
      : (editor.preview && editor.preview.svg) || "";
    if (editor.draft && editor.card && editor.card.isConnected) {
      const draft = editor.draft;
      draft.fields = draft.fields || {};
      draft.fields.circuit = circuit;
      if (scene) draft.fields.scene = scene;
      draft.fields.editor = "circuit";
      draft.fields.scene_svg = plain;
      if (editor.preview && editor.preview.summary) draft.fields.summary = editor.preview.summary;
      if (editor.preview && Array.isArray(editor.preview.warnings)) draft.fields.warnings = editor.preview.warnings;
      const card = editor.card;
      const titleInput = card.querySelector('[data-field="scene_title"]');
      const titleZhInput = card.querySelector('[data-field="scene_title_zh"]');
      if (titleInput && scene) titleInput.value = scene.title || "";
      if (titleZhInput && scene) titleZhInput.value = scene.title_zh || "";
      const frame = card.querySelector(".scene-preview-frame");
      const svgNode = frame ? frame.querySelector("svg") : null;
      if (svgNode && plain) {
        svgNode.outerHTML = plain;
      }
      editor.applied = true;
      closeSceneEditor({ silent: true });
      toast("已应用到草稿；生成时会按这份电路重新编译。");
      return;
    }
    const text = editor.prompt || "把这份串联电路做成 A5 触觉板，标出电池、开关、电阻和灯泡。";
    editor.applied = true;
    closeSceneEditor({ silent: true });
    runTurn(
      { subject: "scene", text, circuit, scene },
      { userText: "用图形编辑器里的串联电路做一块 A5 触觉板", files: [] },
    );
  }

  function providerText(status) {
    if (!status) return "未读取";
    if (status.mode === "locked") return "需要访问码";
    if (status.configured) {
      return `${status.label || status.provider || "已配置"} · ${status.model || ""}`.trim();
    }
    return "未配置（规则与离线模板）";
  }

  function settingRow(label, value, mono = false) {
    return `<div class="setting-row"><span class="k">${escapeHtml(label)}</span><span class="v${mono ? " mono" : ""}">${escapeHtml(value)}</span></div>`;
  }

  function renderSettings() {
    const health = state.health || {};
    const llm = health.llm || null;
    const vision = health.vision || null;
    const voices = Array.isArray(health.voices) ? health.voices : [];
    const providers = state.providers || {};
    const privacy = providers.privacy || "密钥只从服务端环境变量读取，不会返回给浏览器，也不会写入产物。";
    const voiceText = voices.length ? voices.slice(0, 3).join("、") + (voices.length > 3 ? ` 等 ${voices.length} 个` : "") : "未检测到系统语音";
    const baseUrl = health.public_base_url || location.origin;
    const models = state.models || providers.models || null;
    const selectedModel = state.selection ? modelEntry(state.selection.model) : null;
    const selectionText = models && models.configured && selectedModel
      ? `${selectedModel.label} · 思考强度 ${effortLabel(state.selection.effort)}`
      : "未配置（规则与离线模板）";
    $("#settingsBody").innerHTML = `
      <div class="setting-group">
        <h3>服务</h3>
        ${settingRow("版本", health.version || "—", true)}
        ${settingRow("当前地址", location.origin, true)}
        ${settingRow("二维码基础地址", baseUrl, true)}
        ${settingRow("局域网 IP", health.lan_ip || "—", true)}
        ${settingRow("访问控制", health.access_required ? "已启用访问码" : "未设置访问码")}
        ${health.icp ? `<div class="setting-row"><span class="k">ICP备案号</span><span class="v"><a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">${escapeHtml(health.icp)}</a></span></div>` : ""}
        <p class="setting-note">公网临时隧道地址会变化；如果换了地址，请回到本页重新复制二维码基础地址。正式部署见 docs/deploy.md。</p>
        <div class="action-row">
          <button class="btn sm" type="button" id="settingsRefresh">${icon("refresh")}重新检查服务</button>
          <button class="btn sm" type="button" data-copy="${escapeHtml(baseUrl)}">${icon("copy")}复制链接</button>
          ${health.access_required ? `<button class="btn sm ghost" type="button" id="settingsLogout">退出访问码</button>` : ""}
        </div>
      </div>
      <div class="setting-group">
        <h3>理解模型</h3>
        ${settingRow("当前选择", selectionText)}
        ${settingRow("文本理解", providerText(llm))}
        ${settingRow("图像理解", providerText(vision))}
        <p class="setting-note">${escapeHtml((llm && llm.message) || "未配置大模型时，智能体会使用规则解析和 8 个离线模板，仍然可以生成可打印的触觉板。")}</p>
        <p class="setting-note">环境变量只提供初始默认模型和初始思考强度；日常使用时，点底部模型按钮即可切换，选择会保存在本机浏览器，只影响之后的请求。</p>
        <div class="action-row">
          ${models && models.configured ? `<button class="btn sm" type="button" id="settingsModelPick">${icon("spark")}选择模型与思考强度</button>` : ""}
        </div>
        <p class="setting-note">${escapeHtml(privacy)}</p>
        <pre class="code-block"># 这几项只决定“初始默认”，用户可在底部菜单里改选
TOUCHLEARN_LLM_PROVIDER=tokenhub
TOUCHLEARN_LLM_KEY=sk-...
TOUCHLEARN_LLM_MODEL=deepseek-v4.1-flash
TOUCHLEARN_LLM_EFFORT=low

# 场景 JSON 的内部快速通道；用户选择优先
TOUCHLEARN_SCENE_MODEL=deepseek-v4.1-flash
TOUCHLEARN_SCENE_EFFORT=off

# 读图与照片理解（同一 provider 自动复用上面的 KEY）
TOUCHLEARN_VLM_PROVIDER=tokenhub
TOUCHLEARN_VLM_MODEL=deepseek/deepseek-flash</pre>
        <p class="setting-note">把变量写进服务端的 <code>.env</code> 或部署平台环境变量，然后重启服务。不要把密钥贴进聊天框。</p>
      </div>
      <div class="setting-group">
        <h3>讲解语音</h3>
        ${settingRow("系统语音", voiceText)}
        <p class="setting-note">Windows 本机优先使用系统 TTS 生成 WAV；云端 Linux 没有语音时，讲解页会自动降级为浏览器朗读，讲解稿仍可下载。</p>
      </div>
      <div class="setting-group">
        <h3>快捷入口</h3>
        <div class="action-row">
          <button class="btn sm" type="button" id="settingsTemplates">${icon("grid")}示例模板</button>
          <a class="btn sm" href="/api/health" target="_blank" rel="noopener">${icon("server")}服务状态</a>
        </div>
      </div>
    `;
  }

  function openSettings() {
    closeModelPopover();
    renderSettings();
    $("#settingsSheet").classList.remove("hidden");
  }

  function closeSheets() {
    $("#settingsSheet").classList.add("hidden");
    $("#templateSheet").classList.add("hidden");
  }

  function newChat() {
    if (state.sending) {
      toast("正在理解，请稍等。", true);
      return;
    }
    const jobStatus = state.activeJob && state.activeJob.job && state.activeJob.job.status;
    if (jobStatus === "queued" || jobStatus === "building") {
      if (!window.confirm("当前任务还在生成，开始新对话会失去进度显示。继续吗？")) return;
    }
    stopPolling();
    stopSpeaking();
    if (state.sceneEditor) closeSceneEditor({ silent: true });
    if (state.activeJob) closeViewer(state.activeJob);
    state.activeJob = null;
    state.turn += 1;
    state.sending = false;
    state.lastRun = null;
    state.pendingTemplate = "";
    $("#messages").innerHTML = "";
    $("#welcome").classList.remove("hidden");
    clearComposer();
    $("#chatScroll").scrollTop = 0;
    $("#promptInput").focus();
  }

  /* ------------------------------------------------------------------ */
  /* Event binding                                                       */
  /* ------------------------------------------------------------------ */

  function bind() {
    $("#newChatBtn").addEventListener("click", newChat);
    $("#settingsBtn").addEventListener("click", openSettings);
    $("#templateBtn").addEventListener("click", openTemplates);
    $("#modelChip").addEventListener("click", toggleModelPopover);

    $("#sceneEditorClose").addEventListener("click", () => closeSceneEditor());
    $("#sceneEditorApply").addEventListener("click", applySceneEditor);
    $("#sceneUploadBtn").addEventListener("click", () => $("#sceneImageInput").click());
    $("#sceneImageInput").addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      event.target.value = "";
      if (file) sceneRecognizeImage(file);
    });
    $("#sceneResetBtn").addEventListener("click", resetSceneEditor);
    $("#sceneInstructionBtn").addEventListener("click", sceneApplyInstruction);
    $("#sceneInstruction").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing) return;
      event.preventDefault();
      sceneApplyInstruction();
    });
    $("#scenePalette").addEventListener("click", (event) => {
      const button = event.target.closest("[data-scene-add]");
      if (button) sceneAddComponent(button.dataset.sceneAdd);
    });
    $("#sceneInspector").addEventListener("change", (event) => {
      const editor = state.sceneEditor;
      const component = sceneComponent();
      if (!editor || !component) return;
      if (event.target.matches("[data-scene-type]")) {
        const type = event.target.value;
        if (type && type !== component.type) sceneApplyOp({ type: "set_type", id: component.id, component_type: type });
        return;
      }
      if (event.target.matches("[data-scene-state]")) {
        sceneApplyOp({ type: "set_state", id: component.id, state: event.target.value });
        return;
      }
      if (event.target.matches("[data-scene-side]")) {
        sceneApplyOp({ type: "move_component", id: component.id, side: event.target.value, offset: component.offset });
        return;
      }
      if (event.target.matches("[data-scene-label]")) {
        const label = event.target.value.trim();
        if (label && label !== component.label_zh) sceneApplyOp({ type: "relabel", id: component.id, label_zh: label });
      }
    });
    $("#sceneInspector").addEventListener("click", (event) => {
      const component = sceneComponent();
      if (!component) return;
      const rotate = event.target.closest("[data-scene-rotate]");
      if (rotate) {
        sceneApplyOp({ type: "rotate_component", id: component.id, direction: rotate.dataset.sceneRotate });
        return;
      }
      if (event.target.closest("[data-scene-delete]")) {
        if (!window.confirm(`从回路中删除${component.label_zh || "这个元件"}？`)) return;
        sceneApplyOp({ type: "remove_component", id: component.id });
      }
    });

    $("#modelPopover").addEventListener("click", (event) => {
      /* Inner clicks can re-render the popover; stop here so the document-level
         outside-click handler never sees the now-detached original target. */
      event.stopPropagation();
      if (event.target.closest("[data-close-model]")) {
        closeModelPopover();
        return;
      }
      const model = event.target.closest("[data-model]");
      if (model) {
        chooseModel(model.dataset.model);
        return;
      }
      const effort = event.target.closest("[data-effort]");
      if (effort) {
        chooseEffort(effort.dataset.effort);
        return;
      }
      if (event.target.closest("[data-effort-view]")) {
        state.popoverView = "effort";
        renderModelPopover();
        return;
      }
      if (event.target.closest("[data-model-view]")) {
        state.popoverView = "models";
        renderModelPopover();
        return;
      }
      if (event.target.closest("[data-model-settings]")) {
        closeModelPopover();
        openSettings();
      }
    });
    document.addEventListener("click", (event) => {
      if (!state.popoverOpen) return;
      if (event.target.closest("#modelPopover") || event.target.closest("#modelChip")) return;
      closeModelPopover();
    });

    $("#composerForm").addEventListener("submit", handleSubmit);
    $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
    $("#fileInput").addEventListener("change", (event) => {
      addFiles(event.target.files);
      event.target.value = "";
    });
    $("#promptInput").addEventListener("input", () => {
      updateSend();
      resizeComposer();
    });
    $("#promptInput").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      $("#composerForm").requestSubmit();
    });
    $("#attachmentBar").addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-file]");
      if (!button) return;
      state.files.splice(Number(button.dataset.removeFile), 1);
      renderAttachments();
      updateSend();
    });

    const composer = $("#composerForm");
    ["dragenter", "dragover"].forEach((name) => {
      composer.addEventListener(name, (event) => {
        event.preventDefault();
        composer.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      composer.addEventListener(name, (event) => {
        event.preventDefault();
        composer.classList.remove("dragover");
      });
    });
    composer.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
    composer.addEventListener("paste", (event) => {
      if (event.clipboardData && event.clipboardData.files && event.clipboardData.files.length) {
        addFiles(event.clipboardData.files);
      }
    });

    $("#welcome").addEventListener("click", (event) => {
      const button = event.target.closest(".suggestion");
      if (!button) return;
      if (button.dataset.action === "upload") {
        $("#fileInput").click();
        return;
      }
      if (button.dataset.action === "scene-editor") {
        openSceneEditor({
          templateId: button.dataset.template || "circuit",
          prompt: button.dataset.prompt || "",
        });
        return;
      }
      const prompt = button.dataset.prompt;
      if (prompt) prefillComposer(prompt);
    });

    $("#loginForm").addEventListener("submit", submitLogin);

    $$("[data-close-sheet]").forEach((button) => {
      button.addEventListener("click", closeSheets);
    });
    [$("#settingsSheet"), $("#templateSheet")].forEach((backdrop) => {
      backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) closeSheets();
      });
    });

    $("#templateList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-template]");
      if (button) chooseTemplate(button.dataset.template);
    });

    $("#settingsBody").addEventListener("click", (event) => {
      const copy = event.target.closest("[data-copy]");
      if (copy) {
        copyText(copy.dataset.copy || "");
        return;
      }
      if (event.target.closest("#settingsLogout")) {
        logout();
        return;
      }
      if (event.target.closest("#settingsRefresh")) {
        refreshHealth().then(() => {
          loadProviders().then(() => renderSettings());
        });
        return;
      }
      if (event.target.closest("#settingsTemplates")) {
        closeSheets();
        openTemplates();
        return;
      }
      if (event.target.closest("#settingsModelPick")) {
        closeSheets();
        openModelPopover("models");
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (state.popoverOpen) {
        closeModelPopover();
        return;
      }
      if (state.sceneEditor) {
        closeSceneEditor();
        return;
      }
      if (!$("#settingsSheet").classList.contains("hidden") || !$("#templateSheet").classList.contains("hidden")) {
        closeSheets();
        return;
      }
      // 全屏时 Esc 只退出全屏，由 viewer 自己的监听处理；这里不要直接关掉。
      if (document.querySelector(".viewer-wrap.is-fullscreen")) return;
      if (state.activeJob && state.activeJob.viewer) closeViewer(state.activeJob);
    });
  }

  function boot() {
    bind();
    renderAttachments();
    resizeComposer();
    updateSend();
    refreshHealth();
    setInterval(refreshHealth, 45000);
  }

  boot();
})();
