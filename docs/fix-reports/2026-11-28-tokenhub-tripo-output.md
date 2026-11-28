# Fix Report - TokenHub Tripo 任务完成后取不到 GLB

日期：2026-11-28　范围：移动端 Web 应用「照片级 3D 重建」及「文字生成 3D」的 TokenHub 路径

## 一、Bug 描述

线上（47.103.61.115:8765）用照片做「照片级 3D 重建」，第三步「触觉产物」直接失败：

```text
作业 9bd95133258c · TokenHub 任务完成但没有返回 GLB（返回格式：无）。
生成没有完成。
TokenHub 任务完成但没有返回 GLB（返回格式：无）。
```

同一条链路在「触觉 3D 模型 → 生成式路线」上也会以相同错误结束。HY-3D 模型的旧配置没有受影响。

## 二、问题根因

`reconstruct.py::_tokenhub_results()` 只认两种返回结构：

```python
raw = payload.get("data", payload.get("result"))
```

这是混元 3D 的返回结构（`data` 是 `{"type": "glb", "url": ...}` 数组）。但 v0.4.0 起图生默认模型改成 `tripo-3d-p1`、文生默认 `tripo-3d-3.1`，腾讯云 TokenHub 的 Tripo-3D 文档里查询返回是**单个 `output` 对象**：

```json
{
  "status": "completed",
  "input": {"prompt": "一只可爱的小猫", "pbr": true, "texture": true},
  "output": {
    "type": "text_to_model",
    "model_url": "https://vcg-prod-.../******12.glb",
    "generated_image_url": "https://vcg-prod-.../******12.png",
    "rendered_image_url": "https://vcg-prod-.../******12.png"
  },
  "credits_consumed": 50
}
```

`payload.get("data", payload.get("result"))` 取到 `None` → `items` 为空 → `urls` 为空 → 抛「返回格式：无」。任务本身是成功的，积分也已扣（真实的 GLB 已经生成，只是从来没被下载过）。

另有一个容易踩的细节：Tripo 返回里的 `type` 是**任务类型**（`text_to_model` / `image_to_model`），不是文件类型。如果直接沿用旧代码「把 `type` 当格式名」，会把 `text_to_model` 当成一种文件格式写进结果表，仍然拿不到 `glb` 键。

## 三、尝试记录

### 尝试 1：把 `output` 当成 `data` 的别名

- 结果：不完整
- 原因：`output` 里没有 `url` 字段，只有 `model_url`；旧代码的 URL 键表里虽然有 `model_url`，但根本没走到 `output`。

### 尝试 2：直接解析 `output.model_url` 并固定记为 `glb`

- 结果：不完整
- 原因：能救 Tripo，但遇到 `output.type` 是任务名时会误判格式；混元和 Rodin 的 `data` 结构、市场模型只给 `name`+`url` 的情况也照顾不到。

### 尝试 3：按容器递归 + 格式白名单 + 后缀推断

- 结果：成功
- 原因：同时覆盖三种官方结构，也不怕网关再多套一层。

## 四、最终方案

`src/touchlearn/agent/reconstruct.py`

- 新增 `_result_items()`：从 `data` / `result` / `output` 出发递归（最多 4 层，兼容 `outputs` / `files` / `models` / `items`），只认带下载或预览 URL 的字典。
- 新增 `MODEL_KINDS` 与 `_url_suffix()`：只有 `glb / gltf / obj / fbx / usdz` 才算模型格式。
- 重写 `_tokenhub_results()`：
  - 模型地址依次取 `model_url` / `glb_url` / `url` / `file_url` / `download_url`；
  - 格式先看 `type` / `format` / `file_type`，但**只接受模型格式白名单**，否则回退到 URL 后缀；
  - URL 带签名、看不到后缀时按文档默认记为 `glb`；`.zip`、`.png` 不会被硬塞成 `glb`；
  - 预览图依次取 `preview_image_url` / `rendered_image_url` / `generated_image_url` / `thumbnail_url` / `preview` / `image_url`。
- `_tokenhub_download()` 的报错补上「响应字段」，下次网关再变形时能直接看到顶层键名，不用重跑任务。

同步在 `docs/providers.md` 补一句三种返回结构的差异。

## 五、验证

### 官方文档比对

- [x] TokenHub「Tripo-3D 调用指南」查询示例：`output.type` / `output.model_url` / `output.generated_image_url` / `output.rendered_image_url`（<https://cloud.tencent.com/document/product/1823/137074>）
- [x] TokenHub「HY-3D-3.1/3.0」查询示例：`data[]` 里 `{type: "glb", url}` + `preview_image_url`
- [x] TokenHub「Rodin-Gen-3D-2.5」查询示例：`data[]` 里 `{name: "xxx.glb", url}`

### 自动化测试

```text
tests/test_reconstruct.py        37 passed
全量 unittest discover          447 passed, 354 s
```

新增三条断言覆盖：官方 Tripo `output` 结构、`image_to_model` 与 Rodin `data` 结构、多套一层的 `data.output`、无后缀签名 URL。

### 本地端到端（不花钱）

`temporary/e2e_tokenhub_tripo.py` 起了一个本地桩服务，按官方示例返回 `output.model_url`，走真实的 `reconstruct()`：提交 → 轮询 `in_progress` → `completed` → 下载 GLB/预览图 → GLB 解析。

```text
submit body : {"model": "tripo-3d-p1", "input": "data:image/png;base64,..."}
mode        : image_to_3d
output types: ['glb']
files       : ['glb', 'thumbnail']
RESULT      : PASS
```

### 尚未完成

- [ ] 线上真实重跑一次（需部署后由用户在页面发起；本次修复未消耗新的 TokenHub 积分）
- [ ] 部署到阿里云 ECS 并确认容器内版本

## 六、经验教训

1. **同一网关下的模型族返回结构不同**。`data`、`result`、`output` 是三家供应商各自的形状，新增模型族时必须对着该模型的查询示例写解析，而不是复用上一个模型的分支。
2. **`type` 不一定是文件类型**。Tripo 用 `type` 表示 `text_to_model` / `image_to_model`；把字段名当语义用，迟早会把任务名当成文件格式。
3. **“任务完成”和“拿到产物”是两件事**。错误信息应该带上响应里实际出现的字段名，否则线上只能看到「无」。
4. **花了钱却没下载到，是最贵的失败**。同类接口改动应优先做一条「只查询、不提交」的回放测试。