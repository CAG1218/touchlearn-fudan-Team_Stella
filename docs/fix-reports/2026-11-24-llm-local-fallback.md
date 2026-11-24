# Fix Report - 云端接口失败时缺少本地模型回落

日期：2026-11-24　范围：模型接入层、降级演练、单元测试、发布包

## 现象

- 云端 LLM 接口超时或返回 HTTP 500 时，文本任务直接失败，没有本地兜底；
- 本地 Qwen2.5-3B（llama.cpp，`127.0.0.1:8080/v1`）已单独跑通，但未接入应用层；
- 首次三场景演练结果为 `passed: false`，回落分支未按预期触发。

## 问题根因

1. `src/touchlearn/agent/llm.py` 只有单一 provider 调用链，没有第二通道；
2. 判断「是否为本地模型」用的是 `ProviderConfig.local`（按 host 判断），演练 stub 也监听 127.0.0.1，
   于是云端主通道被误判成本地，回落逻辑直接跳过；
3. `_timeout()` 把超时钳制到最小 5 秒，stub 只 sleep 5 秒时不会触发超时；
4. 发布包带顶层目录 `touchlearn-0.4.0/`，服务器解压到子目录，构建用的仍是旧源码，新功能没有上线。

## 尝试记录

### 尝试 1：用 `primary.local` 决定是否回落

- 结果：不可行
- 原因：演练 stub 与真实本地模型都在 127.0.0.1，主通道被误判。

### 尝试 2：stub sleep 5 秒、超时设 5 秒

- 结果：未触发超时
- 原因：`_timeout()` 有 `max(5.0, ...)` 下限，边界值不可靠。

### 尝试 3：比较主通道与兜底通道的 endpoint，并加大 stub 延时

- 结果：成功
- 原因：只有两个 endpoint 不同才重放请求，语义正确且不依赖 host 判断。

## 最终方案

`src/touchlearn/agent/llm.py`：

- 原 `chat()` 改名为 `_chat_once()`，签名与行为不变；
- 新增 `_local_fallback_config(kind)`：读取 `TOUCHLEARN_LOCAL_FALLBACK`、`TOUCHLEARN_LOCAL_URL`、
  `TOUCHLEARN_LOCAL_MODEL`、`TOUCHLEARN_LOCAL_TIMEOUT`，仅 `kind="text"` 生效；
- 新增 `chat()` 包装：主通道失败后，若 `primary.endpoint != fallback.endpoint` 则用兜底通道重放，
  成功时 message 标注「云端异常，已回落本地模型」；
- `provider_status()` 两个分支都返回 `fallback` 字段，环境变量清单补充 4 个 `TOUCHLEARN_LOCAL_*`。

配套：

- `scripts/degrade_drill.py`：不可达 / 超时 / HTTP 500 三场景，输出 JSON（`status`、`cases`、`passed`）；
- `tests/test_agent_llm_fallback.py`：云端失败回落、开关关闭不回落、本地主用不重试、图片识别不回落；
- `.env.example`：新增「本地模型兜底（可选）」配置块。

发布包同步改为平铺结构（不再带 `touchlearn-0.4.0/` 前缀），部署脚本解压后直接覆盖项目目录。

## 验证结果

| 场景 | 回落 | 回落耗时 | 全流程 | 结果 |
|---|---|---:|---:|---|
| 接口不可达（127.0.0.1:1） | 是 | 679 ms | 2733 ms | 通过 |
| 超时（stub sleep 12s，超时 5s） | 是 | 290 ms | 5331 ms | 通过 |
| HTTP 500 | 是 | 275 ms | 303 ms | 通过 |

- 单元测试：`python -m unittest tests.test_agent_llm_fallback tests.test_agent_llm tests.test_agent_router -v`，47 项全部通过；
- 部署核验：阿里云 ECS 容器内 `agent/llm.py` 含 `TOUCHLEARN_LOCAL_FALLBACK`，`/api/health` 返回 `ok: true`、`0.4.0`；
  `provider_status('text')` 返回含 `fallback` 字段（服务器未部署本地模型，值为 null，符合预期）。

## 检查清单

- [x] 确认回落只在文本任务生效，图片识别保持原失败语义；
- [x] 用 endpoint 比较替代 host 判断，避免 stub 误判；
- [x] 演练参数与 `_timeout()` 下限对齐，不依赖边界值；
- [x] 三个失败场景都记录耗时与结果，产出 JSON 证据；
- [x] 部署后回到容器内核对代码，而不是只看构建日志。

## 经验教训

- 「是否本地」不能只看主机名：测试替身通常也在 127.0.0.1，必须比较具体 endpoint。
- 配置项有下限钳制时，演练参数要留出余量，否则会得到「假通过」或「假失败」。
- 发布包结构要和部署脚本的假设一致；解压后先验证目标文件内容，再相信构建日志。
