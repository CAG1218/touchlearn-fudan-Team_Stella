/*
 * Optional "send this STL to a printer" extension.
 *
 * The main app only knows how to build and verify an STL.  This script adds
 * one button to the finished job and a dialog that keeps three things in
 * order: pick a printer, run the server-side pre-flight (bed fit, format,
 * size), then require an explicit confirmation before anything is sent.
 *
 * The server decides whether a request is a rehearsal or a real print; this
 * script only reflects that decision in words.  No printer address or key is
 * ever handled in the browser.
 */
(function () {
  "use strict";

  const state = {
    open: false,
    jobId: "",
    printers: [],
    allowPrint: false,
    printerId: "",
    file: "model.stl",
    plan: null,
    planError: "",
    sending: false,
    confirmed: false,
    result: null,
    triggeredBy: null,
  };

  const root = () => document.querySelector("[data-print-root]");

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function announce(text) {
    if (!text) return;
    if (window.touchlearnVoice && typeof window.touchlearnVoice.announce === "function") {
      window.touchlearnVoice.announce(text);
      return;
    }
    const live = document.getElementById("voiceLive");
    if (live) live.textContent = text;
  }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({ credentials: "same-origin" }, options || {}));
    let payload = {};
    try {
      payload = await response.json();
    } catch (error) {
      payload = {};
    }
    if (!response.ok && !payload.error) {
      payload.error = `请求失败（${response.status}）`;
    }
    return { ok: response.ok, status: response.status, payload };
  }

  function fmtMm(values) {
    return (values || []).map((value) => Number(value).toFixed(1)).join(" × ");
  }

  function humanBytes(count) {
    const value = Number(count) || 0;
    if (value < 1024) return `${value.toFixed(0)} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function modeLabel() {
    return state.allowPrint ? "真实打印" : "演练模式";
  }

  function printerHint() {
    return state.allowPrint
      ? "已允许真实打印：发送后打印机会加热并移动，请先清理平台、装好材料。"
      : "演练模式：只做尺寸与格式检查，不会连接打印机。真实打印需设置 TOUCHLEARN_PRINT_ALLOW=1。";
  }

  function mount() {
    let host = root();
    if (host) return host;
    host = document.createElement("div");
    host.setAttribute("data-print-root", "");
    document.body.appendChild(host);
    return host;
  }

  function close() {
    if (state.sending) return;
    const host = root();
    if (host) host.innerHTML = "";
    state.open = false;
    state.plan = null;
    state.planError = "";
    state.result = null;
    state.confirmed = false;
    if (state.triggeredBy && typeof state.triggeredBy.focus === "function") {
      state.triggeredBy.focus();
    }
  }

  function shell(body) {
    return `
      <div class="print-backdrop" data-print-close>
        <div class="print-dialog" role="dialog" aria-modal="true" aria-labelledby="printTitle">
          <header class="print-head">
            <h2 id="printTitle">发送到 3D 打印机</h2>
            <button class="icon-btn" type="button" data-print-close aria-label="关闭打印对话框">
              <svg class="icon" aria-hidden="true"><use href="#icon-close"/></svg>
            </button>
          </header>
          <div class="print-body">${body}</div>
        </div>
      </div>`;
  }

  function setupPanel() {
    return `
      <div class="print-section">
        <p class="print-lead">服务器还没有配置打印机。这一步是可选的，不影响下载 STL。</p>
        <p class="print-lead">在 <code>.env</code> 里填写下面任意一组，然后重启服务：</p>
        <pre class="print-code">TOUCHLEARN_PRINTER_KIND=octoprint
TOUCHLEARN_PRINTER_URL=http://192.168.1.50
TOUCHLEARN_PRINTER_KEY=在 OctoPrint 设置里生成的应用密钥
TOUCHLEARN_PRINTER_BED=220x220x250
# 确认要真实打印时再加：
TOUCHLEARN_PRINT_ALLOW=1</pre>
        <p class="print-lead">Klipper / Moonraker 把 KIND 改成 <code>moonraker</code> 即可；多台打印机用 <code>TOUCHLEARN_PRINTERS</code> 传 JSON 数组。</p>
      </div>
      <footer class="print-foot">
        <span class="print-note">${esc(printerHint())}</span>
        <button class="btn" type="button" data-print-close>关闭</button>
      </footer>`;
  }

  function printerList() {
    return state.printers.map((printer) => {
      const active = printer.id === state.printerId ? " is-active" : "";
      return `
        <button class="print-printer${active}" type="button" data-print-pick="${esc(printer.id)}" aria-pressed="${printer.id === state.printerId}">
          <span class="print-printer-name">${esc(printer.label)}</span>
          <span class="print-printer-meta">${esc(printer.kind)} · 打印床 ${fmtMm(printer.bed_mm)} mm${printer.key_configured ? "" : " · 无密钥"}</span>
        </button>`;
    }).join("");
  }

  function planPanel() {
    if (state.planError) {
      return `<div class="print-checks"><div class="print-check bad"><span class="print-check-dot"></span><span>${esc(state.planError)}</span></div></div>`;
    }
    if (!state.plan) {
      return `<div class="print-checks"><div class="print-check"><span class="print-check-dot"></span><span>正在检查尺寸与格式…</span></div></div>`;
    }
    const rows = (state.plan.checks || []).map((check) => `
      <div class="print-check ${check.ok ? "ok" : "bad"}">
        <span class="print-check-dot" aria-hidden="true"></span>
        <span><b>${esc(check.label)}</b>：${esc(check.detail)}</span>
      </div>`).join("");
    const file = state.plan.file || {};
    const fit = state.plan.ok
      ? "检查通过，可以发送。"
      : "检查未通过，改动参数或换一台打印床更大的机器后再试。";
    return `
      <div class="print-file">
        <span>${esc(file.name || state.file)}</span>
        <span>${esc(fmtMm(file.size_mm))} mm · ${Number(file.triangles || 0).toLocaleString("zh-CN")} 面 · ${esc(humanBytes(file.bytes))}</span>
      </div>
      <div class="print-checks">${rows}</div>
      <p class="print-lead ${state.plan.ok ? "" : "print-lead-bad"}">${esc(fit)}${state.plan.rotate_deg ? " 这台机器的打印床需要把模型旋转 90° 摆放。" : ""}</p>`;
  }

  function resultPanel() {
    const result = state.result || {};
    const tone = result.ok ? "ok" : "bad";
    const stateLabel = {
      sent: "已发送",
      rehearsed: "演练通过",
      blocked: "检查未通过",
      failed: "发送失败",
    }[result.state] || "未知状态";
    return `
      <div class="print-result ${tone}" role="status">
        <strong>${esc(stateLabel)}</strong>
        <p>${esc(result.message || "")}</p>
        ${result.transport && result.transport.planned_bytes
          ? `<p class="print-note">演练时按真实请求计算，需要上传 ${esc(humanBytes(result.transport.planned_bytes))}。</p>`
          : ""}
      </div>`;
  }

  function footer() {
    if (state.result) {
      return `<footer class="print-foot">
        <span class="print-note">${esc(state.result.ok ? "已发送到打印服务器；如果切片配置或文件类型不匹配，面板会给出原因。" : "没有启动打印，可以修改后重试。")}</span>
        <button class="btn primary" type="button" data-print-close>完成</button>
      </footer>`;
    }
    const ready = state.plan && state.plan.ok && state.confirmed && !state.sending;
    const label = state.allowPrint ? "确认发送并开始打印" : "演练发送";
    return `<footer class="print-foot">
      <span class="print-note">${esc(printerHint())}</span>
      <button class="btn" type="button" data-print-close>取消</button>
      <button class="btn primary" type="button" data-print-send ${ready ? "" : "disabled"}>${state.sending ? "正在发送…" : label}</button>
    </footer>`;
  }

  function render() {
    const host = mount();
    if (!state.printers.length) {
      host.innerHTML = shell(setupPanel());
      return;
    }
    const body = `
      <div class="print-section">
        <div class="print-section-head">
          <span class="eyebrow">PRINTER</span>
          <span class="print-mode ${state.allowPrint ? "real" : ""}">${esc(modeLabel())}</span>
        </div>
        <div class="print-printers">${printerList()}</div>
      </div>
      <div class="print-section">
        <div class="print-section-head">
          <span class="eyebrow">PRE-FLIGHT</span>
          <span class="print-note">${esc(state.file)}</span>
        </div>
        ${planPanel()}
      </div>
      ${state.result ? resultPanel() : `
        <label class="print-confirm">
          <input type="checkbox" data-print-confirm ${state.confirmed ? "checked" : ""} ${state.sending ? "disabled" : ""}>
          <span>我确认打印平台已清理、材料已装好，并允许打印机开始加热和移动。</span>
        </label>`}
      ${footer()}`;
    host.innerHTML = shell(body);
    const confirm = host.querySelector("[data-print-confirm]");
    const send = host.querySelector("[data-print-send]");
    if (confirm && send) {
      confirm.addEventListener("change", () => {
        state.confirmed = confirm.checked;
        const ready = Boolean(state.plan && state.plan.ok) && state.confirmed && !state.sending;
        send.disabled = !ready;
      });
    }
  }

  async function loadPrinters() {
    const response = await api("/api/printers");
    const payload = response.payload || {};
    state.printers = Array.isArray(payload.printers) ? payload.printers : [];
    state.allowPrint = Boolean(payload.allow_print);
    state.printerId = state.printers.length ? state.printers[0].id : "";
  }

  async function loadPlan() {
    if (!state.printerId) return;
    state.plan = null;
    state.planError = "";
    state.confirmed = false;
    render();
    const response = await api("/api/print/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: state.jobId,
        printer_id: state.printerId,
        file: state.file,
      }),
    });
    if (response.payload && response.payload.checks) {
      state.plan = response.payload;
    } else {
      state.planError = response.payload.error || "检查失败";
    }
    render();
  }

  async function send() {
    if (state.sending || !state.plan || !state.plan.ok) return;
    const confirm = root() && root().querySelector("[data-print-confirm]");
    if (!state.confirmed || !confirm || !confirm.checked) return;
    state.sending = true;
    state.result = null;
    render();
    const response = await api("/api/print", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: state.jobId,
        printer_id: state.printerId,
        file: state.file,
        confirm: true,
      }),
    });
    state.sending = false;
    state.result = response.payload.print || {
      ok: false,
      state: "failed",
      message: response.payload.error || "发送失败",
    };
    render();
    const spoken = state.result.ok
      ? (state.result.state === "rehearsed" ? "演练通过，没有连接打印机。" : "已发送到打印机，请留意打印机的提示。")
      : `没有发送：${state.result.message || "检查未通过"}`;
    announce(spoken);
  }

  async function open(options) {
    const jobId = (options && options.jobId) || "";
    if (!jobId) return;
    if (state.open) close();
    state.open = true;
    state.jobId = jobId;
    state.file = "model.stl";
    state.plan = null;
    state.planError = "";
    state.result = null;
    state.printers = [];
    state.sending = false;
    state.confirmed = false;
    state.triggeredBy = document.activeElement;
    mount().innerHTML = shell(`<div class="print-checks"><div class="print-check"><span class="print-check-dot"></span><span>正在读取打印机与模型信息…</span></div></div>`);
    announce("正在打开打印机设置。");
    try {
      await loadPrinters();
    } catch (error) {
      state.planError = "无法读取打印机配置。";
    }
    render();
    if (!state.printers.length) {
      announce("服务器还没有配置打印机。");
      return;
    }
    const first = root().querySelector("[data-print-pick]");
    if (first) first.focus();
    await loadPlan();
    const status = state.plan && state.plan.ok ? "检查通过，请确认后发送。" : "检查没有通过。";
    announce(status);
  }

  function bind() {
    document.addEventListener("click", (event) => {
      if (!state.open) return;
      const host = root();
      if (!host || !host.contains(event.target)) return;
      const pick = event.target.closest("[data-print-pick]");
      if (pick) {
        state.printerId = pick.getAttribute("data-print-pick") || "";
        loadPlan();
        return;
      }
      const sendButton = event.target.closest("[data-print-send]");
      if (sendButton) {
        send();
        return;
      }
      const closeButton = event.target.closest("[data-print-close]");
      if (closeButton) {
        // The backdrop carries the same attribute; only a click on the
        // backdrop itself (not inside the dialog) should dismiss.
        if (closeButton.classList.contains("print-backdrop") && event.target !== closeButton) return;
        event.preventDefault();
        close();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (!state.open) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    });
  }

  window.TouchLearnPrint = { open: open, close: close };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
