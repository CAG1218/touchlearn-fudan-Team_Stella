/* TouchLearn interactive front end.
 * No framework, no build step: the competition machine may be offline.
 */
(() => {
  "use strict";

  const MAX_FILE = 30 * 1024 * 1024;
  const MAX_TOTAL = 80 * 1024 * 1024;
  const ACCEPT = {
    auto: ".png,.jpg,.jpeg,.webp,.bmp,.pdf,.stl,.obj,.json,.txt",
    function: ".png,.jpg,.jpeg,.webp,.bmp,.pdf",
    robot: ".png,.jpg,.jpeg,.webp,.bmp,.pdf,.stl,.obj,.json,.txt",
    mesh: ".stl,.obj",
  };
  const SUBJECT_LABEL = { auto: "任意需求", function: "函数图像", robot: "机械臂", mesh: "三维文件", scene: "触觉场景", unsupported: "暂不支持" };
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

  const state = {
    subject: "function",
    files: [],
    draft: null,
    jobId: null,
    job: null,
    timer: null,
    health: null,
    report: null,
    manifest: null,
    script: "",
    jobTab: "preview",
    viewer: null,
    providers: null,
    templates: [],
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
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
    const response = await fetch(url, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      throw new Error(payload && payload.error ? payload.error : `请求失败（${response.status}）`);
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

  /* ------------------------------------------------------------------ */
  /* Server status                                                       */
  /* ------------------------------------------------------------------ */

  async function loadHealth() {
    const pill = $("#serverPill");
    const label = $("#serverLabel");
    try {
      const health = await fetchJSON("/api/health");
      state.health = health;
      pill.classList.remove("bad");
      pill.classList.add("ok");
      let host = health.public_base_url || "";
      try { host = new URL(health.public_base_url).host; } catch (error) { /* keep raw */ }
      label.textContent = `本地智能体在线 · ${host}`;
      pill.title = `二维码地址：${health.public_base_url}\n输出目录：${health.output_root}`;
      renderProviderStatus(health.llm, health.vision);
    } catch (error) {
      pill.classList.remove("ok");
      pill.classList.add("bad");
      label.textContent = "无法连接本地智能体";
      pill.title = String(error.message || error);
      $("#providerText").textContent = "本地服务未连接";
      $("#providerVision").textContent = "启动服务后读取模型配置";
      $("#providerDot").classList.remove("ok");
    }
  }

  function renderProviderStatus(llm, vision) {
    const dot = $("#providerDot");
    const text = $("#providerText");
    const visionText = $("#providerVision");
    if (!dot || !text || !visionText) return;
    const llmReady = Boolean(llm && llm.configured);
    dot.classList.toggle("ok", llmReady);
    if (llmReady) {
      text.textContent = `理解模型：${llm.label || llm.provider || "已配置"} · ${llm.model || ""}`;
    } else {
      text.textContent = "理解模型未配置：当前使用规则/离线模板";
    }
    if (vision && vision.configured) {
      visionText.textContent = `视觉模型：${vision.label || vision.provider || "已配置"} · ${vision.model || ""}`;
    } else {
      visionText.textContent = "视觉模型未配置：图片只作为来源留档";
    }
    const provider = $("#providerStrip");
    if (provider) {
      provider.title = [
        llm && llm.message ? `文本：${llm.message}` : "",
        vision && vision.message ? `视觉：${vision.message}` : "",
        "配置方式见 docs/providers.md 或 .env.example；密钥不会写入产物。",
      ].filter(Boolean).join("\n");
    }
  }

  async function loadProviders() {
    try {
      state.providers = await fetchJSON("/api/providers");
      if (state.health) renderProviderStatus(state.health.llm, state.health.vision);
    } catch (error) {
      /* health already reports the actionable state */
    }
  }

  async function loadTemplates() {
    try {
      const payload = await fetchJSON("/api/scene-templates");
      state.templates = payload.templates || [];
      const select = $("#sceneTemplate");
      if (!select) return;
      select.innerHTML = '<option value="">让智能体判断或生成</option>' + state.templates.map((entry) => (
        `<option value="${escapeHtml(entry.id)}">${escapeHtml(entry.label)} · ${escapeHtml(entry.description || "")}</option>`
      )).join("");
    } catch (error) {
      /* offline templates are optional */
    }
  }

  /* ------------------------------------------------------------------ */
  /* Composer                                                            */
  /* ------------------------------------------------------------------ */

  function setSubject(subject) {
    state.subject = subject;
    $$("#subjectTabs .segment").forEach((button) => {
      const active = button.dataset.subject === subject;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    $$(".subject-panel").forEach((panel) => {
      panel.classList.toggle("hidden", panel.dataset.panel !== subject);
    });
    const input = $("#fileInput");
    input.accept = ACCEPT[subject];
    const copy = {
      auto: ["拖入教材图片、工程图或三维文件", "智能体自动判断路径；PNG / JPG / PDF / STL / OBJ / TXT / JSON"],
      function: ["拖入函数图片或 PDF", "也可以只输入解析式；图片会作为来源留档"],
      robot: ["拖入机械臂照片、工程图或 3D 文件", "PNG / JPG / PDF / STL / OBJ，单文件 30 MB 以内"],
      mesh: ["拖入 STL 或 OBJ 三维文件", "支持二进制/ASCII STL 与 OBJ，单文件 30 MB 以内"],
    }[subject];
    $("#dropTitle").textContent = copy[0];
    $("#dropNote").textContent = copy[1];

    const meshFiles = state.files.filter((file) => [".stl", ".obj"].includes(fileExt(file.name)));
    if (subject !== "mesh" && subject !== "auto" && meshFiles.length) {
      state.files = meshFiles;
      toast("检测到三维文件，已切换到“三维文件”模式。");
      setSubject("mesh");
      return;
    }
    if (subject === "mesh") {
      state.files = state.files.filter((file) => [".stl", ".obj"].includes(fileExt(file.name)));
    }
    renderFiles();
  }

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    for (const file of incoming) {
      const ext = fileExt(file.name);
      const allowed = ACCEPT[state.subject].split(",");
      if (!allowed.includes(ext)) {
        if ([".stl", ".obj"].includes(ext)) {
          setSubject("mesh");
          addFiles([file]);
          continue;
        }
        toast(`当前模式不支持 ${ext || "该文件"}；请切换输入类型。`, true);
        continue;
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
      if (state.files.some((item) => item.name === file.name && item.size === file.size)) {
        continue;
      }
      state.files.push(file);
    }
    renderFiles();
  }

  function renderFiles() {
    const list = $("#fileList");
    list.innerHTML = state.files.map((file, index) => `
      <li class="file-item">
        <span class="file-ext">${escapeHtml(fileExt(file.name).replace(".", "").toUpperCase() || "FILE")}</span>
        <span class="file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
        <span class="file-size">${fmtBytes(file.size)}</span>
        <button class="file-remove" type="button" data-index="${index}" aria-label="移除 ${escapeHtml(file.name)}">×</button>
      </li>
    `).join("");
  }

  function collectAutoPayload() {
    const payload = { subject: "auto", text: $("#freeText").value.trim() };
    const template = $("#sceneTemplate") ? $("#sceneTemplate").value : "";
    if (template) payload.template = template;
    return payload;
  }

  function collectFunctionPayload() {
    const payload = { subject: "function", expression: $("#expression").value.trim() };
    const windowFields = { x_min: "#xMin", x_max: "#xMax", y_min: "#yMin", y_max: "#yMax" };
    let provided = 0;
    for (const [key, selector] of Object.entries(windowFields)) {
      const raw = $(selector).value.trim();
      if (raw !== "") {
        payload[key] = Number(raw);
        provided += 1;
      }
    }
    if (provided > 0 && provided < 4) {
      throw new Error("手动视窗需要同时填写 x/y 的最小值和最大值，或全部留空使用自动视窗。");
    }
    return payload;
  }

  function collectRobotPayload() {
    return {
      subject: "robot",
      description: $("#robotDescription").value.trim(),
      dof: Number($("#dof").value),
      link_length_mm: Number($("#linkLength").value),
      end_effector: $("#endEffector").value,
      output_mode: $('input[name="robotMode"]:checked').value,
    };
  }

  function collectMeshPayload() {
    return {
      subject: "mesh",
      output_mode: $('input[name="meshMode"]:checked').value,
      target_height_mm: Number($("#meshHeight").value),
      resolution_mm: Number($("#meshResolution").value),
    };
  }

  async function analyze() {
    const button = $("#analyzeBtn");
    if (button.classList.contains("loading")) return;
    let payload;
    try {
      if (state.subject === "auto") payload = collectAutoPayload();
      else if (state.subject === "function") payload = collectFunctionPayload();
      else if (state.subject === "robot") payload = collectRobotPayload();
      else payload = collectMeshPayload();
      if (state.subject === "mesh" && !state.files.some((file) => [".stl", ".obj"].includes(fileExt(file.name)))) {
        throw new Error("请先上传 STL 或 OBJ 文件。");
      }
      if (state.subject === "auto" && !payload.text && !payload.template && !state.files.length) {
        throw new Error("请描述你的需求，或上传图片、PDF、STL/OBJ，或选择一个离线模板。");
      }
      if (state.subject === "function" && !payload.expression && !state.files.length) {
        throw new Error("请输入函数解析式，或上传一张函数图片。");
      }
      if (state.subject === "robot" && !payload.description && !state.files.length) {
        throw new Error("请描述机械臂，或上传图片/工程图/三维文件。");
      }
    } catch (error) {
      toast(error.message, true);
      return;
    }

    button.classList.add("loading");
    button.disabled = true;
    try {
      payload.files = await Promise.all(state.files.map(async (file) => ({
        name: file.name,
        data: await base64FromFile(file),
      })));
      const result = await fetchJSON("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.draft = result.draft;
      state.report = null;
      state.manifest = null;
      state.script = "";
      renderDraft(result.draft);
      $("#draftCard").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      toast(error.message || "智能体分析失败", true);
    } finally {
      button.classList.remove("loading");
      button.disabled = false;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Draft                                                               */
  /* ------------------------------------------------------------------ */

  function confidenceLabel(value) {
    if (value >= 0.85) return "高置信度";
    if (value >= 0.6) return "中等置信度·请核对";
    return "低置信度·需要补充";
  }

  function draftModeSelect(draft) {
    let options = [];
    if (draft.subject === "robot") {
      options = [["volumetric_model", "三维立体缩小模型"], ["tactile_plate", "A5 浮雕触觉板"]];
    } else if (draft.subject === "mesh") {
      options = [["volumetric_model", "缩放为立体模型"], ["silhouette_relief", "A5 轮廓浮雕"]];
    } else {
      return '<span class="v">A5 触觉场景板（固定）</span>';
    }
    return `<select id="draftOutputMode" class="draft-edit">${options.map(([value, label]) => `
      <option value="${value}" ${draft.output_mode === value ? "selected" : ""}>${label}</option>
    `).join("")}</select>`;
  }

  function renderKeyPoints(points) {
    if (!Array.isArray(points) || !points.length) return "";
    const names = { root: "零点", minimum: "极小值", maximum: "极大值", y_intercept: "y 轴截距", extremum: "极值" };
    return `
      <table class="keypoint-table">
        <thead><tr><th>标记</th><th>类型</th><th>x</th><th>y</th></tr></thead>
        <tbody>${points.map((point) => `
          <tr>
            <td>${escapeHtml(point.label)}</td>
            <td>${escapeHtml(names[point.kind] || point.kind)}</td>
            <td class="mono">${fmtNumber(point.x, 3)}</td>
            <td class="mono">${fmtNumber(point.y, 3)}</td>
          </tr>
        `).join("")}</tbody>
      </table>`;
  }

  function chooseTemplate(templateId) {
    if (!templateId) return;
    const entry = state.templates.find((item) => item.id === templateId);
    const select = $("#sceneTemplate");
    if (select) {
      if (!Array.from(select.options).some((option) => option.value === templateId)) {
        select.add(new Option(entry ? `${entry.label} · ${entry.description || ""}` : templateId, templateId));
      }
      select.value = templateId;
    }
    setSubject("auto");
    const free = $("#freeText");
    if (free && !free.value.trim() && entry) {
      free.value = `${entry.description}。请按这个结构做成 A5 触觉板。`;
    }
    analyze();
  }

  function renderDraft(draft) {
    state.draft = draft;
    $("#draftEmpty").classList.add("hidden");
    const body = $("#draftBody");
    body.classList.remove("hidden");

    const percent = Math.round((draft.confidence || 0) * 100);
    const readyPill = draft.ready
      ? '<span class="pill good">可以生成</span>'
      : '<span class="pill warn">等待确认</span>';
    const subjectPill = `<span class="pill neutral">${escapeHtml(SUBJECT_LABEL[draft.subject] || draft.subject)}</span>`;
    const modePill = `<span class="pill info">${escapeHtml(MODE_LABEL[draft.output_mode] || "待选择形态")}</span>`;
    const filePill = draft.files && draft.files.length
      ? `<span class="pill neutral">${draft.files.length} 个来源文件</span>`
      : "";

    let fieldsHtml = "";
    if (draft.subject === "function") {
      fieldsHtml = `
        <div class="draft-grid">
          <label class="draft-field full">
            <span class="k">解析式（可修改）</span>
            <input id="draftExpression" class="draft-edit" type="text" spellcheck="false" value="${escapeHtml(draft.fields.expression || "")}" placeholder="例如 x^2-2x-3">
          </label>
          <label class="draft-field"><span class="k">x 最小</span><input id="draftXMin" class="draft-edit" type="number" step="0.1" value="${escapeHtml(draft.fields.x_min ?? "")}"></label>
          <label class="draft-field"><span class="k">x 最大</span><input id="draftXMax" class="draft-edit" type="number" step="0.1" value="${escapeHtml(draft.fields.x_max ?? "")}"></label>
          <label class="draft-field"><span class="k">y 最小</span><input id="draftYMin" class="draft-edit" type="number" step="0.1" value="${escapeHtml(draft.fields.y_min ?? "")}"></label>
          <label class="draft-field"><span class="k">y 最大</span><input id="draftYMax" class="draft-edit" type="number" step="0.1" value="${escapeHtml(draft.fields.y_max ?? "")}"></label>
        </div>
        ${renderKeyPoints(draft.fields.key_points)}
      `;
    } else if (draft.subject === "robot") {
      const spec = draft.fields.spec || {};
      const lengths = spec.link_lengths_mm || [];
      const longest = lengths.length ? Math.max(...lengths) : 40;
      fieldsHtml = `
        <div class="draft-grid">
          <div class="draft-field full"><span class="k">已理解的描述</span><span class="v">${escapeHtml(draft.fields.description || "未提供文字描述，使用快速参数。")}</span></div>
          <label class="draft-field"><span class="k">自由度</span><select id="draftDof" class="draft-edit">${[3,4,5,6,7].map((dof) => `<option value="${dof}" ${spec.dof === dof ? "selected" : ""}>${dof} 轴</option>`).join("")}</select></label>
          <label class="draft-field"><span class="k">代表连杆长度 / mm</span><input id="draftLink" class="draft-edit" type="number" min="8" step="1" value="${escapeHtml(fmtNumber(longest, 1))}"></label>
          <label class="draft-field"><span class="k">末端执行器</span><select id="draftEnd" class="draft-edit">
            <option value="gripper" ${spec.end_effector === "gripper" ? "selected" : ""}>两指夹爪</option>
            <option value="suction" ${spec.end_effector === "suction" ? "selected" : ""}>吸盘</option>
            <option value="none" ${spec.end_effector === "none" ? "selected" : ""}>无末端</option>
          </select></label>
          <label class="draft-field"><span class="k">输出形态</span>${draftModeSelect(draft)}</label>
          <label class="draft-field"><span class="k">目标高度 / mm（立体）</span><input id="draftTargetHeight" class="draft-edit" type="number" min="30" max="220" step="5" value="${escapeHtml(fmtNumber(draft.fields.target_height_mm || 135, 0))}"></label>
        </div>
      `;
    } else if (draft.subject === "scene") {
      const fields = draft.fields || {};
      const scene = fields.scene || {};
      const summary = fields.summary || {};
      const order = Array.isArray(summary.reading_order) ? summary.reading_order : [];
      const generation = fields.generation || {};
      const templateEntry = state.templates.find((entry) => entry.id === fields.template);
      const meta = [
        `<span class="pill neutral">${Number(summary.block_count || 0)} 个图块</span>`,
        `<span class="pill neutral">${Number(summary.primitive_count || 0)} 个图元</span>`,
      ];
      if (fields.template) {
        meta.push(`<span class="pill info">离线模板：${escapeHtml(templateEntry ? templateEntry.label : fields.template)}</span>`);
      }
      if (generation.provider) {
        meta.push(`<span class="pill info">${escapeHtml(generation.provider)}${generation.model ? ` · ${escapeHtml(generation.model)}` : ""}</span>`);
      }
      const hasScene = scene && Array.isArray(scene.blocks) && scene.blocks.length;
      const rows = order.map((item) => `
        <tr>
          <td class="mono">${escapeHtml(item.index)}</td>
          <td>${escapeHtml(item.label || item.id)}</td>
          <td>${escapeHtml(item.label_zh || "")}</td>
          <td class="scene-note">${escapeHtml(item.note || "—")}</td>
        </tr>
      `).join("");
      fieldsHtml = hasScene ? `
        <div class="scene-meta">${meta.join("")}</div>
        <div class="draft-grid">
          <label class="draft-field"><span class="k">英文标题（可修改）</span><input id="draftSceneTitle" class="draft-edit" type="text" maxlength="32" value="${escapeHtml(scene.title || "")}"></label>
          <label class="draft-field"><span class="k">中文标题（用于讲解）</span><input id="draftSceneTitleZh" class="draft-edit" type="text" maxlength="40" value="${escapeHtml(scene.title_zh || "")}"></label>
        </div>
        <table class="keypoint-table scene-table">
          <thead><tr><th>编号</th><th>标签</th><th>中文</th><th>触摸说明</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
        <p class="hint-line">板面按阅读顺序自动编号；完整英文标签只在空间允许时出现。位置和连接关系可触摸，绝对尺寸以打印设置为准。</p>
      ` : `
        <p class="hint-line">还没有可排版的场景。请补充图块组成和连接关系，或在下面选择一个离线模板。</p>
      `;
      if (!draft.ready && Array.isArray(fields.suggested_templates) && fields.suggested_templates.length) {
        fieldsHtml += `
          <p class="hint-line">选一个模板直接开始，不需要大模型：</p>
          <div class="template-grid">
            ${fields.suggested_templates.map((entry) => `
              <button class="template-card" type="button" data-template="${escapeHtml(entry.id)}">
                <strong>${escapeHtml(entry.label)}</strong>
                <small>${escapeHtml(entry.description || "")}</small>
              </button>
            `).join("")}
          </div>
        `;
      }
    } else if (draft.subject === "unsupported") {
      fieldsHtml = `
        <div class="draft-field full">
          <span class="k">智能体的判断</span>
          <span class="v">${escapeHtml((draft.fields && draft.fields.source_text) || "该请求未提供足够信息。")}</span>
        </div>
      `;
    } else {
      const info = draft.fields.mesh_info || {};
      fieldsHtml = `
        <div class="draft-grid">
          <div class="draft-field full"><span class="k">三维文件</span><span class="v mono">${escapeHtml(draft.fields.mesh_name || "未读取")}</span></div>
          <div class="draft-field"><span class="k">三角面</span><span class="v mono">${escapeHtml(info.triangles ?? "—")}</span></div>
          <div class="draft-field"><span class="k">水密</span><span class="v">${info.watertight ? "是" : "否"}</span></div>
          <div class="draft-field full"><span class="k">包围盒 / mm</span><span class="v mono">${escapeHtml(Array.isArray(info.bounds_mm) ? info.bounds_mm.map((value) => fmtNumber(value, 1)).join(" × ") : "—")}</span></div>
          <label class="draft-field"><span class="k">输出形态</span>${draftModeSelect(draft)}</label>
          <label class="draft-field"><span class="k">目标高度 / mm</span><input id="draftTargetHeight" class="draft-edit" type="number" min="20" max="220" step="5" value="${escapeHtml(fmtNumber(draft.fields.target_height_mm || 120, 0))}"></label>
          <label class="draft-field"><span class="k">浮雕分辨率 / mm</span><input id="draftResolution" class="draft-edit" type="number" min="0.6" max="3" step="0.1" value="${escapeHtml(fmtNumber(draft.fields.resolution_mm || 1, 1))}"></label>
        </div>
      `;
    }

    const questions = Array.isArray(draft.questions) && draft.questions.length
      ? `<ul class="question-list">${draft.questions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
      : "";
    const notes = Array.isArray(draft.notes) && draft.notes.length
      ? `<ul class="note-list">${draft.notes.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
      : "";
    const routing = draft.fields && draft.fields.routing;
    const routeLine = routing
      ? `<p class="hint-line">路径判断：${escapeHtml(SUBJECT_LABEL[routing.subject] || routing.subject)} · ${escapeHtml(routing.reason || "")}（${escapeHtml(routing.provider || "heuristic")}）</p>`
      : "";

    body.innerHTML = `
      <div class="draft-head">
        <div>
          <span class="eyebrow">STEP 02 · AGENT DRAFT</span>
          <h2>${draft.ready ? "智能体已理解，等待你确认" : "智能体需要更多信息"}</h2>
          <p class="sub">草稿 ${escapeHtml(draft.id)} · ${new Date(draft.created_at || Date.now()).toLocaleString("zh-CN", { hour12: false })}</p>
        </div>
        <div class="confidence">
          <span class="confidence-value">${percent}%</span>
          <div class="confidence-track"><div class="confidence-fill" style="width:${percent}%"></div></div>
          <span class="confidence-label">${confidenceLabel(draft.confidence || 0)}</span>
        </div>
      </div>
      <div class="draft-summary">${subjectPill}${modePill}${readyPill}${filePill}</div>
      ${routeLine}
      ${fieldsHtml}
      ${questions}
      ${notes}
      <div class="draft-actions">
        <button class="primary" id="buildBtn" type="button" ${draft.ready ? "" : "disabled"} title="${draft.ready ? "确认后生成 STL" : "请先补充信息或重新理解"}">
          <span class="button-label">确认并生成触觉产物</span>
          <span class="button-spinner" aria-hidden="true"></span>
        </button>
        <button class="secondary" id="reanalyzeBtn" type="button">重新理解</button>
        <button class="ghost" id="downloadDraftBtn" type="button">下载草稿 JSON</button>
      </div>
    `;

    $("#buildBtn").addEventListener("click", build);
    $$(".template-card", body).forEach((button) => {
      button.addEventListener("click", () => chooseTemplate(button.dataset.template));
    });
    $("#reanalyzeBtn").addEventListener("click", analyze);
    $("#downloadDraftBtn").addEventListener("click", () => {
      downloadBlob(JSON.stringify(draft, null, 2), `touchlearn-draft-${draft.id}.json`, "application/json");
    });
  }

  function currentDraftOverrides() {
    const draft = state.draft;
    if (!draft) throw new Error("还没有可生成的草稿。");
    const modes = $("#draftOutputMode");
    const outputMode = modes ? modes.value : draft.output_mode;
    const fields = {};

    if (draft.subject === "function") {
      const expression = $("#draftExpression").value.trim();
      if (!expression) throw new Error("请填写函数解析式。");
      fields.expression = expression;
      const xMin = $("#draftXMin").value.trim();
      const xMax = $("#draftXMax").value.trim();
      const yMin = $("#draftYMin").value.trim();
      const yMax = $("#draftYMax").value.trim();
      const filled = [xMin, xMax, yMin, yMax].filter((value) => value !== "").length;
      if (filled === 4) {
        fields.x_min = Number(xMin);
        fields.x_max = Number(xMax);
        fields.y_min = Number(yMin);
        fields.y_max = Number(yMax);
      } else if (filled > 0) {
        throw new Error("视窗需要同时填写四项，或全部留空使用自动视窗。");
      }
    } else if (draft.subject === "robot") {
      const spec = JSON.parse(JSON.stringify(draft.fields.spec || {}));
      const dof = Number($("#draftDof").value);
      const link = Number($("#draftLink").value);
      const end = $("#draftEnd").value;
      if (!Number.isFinite(dof) || dof < 3 || dof > 7) throw new Error("自由度需在 3–7 之间。");
      if (!Number.isFinite(link) || link <= 0) throw new Error("连杆长度必须为正数。");

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
      spec.base_width_mm = Number((Math.min(60, Math.max(24, (spec.base_width_mm || 34) * Math.min(scale, 1.8)))).toFixed(3));
      spec.base_height_mm = Number((Math.min(24, Math.max(10, (spec.base_height_mm || 14) * Math.min(scale, 1.8)))).toFixed(3));
      spec.end_effector = end;
      spec.dof = dof;

      fields.spec = spec;
      fields.description = draft.fields.description || "";
      fields.target_height_mm = Number($("#draftTargetHeight").value || 135);
    } else if (draft.subject === "scene") {
      const scene = JSON.parse(JSON.stringify(draft.fields.scene || {}));
      if (!Array.isArray(scene.blocks) || !scene.blocks.length) {
        throw new Error("还没有可排版的场景，请先补充描述或选择模板。");
      }
      const titleInput = $("#draftSceneTitle");
      const titleZhInput = $("#draftSceneTitleZh");
      const title = titleInput ? titleInput.value.trim() : "";
      const titleZh = titleZhInput ? titleZhInput.value.trim() : "";
      if (!title) throw new Error("请填写英文标题。");
      scene.title = title;
      if (titleZh) scene.title_zh = titleZh;
      fields.scene = scene;
    } else if (draft.subject === "mesh") {
      fields.target_height_mm = Number($("#draftTargetHeight").value || 120);
      fields.resolution_mm = Number($("#draftResolution").value || 1);
    } else {
      throw new Error("这个草稿还没有可生成的产物。");
    }
    return { fields, output_mode: outputMode };
  }

  /* ------------------------------------------------------------------ */
  /* Build and polling                                                   */
  /* ------------------------------------------------------------------ */

  async function build() {
    const button = $("#buildBtn");
    if (!button || button.classList.contains("loading")) return;
    let overrides;
    try {
      overrides = currentDraftOverrides();
    } catch (error) {
      toast(error.message, true);
      return;
    }
    button.classList.add("loading");
    button.disabled = true;
    try {
      const result = await fetchJSON("/api/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_id: state.draft.id, overrides }),
      });
      state.jobId = result.job_id;
      state.job = null;
      state.report = null;
      state.manifest = null;
      state.script = "";
      state.jobTab = "preview";
      renderJob({ status: "queued", progress: 0, message: "任务已排队", title: "正在准备建模" });
      $("#jobCard").scrollIntoView({ behavior: "smooth", block: "start" });
      startPolling();
    } catch (error) {
      toast(error.message || "提交生成任务失败", true);
    } finally {
      button.classList.remove("loading");
      button.disabled = false;
    }
  }

  function startPolling() {
    stopPolling();
    state.timer = setInterval(pollJob, 650);
    pollJob();
  }

  function stopPolling() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }

  async function pollJob() {
    if (!state.jobId) return;
    try {
      const job = await fetchJSON(`/api/jobs/${state.jobId}`);
      state.job = job;
      renderJob(job);
      if (job.status === "done" || job.status === "failed") {
        stopPolling();
        if (job.status === "done") await loadJobDetails();
      }
    } catch (error) {
      if (String(error.message).includes("尚未创建")) return;
      stopPolling();
      toast(error.message || "读取作业状态失败", true);
    }
  }

  async function loadJobDetails() {
    const jobId = state.jobId;
    const [report, manifest, script] = await Promise.all([
      fetchJSON(`/api/jobs/${jobId}/report`).catch(() => null),
      fetchJSON(`/files/${jobId}/manifest.json`).catch(() => null),
      fetch(`/files/${jobId}/audio_script.txt`).then((response) => response.ok ? response.text() : "").catch(() => ""),
    ]);
    state.report = report;
    state.manifest = manifest;
    state.script = script || "";
    renderJob(state.job);
  }

  /* ------------------------------------------------------------------ */
  /* Job rendering                                                       */
  /* ------------------------------------------------------------------ */

  function jobBadges(job) {
    const badges = [];
    if (job.mode) badges.push(`<span class="pill neutral">${escapeHtml(MODE_LABEL[job.mode] || job.mode)}</span>`);
    if (job.status === "done") {
      badges.push(job.printable
        ? '<span class="pill good">可打印 · 无 FAIL</span>'
        : '<span class="pill bad">存在阻断打印的 FAIL</span>');
      badges.push(job.standards_clear
        ? '<span class="pill good">完全合规</span>'
        : '<span class="pill warn">未达完全合规 · 见报告</span>');
    }
    return badges.join("");
  }

  function renderJob(job) {
    if (!job) return;
    $("#jobEmpty").classList.add("hidden");
    const body = $("#jobBody");
    body.classList.remove("hidden");
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
    const statusText = job.status === "failed" ? "生成失败" : job.status === "done" ? "生成完成" : "智能体正在建模";
    const title = job.title || (state.draft ? `${SUBJECT_LABEL[state.draft.subject]}触觉产物` : "触觉产物");
    const message = job.message || (job.status === "done" ? "全部产物已写入作业目录" : "正在处理");

    const statusClass = job.status === "failed" ? "bad" : job.status === "done" ? "good" : "warn";
    const statusLabel = { queued: "排队", building: "建模中", done: "完成", failed: "失败" }[job.status] || job.status;

    let content = `
      <div class="job-head">
        <div class="job-title-wrap">
          <span class="eyebrow">STEP 03 · DELIVERABLES</span>
          <h2 title="${escapeHtml(title)}">${escapeHtml(title)}</h2>
          <p class="sub">作业 ${escapeHtml(state.jobId)} · ${escapeHtml(message)}</p>
        </div>
        <span class="pill ${statusClass}">${escapeHtml(statusLabel)}</span>
      </div>
      <div class="job-meta">${jobBadges(job)}${job.artifacts ? `<span class="pill neutral">${job.artifacts.length} 个文件</span>` : ""}</div>
      <div class="progress-wrap">
        <div class="progress-top"><span>${escapeHtml(statusText)}</span><strong>${progress}%</strong></div>
        <div class="progress"><div class="progress-fill ${job.status === "done" ? "done" : ""}" style="width:${progress}%"></div></div>
      </div>
    `;

    if (job.status === "failed") {
      content += `
        <div class="compliance-note warn">
          <strong>生成没有完成。</strong> ${escapeHtml(job.error || message)}
          <br>请回到草稿检查参数，或查看作业目录里的 error.txt。
        </div>
        <div class="action-row">
          <button class="secondary" id="retryBtn" type="button">返回草稿重试</button>
        </div>
      `;
      body.innerHTML = content;
      $("#retryBtn").addEventListener("click", () => $("#draftCard").scrollIntoView({ behavior: "smooth", block: "start" }));
      return;
    }

    if (job.status !== "done") {
      content += `
        <div class="preview-frame" style="min-height:200px;color:var(--muted);font-size:.82rem">
          正在生成 STL、预览、合规报告、讲解音频与二维码……
        </div>
      `;
      body.innerHTML = content;
      return;
    }

    content += `
      <div class="job-tabs" role="tablist">
        ${[
          ["preview", "预览与下载"],
          ["compliance", "合规报告"],
          ["audio", "讲解音频"],
          ["qr", "二维码"],
          ["print", "打印说明"],
        ].map(([key, label]) => `<button class="job-tab ${state.jobTab === key ? "active" : ""}" data-tab="${key}" type="button">${label}</button>`).join("")}
      </div>
      <div id="jobPanel" class="job-panel"></div>
    `;
    body.innerHTML = content;
    $$(".job-tab", body).forEach((button) => {
      button.addEventListener("click", () => {
        state.jobTab = button.dataset.tab;
        $$(".job-tab", body).forEach((item) => item.classList.toggle("active", item === button));
        renderJobPanel();
      });
    });
    renderJobPanel();
  }

  function renderJobPanel() {
    const panel = $("#jobPanel");
    if (!panel || !state.job) return;
    if (state.jobTab === "preview") panel.innerHTML = previewPanel();
    else if (state.jobTab === "compliance") panel.innerHTML = compliancePanel();
    else if (state.jobTab === "audio") panel.innerHTML = audioPanel();
    else if (state.jobTab === "qr") panel.innerHTML = qrPanel();
    else panel.innerHTML = printPanel();

    if (state.jobTab === "preview") {
      $("#viewerBtn")?.addEventListener("click", openViewer);
      $("#closeViewerBtn")?.addEventListener("click", closeViewer);
    }
    if (state.jobTab === "audio" && !state.script) {
      fetch(`/files/${state.jobId}/audio_script.txt`)
        .then((response) => response.ok ? response.text() : "")
        .then((text) => {
          state.script = text;
          const box = $("#scriptBox");
          if (box) box.textContent = text;
        })
        .catch(() => {});
    }
  }

  function previewPanel() {
    const jobId = state.jobId;
    const mode = state.job.mode;
    return `
      <div class="preview-frame">
        <img src="/files/${jobId}/preview.svg" alt="触觉产物预览">
      </div>
      <div class="preview-actions">
        <a class="primary" style="text-decoration:none;display:inline-flex;align-items:center;gap:8px" href="/files/${jobId}/model.stl?download=1">下载 STL</a>
        <a class="secondary" style="text-decoration:none;display:inline-flex;align-items:center" href="/api/jobs/${jobId}/bundle.zip">下载整包 ZIP</a>
        <button class="secondary" id="viewerBtn" type="button">三维旋转查看</button>
        <a class="ghost" style="text-decoration:none;display:inline-flex;align-items:center" href="/files/${jobId}/compliance.md" target="_blank" rel="noopener">打开合规报告</a>
      </div>
      <div id="viewerSlot"></div>
      <p class="hint-line" style="margin-top:12px">
        ${mode === "tactile_plate" ? "A5 触觉板：浮雕面朝上、底座贴床，勿开启顶面熨烫。" : "立体模型：预览为等轴测投影；切片时请检查支撑与摆放方向。"}
        预览 SVG 与 STL 来自同一份几何，不是另画的示意图。
      </p>
    `;
  }

  function compliancePanel() {
    const report = state.report;
    if (!report) return '<p class="hint-line">合规报告正在读取……</p>';
    const counts = report.counts || {};
    const summary = Object.entries(counts).map(([status, count]) => `<span class="pill ${status === "FAIL" ? "bad" : status === "PASS" ? "good" : status === "NOT_APPLICABLE" ? "neutral" : "warn"}">${STATUS_LABEL[status] || status} ${count}</span>`).join("");
    const note = report.printable
      ? (report.standards_clear
        ? '<div class="compliance-note">几何与工艺检查全部通过，可以进入切片打印。</div>'
        : '<div class="compliance-note warn"><strong>可打印，但还不是“完全合规”。</strong>报告里存在工艺偏离或待人工复核项；交付给教学场景前请逐条确认。</div>')
      : '<div class="compliance-note warn"><strong>存在阻断打印的 FAIL。</strong>请先修复网格或参数，再下载 STL。</div>';

    const rows = (report.checks || []).map((check) => `
      <tr>
        <td><span class="status ${escapeHtml(check.status)}">${escapeHtml(STATUS_LABEL[check.status] || check.status)}</span></td>
        <td class="rule">${escapeHtml(check.rule_id)}</td>
        <td class="measured">${escapeHtml(check.measured)}</td>
        <td>${escapeHtml(check.message)}</td>
      </tr>
    `).join("");

    const standards = (report.standards || []).map((standard) => `
      <details class="standard-item">
        <summary>
          <span class="standard-number">${escapeHtml(standard.number)}</span>
          <span class="standard-title">${escapeHtml(standard.title)}</span>
          <span class="standard-status">${escapeHtml(standard.status)} · ${escapeHtml(standard.effective)}</span>
        </summary>
        <div class="standard-body">
          ${escapeHtml(standard.scope)}
          ${standard.note ? `<br><br>${escapeHtml(standard.note)}` : ""}
          <br><br><a href="${escapeHtml(standard.url)}" target="_blank" rel="noopener">${escapeHtml(standard.url)}</a>
        </div>
      </details>
    `).join("");

    return `
      ${note}
      <div class="job-meta">${summary}</div>
      <table class="check-table">
        <thead><tr><th>状态</th><th>规则</th><th>实测</th><th>说明</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="standard-list">${standards}</div>
      <p class="hint-line" style="margin-top:12px">判定口径：<strong>可打印</strong>只表示没有 FAIL；<strong>完全合规</strong>还要求没有工艺偏离和待人工复核项。</p>
    `;
  }

  function audioPanel() {
    const jobId = state.jobId;
    return `
      <div class="audio-box">
        <audio controls preload="metadata" src="/files/${jobId}/narration.wav"></audio>
        <div class="action-row">
          <a class="secondary" style="text-decoration:none" href="/files/${jobId}/narration.wav?download=1">下载 WAV</a>
          <a class="secondary" style="text-decoration:none" href="/files/${jobId}/audio_script.txt?download=1">下载讲解稿</a>
          <a class="ghost" style="text-decoration:none" href="/audio/${jobId}/" target="_blank" rel="noopener">打开手机版音频页</a>
        </div>
        <pre class="script-box" id="scriptBox">${escapeHtml(state.script || "正在读取讲解稿……")}</pre>
        <p class="hint-line">${state.draft && state.draft.subject === "scene"
          ? "讲解按板面阅读顺序组织：先确认缺口方向，再按编号逐个描述图块和连接关系。"
          : "讲解顺序按触摸探索路径组织：先定位板面与方向，再沿曲线/结构追踪，最后报关键点与尺寸。"}</p>
      </div>
    `;
  }

  function qrPanel() {
    const jobId = state.jobId;
    const publicUrl = state.health ? state.health.public_base_url : location.origin;
    const audioUrl = `${publicUrl.replace(/\/$/, "")}/audio/${jobId}/`;
    return `
      <div class="qr-box">
        <div class="qr-frame"><img src="/files/${jobId}/qr_card.svg" alt="讲解音频二维码卡片"></div>
        <div class="qr-copy">
          <h3>扫描二维码，收听这份触觉教材</h3>
          <p>手机与电脑需在同一 Wi-Fi。首次访问若被防火墙拦截，请允许 Python 通过专用网络。</p>
          <span class="qr-url">${escapeHtml(audioUrl)}</span>
          <div class="action-row" style="margin-top:0">
            <a class="secondary" style="text-decoration:none" href="/files/${jobId}/qr.png?download=1">下载二维码 PNG</a>
            <a class="secondary" style="text-decoration:none" href="/files/${jobId}/qr_card.svg?download=1">下载二维码卡片</a>
            <a class="ghost" style="text-decoration:none" href="/audio/${jobId}/" target="_blank" rel="noopener">在电脑上打开</a>
          </div>
        </div>
      </div>
      <p class="hint-line" style="margin-top:14px">二维码里写的是局域网地址，不是公网链接；答辩现场可把它打印在作品卡或说明页上。</p>
    `;
  }

  function printPanel() {
    const manifest = state.manifest;
    const print = (manifest && manifest.print) || {};
    const geometry = (manifest && manifest.geometry) || {};
    const metrics = geometry.mesh || {};
    const size = print.size_mm || metrics.size_mm;
    const items = [
      ["尺寸 / mm", Array.isArray(size) ? size.map((value) => fmtNumber(value, 1)).join(" × ") : "—"],
      ["摆放方向", print.orientation || "—"],
      ["喷嘴", print.nozzle_mm ? `${print.nozzle_mm} mm` : "—"],
      ["层高", print.layer_height_mm ? `${print.layer_height_mm} mm` : "—"],
      ["墙线", print.perimeters ? `${print.perimeters} 圈` : "—"],
      ["填充", print.infill_percent ? `${print.infill_percent}%` : "—"],
      ["支撑", print.supports || "—"],
      ["三角面", metrics.triangles ?? "—"],
    ];
    return `
      <div class="print-grid">
        ${items.map(([key, value]) => `<div class="print-item"><span class="k">${escapeHtml(key)}</span><span class="v">${escapeHtml(value)}</span></div>`).join("")}
      </div>
      <p class="print-note">${escapeHtml(print.notes || "请按切片软件预览确认摆放方向与支撑。")}</p>
      <div class="action-row">
        <a class="secondary" style="text-decoration:none" href="/files/${state.jobId}/README.txt?download=1">下载打印说明</a>
        <a class="secondary" style="text-decoration:none" href="/api/jobs/${state.jobId}/bundle.zip">下载整包 ZIP</a>
      </div>
      <p class="hint-line" style="margin-top:12px">本智能体不直接控制打印机；STL 可导入 Bambu Studio、PrusaSlicer、Cura 等切片软件。若模型有悬垂，报告会标记为工艺提示而不是几何错误。</p>
    `;
  }

  /* ------------------------------------------------------------------ */
  /* Lightweight STL viewer                                              */
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
    const response = await fetch(url);
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

  async function openViewer() {
    const slot = $("#viewerSlot");
    if (!slot) return;
    slot.innerHTML = `
      <div class="viewer-wrap" style="margin-top:12px">
        <button class="icon-button viewer-close" id="closeViewerBtn" type="button" aria-label="关闭三维视图">×</button>
        <canvas id="stlCanvas" aria-label="STL 三维预览"></canvas>
        <span class="viewer-hint">拖动旋转 · 滚轮缩放 · 松手后自动重绘</span>
      </div>
    `;
    $("#closeViewerBtn").addEventListener("click", closeViewer);
    try {
      const geometry = await loadSTL(`/files/${state.jobId}/model.stl`);
      state.viewer = createViewer($("#stlCanvas"), geometry);
    } catch (error) {
      slot.innerHTML = `<p class="hint-line" style="color:var(--red)">${escapeHtml(error.message)}</p>`;
      state.viewer = null;
    }
  }

  function closeViewer() {
    if (state.viewer) {
      state.viewer.destroy();
      state.viewer = null;
    }
    const slot = $("#viewerSlot");
    if (slot) slot.innerHTML = "";
  }

  function createViewer(canvas, geometry) {
    const context = canvas.getContext("2d");
    const maxTriangles = 26000;
    const stride = Math.max(1, Math.ceil(geometry.count / maxTriangles));
    const positions = geometry.positions;
    let yaw = -0.7;
    let pitch = 0.55;
    let zoom = 1.0;
    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    let frame = 0;

    const bounds = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity, minZ: Infinity, maxZ: -Infinity };
    for (let index = 0; index < geometry.count * 9; index += 3) {
      const x = positions[index];
      const y = positions[index + 1];
      const z = positions[index + 2];
      bounds.minX = Math.min(bounds.minX, x); bounds.maxX = Math.max(bounds.maxX, x);
      bounds.minY = Math.min(bounds.minY, y); bounds.maxY = Math.max(bounds.maxY, y);
      bounds.minZ = Math.min(bounds.minZ, z); bounds.maxZ = Math.max(bounds.maxZ, z);
    }
    const center = {
      x: (bounds.minX + bounds.maxX) / 2,
      y: (bounds.minY + bounds.maxY) / 2,
      z: (bounds.minZ + bounds.maxZ) / 2,
    };
    const extent = Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY, bounds.maxZ - bounds.minZ, 0.001);

    function resize() {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(320, Math.round(rect.width * dpr));
      canvas.height = Math.max(260, Math.round(rect.height * dpr));
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function draw() {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      context.clearRect(0, 0, width, height);

      const gradient = context.createLinearGradient(0, 0, 0, height);
      gradient.addColorStop(0, "#1d2a26");
      gradient.addColorStop(1, "#101715");
      context.fillStyle = gradient;
      context.fillRect(0, 0, width, height);

      const cosYaw = Math.cos(yaw);
      const sinYaw = Math.sin(yaw);
      const cosPitch = Math.cos(pitch);
      const sinPitch = Math.sin(pitch);
      const scale = (Math.min(width, height) / extent) * 0.72 * zoom;
      const cx = width / 2;
      const cy = height / 2;
      const triangles = [];

      for (let tri = 0; tri < geometry.count; tri += stride) {
        const base = tri * 9;
        const points = [];
        let depth = 0;
        for (let vertex = 0; vertex < 3; vertex += 1) {
          const x = positions[base + vertex * 3] - center.x;
          const y = positions[base + vertex * 3 + 1] - center.y;
          const z = positions[base + vertex * 3 + 2] - center.z;
          const rx = x * cosYaw - y * sinYaw;
          const ry = x * sinYaw + y * cosYaw;
          const rz = z;
          const py = ry * cosPitch - rz * sinPitch;
          const pz = ry * sinPitch + rz * cosPitch;
          points.push([cx + rx * scale, cy - py * scale]);
          depth += pz;
        }
        const ax = points[1][0] - points[0][0];
        const ay = points[1][1] - points[0][1];
        const bx = points[2][0] - points[0][0];
        const by = points[2][1] - points[0][1];
        const normal = ax * by - ay * bx;
        const shade = Math.max(0.18, Math.min(1, Math.abs(normal) / (Math.hypot(ax, ay) * Math.hypot(bx, by) + 1e-6)) * 0.85 + 0.15);
        triangles.push({ points, depth: depth / 3, shade });
      }

      triangles.sort((a, b) => a.depth - b.depth);
      for (const triangle of triangles) {
        const green = Math.round(84 + 104 * triangle.shade);
        const red = Math.round(22 + 40 * triangle.shade);
        const blue = Math.round(72 + 58 * triangle.shade);
        context.fillStyle = `rgb(${red},${green},${blue})`;
        context.strokeStyle = context.fillStyle;
        context.lineWidth = 0.55;
        context.beginPath();
        context.moveTo(triangle.points[0][0], triangle.points[0][1]);
        context.lineTo(triangle.points[1][0], triangle.points[1][1]);
        context.lineTo(triangle.points[2][0], triangle.points[2][1]);
        context.closePath();
        context.fill();
        context.stroke();
      }

      context.fillStyle = "rgba(248,244,233,0.6)";
      context.font = "11px Consolas, monospace";
      context.fillText(`${geometry.count.toLocaleString("en-US")} triangles · ${stride > 1 ? `showing 1/${stride}` : "full"}`, 14, height - 14);
    }

    function scheduleDraw() {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        draw();
      });
    }

    function onPointerDown(event) {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      canvas.setPointerCapture(event.pointerId);
    }

    function onPointerMove(event) {
      if (!dragging) return;
      const dx = event.clientX - lastX;
      const dy = event.clientY - lastY;
      lastX = event.clientX;
      lastY = event.clientY;
      yaw += dx * 0.008;
      pitch = Math.max(-1.45, Math.min(1.45, pitch + dy * 0.008));
      scheduleDraw();
    }

    function onPointerUp(event) {
      dragging = false;
      try { canvas.releasePointerCapture(event.pointerId); } catch (error) { /* noop */ }
    }

    function onWheel(event) {
      event.preventDefault();
      zoom = Math.max(0.35, Math.min(4.0, zoom * (event.deltaY > 0 ? 0.92 : 1.08)));
      scheduleDraw();
    }

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("resize", resize);

    resize();

    return {
      destroy() {
        window.removeEventListener("resize", resize);
        canvas.removeEventListener("pointerdown", onPointerDown);
        canvas.removeEventListener("pointermove", onPointerMove);
        canvas.removeEventListener("pointerup", onPointerUp);
        canvas.removeEventListener("pointercancel", onPointerUp);
        canvas.removeEventListener("wheel", onWheel);
        if (frame) cancelAnimationFrame(frame);
      },
    };
  }

  /* ------------------------------------------------------------------ */
  /* Misc                                                                */
  /* ------------------------------------------------------------------ */

  function downloadBlob(content, filename, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  function bind() {
    $$("#subjectTabs .segment").forEach((button) => {
      button.addEventListener("click", () => setSubject(button.dataset.subject));
    });
    $$(".chip[data-expression]").forEach((button) => {
      button.addEventListener("click", () => {
        $("#expression").value = button.dataset.expression;
        setSubject("function");
      });
    });
    $$(".chip[data-free-text]").forEach((button) => {
      button.addEventListener("click", () => {
        $("#freeText").value = button.dataset.freeText;
        setSubject("auto");
      });
    });
    $("#providerRefresh")?.addEventListener("click", () => {
      loadHealth();
      loadProviders();
    });

    const dropzone = $("#dropzone");
    const input = $("#fileInput");
    dropzone.addEventListener("click", () => input.click());
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        input.click();
      }
    });
    input.addEventListener("change", () => {
      addFiles(input.files);
      input.value = "";
    });
    ["dragenter", "dragover"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
    $("#fileList").addEventListener("click", (event) => {
      const button = event.target.closest(".file-remove");
      if (!button) return;
      state.files.splice(Number(button.dataset.index), 1);
      renderFiles();
    });

    $("#analyzeBtn").addEventListener("click", analyze);

    const dialog = $("#helpDialog");
    $("#helpBtn").addEventListener("click", () => dialog.showModal());
    $("#helpClose").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.viewer) closeViewer();
    });
  }

  bind();
  setSubject("auto");
  loadHealth();
  loadProviders();
  loadTemplates();
  setInterval(loadHealth, 30000);
})();
