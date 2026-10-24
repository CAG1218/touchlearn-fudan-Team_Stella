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
| `deepseek` | DeepSeek | `deepseek-chat` | 无 | 是 |
| `dashscope` | 阿里云百炼 / 通义千问 | `qwen-plus` | `qwen-vl-max` | 是 |
| `moonshot` | 月之暗面 / Kimi | `moonshot-v1-8k` | `moonshot-v1-8k-vision-preview` | 是 |
| `zhipu` | 智谱 GLM | `glm-4-flash` | `glm-4v-flash` | 是 |
| `ollama` | 本地 Ollama / vLLM | `qwen2.5:7b` | `qwen2.5vl:7b` | 否 |
| `custom` | 任意 OpenAI 兼容接口 | 自填 | 自填 | 否 |

预置只是默认值，`URL` 和 `MODEL` 永远可以覆盖。模型名会随服务商更新，正式演示前请以服务商控制台为准。

## 最小配置

复制 `.env.example` 为 `.env`，至少填文本模型：

```dotenv
TOUCHLEARN_LLM_PROVIDER=dashscope
TOUCHLEARN_LLM_KEY=sk-...
```

视觉模型有两种配法：

1. **复用文本连接**：如果文本预置本身带视觉模型（`dashscope`、`zhipu`、`moonshot`、`openai`、`ollama`），不填 `TOUCHLEARN_VLM_*` 也会自动使用该预置的视觉模型。
2. **单独配置**：自定义网关或文本/视觉不是同一家时，填 `TOUCHLEARN_VLM_PROVIDER/URL/KEY/MODEL`。

读取顺序：真实环境变量优先，其次当前目录 `.env`，其次仓库根目录 `.env`。`TOUCHLEARN_ENV_FILE` 可以指向另一个文件。密钥不写入作业目录，不通过 API 返回，状态接口只给 `has_key: true/false`。

## 环境变量

| 变量 | 作用 |
|---|---|
| `TOUCHLEARN_LLM_PROVIDER` | 文本服务预置名 |
| `TOUCHLEARN_LLM_URL` | 文本接口基地址 |
| `TOUCHLEARN_LLM_KEY` | 文本接口密钥 |
| `TOUCHLEARN_LLM_MODEL` | 文本模型名 |
| `TOUCHLEARN_VLM_PROVIDER` | 视觉服务预置名 |
| `TOUCHLEARN_VLM_URL` | 视觉接口基地址 |
| `TOUCHLEARN_VLM_KEY` | 视觉接口密钥 |
| `TOUCHLEARN_VLM_MODEL` | 视觉模型名 |
| `TOUCHLEARN_LLM_TIMEOUT` | 单次请求超时秒数，默认 75，范围 5–300 |
| `TOUCHLEARN_ENV_FILE` | 可选的额外 `.env` 路径 |

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
| `GET /api/providers` | 两套模型的配置状态、预置列表、所需环境变量 |
| `GET /api/scene-templates` | 8 个离线场景模板 |
| `POST /api/analyze` | 生成草稿；`subject` 可为 `auto/function/robot/mesh/scene` |
| `POST /api/build` | 确认草稿并构建产物 |
| `GET /api/jobs/<id>` | 构建进度与产物清单 |

`/api/providers` 不返回密钥，只返回 `has_key`、`endpoint_host`、`model` 等可公开信息。

## 失败时的行为

- 未配置模型：分类走关键词，函数走正则，场景只能匹配离线模板；页面明确显示“离线”。
- 接口超时/HTTP 错误/返回非 JSON：草稿保留本地候选，并记录失败原因；不会把空结果当成成功。
- 视觉模型未配置：图片仍保存为来源证据，草稿提示需要补充文字或配置视觉接口。
- 模型给出无法解析的表达式或超出限值的场景：本地校验拒绝，要求用户修改，不进入构建。

## 测试与费用

单元测试用本机 `127.0.0.1` 上的临时 HTTP mock，不会调用任何付费接口，也不需要真实密钥。测试覆盖端点拼接、JSON 抽取、请求头、错误响应和“未配置”状态。正式演示前如需真机联调，请自己设置环境变量并确认调用费用。
