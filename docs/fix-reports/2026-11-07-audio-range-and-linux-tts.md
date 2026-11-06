# Fix Report - 扫码音频在手机上不可播 / 服务器没有语音

日期：2026-11-07　版本：v0.3.5

## Bug 描述

用户要求验证两个细节：作业产出的二维码是否指向正确地址、扫码后的讲解音频是否真能播放。验证时发现两处问题：

1. 静态文件不支持 HTTP Range。`<audio>` 元素在 iOS Safari 上会先发 `Range: bytes=0-1` 探测；服务端始终返回 200 和完整文件，Safari 会拒绝播放或不允许拖动进度条。
2. 生产环境是阿里云 ECS 上的 `python:3.12-slim` 容器，没有 PowerShell，也就没有 `System.Speech`。原来的语音链路只有 Windows SAPI 一条路径，服务器上每次都在音频步骤失败，前端退回浏览器 `speechSynthesis`——手机扫码听到的不是作业里预生成的讲解。

## 问题根因

**Range**：`server.py::_send_file` 只设置 `Content-Type`、`Content-Length` 和 `Cache-Control`，完全忽略 `Range` 请求头，也没有 `Accept-Ranges`。移动端媒体播放器依赖分段请求，缺失时行为不一致。

**语音**：`synthesize_speech` 直接调用 `synthesize_windows_tts`，后者用 `subprocess` 启动 PowerShell 加载 `System.Speech.Synthesis.SpeechSynthesizer`。Linux 镜像里既没有 `powershell` 也没有该程序集，异常被吞成“语音生成失败”，作业仍然完成，所以问题只在产物里可见（`manifest.audio.wav_ready=false`）。

## 尝试记录

### 尝试 1：只在容器里装 PowerShell 和中文语音包

- 结果：放弃
- 原因：`powershell` + `SpeechSynthesis` + 中文 voice 在 slim 镜像里体积增加数百 MB，且微软不再为 Linux 提供桌面语音包，成功率不可控。

### 尝试 2：接云端 TTS API

- 结果：未采用
- 原因：需要额外账号和密钥，与“部署包开箱可用”的目标冲突，也给评委复现增加门槛。

### 尝试 3：edge-tts

- 结果：成功
- 原因：纯 Python、无需密钥、`pip install edge-tts` 即可；中文 `zh-CN-XiaoxiaoNeural` 质量稳定；输出 MP3 体积约为同长度 WAV 的 1/10。代价是依赖微软的朗读端点，需要容器出网，属于非官方接口。

## 最终方案

1. `audio.py` 新增 `synthesize_edge_tts()`：`asyncio.run(asyncio.wait_for(...))` + 两次尝试（间隔 1 秒）+ 120 秒超时；失败时删除小于 1 KB 的残片并返回可读原因。
2. `synthesize_speech()` 变为引擎选择器：
   - `TOUCHLEARN_TTS_ENGINE=auto`（默认）：Windows 先试 SAPI，失败再试 edge；非 Windows 直接走 edge。
   - `sapi` / `edge` 可强制单一引擎，方便排查。
   - `TOUCHLEARN_TTS_VOICE` 覆盖音色。
   - 返回 `(ok, message, filename)`，由调用方决定文件名。
3. `agent.py` 写入 `audio_file`、`audio_format`、`engine`、`audio_ready`；保留 `wav_ready` / `wav_file` 兼容旧清单和客户端。合规检查在脚本与音频都存在时才算 PASS。
4. `app.js` 根据 `audio_file` 扩展名选择播放器和“下载 WAV / MP3”文案；无音频时保留浏览器朗读降级。
5. `server.py::_send_file` 支持单段 Range：`bytes=a-b`、`bytes=a-`、`bytes=-n`，返回 206 + `Content-Range` + `Accept-Ranges: bytes`；越界返回 416 和 `bytes */total`；多段请求按整文件 200 处理。
6. `pyproject.toml` 增加 `tts = ["edge-tts>=6.1"]`，`Dockerfile` 安装 `.[docs-reader,tts]`；版本升到 0.3.5。

## 检查清单

- [x] 对比官方文档：Python `http.server` 的 Range 语义（RFC 7233）；edge-tts 7.x 的 `Communicate.save()` 异步接口。
- [x] 检查模块：`audio.py`、`agent.py`、`server.py`、`static/app.js`、`qr.py`、`Dockerfile`、`pyproject.toml`。
- [x] 性能检查：MP3 比 WAV 小一个数量级；TTS 在作业线程内串行执行，不阻塞 HTTP 线程；Range 只在已读入内存的文件上切片，不额外读盘。
- [x] 真实合成：`temporary/verify-edge-tts/jobs/9316bc07eafa/narration.mp3`，433,728 字节，约 72.3 秒，`engine=edge-tts`。
- [x] HTTP 验证：完整请求 200 + `audio/mpeg`；`Range: bytes=0-1023` 返回 206；越界返回 416。
- [x] 回归测试：`301 passed, 32 subtests passed`。

## 经验教训

- “作业成功”不等于“产物可用”。音频失败过去只写进清单，界面仍然显示完成，必须把关键产物的可用性提升为合规项。
- 部署平台的差异要落到构造函数里，而不是靠文档提醒。引擎选择器比“记得在 Linux 上别调 SAPI”更可靠。
- 移动端媒体播放要按 RFC 7233 实现 Range，不能只靠 `Content-Length`。
