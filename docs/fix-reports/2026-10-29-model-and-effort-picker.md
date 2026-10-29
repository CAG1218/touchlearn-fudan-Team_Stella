# Fix Report - 模型与思考强度改为用户可选

日期：2026-10-29

## 需求描述

原先把模型和思考强度当成部署配置：服务端读 `.env` 里的 `TOUCHLEARN_LLM_MODEL` / `TOUCHLEARN_LLM_EFFORT`，每个请求都使用同一套值。用户提出：

- 不要预设思考强度和模型选择；
- 只保留一个初始默认值；
- 在页面上像 WorkBuddy 一样提供模型菜单，用户可以自行选择实例提供的模型；
- 思考强度作为二级菜单，提供低 / 中 / 高。

这不是一个“改文案”的需求，涉及前端交互、请求协议、服务端校验、模型路由、场景生成、读图、持久化和测试。

## 根因与改造点

### 1. 选择入口缺失

旧界面只在状态栏和设置里显示当前模型，没有可操作的选择器。用户无法在不改 `.env`、不重启服务的情况下换模型。

### 2. 请求协议没有选择字段

`/api/analyze` 只接收 `subject`、`text`、`files` 等字段，模型和强度完全由服务端决定。即使前端加了菜单，也没有通道把选择带到后端。

### 3. 模型名没有白名单

如果把前端传来的 `model` 直接转发给服务商，接口会变成任意模型代理。必须在服务端维护可选目录并校验。

### 4. 场景与读图各有自己的默认值

`scene.generate_scene` 会读 `TOUCHLEARN_SCENE_MODEL` / `TOUCHLEARN_SCENE_EFFORT`，`vision.interpret_image` 使用视觉配置；它们不会继承用户当次选择。图片请求还需要区分“用户选了文本模型”和“用户选了视觉模型”。

### 5. 高强度可能截断长 JSON

场景 JSON 有 8192 token 预算。中 / 高强度下隐藏推理可能吃满预算，返回 `finish_reason=length`。不能因为用户选了高强度就直接失败。

## 方案

### 服务端：目录 + 校验 + 透传

`src/touchlearn/agent/llm.py`

- 新增 `MODEL_CATALOG`：按 provider 列出可展示的模型，每项包含 `id`、`label`、`short`、`tag`、`note`、`vision`。
- 新增 `EFFORT_OPTIONS`：`low` / `medium` / `high`，各带中文标签和说明；`EFFORT_LEVELS` 用于校验。目录同时返回 `supports_effort`，只有 `deepseek` / `tokenhub` 这类真正支持 `reasoning_effort` 的预置才显示强度菜单。
- 新增 `model_catalog()`：返回当前实例的 `configured`、`provider`、`label`、`default`、`models`、`efforts`、`note`。环境变量指定但不在目录里的模型会自动补进列表，保证自定义网关仍有一个可选项。
- 新增 `resolve_selection(model, effort)`：校验模型是否在白名单、强度是否合法；未知值抛出 `ValueError`，由 API 转成 HTTP 400。选中纯文本模型时，`vision_model` 回退到实例默认视觉模型；选中视觉模型时直接用它读图。
- `_default_ui_effort()` 把服务端的 `off` 映射成选择器里的 `low`，避免页面出现一个看不见的默认值。

`src/touchlearn/agent/agent.py`

- `/api/analyze` 在写上传文件、生成草稿之前先调用 `resolve_selection`，非法选择不会留下半成品。
- 选择透传给分类、函数抽取、图片理解和场景生成。
- 草稿写入 `fields["llm"]`：provider、label、model、effort、vision_model、requested。构建产物和 API 响应不包含密钥。

`src/touchlearn/agent/router.py`、`vision.py`、`scene.py`

- 分类、函数抽取、读图、场景生成都接受 `model` / `effort` 覆盖。
- 图片请求统一走 `vision_model`；文本请求走用户选的文本模型。
- 任何长 JSON 调用在 `finish_reason=length` 且强度不是 `off` 时，自动用 `off` 重试一次，并在草稿备注里说明“已自动关闭隐藏推理重试”。

### 前端：WorkBuddy 风格选择器

`src/touchlearn/agent/static/index.html` / `app.js` / `styles.css`

- 输入框上方的模型按钮显示“短名 · 思考强度”，点击打开浮层。
- 模型列表展示名称、标签（默认 / 通用 / 强文本 / 读图 / 预览）、说明和选中勾。
- “思考强度”作为列表中的一项，右侧显示当前值；点进去是低 / 中 / 高二级菜单，带返回按钮。
- 选择保存在 `localStorage` 的 `touchlearn.llm.selection.v1`；刷新页面后保留，服务端目录变化或模型下线时自动回退到新的默认值。
- 如果页面缓存的选择在提交时已被服务端下线，收到“不支持所选模型”后会自动重新拉取目录、回退到初始默认，并提示用户重试。
- 只有 `models.configured` 为真时才在请求里带 `model` / `effort`；离线模式保持原样。
- 草稿卡片增加本次实际使用的模型标签；设置面板增加“当前选择”和“选择模型与思考强度”入口。
- 点击浮层外部、按 Escape、打开设置或模板时关闭浮层。
- “初始默认”徽标由服务端 `default` 字段动态生成，不再把“默认”写死在目录条目里；改 `TOUCHLEARN_LLM_MODEL` 后，徽标会跟着移动，不会出现两个“默认”。

## 验证

```text
node --check src/touchlearn/agent/static/app.js        通过
python -m compileall -q src                            通过
python -m unittest discover -t . -s tests              Ran 125 tests ... OK

GET /api/providers
  configured=True  provider=tokenhub
  default={model: deepseek-v4.1-flash, effort: low, vision_model: deepseek/deepseek-flash}
  models=9 项（含 2 个读图模型）  efforts=low/medium/high

POST /api/analyze
  {"text":"请生成函数 y=x^2 的触觉教材图","model":"glm-5.3","effort":"medium"}
  → 25.1s，subject=function
  → fields.llm={provider: tokenhub, label: GLM-5.3, model: glm-5.3,
                effort: medium, vision_model: deepseek/deepseek-flash, requested: true}
  → fields.extraction.effort=medium

POST /api/analyze  {"model":"not-a-real-model"}
  → HTTP 400，返回“不支持所选模型：not-a-real-model。可选：…”
```

## 检查清单

- [x] 对比官方文档：OpenAI 兼容 `chat/completions` 的 `model` 字段；TokenHub / DeepSeek 对 `reasoning_effort` 的差异沿用上一轮实测结论
- [x] 检查模块：`llm.py`、`agent.py`、`router.py`、`vision.py`、`scene.py`、`server.py`、`app.js`、`index.html`、`styles.css`
- [x] 协议检查：`/api/providers` 返回目录；`/api/analyze` 可选 `model` / `effort`；非法值 HTTP 400
- [x] 性能检查：默认低强度不变；GLM-5.3 中强度函数链路 25.1s，属于该模型正常范围；长 JSON 截断有自动降级
- [x] 安全检查：模型白名单阻止任意模型代理；密钥不进入前端、草稿或 API 响应
- [x] 离线检查：未配置模型时目录为空，页面显示“离线规则”，请求不带选择字段
- [x] 回归检查：125 个单元测试全部通过，覆盖目录、校验、默认标记、透传、视觉模型、截断重试和服务端 400

## 经验教训

- “用户可选”必须在 API 边界校验，不能把浏览器传来的模型名直接转发；白名单同时也是成本边界。
- 选择要一次性解析成 `selection` 对象再向下传，避免每个子系统各读一次环境变量，出现“页面显示 A、实际请求 B”。
- 面向用户的强度档位和服务端的内部档位要分开：`off` 是工程兜底，不应出现在选择器里。
- 长 JSON 任务不能假设用户会选低强度；截断后的自动降级比重试整次请求更可靠。
- 前端持久化要允许失效：模型下线、目录变化、换设备时都要能回退到服务端默认值。
