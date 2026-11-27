# Fix Report - 自定义板材尺寸与盲人语音模式改进

日期：2026-11-27

## 背景

决赛前的自查提出三类改进：盲人模式要接近 Siri / 系统级无障碍的可用度、智能体架构要经得起追问、触觉板尺寸不能只给六档固定规格。本次只做低风险、能当场演示、可回归验证的部分；结构性重构写进 [`docs/improvement-plan-2026-11-27.md`](../improvement-plan-2026-11-27.md)，不赶在决赛前动。

## 一、触觉板尺寸自由选择

### 问题描述

1. 用户只能说 A5 / B5 / 16K / A4 / B4 / A3，遇到打印机只有 200×280 的纸、或学校统一采购 190×260 的卡纸时无法出图。
2. `choose_paper` 只认目录名，`parse_canvas` 之后虽然只校验正数，但上游 `canvas.size` 的 JSON Schema 是 `enum`，自定义尺寸会被 schema 直接拒绝。

### 问题根因

- 纸张规格在四个层次各写了一份：`paper.py` 的目录、`schema.py` 的一致性校验、`diagram.schema.json` 的 `enum`、前端 `PAPER_CHOICES`。
- 尺寸信息以“名字”为唯一键，没有“名字里带尺寸”的通道，所以任何自定义值都会在某一层被当成未知纸张。

### 方案

采用“名称内嵌尺寸”的最小改造：自定义板命名为 `自定义<宽>×<高>`（单位毫米），目录纸张行为不变。

- `src/touchlearn/paper.py`
  - 新增 `CUSTOM_PREFIX`、`CUSTOM_MIN_SIDE_MM=148`、`CUSTOM_MAX_SIDE_MM=420`、`CUSTOM_MIN_AREA_MM2=148×210` 与 `_CUSTOM_RE`；
  - `custom_name()` / `custom_size()` / `is_custom()` 负责生成与解析；
  - `paper_size()` 先尝试自定义，再查目录；
  - `paper_name()` 在最近目录误差超过 2 mm 时返回自定义名，避免把 200×280 误报成 A4；
  - `parse_paper()` 识别 `自定义200×280`、`CUSTOM 200*280`；**显式**写了“自定义”但越界时抛出中文 `ValueError`，正文里偶然出现的 `200x280` 不劫持自动选型；
  - `canvas_for()` 增加 `custom` 布尔字段，前端和报告可直接判断。
- `schemas/diagram.schema.json`、`src/touchlearn/data/diagram.schema.json`
  - `canvas.size` 由 `enum` 改为锚定 `pattern`，允许 `自定义W×H`；
  - 补充 `custom: boolean`，因为 `canvas` 仍是 `additionalProperties: false`。
- `schema.py` 无需改动：它只对目录名核对宽高，自定义尺寸由 `parse_paper` 与 `min(width,height)` 规则保证。

### 边界

- 短边不小于 148 mm，长边不大于 420 mm，面积不小于 A5；更小放不下承诺的盲文行数，更大属于海报，需分块打印（尚未实现）。
- 保留用户输入的方向：`自定义280×200` 不会被自动转成竖版。
- 只支持矩形；圆角、装订孔、非矩形异形板不在本次范围。

## 二、盲人语音模式（对标 Siri 的最小闭环）

### 问题描述

1. 语速写死 0.92，读得快或读得慢的用户都无法调整。
2. 没有“停止朗读”，只能等整段念完或关掉整个语音模式。
3. 有语音合成时，同一句话既进 `speechSynthesis`、又写 `#voiceLive`，屏幕阅读器用户会听到两遍。
4. 语音模式开启时 `#messages` 仍是 `aria-live="polite"`，与自建语音叠加播报。
5. 全站只有局部控件有 `:focus-visible`，键盘用户看不到焦点；没有跳过导航的入口。
6. 弹层没有焦点陷阱：打开纸规格、设置或场景编辑器后，连续 Tab 会走到背景页面，盲人用户失去位置。
7. 构建期间只有进度条，没有语音反馈；提交后几分钟内听不到任何状态。
8. 错误只在小角落弹 toast，语音模式不播报。

### 修改

- `voice.js`
  - 语速四档 0.75 / 0.92 / 1.15 / 1.4，持久化到 `localStorage["touchlearn.voice.rate"]`，按钮文案同步“语速 慢/标准/快/更快”；
  - 新增 `stopSpeech()` 的队列清空与 `window.touchlearnVoice.stop()/isSpeaking()`；
  - 新增 `notify()`：进度类播报入队，不打断当前朗读；
  - 有合成器时不再写 `live()`，只在无合成器降级路径写 live region；
  - `turnOn()` 把 `#messages` 的 `aria-live` 置 `off`，`turnOff()` 恢复 `polite`；
  - 新增 `updateRate()`、`cycleRate()`、`setMessagesLive()`。
- `index.html`
  - 语音栏增加“停止”“语速”两个按钮；
  - 增加 `.skip-link`（跳到输入框）；
  - 纸张弹层增加自定义宽高输入与“应用”按钮。
- `app.js`
  - `PAPER_CHOICES` 之外接受 `自定义W×H`；`paperEntry()` 支持自定义条目；`choosePaper()` 用边界函数校验；
  - `applyCustomPaper()` 做前端校验并给出中文提示；
  - Esc 的第一优先级改为停止朗读（Siri 行为），再依次关闭弹层；
  - 纸张按钮从无效的 `role="option"` 改为 `aria-pressed`；
  - 跳过链接点击后聚焦输入框；
  - 新增焦点陷阱工具 `pushFocusTrap/popFocusTrap`，接入纸规格、模型、设置、模板、账户、场景编辑器与登录页；Tab 在容器内循环，关闭后焦点回到触发按钮；登录页出现前先解除背景弹层的陷阱；
  - 轮询作业时按 `jobStepIndex` 在阶段切换时播报服务端的具体动作，完成/失败各播报一次；
  - `toast(message, true)` 在语音模式下同步复述错误；
  - 登录失败同样通过语音复述，`#loginError` 增加 `role="alert"`；
  - 纸张选择成功后播报新的板材规格，自定义尺寸校验失败时同时更新提示、弹 toast 并聚焦宽度输入。
- `styles.css`
  - 全局 `:where(a, button, input, textarea, select, summary, [tabindex]):focus-visible`；
  - `.skip-link` 样式；
  - `.paper-custom` 输入块；
  - 窄屏下语音栏允许换行，避免五个按钮挤掉正文。

## 三、验证

### 自动化

| 命令 | 结果 |
|---|---|
| `python -m unittest discover -t . -s tests -p "test_paper.py"` | 11 项通过（新增 5 项自定义尺寸用例） |
| `python -m unittest discover -t . -s tests -p "test_schema.py"` | 9 项通过 |
| `python -m unittest discover -t . -s tests -p "test_voice.py"` | 39 项通过 |
| `python -m unittest discover -t . -s tests -p "test_agent_server.py"` | 26 项通过 |
| `python -m unittest discover -t . -s tests -p "test_agent_pipeline.py"` | 20 项通过（192 s，含真实布局与 STL 构建） |
| `node --check app.js` / `node --check voice.js` | 语法通过 |

### 真实构建

用 `examples/quadratic/diagram.json` 把 canvas 换成 `自定义200×280`，经 `_checked_diagram`（JSON Schema + 内置语义）与 `_compile`：

- schema 与语义校验：通过；
- 可打印性：PASS，0 条 issue；
- STL：`model.stl`，体积约 169716.9 mm³（A5 基线约 191651.4 mm³ 的 88.5%，与面积比例一致）。

再用智能体完整链路（`analyze` → `build`，`paper=自定义200×280`）：

- 作业 `status=done`；
- `diagram.json` 的 `canvas` 为 `{size: 自定义200×280, width_mm: 200, height_mm: 280, custom: true}`；
- `compliance.json` `printable=true`、0 issue；
- `manifest.print.size_mm = [200, 280, 3.8]`，`paper` 字段为 `自定义200×280`。

### 静态资源

本地起服务后 `/`、`/static/app.js`、`/static/voice.js` 均返回 200，页面 111 个 id 无重复，新增的 `paperCustomW/H/Apply/Hint`、`voiceStop`、`voiceRate`、`skipLink` 各出现一次。

## 四、未做与已知限制

- 自定义尺寸没有“分块打印 / 拼接定位标记”，超过 A3 仍只能换大机器或改小内容。
- 语音模式已有焦点陷阱、阶段播报和错误播报，但语音指令仍只覆盖生成流程；“换纸”“读合规报告”这类操作还需要键盘或触摸。
- 尚未提供“系统读屏优先”开关；VoiceOver / TalkBack 与自建语音同时开启时仍可能重复。
- 语速只影响自建 `speechSynthesis`，不影响系统 VoiceOver / TalkBack 的语速（那由系统设置控制）。
- 前端仍是单文件 `app.js`，本次只做局部修补，没有拆模块。

## 五、经验

- 尺寸这类“名字即数据”的字段，先用“名字内嵌尺寸”打通全链路，比引入新字段、改四处调用点的风险低得多。
- 无障碍问题的根因常常不是缺功能，而是**同一句话被播报两次**；先消除重复，再谈新增。
- Esc 的优先级应按“打断正在发生的事”排序：先停语音，再关弹层，最后才是丢弃页面状态。