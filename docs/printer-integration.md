# 3D 打印机直连（可选扩展）

这份文档说明“把生成好的 STL 直接发送到打印机”这个扩展：它做什么、不做什么、怎么开、怎么验证。主链路（理解 → 草稿 → 生成 → 合规检查 → 下载）不依赖它，关掉这个扩展不影响任何现有功能。

## 1. 定位

TouchLearn 的核心产物是一个经过确定性检查的 `model.stl`。打印扩展只负责最后一公里：

1. 读取作业目录里的 `model.stl`，解析三角面数量、文件体积和包围盒；
2. 用打印机的实际打印床尺寸做**贴合检查**（四周各留 3 mm）；
3. 通过 OctoPrint 或 Klipper / Moonraker 的 HTTP 接口上传并请求开始打印；
4. 把检查项、传输结果和文件哈希写进作业目录的 `print.json`，便于复核。

**它不做切片。** STL 是几何文件，打印机只吃 G-code。OctoPrint 只有在配置了切片配置（Slicing Profile）时才能把 STL 直接切片并打印；Moonraker 通常需要先把 STL 交给切片器。因此当前的实现是“把 STL 送到打印服务器并请求打印”，而不是“在本地替用户切片”。等本地切片器（CuraEngine / PrusaSlicer CLI）接入后，才能保证任何一台只吃 G-code 的机器直接开打。这一条在提交材料里要如实写。

## 2. 安全设计

打印是物理动作：喷头会升温到 200 °C 以上，打印床会移动。所以这个功能默认是**关闭的**，并且需要两道互相独立的闸门：

| 闸门 | 位置 | 作用 |
|---|---|---|
| `TOUCHLEARN_PRINT_ALLOW=1` | 服务器环境变量 | 由管理员决定这台实例是否允许真实打印。不设置时所有请求都只做演练，**不会建立任何网络连接**。 |
| `confirm: true` | 用户请求体 + 界面复选框 | 用户看到检查结果后显式确认。缺少这个字段的请求直接 400，不会上传文件。 |

另外几条约束：

- API 只接受配置里存在的 `printer_id`，浏览器不能传任意 URL，避免把服务器变成内网探测代理；
- 打印机密钥只放在 `X-Api-Key` 请求头里，不会出现在响应、`print.json`、日志或错误信息中；
- 只允许发送 `model.stl` 和 `plate_model.stl`，文件名固定，杜绝路径穿越；
- 合规报告 `printable=false` 时，`compliance` 检查项直接阻断发送；
- 同一作业同一时间只允许一个打印请求，防止双击排队两次；
- 超过 200 MB 或 400 万面的 STL 直接拒绝，避免把打印服务器撑爆。

## 3. 支持的打印服务器

| kind | 协议 | 上传 | 开始打印 |
|---|---|---|---|
| `octoprint` | OctoPrint REST API | `POST /api/files/local`（multipart，`select=true`、`print=true`） | 上传时带 `print=true`，由 OctoPrint 切片后开始 |
| `moonraker` | Klipper / Moonraker HTTP API | `POST /server/files/upload`（multipart，`root=gcodes`） | `POST /printer/print/start?filename=...` |

两种都用标准库 `http.client` 实现，没有新增依赖。Bambu、PrusaLink 等私有协议暂时不做；新增一种大约需要 30 行，遵循同样两个函数（上传 + 开始）。

## 4. 配置

### 4.1 单台打印机

在 `.env` 或 docker-compose 的环境变量里：

```dotenv
TOUCHLEARN_PRINTER_KIND=octoprint
TOUCHLEARN_PRINTER_URL=http://192.168.1.50
TOUCHLEARN_PRINTER_KEY=在 OctoPrint 设置里生成的应用密钥
TOUCHLEARN_PRINTER_BED=220x220x250
TOUCHLEARN_PRINTER_LABEL=实验室 Prusa
# 确认要真实打印时再加：
TOUCHLEARN_PRINT_ALLOW=1
```

`TOUCHLEARN_PRINTER_BED` 接受 `220x220x250`、`220 220 250` 或 JSON 数组。

### 4.2 多台打印机

```dotenv
TOUCHLEARN_PRINTERS=[{"id":"prusa","label":"Prusa MK4","kind":"octoprint","url":"http://192.168.1.50","api_key":"...","bed_mm":[250,210,220]},{"id":"voron","label":"Voron 2.4","kind":"moonraker","url":"http://192.168.1.60","bed_mm":[350,350,330]}]
```

`id` 只能包含字母、数字、下划线和短横线，不能重复。`url` 必须是 `http://` 或 `https://`，不能把账号密码写进 URL。

### 4.3 演练模式

什么都不配置时，界面上的“发送到打印机”会提示去配置；配置了打印机但不设 `TOUCHLEARN_PRINT_ALLOW` 时，界面会显示“演练模式”，仍然跑完整的贴合检查、文件检查和上传体积计算，但不连接打印机。设置 `TOUCHLEARN_PRINT_DRY_RUN=1` 可以强制回到演练模式，用于演示。

## 5. API

全部需要登录（与 `/api/build` 一致）。

### `GET /api/printers`

```json
{
  "ok": true,
  "allow_print": false,
  "mode": "rehearsal",
  "printers": [
    {"id": "prusa", "label": "Prusa MK4", "kind": "octoprint",
     "host": "192.168.1.50", "secure": false,
     "bed_mm": [250, 210, 220], "key_configured": true}
  ]
}
```

### `POST /api/print/plan`

请求：`{"job_id": "...", "printer_id": "prusa", "file": "model.stl"}`

返回检查项，不发送文件：

```json
{
  "ok": true,
  "printer": {"id": "prusa", "label": "Prusa MK4"},
  "file": {"name": "model.stl", "bytes": 1234567, "triangles": 24000,
           "size_mm": [80.0, 60.0, 20.0], "sha256": "..."},
  "checks": [
    {"id": "format", "label": "文件格式", "ok": true, "detail": "二进制 STL，三角面数量与文件长度一致"},
    {"id": "size", "label": "文件体积", "ok": true, "detail": "1.2 MB，上限 200 MB"},
    {"id": "triangles", "label": "三角面数量", "ok": true, "detail": "24,000 面，上限 4,000,000 面"},
    {"id": "bed_fit", "label": "打印床尺寸", "ok": true, "detail": "80.0 × 60.0 × 20.0 mm；打印床 250 × 210 × 220 mm，四周留边 3 mm"}
  ],
  "rotate_deg": 0,
  "mode": "rehearsal"
}
```

### `POST /api/print`

请求必须带确认：

```json
{"job_id": "...", "printer_id": "prusa", "file": "model.stl", "confirm": true}
```

状态码：`200` 已发送或演练通过；`400` 缺少确认或参数非法；`404` 作业/打印机不存在；`409` 检查未通过、没有配置打印机、同一作业正在发送；`502` 打印机拒绝或连不上。

### `GET /api/jobs/<job_id>/print`

返回最近一次打印记录（即 `print.json`），没有记录时 404。

## 6. 作业目录里的 `print.json`

```json
{
  "ok": true,
  "state": "rehearsed",
  "printer": {"id": "prusa", "label": "Prusa MK4", "kind": "octoprint",
              "host": "192.168.1.50", "bed_mm": [250, 210, 220], "key_configured": true},
  "file": {"name": "model.stl", "bytes": 1234567, "triangles": 24000,
           "size_mm": [80.0, 60.0, 20.0], "sha256": "..."},
  "checks": [ ... ],
  "rotate_deg": 0,
  "dry_run": true,
  "message": "演练通过：将向 Prusa MK4 上传 1.2 MB，本次没有连接打印机。",
  "started_at": "2026-11-07T15:00:00+00:00",
  "finished_at": "2026-11-07T15:00:00+00:00",
  "transport": {"kind": "octoprint", "planned_bytes": 1235000, "sent": false}
}
```

`state` 有四种：`rehearsed`（演练）、`sent`（已发送）、`blocked`（检查未通过）、`failed`（传输失败）。记录里不含任何密钥。

## 7. 已验证与未验证

**已验证（自动化测试，`tests/test_printers.py`，33 项）**

- 环境变量解析：单台、多台、字符串/数组打印床、非法 kind/url/重复 id/缺失密钥全部报错；
- 公开展示的打印机信息不含密钥；
- 二进制 STL 的三角面、体积、包围盒解析；ASCII STL 被拒绝；
- 打印床贴合检查、留边、需要旋转 90° 的提示；
- 合规报告 `printable=false` 阻断；
- 用本地假打印机验证 OctoPrint 上传（multipart、`select`/`print` 字段、`X-Api-Key`）和 Moonraker 两步流程；
- 403 与端口不可达分别映射为 `printer_rejected` 和 `printer_unreachable`；
- 演练模式不建立任何连接；
- 缺少确认时 400，未登录时 401，未配置打印机时 409；
- `print.json` 落盘并可通过 `GET /api/jobs/<id>/print` 读取。

**未验证（不要在这些点上做承诺）**

- 没有连接过真实打印机，`X-Api-Key` 与真实 OctoPrint / Moonraker 的兼容性只在假服务器上验证；
- OctoPrint 未配置切片配置时只会把 STL 存进队列，不会自动开打；这一点取决于用户自己的 OctoPrint 设置；
- 没有轮询打印进度、没有断点续传、没有超时重试；
- Moonraker 的 `print/start` 只是按协议发起请求；Klipper 只执行 G-code，未接入切片器时上传的 STL 不会真正开打，目前只把错误原文回显；
- HTTPS 使用系统默认证书校验，自签名证书的打印机需要换 HTTP 或给系统装根证书；
- 多进程部署时“同一作业只允许一个请求”的锁只在单进程内有效。

## 8. 后续路线

1. 接入本地切片器（CuraEngine / PrusaSlicer CLI），用 `manifest.json` 里的喷嘴、层高、墙线、填充参数生成 G-code，再把 G-code 发给打印机；
2. 打印任务状态轮询（`/api/job`、`/printer/objects/query`），把进度和温度显示在作业卡上；
3. 材料、颜色、喷嘴直径的作业级覆盖项；
4. 队列与重试，以及断网后的幂等重发；
5. 管理员可见的审计日志（谁在什么时候打印了哪个作业）。

## 9. 提交材料口径

> 触见同时保留了“直接下发打印”的可选扩展：生成完成的 STL 可以在作业卡里选择已配置的 OctoPrint 或 Klipper / Moonraker 打印机，服务端先做文件格式、文件体积、三角面数量、打印床贴合和合规结论五项检查，用户确认后再上传并请求开始打印。该扩展默认关闭，必须由管理员显式开启；关闭时只做演练，方便在没有硬件的机器上演示。当前已完成协议对接、安全闸门和 33 项自动化测试，尚未连接真实打印机，也未完成本地切片，因此初赛阶段只作为设计能力说明，不作为现场演示承诺。
