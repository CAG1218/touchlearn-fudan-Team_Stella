/**
 * TouchLearn voice assistant — blind and low-vision mode.
 *
 * The spoken copy and the intent rules live on the server
 * (`touchlearn.agent.voice`), so the phone and the desktop hear exactly the
 * same wording and reach exactly the same decisions. This file only does three
 * things:
 *
 *   1. speak the server's line with the Web Speech API,
 *   2. listen with SpeechRecognition and post the transcript to /api/voice/interpret,
 *   3. hand the finished request to the existing composer.
 *
 * No model calls, no framework, no build step.
 */
(function () {
  "use strict";

  const STORE_KEY = "touchlearn.voice.enabled";
  const MAX_READ = 700;
  const MAX_QUEUE = 4;

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  const FALLBACK = {
    greeting:
      "您好，这里是触见。需要开启盲人语音模式吗？开启后，我会把每一步念给您听。说 开启，或者 不用。",
    enabled_short: "好的，语音模式已开启。",
    ask_goal: "请问您想做什么样的触觉教材，或者三维模型？直接说出对象和用途就好。",
    disabled: "好的，语音模式已关闭。需要的时候，点右上角的语音助手按钮就能重新打开。",
    login_needed:
      "请先登录。您可以让家人帮忙输入手机号和验证码，登录完成后我会自动继续。",
    microphone_denied:
      "浏览器没有拿到麦克风权限。请允许麦克风后重新开启语音模式，也可以先用键盘输入。",
    unsupported:
      "当前浏览器不支持语音识别。语音播报仍然可用，您可以用键盘输入需求，我会念给您听。",
    not_understood: "抱歉，我没有听清。可以再说一次，也可以点输入框用键盘输入。",
    help: "您可以说：开启、关闭语音、触觉教材、三维模型、照片重建、开始、修改、取消、再说一遍。",
    draft_ready: "草稿已生成。说 开始生成，我就开始建模；说 修改，可以重新描述。",
    submitting: "收到，正在提交。",
    network: "网络不太顺畅，请稍后再说一次。",
  };

  const state = {
    on: false,
    stage: "greet",
    goal: "",
    modes: [],
    prompts: null,
    speaking: false,
    listening: false,
    busy: false,
    handoff: false,
    awaitingBuild: false,
    pendingSubmit: null,
    asrDenied: false,
    recognition: null,
    queue: [],
    seen: new WeakSet(),
    speakToken: 0,
    restartTimer: 0,
    watchdog: 0,
    afterSpeak: null,
    draftTimer: 0,
    errorCount: 0,
  };

  const RecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
  let cachedVoice = null;

  /* ------------------------------------------------------------------ */
  /* Spoken copy                                                        */
  /* ------------------------------------------------------------------ */

  function prompt(key) {
    if (state.prompts && typeof state.prompts[key] === "string") return state.prompts[key];
    return FALLBACK[key] || "";
  }

  function loadPrompts() {
    fetch("/api/voice/prompts", { credentials: "same-origin" })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (data && data.prompts && typeof data.prompts === "object") state.prompts = data.prompts;
      })
      .catch(() => {
        /* keep the embedded fallback */
      });
  }

  /* ------------------------------------------------------------------ */
  /* Text to speech                                                     */
  /* ------------------------------------------------------------------ */

  function pickVoice() {
    if (cachedVoice) return cachedVoice;
    const synth = window.speechSynthesis;
    if (!synth || typeof synth.getVoices !== "function") return null;
    const voices = synth.getVoices() || [];
    const chinese = voices.filter((voice) => /^zh/i.test(voice.lang || ""));
    if (!chinese.length) return null;
    cachedVoice =
      chinese.find((voice) => /zh[-_]?CN|Hans|Xiaoxiao|Yaoyao|Huihui|Kangkang/i.test(`${voice.lang} ${voice.name}`)) ||
      chinese[0];
    return cachedVoice;
  }

  function live(text) {
    const region = $("#voiceLive");
    if (!region) return;
    // Re-setting the same string does not re-announce in some screen readers,
    // so alternate a trailing zero-width space.
    region.textContent = region.textContent === text ? `${text}\u200b` : text;
  }

  function updateBar(text, mode) {
    const bar = $("#voiceBar");
    const label = $("#voiceBarText");
    if (!bar || !label) return;
    if (state.on) bar.classList.remove("hidden");
    else bar.classList.add("hidden");
    if (text) label.textContent = text;
    const dot = $("#voiceBarDot");
    if (dot) dot.dataset.mode = mode || (state.speaking ? "speaking" : state.listening ? "listening" : "idle");
  }

  function clearWatchdog() {
    if (state.watchdog) {
      clearTimeout(state.watchdog);
      state.watchdog = 0;
    }
  }

  function stopSpeech() {
    state.speakToken += 1;
    clearWatchdog();
    state.speaking = false;
    try {
      if (window.speechSynthesis) window.speechSynthesis.cancel();
    } catch (error) {
      /* nothing to cancel */
    }
  }

  function finishSpeech() {
    clearWatchdog();
    state.speaking = false;
    if (state.queue.length) {
      speak(state.queue.shift());
      return;
    }
    const next = state.afterSpeak;
    state.afterSpeak = null;
    if (typeof next === "function") {
      next();
      return;
    }
    resumeListening();
  }

  function speak(text) {
    if (!text) {
      finishSpeech();
      return;
    }
    const synth = window.speechSynthesis;
    const Utterance = window.SpeechSynthesisUtterance;
    if (!synth || typeof Utterance !== "function") {
      live(text);
      updateBar(text, "idle");
      state.speaking = false;
      const next = state.afterSpeak;
      state.afterSpeak = null;
      if (typeof next === "function") next();
      return;
    }

    state.speaking = true;
    state.lastPrompt = text;
    pauseListening();
    live(text);
    updateBar(text, "speaking");

    const token = ++state.speakToken;
    const run = () => {
      if (token !== state.speakToken) return;
      const utterance = new Utterance(text);
      utterance.lang = "zh-CN";
      utterance.rate = 0.92;
      utterance.pitch = 1;
      utterance.volume = 1;
      const voice = pickVoice();
      if (voice) utterance.voice = voice;
      utterance.onend = () => {
        if (token !== state.speakToken) return;
        finishSpeech();
      };
      utterance.onerror = () => {
        if (token !== state.speakToken) return;
        finishSpeech();
      };
      clearWatchdog();
      // A cancelled utterance sometimes never fires onend; the watchdog keeps
      // the dialog moving on slow or silent speech engines.
      state.watchdog = setTimeout(() => {
        if (token !== state.speakToken) return;
        finishSpeech();
      }, Math.min(45000, 3000 + text.length * 300));
      try {
        synth.speak(utterance);
      } catch (error) {
        finishSpeech();
      }
    };

    // Chrome drops a speak() issued in the same tick as cancel().
    try {
      if (synth.speaking || synth.pending) {
        synth.cancel();
        setTimeout(run, 70);
      } else {
        run();
      }
    } catch (error) {
      run();
    }
  }

  /** Interrupt whatever is playing and say this now. */
  function say(text, options) {
    if (!text) return;
    state.queue.length = 0;
    state.afterSpeak = options && typeof options.then === "function" ? options.then : null;
    stopSpeech();
    speak(text);
  }

  /** Append to the reading queue without cutting off the current sentence. */
  function enqueue(text) {
    if (!text) return;
    if (state.queue.length >= MAX_QUEUE) return;
    if (!state.speaking && !state.queue.length) {
      speak(text);
      return;
    }
    state.queue.push(text);
  }

  /* ------------------------------------------------------------------ */
  /* Speech recognition                                                 */
  /* ------------------------------------------------------------------ */

  function asrSupported() {
    return Boolean(RecognitionCtor);
  }

  function ensureRecognition() {
    if (state.recognition || !RecognitionCtor) return state.recognition;
    const recognition = new RecognitionCtor();
    recognition.lang = "zh-CN";
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.maxAlternatives = 3;
    recognition.onstart = () => {
      state.listening = true;
      updateBar(null, "listening");
    };
    recognition.onresult = onResult;
    recognition.onerror = onRecognitionError;
    recognition.onend = onRecognitionEnd;
    state.recognition = recognition;
    return recognition;
  }

  function startListening() {
    if (!state.on || state.speaking || state.busy || state.asrDenied) return;
    if (document.hidden || !asrSupported()) return;
    clearTimeout(state.restartTimer);
    const recognition = ensureRecognition();
    if (!recognition) return;
    try {
      recognition.start();
    } catch (error) {
      // "already started" is harmless; anything else retries on onend.
    }
  }

  function pauseListening() {
    clearTimeout(state.restartTimer);
    if (!state.recognition) return;
    try {
      state.recognition.abort();
    } catch (error) {
      /* not running */
    }
    state.listening = false;
  }

  function resumeListening() {
    if (!state.on || state.speaking || state.busy || state.asrDenied) return;
    if (!asrSupported()) return;
    clearTimeout(state.restartTimer);
    state.restartTimer = setTimeout(startListening, 420);
  }

  function onRecognitionEnd() {
    state.listening = false;
    updateBar(null, "idle");
    resumeListening();
  }

  function onRecognitionError(event) {
    const code = event && event.error;
    if (code === "aborted") return;
    if (code === "not-allowed" || code === "service-not-allowed" || code === "audio-capture") {
      state.asrDenied = true;
      say(prompt("microphone_denied"));
      return;
    }
    if (code === "no-speech") return;
    state.errorCount += 1;
    if (state.errorCount <= 2) {
      say(prompt("not_understood"));
    } else {
      state.asrDenied = true;
      say(prompt("unsupported"));
    }
  }

  function onResult(event) {
    const results = event.results;
    if (!results || !results.length) return;
    const result = results[results.length - 1];
    if (!result || !result.isFinal) return;
    let best = result[0];
    for (let index = 1; index < result.length; index += 1) {
      if ((result[index].confidence || 0) > (best.confidence || 0)) best = result[index];
    }
    const text = String((best && best.transcript) || "").trim();
    if (!text) return;
    state.busy = true;
    pauseListening();
    handleUtterance(text)
      .catch(() => say(prompt("network")))
      .then(() => {
        state.busy = false;
        if (!state.speaking && !state.queue.length) resumeListening();
      });
  }

  /* ------------------------------------------------------------------ */
  /* Dialog                                                             */
  /* ------------------------------------------------------------------ */

  function normalize(text) {
    return String(text || "")
      .replace(/[\s，。！？、；：,.!?;:…—～~“”"'‘’「」『』（）()【】[]《》<>·]/g, "")
      .trim();
  }

  function isLoginVisible() {
    const overlay = $("#loginOverlay");
    return Boolean(overlay && !overlay.classList.contains("hidden"));
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function focusLoginPhone() {
    const phone = $("#loginPhone");
    if (phone && !phone.closest(".hidden")) {
      try {
        phone.focus();
      } catch (error) {
        /* focus is best effort */
      }
    }
  }

  async function handleLoginUtterance(text) {
    const said = normalize(text);
    if (state.stage === "greet") {
      if (/(开启|需要|要开|打开|好|可以|行|是)/.test(said) && !/(不|别|不用)/.test(said)) {
        state.stage = "handoff";
        state.handoff = true;
        say(`${prompt("enabled_short")}${prompt("login_needed")}`, {
          then: focusLoginPhone,
        });
        return;
      }
      if (/(不用|不要|不需要|关闭|算了|不)/.test(said)) {
        say(prompt("disabled"), { then: () => turnOff({ silent: true }) });
        return;
      }
      say(prompt("greeting"));
      return;
    }
    if (/获取验证码|发送验证码|验证码/.test(said)) {
      const button = $("#loginSendCode");
      if (button) {
        button.click();
        say("正在发送验证码。收到后请用键盘输入六位数字，然后点登录。登录完成后我会自动继续。");
      } else {
        say(prompt("login_needed"));
      }
      return;
    }
    if (/登录|确认|好了|完成/.test(said)) {
      say("请用键盘输入完手机号和验证码后，点登录按钮。登录完成后我会自动继续。");
      return;
    }
    if (/帮助|怎么用|不会/.test(said)) {
      say(
        "现在在登录页。请先输入手机号，点获取验证码，收到短信后输入六位验证码，再点登录。需要的话可以让家人帮忙。登录完成后我会自动继续。"
      );
      return;
    }
    say("现在在登录页。登录完成后我会自动继续。需要帮助可以说 帮助。");
  }

  function buildButton() {
    const cards = $$("#messages [data-build]");
    return cards.length ? cards[cards.length - 1] : null;
  }

  function tryBuildCommand(text) {
    if (!state.awaitingBuild) return false;
    const said = normalize(text);
    if (/^(取消|不用|不做|等一?下|先不)/.test(said)) {
      state.awaitingBuild = false;
      clearTimeout(state.draftTimer);
      say("好的，先不生成。想修改的话说 修改。");
      return true;
    }
    if (/(修改|重说|重新说|不对|换一个|改一下)/.test(said)) {
      state.awaitingBuild = false;
      clearTimeout(state.draftTimer);
      state.stage = "detail";
      say("请把新的要求说一遍，我会覆盖上一条。");
      return true;
    }
    if (!/(开始|确认|生成|继续|可以|好了|行)/.test(said)) return false;
    const button = buildButton();
    if (!button) {
      state.awaitingBuild = false;
      say("还没有可确认的草稿。请先把需求说完。");
      return true;
    }
    state.awaitingBuild = false;
    clearTimeout(state.draftTimer);
    say(prompt("submitting"));
    button.click();
    return true;
  }

  function applyModes(modes) {
    if (!Array.isArray(modes) || !modes.length) return;
    const options = $$(".mode-option");
    const wanted = new Set(modes);
    const current = new Set(options.filter((button) => button.classList.contains("is-active")).map((button) => button.dataset.mode));
    // Activate first: the app refuses to leave zero modes selected.
    options.forEach((button) => {
      if (wanted.has(button.dataset.mode) && !current.has(button.dataset.mode)) button.click();
    });
    options.forEach((button) => {
      if (!wanted.has(button.dataset.mode) && current.has(button.dataset.mode)) button.click();
    });
  }

  function submitGoal(goal, modes) {
    if (isLoginVisible()) {
      state.pendingSubmit = { goal, modes };
      state.handoff = true;
      say(prompt("login_needed"));
      return;
    }
    applyModes(modes);
    const input = $("#promptInput");
    const form = $("#composerForm");
    if (!input || !form) return;
    input.value = goal;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    state.stage = "idle";
    state.awaitingBuild = false;
    form.requestSubmit();
    clearTimeout(state.draftTimer);
    state.draftTimer = setTimeout(() => {
      if (!state.on) return;
      if (buildButton()) {
        state.awaitingBuild = true;
        enqueue(prompt("draft_ready"));
      }
    }, 1200);
    // If the analysis fails there is no new agent text to restart the loop.
    setTimeout(() => {
      if (state.on) resumeListening();
    }, 25000);
  }

  async function handleUtterance(text) {
    if (isLoginVisible()) {
      await handleLoginUtterance(text);
      return;
    }
    if (tryBuildCommand(text)) return;

    const response = await postJSON("/api/voice/interpret", {
      stage: state.stage,
      text,
      context: { goal: state.goal, modes: state.modes },
    });

    state.stage = response.stage || state.stage;
    state.goal = response.goal || "";
    state.modes = Array.isArray(response.modes) ? response.modes : [];
    state.errorCount = 0;

    if (response.intent === "disable") {
      state.stage = "greet";
      state.goal = "";
      state.modes = [];
      say(response.say, { then: () => turnOff({ silent: true }) });
      return;
    }

    if (response.ready) {
      const goal = response.goal || state.goal;
      const modes = state.modes.slice();
      say(response.say, { then: () => submitGoal(goal, modes) });
      return;
    }

    say(response.say);
  }

  /* ------------------------------------------------------------------ */
  /* Page observation                                                   */
  /* ------------------------------------------------------------------ */

  function markSeen() {
    $$("#messages .agent-text").forEach((node) => state.seen.add(node));
  }

  function readAgentNode(node) {
    if (!state.on || state.seen.has(node)) return;
    state.seen.add(node);
    const text = String(node.textContent || "").replace(/\s+/g, " ").trim();
    if (!text) return;
    enqueue(text.length > MAX_READ ? `${text.slice(0, MAX_READ)}。内容较长，其余部分可以让我再念一遍。` : text);
  }

  function watchMessages() {
    const messages = $("#messages");
    if (!messages) return;
    const observer = new MutationObserver((records) => {
      records.forEach((record) => {
        record.addedNodes.forEach((node) => {
          if (!(node instanceof Element)) return;
          if (node.classList.contains("agent-text")) readAgentNode(node);
          $$(".agent-text", node).forEach(readAgentNode);
          if (node.matches("[data-build]") || node.querySelector("[data-build]")) {
            state.awaitingBuild = true;
            if (state.on) enqueue(prompt("draft_ready"));
          }
        });
      });
    });
    observer.observe(messages, { childList: true, subtree: true });
  }

  function watchLogin() {
    const overlay = $("#loginOverlay");
    if (!overlay) return;
    const observer = new MutationObserver(() => {
      if (isLoginVisible() || !state.on) return;
      const pending = state.pendingSubmit;
      state.handoff = false;
      state.pendingSubmit = null;
      if (pending) {
        say(`${prompt("enabled_short")}登录成功。`, { then: () => submitGoal(pending.goal, pending.modes) });
        return;
      }
      if (state.stage === "greet" || state.stage === "handoff") {
        state.stage = "goal";
        say(`登录成功。${prompt("ask_goal")}`);
      }
    });
    observer.observe(overlay, { attributes: true, attributeFilter: ["class"] });
  }

  /* ------------------------------------------------------------------ */
  /* Controls                                                           */
  /* ------------------------------------------------------------------ */

  function updateToggles() {
    $$("[data-voice-toggle]").forEach((button) => {
      button.setAttribute("aria-pressed", state.on ? "true" : "false");
      button.classList.toggle("is-on", state.on);
      if (button.id === "voiceToggle") {
        button.setAttribute("aria-label", state.on ? "关闭盲人语音模式" : "开启盲人语音模式");
        button.setAttribute("title", state.on ? "关闭盲人语音模式" : "开启盲人语音模式");
      }
    });
    const bar = $("#voiceBar");
    if (bar) bar.classList.toggle("hidden", !state.on);
  }

  function remember(value) {
    try {
      window.localStorage.setItem(STORE_KEY, value ? "1" : "0");
    } catch (error) {
      /* private mode: the session still works */
    }
  }

  function turnOn() {
    if (state.on) return;
    state.on = true;
    state.stage = "greet";
    state.goal = "";
    state.modes = [];
    state.asrDenied = false;
    state.errorCount = 0;
    state.handoff = isLoginVisible();
    remember(true);
    updateToggles();
    markSeen();
    updateBar(prompt("greeting"), "speaking");
    if (!asrSupported()) {
      say(prompt("unsupported"));
      return;
    }
    say(prompt("greeting"));
  }

  function turnOff(options) {
    const silent = Boolean(options && options.silent);
    state.on = false;
    state.stage = "greet";
    state.goal = "";
    state.modes = [];
    state.handoff = false;
    state.awaitingBuild = false;
    state.pendingSubmit = null;
    state.queue.length = 0;
    state.afterSpeak = null;
    clearTimeout(state.draftTimer);
    clearTimeout(state.restartTimer);
    remember(false);
    updateToggles();
    pauseListening();
    if (!silent) say(prompt("disabled"));
    if (!silent) updateBar(null, "idle");
  }

  function toggle() {
    if (state.on) turnOff({ silent: false });
    else turnOn();
  }

  function bindControls() {
    $$("[data-voice-toggle]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        // A user gesture also unlocks speechSynthesis on strict browsers.
        if (window.speechSynthesis && !state.on) {
          try {
            window.speechSynthesis.resume();
          } catch (error) {
            /* ignore */
          }
        }
        toggle();
      });
    });
    const replay = $("#voiceReplay");
    if (replay) {
      replay.addEventListener("click", () => {
        if (!state.on) return;
        const text = state.lastPrompt || prompt("greeting");
        say(text);
      });
    }
    const help = $("#voiceHelp");
    if (help) {
      help.addEventListener("click", () => {
        if (!state.on) return;
        say(prompt("help"));
      });
    }
    const close = $("#voiceClose");
    if (close) {
      close.addEventListener("click", () => turnOff({ silent: false }));
    }
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) pauseListening();
      else resumeListening();
    });
    window.addEventListener("pagehide", () => {
      pauseListening();
      stopSpeech();
    });
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                               */
  /* ------------------------------------------------------------------ */

  function boot() {
    loadPrompts();
    bindControls();
    watchMessages();
    watchLogin();
    updateToggles();

    if (window.speechSynthesis) {
      const refresh = () => {
        cachedVoice = null;
        pickVoice();
      };
      if (typeof window.speechSynthesis.addEventListener === "function") {
        window.speechSynthesis.addEventListener("voiceschanged", refresh);
      } else {
        window.speechSynthesis.onvoiceschanged = refresh;
      }
    }

    let remembered = false;
    try {
      remembered = window.localStorage.getItem(STORE_KEY) === "1";
    } catch (error) {
      remembered = false;
    }
    if (!remembered) return;

    // Browsers only allow speech after a gesture on some platforms; try right
    // away, then retry once the user touches the page.
    const autoStart = () => {
      if (state.on) return;
      turnOn();
    };
    setTimeout(autoStart, 700);
    const unlock = (event) => {
      if (event && event.target && event.target.closest && event.target.closest("[data-voice-toggle]")) return;
      if (!state.on) turnOn();
      document.removeEventListener("pointerdown", unlock);
      document.removeEventListener("keydown", unlock);
    };
    document.addEventListener("pointerdown", unlock);
    document.addEventListener("keydown", unlock);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
