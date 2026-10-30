# 部署与多设备访问

触见是一个标准的 Python HTTP 服务，前端是静态文件。它不需要 Node 构建，也不依赖数据库。本文给出三条从轻到重的路径：

| 场景 | 入口 | 适合 | 代价 |
|---|---|---|---|
| 同一 Wi-Fi | `http://<电脑局域网 IP>:8765` | 比赛现场、手机扫码 | 电脑必须开着，手机和电脑同一网络 |
| 临时公网 | `https://*.trycloudflare.com` | 异地演示、发给评委快速体验 | 地址随进程变化，电脑休眠即失效 |
| 固定公网（推荐） | `deploy/` + Docker Compose + Caddy + 自己的服务器 | 长期展示、随时访问、自动 HTTPS | 需要一台云服务器和域名；ECS/VPS 选型见 [deploy/README.md](../deploy/README.md) |
| 固定公网（托管平台） | Render / Railway | 不想管服务器、先看效果 | 免费实例会休眠，产物盘可能不持久 |

> 无论走哪条路，公网暴露时都必须设置 `TOUCHLEARN_ACCESS_CODE`。分析和生成接口需要登录；已经生成的产物链接按不可猜测的作业 ID 公开，方便手机扫码，不需要登录。

## 一、同一 Wi-Fi（零配置）

```bash
python scripts/run_agent.py --port 8765
```

- 默认监听 `0.0.0.0`，终端会打印本机和局域网地址。
- 手机浏览器打开 `http://<局域网 IP>:8765`。
- Windows 第一次运行可能弹出防火墙提示，放行“专用网络”即可。
- 二维码基础地址会自动使用手机访问时的 Host。用局域网 IP 打开，二维码就指向局域网 IP。

如果扫码打不开：

1. 确认手机和电脑在同一 Wi-Fi，且没有开启 AP 隔离；
2. 用 `ipconfig` 核对电脑当前 IPv4；
3. 临时关闭防火墙测试，或添加入站规则放行 TCP 8765；
4. 换端口：`python scripts/run_agent.py --port 8877`。

## 二、临时公网（Cloudflare Quick Tunnel）

电脑上安装一次 cloudflared：

```powershell
winget install --id Cloudflare.cloudflared
```

然后：

```bash
python scripts/run_public.py
```

脚本会：

1. 生成或复用访问码，写入仓库根目录的 `.touchlearn_access`（已 gitignore，权限尽量设为仅当前用户可读）；
2. 启动触见服务；
3. 启动 Cloudflare Quick Tunnel；
4. 打印公网地址和访问码，并写入 `outputs/public_access.json`。

典型输出：

```text
==========================================================
  公网地址（任何设备可访问）
  https://random-words.trycloudflare.com
  访问码: TL-XXXXXXXX
==========================================================
```

常用参数：

```bash
python scripts/run_public.py --new-code          # 换一个访问码
python scripts/run_public.py --port 8877         # 换端口
python scripts/run_public.py --no-tunnel         # 只开局域网
python scripts/run_public.py --access-code XXX   # 临时指定访问码
```

限制必须说清楚：

- Quick Tunnel 地址每次重启都变；
- 电脑休眠、断网或关闭终端后链接立即失效；
- Cloudflare 不保证该地址的长期可用性；
- 它只适合演示，不是“永久在线”。

如果只要一个临时公网地址做验收，这一条就够了。要真正“随时可访问”，继续看下一节。

## 三、固定公网（Render + Docker）

> 如果你已经有自己的云服务器，直接看 [deploy/README.md](../deploy/README.md)：里面包含 ECS/VPS 选型、配置建议、备案说明、一键 Compose、Caddy 自动 HTTPS、备份与排障。目标是“中国大陆任意网络都能打开”时，看 [deploy/mainland.md](../deploy/mainland.md)，按大陆服务器 + 备案域名 + 443 的路径走。本节是托管平台路线，适合不想维护服务器的场景。

仓库已经包含 `Dockerfile`、`.dockerignore`、`render.yaml` 和 `Procfile`。Render 可以直接读取 Blueprint。

### 3.1 前置条件

1. 代码在 GitHub 仓库里（当前远程：`CAG1218/touchlearn-fudan-Team_Stella`）；
2. 有 Render 账号；
3. 决定访问码。建议用 `python -c "import secrets; print('TL-'+secrets.token_hex(4).upper())"` 生成，不要用弱口令。

### 3.2 Render Blueprint

1. 登录 Render Dashboard；
2. **New → Blueprint**；
3. 选择仓库，Render 会读取 `render.yaml`；
4. 在环境变量页填写：
   - `TOUCHLEARN_ACCESS_CODE`：必填；
   - `TOUCHLEARN_LLM_*` / `TOUCHLEARN_VLM_*`：按需；
   - `TOUCHLEARN_PUBLIC_URL`：只有绑定自定义域名时才填，例如 `https://touch.example.com`。留空时按请求 Host 自动推导，二维码会跟着当前访问地址走。
5. Apply，等待构建完成；
6. 打开 `https://<service>.onrender.com`，输入访问码。

`render.yaml` 使用 Docker 运行时，健康检查路径是 `/api/health`。免费实例在闲置后休眠，下一次打开需要几十秒冷启动；这是 Render 的套餐行为，不是触见卡死。

### 3.3 直接使用 Docker

本地或任意 VPS：

```bash
docker build -t touchlearn .
docker run --rm -p 8765:8765 \
  -e TOUCHLEARN_ACCESS_CODE=TL-XXXXXXXX \
  -e TOUCHLEARN_LLM_PROVIDER=tokenhub \
  -e TOUCHLEARN_LLM_KEY=sk-... \
  -e TOUCHLEARN_LLM_MODEL=deepseek-v4.1-flash \
  -v touchlearn-outputs:/app/outputs \
  touchlearn
```

浏览器打开 `http://127.0.0.1:8765`。前面放 Nginx 时，记得转发 `Host` 和 `X-Forwarded-Proto`，否则二维码可能指回内网地址；Caddy 的 `reverse_proxy` 默认会保留 Host 并补全转发头，仓库里的 Caddyfile 已验证。

Railway/Heroku 一类平台可以使用 `Procfile`；平台会注入 `PORT`，服务已适配。

### 3.4 持久化与容量

- 免费容器通常使用临时文件系统，重启/重新部署后 `outputs/` 里的作业会消失，历史二维码链接随之失效。
- 要保留产物，给 `/app/outputs` 挂持久卷（Render 的 Persistent Disk 需要付费实例），或自行把作业目录同步到对象存储。
- 单文件上限 30 MB、单次总计 80 MB、最多 12 个附件；公开实例建议在反向代理再加请求频率限制和磁盘配额。
- 作业产物按 12 位十六进制 ID 公开读取，这是为了让扫码音频页不依赖登录 Cookie；不要把它当成权限边界，敏感内容不要上传。

## 四、接入大模型

不配置也能跑：分类用关键词，函数用正则，场景用 8 个离线模板；但图片理解、自由文字生成场景、复杂图结构抽取会降级。

最少配置一个文本模型：

```dotenv
TOUCHLEARN_LLM_PROVIDER=tokenhub
TOUCHLEARN_LLM_KEY=sk-...
# 可选；留空用预置默认值。这里只是初始默认，用户可在页面模型菜单里切换
TOUCHLEARN_LLM_MODEL=deepseek-v4.1-flash
# 可选：同一 provider 会复用上面的 KEY
TOUCHLEARN_VLM_PROVIDER=tokenhub
TOUCHLEARN_VLM_MODEL=deepseek/deepseek-flash
# 可选：长场景 JSON 用更快的模型并关闭思考，避免被 max_tokens 截断
TOUCHLEARN_SCENE_MODEL=deepseek-v4.1-flash
TOUCHLEARN_LLM_EFFORT=low
TOUCHLEARN_SCENE_EFFORT=off
```

模型与思考强度是**用户可选项**，不是部署时锁死的配置：服务启动后，页面输入框上方的模型按钮会列出当前实例提供的模型，用户可以自行切换；`.env` 里的值只决定初始默认。选择保存在浏览器本地，下一次请求生效，不影响其他设备。

也可以把 `TOUCHLEARN_LLM_PROVIDER` 换成 `deepseek`，模型改为 `deepseek-v4-pro` / `deepseek-flash`。

图片理解需要视觉模型。预置里自带视觉模型的包括：

| 预置 | 文本模型 | 视觉模型 |
|---|---|---|
| `tokenhub` | `deepseek-v4.1-flash` | `deepseek/deepseek-flash` |
| `deepseek` | `deepseek-v4-pro` | `deepseek-flash` |
| `openai` | `gpt-4o-mini` | `gpt-4o-mini` |
| `dashscope` | `qwen-plus` | `qwen-vl-max` |
| `moonshot` | `moonshot-v1-8k` | `moonshot-v1-8k-vision-preview` |
| `zhipu` | `glm-4-flash` | `glm-4v-flash` |
| `ollama` | `qwen2.5:7b` | `qwen2.5vl:7b` |

如果更偏好百炼的 Qwen-VL，也可以文本用 DeepSeek、视觉单独用百炼：

```dotenv
TOUCHLEARN_LLM_PROVIDER=deepseek
TOUCHLEARN_LLM_KEY=sk-...
TOUCHLEARN_VLM_PROVIDER=dashscope
TOUCHLEARN_VLM_KEY=sk-...
TOUCHLEARN_VLM_MODEL=qwen-vl-max
```

完整预置、端点规则和失败行为见 [providers.md](providers.md)。密钥只从环境变量或本机 `.env` 读取，不会写进作业目录，也不会通过 `/api/providers` 返回。

## 五、语音与 PDF 的平台差异

- 本地 Windows：使用系统 SAPI 生成 `narration.wav`，当前机器有 `Microsoft Huihui Desktop` 中文语音。
- Linux 容器：没有 SAPI，WAV 生成会失败并记录到 `manifest.audio.tts_message`；前端会自动切换为浏览器 `speechSynthesis` 朗读讲解稿。讲解稿文本、音频页和二维码仍然可用。
- PDF：Docker 镜像安装了 `pypdf`，能读文字型 PDF 的文字层。扫描件没有文字层时不会假装识别，需要配置视觉模型并上传关键页图片；当前不做服务端 PDF 光栅化。

## 六、排障

### 页面显示“待登录”或 401

设置了 `TOUCHLEARN_ACCESS_CODE`，但浏览器没有登录 Cookie。输入访问码；忘记时查看 `.touchlearn_access`（本地）或 Render 环境变量。

### 页面能开，但二维码指向 127.0.0.1

服务前面有反向代理，但没有传 `Host` / `X-Forwarded-Host`。修代理配置，或显式设置 `TOUCHLEARN_PUBLIC_URL`。

### 公网地址打开是 502

cloudflared 还在，但本地服务已经退出。看终端日志；重新运行 `python scripts/run_public.py`。

### 生成一直停在“建模中”

看终端日志和 `outputs/jobs/<id>/error.txt`。常见原因是上传文件损坏、STL 非流形、或磁盘写满。生成是单进程串行锁，多个作业会排队。

### 免费实例冷启动很慢

Render 免费实例会休眠。升级实例，或改用常驻 VPS；不建议用“定时 ping”绕过平台策略。

## 七、当前还需要人来做的事

1. **大模型 API Key**：没有也能离线演示；要演示照片/复杂教材理解，需要至少一个文本模型，最好再加一个视觉模型。
2. **固定托管环境**：渲染平台或自有服务器任选其一。仓库侧配置已经准备好，账号、域名和备案需要人来完成；自有服务器的完整步骤见 [deploy/README.md](../deploy/README.md)。
3. **自定义域名（可选）**：没有域名也能用 `onrender.com` 子域；有域名时在平台绑定后设置 `TOUCHLEARN_PUBLIC_URL`。
4. **实体打印与盲人用户测试**：仍未完成。内部 PASS 只代表几何和工艺检查通过，不等于国家标准认证，也不等于真实触觉可用性验证。
