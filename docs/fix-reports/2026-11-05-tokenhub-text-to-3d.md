# Fix Report - 接入 TokenHub 文生 3D，3D 模式改为生成式 + 本地统一后处理

## 需求描述

用户明确指出：

1. TokenHub 里已有多个高质量 3D 大模型，需要真正生成精致、复杂、完整的 3D 模型，不能继续用“球 + 盒子 + 圆柱”拼基础几何体；
2. 不再要求把部件名刻到对应表面；
3. 只保留统一的后处理：标题、底座、方向适配，以及原有的缩放、中文盲文、二维码位、可打印性与合规检查。

## 问题根因

1. **能力判断过时**。此前文档和代码都假设“TokenHub 3D 只接受图片”。实际情况是 TokenHub 的 HY-3D 与 Tripo 模型均支持纯 `prompt` 文生 3D，只是图生 3D 不允许 `prompt` 与照片同时提交。
2. **中间表示太弱**。本地结构化编译器只有有限图元，尾翼、水杯、地球仪、台灯这类曲面/薄壁/复合结构很难靠图元拼出可信外形，泛化性必然差。
3. **部件级语义不可得**。第三方生成的网格是一个整体，没有“这是杯柄、那是翅膀”的分割和坐标，继续在表面刻猜出来的名字会误导盲人用户。
4. **职责边界需要重划**。生成层应该只负责“像不像”，本地确定性层负责“能不能摸、能不能读、能不能打印”。

## 尝试记录

### 尝试 1：继续扩展本地图元库

- 结果：失败。
- 原因：图元数量增加只能覆盖少数形状，曲面、倒角、薄壁和有机轮廓仍然失真；与“生成精致复杂模型”的目标相悖。

### 尝试 2：文生图 → 图生 3D 两段式

- 结果：不作为主路径。
- 原因：多一次生成会引入风格漂移和细节损失，链路更长、成本更高；TokenHub 已验证可直接文生 3D。

### 尝试 3：TokenHub prompt-only 文生 3D

- 结果：成功。
- 原因：`POST /v1/api/3d/submit` 接受 `prompt`，`POST /v1/api/3d/query` 轮询任务，完成后返回 GLB。按模型族发送不同质量参数即可稳定适配。

## 最终方案

### 1. 新增统一入口 `text_to_3d()`

- 输入：生成提示词、`ReconstructionPlan`、输出目录、进度回调；
- 按 `TOUCHLEARN_3D_PROVIDER` 分派到 TokenHub / Tripo / Meshy；
- TokenHub 路径：
  - 提交：`{"model", "prompt"≤1024, "result_format": "GLB", ...质量参数}`；
  - 查询：`{"model", "id"}`，轮询到 `completed`；
  - 下载：优先 `glb`，有预览图则一并下载；
  - 解析：复用现有 GLB 解析器，限制 150 万三角面。

### 2. 按模型族自动切换参数

| 模型族 | 参数 |
|---|---|
| `tripo-3.1` | `face_limit`（high 档 1,500,000）、`geometry_quality=detailed`、`texture_quality=detailed`、`pbr`、`texture` |
| `tripo-3-p1` | `face_limit` 20,000、`pbr`、`texture` |
| `tripo-3-p2` | `face_limit` 50,000、`pbr`、`texture` |
| `hy-3d-3.x` | `generate_type=Normal`、`face_count`（high 档 1,000,000）、`enable_pbr` |
| 其他市场模型 | `prompt` + `result_format=GLB` + `enable_pbr` |

质量档位 `draft` / `standard` / `high` 只改变面数与纹理预算，不改变协议；默认 `high`。

### 3. 智能体路由

- `_analyze_model3d()` 在满足“有文字 + 已配置三维 Key + 文生 3D 可用 + 非太阳系”时走生成式；
- 草稿只保留一个盲文标题，不再生成部件名；
- 前端生成式草稿只展示标题、高度、底座三项，不展示部件表；
- 太阳系目录和 `TOUCHLEARN_3D_GENERATIVE=0` 仍走本地确定性编译器。

### 4. 本地统一后处理

`compose_printable()` 继续作为唯一后处理器：

- 按目标高度缩放并居中，底面贴床；
- 加圆盘/圆角矩形底座和短支撑柱；
- 正面定义标题牌 + 二维码位所在的一侧；
- 中文盲文按 GF 0019-2018 编码；
- 输出 `model.stl`、`preview.svg`、`model_spec.json`、`generation.json`、`decision_trace.json`、`model_view.html`；
- 合规报告新增 `generation.provider`、`generation.texture`、`generation.mesh_repair`、`generation.orientation`、`generation.scale`；
- 生成网格的开放边/退化面降为 `NEEDS_REVIEW`，不谎称完全合规。

### 5. 配置与文档

新增环境变量：

- `TOUCHLEARN_3D_TEXT_MODEL`（默认 `tripo-3d-3.1`）
- `TOUCHLEARN_3D_QUALITY`（`draft` / `standard` / `high`）
- `TOUCHLEARN_3D_GENERATIVE`（`0` 关闭生成式）

同步更新 `.env.example`、`deploy/.env.example`、README、`docs/deploy.md`、`docs/deploy-tokenhub.md`、`docs/providers.md`、`docs/roadmap.md`、`docs/submission.md`。

### 6. 按控制台示例对齐图生 3D（后续补充）

TokenHub 控制台给出的示例比先前实现更具体，适配层据此调整：

- `hy-3d-express`、`hy-3d-3.1`：文字请求按示例发送 `model` + `prompt`。
- `tripo-3d-p1`：文字严格发送 `{model, prompt}`；单图严格发送 `{model, input}`；多视图严格发送 `{model, inputs: [{front, left, back, right}]}`。不再给 P1 附加未在示例中出现的 `face_limit` / `result_format`。
- `hy-3d-3.1` 图生仍走 `image_base64`（混元路线），Tripo 路线走 `input`。
- 图生默认模型改为 `tripo-3d-p1`，因为它同时支持单图、多视图和文字，且请求格式有控制台示例可对。
- 多视图按文件名识别方向（`front/left/back/right` 或 `前/左/后/右`）；无方向词时按上传顺序填充四个 slot。
- 公网部署时把照片临时发布到 `/files/<job>/input-N.<ext>`，把 HTTPS URL 交给 TokenHub；本机 `127.0.0.1` 预览自动退回 data URI。重建结束立即删除临时照片。
- `hy-3d-motion` 明确拒绝：它输出动画，不是可打印静态网格。
- 查询结果解析同时接受 `data` / `result` 的列表和映射形式，并按 URL 后缀兜底识别 `glb/gltf/obj`。

## 检查清单

- [x] 对比 TokenHub 3D 提交/查询接口文档：`prompt`、`result_format=GLB`、`face_count`、`face_limit`、`geometry_quality`、`texture_quality`、`pbr`、`texture`
- [x] 检查模型族分支：Tripo 3.x / P1 / P2、HY-3D、未知市场模型
- [x] 检查图生 3D 旧路径未回归：`_tokenhub_reconstruct()` 仍使用图片字段
- [x] 检查生成式草稿前端：标题、高度、底座、标签与证据面板
- [x] 检查本地后处理：底座、标题牌、盲文、方向、二维码、可打印性
- [x] 新增 mock 测试：Tripo / HY-3D 请求体、质量档位、文生 3D 提交与 GLB 解析、智能体生成式草稿与构建
- [x] 全量测试：`282 passed, 32 subtests passed`
- [x] `node --check src/touchlearn/agent/static/app.js` 通过

## 已知限制

- 本次没有真实调用付费接口（环境未提供 Key），TokenHub 链路用本机 HTTP mock 验证；上线前需要用真实 Key 跑一次机械臂、水杯、地球仪三个提示词。
- 生成网格可能有开放边或退化面，切片前需要修复；报告会标为 `NEEDS_REVIEW`。
- `high` 档 150 万面在 2 GiB 服务器上并发生成时内存压力较大，建议并发高时降到 `standard` 或 `draft`。
- 第三方网格没有部件语义，因此只做统一标题和底座图例，不承诺把部件名刻到对应表面。

## 经验教训

1. **接口能力要定期复核**。一条过时的“只能收图片”结论会让整个产品路线绕远路。
2. **生成与确定性必须分层**。生成模型负责外观，本地代码负责盲文、方向、合规和可追溯性，边界越清楚，越不容易在答辩时说不清。
3. **不要为了“看起来有标注”而刻猜出来的名字**。底座图例和统一标题是诚实且可用的方案。
4. **质量档位要可降级**。高面数带来细节，也带来内存和等待时间，必须给部署环境留退路。
