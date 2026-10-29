# Fix Report - 下载文件格式、DeepSeek 与 TokenHub 接入

日期：2026-10-29

## 一、下载的 STL / ZIP 被识别为未知格式

### Bug 描述

用户在离线模式下用示例生成产物，下载得到：

- `model.stl 下载示例`
- `touchlearn-f070b13d8b80.zip示例预览`

Windows 把前者的类型显示为“文件”，后者显示为“ZIP示例预览文件”；Bambu Studio 无法打开 STL，系统也无法双击解压 ZIP。用 VS Code 或 Word 打开 STL 时看到二进制乱码。

### 问题根因

文件内容本身是完好的，问题在文件名：

1. 两个文件的扩展名后面都多了中文说明（`下载示例`、`示例预览`）。Windows 和 Bambu Studio 都按最后一段扩展名判断类型，`.stl 下载示例` 不等于 `.stl`，`.zip示例预览` 也不等于 `.zip`。
2. STL 是二进制网格，本来就不该用 VS Code 或 Word 阅读；看到乱码不代表文件损坏。
3. HTTP 下载响应缺少标准的 `Content-Disposition filename`，前端也没有给下载链接显式指定 `download` 文件名，浏览器只能沿用链接文字，容易把界面上的说明文字带进文件名。

验证方式：读取文件头。

- `model.stl 下载示例` 前 80 字节为合法二进制 STL 头，随后 4 字节小端整数为三角形数 28,932，文件长度 1,446,684 = 84 + 50 × 28,932，完全吻合。
- `touchlearn-f070b13d8b80.zip示例预览` 以 `PK\x03\x04` 开头，用 `zipfile` 可正常列出 14 个条目并通过 `testzip()`。

### 尝试记录

#### 尝试 1：重新生成模型，怀疑网格损坏

- 结果：失败（问题不在网格）
- 原因：二进制结构和三角形数量都正确，重新生成只会得到同样的合法文件。

#### 尝试 2：改用 ASCII STL

- 结果：未采用
- 原因：ASCII STL 体积更大，且不能解决扩展名被污染的问题；Bambu Studio 对二进制 STL 支持良好。

#### 尝试 3：修正下载文件名与响应头

- 结果：成功
- 原因：让保存到磁盘的文件名严格以 `.stl` / `.zip` 结尾，Windows 和切片软件即可按扩展名识别。

### 最终方案

1. `src/touchlearn/agent/server.py`
   - 新增 `content_disposition(filename)`，同时输出 RFC 6266 的 `filename` 与 `filename*=UTF-8''...`。
   - `_send_file()` 和整包 ZIP 接口统一使用该响应头。
   - MIME 保持 `model/stl` 与 `application/zip`。
2. `src/touchlearn/agent/static/app.js`
   - STL、ZIP、README、WAV、讲解稿、二维码等下载链接全部显式设置 `download="touchlearn-<job>-..."`，不再依赖链接文字。
   - 预览区增加提示：STL 用 Bambu Studio / PrusaSlicer / Cura 打开，ZIP 是标准压缩包，Word 和 VS Code 不是查看这两种文件的工具。
3. 回归测试
   - `test_stl_download_is_binary_stl_with_exact_filename`：检查 `Content-Type`、`Content-Disposition` 和 `84 + 50 × 三角形数`。
   - `test_bundle_download_is_a_real_zip`：检查 `PK\x03\x04`、ZIP 可读性和条目。
   - `test_content_disposition_encodes_unicode`：检查中文文件名编码。

实测下载：

```text
Content-Type: model/stl
Content-Disposition: attachment; filename="model.stl"; filename*=UTF-8''model.stl
triangles 39768, expected bytes 1988484, exact True

Content-Type: application/zip
Content-Disposition: attachment; filename="touchlearn-cadf7b92d090.zip"; ...
magic b'PK\x03\x04', zipfile ok True, entries 14, testzip None
```

### 检查清单

- [x] 对比官方文档：RFC 6266 / RFC 5987 的 `Content-Disposition` 文件名语法
- [x] 检查模块：`server.py` 下载路由、`app.js` 下载链接、MIME 映射
- [x] 字节级检查：STL 长度公式、ZIP 魔数与 `zipfile.testzip()`
- [x] 端到端检查：登录 → 分析 → 构建 → 下载 STL/ZIP
- [x] 性能检查：下载为流式文件发送，无额外内存拷贝

### 经验教训

- “文件打不开”不等于“文件内容坏了”，先看文件名和文件头。
- 二进制格式不要用文本编辑器验证。
- 下载接口应同时设置响应头和前端 `download` 属性，任何一端缺失都可能被浏览器或 WebView 改写文件名。

## 二、DeepSeek 接入

### 问题描述

1. 按旧文档配置 `deepseek-chat` 时接口不可用。
2. 视觉模型已选 DeepSeek，但只填了文本 KEY，视觉状态仍显示未配置。
3. 场景生成返回“接口返回为空”，看起来像接口故障。

### 根因

1. DeepSeek 已于 2026-07-24 下线 `deepseek-chat` / `deepseek-reasoner`。当前可用的是 `deepseek-v4-pro`（文本）与 `deepseek-flash`（文本 + 图像，V4.1）。
2. 视觉配置的密钥没有复用文本密钥，`_config_from_env("vision")` 在缺少 `TOUCHLEARN_VLM_KEY` 时直接返回 `None`。
3. DeepSeek V4 默认开启思考模式，推理 token 计入 `max_tokens`。场景生成原来只给 3200 token，推理耗尽预算后正文为空，`finish_reason=length`。

### 尝试记录

#### 尝试 1：继续使用 `deepseek-chat`

- 结果：失败
- 原因：`/models` 只返回 `deepseek-flash` 与 `deepseek-v4-pro`，旧模型已下线。

#### 尝试 2：只提高 `max_tokens`

- 结果：部分成功
- 原因：Flash + 8000 token 能成功，但 Pro 仍然很慢（一次 138 秒），且 3200 时依旧为空。

#### 尝试 3：关闭思考模式

- 结果：成功但未作为默认
- 原因：`thinking: {"type": "disabled"}` 响应约 1.5 秒，但复杂图形规划会损失推理质量。

#### 尝试 4：低推理强度 + 场景专用模型

- 结果：成功
- 原因：`reasoning_effort=low` 把预算留给正文；场景 JSON 改用 `deepseek-flash` 后，同一电路示例从 138 秒降到约 19–25 秒，图块从 1 个增加到 4 个。

### 最终方案

- 预置更新：`deepseek-v4-pro`（文本）、`deepseek-flash`（视觉与场景 JSON）。
- 同 provider 自动复用文本 KEY，视觉配置不必重复填密钥。
- 新增 `TOUCHLEARN_LLM_EFFORT`（`low`/`high`/`max`/`off`），DeepSeek 预置默认 `low`；`off` 发送 `thinking: {"type": "disabled"}`。
- 新增 `TOUCHLEARN_SCENE_MODEL`，DeepSeek 预置默认 `deepseek-flash`；图片场景仍使用视觉模型。
- 场景 JSON 预算提高到 8192 token；分类与函数抽取同步提高预算。
- `finish_reason=length` 不再显示“接口返回为空”，而是明确提示提高 `max_tokens` 或降低推理强度。
- 测试隔离：`tests/__init__.py` 在导入前清空所有 `TOUCHLEARN_LLM_*` / `TOUCHLEARN_VLM_*`，单元测试绝不访问付费接口。

### 验证

```text
TEXT   configured=True  provider=deepseek  model=deepseek-v4-pro   effort=low
VISION configured=True  provider=deepseek  model=deepseek-flash    effort=low
场景生成：model=deepseek-flash, ok=True, 25.0s, blocks=4, confidence=0.82
单元测试：Ran 97 tests ... OK
```

### 检查清单

- [x] 对比官方文档：DeepSeek 模型下线公告、`reasoning_effort`、`thinking` 开关
- [x] 检查模块：`llm.py`、`scene.py`、`vision.py`、`router.py`、`.env.example`、`docs/providers.md`
- [x] 性能检查：场景生成 138s → 约 25s；简单对话约 2.5s
- [x] 安全检查：密钥仅存本机 `.env`，不写作业目录、不出现在 API 响应和日志

### 经验教训

- 推理模型的 `max_tokens` 包含隐藏推理 token，结构化输出必须留足预算。
- 模型下线要先查服务商 `/models` 和控制台公告，不能沿用旧文档。
- 快模型不一定差：长 JSON 任务用 Flash 反而更快、更完整；Pro 留给需要推理的文本判断。

## 三、腾讯云 TokenHub 接入

### 问题描述

需要在不改业务代码的前提下接入腾讯云 TokenHub，并让意图分类、函数抽取、场景生成和读图都走可用模型。TokenHub 控制台默认展示的是 Anthropic 兼容页，模型列表中有 `deepseek-v4.1-flash`、`glm-5.3`、`kimi-k3.1-pro`、`hy-vision-2.0-instruct` 等。

### 根因与适配点

1. TokenHub 的 OpenAI 兼容端点是 `https://tokenhub.tencentmaas.com/v1/chat/completions`，新增一个预置即可复用现有传输层。
2. Windows 注册表残留 `http://127.0.0.1:7890` 代理，代理未启动时 `urllib` 会等待死地址，表现为所有请求超时，而 `curl` 正常。
3. `kimi-k3.1-pro` 拒绝 `temperature` 字段，直接返回 HTTP 400。
4. `glm-5.3` 系列拒绝 `thinking` 字段，但接受 `reasoning_effort`。
5. TokenHub 上的 `deepseek-v4.1-flash`、`glm-5.3-flash` 开启思考后，推理 token 会占满 8192 预算，场景 JSON 被截断。

### 尝试记录

#### 尝试 1：直接用 urllib 调 TokenHub

- 结果：失败
- 原因：`URLError(TimeoutError('_ssl.c:1064: The handshake operation timed out'))`；同一条 curl 请求 0.2 秒返回。`urllib.request.getproxies()` 返回了已失效的 `127.0.0.1:7890`。

#### 尝试 2：绕过系统代理

- 结果：成功
- 原因：传输层默认 `ProxyHandler({})`，需要代理时用 `TOUCHLEARN_LLM_PROXY` 显式开启。`/v1/models` 0.2 秒返回。

#### 尝试 3：Kimi K3.1 Pro 带 temperature

- 结果：失败（HTTP 400）
- 原因：模型侧拒绝 `temperature: 0`。对 `kimi-k3.1` 前缀不发送该字段后正常。

#### 尝试 4：场景生成沿用 low 推理

- 结果：失败
- 原因：`glm-5.3-flash` 145.7 秒仍 `finish_reason=length`；`deepseek-v4.1-flash` 约 28 秒、只出 4 个图块。关闭思考后分别为约 8 秒 / 7.8 秒，5 个图块且 JSON 完整。

### 最终方案

- 新增 `tokenhub` 预置：`deepseek-v4.1-flash`（文本与场景）、`deepseek/deepseek-flash`（视觉），`default_effort=low`、`scene_effort=off`。
- 新增 `TOUCHLEARN_SCENE_EFFORT`，DeepSeek 与 TokenHub 预置默认 `off`；文字场景与图片场景都使用该值，避免视觉模型把预算耗在思考上。
- 推理参数按 provider 收敛：`deepseek` 关闭思考发 `thinking`，`tokenhub` 发 `reasoning_effort: "none"`，其他 provider 不发送推理字段。
- `kimi-k3.1` 前缀不发送 `temperature`；需要更高质量时可把 `TOUCHLEARN_LLM_MODEL` 切到 `kimi-k3.1-pro` 或 `deepseek-v4-pro`。
- 默认忽略系统代理，新增 `TOUCHLEARN_LLM_PROXY` 显式覆盖。
- `.env` 主用 TokenHub，DeepSeek 直连保留为注释备用；文档、示例和设置面板同步更新。

### 验证

```text
TEXT   provider=tokenhub  model=deepseek-v4.1-flash       effort=low
VISION provider=tokenhub  model=deepseek/deepseek-flash   effort=low
分类：scene 5.5s / function 3.2s / scene 4.0s，置信度 0.95-0.98
函数：2X+1、X^2-2X 均解析正确，各 3.3s
场景：5 个图块，5.6-7.5s；水循环 5 个图块，结构化草稿 provider=tokenhub
读图：界面截图 5.0s，置信度 1.0，正确读出 y=x^2-2x-3；图片转场景 16.1s，7 个图块
构建：job b2abeceb586e 完成，STL 2,246,484 B / 44,928 三角面，ZIP 2,590,531 B / 14 项
下载：Content-Type=model/stl、application/zip，Content-Disposition 正常
单元测试：Ran 104 tests ... OK
```

图片转场景还有一处修复：最初对视觉模型也不传推理强度，TokenHub 的 `deepseek/deepseek-flash` 在 8192 token 下 43.4 秒后 `finish_reason=length`。改为同样使用 `TOUCHLEARN_SCENE_EFFORT=off` 后，同一张图 7.0 秒返回 7 个图块。

### 检查清单

- [x] 对比官方文档：TokenHub OpenAI 兼容端点、`/v1/models`、`reasoning_effort`
- [x] 检查模块：`llm.py`、`scene.py`、`.env`、`.env.example`、`docs/providers.md`、`docs/deploy.md`、`app.js`、测试
- [x] 性能检查：分类 3.2-5.5s；场景 5.6-7.5s；关闭思考后不再截断
- [x] 安全检查：Key 只在本机 `.env`；`/api/providers` 只返回 `has_key`；未提交、未推送

### 经验教训

- “curl 通、Python 不通”优先查系统代理，而不是怀疑接口或证书。
- 聚合平台的同名模型参数支持可能不同，按 provider/model 分别适配，不要假设 OpenAI 字段全可用。
- 结构化 JSON 任务关闭思考通常更快、更完整；需要推理的判断类任务才保留低强度思考。
