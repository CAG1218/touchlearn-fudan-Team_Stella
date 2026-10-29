# 大模型接入

TouchLearn 只把大模型用在**理解**环节：判断用户想做什么、从文字或图片里读函数、把教材图转成归一化场景结构。几何、STL 和合规判定仍是确定性的，模型不能改 PASS/FAIL。

## 为什么是一层而不是每家一套

所有预置服务都兼容 OpenAI 的 `POST /chat/completions` 协议，所以仓库只有一个传输层 `src/touchlearn/agent/llm.py`，换供应商只改环境变量，不改业务代码。预置里的 `base_url` 是**基地址**，不是完整端点；传输层会自动补 `/chat/completions`：

- `https://api.openai.com/v1` → `https://api.openai.com/v1/chat/completions`
- `http://127.0.0.1:11434/v1` → `http://127.0.0.1:11434/v1/chat/completions`
- 已经以 `/chat/completions` 结尾的完整地址会原样使用。

## 预置

| `*_PROVIDER` | 服务 | 默认文本模型 | 默认视觉模型 | 需要密钥 |
|---|---|---|---|---|
| `openai` | OpenAI | `gpt-4o-mini` | `gpt-4o-mini` | 是 |
| `deepseek` | DeepSeek | `deepseek-v4-pro` | `deepseek-flash` | 是 |
| `tokenhub` | 腾讯云 TokenHub | `deepseek-v4.1-flash` | `deepseek/deepseek-flash` | 是 |
| `dashscope` | 阿里云百炼 / 通义千问 | `qwen-plus` | `qwen-vl-max` | 是 |
| `moonshot` | 月之暗面 / Kimi | `moonshot-v1-8k` | `moonshot-v1-8k-vision-preview` | 是 |
| `zhipu` | 智谱 GLM | `glm-4-flash` | `glm-4v-flash` | 是 |
| `ollama` | 本地 Ollama / vLLM | `qwen2.5:7b` | `qwen2.5vl:7b` | 否 |
| `custom` | 任意 OpenAI 兼容接口 | 自填 | 自填 | 否 |

预置只是默认值，`URL` 和 `MODEL` 永远可以覆盖。模型名会随服务商更新，正式演示前请以服务商控制台为准。`deepseek` 预置会把场景 JSON 交给 `deepseek-flash`，文本理解仍用 `deepseek-v4-pro`；`tokenhub` 预置文本与场景都用 `deepseek-v4.1-flash`，读图用 `deepseek/deepseek-flash`。

## 模型与思考强度由用户选择

环境变量只提供**初始默认值**。页面输入框上方的模型按钮会列出当前实例可用的模型与思考强度，用户切换后只作用于下一次请求，并保存在浏览器本地（`localStorage` 的 `touchlearn.llm.selection.v1`）；不写回 `.env`，也不影响其他设备。

- 初始默认：`TOUCHLEARN_LLM_MODEL` 与 `TOUCHLEARN_LLM_EFFORT`；TokenHub / DeepSeek 预置默认低强度。
- 可选项：服务端 `MODEL_CATALOG` 白名单；环境变量指定但不在白名单里的模型会自动补进列表，保证自定义网关仍有一个可选项。
- 每个请求可以带 `model` 与 `effort`；服务端先校验再转发，未知模型或非法强度直接返回 HTTP 400，避免把接口变成任意模型代理。
- 选择只影响理解环节（分类、函数抽取、读图、场景 JSON）。几何、STL 与合规判定仍由本地确定性代码完成，不因换模型而改变。
- 页面上的思考强度只有低 / 中 / 高三档；内部另有 `off`，仅用于长 JSON 被截断时的自动重试，不出现在选择器里。

`GET /api/providers` 的 `models` 字段：

```json
{
  "configured": true,
  "provider": "tokenhub",
  "label": "腾讯云 TokenHub",
  "default": {
    "model": "deepseek-v4.1-flash",
    "effort": "low",
    "vision_model": "deepseek/deepseek-flash"
  },
  "models": [
    {
      "id": "deepseek-v4.1-flash",
      "label": "DeepSeek-V4.1-Flash",
      "short": "V4.1 Flash",
      "tag": "快速",
      "note": "响应快，适合分类与结构化输出",
      "vision": false,
      "default": true
    }
  ],
  "efforts": [
    {"id": "low", "label": "低", "note": "响应更快，适合大多数分类与结构化任务"},
    {"id": "medium", "label": "中", "note": "在速度与推理之间取平衡"},
    {"id": "high", "label": "高", "note": "复杂描述更稳，响应更慢"}
  ],
  "supports_effort": true,
  "note": "模型由当前服务商提供；切换只影响下一次请求。"
}
```

只有支持 `reasoning_effort` 的预置（当前是 `deepseek` 与 `tokenhub`）会显示思考强度；其他预置只显示模型列表，避免给出一个实际不生效的开关。

`POST /api/analyze` 可选字段：

```json
{"subject": "auto", "text": "画出 y=x^2 的函数图像", "model": "glm-5.3", "effort": "medium"}
```

不带这两个字段时使用服务端初始默认。`model` 不在白名单返回 HTTP 400；`effort` 由选择器发送 `low` / `medium` / `high`，服务端另接受内部值 `off`（关闭隐藏推理，页面不展示），其他值返回 HTTP 400。若选择了纯文本模型但请求带图片，读图仍走该实例的默认视觉模型；草稿的 `fields.llm` 会记录本次实际使用的 provider、模型、强度和视觉模型，便于复现。

## 腾讯云 TokenHub

TokenHub 是腾讯云的模型聚合服务，一个 Key 同时提供 DeepSeek、GLM、Kimi、MiniMax、混元等模型，接口兼容 OpenAI：

```dotenv
TOUCHLEARN_LLM_PROVIDER=tokenhub
TOUCHLEARN_LLM_KEY=sk-...
TOUCHLEARN_LLM_MODEL=deepseek-v4.1-flash
TOUCHLEARN_VLM_PROVIDER=tokenhub
TOUCHLEARN_VLM_MODEL=deepseek/deepseek-flash
```

实测（本机、单次调用）：

| 模型 | 意图分类 | 函数抽取 | 场景 JSON |
|---|---|---|---|
| `deepseek-v4.1-flash` | 3.2–5.5 s，置信度 0.95–0.98 | 3.3 s，解析正确 | 5.6–7.5 s，5 个图块 |
| `glm-5.3-flash` | 2.9–6.0 s，置信度 0.70–0.98 | 未测 | 未测 |
| `kimi-k3.1-pro` | 6.6–19.7 s，置信度正确 | 未测 | 未测 |

`deepseek-v4.1-flash` 在速度和准确率之间最均衡，因此作为 TokenHub 预置默认值。需要更强的写作/推理质量时，直接在页面模型菜单里切到 `kimi-k3.1-pro` 或 `deepseek-v4-pro` 即可，代码和 `.env` 都不用改；把环境变量改成它们则会让该模型成为新的初始默认。

TokenHub 各模型对参数的支持并不完全一致，传输层已做适配：

- `kimi-k3.1-pro` 拒绝 `temperature`（HTTP 400），对该模型不发送该字段。
- `glm-5.3` 系列拒绝 `thinking`，但接受 `reasoning_effort`；关闭思考时发送 `reasoning_effort: "none"`。
- 场景 JSON 的初始强度为 `off`。实测开启思考时 `glm-5.3-flash` 会用 145 s 仍被 `max_tokens` 截断，关闭后 8 s 左右返回完整 JSON；页面选择中/高强度时会先按用户选择执行，截断后再自动降级重试。

## 最小配置

复制 `.env.example` 为 `.env`，至少填文本模型：

```dotenv
TOUCHLEARN_LLM_PROVIDER=dashscope
TOUCHLEARN_LLM_KEY=sk-...
```

视觉模型有两种配法：

1. **复用文本连接**：如果文本预置本身带视觉模型（`deepseek`、`tokenhub`、`dashscope`、`zhipu`、`moonshot`、`openai`、`ollama`），不填 `TOUCHLEARN_VLM_*` 也会自动使用该预置的视觉模型；同一 provider 下视觉密钥留空时会复用文本密钥。
2. **单独配置**：自定义网关或文本/视觉不是同一家时，填 `TOUCHLEARN_VLM_PROVIDER/URL/KEY/MODEL`。

读取顺序：真实环境变量优先，其次当前目录 `.env`，其次仓库根目录 `.env`。`TOUCHLEARN_ENV_FILE` 可以指向另一个文件。密钥不写入作业目录，不通过 API 返回，状态接口只给 `has_key: true/false`。

## 环境变量

| 变量 | 作用 |
|---|---|
| `TOUCHLEARN_LLM_PROVIDER` | 文本服务预置名 |
| `TOUCHLEARN_LLM_URL` | 文本接口基地址 |
| `TOUCHLEARN_LLM_KEY` | 文本接口密钥 |
| `TOUCHLEARN_LLM_MODEL` | 初始文本模型；页面可随时切换 |
| `TOUCHLEARN_VLM_PROVIDER` | 视觉服务预置名 |
| `TOUCHLEARN_VLM_URL` | 视觉接口基地址 |
| `TOUCHLEARN_VLM_KEY` | 视觉接口密钥 |
| `TOUCHLEARN_VLM_MODEL` | 初始视觉模型；选择纯文本模型时，带图请求回退到该模型 |
| `TOUCHLEARN_LLM_TIMEOUT` | 单次请求超时秒数，默认 75，范围 5–300 |
| `TOUCHLEARN_LLM_EFFORT` | 初始思考强度：`low` / `medium` / `high`（内部 `off` 仅用于自动重试）；页面可随时切换 |
| `TOUCHLEARN_SCENE_MODEL` | 场景 JSON 的初始模型；留空用文本模型或预置的 `scene_model`；页面选择优先 |
| `TOUCHLEARN_SCENE_EFFORT` | 场景 JSON 的初始强度；DeepSeek 与 TokenHub 预置默认 `off`；页面选择优先 |
| `TOUCHLEARN_LLM_PROXY` | 仅当必须走代理时填写，如 `http://127.0.0.1:7890`；默认忽略系统代理 |
| `TOUCHLEARN_ENV_FILE` | 可选的额外 `.env` 路径 |

## 推理模型与输出预算

DeepSeek V4 系列默认开启思考模式，隐藏的推理 token 也计入 `max_tokens`。早期版本给场景 JSON 只留 3200 token，推理一多就会返回空正文，看起来像“接口坏了”。现在的处理是：

- DeepSeek / TokenHub 预置默认 `TOUCHLEARN_LLM_EFFORT=low`，把大部分预算留给正文；用户可在页面上切到中或高，要最快响应也可以由服务端配置为 `off`。
- 场景生成使用 8192 token 预算，`deepseek` 预置换用 `deepseek-flash`，`tokenhub` 预置换用 `deepseek-v4.1-flash`。长 JSON 不需要思考，服务端默认把场景推理设为 `off`（`TOUCHLEARN_SCENE_EFFORT`）；用户在页面选定的强度优先，若因此被截断，会自动用 `off` 重试一次并在草稿里注明。
- 若仍被截断，错误信息会直接写 `finish_reason=length` 并提示提高 `max_tokens`，不再用“接口返回为空”掩盖原因。

## Windows 系统代理

Windows 注册表里常有 Clash/V2Ray 留下的 `http://127.0.0.1:7890`。代理没启动时，`urllib` 会先等这个死地址，导致所有模型请求超时，而 `curl` 看起来正常。传输层默认构造 `ProxyHandler({})` 忽略系统代理；确实需要代理时设置 `TOUCHLEARN_LLM_PROXY`，只影响模型请求，不影响其他网络代码。

## 四段流程里模型做什么

```text
用户输入
  ├─ 文字/PDF/TXT/JSON ── 本地读取文字
  ├─ 图片 ─────────────── 视觉模型（未配置则只留档，不猜）
  └─ STL/OBJ ──────────── 本地解析网格
        ↓
  意图分类（模型 + 关键词兜底）
        ↓
  结构化草稿（函数解析式 / 机械臂参数 / 归一化场景 JSON）
        ↓
  用户在页面确认、修改
        ↓
  确定性几何 + 合规检查（模型不参与）
        ↓
  STL / 预览 / 报告 / 音频 / 二维码
```

- **分类**：`router.classify_request`。有显式解析式、规格或网格时，硬证据优先；否则让模型判断，失败时退回关键词。
- **函数**：`router.extract_function`。模型只给候选解析式，仍要经过本地表达式解析器；解析不了就拒绝。
- **场景**：`scene.generate_scene`。模型输出归一化 0–100 坐标的 JSON，本地归一化、限块数、限图元、剔除不可打印字符；模型不能给毫米尺寸。
- **图片**：`vision.interpret_image`。未配置视觉模型时返回“只作为来源留档”，不会从图片猜公式。
- **合规**：`standards.check_plate` 等规则引擎，纯本地确定性检查。

## 接口

启动本地服务：

```bash
python scripts/run_agent.py --port 8765
```

| 接口 | 用途 |
|---|---|
| `GET /api/health` | 服务状态，含 `llm` 与 `vision` 配置摘要 |
| `GET /api/providers` | 两套模型的配置状态、可选模型目录、初始默认、思考强度 |
| `GET /api/scene-templates` | 8 个离线场景模板 |
| `POST /api/analyze` | 生成草稿；`subject` 可为 `auto/function/robot/mesh/scene`，可选 `model` / `effort` |
| `POST /api/build` | 确认草稿并构建产物 |
| `GET /api/jobs/<id>` | 构建进度与产物清单 |

`/api/providers` 不返回密钥，只返回 `has_key`、`endpoint_host`、`model`、`models` 等可公开信息。选择的模型与强度会随草稿保存在 `fields.llm`，但不会写入构建产物或对外暴露密钥。

## 失败时的行为

- 未配置模型：分类走关键词，函数走正则，场景只能匹配离线模板；页面明确显示“离线”，模型菜单只显示不可用的说明。
- 用户选了服务端已下线的模型：`/api/analyze` 返回 HTTP 400 并列出可选模型，页面提示后回退到初始默认，不会静默改用别的模型。
- 接口超时/HTTP 错误/返回非 JSON：草稿保留本地候选，并记录失败原因；不会把空结果当成成功。
- 视觉模型未配置：图片仍保存为来源证据，草稿提示需要补充文字或配置视觉接口。
- 模型给出无法解析的表达式或超出限值的场景：本地校验拒绝，要求用户修改，不进入构建。

## WorkBuddy 积分能不能直接接进来

不能把 WorkBuddy 个人版积分当作第三方 API 计费。WorkBuddy 是腾讯云的桌面 AI 智能体，积分是客户端内的资源配额；官方文档开放的是“把外部模型接进 WorkBuddy”，不是“把 WorkBuddy 积分开放给外部应用调用”。参见 [WorkBuddy 积分说明](https://www.workbuddy.cn/docs/workbuddy/Credits) 和 [腾讯云 WorkBuddy 配置指南](https://cloud.tencent.com/document/product/1823/131902)。

可行的替代路径：

1. 使用 DeepSeek、阿里云百炼等服务商的官方 API，也就是本文的接入方式。
2. 如果账号里有腾讯云 TokenHub 的 API Key，可以直接用本项目的 `tokenhub` 预置接入（OpenAI 兼容接口，见上一节）。TokenHub 是独立于 WorkBuddy 客户端积分的计费体系。
3. 只把 WorkBuddy 当作上层客户端使用：在 WorkBuddy 里添加 DeepSeek 自定义模型，请求走外部 API，不消耗 WorkBuddy 积分。方向和本项目的接入相反，但能降低 WorkBuddy 侧的积分消耗。

不建议使用非官方的“WorkBuddy 积分转 OpenAI API”网关：它通常需要保存账号登录 token，稳定性和服务条款都有风险。

## 测试与费用

单元测试用本机 `127.0.0.1` 上的临时 HTTP mock，不会调用任何付费接口，也不需要真实密钥。测试覆盖端点拼接、JSON 抽取、请求头、错误响应和“未配置”状态。正式演示前如需真机联调，请自己设置环境变量并确认调用费用。
